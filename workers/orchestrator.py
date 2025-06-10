import asyncio
from celery import group
from .celery_app import app
from app.services.crawler import CrawlerService
from .seo_tasks import get_page_title

@app.task(bind=True)
def run_full_audit(self, url: str):
    """
    The master orchestrator task that runs the entire audit workflow.
    """
    # Step 1: Run the crawler to get all page URLs
    print(f"Starting crawl for {url}...")
    crawler = CrawlerService(start_url=url, max_pages=10) # Using a small max_pages for testing
    pages_to_audit = asyncio.run(crawler.crawl())
    print(f"Crawl complete. Found {len(pages_to_audit)} pages to audit.")

    # Step 2: Create a group of parallel SEO tasks
    # The | operator is a shortcut for creating a chain.
    # Here, we create a group of get_page_title tasks and then chain it
    # with the 'collect_results' task.
    # For now, we'll just run the group.
    
    # Create a group of tasks, one for each page
    tasks_to_run = [get_page_title.s(page) for page in pages_to_audit]
    task_group = group(tasks_to_run)

    # Step 3: Execute the group and wait for results
    print("Dispatching SEO tasks in parallel...")
    result_group = task_group.apply_async()
    
    # This is a synchronous wait, which is okay inside a Celery task.
    # It will wait until all tasks in the group have finished.
    results = result_group.get()
    print("All SEO tasks complete. Results collected.")
    
    return results 