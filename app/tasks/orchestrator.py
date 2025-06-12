import advertools as adv
from celery import group, chord
from app.celery_app import celery_app
from app.tasks.seo_tasks import get_page_title, get_meta_description, get_h1_heading
from app.db.session import SessionLocal
from app.models.audit import Audit
import datetime
import pandas as pd
import os

@celery_app.task(bind=True)
def analyze_site_with_advertools(self, url: str) -> dict:
    """
    Uses advertools to crawl a site, collecting all internal, non-asset URLs
    and identifying broken links (4xx/5xx status) in one pass.
    """
    print(f"Starting advertools analysis for: {url}")
    output_file = f"crawl_results_{self.request.id}.jl"
    
    # Configure advertools to be polite and efficient.
    # CONCURRENT_REQUESTS_PER_DOMAIN=2: Limits simultaneous requests to the server.
    # DOWNLOAD_DELAY=1: Waits 1 second between requests.
    # CLOSESPIDER_PAGECOUNT=100: Safety limit to avoid crawling huge sites.
    adv.crawl(
        url_list=url,
        output_file=output_file,
        follow_links=True,
        custom_settings={
            'CONCURRENT_REQUESTS_PER_DOMAIN': 2,
            'DOWNLOAD_DELAY': 1,
            'CLOSESPIDER_PAGECOUNT': 100,
            'USER_AGENT': 'Python-SEOAuditAgent/1.0 (advertools)',
            'LOG_FILE': 'advertools.log'
        }
    )
    print(f"advertools crawl finished for: {url}")

    # Process the results
    crawl_df = pd.read_json(output_file, lines=True)
    os.remove(output_file) # Clean up the file after reading

    # Filter for crawlable HTML pages (status 200, content-type text/html)
    successful_pages = crawl_df[
        (crawl_df['status'] == 200) & 
        (crawl_df['resp_headers_Content-Type'].str.contains('text/html', na=False))
    ]
    crawlable_urls = successful_pages['url'].unique().tolist()

    # Identify broken links (any 4xx or 5xx status code)
    broken_links_df = crawl_df[crawl_df['status'] >= 400]
    broken_links_report = [
        {"url": row['url'], "status_code": row['status'], "source_page": row.get('request_headers_Referer')}
        for index, row in broken_links_df.iterrows()
    ]
    
    return {
        "crawlable_urls": crawlable_urls,
        "broken_links_report": {
            "status": "SUCCESS" if not broken_links_report else "FAILURE",
            "url": url, # The top-level URL this check is for
            "check": "broken_links",
            "value": broken_links_report,
            "message": f"Found {len(broken_links_report)} broken links."
        }
    }

@celery_app.task
def pass_through_result(result: dict) -> dict:
    """
    A simple task that just returns its input. Useful for injecting
    an already-computed result into a Celery workflow (like a chord).
    """
    return result

@celery_app.task(bind=True)
def compile_report_task(self, results: list, audit_id: int) -> dict:
    """
    Takes the list of results from all analysis tasks, groups them by URL,
    and saves the structured report to the database.
    """
    print(f"Executing compile_report_task for audit_id: {audit_id}")
    
    # The first result is the master report of all broken links found.
    broken_links_master_report = results[0]
    all_broken_links = broken_links_master_report.get('value', [])
    
    # Create a mapping from a source page to the links that are broken on it.
    broken_links_by_source = {}
    for link in all_broken_links:
        source_page = link.get('source_page')
        if source_page:
            if source_page not in broken_links_by_source:
                broken_links_by_source[source_page] = []
            # We don't need to repeat the source_page in the final report value
            broken_link_details = {k: v for k, v in link.items() if k != 'source_page'}
            broken_links_by_source[source_page].append(broken_link_details)

    # Group the main SEO check results by URL
    grouped_results = {}
    for result in results[1:]: # Process the rest of the SEO checks
        url = result.get("url")
        if url:
            if url not in grouped_results:
                grouped_results[url] = []
            
            check_result = result.copy()
            if "url" in check_result:
                del check_result["url"]
            grouped_results[url].append(check_result)

    # Now, inject a specific broken links report into each page that needs one.
    for url, broken_links in broken_links_by_source.items():
        if url in grouped_results:
            grouped_results[url].append({
                "status": "FAILURE",
                "check": "broken_links",
                "value": broken_links,
                "message": f"Found {len(broken_links)} broken links on this page."
            })

    final_report = {
        "status": "COMPLETE",
        "audit_id": audit_id,
        "total_pages_analyzed": len(grouped_results),
        "report": grouped_results
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
    The master orchestrator task. Replaces crawling with a single advertools analysis task.
    """
    workflow = analyze_site_with_advertools.s(url) | start_seo_analysis_workflow.s(audit_id=audit_id)
    workflow.apply_async()
    
    print(f"Launched audit workflow for url: {url} (Audit ID: {audit_id})")
    return {"message": "Audit workflow successfully launched."}

@celery_app.task(bind=True)
def start_seo_analysis_workflow(self, analysis_result: dict, audit_id: int):
    """
    This task acts as a dynamic dispatcher.
    It receives the data from advertools and creates a chord for further analysis.
    """
    crawlable_urls = analysis_result.get("crawlable_urls", [])
    broken_links_report = analysis_result.get("broken_links_report", {})

    if not crawlable_urls:
        print(f"No crawlable URLs to analyze for audit_id: {audit_id}. Compiling report now.")
        # Pass just the broken links report to the compiler
        compile_report_task.s(results=[broken_links_report], audit_id=audit_id).apply_async()
        return

    # Create a list of analysis tasks for all crawlable pages.
    # We use .si() (immutable signature) to prevent the parent task's arguments
    # (i.e., analysis_result) from being passed to these sub-tasks.
    page_analysis_tasks = []
    for url in crawlable_urls:
        page_analysis_tasks.append(get_page_title.si(url))
        page_analysis_tasks.append(get_meta_description.si(url))
        page_analysis_tasks.append(get_h1_heading.si(url))

    # The header for our chord will run all page analysis tasks in parallel.
    # We also include a task to pass our broken_links_report into the results list.
    header_tasks = [pass_through_result.s(broken_links_report)] + page_analysis_tasks
    header = group(header_tasks)
    
    # The callback task will receive the results from all header tasks.
    callback = compile_report_task.s(audit_id=audit_id)
    
    # The chord will execute the header group, then the callback with the results.
    chord(header, body=callback).apply_async()
    
    print(f"Dispatched a chord of {len(header_tasks)} total SEO tasks for audit_id: {audit_id}.")