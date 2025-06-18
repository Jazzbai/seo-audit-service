import advertools as adv
from celery import chain, group
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
    The second step in the main audit. Takes the file path from the crawl task,
    reads the advertools crawl data, transforms it into our desired report
    format, saves that initial report to the database, and then kicks off
    the external link check.
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
        # Before creating the report, replace any pandas NaN/NaT values in the
        # source_url column with None, which serializes to 'null' in JSON.
        # This prevents database errors for broken pages that have no referrer.
        broken_links_df.rename(columns={referer_col: 'source_url'}, inplace=True)
        broken_links_df['source_url'] = broken_links_df['source_url'].where(pd.notna(broken_links_df['source_url']), None)
        broken_links_report = broken_links_df[['url', 'status', 'source_url']].to_dict('records')
    else:
        broken_links_report = broken_links_df[['url', 'status']].to_dict('records')

    # --- Section 3: Extract external links for the next step ---
    # To get the domain for our internal link regex, we parse the first URL
    # in the crawl data, which is the starting URL.
    try:
        main_domain = urlparse(crawl_df['url'][0]).netloc
        link_df = adv.crawlytics.links(crawl_df, internal_url_regex=main_domain)
        
        # Filter for external links
        external_links_df = link_df[~link_df['internal']].copy()

        # --- Data Cleaning & Structuring ---
        # Clean the DataFrame by removing rows with missing links and ensure all
        # link data is string type to prevent errors.
        # We also rename the 'url' column from crawlytics (which is the source page)
        # to 'source_url' for clarity and select the columns we need.
        external_links_df.dropna(subset=['link'], inplace=True)
        external_links_df['link'] = external_links_df['link'].astype(str)
        
        # Create a list of dicts to pass to the next task, preserving the source.
        links_to_check = external_links_df.rename(
            columns={'url': 'source_url'}
        )[['link', 'source_url']].to_dict('records')

        # For logging, we still want a unique count.
        unique_links = len(external_links_df['link'].unique())
        logger.info(f"Found {unique_links} unique external links to check for audit_id: {audit_id}")

    except Exception as e:
        logger.error(f"Failed to extract external links for audit_id {audit_id}: {e}", exc_info=True)
        links_to_check = []
        

    # --- Section 4: Assemble the final, structured report ---
    total_pages = len(page_level_report)
    final_report = {
        "status": "ANALYSIS_COMPLETE", # This is now an intermediate status
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

    # --- Section 5: Save intermediate report and pass data to the next task ---
    db = SessionLocal()
    try:
        audit = db.query(Audit).filter(Audit.id == audit_id).first()
        if audit:
            audit.status = "ANALYZING_EXTERNAL" # New intermediate status
            audit.report_json = final_report
            db.commit()
            logger.info(f"Successfully saved initial report for audit_id: {audit_id}")
    finally:
        db.close()
    
    # Pass the crawl file path and the list of links to the next task in the chain.
    return {"crawl_output_file": crawl_output_file, "links_to_check": links_to_check}

@celery_app.task(bind=True)
def check_external_links(self, previous_task_output: dict, audit_id: int) -> dict:
    """
    Checks the status of all external links found during the crawl.

    This task receives a list of external links, uses the efficient `crawl_headers`
    function to make HEAD requests, and identifies any links that return a
    client or server error (4xx or 5xx status codes).
    """
    links_to_check = previous_task_output.get('links_to_check', [])
    crawl_output_file = previous_task_output.get('crawl_output_file')

    if not links_to_check:
        logger.info(f"No external links to check for audit_id: {audit_id}. Skipping.")
        return {"external_links_report": {}, "crawl_output_file": crawl_output_file}

    # Create a DataFrame from the input list of dictionaries.
    links_df = pd.DataFrame(links_to_check)
    
    # Get a unique list of URLs to check to avoid redundant requests.
    unique_urls_to_check = links_df['link'].unique().tolist()
    
    logger.info(f"Checking {len(unique_urls_to_check)} unique external links for audit_id: {audit_id}")
    
    # Define a temporary file for the header crawl results.
    os.makedirs('results', exist_ok=True)
    headers_output_file = f"results/headers_results_{audit_id}.jl"

    # --- Learning Note: DNS Timeouts ---
    # `DOWNLOAD_TIMEOUT` is crucial for performance. When checking thousands of
    # external links, some domains may not exist or have DNS issues. Without a
    # timeout, the crawler could hang for a long time on each one. Setting a
    # reasonable timeout (e.g., 10-15 seconds) ensures the process moves on.
    # `DNS_TIMEOUT` specifically handles the time to wait for a DNS lookup.
    custom_settings = {
        'ROBOTSTXT_OBEY': False, # External sites' robots.txt are not relevant here
        'USER_AGENT': 'Python-SEOAuditAgent/1.0 (External Link Checker)',
        'DOWNLOAD_TIMEOUT': 15,
        'DNS_TIMEOUT': 10,
    }

    external_links_report = {
        "unreachable_links": [],
        "broken_links": [],
        "permission_issues": [],
        "method_issues": [],
        "other_client_errors": []
    }

    try:
        adv.crawl_headers(unique_urls_to_check, headers_output_file, custom_settings=custom_settings)
        headers_df = pd.read_json(headers_output_file, lines=True)

        # --- Data Cleaning: Handle Network/DNS Failures ---
        # If the crawler fails to get a response (e.g., DNS error), the status
        # will be NaN. We fill these with a placeholder (-1) to represent an
        # "unreachable" link and prevent crashes during type conversion.
        if 'status' in headers_df.columns:
            headers_df['status'].fillna(-1, inplace=True)
            headers_df['status'] = headers_df['status'].astype(int)
        else:
            # If there's no status column at all, create it and fill with -1.
            # This can happen if every single request fails at the network level.
            headers_df['status'] = -1

        # --- Learning Notes: Categorizing HTTP Errors ---
        # Instead of treating all 4xx/5xx errors as "broken", we now categorize
        # them to provide a more accurate and actionable report.
        # - -1 (Our custom code): Unreachable links (DNS/network errors).
        # - 404/410: Truly broken links. The resource does not exist.
        # - 403: Permission issue. The server is blocking our crawler. The link
        #   is likely fine for human users.
        # - 405: Method Not Allowed. Our checker used a method (HEAD) that the
        #   server doesn't support for that URL. The link is likely fine.
        # - Other 4xx: Catch-all for other client-side errors.
        
        # We now consider any non-2xx/3xx status as an "error" to be categorized
        error_headers_df = headers_df[~headers_df['status'].between(200, 399)].copy()

        # Merge with original links_df to get the source_url for each error
        report_df = pd.merge(
            links_df,
            error_headers_df[['url', 'status']],
            left_on='link',
            right_on='url',
            how='inner'
        ).rename(columns={'link': 'url_to'}) # Rename to avoid confusion with source 'url'
        
        # Now, iterate through the merged DataFrame to categorize
        for _, row in report_df.iterrows():
            status = row['status']
            # For unreachable links, the status is our placeholder, so we don't
            # include it in the report for clarity.
            if status == -1:
                link_info = {'url': row['url_to'], 'status': 'Unreachable', 'source_url': row['source_url']}
                external_links_report["unreachable_links"].append(link_info)
            else:
                link_info = {'url': row['url_to'], 'status': status, 'source_url': row['source_url']}
                if status in [404, 410]:
                    external_links_report["broken_links"].append(link_info)
                elif status == 403:
                    external_links_report["permission_issues"].append(link_info)
                elif status == 405:
                    external_links_report["method_issues"].append(link_info)
                elif 400 <= status < 500: # Catch other 4xx errors
                    external_links_report["other_client_errors"].append(link_info)

        logger.info(
            f"External link check complete for audit_id: {audit_id}. "
            f"Found: {len(external_links_report['unreachable_links'])} unreachable, "
            f"{len(external_links_report['broken_links'])} broken, "
            f"{len(external_links_report['permission_issues'])} permission issues, "
            f"{len(external_links_report['method_issues'])} method issues."
        )

    except Exception as e:
        logger.error(f"Failed to check external links for audit_id {audit_id}: {e}", exc_info=True)
        # Return empty report on failure
        external_links_report = {key: [] for key in external_links_report}
    finally:
        # Clean up the temporary header results file
        if os.path.exists(headers_output_file):
            try:
                os.remove(headers_output_file)
            except OSError as e:
                logger.warning(f"Error cleaning up headers file {headers_output_file}: {e}")

    return {"external_links_report": external_links_report, "crawl_output_file": crawl_output_file}

@celery_app.task(bind=True)
def save_final_report(self, previous_task_output: dict, audit_id: int):
    """
    Saves the final, complete report to the database.

    This is the last step. It merges the results from the external link check
    into the main report, updates the audit status to COMPLETE, and cleans up
    any remaining temporary files.
    """
    logger.info(f"Saving final report for audit_id: {audit_id}")
    
    external_links_report = previous_task_output.get('external_links_report', {})
    crawl_output_file = previous_task_output.get('crawl_output_file')

    # Ensure we have a default structure if the report is missing
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

        # Get the intermediate report that was saved earlier.
        # We explicitly copy it to avoid modifying the object in-place.
        report_json = audit.report_json.copy()

        # Update summary with the new, detailed external link counts.
        summary = report_json["summary"]
        summary["external_unreachable_links_found"] = len(unreachable_links)
        summary["external_broken_links_found"] = len(broken_links)
        summary["external_permission_issues_found"] = len(permission_issues)
        summary["external_method_issues_found"] = len(method_issues)
        summary["external_other_client_errors_found"] = len(other_client_errors)

        # --- Re-order the report for better readability ---
        # Create a new dictionary with the desired key order, adding the
        # new categorized link lists.
        final_report = {
            "status": "COMPLETE",
            "audit_id": audit.id,
            "summary": summary,
            "external_unreachable_links": unreachable_links,
            "external_broken_links": broken_links,
            "external_permission_issue_links": permission_issues,
            "external_method_issue_links": method_issues,
            "external_other_client_errors": other_client_errors,
            "internal_broken_links": report_json["internal_broken_links"],
            "page_level_report": report_json["page_level_report"]
        }
        
        # Update the audit record with the final, re-ordered report.
        audit.report_json = final_report
        audit.status = "COMPLETE"
        audit.completed_at = datetime.datetime.utcnow()
        db.commit()
        logger.info(f"Successfully saved final report for audit_id: {audit_id}")

    except Exception as e:
        logger.error(f"Failed to save final report for audit_id {audit_id}: {e}", exc_info=True)
        if audit:
            audit.status = "ERROR"
            db.commit()
    finally:
        db.close()

    # Final cleanup of the main crawl file.
    if crawl_output_file and os.path.exists(crawl_output_file):
        try:
            os.remove(crawl_output_file)
            logger.info(f"Cleaned up main crawl file: {crawl_output_file}")
        except OSError as e:
            logger.warning(f"Error cleaning up main crawl file {crawl_output_file}: {e}")

    return {"status": "SUCCESS", "audit_id": audit_id}

@celery_app.task(bind=True)
def run_full_audit(self, audit_id: int, url: str, max_pages: int = 100):
    """
    The main orchestrator task that chains together the full audit process.
    1. Crawl the site.
    2. Compile the main (internal) report.
    3. Check all discovered external links.
    4. Save the final combined report.
    """
    logger.info(f"Launched audit workflow for url: {url} (Audit ID: {audit_id})")
    # This chain ensures that each task runs in sequence, passing its output
    # to the next task in the chain.
    task_chain = chain(
        run_advertools_crawl.s(audit_id=audit_id, url=url, max_pages=max_pages),
        compile_report_from_crawl.s(audit_id=audit_id),
        check_external_links.s(audit_id=audit_id),
        save_final_report.s(audit_id=audit_id)
    )
    task_chain.apply_async()
    return {'message': 'Audit workflow successfully launched.'}

@celery_app.task
def debug_task():
    print('Request: {0!r}'.format(debug_task.request))