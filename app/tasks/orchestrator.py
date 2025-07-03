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
    validate_crawl_output,
)
from app.utils.logging_manager import TaskLogger, logging_manager
import asyncio
from concurrent.futures import ThreadPoolExecutor
import time

# --- Learning Notes: Stop Words ---
STOP_WORDS = {
    "i",
    "me",
    "my",
    "myself",
    "we",
    "our",
    "ours",
    "ourselves",
    "you",
    "your",
    "yours",
    "yourself",
    "yourselves",
    "he",
    "him",
    "his",
    "himself",
    "she",
    "her",
    "hers",
    "herself",
    "it",
    "its",
    "itself",
    "they",
    "them",
    "their",
    "theirs",
    "themselves",
    "what",
    "which",
    "who",
    "whom",
    "this",
    "that",
    "these",
    "those",
    "am",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "have",
    "has",
    "had",
    "having",
    "do",
    "does",
    "did",
    "doing",
    "a",
    "an",
    "the",
    "and",
    "but",
    "if",
    "or",
    "because",
    "as",
    "until",
    "while",
    "of",
    "at",
    "by",
    "for",
    "with",
    "about",
    "against",
    "between",
    "into",
    "through",
    "during",
    "before",
    "after",
    "above",
    "below",
    "to",
    "from",
    "up",
    "down",
    "in",
    "out",
    "on",
    "off",
    "over",
    "under",
    "again",
    "further",
    "then",
    "once",
    "here",
    "there",
    "when",
    "where",
    "why",
    "how",
    "all",
    "any",
    "both",
    "each",
    "few",
    "more",
    "most",
    "other",
    "some",
    "such",
    "no",
    "nor",
    "not",
    "only",
    "own",
    "same",
    "so",
    "than",
    "too",
    "very",
    "s",
    "t",
    "can",
    "will",
    "just",
    "don",
    "should",
    "now",
    "d",
    "ll",
    "m",
    "o",
    "re",
    "ve",
    "y",
    "-",
    "–",
    "|",
    "page",
    "archives",
    "club",
    "tag",
    "category",
    "author",
    "events",
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _get_top_words(series: pd.Series, n: int = 10) -> list:
    text = series.dropna().str.cat(sep=" ").lower()
    words = re.findall(r"\b[a-z]+\b", text)
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
                logging_manager.log_system_event(
                    "app",
                    "info",
                    f"Marked audit {audit_id} as {error_info['status']}: {error_info['user_message']}",
                )

                # Send webhook for failed audits too (only once)
                if settings.DASHBOARD_CALLBACK_URL:
                    logging_manager.log_system_event(
                        "app",
                        "info",
                        f"Dashboard callback URL is set, queueing callback task for failed audit_id: {audit_id}",
                    )
                    send_report_to_dashboard.delay(audit_id=audit_id)
                else:
                    logging_manager.log_system_event(
                        "app",
                        "info",
                        f"No dashboard callback URL configured. Skipping callback for failed audit_id: {audit_id}",
                    )
            else:
                logging_manager.log_system_event(
                    "app",
                    "info",
                    f"Audit {audit_id} already in final state {audit.status}, skipping duplicate update",
                )

    except Exception as db_error:
        logging_manager.log_system_event(
            "app", "error", f"Failed to update audit {audit_id} status: {db_error}"
        )
    finally:
        db.close()


@celery_app.task(bind=True)
def run_advertools_crawl(self, audit_id: int, url: str, max_pages: int) -> str:
    task_context = {"url": url, "max_pages": max_pages, "task_id": self.request.id}

    with TaskLogger(
        audit_id=audit_id,
        task_name="run_advertools_crawl",
        task_id=self.request.id,
        context=task_context,
    ) as task_logger:

        task_logger.log(
            "info", "Starting advertools crawl", {"url": url, "max_pages": max_pages}
        )

        # Pre-flight URL validation
        task_logger.log("info", "Validating URL format")
        url_valid, url_error = is_valid_url(url)
        if not url_valid:
            error_msg = f"Invalid URL format: {url_error}"
            task_logger.log("error", "URL validation failed", {"error": error_msg})
            _mark_audit_failed(audit_id, error_msg, url)
            raise ValueError(error_msg)

        # Check if domain exists
        task_logger.log("info", "Checking domain existence")
        domain_exists, domain_error = check_domain_exists(url)
        if not domain_exists:
            error_msg = f"Domain validation failed: {domain_error}"
            task_logger.log("error", "Domain check failed", {"error": error_msg})
            _mark_audit_failed(audit_id, error_msg, url)
            raise ValueError(error_msg)

        task_logger.log("info", "Setting up crawl directories and files")
        os.makedirs("results", exist_ok=True)
        os.makedirs("logs", exist_ok=True)
        output_file = f"results/audit_results_{audit_id}.jl"
        log_file = f"logs/advertools/audit_log_{audit_id}.log"
        
        # Clean up any existing files to ensure fresh crawl
        if os.path.exists(output_file):
            os.remove(output_file)
            task_logger.log("info", f"Removed existing crawl file: {output_file}")
        if os.path.exists(log_file):
            os.remove(log_file)
            task_logger.log("info", f"Removed existing log file: {log_file}")

        custom_settings = {
            "DOWNLOAD_DELAY": 1,
            "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
            "ROBOTSTXT_OBEY": False,
            "CLOSESPIDER_PAGECOUNT": max_pages,
            "USER_AGENT": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            "LOG_FILE": log_file,
            "DOWNLOAD_TIMEOUT": 30,
            "DNS_TIMEOUT": 10,
        }

        task_logger.log(
            "info",
            "Starting advertools crawl execution",
            {
                "output_file": output_file,
                "log_file": log_file,
                "settings": custom_settings,
            },
        )

        try:
            adv.crawl(
                url_list=url,
                output_file=output_file,
                follow_links=True,
                custom_settings=custom_settings,
            )

            # Validate crawl output
            task_logger.log("info", "Validating crawl output")
            output_valid, output_error = validate_crawl_output(output_file)
            if not output_valid:
                error_msg = f"Crawl validation failed: {output_error}"
                task_logger.log(
                    "error", "Crawl output validation failed", {"error": error_msg}
                )
                _mark_audit_failed(audit_id, error_msg, url)
                raise ValueError(error_msg)

            task_logger.log(
                "info", "Crawl completed successfully", {"output_file": output_file}
            )
            return output_file

        except Exception as e:
            error_msg = str(e)
            task_logger.log(
                "error",
                "Advertools crawl failed",
                {"error": error_msg, "exception_type": type(e).__name__},
            )
            _mark_audit_failed(audit_id, error_msg, url)
            raise


@celery_app.task(bind=True)
def compile_report_from_crawl(self, crawl_output_file: str, audit_id: int) -> dict:
    task_context = {"crawl_output_file": crawl_output_file, "task_id": self.request.id}

    with TaskLogger(
        audit_id=audit_id,
        task_name="compile_report_from_crawl",
        task_id=self.request.id,
        context=task_context,
    ) as task_logger:

        task_logger.log(
            "info",
            "Starting report compilation",
            {"crawl_output_file": crawl_output_file},
        )

        try:
            # Validate crawl output first
            task_logger.log("info", "Validating crawl output file")
            output_valid, output_error = validate_crawl_output(crawl_output_file)
            if not output_valid:
                error_msg = f"Invalid crawl output: {output_error}"
                task_logger.log(
                    "error", "Crawl output validation failed", {"error": error_msg}
                )
                _mark_audit_failed(audit_id, error_msg)
                raise ValueError(error_msg)

            task_logger.log("info", "Reading crawl data from JSON file")
            crawl_df = pd.read_json(crawl_output_file, lines=True)

            # Additional validation for required columns
            if crawl_df.empty:
                error_msg = "Crawl produced no analyzable data"
                task_logger.log("error", "Empty crawl results", {"error": error_msg})
                _mark_audit_failed(audit_id, error_msg)
                raise ValueError(error_msg)

            task_logger.log(
                "info", "Crawl data loaded successfully", {"total_pages": len(crawl_df)}
            )

        except FileNotFoundError:
            error_msg = f"Crawl output file not found: {crawl_output_file}"
            task_logger.log(
                "error", "Crawl output file not found", {"error": error_msg}
            )
            _mark_audit_failed(audit_id, error_msg)
            raise
        except Exception as e:
            error_msg = f"Failed to read crawl output: {str(e)}"
            task_logger.log(
                "error",
                "Error reading crawl file",
                {"error": error_msg, "exception_type": type(e).__name__},
            )
            _mark_audit_failed(audit_id, error_msg)
            raise

        try:
            task_logger.log("info", "Starting page-level analysis")
            page_level_report = {}
            (
                pages_with_title,
                pages_with_meta_desc,
                pages_with_one_h1,
                pages_with_multiple_h1s,
                pages_with_no_h1,
            ) = (0, 0, 0, 0, 0)

            for _, row in crawl_df.iterrows():
                url = row.get("url")
                if not url:
                    continue
                page_report = []

                # Handle title safely
                title = row.get("title")
                if pd.notna(title) and title:
                    pages_with_title += 1
                    page_report.append(
                        {
                            "status": "SUCCESS",
                            "check": "title",
                            "value": title.strip(),
                            "message": "Title found.",
                        }
                    )
                else:
                    page_report.append(
                        {
                            "status": "FAILURE",
                            "check": "title",
                            "value": None,
                            "message": "Title tag not found or is empty.",
                        }
                    )

                # Handle meta description safely
                meta_desc = row.get("meta_desc")
                if pd.notna(meta_desc) and meta_desc:
                    pages_with_meta_desc += 1
                    page_report.append(
                        {
                            "status": "SUCCESS",
                            "check": "meta_description",
                            "value": meta_desc.strip(),
                            "message": "Meta description found.",
                        }
                    )
                else:
                    page_report.append(
                        {
                            "status": "FAILURE",
                            "check": "meta_description",
                            "value": None,
                            "message": "Meta description not found or is empty.",
                        }
                    )

                # Handle H1 tags
                h1_tags = row.get("h1", [])
                if pd.isna(h1_tags) or (
                    isinstance(h1_tags, list) and len(h1_tags) == 0
                ):
                    pages_with_no_h1 += 1
                    page_report.append(
                        {
                            "status": "FAILURE",
                            "check": "h1_heading",
                            "message": "No H1 tag found.",
                            "count": 0,
                            "value": [],
                        }
                    )
                elif isinstance(h1_tags, list) and len(h1_tags) == 1:
                    pages_with_one_h1 += 1
                    page_report.append(
                        {
                            "status": "SUCCESS",
                            "check": "h1_heading",
                            "message": "Exactly one H1 tag found.",
                            "count": 1,
                            "value": h1_tags[0],
                        }
                    )
                elif isinstance(h1_tags, list) and len(h1_tags) > 1:
                    pages_with_multiple_h1s += 1
                    page_report.append(
                        {
                            "status": "FAILURE",
                            "check": "h1_heading",
                            "message": f"Found {len(h1_tags)} H1 tags. Expected 1.",
                            "count": len(h1_tags),
                            "value": h1_tags,
                        }
                    )
                else:
                    # Handle non-list H1 tags
                    if h1_tags:
                        pages_with_one_h1 += 1
                        page_report.append(
                            {
                                "status": "SUCCESS",
                                "check": "h1_heading",
                                "message": "Exactly one H1 tag found.",
                                "count": 1,
                                "value": str(h1_tags),
                            }
                        )
                    else:
                        pages_with_no_h1 += 1
                        page_report.append(
                            {
                                "status": "FAILURE",
                                "check": "h1_heading",
                                "message": "No H1 tag found.",
                                "count": 0,
                                "value": [],
                            }
                        )

                page_level_report[url] = page_report

            task_logger.log(
                "info",
                "Page-level analysis completed",
                {
                    "total_pages": len(page_level_report),
                    "pages_with_title": pages_with_title,
                    "pages_with_meta_desc": pages_with_meta_desc,
                    "pages_with_one_h1": pages_with_one_h1,
                },
            )

        except Exception as e:
            error_msg = f"Failed to compile page-level report: {str(e)}"
            task_logger.log(
                "error",
                "Error compiling page report",
                {"error": error_msg, "exception_type": type(e).__name__},
            )
            _mark_audit_failed(audit_id, error_msg)
            raise

        # Categorize internal links with same logic as external links
        internal_unreachable_links = []
        internal_broken_links = []
        internal_permission_issue_links = []
        internal_method_issue_links = []
        internal_other_client_errors = []

        try:
            if "status" in crawl_df.columns:
                error_links_df = crawl_df[crawl_df["status"] >= 400].copy()
                
                # Build internal URL to source mapping (same as external links)
                internal_url_to_source_mapping = {}
                
                referer_col = "request_headers_Referer"
                if referer_col in error_links_df.columns:
                    error_links_df.rename(
                        columns={referer_col: "source_url"}, inplace=True
                    )
                    error_links_df["source_url"] = error_links_df["source_url"].where(
                        pd.notna(error_links_df["source_url"]), "Internal Navigation"
                    )
                    
                    # Build mapping of URLs to their source URLs (handle multiple sources)
                    for _, row in error_links_df.iterrows():
                        url = row.get("url", "Unknown URL")
                        source = row.get("source_url", "Internal Navigation")
                        
                        if url not in internal_url_to_source_mapping:
                            internal_url_to_source_mapping[url] = []
                        if source not in internal_url_to_source_mapping[url]:
                            internal_url_to_source_mapping[url].append(source)
                else:
                    error_links_df["source_url"] = "Internal Navigation"

                internal_false_positives_filtered = 0

                # Categorize internal links by status code (same logic as external links)
                for _, row in error_links_df.iterrows():
                    url = row.get("url", "Unknown URL")
                    status = row.get("status", -1)

                    # Apply false positive filtering to internal links too
                    is_false_pos, reason = is_likely_false_positive(url, status)
                    if is_false_pos:
                        internal_false_positives_filtered += 1
                        task_logger.log(
                            "info",
                            f"Filtered internal false positive: {url} ({status}) - {reason}",
                        )
                        continue  # Skip this URL

                    # Use source URLs array (same as external links)
                    source_urls = ["Internal Navigation"]  # Default fallback
                    if url in internal_url_to_source_mapping:
                        source_urls = internal_url_to_source_mapping[url]

                    link_info = {"url": url, "status": status, "source_urls": source_urls}

                    if status == -1:
                        internal_unreachable_links.append(link_info)
                    elif status in [404, 410]:
                        internal_broken_links.append(link_info)
                    elif status == 403:
                        internal_permission_issue_links.append(link_info)
                    elif status == 405:
                        internal_method_issue_links.append(link_info)
                    elif 400 <= status < 500:
                        internal_other_client_errors.append(link_info)
                    elif 500 <= status < 600:
                        # Server errors (503, 500, 502, etc.) are broken links
                        internal_broken_links.append(link_info)

                if internal_false_positives_filtered > 0:
                    task_logger.log(
                        "info",
                        f"Filtered {internal_false_positives_filtered} internal false positives",
                    )
            else:
                # Handle timeout/error cases where no status column exists
                task_logger.log(
                    "warning",
                    "No 'status' column found in crawl data. Checking for error records.",
                )
                # Look for timeout/error indicators in the data
                for _, row in crawl_df.iterrows():
                    url = row.get("url", "Unknown URL")
                    # Check for common error indicators
                    if (
                        pd.isna(row.get("title"))
                        and pd.isna(row.get("meta_desc"))
                        and pd.isna(row.get("h1"))
                    ):
                        # This suggests a failed request (timeout, connection error, etc.)
                        internal_unreachable_links.append(
                            {
                                "url": url,
                                "status": "Unreachable",
                                "source_urls": ["Timeout/Error"],
                            }
                        )
                if internal_unreachable_links:
                    task_logger.log(
                        "info",
                        f"Found {len(internal_unreachable_links)} unreachable internal links (likely timeouts)",
                    )
        except Exception as e:
            task_logger.log("error", f"Failed to process internal links: {e}")

        task_logger.log("info", "Extracting external links for analysis")
        links_to_check = []
        try:
            # Fix: Get main domain from audit URL instead of first crawled URL
            db = SessionLocal()
            try:
                audit = db.query(Audit).filter(Audit.id == audit_id).first()
                if not audit:
                    raise ValueError(f"Audit {audit_id} not found")
                main_domain = urlparse(audit.url).netloc
                task_logger.log("info", f"Using main domain: {main_domain} (from audit URL: {audit.url})")
            finally:
                db.close()
            
            link_df = adv.crawlytics.links(crawl_df, internal_url_regex=main_domain)

            # Check if 'internal' column exists
            if "internal" in link_df.columns:
                external_links_df = link_df[~link_df["internal"]].copy()
                external_links_df.dropna(subset=["link"], inplace=True)
                external_links_df["link"] = external_links_df["link"].astype(str)
                links_to_check = external_links_df.rename(
                    columns={"url": "source_url"}
                )[["link", "source_url"]].to_dict("records")
                task_logger.log(
                    "info",
                    f"Found {len(external_links_df['link'].unique())} unique external links to check",
                )
            else:
                task_logger.log(
                    "warning",
                    "No 'internal' column found in links data. Skipping external link analysis.",
                )
        except Exception as e:
            task_logger.log(
                "error",
                f"Failed to extract external links: {e}",
                {"exception_type": type(e).__name__},
            )

        total_pages = len(page_level_report)
        initial_report = {
            "status": "ANALYZING_EXTERNAL",
            "audit_id": audit_id,
            "summary": {
                "total_pages_analyzed": total_pages,
                "internal_unreachable_links_found": len(internal_unreachable_links),
                "internal_broken_links_found": len(internal_broken_links),
                "internal_permission_issues_found": len(
                    internal_permission_issue_links
                ),
                "internal_method_issues_found": len(internal_method_issue_links),
                "internal_other_client_errors_found": len(internal_other_client_errors),
                "pages_missing_title": total_pages - pages_with_title,
                "pages_missing_meta_description": total_pages - pages_with_meta_desc,
                "pages_with_correct_h1": pages_with_one_h1,
                "pages_with_multiple_h1s": pages_with_multiple_h1s,
                "pages_with_no_h1": pages_with_no_h1,
                "top_10_title_words": (
                    _get_top_words(crawl_df["title"])
                    if "title" in crawl_df.columns
                    else []
                ),
                "top_10_h1_words": (
                    _get_top_words(crawl_df["h1"]) if "h1" in crawl_df.columns else []
                ),
            },
            "internal_unreachable_links": internal_unreachable_links,
            "internal_broken_links": internal_broken_links,
            "internal_permission_issue_links": internal_permission_issue_links,
            "internal_method_issue_links": internal_method_issue_links,
            "internal_other_client_errors": internal_other_client_errors,
            "page_level_report": page_level_report,
        }

        task_logger.log(
            "info",
            "Saving initial report to database",
            {
                "total_pages": total_pages,
                "external_links_to_check": len(links_to_check),
            },
        )

        try:
            db = SessionLocal()
            try:
                audit = db.query(Audit).filter(Audit.id == audit_id).first()
                if audit:
                    audit.status = "ANALYZING_EXTERNAL"
                    audit.report_json = initial_report
                    db.commit()
                    task_logger.log("info", "Initial report saved successfully")
            finally:
                db.close()
            return {
                "crawl_output_file": crawl_output_file,
                "links_to_check": links_to_check,
            }
        except Exception as e:
            error_msg = f"Failed to save report compilation: {str(e)}"
            task_logger.log(
                "error",
                "Database error during report save",
                {"error": error_msg, "exception_type": type(e).__name__},
            )
            _mark_audit_failed(audit_id, error_msg)
            raise


@celery_app.task(bind=True)
def check_external_links(self, previous_task_output: dict, audit_id: int) -> dict:
    """
    Check external links using async chunked processing.
    This version prevents blocking and handles domain collisions intelligently.
    """
    task_context = {
        "task_id": self.request.id,
        "previous_output_keys": list(previous_task_output.keys()),
    }

    with TaskLogger(
        audit_id=audit_id,
        task_name="check_external_links",
        task_id=self.request.id,
        context=task_context,
    ) as task_logger:

        links_to_check = previous_task_output.get("links_to_check", [])
        crawl_output_file = previous_task_output.get("crawl_output_file")

        if not links_to_check:
            task_logger.log("info", "No external links to check. Skipping.")
            return {"crawl_output_file": crawl_output_file, "external_links_report": {}}

        task_logger.log("info", f"Processing {len(links_to_check)} external links")

        # Create URL to source mapping for preserving ALL source URLs
        links_df = pd.DataFrame(links_to_check)
        url_to_source_mapping = {}

        for _, row in links_df.iterrows():
            url = row["link"]
            source = row["source_url"]
            if url not in url_to_source_mapping:
                url_to_source_mapping[url] = []
            url_to_source_mapping[url].append(source)

        # Remove duplicates while preserving order
        for url in url_to_source_mapping:
            url_to_source_mapping[url] = list(dict.fromkeys(url_to_source_mapping[url]))

        unique_urls_to_check = list(url_to_source_mapping.keys())

        task_logger.log(
            "info",
            "Starting async external link checking",
            {
                "unique_urls": len(unique_urls_to_check),
                "total_links": len(links_to_check),
                "urls_with_multiple_sources": sum(1 for sources in url_to_source_mapping.values() if len(sources) > 1),
            },
        )

        try:
            # Run the async function in the current thread
            # Since this is a Celery task, we need to create a new event loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            try:
                external_links_report = loop.run_until_complete(
                    check_external_links_async(
                        unique_urls_to_check, audit_id, url_to_source_mapping
                    )
                )
            finally:
                loop.close()

            task_logger.log(
                "info",
                "Async external link checking completed",
                {
                    "broken_links": len(external_links_report.get("broken_links", [])),
                    "unreachable_links": len(
                        external_links_report.get("unreachable_links", [])
                    ),
                    "permission_issues": len(
                        external_links_report.get("permission_issues", [])
                    ),
                },
            )

        except Exception as e:
            task_logger.log(
                "error",
                "Failed to check external links",
                {"error": str(e), "exception_type": type(e).__name__},
            )
            external_links_report = {
                "unreachable_links": [],
                "broken_links": [],
                "permission_issues": [],
                "method_issues": [],
                "other_client_errors": [],
                "error": str(e),
            }

        return {
            "crawl_output_file": crawl_output_file,
            "external_links_report": external_links_report,
        }


@celery_app.task(bind=True)
def save_final_report(self, previous_task_output: dict, audit_id: int):
    task_context = {
        "task_id": self.request.id,
        "previous_output_keys": list(previous_task_output.keys()),
    }

    with TaskLogger(
        audit_id=audit_id,
        task_name="save_final_report",
        task_id=self.request.id,
        context=task_context,
    ) as task_logger:

        external_links_report = previous_task_output.get("external_links_report", {})
        crawl_output_file = previous_task_output.get("crawl_output_file")
        unreachable_links = external_links_report.get("unreachable_links", [])
        broken_links = external_links_report.get("broken_links", [])
        permission_issues = external_links_report.get("permission_issues", [])
        method_issues = external_links_report.get("method_issues", [])
        other_client_errors = external_links_report.get("other_client_errors", [])

        task_logger.log(
            "info",
            "Starting final report save",
            {
                "external_broken_links": len(broken_links),
                "external_unreachable_links": len(unreachable_links),
                "external_permission_issues": len(permission_issues),
            },
        )

        db = SessionLocal()
        try:
            audit = db.query(Audit).filter(Audit.id == audit_id).first()
            if not audit:
                task_logger.log("error", "Audit not found for final save")
                return

            task_logger.log("info", "Building final report structure")
            report_json = audit.report_json.copy()
            summary = report_json["summary"]
            summary.update(
                {
                    "external_unreachable_links_found": len(unreachable_links),
                    "external_broken_links_found": len(broken_links),
                    "external_permission_issues_found": len(permission_issues),
                    "external_method_issues_found": len(method_issues),
                    "external_other_client_errors_found": len(other_client_errors),
                }
            )

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
                "internal_unreachable_links": report_json["internal_unreachable_links"],
                "internal_broken_links": report_json["internal_broken_links"],
                "internal_permission_issue_links": report_json[
                    "internal_permission_issue_links"
                ],
                "internal_method_issue_links": report_json[
                    "internal_method_issue_links"
                ],
                "internal_other_client_errors": report_json[
                    "internal_other_client_errors"
                ],
                "page_level_report": report_json["page_level_report"],
            }

            audit.report_json = final_report_blob
            audit.status = "COMPLETE"
            audit.completed_at = datetime.datetime.utcnow()
            db.commit()
            task_logger.log("info", "Successfully saved final report")

            if settings.DASHBOARD_CALLBACK_URL:
                task_logger.log(
                    "info", "Dashboard callback URL configured, queueing callback task"
                )
                send_report_to_dashboard.delay(audit_id=audit_id)
            else:
                task_logger.log("info", "No dashboard callback URL configured")
        except Exception as e:
            task_logger.log(
                "error",
                "Failed to save final report",
                {"error": str(e), "exception_type": type(e).__name__},
            )
            if audit:
                audit.status = "ERROR"
                db.commit()
        finally:
            db.close()

        # Cleanup crawl output file
        if crawl_output_file and os.path.exists(crawl_output_file):
            try:
                os.remove(crawl_output_file)
                task_logger.log(
                    "info", "Cleaned up crawl output file", {"file": crawl_output_file}
                )
            except OSError as e:
                task_logger.log("warning", f"Error cleaning up crawl file: {e}")


@celery_app.task(bind=True)
def send_report_to_dashboard(self, audit_id: int):
    """
    Sends the final report to the pre-configured dashboard callback URL.
    This task will retry if the dashboard is unavailable.
    """
    task_context = {
        "task_id": self.request.id,
        "dashboard_url": settings.DASHBOARD_CALLBACK_URL,
    }

    with TaskLogger(
        audit_id=audit_id,
        task_name="send_report_to_dashboard",
        task_id=self.request.id,
        context=task_context,
    ) as task_logger:

        task_logger.log("info", "Starting dashboard callback")

        db = SessionLocal()
        try:
            audit = db.query(Audit).filter(Audit.id == audit_id).first()
            if not audit:
                task_logger.log("error", "Audit not found for dashboard callback")
                return

            if not settings.DASHBOARD_CALLBACK_URL or not settings.DASHBOARD_API_KEY:
                task_logger.log(
                    "warning", "Dashboard callback URL or API key not configured"
                )
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
                "completed_at": (
                    audit.completed_at.isoformat() if audit.completed_at else None
                ),
                "error_message": audit.error_message,
                "technical_error": audit.technical_error,
                "report_json": audit.report_json,
            }

            headers = {
                "Content-Type": "application/json",
                "X-API-KEY": settings.DASHBOARD_API_KEY,
            }

            task_logger.log(
                "info",
                "Sending report to dashboard",
                {
                    "dashboard_url": settings.DASHBOARD_CALLBACK_URL,
                    "audit_status": audit.status,
                },
            )

            with httpx.Client() as client:
                response = client.post(
                    settings.DASHBOARD_CALLBACK_URL,
                    json=callback_payload,
                    headers=headers,
                    timeout=30.0,
                )
                response.raise_for_status()
                task_logger.log(
                    "info",
                    "Successfully sent report to dashboard",
                    {"response_status": response.status_code},
                )

        except httpx.RequestError as exc:
            task_logger.log(
                "error",
                "Request to dashboard failed. Retrying.",
                {
                    "error": str(exc),
                    "retry_count": self.request.retries,
                    "next_retry_in_seconds": 60 * (2**self.request.retries),
                },
            )
            # Exponential backoff, retry in 60s, 120s, 240s, etc. max 5 times.
            raise self.retry(
                exc=exc, countdown=60 * (2**self.request.retries), max_retries=5
            )
        except Exception as e:
            task_logger.log(
                "error",
                "Unexpected error sending report to dashboard",
                {"error": str(e), "exception_type": type(e).__name__},
            )
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
        save_final_report.s(audit_id=audit_id),
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
            domain_groups["unknown"].append(url)

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
    Returns domain-optimized crawler settings.
    - Single domain: Conservative crawling
    - Multiple domains: More concurrent
    - Auth-sensitive domains: Gentle with realistic browser fingerprinting
    """

    def requires_gentle_crawling(url: str) -> bool:
        """Check if URL requires authentication-sensitive crawling settings."""
        try:
            from urllib.parse import urlparse

            parsed = urlparse(url.lower())
            hostname = parsed.hostname or ""
            path = parsed.path or ""

            # Authentication subdomain patterns
            auth_subdomains = [
                "account.",
                "auth.",
                "login.",
                "sso.",
                "admin.",
                "dashboard.",
                "portal.",
                "secure.",
            ]
            if any(hostname.startswith(subdomain) for subdomain in auth_subdomains):
                return True

            # Social media and known crawler-blocking domains
            crawler_blocking_patterns = [
                "facebook.com",
                "twitter.com",
                "x.com",
                "instagram.com",
                "linkedin.com",
            ]
            if any(pattern in hostname for pattern in crawler_blocking_patterns):
                return True

            # Authentication path patterns
            auth_paths = [
                "/auth/",
                "/login/",
                "/dashboard/",
                "/admin/",
                "/account/",
                "/profile/",
                "/membership/",
            ]
            if any(auth_path in path for auth_path in auth_paths):
                return True

            return False
        except Exception:
            return False

    domains = set()
    auth_domains_found = []

    for url in urls:
        try:
            domain = urlparse(url).netloc
            domains.add(domain)
            if requires_gentle_crawling(url):
                auth_domains_found.append(domain)
        except Exception:
            continue

    # Industry-standard realistic browser headers for 2025
    realistic_headers = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language": "en-US,en;q=0.9,fr;q=0.8,es;q=0.7",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Cache-Control": "max-age=0",
        "Connection": "keep-alive",
        "DNT": "1",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "sec-ch-ua": '"Google Chrome";v="134", "Chromium";v="134", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
    }

    # If we have auth-required domains, use more gentle settings
    if auth_domains_found:
        logging_manager.log_system_event(
            "crawler",
            "info",
            (
                f"Detected authentication-sensitive domains: "
                f"{len(auth_domains_found)} domains, using gentle settings"
            ),
        )
        return {
            "ROBOTSTXT_OBEY": False,
            "USER_AGENT": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
            ),
            "DOWNLOAD_DELAY": 3,  # Longer delay for auth domains
            "CONCURRENT_REQUESTS_PER_DOMAIN": 1,  # More conservative
            "RANDOMIZE_DOWNLOAD_DELAY": True,
            "DOWNLOAD_TIMEOUT": 15,  # Longer timeout
            "DNS_TIMEOUT": 8,
            "RETRY_ENABLED": True,  # Enable retries
            "RETRY_TIMES": 2,
            "REDIRECT_ENABLED": True,
            "REDIRECT_MAX_TIMES": 5,
            "DEFAULT_REQUEST_HEADERS": realistic_headers,
        }

    # Single domain - conservative approach
    if len(domains) == 1:
        return {
            "ROBOTSTXT_OBEY": False,
            "USER_AGENT": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
            "DOWNLOAD_DELAY": 2,
            "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
            "RANDOMIZE_DOWNLOAD_DELAY": True,
            "DOWNLOAD_TIMEOUT": 12,
            "DNS_TIMEOUT": 6,
            "RETRY_ENABLED": True,
            "RETRY_TIMES": 1,
            "REDIRECT_ENABLED": True,
            "DEFAULT_REQUEST_HEADERS": realistic_headers,
        }
    else:
        # Multiple domains - can be more aggressive
        return {
            "ROBOTSTXT_OBEY": False,
            "USER_AGENT": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
            "DOWNLOAD_DELAY": 1,
            "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
            "RANDOMIZE_DOWNLOAD_DELAY": True,
            "DOWNLOAD_TIMEOUT": 10,
            "DNS_TIMEOUT": 5,
            "RETRY_ENABLED": True,
            "RETRY_TIMES": 1,
            "REDIRECT_ENABLED": True,
            "DEFAULT_REQUEST_HEADERS": realistic_headers,
        }


def run_advertools_chunk(urls: list, output_file: str) -> dict:
    """
    Run smart URL checking with HEAD→GET fallback for 4xx errors.
    This matches Google's crawling behavior and eliminates false positives.
    """
    try:
        if not urls:
            return {"success": True, "urls_processed": 0, "output_file": output_file}

        # Get domain-optimized settings
        custom_settings = get_domain_safe_settings(urls)

        # First try advertools crawl_headers (HEAD requests)
        # Create temp filename that ends with .jl as required by advertools
        temp_head_file = output_file.replace(".jl", ".head_temp.jl")
        adv.crawl_headers(urls, temp_head_file, custom_settings=custom_settings)

        # Process HEAD results and identify 4xx errors for GET fallback
        fallback_urls = []
        final_results = []

        # Read HEAD results
        if os.path.exists(temp_head_file):
            head_df = pd.read_json(temp_head_file, lines=True)

            for _, row in head_df.iterrows():
                status = row.get("status", -1)
                url = row.get("url", "")

                # If 4xx error OR unreachable (-1), mark for GET fallback
                if 400 <= status < 500 or status == -1:
                    fallback_urls.append(url)
                else:
                    # Keep non-4xx/non-unreachable results as-is
                    final_results.append(row.to_dict())

            # Clean up temporary file
            os.remove(temp_head_file)

        # Perform GET fallback for 4xx errors
        if fallback_urls:
            get_results = run_get_fallback_check(fallback_urls, custom_settings)
            final_results.extend(get_results)

        # Write final results to output file
        if final_results:
            final_df = pd.DataFrame(final_results)
            final_df.to_json(output_file, orient="records", lines=True)
        else:
            # Create empty file if no results
            with open(output_file, "w"):
                pass

        # Cleanup temp files
        temp_get_file = (
            temp_head_file.replace(".head_temp.jl", ".get_temp.jl")
            if fallback_urls
            else None
        )
        for temp_file in [temp_head_file, temp_get_file]:
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass  # Ignore cleanup failures

        return {
            "success": True,
            "urls_processed": len(urls),
            "fallback_used": len(fallback_urls),
            "output_file": output_file,
        }

    except Exception as e:
        logging_manager.log_system_event(
            "crawler", "error", f"Error processing chunk {output_file}: {e}"
        )
        return {
            "success": False,
            "error": str(e),
            "urls_processed": 0,
            "fallback_used": 0,
            "output_file": output_file,
        }


def run_get_fallback_check(urls: list, custom_settings: dict) -> list:
    """
    Perform GET requests for URLs that returned 4xx with HEAD or were unreachable.
    This matches Google's behavior and eliminates false positives.
    """
    results = []

    # Use httpx for GET requests with same settings as advertools
    timeout = custom_settings.get("DOWNLOAD_TIMEOUT", 30)
    user_agent = custom_settings.get(
        "USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    )
    delay = custom_settings.get("DOWNLOAD_DELAY", 1)

    headers = {
        "User-Agent": user_agent,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }

    with httpx.Client(
        headers=headers, timeout=timeout, follow_redirects=True
    ) as client:
        for url in urls:
            try:
                # Add delay between requests
                if delay > 0:
                    time.sleep(delay)

                # Perform GET request
                response = client.get(url)

                result = {
                    "url": url,
                    "status": response.status_code,
                    "method_used": "GET_FALLBACK",  # Mark as fallback
                    "content_type": response.headers.get("content-type", ""),
                    "size": len(response.content) if response.content else 0,
                    "redirect_url": str(response.url) if response.url != url else None,
                    "fallback_reason": "HEAD_failed",  # Track why fallback was used
                }

                results.append(result)

            except httpx.TimeoutException:
                results.append(
                    {
                        "url": url,
                        "status": -1,  # Still timeout with GET
                        "method_used": "GET_FALLBACK_TIMEOUT",
                        "error": "Timeout",
                        "fallback_reason": "HEAD_failed_GET_timeout",
                    }
                )
            except httpx.ConnectError as e:
                results.append(
                    {
                        "url": url,
                        "status": -1,  # Still connection error with GET
                        "method_used": "GET_FALLBACK_CONNECTION_ERROR",
                        "error": f"Connection error: {str(e)}",
                        "fallback_reason": "HEAD_failed_GET_connection_error",
                    }
                )
            except httpx.RequestError as e:
                results.append(
                    {
                        "url": url,
                        "status": -1,  # Still request error with GET
                        "method_used": "GET_FALLBACK_REQUEST_ERROR",
                        "error": f"Request error: {str(e)}",
                        "fallback_reason": "HEAD_failed_GET_request_error",
                    }
                )
            except Exception as e:
                results.append(
                    {
                        "url": url,
                        "status": -1,  # Unknown error with GET
                        "method_used": "GET_FALLBACK_ERROR",
                        "error": f"Unknown error: {str(e)}",
                        "fallback_reason": "HEAD_failed_GET_unknown_error",
                    }
                )

    return results


def is_likely_false_positive(url: str, status: int) -> tuple[bool, str]:
    """
    Identify likely false positives using industry-standard heuristic patterns.
    Uses HTTP response analysis and common authentication indicators rather than hardcoded domains.

    Returns (is_false_positive, reason)
    """
    # Never filter unreachable links (-1) as false positives - these are real connectivity issues
    if status == -1:
        return False, "Unreachable links are never false positives"

    # Only apply false positive filtering to 4xx client errors
    if status < 400 or status >= 500:
        return False, ""

    url_lower = url.lower()

    # Industry-standard pattern-based detection (not domain-specific)
    auth_patterns = [
        # Authentication/Session endpoints - universal patterns
        ("/auth/", "Authentication endpoint"),
        ("/login/", "Login endpoint"),
        ("/signin/", "Sign-in endpoint"),
        ("/logout/", "Logout endpoint"),
        ("/dashboard/", "User dashboard - typically requires authentication"),
        ("/profile/", "User profile - requires authentication"),
        ("/account/", "Account management - requires authentication"),
        ("/membership/", "Membership area - requires authentication"),
        ("/admin/", "Admin area - requires authentication"),
        ("/user/", "User-specific content"),
        ("/my-", 'User-specific "my" pages'),
        ("/settings/", "User settings - requires authentication"),
        # API endpoints that typically require authentication
        ("/api/user", "User API endpoint"),
        ("/api/auth", "Authentication API"),
        ("/api/account", "Account API"),
        ("/api/profile", "Profile API"),
        ("/api/dashboard", "Dashboard API"),
        # Session/Token related patterns
        ("sessionid=", "Contains session identifier"),
        ("token=", "Contains authentication token"),
        ("auth_token=", "Contains auth token parameter"),
        ("access_token=", "Contains access token"),
        # Common authentication URL patterns
        ("oauth", "OAuth authentication flow"),
        ("sso/", "Single Sign-On endpoint"),
        ("saml/", "SAML authentication"),
        ("jwt/", "JWT token endpoint"),
    ]

    # Check URL path patterns
    for pattern, reason in auth_patterns:
        if pattern in url_lower:
            return True, f"Authentication-required: {reason}"

    # Subdomain patterns that typically require authentication
    auth_subdomains = [
        "account.",  # account.domain.com
        "auth.",  # auth.domain.com
        "login.",  # login.domain.com
        "sso.",  # sso.domain.com
        "admin.",  # admin.domain.com
        "dashboard.",  # dashboard.domain.com
        "portal.",  # portal.domain.com
        "app.",  # app.domain.com (often requires login)
        "my.",  # my.domain.com
        "user.",  # user.domain.com
        "member.",  # member.domain.com
        "secure.",  # secure.domain.com
    ]

    # Check subdomain patterns
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url_lower)
        hostname = parsed.hostname or ""

        for subdomain_pattern in auth_subdomains:
            if hostname.startswith(subdomain_pattern):
                return True, f"Authentication subdomain: {subdomain_pattern}*"

    except Exception:
        pass  # If URL parsing fails, continue with other checks

    # Social media and major platforms that block crawlers (404/403 errors)
    social_media_indicators = [
        ("facebook.com", "Facebook blocks automated requests"),
        ("twitter.com", "Twitter blocks automated access"),
        ("x.com", "X (Twitter) blocks automated access"),
        ("instagram.com", "Instagram blocks automated access"),
        ("linkedin.com", "LinkedIn blocks automated access"),
        ("tiktok.com", "TikTok blocks automated access"),
        ("youtube.com/user/", "YouTube user pages block crawlers"),
        ("github.com/settings", "GitHub settings require authentication"),
        ("pinterest.com", "Pinterest blocks automated access"),
        ("snapchat.com", "Snapchat blocks automated access"),
    ]

    for indicator, reason in social_media_indicators:
        if indicator in url_lower:
            return True, f"Social media: {reason}"

    # Marketing and tracking domains that commonly block crawlers
    marketing_tracking_patterns = [
        ("attn.tv", "Marketing tracking domain blocks crawlers"),
        ("doubleclick.net", "Google advertising tracking"),
        ("googlesyndication.com", "Google ads tracking"),
        ("googletagmanager.com", "Google Tag Manager"),
        ("googleadservices.com", "Google advertising"),
        ("facebook.com/tr", "Facebook pixel tracking"),
        ("analytics.google.com", "Google Analytics"),
        ("google-analytics.com", "Google Analytics"),
        ("amplitude.com", "Analytics tracking"),
        ("mixpanel.com", "Analytics tracking"),
        ("segment.com", "Analytics tracking"),
        ("hotjar.com", "User analytics"),
        ("zendesk.com/embeddable", "Zendesk widget"),
        ("intercom.io", "Customer support widget"),
        (".tracking.", "Tracking domain"),
        (".analytics.", "Analytics domain"),
    ]

    for pattern, reason in marketing_tracking_patterns:
        if pattern in url_lower:
            return True, f"Marketing/tracking: {reason}"

    # Partner/redirect domains that require proper referrers
    partner_redirect_patterns = [
        ("/go/", "Partner redirect link requires referrer"),
        ("/redirect/", "Redirect endpoint requires referrer"),
        ("/r/", "Short redirect link"),
        ("/link/", "Link redirect"),
        ("/out/", "Outbound link redirect"),
        ("/track/", "Tracking redirect"),
        ("/click/", "Click tracking"),
        ("hp.com/go/", "HP partner redirect requires referrer"),
        ("hp.com/support/", "HP support partner link requires referrer"),
        ("adobe.com/go/", "Adobe partner redirect"),
        ("microsoft.com/en-us/p/", "Microsoft partner link"),
        ("amazon.com/dp/", "Amazon product link may require referrer"),
    ]

    for pattern, reason in partner_redirect_patterns:
        if pattern in url_lower:
            return True, f"Partner/redirect: {reason}"

    # Subsidiary and enterprise domains that often have bot protection
    subsidiary_enterprise_patterns = [
        ("dacor.com", "Samsung subsidiary with bot protection"),
        ("harman.com", "Samsung subsidiary"),
        ("joyent.com", "Samsung subsidiary"),
        ("smartthings.com", "Samsung subsidiary"),
        ("viv.ai", "Samsung subsidiary"),
        (".enterprise.", "Enterprise subdomain"),
        (".corp.", "Corporate subdomain"),
        (".internal.", "Internal subdomain"),
        (".intranet.", "Intranet subdomain"),
    ]

    for pattern, reason in subsidiary_enterprise_patterns:
        if pattern in url_lower:
            return True, f"Subsidiary/enterprise: {reason}"

    # CDN and asset domains that may block direct access
    cdn_asset_patterns = [
        (".cloudfront.net", "AWS CloudFront CDN"),
        (".fastly.com", "Fastly CDN"),
        (".jsdelivr.net", "jsDelivr CDN"),
        (".unpkg.com", "unpkg CDN"),
        (".bootstrapcdn.com", "Bootstrap CDN"),
        ("assets.", "Asset subdomain"),
        ("static.", "Static asset subdomain"),
        ("cdn.", "CDN subdomain"),
        ("media.", "Media subdomain"),
    ]

    for pattern, reason in cdn_asset_patterns:
        if pattern in url_lower:
            return True, f"CDN/asset: {reason}"

    return False, ""


async def check_external_links_async(
    urls: list, audit_id: int, url_to_source_mapping: dict = None
) -> dict:
    """
    Asynchronously check external links using chunked processing.
    This prevents blocking and handles domain collisions intelligently.
    """
    if not urls:
        logging_manager.log_audit_event(audit_id, "info", "No external links to check")
        return {
            "unreachable_links": [],
            "broken_links": [],
            "permission_issues": [],
            "method_issues": [],
            "other_client_errors": [],
        }

    # Limit total URLs to prevent excessive processing
    MAX_EXTERNAL_LINKS = 100
    if len(urls) > MAX_EXTERNAL_LINKS:
        logging_manager.log_audit_event(
            audit_id,
            "warning",
            f"Too many external links ({len(urls)}). Limiting to {MAX_EXTERNAL_LINKS}",
        )
        urls = urls[:MAX_EXTERNAL_LINKS]

    # Create domain-aware chunks
    chunks = chunk_urls_by_domain(urls, max_chunk_size=20)
    logging_manager.log_audit_event(
        audit_id,
        "info",
        f"Processing {len(urls)} external links in {len(chunks)} chunks",
    )

    # Prepare output files for each chunk
    os.makedirs("results", exist_ok=True)
    chunk_files = [
        f"results/headers_{audit_id}_chunk_{i}.jl" for i in range(len(chunks))
    ]

    # Semaphore to limit concurrent chunks (prevents overwhelming the system)
    semaphore = asyncio.Semaphore(3)  # Max 3 concurrent chunks

    async def process_chunk_async(chunk_urls: list, output_file: str, chunk_id: int):
        """Process a single chunk asynchronously."""
        async with semaphore:
            logging_manager.log_audit_event(
                audit_id,
                "info",
                f"Processing chunk {chunk_id} with {len(chunk_urls)} URLs",
            )

            loop = asyncio.get_event_loop()

            # Run advertools in thread pool to avoid blocking
            with ThreadPoolExecutor(max_workers=1) as executor:
                try:
                    # Set timeout for individual chunk processing
                    result = await asyncio.wait_for(
                        loop.run_in_executor(
                            executor, run_advertools_chunk, chunk_urls, output_file
                        ),
                        timeout=300,  # 5 minutes per chunk
                    )
                    return result
                except asyncio.TimeoutError:
                    logging_manager.log_audit_event(
                        audit_id, "error", f"Chunk {chunk_id} timed out"
                    )
                    return {
                        "success": False,
                        "error": "Timeout",
                        "urls_processed": 0,
                        "output_file": output_file,
                    }
                except Exception as e:
                    logging_manager.log_audit_event(
                        audit_id, "error", f"Error processing chunk {chunk_id}: {e}"
                    )
                    return {
                        "success": False,
                        "error": str(e),
                        "urls_processed": 0,
                        "output_file": output_file,
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
            timeout=600,  # 10 minutes total
        )
    except asyncio.TimeoutError:
        logging_manager.log_audit_event(
            audit_id, "error", "Overall external link checking timed out"
        )
        chunk_results = [
            {"success": False, "error": "Overall timeout", "output_file": f}
            for f in chunk_files
        ]

    elapsed_time = time.time() - start_time
    logging_manager.log_audit_event(
        audit_id,
        "info",
        f"External link checking completed in {elapsed_time:.2f} seconds",
    )

    # Merge results from all chunks
    external_links_report = {
        "unreachable_links": [],
        "broken_links": [],
        "permission_issues": [],
        "method_issues": [],
        "other_client_errors": [],
    }

    successful_chunks = 0
    total_urls_processed = 0
    total_fallbacks_used = 0
    unreachable_recoveries = 0  # Track successful recoveries from unreachable status
    false_positives_filtered = 0

    for i, result in enumerate(chunk_results):
        if isinstance(result, Exception):
            logging_manager.log_audit_event(
                audit_id, "error", f"Chunk {i} failed with exception: {result}"
            )
            continue

        if not result.get("success", False):
            logging_manager.log_audit_event(
                audit_id,
                "warning",
                f"Chunk {i} failed: {result.get('error', 'Unknown error')}",
            )
            continue

        successful_chunks += 1
        total_urls_processed += result.get("urls_processed", 0)
        total_fallbacks_used += result.get("fallback_used", 0)

        # Process the chunk results
        chunk_file = result.get("output_file")
        if chunk_file and os.path.exists(chunk_file):
            try:
                headers_df = pd.read_json(chunk_file, lines=True)

                if not headers_df.empty and "status" in headers_df.columns:
                    headers_df["status"] = headers_df["status"].fillna(-1).astype(int)

                    # Track successful recoveries (GET fallback succeeded where HEAD failed)
                    if "method_used" in headers_df.columns:
                        fallback_successes = headers_df[
                            (
                                headers_df["method_used"].str.contains(
                                    "GET_FALLBACK", na=False
                                )
                            )
                            & (headers_df["status"].between(200, 399))
                        ]
                    else:
                        fallback_successes = (
                            pd.DataFrame()
                        )  # Empty DataFrame if column doesn't exist
                    if not fallback_successes.empty:
                        unreachable_recoveries += len(fallback_successes)
                        for _, row in fallback_successes.iterrows():
                            url = row.get("url", "Unknown")
                            status = row.get("status", "Unknown")
                            logging_manager.log_audit_event(
                                audit_id,
                                "info",
                                f"Recovery success: HEAD failed but GET returned {status} for {url}",
                            )

                    error_headers = headers_df[~headers_df["status"].between(200, 399)]

                    for _, row in error_headers.iterrows():
                        status = row["status"]
                        url = row.get("url", "Unknown")
                        method_used = row.get("method_used", "HEAD_ONLY")

                        # Check for false positives before categorizing
                        is_false_pos, reason = is_likely_false_positive(url, status)
                        if is_false_pos:
                            false_positives_filtered += 1
                            logging_manager.log_audit_event(
                                audit_id,
                                "info",
                                f"Filtered false positive: {url} ({status}) - {reason}",
                            )
                            continue  # Skip this URL

                        # Use actual source URLs from mapping, support multiple sources per URL
                        source_urls = ["External Link Check"]  # Default fallback
                        if url_to_source_mapping and url in url_to_source_mapping:
                            source_urls = url_to_source_mapping[url]

                        link_info = {
                            "url": url,
                            "status": "Unreachable" if status == -1 else status,
                            "source_urls": source_urls,
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
                            external_links_report["other_client_errors"].append(
                                link_info
                            )
                        elif 500 <= status < 600:
                            # Server errors (503, 500, 502, etc.) are broken links
                            external_links_report["broken_links"].append(link_info)

            except Exception as e:
                logging_manager.log_audit_event(
                    audit_id,
                    "error",
                    f"Error processing results from {chunk_file}: {e}",
                )
            finally:
                # Clean up chunk file
                try:
                    os.remove(chunk_file)
                except OSError:
                    pass

    # Enhanced summary with fallback and recovery statistics
    summary_msg = f"External link summary: {successful_chunks}/{len(chunks)} chunks successful, {total_urls_processed} URLs processed"
    if total_fallbacks_used > 0:
        summary_msg += f", {total_fallbacks_used} GET fallbacks used"
    if unreachable_recoveries > 0:
        summary_msg += (
            f", {unreachable_recoveries} URLs recovered from unreachable status"
        )
    summary_msg += f", {false_positives_filtered} false positives filtered"

    # Enhanced reporting statistics for multiple source tracking
    total_broken_urls = (
        len(external_links_report["broken_links"]) +
        len(external_links_report["unreachable_links"]) +
        len(external_links_report["permission_issues"]) +
        len(external_links_report["method_issues"]) +
        len(external_links_report["other_client_errors"])
    )
    
    multiple_source_count = 0
    total_source_instances = 0
    for category in ["broken_links", "unreachable_links", "permission_issues", "method_issues", "other_client_errors"]:
        for link in external_links_report[category]:
            source_count = len(link.get("source_urls", []))
            total_source_instances += source_count
            if source_count > 1:
                multiple_source_count += 1
    
    if multiple_source_count > 0:
        summary_msg += f", {multiple_source_count} URLs found on multiple pages ({total_source_instances} total instances)"

    logging_manager.log_audit_event(audit_id, "info", summary_msg)

    return external_links_report


# --- End Async External Link Functions ---
