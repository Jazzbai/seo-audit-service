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
