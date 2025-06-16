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

# --- Learning Notes: Stop Words ---
# Stop words are common words (like 'a', 'the', 'is') that are filtered out
# during natural language processing because they provide little semantic value.
# Removing them helps us focus on the most meaningful keywords in a text.
# While libraries like NLTK or spaCy have extensive lists, a simple, curated
# list like this is often sufficient and avoids adding heavy dependencies.
STOP_WORDS = {
    # Standard English stop words
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
    # Website-specific noise words we have observed
    'page', 'archives', 'club', 'tag', 'category', 'author', 'events',
    # Months
    'jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec',
    'january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 
    'september', 'october', 'november', 'december'
}

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def _get_top_words(series: pd.Series, n: int = 10) -> list:
    """Helper function to extract top N words from a pandas Series of text."""
    # Drop missing values and concatenate all text into a single string
    text = series.dropna().str.cat(sep=' ').lower()
    
    # Find all words, ignoring punctuation
    words = re.findall(r'\b[a-z]+\b', text)
    
    # Filter out stop words and count frequencies
    word_counts = Counter(w for w in words if w not in STOP_WORDS)
    
    # Return the N most common words and their counts
    return word_counts.most_common(n)

@celery_app.task(bind=True)
def run_advertools_crawl(self, audit_id: int, url: str, max_pages: int) -> str:
    """
    Runs a polite and comprehensive advertools crawl for a given URL.

    This task is the first step in the audit. It delegates the complex,
    I/O-bound work of crawling to a specialized library (`advertools`).
    It saves the raw results to a file and passes the file path to the
    next task for processing.

    Args:
        audit_id: The ID for the audit to associate the crawl with.
        url: The starting URL for the crawl.
        max_pages: The maximum number of pages to crawl.

    Returns:
        The file path to the JSON Lines (.jl) file containing the crawl results.
    """
    logger.info(f"Starting advertools crawl for audit_id: {audit_id}, url: {url}")
    
    # Define unique output and log files for this specific audit.
    # We create dedicated directories to keep the project root clean.
    os.makedirs('results', exist_ok=True)
    os.makedirs('logs', exist_ok=True)
    output_file = f"results/audit_results_{audit_id}.jl"
    log_file = f"logs/audit_log_{audit_id}.log"

    # --- Learning Notes on `custom_settings` ---
    # This is how we make our crawler robust and polite.
    # 1. `DOWNLOAD_DELAY`: The most important setting for avoiding rate-limiting.
    #    It forces the crawler to wait for this many seconds between requests to
    #    the same domain. A value of 1-3 is generally considered polite.
    # 2. `CONCURRENT_REQUESTS_PER_DOMAIN`: Further limits the crawler to only
    #    make this many requests at once. Setting it to 1 makes the crawl
    #    strictly sequential for the domain, which is very safe.
    # 3. `ROBOTSTXT_OBEY`: For a comprehensive audit, we often want to see
    #    pages that might be disallowed for regular bots. We set this to False
    #    to ensure we analyze the entire public site.
    # 4. `CLOSESPIDER_PAGECOUNT`: A crucial safety net. It prevents runaway crawls
    #    on massive websites by stopping after a set number of pages.
    # 5. `LOG_FILE`: Essential for debugging. All of the crawler's internal
    #    actions and any errors will be saved here.
    custom_settings = {
        'DOWNLOAD_DELAY': 1,
        'CONCURRENT_REQUESTS_PER_DOMAIN': 2,
        'ROBOTSTXT_OBEY': False,
        'CLOSESPIDER_PAGECOUNT': max_pages,
        'USER_AGENT': 'Python-SEOAuditAgent/1.0 (+https://github.com/user/repo)', # Good practice to identify your bot
        'LOG_FILE': log_file,
    }

    try:
        adv.crawl(
            url_list=url,
            output_file=output_file,
            follow_links=True,
            custom_settings=custom_settings
        )
        logger.info(f"Crawl finished for audit_id: {audit_id}. Results in: {output_file}")
        return output_file
    except Exception as e:
        logger.error(f"Advertools crawl failed for audit_id: {audit_id} with error: {e}", exc_info=True)
        # In case of a catastrophic failure, update the audit and stop.
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
        raise  # Re-raise exception to mark the Celery task as FAILED

@celery_app.task(bind=True)
def compile_report_from_crawl(self, crawl_output_file: str, audit_id: int) -> dict:
    """
    The second and final step. Takes the file path from the crawl task,
    reads the advertools crawl data, transforms it into our desired report
    format, and saves it to the database.
    """
    logger.info(f"Compiling report for audit_id: {audit_id} from file: {crawl_output_file}")

    try:
        crawl_df = pd.read_json(crawl_output_file, lines=True)
    except FileNotFoundError:
        logger.error(f"Crawl output file not found: {crawl_output_file} for audit_id: {audit_id}")
        return {"status": "ERROR", "message": "Crawl output file not found."}

    # --- Section 1: Generate the detailed per-page report and collect summary stats ---
    page_level_report = {}
    pages_with_title = 0
    pages_with_meta_desc = 0
    pages_with_one_h1 = 0
    pages_with_multiple_h1s = 0
    pages_with_no_h1 = 0

    for _, row in crawl_df.iterrows():
        url = row.get('url')
        if not url:
            continue

        page_report = []
        
        # 1. Title Check
        title = row.get('title')
        if pd.notna(title) and title:
            pages_with_title += 1
            page_report.append({"status": "SUCCESS", "check": "title", "value": title.strip(), "message": "Title found."})
        else:
            page_report.append({"status": "FAILURE", "check": "title", "value": None, "message": "Title tag not found or is empty."})
            
        # 2. Meta Description Check
        meta_desc = row.get('meta_desc')
        if pd.notna(meta_desc) and meta_desc:
            pages_with_meta_desc += 1
            page_report.append({"status": "SUCCESS", "check": "meta_description", "value": meta_desc.strip(), "message": "Meta description found."})
        else:
            page_report.append({"status": "FAILURE", "check": "meta_description", "value": None, "message": "Meta description not found or is empty."})
        
        # 3. H1 Heading Check
        h1s = []
        h1_raw = row.get('h1')
        if pd.notna(h1_raw) and h1_raw:
            h1s = [h.strip() for h in h1_raw.split('@@') if h.strip()]
        
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

    # --- Section 2: Generate the broken links report ---
    # We identify broken links as any URL that returned a status code of 400 or greater.
    broken_links_df = crawl_df[crawl_df['status'] >= 400].copy()
    broken_links_report = []
    
    referer_col = 'request_headers_Referer'
    if referer_col in broken_links_df.columns:
        broken_links_df.rename(columns={referer_col: 'source_url'}, inplace=True)
        broken_links_report = broken_links_df[['url', 'status', 'source_url']].to_dict('records')
    else:
        broken_links_report = broken_links_df[['url', 'status']].to_dict('records')

    # --- Section 3: Assemble the final, structured report ---
    total_pages = len(page_level_report)
    final_report = {
        "status": "COMPLETE",
        "audit_id": audit_id,
        "summary": {
            "total_pages_analyzed": total_pages,
            "broken_links_found": len(broken_links_report),
            "pages_missing_title": total_pages - pages_with_title,
            "pages_missing_meta_description": total_pages - pages_with_meta_desc,
            "pages_with_correct_h1": pages_with_one_h1,
            "pages_with_multiple_h1s": pages_with_multiple_h1s,
            "pages_with_no_h1": pages_with_no_h1,
            "top_10_title_words": _get_top_words(crawl_df['title']),
            "top_10_h1_words": _get_top_words(crawl_df['h1']),
        },
        "broken_links": broken_links_report,
        "page_level_report": page_level_report
    }

    # Update Database
    db = SessionLocal()
    try:
        audit = db.query(Audit).filter(Audit.id == audit_id).first()
        if audit:
            audit.status = "COMPLETE"
            audit.report_json = final_report
            audit.completed_at = datetime.datetime.utcnow()
            db.commit()
            logger.info(f"Successfully saved report for audit_id: {audit_id}")
    finally:
        db.close()
    
    # Clean up the crawl files
    try:
        os.remove(crawl_output_file)
        # We are intentionally NOT deleting the log file. It's a valuable
        # artifact for debugging any crawl or analysis issues.
    except OSError as e:
        logger.warning(f"Error cleaning up result file for audit_id {audit_id}: {e}")

    return final_report

@celery_app.task(bind=True)
def run_full_audit(self, audit_id: int, url: str, max_pages: int = 100):
    """
    The main orchestrator task that chains together the audit process.
    It starts with a crawl and then compiles the report.
    """
    logger.info(f"Launched audit workflow for url: {url} (Audit ID: {audit_id})")
    # This chain ensures that the report compilation task only runs after the
    # crawl task has successfully completed and passed its output file path.
    # The `s()` signature creates a 'subtask' that can be part of a chain.
    task_chain = chain(
        run_advertools_crawl.s(audit_id, url, max_pages),
        compile_report_from_crawl.s(audit_id)
    )
    task_chain.apply_async()
    return {'message': 'Audit workflow successfully launched.'}

@celery_app.task
def debug_task():
    print('Request: {0!r}'.format(debug_task.request))