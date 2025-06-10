import asyncio
from .celery_app import app
import httpx
from bs4 import BeautifulSoup

async def fetch_and_parse(url: str) -> BeautifulSoup:
    """Asynchronous helper to fetch and parse a URL."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            url, 
            headers={"User-Agent": "Python-SEOAuditAgent/1.0"},
            timeout=10.0,
            follow_redirects=True
        )
        response.raise_for_status()
        return BeautifulSoup(response.text, 'lxml')

@app.task(bind=True)
def get_page_title(self, url: str) -> dict:
    """
    A synchronous Celery task that wraps an async helper to get a page title.
    
    This is the standard pattern for using async code within sync Celery tasks.
    """
    try:
        soup = asyncio.run(fetch_and_parse(url))
        
        title_tag = soup.find('title')
        
        if title_tag and title_tag.string:
            return {"status": "SUCCESS", "url": url, "check": "title", "value": title_tag.string.strip()}
        else:
            return {"status": "FAILURE", "url": url, "check": "title", "error": "Title tag not found or is empty"}

    except httpx.RequestError as e:
        return {"status": "FAILURE", "url": url, "check": "title", "error": f"Request failed: {e!r}"}
    except Exception as e:
        return {"status": "FAILURE", "url": url, "check": "title", "error": f"An unexpected error occurred: {e!r}"} 