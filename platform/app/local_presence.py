"""Service-list planning shared by every site's local SEO action surface.

Plans are not Google writes or a claim of listing eligibility/ownership.
"""
from urllib.parse import urlsplit


def service_plan(site):
    facts = site.facts if isinstance(site.facts, dict) else {}
    services = facts.get('services', [])
    services = services if isinstance(services, list) else []
    names = []
    seen = set()
    for value in services:
        if not isinstance(value, str):
            continue
        name = ' '.join(value.split())
        key = name.casefold()
        if not 1 <= len(name) <= 120 or key in seen:
            continue
        seen.add(key)
        names.append(name)
        if len(names) >= 50:
            break
    refs = facts.get('confirmed_sources', [])
    refs = refs if isinstance(refs, list) else []
    origin = urlsplit(site.origin)
    sources = []
    for url in refs:
        if not isinstance(url, str):
            continue
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme == 'https' and parsed.netloc == origin.netloc and url not in sources:
            sources.append(url)
        if len(sources) >= 50:
            break
    return {
        'action': 'google_business_profile_services',
        'site_id': site.id,
        'status': 'needs_profile_access' if names and sources else 'needs_business_facts',
        'services': names,
        'sources': sources,
        'evidence_basis': 'saved_business_facts_not_independent_verification',
        'remote_write_supported': False,
        'requirements': ['Confirm this business is eligible for a Google Business Profile',
                         'Sign in as an owner or manager and select the matching location',
                         'Review the existing service list and preserve unrelated services',
                         'Add only confirmed offered services; no invented hours, prices or claims'],
        'next_action': 'Open Google Business Profile and review the matching location services',
        'manage_url': 'https://business.google.com/locations',
    }
