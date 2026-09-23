"""Isolated Chromium checks. Every browser request is fetched through DNS-pinned transport."""
import hashlib
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from app.config import settings
from app.models import Measurement, Page
from app.network import PublicTransport
from app.operations import event, iso, now


def _chromium_navigation_provenance():
    return {
        'measurement_context': 'lab',
        'core_web_vitals': False,
        'field_data': False,
    }


def _browser_url_allowed(url, origin):
    """Return whether a browser request stays on the registered origin.

    Browser resources are deliberately stricter than ordinary page links: the
    dedicated renderer must not become a general public-web fetcher.  Compare
    effective ports as well as scheme and hostname, and fail closed for
    malformed URLs or embedded credentials.
    """
    try:
        candidate = urlsplit(url)
        expected = urlsplit(origin)
        if candidate.scheme.casefold() not in ('http', 'https'):
            return False
        if candidate.username is not None or candidate.password is not None:
            return False
        candidate_host = (candidate.hostname or '').casefold().rstrip('.')
        expected_host = (expected.hostname or '').casefold().rstrip('.')
        if not candidate_host or candidate_host != expected_host:
            return False
        candidate_port = candidate.port or (443 if candidate.scheme.casefold() == 'https' else 80)
        expected_port = expected.port or (443 if expected.scheme.casefold() == 'https' else 80)
        return (
            candidate.scheme.casefold() == expected.scheme.casefold()
            and candidate_port == expected_port
        )
    except (AttributeError, TypeError, ValueError):
        return False


async def inspect_page(db,site,job):
    from playwright.async_api import async_playwright
    from app.intelligence.audit import audit_page
    from app.workflows import scoped, upsert_observation, capture_html
    page = scoped(db,Page,job.payload.get('page_id'),site)
    if not _browser_url_allowed(page.url, site.origin):
        raise ValueError('Rendered checks are limited to this site')
    failures = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True,args=['--disable-dev-shm-usage'])
        context = await browser.new_context(service_workers='block',viewport={'width':1440,'height':1000},accept_downloads=False)
        async with httpx.AsyncClient(transport=PublicTransport(site.origin),timeout=15,trust_env=False,follow_redirects=False) as client:
            async def route_request(route):
                request = route.request
                if request.method not in ('GET','HEAD'):
                    await route.abort()
                    return
                if not _browser_url_allowed(request.url, site.origin):
                    failures.append(request.resource_type or 'unknown')
                    await route.abort()
                    return
                try:
                    async with client.stream(request.method,request.url) as response:
                        body = bytearray()
                        async for part in response.aiter_bytes():
                            body.extend(part)
                            if len(body) > 8_000_000:
                                raise ValueError('Browser resource exceeds limit')
                        headers = {k:v for k,v in response.headers.items() if k.lower() not in ('content-encoding','content-length','transfer-encoding','set-cookie')}
                        await route.fulfill(status=response.status_code,headers=headers,body=bytes(body))
                except Exception:
                    failures.append(request.resource_type)
                    await route.abort()
            await context.route('**/*',route_request)
            await context.route_web_socket('**/*',lambda socket: socket.close())
            tab = await context.new_page()
            errors = []
            tab.on('pageerror',lambda error: errors.append(str(error)[:300]))
            try:
                response = await tab.goto(page.url,wait_until='domcontentloaded',timeout=45000)
                await tab.wait_for_timeout(1500)
                html = await tab.content()
                observation = audit_page(page.url,html,page.source)
                observed = iso(now())
                observation['signals'].update({'observation_type':'browser_rendered','observed_at':observed,'resource_failures':len(failures)})
                evidence = capture_html(site,job,page.url,html,'browser_rendered')
                evidence['observation_type'] = 'browser_rendered'
                observation['signals']['evidence'] = evidence
                # Rendered findings have their own namespace; never resolve source findings by absence.
                for finding in observation['findings']:
                    finding['key'] = 'browser:' + finding.get('key',finding['code'])
                    finding['details'] = {**finding.get('details',{}),'observation_type':'browser_rendered','evidence':evidence}
                observation['candidates'] = []
                complete = bool(response and response.status == 200 and not failures)
                upsert_observation(db,site,page,observation,complete=complete)
                timing = await tab.evaluate('() => {const n=performance.getEntriesByType("navigation")[0]; return n ? {dom_content_loaded_ms:n.domContentLoadedEventEnd,response_ms:n.responseEnd,load_ms:n.loadEventEnd} : {}}')
                screenshot = await tab.screenshot(full_page=False)
                root = Path(settings.ARTIFACT_ROOT)/site.id/'browser'
                root.mkdir(parents=True,exist_ok=True)
                artifact = root/(job.id+'.png')
                artifact.write_bytes(screenshot)
                data = {'page_id':page.id,'url':page.url,'status_code':response.status if response else None,
                        'measurement_type':'lab_navigation_not_core_web_vitals','timing':timing,'script_errors':errors[:20],
                        'resource_failures':len(failures),'screenshot_sha256':hashlib.sha256(screenshot).hexdigest(),
                        'artifact':str(artifact.relative_to(Path(settings.ARTIFACT_ROOT))),
                        **_chromium_navigation_provenance()}
                db.add(Measurement(site_id=site.id,kind='browser',source='chromium_lab',data=data,observed_at=now()))
                event(db,site,'browser_checked',f'Rendered check completed: {page.title}',{'page_id':page.id})
                return {**data,'complete':complete}
            finally:
                await context.close()
                await browser.close()
