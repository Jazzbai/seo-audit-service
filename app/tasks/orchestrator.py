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
from collections import Counter
from urllib.parse import urlparse
import httpx
from app.core.config import settings

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

@celery_app.task(bind=True)
def run_advertools_crawl(self, audit_id: int, url: str, max_pages: int) -> str:
    logger.info(f"Starting advertools crawl for audit_id: {audit_id}, url: {url}")
    os.makedirs('results', exist_ok=True)
    os.makedirs('logs', exist_ok=True)
    output_file = f"results/audit_results_{audit_id}.jl"
    log_file = f"logs/audit_log_{audit_id}.log"
    custom_settings = {
        'DOWNLOAD_DELAY': 1,
        'CONCURRENT_REQUESTS_PER_DOMAIN': 2,
        'ROBOTSTXT_OBEY': False,
        'CLOSESPIDER_PAGECOUNT': max_pages,
        'USER_AGENT': 'Python-SEOAuditAgent/1.0 (+https://github.com/user/repo)',
        'LOG_FILE': log_file,
    }
    try:
        adv.crawl(url_list=url, output_file=output_file, follow_links=True, custom_settings=custom_settings)
        logger.info(f"Crawl finished for audit_id: {audit_id}. Results in: {output_file}")
        return output_file
    except Exception as e:
        logger.error(f"Advertools crawl failed for audit_id: {audit_id} with error: {e}", exc_info=True)
        db = SessionLocal()
        try:
            audit = db.query(Audit).filter(Audit.id == audit_id).first()
            if audit:
                audit.status = "ERROR"
                audit.report_json = {"status": "ERROR", "message": f"Crawling failed: {e!r}"}
                audit.completed_at = datetime.datetime.utcnow()
                db.commit()
        finally:
            db.close()
        raise

@celery_app.task(bind=True)
def compile_report_from_crawl(self, crawl_output_file: str, audit_id: int) -> dict:
    logger.info(f"Compiling report for audit_id: {audit_id} from file: {crawl_output_file}")
    try:
        crawl_df = pd.read_json(crawl_output_file, lines=True)
    except FileNotFoundError:
        logger.error(f"Crawl output file not found: {crawl_output_file} for audit_id: {audit_id}")
        return {"status": "ERROR", "message": "Crawl output file not found."}

    page_level_report = {}
    pages_with_title, pages_with_meta_desc, pages_with_one_h1, pages_with_multiple_h1s, pages_with_no_h1 = 0, 0, 0, 0, 0
    for _, row in crawl_df.iterrows():
        url = row.get('url')
        if not url: continue
        page_report = []
        title = row.get('title')
        if pd.notna(title) and title:
            pages_with_title += 1
            page_report.append({"status": "SUCCESS", "check": "title", "value": title.strip(), "message": "Title found."})
        else:
            page_report.append({"status": "FAILURE", "check": "title", "value": None, "message": "Title tag not found or is empty."})
        meta_desc = row.get('meta_desc')
        if pd.notna(meta_desc) and meta_desc:
            pages_with_meta_desc += 1
            page_report.append({"status": "SUCCESS", "check": "meta_description", "value": meta_desc.strip(), "message": "Meta description found."})
        else:
            page_report.append({"status": "FAILURE", "check": "meta_description", "value": None, "message": "Meta description not found or is empty."})
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

    broken_links_df = crawl_df[crawl_df['status'] >= 400].copy()
    broken_links_report = []
    referer_col = 'request_headers_Referer'
    if referer_col in broken_links_df.columns:
        broken_links_df.rename(columns={referer_col: 'source_url'}, inplace=True)
        broken_links_df['source_url'] = broken_links_df['source_url'].where(pd.notna(broken_links_df['source_url']), None)
        broken_links_report = broken_links_df[['url', 'status', 'source_url']].to_dict('records')
    else:
        broken_links_report = broken_links_df[['url', 'status']].to_dict('records')

    links_to_check = []
    try:
        main_domain = urlparse(crawl_df['url'][0]).netloc
        link_df = adv.crawlytics.links(crawl_df, internal_url_regex=main_domain)
        external_links_df = link_df[~link_df['internal']].copy()
        external_links_df.dropna(subset=['link'], inplace=True)
        external_links_df['link'] = external_links_df['link'].astype(str)
        links_to_check = external_links_df.rename(columns={'url': 'source_url'})[['link', 'source_url']].to_dict('records')
        logger.info(f"Found {len(external_links_df['link'].unique())} unique external links to check for audit_id: {audit_id}")
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
            "top_10_title_words": _get_top_words(crawl_df['title']),
            "top_10_h1_words": _get_top_words(crawl_df['h1']),
        },
        "internal_broken_links": broken_links_report,
        "page_level_report": page_level_report
    }

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

@celery_app.task(bind=True)
def check_external_links(self, previous_task_output: dict, audit_id: int) -> dict:
    links_to_check = previous_task_output.get('links_to_check', [])
    crawl_output_file = previous_task_output.get('crawl_output_file')
    if not links_to_check:
        logger.info(f"No external links to check for audit_id: {audit_id}. Skipping.")
        return {"crawl_output_file": crawl_output_file, "external_links_report": {}}

    links_df = pd.DataFrame(links_to_check)
    unique_urls_to_check = links_df['link'].unique().tolist()
    logger.info(f"Checking {len(unique_urls_to_check)} unique external links for audit_id: {audit_id}")
    os.makedirs('results', exist_ok=True)
    headers_output_file = f"results/headers_results_{audit_id}.jl"
    custom_settings = {
        'ROBOTSTXT_OBEY': False,
        'USER_AGENT': 'Python-SEOAuditAgent/1.0 (External Link Checker)',
        'DOWNLOAD_TIMEOUT': 15,
        'DNS_TIMEOUT': 10,
    }
    external_links_report = {"unreachable_links": [], "broken_links": [], "permission_issues": [], "method_issues": [], "other_client_errors": []}
    try:
        adv.crawl_headers(unique_urls_to_check, headers_output_file, custom_settings=custom_settings)
        headers_df = pd.read_json(headers_output_file, lines=True)
        if 'status' in headers_df.columns:
            headers_df['status'] = headers_df['status'].fillna(-1)
            headers_df['status'] = headers_df['status'].astype(int)
        else:
            headers_df['status'] = -1
        error_headers_df = headers_df[~headers_df['status'].between(200, 399)].copy()
        report_df = pd.merge(links_df, error_headers_df[['url', 'status']], left_on='link', right_on='url', how='inner').rename(columns={'link': 'url_to'})
        for _, row in report_df.iterrows():
            status = row['status']
            link_info = {'url': row['url_to'], 'status': 'Unreachable' if status == -1 else status, 'source_url': row['source_url']}
            if status == -1: external_links_report["unreachable_links"].append(link_info)
            elif status in [404, 410]: external_links_report["broken_links"].append(link_info)
            elif status == 403: external_links_report["permission_issues"].append(link_info)
            elif status == 405: external_links_report["method_issues"].append(link_info)
            elif 400 <= status < 500: external_links_report["other_client_errors"].append(link_info)
    except Exception as e:
        logger.error(f"Failed to check external links for audit_id {audit_id}: {e}", exc_info=True)
    finally:
        if os.path.exists(headers_output_file):
            try: os.remove(headers_output_file)
            except OSError as e: logger.warning(f"Error cleaning up headers file {headers_output_file}: {e}")
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
        callback_payload = {
            "user_id": audit.user_id,
            "user_audit_report_request_id": audit.user_audit_report_request_id,
            "status": audit.status,
            "audit_id": audit.id,
            "url": audit.url,
            "created_at": audit.created_at.isoformat(),
            "completed_at": audit.completed_at.isoformat() if audit.completed_at else None,
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