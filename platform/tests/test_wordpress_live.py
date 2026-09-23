"""Opt-in tests against the separate loopback-only Docker fixtures.

Start deploy/compose.integration.yaml, then set FORGE_LIVE_WP=1.
Production transports are NOT weakened; the test transport maps only fixture hosts.
"""
import json
import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.connectors.wordpress import WordPressClient
from app.connectors.errors import SourceConflict
from app.connectors.errors import ProtectedField
from app.connectors.errors import ConnectorError
from app.connectors.woocommerce import WooCommerceClient
from test_platform import platform

pytestmark=pytest.mark.skipif(os.environ.get('FORGE_LIVE_WP')!='1',reason='Explicit isolated WordPress integration opt-in required')
ROOT=Path(__file__).resolve().parents[1]
COMPOSE_FILE=ROOT/'deploy/compose.integration.yaml'


def _compose(*arguments):
    return ['docker','compose','-f',str(COMPOSE_FILE),*arguments]


def _run_compose(*arguments):
    try:
        return subprocess.run(_compose(*arguments),cwd=ROOT,capture_output=True,text=True,timeout=180)
    except (OSError,subprocess.TimeoutExpired) as exc:
        pytest.fail(f'Isolated WordPress Compose fixture could not be controlled ({type(exc).__name__})')


@pytest.fixture(scope='module',autouse=True)
def integration_stack():
    """Own only the named loopback fixture when the opt-in suite starts it."""
    running = True
    for service in ('wp','woo'):
        status = _run_compose('ps','--status','running','-q',service)
        if status.returncode != 0 or not status.stdout.strip():
            running = False
            break
    owns_stack = not running
    if owns_stack:
        started = _run_compose('up','-d','--remove-orphans')
        if started.returncode:
            pytest.fail('The isolated WordPress/WooCommerce Compose fixture failed to start')
    try:
        # The database healthcheck only proves MariaDB is accepting connections;
        # wait until both WordPress containers can load their bootstrap before
        # configure() invokes the fixture installer.
        for service in ('wp','woo'):
            for _ in range(90):
                ready = _run_compose('exec','-T',service,'php','-r',
                                     "require '/var/www/html/wp-load.php'; echo 'ready';")
                if ready.returncode == 0 and ready.stdout.strip() == 'ready':
                    break
                time.sleep(1)
            else:
                pytest.fail(f'Isolated {service} fixture did not become ready')
        yield
    finally:
        if owns_stack:
            _run_compose('down','--remove-orphans')


class FixtureTransport(httpx.AsyncBaseTransport):
    """Only these two test domains can reach the loopback fixture ports."""
    def __init__(self):
        self.inner=httpx.AsyncHTTPTransport(retries=0,trust_env=False)

    async def handle_async_request(self,request):
        ports={'wordpress.fixture.test':18090,'store.fixture.test':18091}
        if request.url.host not in ports:
            raise ValueError('Request escaped the isolated fixture allowlist')
        target=request.url.copy_with(scheme='http',host='127.0.0.1',port=ports[request.url.host])
        headers={**request.headers,'X-Forwarded-Proto':'https'}
        forwarded=httpx.Request(request.method,target,headers=headers,stream=request.stream,extensions=request.extensions)
        return await self.inner.handle_async_request(forwarded)

    async def aclose(self):
        await self.inner.aclose()


class LostResponseTransport(FixtureTransport):
    """Allow the real server write, then simulate loss of its successful reply."""
    def __init__(self,operation):
        super().__init__()
        self.operation=operation
        self.dropped=False

    async def handle_async_request(self,request):
        body=json.loads(await request.aread()) if request.method=='POST' else {}
        response=await super().handle_async_request(request)
        is_create=request.url.path.rstrip('/')=='/wp-json/wp/v2/posts' and request.method=='POST'
        is_publish=request.method=='POST' and body.get('status')=='publish'
        if not self.dropped and ((self.operation=='create' and is_create) or (self.operation=='publish' and is_publish)) and response.status_code<300:
            self.dropped=True
            await response.aread()
            await response.aclose()
            raise httpx.ReadTimeout('Simulated lost successful fixture response',request=request)
        return response


class FixtureConfig(dict):
    def __repr__(self):
        return '<isolated fixture credentials redacted>'


def configure(service='wp',mode='native',connector=False):
    command=['docker','compose','-f','deploy/compose.integration.yaml','exec','-T','--user','www-data',service,
             'php','-d','memory_limit=512M','/fixture/setup.php',mode]
    if connector:
        command.append('connector')
    result=subprocess.run(command,cwd=ROOT,capture_output=True,timeout=240)
    if result.returncode:
        pytest.fail(f'Isolated fixture initialization failed with exit code {result.returncode}; no credential output logged')
    try:
        return FixtureConfig(json.loads(result.stdout))
    except ValueError:
        pytest.fail('Fixture did not return its expected credential envelope; output withheld')


@pytest.fixture(scope='module')
def native_site():
    return configure()


@pytest.mark.asyncio
async def test_native_draft_publish_public_verify_and_restore(native_site):
    article={'title':'Independent repair preparation guide '+uuid4().hex[:8],
             'body':'<p>Bring a clear description of the repair request and any useful photographs.</p>',
             'author_id':native_site['author_id']}
    async with WordPressClient(native_site['origin'],native_site,transport=FixtureTransport()) as client:
        capability=await client.discover()
        assert capability['native']['create'] is True
        draft=await client.create_draft(article,'fixture-'+uuid4().hex)
        assert draft['status']=='draft'
        assert draft['body']==article['body']
        published=await client.publish(str(draft['id']))
        assert published['status']=='publish'
        async with httpx.AsyncClient(transport=FixtureTransport()) as public:
            response=await public.get(published['url'])
            assert response.status_code==200
            assert 'clear description of the repair request' in response.text
        restored=await client.restore(draft['resource_key'],draft,published['source_hash'])
        assert restored['status']=='draft'
        async with httpx.AsyncClient(transport=FixtureTransport()) as public:
            response=await public.get(published['url'])
            assert response.status_code==404


@pytest.mark.asyncio
async def test_native_source_conflict_leaves_external_edit_intact(native_site):
    async with WordPressClient(native_site['origin'],native_site,transport=FixtureTransport()) as client:
        draft=await client.create_draft({'title':'Conflict fixture '+uuid4().hex[:8],'body':'<p>Original text.</p>','author_id':native_site['author_id']},'fixture-'+uuid4().hex)
        changed=await client.update(draft['resource_key'],{'body':'<p>External editor replacement.</p>'},draft['source_hash'])
        with pytest.raises(SourceConflict):
            await client.update(draft['resource_key'],{'body':'<p>Stale optimization.</p>'},draft['source_hash'])
        observed=await client.read(draft['resource_key'])
        assert observed['body']==changed['body']


@pytest.fixture(scope='module')
def store_site():
    return configure('woo','woo')


@pytest.mark.asyncio
async def test_woocommerce_editorial_roundtrip_preserves_commerce(store_site):
    key='products:'+str(store_site['product_id'])
    async with WooCommerceClient(store_site['origin'],store_site,transport=FixtureTransport()) as client:
        await client.discover()
        original=await client.read(key)
        protected=['price','regular_price','sale_price','sku','stock_quantity','stock_status','manage_stock','variations']
        commerce={field:original['raw'].get(field) for field in protected}
        changed=await client.update(key,{'description':'<p>Verified editorial revision for the isolated catalog item.</p>'},original['source_hash'])
        assert {field:changed['raw'].get(field) for field in protected}==commerce
        assert 'Verified editorial revision' in changed['raw']['description']
        for field in ['regular_price','stock_quantity','sku']:
            with pytest.raises(ProtectedField):
                await client.update(key,{field:'forbidden'},changed['source_hash'])
        restored=await client.restore(key,original,changed['source_hash'])
        assert restored['raw']['description']==original['raw']['description']
        assert {field:restored['raw'].get(field) for field in protected}==commerce


@pytest.mark.asyncio
async def test_inventory_matches_real_wordpress_and_woocommerce_collection_totals(native_site, store_site):
    # Compare the public inventory entry point with each server's count headers.
    # These are loopback fixtures, with no production credentials or routing.
    for client_type, config, endpoints in (
        (WordPressClient, native_site, {'post': '/wp-json/wp/v2/posts', 'page': '/wp-json/wp/v2/pages', 'author': '/wp-json/wp/v2/users'}),
        (WooCommerceClient, store_site, {'product': '/wp-json/wc/v3/products', 'category': '/wp-json/wc/v3/products/categories'}),
    ):
        async with client_type(config['origin'], config, transport=FixtureTransport()) as connector:
            records = await connector.inventory()
            for resource_type, endpoint in endpoints.items():
                _, response = await connector._json_request('GET', endpoint, params={'context': 'edit', 'per_page': 1})
                expected = int(response.headers['X-WP-Total'])
                assert sum(record['resource_type'] == resource_type for record in records) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('drop_response',['none','create','publish','process_exit_after_create','external_title_edit'])
async def test_platform_publication_lifecycle_against_real_wordpress(platform,native_site,monkeypatch,drop_response):
    from sqlalchemy import select
    from app import workflows
    from app.config import settings
    from app.models import Article,Job,Page,Publication,Site
    from app.policies import create_policy
    from app.network import fetch
    client,factory,site_id=platform
    monkeypatch.setattr(settings,'GLOBAL_PAUSE',False)
    class ProcessExit(BaseException):
        pass
    interrupted=False
    class RecoveryClient(WordPressClient):
        async def create_draft(self,article,operation_key):
            nonlocal interrupted
            created=await super().create_draft(article,operation_key)
            if drop_response=='process_exit_after_create' and not interrupted:
                interrupted=True
                raise ProcessExit('Simulated worker exit after remote success, before local id commit')
            if drop_response=='external_title_edit':
                await self.update(created['resource_key'],{'title':'External editor title must be preserved'},created['source_hash'])
            return created
    async def fixture_client(db,site,kind='wordpress'):
        return RecoveryClient(native_site['origin'],native_site,transport=LostResponseTransport(drop_response))
    async def public_fetch(url):
        return await fetch(url,transport=FixtureTransport())
    monkeypatch.setattr(workflows,'client_for',fixture_client)
    monkeypatch.setattr(workflows,'fetch',public_fetch)
    with factory() as db:
        site=db.get(Site,site_id)
        site.origin=native_site['origin']
        site.paused=False
        site.facts={'business_name':'Independent Workshop','services':['Repairs'],'confirmed_sources':[{'url':native_site['origin']+'/about','title':'Fixture business facts'}]}
        create_policy(db,site,None,{'enabled':True,'allowed_actions':['publish'],'author_id':native_site['author_id']})
        article=Article(site_id=site_id,title='Preparing for a workshop repair visit '+uuid4().hex[:8],slug='workflow-'+uuid4().hex,
            body='<p>Independent Workshop provides Repairs.</p>',author_id=native_site['author_id'],
            sources=site.facts['confirmed_sources'],brief={'generation':{'kind':'fixture_authored','source':'fixture business facts'}})
        db.add(article)
        db.commit()
        job=Job(payload={'article_id':article.id})
        if drop_response=='process_exit_after_create':
            with pytest.raises(ProcessExit):
                await workflows.publish(db,site,job)
            db.rollback()
            existing=db.scalar(select(Publication).where(Publication.article_id==article.id))
            assert existing.snapshot['create_started']
            assert existing.remote_id is None
        if drop_response=='external_title_edit':
            with pytest.raises(SourceConflict):
                await workflows.publish(db,site,job)
            publication=db.scalar(select(Publication).where(Publication.article_id==article.id))
            async with await fixture_client(db,site) as remote:
                observed=await remote.read('posts:'+publication.remote_id)
            assert observed['title']=='External editor title must be preserved'
            assert observed['status']=='draft'
            assert article.status=='review_needed'
            return
        result=await workflows.publish(db,site,job)
        db.commit()
        assert result['status']=='published'
        assert article.status=='published'
        page=db.scalar(select(Page).where(Page.site_id==site_id))
        assert page.managed and page.enrolled
        publication=db.scalar(select(Publication).where(Publication.article_id==article.id))
        assert publication.snapshot['draft']['status']=='draft'
        assert (await workflows.publish(db,site,job))==result
        async with httpx.AsyncClient(transport=FixtureTransport(),auth=(native_site['username'],native_site['application_password'])) as remote:
            matches=await remote.get(native_site['origin']+'/wp-json/wp/v2/posts',params={'slug':article.slug,'context':'edit','status':'any'})
            assert matches.status_code==200
            assert len(matches.json())==1
        restored=await workflows.rollback(db,site,job)
        db.commit()
        assert restored['status']=='rolled_back'
        assert article.status=='rolled_back'


@pytest.mark.asyncio
async def test_native_connector_metadata_is_rendered_and_restorable():
    from bs4 import BeautifulSoup
    from app.workflows import protected_html
    site=configure(connector=True)
    async with WordPressClient(site['origin'],site,transport=FixtureTransport()) as client:
        draft=await client.create_draft({'title':'Metadata fixture '+uuid4().hex[:8],'body':'<h2>Repair preparation</h2><p>Keep these original instructions unchanged.</p>','author_id':site['author_id']},'fixture-'+uuid4().hex)
        published=await client.publish(str(draft['id']))
        before=await client.read(published['resource_key'])
        async with httpx.AsyncClient(transport=FixtureTransport()) as public:
            original_html=(await public.get(published['url'])).text
        after=await client.update(before['resource_key'],{'seo':{'title':'A complete repair preparation guide','description':'Prepare for your repair visit with these practical instructions.'}},before['source_hash'])
        assert after['metadata']['seo']['forgeseo']['title']=='A complete repair preparation guide'
        async with httpx.AsyncClient(transport=FixtureTransport()) as public:
            changed_html=(await public.get(published['url'])).text
        soup=BeautifulSoup(changed_html,'html.parser')
        assert soup.title.get_text()=='A complete repair preparation guide'
        assert len(soup.select('meta[name="description"]'))==1
        assert soup.select_one('meta[name="description"]')['content']=='Prepare for your repair visit with these practical instructions.'
        assert protected_html(original_html)==protected_html(changed_html)
        restored=await client.restore(before['resource_key'],before,after['source_hash'])
        assert restored['source_hash']==before['source_hash']
        async with httpx.AsyncClient(transport=FixtureTransport()) as public:
            restored_html=(await public.get(published['url'])).text
        assert BeautifulSoup(restored_html,'html.parser').title.get_text()==BeautifulSoup(original_html,'html.parser').title.get_text()
        assert protected_html(restored_html)==protected_html(original_html)
        await client.restore(draft['resource_key'],draft,restored['source_hash'])


@pytest.mark.asyncio
async def test_connection_validation_requires_real_credentials(native_site,store_site):
    async with WordPressClient(native_site['origin'],native_site,transport=FixtureTransport()) as client:
        result=await client.validate_connection()
        assert result['authenticated'] is True
        assert result['authenticated_author']['id']==native_site['author_id']
    async with WordPressClient(native_site['origin'],{},transport=FixtureTransport()) as client:
        with pytest.raises(ConnectorError):
            await client.validate_connection()
    async with WooCommerceClient(store_site['origin'],store_site,transport=FixtureTransport()) as client:
        assert (await client.validate_connection())['authenticated'] is True
    async with WooCommerceClient(store_site['origin'],{},transport=FixtureTransport()) as client:
        with pytest.raises(ConnectorError):
            await client.validate_connection()


@pytest.mark.asyncio
async def test_platform_candidate_applies_one_metadata_change_without_sibling_or_body_drift(platform,monkeypatch):
    from bs4 import BeautifulSoup
    from sqlalchemy import select
    from app import workflows
    from app.config import settings
    from app.models import Candidate,Connection,Finding,Job,Site
    from app.policies import create_policy
    from app.network import fetch
    test_site=configure(connector=True)
    _,factory,site_id=platform
    monkeypatch.setattr(settings,'GLOBAL_PAUSE',False)
    async def fixture_client(db,site,kind='wordpress'):
        assert kind=='wordpress'
        return WordPressClient(test_site['origin'],test_site,transport=FixtureTransport())
    async def public_fetch(url):
        return await fetch(url,transport=FixtureTransport())
    monkeypatch.setattr(workflows,'client_for',fixture_client)
    monkeypatch.setattr(workflows,'fetch',public_fetch)
    draft=None
    try:
        async with await fixture_client(None,None) as remote:
            capabilities=await remote.validate_connection()
            assert capabilities['authenticated'] is True
            assert capabilities['seo']['write'] is True
            assert set(capabilities['seo']['writable_fields']) >= {'title','description'}
            draft=await remote.create_draft({'title':'Candidate fixture '+uuid4().hex[:8],
                'body':'<h2>Repair preparation</h2><p>Keep the repair instructions and layout unchanged.</p>',
                'author_id':test_site['author_id']},'candidate-'+uuid4().hex)
            published=await remote.publish(str(draft['id']))
            source=await remote.read(published['resource_key'])
        original_body=source['body']
        original_public=(await public_fetch(source['url']))['html']
        selected_value='A complete repair preparation guide'
        sibling_value='Prepare for your repair visit with useful instructions and practical guidance.'
        with factory() as db:
            site=db.get(Site,site_id)
            site.origin=test_site['origin']
            site.paused=False
            create_policy(db,site,None,{'enabled':True,'allowed_actions':['metadata']})
            db.add(Connection(
                site_id=site_id,
                kind='wordpress',
                encrypted_credentials='fixture-only',
                status='connected',
                checked_at=workflows.now(),
                capabilities=capabilities,
            ))
            db.flush()
            page=workflows.store_page(db,site,source)
            observation={'signals':{'source':'connector'},
                'findings':[{'code':'metadata_review','severity':'medium','title':'Review metadata'}],
                'candidates':[{'field':'seo_title','before_value':workflows.candidate_value(source,'seo_title'),
                               'after_value':selected_value},
                              {'field':'meta_description','before_value':workflows.candidate_value(source,'meta_description'),
                               'after_value':sibling_value}]}
            workflows.upsert_observation(db,site,page,observation)
            db.commit()
            selected=db.scalar(select(Candidate).where(Candidate.field=='seo_title',Candidate.site_id==site_id))
            sibling=db.scalar(select(Candidate).where(Candidate.field=='meta_description',Candidate.site_id==site_id))
            finding=db.scalar(select(Finding).where(Finding.site_id==site_id))
            selected.status='approved'
            db.commit()
            selected_id=selected.id
            sibling_id=sibling.id
            finding_id=finding.id
            job=Job(id='candidate-'+uuid4().hex,payload={'candidate_id':selected_id})
            result=await workflows.candidate(db,site,job)
            db.commit()
            assert result['status']=='applied'
            assert result['protected_drift']==[]
            assert db.get(Candidate,sibling_id).status=='pending'
            assert db.get(Finding,finding_id).status=='open'
            applied_hash=result['source_hash']

            async with await fixture_client(None,None) as remote:
                current=await remote.read(source['resource_key'])
                assert current['source_hash']==applied_hash
                assert workflows.candidate_value(current,'seo_title')==selected_value
                assert workflows.candidate_value(current,'meta_description')==workflows.candidate_value(source,'meta_description')
                assert current['body']==original_body
                async with httpx.AsyncClient(transport=FixtureTransport()) as public:
                    rendered=await public.get(source['url'])
            assert rendered.status_code==200
            soup=BeautifulSoup(rendered.text,'html.parser')
            assert soup.title is not None
            assert soup.title.get_text(strip=True)==selected_value
            description=soup.select_one('meta[name="description"]')
            assert description is None or description.get('content')!=sibling_value
            assert workflows.protected_html(original_public)==workflows.protected_html(rendered.text)

            replay=await workflows.candidate(db,site,job)
            db.commit()
            assert replay=={'status':'applied','candidate_id':selected_id,'replayed':True}
            assert db.get(Candidate,sibling_id).status=='pending'
            assert db.get(Finding,finding_id).status=='open'
            async with await fixture_client(None,None) as remote:
                replayed_source=await remote.read(source['resource_key'])
            assert replayed_source['source_hash']==applied_hash
            assert workflows.candidate_value(replayed_source,'meta_description')==workflows.candidate_value(source,'meta_description')
            assert replayed_source['body']==original_body
    finally:
        if draft is not None:
            async with await fixture_client(None,None) as remote:
                current=await remote.read(draft['resource_key'])
                await remote.restore(draft['resource_key'],draft,current['source_hash'])
