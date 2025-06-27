import advertools as adv
from celery import chain
from app.celery_app import celery_app
from app.db.session import SessionLocal
from app.models.audit import Audit
import datetime
import pandas as pd
import os
import logging
import re
from collections import Counter, defaultdict
from urllib.parse import urlparse
import httpx
from app.core.config import settings
from app.utils.error_handler import (
    classify_error, 
    is_valid_url, 
    check_domain_exists, 
    validate_crawl_output
)
import asyncio
from concurrent.futures import ThreadPoolExecutor
import time

# --- Learning Notes: Stop Words ---
STOP_WORDS = {
    'i', 'me', 'my', 'myself', 'we', 'our', 'ours', 'ourselves', 'you', 'your', 
    'yours', 'yourself', 'yourselves', 'he', 'him', 'his', 'himself', 'she', 
    'her', 'hers', 'herself', 'it', 'its', 'itself', 'they', 'them', 'their', 
    'theirs', 'themselves', 'what', 'which', 'who', 'whom', 'this', 'that', 
    'these', 'those', 'am', 'is', 'are', 'was', 'were', 'be', 'been', 'being', 
    'have', 'has', 'had', 'having', 'do', 'does', 'did', 'doing', 'a', 'an', 
    'the', 'and', 'but', 'if', 'or', 'because', 'as', 'until', 'while', 'of', 
    'at', 'by', 'for', 'with', 'about', 'against', 'between', 'into', 'through', 
    'during', 'before', 'after', 'above', 'below', 'to', 'from', 'up', 'down', 
    'in', 'out', 'on', 'off', 'over', 'under', 'again', 'further', 'then', 'once', 
    'here', 'there', 'when', 'where', 'why', 'how', 'all', 'any', 'both', 'each', 
    'few', 'more', 'most', 'other', 'some', 'such', 'no', 'nor', 'not', 'only', 
    'own', 'same', 'so', 'than', 'too', 'very', 's', 't', 'can', 'will', 'just', 
    'don', 'should', 'now', 'd', 'll', 'm', 'o', 're', 've', 'y', '-', '–', '|',
    'page', 'archives', 'club', 'tag', 'category', 'author', 'events',
    'jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec',
    'january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 
    'september', 'october', 'november', 'december'
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def _get_top_words(series: pd.Series, n: int = 10) -> list:
    text = series.dropna().str.cat(sep=' ').lower()
    words = re.findall(r'\b[a-z]+\b', text)
    word_counts = Counter(w for w in words if w not in STOP_WORDS)
    return word_counts.most_common(n)

def _mark_audit_failed(audit_id: int, error_message: str, url: str = None):
    """Mark audit as failed with proper error classification."""
    error_info = classify_error(error_message, url)
    
    db = SessionLocal()
    try:
        audit = db.query(Audit).filter(Audit.id == audit_id).first()
        if audit:
            # Only update if not already in a final state to prevent duplicates
            if audit.status not in ["FAILED", "COMPLETE"]:
                audit.status = error_info["status"]
                audit.error_message = error_info["user_message"]
                audit.technical_error = error_info["technical_message"]
                audit.report_json = {}
                audit.completed_at = datetime.datetime.utcnow()
                db.commit()
                logger.info(f"Marked audit {audit_id} as {error_info['status']}: {error_info['user_message']}")
                
                # Send webhook for failed audits too (only once)
                if settings.DASHBOARD_CALLBACK_URL:
                    logger.info(f"Dashboard callback URL is set, queueing callback task for failed audit_id: {audit_id}")
                    send_report_to_dashboard.delay(audit_id=audit_id)
                else:
                    logger.info(f"No dashboard callback URL configured. Skipping callback for failed audit_id: {audit_id}")
            else:
                logger.info(f"Audit {audit_id} already in final state {audit.status}, skipping duplicate update")
                
    except Exception as db_error:
        logger.error(f"Failed to update audit {audit_id} status: {db_error}")
    finally:
        db.close()

@celery_app.task(bind=True)
def run_advertools_crawl(self, audit_id: int, url: str, max_pages: int) -> str:
    logger.info(f"Starting advertools crawl for audit_id: {audit_id}, url: {url}")
    
    # Pre-flight URL validation
    url_valid, url_error = is_valid_url(url)
    if not url_valid:
        error_msg = f"Invalid URL format: {url_error}"
        logger.error(f"URL validation failed for audit_id {audit_id}: {error_msg}")
        _mark_audit_failed(audit_id, error_msg, url)
        raise ValueError(error_msg)
    
    # Check if domain exists
    domain_exists, domain_error = check_domain_exists(url)
    if not domain_exists:
        error_msg = f"Domain validation failed: {domain_error}"
        logger.error(f"Domain check failed for audit_id {audit_id}: {error_msg}")
        _mark_audit_failed(audit_id, error_msg, url)
        raise ValueError(error_msg)
    
    os.makedirs('results', exist_ok=True)
    os.makedirs('logs', exist_ok=True)
    output_file = f"results/audit_results_{audit_id}.jl"
    log_file = f"logs/audit_log_{audit_id}.log"
    
    custom_settings = {
        'DOWNLOAD_DELAY': 1,
        'CONCURRENT_REQUESTS_PER_DOMAIN': 2,
        'ROBOTSTXT_OBEY': False,
        'CLOSESPIDER_PAGECOUNT': max_pages,
        'USER_AGENT': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'LOG_FILE': log_file,
        'DOWNLOAD_TIMEOUT': 30,
        'DNS_TIMEOUT': 10,
    }
    
    try:
        adv.crawl(url_list=url, output_file=output_file, follow_links=True, custom_settings=custom_settings)
        
        # Validate crawl output
        output_valid, output_error = validate_crawl_output(output_file)
        if not output_valid:
            error_msg = f"Crawl validation failed: {output_error}"
            logger.error(f"Crawl output validation failed for audit_id {audit_id}: {error_msg}")
            _mark_audit_failed(audit_id, error_msg, url)
            raise ValueError(error_msg)
        
        logger.info(f"Crawl finished for audit_id: {audit_id}. Results in: {output_file}")
        return output_file
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Advertools crawl failed for audit_id: {audit_id} with error: {error_msg}", exc_info=True)
        _mark_audit_failed(audit_id, error_msg, url)
        raise

@celery_app.task(bind=True)
def compile_report_from_crawl(self, crawl_output_file: str, audit_id: int) -> dict:
    logger.info(f"Compiling report for audit_id: {audit_id} from file: {crawl_output_file}")
    
    try:
        # Validate crawl output first
        output_valid, output_error = validate_crawl_output(crawl_output_file)
        if not output_valid:
            error_msg = f"Invalid crawl output: {output_error}"
            logger.error(f"Crawl output validation failed for audit_id {audit_id}: {error_msg}")
            _mark_audit_failed(audit_id, error_msg)
            raise ValueError(error_msg)
        
        crawl_df = pd.read_json(crawl_output_file, lines=True)
        
        # Additional validation for required columns
        if crawl_df.empty:
            error_msg = "Crawl produced no analyzable data"
            logger.error(f"Empty crawl results for audit_id {audit_id}")
            _mark_audit_failed(audit_id, error_msg)
            raise ValueError(error_msg)
            
    except FileNotFoundError:
        error_msg = f"Crawl output file not found: {crawl_output_file}"
        logger.error(f"File not found for audit_id {audit_id}: {error_msg}")
        _mark_audit_failed(audit_id, error_msg)
        raise
    except Exception as e:
        error_msg = f"Failed to read crawl output: {str(e)}"
        logger.error(f"Error reading crawl file for audit_id {audit_id}: {error_msg}")
        _mark_audit_failed(audit_id, error_msg)
        raise

    try:
        page_level_report = {}
        pages_with_title, pages_with_meta_desc, pages_with_one_h1, pages_with_multiple_h1s, pages_with_no_h1 = 0, 0, 0, 0, 0
        for _, row in crawl_df.iterrows():
            url = row.get('url')
            if not url: continue
            page_report = []
            
            # Handle title safely
            title = row.get('title')
            if pd.notna(title) and title:
                pages_with_title += 1
                page_report.append({"status": "SUCCESS", "check": "title", "value": title.strip(), "message": "Title found."})
            else:
                page_report.append({"status": "FAILURE", "check": "title", "value": None, "message": "Title tag not found or is empty."})
            
            # Handle meta description safely
            meta_desc = row.get('meta_desc')
            if pd.notna(meta_desc) and meta_desc:
                pages_with_meta_desc += 1
                page_report.append({"status": "SUCCESS", "check": "meta_description", "value": meta_desc.strip(), "message": "Meta description found."})
            else:
                page_report.append({"status": "FAILURE", "check": "meta_description", "value": None, "message": "Meta description not found or is empty."})
            
            # Handle H1 safely
            h1s = [h.strip() for h in row.get('h1', '').split('@@') if h.strip()] if pd.notna(row.get('h1')) else []
            if len(h1s) == 1:
                pages_with_one_h1 += 1
                page_report.append({"status": "SUCCESS", "check": "h1_heading", "message": "Exactly one H1 tag found.", "count": 1, "value": h1s[0]})
            elif len(h1s) == 0:
                pages_with_no_h1 += 1
                page_report.append({"status": "FAILURE", "check": "h1_heading", "message": "No H1 tag found.", "count": 0, "value": []})
            else:
                pages_with_multiple_h1s += 1
                page_report.append({"status": "FAILURE", "check": "h1_heading", "message": f"Found {len(h1s)} H1 tags. Expected 1.", "count": len(h1s), "value": h1s})
            page_level_report[url] = page_report
    except Exception as e:
        error_msg = f"Failed to process page-level data: {str(e)}"
        logger.error(f"Page processing error for audit_id {audit_id}: {error_msg}")
        _mark_audit_failed(audit_id, error_msg)
        raise

    # Handle broken links safely - check if 'status' column exists
    broken_links_report = []
    try:
        if 'status' in crawl_df.columns:
            broken_links_df = crawl_df[crawl_df['status'] >= 400].copy()
            referer_col = 'request_headers_Referer'
            if referer_col in broken_links_df.columns:
                broken_links_df.rename(columns={referer_col: 'source_url'}, inplace=True)
                broken_links_df['source_url'] = broken_links_df['source_url'].where(pd.notna(broken_links_df['source_url']), None)
                broken_links_report = broken_links_df[['url', 'status', 'source_url']].to_dict('records')
            else:
                broken_links_report = broken_links_df[['url', 'status']].to_dict('records')
        else:
            # Handle timeout/error cases where no status column exists
            logger.warning(f"No 'status' column found in crawl data for audit_id {audit_id}. Checking for error records.")
            # Look for timeout/error indicators in the data
            error_rows = []
            for _, row in crawl_df.iterrows():
                url = row.get('url', 'Unknown URL')
                # Check for common error indicators
                if pd.isna(row.get('title')) and pd.isna(row.get('meta_desc')) and pd.isna(row.get('h1')):
                    # This suggests a failed request (timeout, connection error, etc.)
                    error_rows.append({
                        'url': url,
                        'status': 'Timeout/Error'  # Generic error status for display
                    })
            broken_links_report = error_rows
            if error_rows:
                logger.info(f"Found {len(error_rows)} error records (likely timeouts) for audit_id {audit_id}")
    except Exception as e:
        logger.error(f"Failed to process broken links for audit_id {audit_id}: {e}")
        broken_links_report = []

    links_to_check = []
    try:
        main_domain = urlparse(crawl_df['url'][0]).netloc
        link_df = adv.crawlytics.links(crawl_df, internal_url_regex=main_domain)
        
        # Check if 'internal' column exists
        if 'internal' in link_df.columns:
            external_links_df = link_df[~link_df['internal']].copy()
            external_links_df.dropna(subset=['link'], inplace=True)
            external_links_df['link'] = external_links_df['link'].astype(str)
            links_to_check = external_links_df.rename(columns={'url': 'source_url'})[['link', 'source_url']].to_dict('records')
            logger.info(f"Found {len(external_links_df['link'].unique())} unique external links to check for audit_id: {audit_id}")
        else:
            logger.warning(f"No 'internal' column found in links data for audit_id {audit_id}. Skipping external link analysis.")
    except Exception as e:
        logger.error(f"Failed to extract external links for audit_id {audit_id}: {e}", exc_info=True)

    total_pages = len(page_level_report)
    initial_report = {
        "status": "ANALYZING_EXTERNAL",
        "audit_id": audit_id,
        "summary": {
            "total_pages_analyzed": total_pages,
            "internal_broken_links_found": len(broken_links_report),
            "pages_missing_title": total_pages - pages_with_title,
            "pages_missing_meta_description": total_pages - pages_with_meta_desc,
            "pages_with_correct_h1": pages_with_one_h1,
            "pages_with_multiple_h1s": pages_with_multiple_h1s,
            "pages_with_no_h1": pages_with_no_h1,
            "top_10_title_words": _get_top_words(crawl_df['title']) if 'title' in crawl_df.columns else [],
            "top_10_h1_words": _get_top_words(crawl_df['h1']) if 'h1' in crawl_df.columns else [],
        },
        "internal_broken_links": broken_links_report,
        "page_level_report": page_level_report
    }

    try:
        db = SessionLocal()
        try:
            audit = db.query(Audit).filter(Audit.id == audit_id).first()
            if audit:
                audit.status = "ANALYZING_EXTERNAL"
                audit.report_json = initial_report
                db.commit()
        finally:
            db.close()
        return {"crawl_output_file": crawl_output_file, "links_to_check": links_to_check}
    except Exception as e:
        error_msg = f"Failed to save report compilation: {str(e)}"
        logger.error(f"Database error for audit_id {audit_id}: {error_msg}")
        _mark_audit_failed(audit_id, error_msg)
        raise

@celery_app.task(bind=True)
def check_external_links(self, previous_task_output: dict, audit_id: int) -> dict:
    """
    Check external links using async chunked processing.
    This version prevents blocking and handles domain collisions intelligently.
    """
    links_to_check = previous_task_output.get('links_to_check', [])
    crawl_output_file = previous_task_output.get('crawl_output_file')
    
    if not links_to_check:
        logger.info(f"No external links to check for audit_id: {audit_id}. Skipping.")
        return {"crawl_output_file": crawl_output_file, "external_links_report": {}}

    # Create URL to source mapping for preserving source URLs
    links_df = pd.DataFrame(links_to_check)
    url_to_source_mapping = {}
    
    for _, row in links_df.iterrows():
        url = row['link']
        source = row['source_url']
        if url not in url_to_source_mapping:
            url_to_source_mapping[url] = source
        # If URL appears multiple times, keep the first source for simplicity
    
    unique_urls_to_check = list(url_to_source_mapping.keys())
    
    logger.info(f"Starting async external link checking for {len(unique_urls_to_check)} unique URLs (audit_id: {audit_id})")
    
    try:
        # Run the async function in the current thread
        # Since this is a Celery task, we need to create a new event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            external_links_report = loop.run_until_complete(
                check_external_links_async(unique_urls_to_check, audit_id, url_to_source_mapping)
            )
        finally:
            loop.close()
        
        logger.info(f"Async external link checking completed for audit_id: {audit_id}")
        
    except Exception as e:
        logger.error(f"Failed to check external links for audit_id {audit_id}: {e}", exc_info=True)
        external_links_report = {
            "unreachable_links": [],
            "broken_links": [],
            "permission_issues": [],
            "method_issues": [],
            "other_client_errors": [],
            "error": str(e)
        }
    
    return {"crawl_output_file": crawl_output_file, "external_links_report": external_links_report}

@celery_app.task(bind=True)
def save_final_report(self, previous_task_output: dict, audit_id: int):
    logger.info(f"Saving final report for audit_id: {audit_id}")
    external_links_report = previous_task_output.get('external_links_report', {})
    crawl_output_file = previous_task_output.get('crawl_output_file')
    unreachable_links = external_links_report.get('unreachable_links', [])
    broken_links = external_links_report.get('broken_links', [])
    permission_issues = external_links_report.get('permission_issues', [])
    method_issues = external_links_report.get('method_issues', [])
    other_client_errors = external_links_report.get('other_client_errors', [])

    db = SessionLocal()
    try:
        audit = db.query(Audit).filter(Audit.id == audit_id).first()
        if not audit:
            logger.error(f"Audit with ID {audit_id} not found for final save.")
            return

        report_json = audit.report_json.copy()
        summary = report_json["summary"]
        summary.update({
            "external_unreachable_links_found": len(unreachable_links),
            "external_broken_links_found": len(broken_links),
            "external_permission_issues_found": len(permission_issues),
            "external_method_issues_found": len(method_issues),
            "external_other_client_errors_found": len(other_client_errors)
        })
        
        # This is the final JSON object that will be stored in the database.
        # It should NOT contain redundant top-level keys like status or audit_id,
        # as those are separate columns in the 'audits' table.
        final_report_blob = {
            "summary": summary,
            "external_unreachable_links": unreachable_links,
            "external_broken_links": broken_links,
            "external_permission_issue_links": permission_issues,
            "external_method_issue_links": method_issues,
            "external_other_client_errors": other_client_errors,
            "internal_broken_links": report_json["internal_broken_links"],
            "page_level_report": report_json["page_level_report"]
        }
        
        audit.report_json = final_report_blob
        audit.status = "COMPLETE"
        audit.completed_at = datetime.datetime.utcnow()
        db.commit()
        logger.info(f"Successfully saved final report for audit_id: {audit_id}")

        if settings.DASHBOARD_CALLBACK_URL:
            logger.info(f"Dashboard callback URL is set, queueing callback task for audit_id: {audit_id}")
            send_report_to_dashboard.delay(audit_id=audit_id)
        else:
            logger.info(f"No dashboard callback URL configured. Skipping callback for audit_id: {audit_id}")
    except Exception as e:
        logger.error(f"Failed to save final report for audit_id {audit_id}: {e}", exc_info=True)
        if audit:
            audit.status = "ERROR"
            db.commit()
    finally:
        db.close()

    if crawl_output_file and os.path.exists(crawl_output_file):
        try: os.remove(crawl_output_file)
        except OSError as e: logger.warning(f"Error cleaning up main crawl file {crawl_output_file}: {e}")

@celery_app.task(bind=True)
def send_report_to_dashboard(self, audit_id: int):
    """
    Sends the final report to the pre-configured dashboard callback URL.
    This task will retry if the dashboard is unavailable.
    """
    db = SessionLocal()
    try:
        audit = db.query(Audit).filter(Audit.id == audit_id).first()
        if not audit:
            logger.error(f"Audit with ID {audit_id} not found for sending report.")
            return

        if not settings.DASHBOARD_CALLBACK_URL or not settings.DASHBOARD_API_KEY:
            logger.warning("DASHBOARD_CALLBACK_URL or DASHBOARD_API_KEY not set. Skipping callback.")
            return
        
        # The data in audit.report_json is now clean. We construct the payload
        # using the direct attributes from the model for clarity and consistency.
        # Order matches the API response exactly for consistency
        callback_payload = {
            "audit_id": audit.id,
            "status": audit.status,
            "url": audit.url,
            "user_id": audit.user_id,
            "user_audit_report_request_id": audit.user_audit_report_request_id,
            "created_at": audit.created_at.isoformat(),
            "completed_at": audit.completed_at.isoformat() if audit.completed_at else None,
            "error_message": audit.error_message,
            "technical_error": audit.technical_error,
            "report_json": audit.report_json
        }

        headers = {
            "Content-Type": "application/json",
            "X-API-KEY": settings.DASHBOARD_API_KEY
        }

        logger.info(f"Sending final report for audit_id: {audit_id} to {settings.DASHBOARD_CALLBACK_URL}")

        with httpx.Client() as client:
            response = client.post(
                settings.DASHBOARD_CALLBACK_URL,
                json=callback_payload,
                headers=headers,
                timeout=30.0 
            )
            response.raise_for_status()
            logger.info(f"Successfully sent report for audit_id: {audit_id}. Dashboard responded with {response.status_code}.")

    except httpx.RequestError as exc:
        logger.error(f"Request to dashboard failed for audit {audit_id}: {exc}. Retrying in 60 seconds.")
        # Exponential backoff, retry in 60s, 120s, 240s, etc. max 5 times.
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries), max_retries=5)
    except Exception as e:
        logger.error(f"An unexpected error occurred while sending report for audit {audit_id}: {e}", exc_info=True)
        # For non-HTTP errors, you might not want to retry, or use a different strategy.
        # Here we will not retry for unexpected errors to avoid poison pills.
    finally:
        db.close()

@celery_app.task
def run_full_audit(audit_id: int, url: str, max_pages: int = 100):
    task_chain = chain(
        run_advertools_crawl.s(audit_id=audit_id, url=url, max_pages=max_pages),
        compile_report_from_crawl.s(audit_id=audit_id),
        check_external_links.s(audit_id=audit_id),
        save_final_report.s(audit_id=audit_id)
    )
    task_chain.apply_async()

@celery_app.task
def debug_task():
    print("Debug task executed.")

# --- Async External Link Checking Functions ---

def chunk_urls_by_domain(urls: list, max_chunk_size: int = 20) -> list:
    """
    Intelligently chunk URLs to prevent domain collision.
    Groups URLs by domain and distributes them across chunks.
    """
    domain_groups = defaultdict(list)
    
    # Group URLs by domain
    for url in urls:
        try:
            domain = urlparse(url).netloc
            domain_groups[domain].append(url)
        except Exception:
            # Handle malformed URLs
            domain_groups['unknown'].append(url)
    
    chunks = []
    
    # For each domain, distribute URLs across different chunks
    for domain, domain_urls in domain_groups.items():
        for i, url in enumerate(domain_urls):
            chunk_index = i % max_chunk_size
            
            # Ensure we have enough chunks
            while len(chunks) <= chunk_index:
                chunks.append([])
            
            chunks[chunk_index].append(url)
    
    # Remove empty chunks and limit chunk sizes
    final_chunks = []
    for chunk in chunks:
        if chunk:
            # Split large chunks further
            while len(chunk) > max_chunk_size:
                final_chunks.append(chunk[:max_chunk_size])
                chunk = chunk[max_chunk_size:]
            if chunk:
                final_chunks.append(chunk)
    
    return final_chunks

def get_domain_safe_settings(urls: list) -> dict:
    """
    Get advertools settings optimized for the domains in the URL list.
    More conservative for single domain, more aggressive for multiple domains.
    """
    domains = set()
    for url in urls:
        try:
            domains.add(urlparse(url).netloc)
        except Exception:
            continue
    
    if len(domains) <= 1:
        # Single domain - be conservative to avoid getting blocked
        return {
            'ROBOTSTXT_OBEY': False,
            'USER_AGENT': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
            'DOWNLOAD_DELAY': 2,
            'CONCURRENT_REQUESTS_PER_DOMAIN': 1,
            'RANDOMIZE_DOWNLOAD_DELAY': True,
            'DOWNLOAD_TIMEOUT': 15,
            'DNS_TIMEOUT': 10,
            'RETRY_ENABLED': False,
        }
    else:
        # Multiple domains - can be more aggressive
        return {
            'ROBOTSTXT_OBEY': False,
            'USER_AGENT': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
            'DOWNLOAD_DELAY': 1,
            'CONCURRENT_REQUESTS_PER_DOMAIN': 2,
            'RANDOMIZE_DOWNLOAD_DELAY': True,
            'DOWNLOAD_TIMEOUT': 10,
            'DNS_TIMEOUT': 5,
            'RETRY_ENABLED': False,
        }

def run_advertools_chunk(urls: list, output_file: str) -> dict:
    """
    Run advertools crawl_headers on a chunk of URLs.
    This function runs in a separate thread.
    """
    try:
        if not urls:
            return {"success": True, "urls_processed": 0, "output_file": output_file}
        
        # Get domain-optimized settings
        custom_settings = get_domain_safe_settings(urls)
        
        # Run advertools crawl_headers
        adv.crawl_headers(urls, output_file, custom_settings=custom_settings)
        
        return {
            "success": True, 
            "urls_processed": len(urls), 
            "output_file": output_file
        }
        
    except Exception as e:
        logger.error(f"Error processing chunk {output_file}: {e}")
        return {
            "success": False, 
            "error": str(e), 
            "urls_processed": 0,
            "output_file": output_file
        }

async def check_external_links_async(urls: list, audit_id: int, url_to_source_mapping: dict = None) -> dict:
    """
    Asynchronously check external links using chunked processing.
    This prevents blocking and handles domain collisions intelligently.
    """
    if not urls:
        logger.info(f"No external links to check for audit_id: {audit_id}")
        return {
            "unreachable_links": [],
            "broken_links": [],
            "permission_issues": [],
            "method_issues": [],
            "other_client_errors": []
        }
    
    # Limit total URLs to prevent excessive processing
    MAX_EXTERNAL_LINKS = 100
    if len(urls) > MAX_EXTERNAL_LINKS:
        logger.warning(f"Too many external links ({len(urls)}) for audit_id: {audit_id}. Limiting to {MAX_EXTERNAL_LINKS}")
        urls = urls[:MAX_EXTERNAL_LINKS]
    
    # Create domain-aware chunks
    chunks = chunk_urls_by_domain(urls, max_chunk_size=20)
    logger.info(f"Processing {len(urls)} external links in {len(chunks)} chunks for audit_id: {audit_id}")
    
    # Prepare output files for each chunk
    os.makedirs('results', exist_ok=True)
    chunk_files = [f"results/headers_{audit_id}_chunk_{i}.jl" for i in range(len(chunks))]
    
    # Semaphore to limit concurrent chunks (prevents overwhelming the system)
    semaphore = asyncio.Semaphore(3)  # Max 3 concurrent chunks
    
    async def process_chunk_async(chunk_urls: list, output_file: str, chunk_id: int):
        """Process a single chunk asynchronously."""
        async with semaphore:
            logger.info(f"Processing chunk {chunk_id} with {len(chunk_urls)} URLs for audit_id: {audit_id}")
            
            loop = asyncio.get_event_loop()
            
            # Run advertools in thread pool to avoid blocking
            with ThreadPoolExecutor(max_workers=1) as executor:
                try:
                    # Set timeout for individual chunk processing
                    result = await asyncio.wait_for(
                        loop.run_in_executor(executor, run_advertools_chunk, chunk_urls, output_file),
                        timeout=300  # 5 minutes per chunk
                    )
                    return result
                except asyncio.TimeoutError:
                    logger.error(f"Chunk {chunk_id} timed out for audit_id: {audit_id}")
                    return {
                        "success": False,
                        "error": "Timeout",
                        "urls_processed": 0,
                        "output_file": output_file
                    }
                except Exception as e:
                    logger.error(f"Error processing chunk {chunk_id} for audit_id: {audit_id}: {e}")
                    return {
                        "success": False,
                        "error": str(e),
                        "urls_processed": 0,
                        "output_file": output_file
                    }
    
    # Process all chunks concurrently
    start_time = time.time()
    tasks = [
        process_chunk_async(chunk, chunk_file, i) 
        for i, (chunk, chunk_file) in enumerate(zip(chunks, chunk_files))
    ]
    
    try:
        # Wait for all chunks with overall timeout
        chunk_results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=600  # 10 minutes total
        )
    except asyncio.TimeoutError:
        logger.error(f"Overall external link checking timed out for audit_id: {audit_id}")
        chunk_results = [{"success": False, "error": "Overall timeout", "output_file": f} for f in chunk_files]
    
    elapsed_time = time.time() - start_time
    logger.info(f"External link checking completed for audit_id: {audit_id} in {elapsed_time:.2f} seconds")
    
    # Merge results from all chunks
    external_links_report = {
        "unreachable_links": [],
        "broken_links": [],
        "permission_issues": [],
        "method_issues": [],
        "other_client_errors": []
    }
    
    successful_chunks = 0
    total_urls_processed = 0
    
    for i, result in enumerate(chunk_results):
        if isinstance(result, Exception):
            logger.error(f"Chunk {i} failed with exception: {result}")
            continue
            
        if not result.get("success", False):
            logger.warning(f"Chunk {i} failed: {result.get('error', 'Unknown error')}")
            continue
        
        successful_chunks += 1
        total_urls_processed += result.get("urls_processed", 0)
        
        # Process the chunk results
        chunk_file = result.get("output_file")
        if chunk_file and os.path.exists(chunk_file):
            try:
                headers_df = pd.read_json(chunk_file, lines=True)
                
                if not headers_df.empty and 'status' in headers_df.columns:
                    headers_df['status'] = headers_df['status'].fillna(-1).astype(int)
                    error_headers = headers_df[~headers_df['status'].between(200, 399)]
                    
                    for _, row in error_headers.iterrows():
                        status = row['status']
                        url = row.get('url', 'Unknown')
                        # Use actual source URL from mapping, fallback to 'External Link Check'
                        source_url = 'External Link Check'
                        if url_to_source_mapping and url in url_to_source_mapping:
                            source_url = url_to_source_mapping[url]
                        
                        link_info = {
                            'url': url,
                            'status': 'Unreachable' if status == -1 else status,
                            'source_url': source_url
                        }
                        
                        if status == -1:
                            external_links_report["unreachable_links"].append(link_info)
                        elif status in [404, 410]:
                            external_links_report["broken_links"].append(link_info)
                        elif status == 403:
                            external_links_report["permission_issues"].append(link_info)
                        elif status == 405:
                            external_links_report["method_issues"].append(link_info)
                        elif 400 <= status < 500:
                            external_links_report["other_client_errors"].append(link_info)
                
            except Exception as e:
                logger.error(f"Error processing results from {chunk_file}: {e}")
            finally:
                # Clean up chunk file
                try:
                    os.remove(chunk_file)
                except OSError:
                    pass
    
    logger.info(f"External link summary for audit_id {audit_id}: {successful_chunks}/{len(chunks)} chunks successful, {total_urls_processed} URLs processed")
    
    return external_links_report

# --- End Async External Link Functions ---