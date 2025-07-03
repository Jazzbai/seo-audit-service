import json
import sys
import re

from celery.result import AsyncResult
from app.celery_app import celery_app
from app.db.session import get_db
from app.models.audit import Audit


def is_audit_id(identifier: str) -> bool:
    """Check if the identifier looks like an audit ID (integer)"""
    return identifier.isdigit()


def is_task_id(identifier: str) -> bool:
    """Check if the identifier looks like a Celery task ID (UUID format)"""
    uuid_pattern = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    return re.match(uuid_pattern, identifier, re.IGNORECASE) is not None


def view_audit_result(audit_id: int):
    """
    Get audit result from database by audit ID
    """
    print(f"Fetching audit result for audit ID: {audit_id}")
    
    db = next(get_db())
    try:
        audit = db.query(Audit).filter(Audit.id == audit_id).first()
        if audit:
            print(f"\n--- Audit {audit_id} Found ---")
            print(f"Status: {audit.status}")
            print(f"URL: {audit.url}")
            print(f"Created: {audit.created_at}")
            print(f"Completed: {audit.completed_at}")
            print(f"User ID: {audit.user_id}")
            
            if audit.error_message:
                print(f"Error: {audit.error_message}")
            
            if audit.report_json:
                print(f"\n--- Report Summary ---")
                summary = audit.report_json.get('summary', {})
                print(f"Pages analyzed: {summary.get('total_pages_analyzed', 'N/A')}")
                print(f"Internal broken links: {summary.get('internal_broken_links_found', 'N/A')}")
                print(f"External broken links: {summary.get('external_broken_links_found', 'N/A')}")
                print(f"Pages missing title: {summary.get('pages_missing_title', 'N/A')}")
                print(f"Pages missing meta desc: {summary.get('pages_missing_meta_description', 'N/A')}")
                
                # Show broken links if any
                broken_links = audit.report_json.get('internal_broken_links', [])
                if broken_links:
                    print(f"\n--- Internal Broken Links ({len(broken_links)}) ---")
                    for i, link in enumerate(broken_links, 1):
                        print(f"{i}. {link['url']}")
                        # Handle both old single source_url and new source_urls array
                        if 'source_urls' in link:
                            sources = link['source_urls']
                            if len(sources) == 1:
                                print(f"   Source: {sources[0]}")
                            else:
                                print(f"   Sources ({len(sources)}):")
                                for j, source in enumerate(sources, 1):
                                    print(f"     {j}. {source}")
                        else:
                            print(f"   Source: {link.get('source_url', 'Unknown')}")
                
                external_broken = audit.report_json.get('external_broken_links', [])
                if external_broken:
                    print(f"\n--- External Broken Links ({len(external_broken)}) ---")
                    for i, link in enumerate(external_broken, 1):
                        print(f"{i}. {link['url']}")
                        # Handle both old single source_url and new source_urls array
                        if 'source_urls' in link:
                            sources = link['source_urls']
                            if len(sources) == 1:
                                print(f"   Source: {sources[0]}")
                            else:
                                print(f"   Sources ({len(sources)}):")
                                for j, source in enumerate(sources, 1):
                                    print(f"     {j}. {source}")
                        else:
                            print(f"   Source: {link.get('source_url', 'Unknown')}")
                        print(f"   Status: {link['status']}")
                
                # Option to see full report (non-interactive safe)
                import sys
                if sys.stdin.isatty():  # Only prompt if running interactively
                    show_full = input("\nShow full report JSON? (y/n): ").lower().strip()
                    if show_full == 'y':
                        print(f"\n--- Full Report JSON ---")
                        print(json.dumps(audit.report_json, indent=2, default=str))
                else:
                    print(f"\n--- Run with --full flag for complete JSON ---")
            else:
                print("No report data available")
        else:
            print(f"\n--- Audit {audit_id} Not Found ---")
            print("Available audit IDs:")
            recent_audits = db.query(Audit).order_by(Audit.id.desc()).limit(10).all()
            for audit in recent_audits:
                print(f"  {audit.id}: {audit.url} ({audit.status}) - {audit.created_at}")
    finally:
        db.close()


def view_task_result(task_id: str):
    """
    Connects to the Celery result backend and retrieves the
    result for a given task ID, printing it in a readable format.
    """
    print(f"Fetching Celery task result for: {task_id}")

    # Create an AsyncResult object for the given task ID.
    result = AsyncResult(task_id, app=celery_app)

    if result.ready():
        if result.successful():
            print("\n--- Task Succeeded ---")
            task_result = result.get()
            print("Result:")
            print(json.dumps(task_result, indent=4, default=str))
        else:
            print("\n--- Task Failed ---")
            try:
                result.get()
            except Exception as e:
                print(f"Exception: {e!r}")
                print(f"\nTraceback:\n{result.traceback}")
    else:
        print("\n--- Task Not Ready ---")
        print(f"Current state: {result.state}")


def main():
    """
    Main function that determines whether to treat input as audit ID or task ID
    """
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python view_result.py <audit_id>     # View audit by ID (e.g., 53)")
        print("  python view_result.py <task_id>      # View Celery task by UUID")
        print("  python view_result.py list           # List recent audits")
        return
    
    identifier = sys.argv[1]
    
    if identifier.lower() == 'list':
        # List recent audits
        print("Recent audits:")
        db = next(get_db())
        try:
            recent_audits = db.query(Audit).order_by(Audit.id.desc()).limit(20).all()
            for audit in recent_audits:
                status_emoji = "✅" if audit.status == "COMPLETE" else "❌" if audit.status == "FAILED" else "⏳"
                print(f"  {status_emoji} {audit.id}: {audit.url} ({audit.status}) - {audit.created_at}")
        finally:
            db.close()
        return
    
    if is_audit_id(identifier):
        # It's an audit ID
        audit_id = int(identifier)
        view_audit_result(audit_id)
    elif is_task_id(identifier):
        # It's a Celery task ID
        view_task_result(identifier)
    else:
        print(f"Error: '{identifier}' doesn't look like an audit ID or task ID")
        print("Audit IDs should be numbers (e.g., 53)")
        print("Task IDs should be UUIDs (e.g., 9a59b135-4a70-4eea-a078-444134bd66e6)")


if __name__ == "__main__":
    main()
