"""Tests for var_defs.py — constants sanity checks."""

from var_defs import (
    API_BASE_URL,
    WEBSITE_BASE_URL,
    VERSION,
    DEFAULT_PROTECTED_TAGS,
    PREFIX_OPTIONAL_TAGS,
    PREFIX_PROTECTED_FIELDS,
    PREFIX_PROTECTED_TAGS,
    SENTRY_DSN,
    SENTRY_ENVIRONMENT,
    SENTRY_SAMPLE_RATE,
    ERROR_REPORTING_DEFAULT_ENABLED,
)


class TestConstants:
    def test_api_base_url_is_http(self):
        assert API_BASE_URL.startswith("http")

    def test_website_base_url_is_http_and_has_no_trailing_slash(self):
        # Pairing builds `/pair?code=...` from this, so a trailing slash would make
        # a double slash in a link the user is asked to open.
        assert WEBSITE_BASE_URL.startswith("http")
        assert not WEBSITE_BASE_URL.endswith("/")

    def test_version_format(self):
        # Version should be a dotted date string like "2026.01.15.1"
        parts = VERSION.split(".")
        assert (
            len(parts) >= 3
        ), f"VERSION should have at least 3 dot-separated parts: {VERSION}"

    def test_default_protected_tags_is_list(self):
        assert isinstance(DEFAULT_PROTECTED_TAGS, list)
        assert len(DEFAULT_PROTECTED_TAGS) > 0

    def test_protected_tags_contains_expected(self):
        assert "leech" in DEFAULT_PROTECTED_TAGS
        assert "marked" in DEFAULT_PROTECTED_TAGS

    def test_prefix_optional_tags(self):
        assert PREFIX_OPTIONAL_TAGS == "AnkiCollab_Optional"

    def test_prefix_protected_fields(self):
        assert PREFIX_PROTECTED_FIELDS == "AnkiCollab_Protect"

    def test_prefix_protected_tags(self):
        assert PREFIX_PROTECTED_TAGS == "AnkiCollab_Personal"

    def test_sentry_dsn_is_string(self):
        assert isinstance(SENTRY_DSN, str)
        assert "bugsink" in SENTRY_DSN

    def test_sentry_sample_rate_range(self):
        assert 0.0 <= SENTRY_SAMPLE_RATE <= 1.0

    def test_error_reporting_default_disabled(self):
        assert ERROR_REPORTING_DEFAULT_ENABLED is False
