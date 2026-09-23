from __future__ import annotations

from app.intelligence.audit import audit_page


def test_page_purpose_does_not_treat_head_title_as_visible_page_content():
    result = audit_page(
        "https://example.test/empty",
        """
        <html>
          <head>
            <title>A deliberately long title that describes a page with no body content</title>
          </head>
          <body></body>
        </html>
        """,
        source="source_html",
    )

    codes = {finding["code"] for finding in result["findings"]}
    assert "page_purpose_missing" in codes
    assert result["signals"]["page_purpose"] is None
    assert result["signals"]["word_count"] == 0


def test_confirmation_path_classifies_a_short_thank_you_page_without_thin_content_claim():
    result = audit_page(
        "https://example.test/thank-you",
        """
        <html>
          <head><title>Thank you</title></head>
          <body><p>Done</p></body>
        </html>
        """,
        source="source_html",
    )

    codes = {finding["code"] for finding in result["findings"]}
    assert result["signals"]["page_purpose"] == "confirmation"
    assert "page_purpose_missing" not in codes
    assert "thin_content" not in codes


def test_confirmation_visible_text_classifies_submitted_message():
    result = audit_page(
        "https://example.test/contact/complete",
        """
        <html>
          <head><title>Message sent</title></head>
          <body><main>
            <h1>Thanks for contacting us</h1>
            <p>Your request was submitted successfully.</p>
          </main></body>
        </html>
        """,
        source="source_html",
    )

    codes = {finding["code"] for finding in result["findings"]}
    assert result["signals"]["page_purpose"] == "confirmation"
    assert "page_purpose_missing" not in codes


def test_success_stories_is_not_mistaken_for_a_confirmation_page():
    result = audit_page(
        "https://example.test/success-stories",
        """
        <html>
          <head><title>Success Stories</title></head>
          <body><main><h1>Success Stories</h1>
            <p>Read how customers solved complex problems with our service.</p>
          </main></body>
        </html>
        """,
        source="source_html",
    )

    assert result["signals"]["page_purpose"] == "informational"
    assert "page_purpose_missing" not in {finding["code"] for finding in result["findings"]}
