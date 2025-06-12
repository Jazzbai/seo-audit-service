from celery import group, chord
from app.celery_app import celery_app
from app.services.crawler import CrawlerService
from app.tasks.seo_tasks import get_page_title
from app.db.session import SessionLocal
from app.models.audit import Audit
import datetime

@celery_app.task(bind=True)
async def crawl_task(self, url: str) -> list[str]:
    """
    Runs the crawler asynchronously and returns a list of URLs.
    """
    print(f"Executing crawl_task for {url}")
    crawler = CrawlerService(start_url=url, max_pages=10) # Small for testing
    pages = await crawler.crawl()
    print(f"Crawl task found {len(pages)} pages.")
    return pages

@celery_app.task(bind=True)
def compile_report_task(self, results: list, audit_id: int) -> dict:
    """
    Takes the list of results and the audit_id, compiles the final report,
    and saves it to the database.
    """
    print(f"Executing compile_report_task for audit_id: {audit_id}")
    
    final_report = {
        "status": "COMPLETE",
        "total_pages_analyzed": len(results),
        "analysis_results": results
    }
    
    # --- Update Database ---
    db = SessionLocal()
    try:
        audit = db.query(Audit).filter(Audit.id == audit_id).first()
        if audit:
            audit.status = "COMPLETE"
            audit.report_json = final_report
            audit.completed_at = datetime.datetime.utcnow()
            db.commit()
    finally:
        db.close()
    # ---------------------

    return final_report

@celery_app.task(bind=True)
def run_full_audit(self, audit_id: int, url: str):
    """
    The master orchestrator task that chains crawling to analysis.
    """
    # Create a chain: crawl_task -> start_seo_analysis_workflow
    workflow = crawl_task.s(url) | start_seo_analysis_workflow.s(audit_id=audit_id)
    workflow.apply_async()
    
    print(f"Launched audit workflow for url: {url} (Audit ID: {audit_id})")
    return {"message": "Audit workflow successfully launched."}

@celery_app.task(bind=True)
def start_seo_analysis_workflow(self, crawled_urls: list[str], audit_id: int):
    """
    This task acts as a dynamic dispatcher.
    It receives the list of URLs and creates a chord to run analysis in parallel.
    """
    if not crawled_urls:
        print(f"No URLs to analyze for audit_id: {audit_id}. Ending workflow.")
        # Even if no URLs, we should update the DB record.
        compile_report_task.s(
            results=[], 
            audit_id=audit_id
        ).apply_async()
        return

    # A group of parallel tasks to run on each URL
    header = group(get_page_title.s(url) for url in crawled_urls)

    # The callback task that runs after all parallel tasks are finished.
    # We pass the audit_id to it.
    callback = compile_report_task.s(audit_id=audit_id)

    # The chord primitive links the header (group) to the body (callback).
    chord(header)(callback)
    
    print(f"Dispatched a chord of {len(crawled_urls)} SEO tasks for audit_id: {audit_id}.") 