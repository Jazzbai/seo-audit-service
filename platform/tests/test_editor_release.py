from test_platform import platform


def test_editor_origin_is_recorded_without_implying_approval(platform):
    client, _, site_id = platform
    path = f'/api/v1/sites/{site_id}/articles'
    article = client.post(path, json={'title': 'Preparing for a repair visit'}).json()
    edited = client.patch(f'{path}/{article["id"]}', json={'body': '<p>Bring your questions.</p>'})
    assert edited.status_code == 200
    provenance = edited.json()['brief']['generation']
    assert provenance['kind'] == 'authenticated_editor'
    assert provenance['user_id'] and provenance['recorded_at']
    checked = client.post(f'{path}/{article["id"]}/check').json()
    assert not checked['passed']
    assert 'missing_provenance' not in checked['blockers']
    assert 'missing_author' in checked['blockers']
    assert 'missing_sources' in checked['blockers']


def test_edit_keeps_provider_provenance_and_research(platform):
    client, _, site_id = platform
    path = f'/api/v1/sites/{site_id}/articles'
    brief = {'generation': {'kind': 'provider_draft', 'provider': 'isolated-fixture'},
             'research': {'complete': False, 'blockers': ['missing_fact']}}
    article = client.post(path, json={'title': 'Repair preparation checklist', 'brief': brief}).json()
    edited = client.patch(f'{path}/{article["id"]}', json={'body': '<p>Bring your questions.</p>'}).json()
    assert edited['brief']['generation'] == brief['generation']
    assert edited['brief']['research'] == brief['research']
    assert edited['brief']['last_editor_revision']['kind'] == 'authenticated_editor'
    checked = client.post(f'{path}/{article["id"]}/check').json()
    assert 'research_review_required' in checked['blockers']
