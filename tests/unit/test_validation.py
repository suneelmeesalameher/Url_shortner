"""Unit tests for URL/alias validation and the SSRF guard (app.core.validation).
Pure functions, no I/O.
"""
import pytest

from app.core.exceptions import InvalidURLError
from app.core.validation import validate_and_sanitize_url, validate_custom_alias


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://example.com/path?query=1&x=2",
        "https://sub.example.co.uk/a/b/c#fragment",
    ],
)
def test_valid_public_urls_are_accepted_unchanged(url):
    assert validate_and_sanitize_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "not a url",
        "example.com",  # missing scheme
        "ftp://example.com/file",  # unsupported scheme
        "javascript:alert(1)",  # unsupported scheme, classic XSS-via-shortener vector
    ],
)
def test_malformed_or_unsupported_scheme_urls_are_rejected(url):
    with pytest.raises(InvalidURLError):
        validate_and_sanitize_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://127.0.0.1/admin",
        "http://[::1]/",
        "http://0.0.0.0/",
        "http://169.254.169.254/latest/meta-data",  # cloud metadata endpoint
        "http://10.0.0.5/internal",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
    ],
)
def test_ssrf_targets_are_blocked(url):
    """These are exactly the targets that turn a shortener's redirect into an SSRF
    vector against internal infrastructure - see app.core.validation's module
    docstring for the threat model (automated consumers of the redirect, not the
    shortener server itself, are what's being protected here).
    """
    with pytest.raises(InvalidURLError):
        validate_and_sanitize_url(url)


def test_custom_alias_allows_letters_digits_hyphens_and_underscores():
    validate_custom_alias("my-brand_123")  # must not raise


@pytest.mark.parametrize("alias", ["bad alias", "bad/alias", "bad!alias", "bad$alias", "bad.alias"])
def test_custom_alias_rejects_unsafe_characters(alias):
    with pytest.raises(InvalidURLError):
        validate_custom_alias(alias)
