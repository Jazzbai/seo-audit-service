import httpx
from bs4 import BeautifulSoup
from app.celery_app import celery_app

async def fetch_and_parse(client: httpx.AsyncClient, url: str) -> BeautifulSoup:
    """Asynchronous helper to fetch and parse a URL using a shared client."""
    response = await client.get(url, follow_redirects=True)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'lxml')

@celery_app.task(bind=True)
async def get_page_title(self, url: str) -> dict:
    """
    An asynchronous Celery task that gets a page title using httpx.
    """
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "Python-SEOAuditAgent/1.0"},
            timeout=10.0
        ) as client:
            soup = await fetch_and_parse(client, url)
            title_tag = soup.find('title')
            
            if title_tag and title_tag.string:
                return {"status": "SUCCESS", "url": url, "check": "title", "value": title_tag.string.strip()}
            else:
                return {"status": "FAILURE", "url": url, "check": "title", "error": "Title tag not found or is empty"}

    except httpx.RequestError as e:
        return {"status": "FAILURE", "url": url, "check": "title", "error": f"Request failed: {e!r}"}
    except Exception as e:
        return {"status": "FAILURE", "url": url, "check": "title", "error": f"An unexpected error occurred: {e!r}"} 