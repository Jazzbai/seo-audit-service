import logging
import re
import socket
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

import dns.exception
import dns.resolver

logger = logging.getLogger(__name__)

# Comprehensive error classification mapping
ERROR_PATTERNS = {
    # DNS and Network Errors
    "nxdomain": {
        "user": "Website not found. Please check the URL and try again.",
        "status": "FAILED",
    },
    "dns resolution failed": {
        "user": "Website not found. Please check the URL and try again.",
        "status": "FAILED",
    },
    "name or service not known": {
        "user": "Website not found. Please check the URL and try again.",
        "status": "FAILED",
    },
    "nodename nor servname provided": {
        "user": "Invalid website address. Please check the URL format.",
        "status": "FAILED",
    },
    "domain validation failed": {
        "user": "Website not found. Please check the URL and try again.",
        "status": "FAILED",
    },
    "does not exist": {
        "user": "Website not found. Please check the URL and try again.",
        "status": "FAILED",
    },
    # Connection Errors
    "connection timeout": {
        "user": "Website is taking too long to respond. Please try again later.",
        "status": "FAILED",
    },
    "connection timed out": {
        "user": "Website is taking too long to respond. Please try again later.",
        "status": "FAILED",
    },
    "connection refused": {
        "user": "Website is currently unavailable. Please try again later.",
        "status": "FAILED",
    },
    "connection reset": {
        "user": "Connection to website was interrupted. Please try again.",
        "status": "FAILED",
    },
    "network is unreachable": {
        "user": "Network connection issue. Please check your internet connection.",
        "status": "FAILED",
    },
    "no route to host": {
        "user": "Website server is unreachable. Please try again later.",
        "status": "FAILED",
    },
    # HTTP Errors
    "http 400": {
        "user": "Invalid request to website. Please check the URL.",
        "status": "FAILED",
    },
    "http 401": {
        "user": "Website requires authentication to access.",
        "status": "FAILED",
    },
    "http 403": {"user": "Access to this website is forbidden.", "status": "FAILED"},
    "http 404": {
        "user": "The webpage was not found. Please check the URL.",
        "status": "FAILED",
    },
    "http 429": {
        "user": "Website is limiting requests. Please try again later.",
        "status": "FAILED",
    },
    "http 500": {
        "user": "Website is experiencing technical difficulties.",
        "status": "PARTIAL",
    },
    "http 502": {
        "user": "Website gateway error. Please try again later.",
        "status": "FAILED",
    },
    "http 503": {
        "user": "Website is temporarily unavailable. Please try again later.",
        "status": "FAILED",
    },
    "http 504": {
        "user": "Website gateway timeout. Please try again later.",
        "status": "FAILED",
    },
    # SSL/TLS Errors
    "ssl": {
        "user": "Website security certificate issues detected.",
        "status": "FAILED",
    },
    "certificate": {
        "user": "Website security certificate issues detected.",
        "status": "FAILED",
    },
    "handshake": {"user": "Secure connection to website failed.", "status": "FAILED"},
    # Redirect Errors
    "redirect": {
        "user": "Website has configuration issues preventing analysis.",
        "status": "FAILED",
    },
    "too many redirects": {
        "user": "Website has too many redirects. Please check the URL.",
        "status": "FAILED",
    },
    # Content/Parsing Errors
    "empty response": {
        "user": "Website returned no content to analyze.",
        "status": "FAILED",
    },
    "no content": {
        "user": "No content could be analyzed on this website.",
        "status": "FAILED",
    },
    "keyerror: 'status'": {
        "user": "Website structure prevented proper analysis.",
        "status": "FAILED",
    },
    "keyerror: 'url'": {
        "user": "Website structure prevented proper analysis.",
        "status": "FAILED",
    },
    "empty dataframe": {
        "user": "No analyzable content found on this website.",
        "status": "FAILED",
    },
    "keyerror: 'title'": {
        "user": "Website structure prevented proper analysis.",
        "status": "FAILED",
    },
    "keyerror: 'internal'": {
        "user": "Website structure prevented proper analysis.",
        "status": "FAILED",
    },
    "crawl validation failed": {
        "user": "Website could not be properly analyzed.",
        "status": "FAILED",
    },
    # Crawl-specific Errors
    "crawl produced no usable results": {
        "user": "No content could be analyzed on this website.",
        "status": "FAILED",
    },
    "spider closed": {"user": "Website analysis was interrupted.", "status": "PARTIAL"},
    "closespider": {
        "user": "Website analysis reached configured limits.",
        "status": "PARTIAL",
    },
    # URL/Format Errors
    "invalid url": {
        "user": "Please enter a valid website URL (e.g., https://example.com).",
        "status": "FAILED",
    },
    "malformed url": {
        "user": "Please enter a valid website URL format.",
        "status": "FAILED",
    },
    "missing scheme": {
        "user": "Please include http:// or https:// in the URL.",
        "status": "FAILED",
    },
    # File/System Errors
    "permission denied": {
        "user": "System permission error occurred during analysis.",
        "status": "FAILED",
    },
    "disk space": {"user": "System storage issue during analysis.", "status": "FAILED"},
    "file not found": {
        "user": "Analysis results could not be processed.",
        "status": "FAILED",
    },
    # Timeout Errors
    "timeout": {
        "user": "Analysis took too long to complete. Please try again.",
        "status": "FAILED",
    },
    "timed out": {
        "user": "Website analysis timed out. Please try again later.",
        "status": "FAILED",
    },
    # Memory/Resource Errors
    "memory": {
        "user": "Website is too large to analyze completely.",
        "status": "PARTIAL",
    },
    "out of memory": {
        "user": "Website is too large to analyze completely.",
        "status": "PARTIAL",
    },
}


def is_valid_url(url: str) -> Tuple[bool, Optional[str]]:
    """
    Validate URL format and basic reachability.

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not url or not isinstance(url, str):
        return False, "URL cannot be empty"

    url = url.strip()

    # Add protocol if missing
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    try:
        parsed = urlparse(url)

        # Check for valid scheme
        if parsed.scheme not in ["http", "https"]:
            return False, "URL must use http:// or https://"

        # Check for valid hostname
        if not parsed.netloc:
            return False, "URL must include a domain name"

        # Check for invalid characters
        if any(char in parsed.netloc for char in [" ", "\t", "\n", "\r"]):
            return False, "URL contains invalid characters"

        # Basic hostname format validation
        hostname = parsed.netloc.split(":")[0]  # Remove port if present
        if not re.match(r"^[a-zA-Z0-9.-]+$", hostname):
            return False, "Invalid domain name format"

        # Check for minimum domain structure
        if "." not in hostname or hostname.startswith(".") or hostname.endswith("."):
            return False, "Invalid domain name format"

        return True, None

    except Exception as e:
        return False, f"Invalid URL format: {str(e)}"


def check_domain_exists(url: str) -> Tuple[bool, Optional[str]]:
    """
    Check if domain exists via DNS lookup with proper timeout handling.

    Returns:
        Tuple of (exists, error_message)
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.netloc.split(":")[0]  # Remove port if present

        # Create resolver with explicit timeout settings
        resolver = dns.resolver.Resolver()
        resolver.timeout = 15  # 15 second timeout for each DNS server
        resolver.lifetime = 30  # 30 second total timeout

        # Try DNS resolution with custom timeout
        try:
            resolver.resolve(hostname, "A")
            return True, None
        except dns.resolver.NXDOMAIN:
            return False, f"Domain '{hostname}' does not exist"
        except dns.resolver.NoAnswer:
            return False, f"Domain '{hostname}' has no IP address"
        except dns.resolver.Timeout:
            logger.warning(f"DNS timeout for {hostname}, trying socket fallback")
            # Fall back to socket-based check
            try:
                socket.gethostbyname(hostname)
                return True, None
            except socket.gaierror:
                return False, f"DNS lookup timeout for '{hostname}'"
        except Exception as dns_e:
            logger.warning(f"DNS lookup failed for {hostname}: {dns_e}")
            # Fall back to socket-based check
            try:
                socket.gethostbyname(hostname)
                return True, None
            except socket.gaierror:
                return False, f"Domain '{hostname}' cannot be resolved"

    except Exception as e:
        return False, f"Domain validation error: {str(e)}"


def classify_error(error_message: str, url: str = None) -> Dict[str, str]:
    """
    Classify error with comprehensive pattern matching and graceful fallback.

    Args:
        error_message: The technical error message
        url: Optional URL for context

    Returns:
        Dictionary with user_message, technical_message, and status
    """
    if not error_message:
        error_message = "Unknown error occurred"

    error_lower = error_message.lower()

    # Check for known error patterns
    for pattern, info in ERROR_PATTERNS.items():
        if pattern in error_lower:
            logger.info(f"Classified error '{pattern}' for URL: {url}")
            return {
                "user_message": info["user"],
                "technical_message": error_message,
                "status": info["status"],
            }

    # Graceful fallback for unknown errors
    logger.warning(f"Unknown error pattern for URL {url}: {error_message}")
    return {
        "user_message": "Something went wrong while analyzing this website. Please try again.",
        "technical_message": error_message,
        "status": "FAILED",
    }


def validate_crawl_output(output_file: str) -> Tuple[bool, Optional[str]]:
    """
    Validate that crawl output file contains usable data.

    Returns:
        Tuple of (is_valid, error_message)
    """
    try:
        import os

        import pandas as pd

        if not os.path.exists(output_file):
            return False, "Crawl output file not found"

        if os.path.getsize(output_file) == 0:
            return False, "Crawl output file is empty"

        try:
            df = pd.read_json(output_file, lines=True)
        except Exception as e:
            return False, f"Crawl output file is corrupted: {str(e)}"

        if df.empty:
            return False, "Crawl produced no results"

        # Check for required columns
        required_columns = ["url"]
        missing_columns = [col for col in required_columns if col not in df.columns]
        if missing_columns:
            return False, f"Crawl output missing required columns: {missing_columns}"

        # Check if we have any valid URLs
        if df["url"].isna().all() or len(df["url"].dropna()) == 0:
            return False, "Crawl produced no valid URLs"

        return True, None

    except Exception as e:
        return False, f"Error validating crawl output: {str(e)}"


def get_user_friendly_url_error(url: str) -> str:
    """Get user-friendly error message for URL validation failures."""
    url_valid, url_error = is_valid_url(url)
    if not url_valid:
        return f"Invalid URL format: {url_error}"

    domain_exists, domain_error = check_domain_exists(url)
    if not domain_exists:
        return f"Website not found: {domain_error}"

    return "URL validation passed"
