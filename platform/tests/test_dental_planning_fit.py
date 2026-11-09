import pytest

from app.intelligence.content import business_topic_matches, plan_topics


FACTS = {'business_name': 'H-Town Star Family Dentistry', 'locations': ['Houston'],
         'services': ['General dentistry', 'Pediatric dentistry', 'Preventive dental care']}


@pytest.mark.parametrize('query', ['4 star dental service', '5 star dental group', '5 star dental near me', '5-star dental care'])
def test_rating_queries_do_not_become_automatic_dental_guides(query):
    assert not business_topic_matches(query, FACTS)


def test_a_real_numeric_business_name_can_still_match_its_own_offering():
    assert business_topic_matches('5 Star Dental cleaning', {'business_name': '5 Star Dental', 'services': ['Dental cleaning']})


def test_filtered_discovery_phrases_leave_room_for_relevant_plans():
    inputs = [{'kind': 'search_observation', 'query': query, 'source': 'gsc'} for query in
              ['4 star dental service', '5 star dental group', '5 star dental near me']]
    briefs = plan_topics(FACTS, [], research_inputs=inputs, automatic=True)
    assert briefs
    assert all('4 star' not in item['title'].lower() and '5 star' not in item['title'].lower() for item in briefs)


@pytest.mark.parametrize('query', [
    'cedar house family dentistry', 'applewhite dental arts', 'coolspring dental',
    'coolspring dental care',
])
def test_an_industry_word_does_not_qualify_a_competing_practice_name(query):
    assert not business_topic_matches(query, FACTS)


@pytest.mark.parametrize('query', ['pediatric dental visit questions', 'preventive dental care'])
def test_specific_offerings_still_qualify_for_automatic_planning(query):
    assert business_topic_matches(query, FACTS)


def test_discovery_planner_excludes_competitor_names_but_keeps_an_actual_offering():
    inputs = [{'kind': 'search_observation', 'query': query, 'source': 'gsc'} for query in
              ['cedar house family dentistry', 'applewhite dental arts', 'coolspring dental',
               'pediatric dental visit questions']]
    briefs = plan_topics(FACTS, [], research_inputs=inputs, automatic=True)
    titles = [item['title'].lower() for item in briefs]
    assert any('pediatric dental visit questions' in title for title in titles)
    assert not any(name in title for title in titles for name in ('cedar house', 'applewhite', 'coolspring'))
