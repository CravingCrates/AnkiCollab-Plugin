"""Tests for sentry_integration.py — privacy-relevant opt-in/opt-out behavior.

The add-on promises users that error reporting is opt-in and disabled by
default.  That is a user-facing privacy guarantee, so it gets a direct test:
with ``error_reporting_enabled`` unset/false, ``init_sentry()`` must make no
network call and must not invoke ``sentry_sdk.init()`` at all.
"""

from unittest.mock import patch

import pytest
import sentry_sdk

import config_manager
from sentry_integration import (
    _parse_version_tuple,
    init_sentry,
    is_sentry_enabled,
    obsolete_version_of_sentry_sdk,
)


class TestParseVersionTuple:
    def test_parses_full_version(self):
        assert _parse_version_tuple("1.5.5") == (1, 5, 5)
        assert _parse_version_tuple("1.18.0") == (1, 18, 0)

    def test_pads_short_versions(self):
        assert _parse_version_tuple("1.5") == (1, 5, 0)
        assert _parse_version_tuple("2") == (2, 0, 0)

    def test_malformed_returns_zeros(self):
        assert _parse_version_tuple("") == (0, 0, 0)
        assert _parse_version_tuple("not.a.version") == (0, 0, 0)
        # Non-digit segments are skipped: only digits are kept.
        assert _parse_version_tuple("1.alpha.2") == (1, 2, 0)


class TestObsoleteVersion:
    @pytest.mark.parametrize(
        "ver,expected",
        [
            ("1.5.5", False),  # boundary: not obsolete
            ("1.6.0", False),
            ("2.0.0", False),
            ("1.5.4", True),  # boundary: obsolete
            ("1.4.0", True),
            ("0.10.0", True),
        ],
    )
    def test_version_threshold(self, ver, expected):
        with patch.object(sentry_sdk, "__version__", ver, create=True):
            assert obsolete_version_of_sentry_sdk() is expected

    def test_malformed_version_treated_as_obsolete(self):
        """Malformed versions parse as (0,0,0) → obsolete → telemetry stays off.

        This is the fail-closed design: if we cannot verify the SDK version we
        do not risk sending telemetry through an unknown/old SDK.
        """
        with patch.object(sentry_sdk, "__version__", "garbage", create=True):
            assert obsolete_version_of_sentry_sdk() is True


class TestInitSentryPrivacy:
    @pytest.fixture
    def sentry_config(self, mw_mock):
        def _apply(config):
            config_manager.write_config(config)
            return config

        return _apply

    def test_disabled_by_default_no_init(self, mw_mock, sentry_config):
        """No error_reporting_enabled → Sentry must not initialize."""
        sentry_config({"settings": {}})
        with patch.object(sentry_sdk, "init") as mock_init:
            init_sentry()
        mock_init.assert_not_called()

    def test_explicit_false_no_init(self, mw_mock, sentry_config):
        sentry_config({"settings": {"error_reporting_enabled": False}})
        with patch.object(sentry_sdk, "init") as mock_init:
            init_sentry()
        mock_init.assert_not_called()

    def test_enabled_calls_init_with_dsn(self, mw_mock, sentry_config):
        """When the user opts in, Sentry is configured with the addon DSN."""
        from var_defs import SENTRY_DSN

        sentry_config({"settings": {"error_reporting_enabled": True}})
        with patch.object(sentry_sdk, "init") as mock_init:
            init_sentry()
        mock_init.assert_called_once()
        kwargs = mock_init.call_args.kwargs
        assert kwargs["dsn"] == SENTRY_DSN
        # Privacy-preserving flags that must always be set
        assert kwargs.get("send_default_pii") is False
        assert kwargs.get("include_local_variables") is False

    def test_missing_config_no_init(self, mw_mock, sentry_config):
        sentry_config(None)
        with patch.object(sentry_sdk, "init") as mock_init:
            init_sentry()
        mock_init.assert_not_called()


class TestIsSentryEnabled:
    def test_false_when_config_disabled(self, mw_mock):
        config_manager.write_config({"settings": {"error_reporting_enabled": False}})
        assert is_sentry_enabled() is False

    def test_false_when_no_client(self, mw_mock):
        config_manager.write_config({"settings": {"error_reporting_enabled": True}})
        hub = sentry_sdk.Hub
        with patch.object(hub, "current", create=True) as mock_current:
            mock_current.client = None
            assert is_sentry_enabled() is False
