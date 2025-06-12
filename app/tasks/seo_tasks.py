from celery.exceptions import MaxRetriesExceededError
import httpx
from bs4 import BeautifulSoup
from app.celery_app import celery_app
import asyncio
from urllib.parse import urljoin, urlparse
import random
from httpx import codes

def get_retrying_client() -> httpx.AsyncClient:
    """
    Returns an httpx.AsyncClient configured with a transport that
    automatically retries on 5xx server errors, but *not* on 429 (Too Many Requests).
    We want to handle 429s with Celery's exponential backoff mechanism.
    """
    class CustomRetryTransport(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            max_retries = 3
            delay = 1.0
            for i in range(max_retries):
                try:
                    response = await super().handle_async_request(request)
                    
                    # Only retry on 5xx server errors.
                    # 429s will be handled by the Celery task retry logic.
                    if response.status_code < 500:
                        return response
                    
                    # This is a 5xx error, so we'll retry.
                    response.raise_for_status()

                except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPStatusError) as e:
                    # If this was the last retry, or not a 5xx error, re-raise.
                    if i == max_retries - 1 or not (isinstance(e, httpx.HTTPStatusError) and e.response.status_code >= 500):
                        raise e
                    
                    await asyncio.sleep(delay)
                    delay *= 2 # Exponential backoff for server errors
                    continue
            # This line should not be reachable if retries are configured
            raise httpx.RequestError("Failed to get a response after multiple retries.")


    return httpx.AsyncClient(
        transport=CustomRetryTransport(),
        headers={"User-Agent": "Python-SEOAuditAgent/1.0"},
        timeout=10.0,
        follow_redirects=True
    )

@celery_app.task(bind=True, max_retries=5, rate_limit='5/s')
def get_page_title(self, url: str) -> dict:
    """
    A synchronous Celery task that gets a page title.
    It uses Celery's retry mechanism for handling 429s with exponential backoff.
    """
    async def _get_title_async():
        """Asynchronous helper. It returns data or raises an httpx exception."""
        async with get_retrying_client() as client:
            response = await client.get(url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'lxml')
            title_tag = soup.find('title')
            
            if title_tag and title_tag.string:
                return {
                    "status": "SUCCESS", "url": url, "check": "title", 
                    "value": title_tag.string.strip(), "message": "Title found."
                }
            else:
                return {
                    "status": "FAILURE", "url": url, "check": "title", 
                    "value": None, "message": "Title tag not found or is empty."
                }

    try:
        return asyncio.run(_get_title_async())
    except httpx.HTTPStatusError as e:
        if e.response.status_code == codes.TOO_MANY_REQUESTS:
            try:
                retry_delay = 2 ** self.request.retries
                self.retry(exc=e, countdown=retry_delay)
            except MaxRetriesExceededError:
                return {"status": "FAILURE", "url": url, "check": "title", "value": None, "message": f"Task failed after {self.max_retries} retries due to persistent rate-limiting (429)."}
        return {"status": "FAILURE", "url": url, "check": "title", "error": f"Request failed: {e!r}"}
    except httpx.RequestError as e:
        return {"status": "FAILURE", "url": url, "check": "title", "error": f"Request failed: {e!r}"}


@celery_app.task(bind=True, max_retries=5, rate_limit='5/s')
def get_meta_description(self, url: str) -> dict:
    """
    A synchronous Celery task that gets a page's meta description.
    It uses Celery's retry mechanism for handling 429s with exponential backoff.
    """
    async def _get_meta_desc_async():
        """Asynchronous helper. It returns data or raises an httpx exception."""
        async with get_retrying_client() as client:
            response = await client.get(url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'lxml')
            
            meta_tag = soup.find('meta', attrs={'name': 'description'})
            
            if meta_tag and meta_tag.get('content'):
                return {
                    "status": "SUCCESS", "url": url, "check": "meta_description", 
                    "value": meta_tag['content'].strip(), "message": "Meta description found."
                }
            else:
                return {
                    "status": "FAILURE", "url": url, "check": "meta_description", 
                    "value": None, "message": "Meta description not found or is empty."
                }

    try:
        return asyncio.run(_get_meta_desc_async())
    except httpx.HTTPStatusError as e:
        if e.response.status_code == codes.TOO_MANY_REQUESTS:
            try:
                retry_delay = 2 ** self.request.retries
                self.retry(exc=e, countdown=retry_delay)
            except MaxRetriesExceededError:
                return {"status": "FAILURE", "url": url, "check": "meta_description", "value": None, "message": f"Task failed after {self.max_retries} retries due to persistent rate-limiting (429)."}
        return {"status": "FAILURE", "url": url, "check": "meta_description", "error": f"Request failed: {e!r}"}
    except httpx.RequestError as e:
        return {"status": "FAILURE", "url": url, "check": "meta_description", "error": f"Request failed: {e!r}"}


@celery_app.task(bind=True, max_retries=5, rate_limit='5/s')
def get_h1_heading(self, url: str) -> dict:
    """
    A sync Celery task that checks for the presence and count of <h1> tags.
    It uses Celery's retry mechanism for handling 429s with exponential backoff.
    """
    async def _get_h1_async():
        """Asynchronous helper. It returns data or raises an httpx exception."""
        async with get_retrying_client() as client:
            response = await client.get(url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'lxml')
            
            h1_tags = soup.find_all('h1')
            h1_count = len(h1_tags)
            h1_values = [h1.get_text(strip=True) for h1 in h1_tags]

            if h1_count == 1:
                if not h1_values[0]:
                    return {
                        "status": "FAILURE", "url": url, "check": "h1_heading",
                        "message": "H1 tag is empty.", "count": 1, "value": [""]
                    }
                return {
                    "status": "SUCCESS", "url": url, "check": "h1_heading", 
                    "message": "Exactly one H1 tag found.", "count": 1, "value": h1_values[0]
                }
            elif h1_count == 0:
                return {
                    "status": "FAILURE", "url": url, "check": "h1_heading", 
                    "message": "No H1 tag found.", "count": 0, "value": []
                }
            else:
                return {
                    "status": "FAILURE", "url": url, "check": "h1_heading", 
                    "message": f"Found {h1_count} H1 tags. Expected 1.", "count": h1_count, "value": h1_values
                }
    
    try:
        return asyncio.run(_get_h1_async())
    except httpx.HTTPStatusError as e:
        if e.response.status_code == codes.TOO_MANY_REQUESTS:
            try:
                retry_delay = 2 ** self.request.retries
                self.retry(exc=e, countdown=retry_delay)
            except MaxRetriesExceededError:
                return {"status": "FAILURE", "url": url, "check": "h1_heading", "value": None, "message": f"Task failed after {self.max_retries} retries due to persistent rate-limiting (429)."}
        return {"status": "FAILURE", "url": url, "check": "h1_heading", "error": f"Request failed: {e!r}"}
    except httpx.RequestError as e:
        return {"status": "FAILURE", "url": url, "check": "h1_heading", "error": f"Request failed: {e!r}"}