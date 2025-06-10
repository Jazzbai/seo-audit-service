import httpx
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

class CrawlerService:
    """
    A service to crawl a website and discover all unique, internal links.
    """
    def __init__(
        self, 
        start_url: str, 
        max_pages: int = 100, 
        max_depth: int = 5
    ):
        """
        Initializes the crawler.
        
        Args:
            start_url: The URL to begin crawling from.
            max_pages: The maximum number of pages to crawl.
            max_depth: The maximum link depth to follow from the start URL.
        """
        self.start_url = start_url
        self.root_domain = urlparse(start_url).netloc
        self.max_pages = max_pages
        self.max_depth = max_depth
        
        self.crawled_urls = set()
        self.urls_to_crawl = [(start_url, 0)] # A queue of (url, depth) tuples

    async def crawl(self) -> list[str]:
        """
        Executes the crawl asynchronously.
        
        This method manages the queue of URLs to visit, respecting the max_pages
        and max_depth limits, and ensures that only unique, internal URLs are
        processed.
        """
        while self.urls_to_crawl and len(self.crawled_urls) < self.max_pages:
            current_url, current_depth = self.urls_to_crawl.pop(0)

            if current_url in self.crawled_urls or current_depth > self.max_depth:
                continue

            self.crawled_urls.add(current_url)
            print(f"Crawling: {current_url} at depth {current_depth}")

            links = await get_links_from_url(current_url)
            
            for link in links:
                absolute_link = urljoin(current_url, link)
                parsed_link = urlparse(absolute_link)
                
                # Basic validation to ensure we're getting a usable URL
                if not all([parsed_link.scheme, parsed_link.netloc]):
                    continue
                
                # Remove URL fragment if it exists
                absolute_link = parsed_link._replace(fragment="").geturl()

                if self.is_internal_link(absolute_link):
                    if absolute_link not in self.crawled_urls and not any(url == absolute_link for url, _ in self.urls_to_crawl):
                        self.urls_to_crawl.append((absolute_link, current_depth + 1))
        
        return sorted(list(self.crawled_urls))

    def is_internal_link(self, url: str) -> bool:
        """
        Checks if a URL belongs to the same root domain as the start URL,
        including subdomains. It prevents matching unrelated domains that happen
        to end with the same string.
        """
        link_domain = urlparse(url).netloc
        
        # Exact match (e.g., toscrape.com == toscrape.com)
        if link_domain == self.root_domain:
            return True
            
        # Subdomain match (e.g., books.toscrape.com ends with .toscrape.com)
        # The leading dot is crucial.
        return link_domain.endswith("." + self.root_domain)

async def get_links_from_url(url: str) -> set[str]:
    """
    A helper function to fetch a single URL and extract all links.
    
    This function includes a custom User-Agent and request timeout for robustness.
    """
    links = set()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url, 
                headers={"User-Agent": "Python-SEOAuditAgent/1.0"},
                timeout=10.0,
                follow_redirects=True
            )
            response.raise_for_status()  # Raise an exception for 4xx or 5xx status codes
            
            soup = BeautifulSoup(response.text, 'lxml')
            for a_tag in soup.find_all('a', href=True):
                link = a_tag['href']
                links.add(link)

    except httpx.RequestError as e:
        print(f"An error occurred while requesting {e.request.url!r}: {e!r}")
    except Exception as e:
        print(f"An unexpected error occurred for url {url}: {e!r}")
        
    return links 