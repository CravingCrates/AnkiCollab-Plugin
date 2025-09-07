from __future__ import annotations

import os
import sys
import platform
from typing import Any, Dict, Tuple
import sentry_sdk
import logging

logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)

# Anki
try:
    import aqt  # type: ignore
    from aqt import mw  # type: ignore
except Exception:  # pragma: no cover - allow import at build tools
    aqt = None
    mw = None


def _get_addon_root() -> str:
    # Absolute path to this add-on's root directory
    return os.path.abspath(os.path.dirname(__file__))


def _normpath(path: str) -> str:
    # Normalize for cross-platform comparisons
    try:
        return os.path.normcase(os.path.abspath(path))
    except Exception:
        return path or ""


SENSITIVE_KEYS = {
    "token",
    "refresh_token",
    "user_hash",
    "authorization",
    "cookie",
    "set-cookie",
    "password",
    "secret",
    "api_key",
    "apikey",
    "key",
    "session",
}


def _scrub(obj: Any) -> Any:
    # Deeply scrub sensitive keys; leave structure intact
    try:
        if isinstance(obj, dict):
            return {
                k: ("[redacted]" if str(k).lower() in SENSITIVE_KEYS else _scrub(v))
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [_scrub(x) for x in obj]
    except Exception:
        pass
    return obj


def _event_has_our_frame(event: Dict[str, Any], addon_root: str) -> bool:
    root = _normpath(addon_root)

    def frames_from_exc() -> Any:
        exc = event.get("exception") or {}
        values = exc.get("values") or []
        for val in values:
            st = (val or {}).get("stacktrace") or {}
            frames = st.get("frames") or []
            for fr in frames:
                yield fr

    def frames_from_threads() -> Any:
        th = event.get("threads") or {}
        values = th.get("values") or []
        for val in values:
            st = (val or {}).get("stacktrace") or {}
            frames = st.get("frames") or []
            for fr in frames:
                yield fr

    for fr in list(frames_from_exc()) + list(frames_from_threads()):
        abs_path = fr.get("abs_path") or fr.get("filename") or ""
        if abs_path:
            if _normpath(abs_path).startswith(root):
                return True
        # Fallback: module heuristic
        module = fr.get("module") or ""
        if module.startswith(__name__.split(".")[0]):
            return True
    return False


def _before_send_factory(addon_root: str):
    def before_send(event: Dict[str, Any], hint: Dict[str, Any] | None = None):
        # Only send if the stack contains frames from our add-on
        try:
            if not _event_has_our_frame(event, addon_root):
                return None
            # Drop list of installed modules to avoid cross-addon leakage
            try:
                event.pop("modules", None)
            except Exception:
                pass
            # Remove any potential PII sources explicitly
            try:
                event.pop("user", None)  # avoid IP-based user attribution
                event.pop("request", None)  # no request metadata
                event.pop("server_name", None)  # avoid host/device name
                # Prune contexts that could include host/device identifiers
                ctx = event.get("contexts") or {}
                if isinstance(ctx, dict):
                    for key in ("device", "runtime", "gpu", "user"):
                        ctx.pop(key, None)
                    event["contexts"] = ctx
            except Exception:
                pass
            return _scrub(event)
        except Exception:
            # Be conservative – if scrubbing/filtering errors, drop the event
            return None

    return before_send


def _before_breadcrumb_factory(addon_root: str):
    root = _normpath(addon_root)

    def before_breadcrumb(crumb: Dict[str, Any], hint: Dict[str, Any] | None = None):
        """Keep only breadcrumbs that originate from this add-on.

        - For logging breadcrumbs, the LoggingIntegration provides LogRecord in hint;
          we match pathname to our add-on root.
        - For other breadcrumb types, we drop by default to avoid cross-addon leakage.
        - Scrub any breadcrumb data we keep.
        """
        try:
            log_record = (hint or {}).get("log_record")
            if log_record is not None:
                path = getattr(log_record, "pathname", "") or ""
                if path and _normpath(path).startswith(root):
                    if isinstance(crumb.get("data"), dict):
                        crumb["data"] = _scrub(crumb.get("data"))
                    return crumb
                return None

            # Very conservative: drop non-logging breadcrumbs
            return None
        except Exception:
            return None

    return before_breadcrumb


def init_sentry() -> None:
    """Initialize Sentry with strong filtering and privacy defaults.

    - Enabled is controlled via add-on config (settings.error_reporting_enabled)
    - Events are only sent if our add-on appears in the callstack.
    - Sensitive data is scrubbed.
    """
    # Read centralized defaults
    try:
        from .var_defs import (
            SENTRY_DSN,
            SENTRY_ENVIRONMENT,
            SENTRY_SAMPLE_RATE,
            ERROR_REPORTING_DEFAULT_ENABLED,
        )
    except Exception:
        SENTRY_DSN = ""  # type: ignore
        SENTRY_ENVIRONMENT = "production"  # type: ignore
        SENTRY_SAMPLE_RATE = 1.0  # type: ignore
        ERROR_REPORTING_DEFAULT_ENABLED = False  # type: ignore

    # Read config
    enabled = bool(ERROR_REPORTING_DEFAULT_ENABLED)
    dsn = SENTRY_DSN
    environment = str(SENTRY_ENVIRONMENT)
    sample_rate = float(SENTRY_SAMPLE_RATE)

    # Smoke test: log vendored SDK version and whether we think it's obsolete
    try:
        ver = getattr(sentry_sdk, "__version__", None) or getattr(sentry_sdk, "VERSION", "unknown")
        LOGGER.info(f"sentry-sdk version: {ver}; obsolete={obsolete_version_of_sentry_sdk()}")
    except Exception:
        LOGGER.info("sentry-sdk version: unknown")

    try:
        cfg = mw.addonManager.getConfig(__name__) or {}
        settings = cfg.get("settings", {})
        enabled = bool(settings.get("error_reporting_enabled", enabled))
    except Exception as e:
        LOGGER.debug(f"Sentry config read error: {e}")
        pass

    # Only proceed if enabled in settings and DSN exists
    if not enabled or not dsn:
        return

    addon_root = _get_addon_root()

    # Release/version tag
    release = None
    try:
        # Prefer explicit version constant if available
        from .var_defs import VERSION  # type: ignore

        release = f"ankicollab@{VERSION}"
    except Exception:
        # Fallback to directory mtime
        try:
            release = f"ankicollab@{int(os.path.getmtime(addon_root))}"
        except Exception:
            release = "ankicollab@unknown"

    traces_sample_rate_map = {
        'development': 1.0,
        'production': 0.05
    }
    
    # Configure Sentry
    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            release=release,
            send_default_pii=False,
            traces_sample_rate=traces_sample_rate_map.get(environment, 0.0),
            sample_rate=sample_rate,
            with_locals=False,
            before_send=_before_send_factory(addon_root),
            before_breadcrumb=_before_breadcrumb_factory(addon_root),
            default_integrations=True,
            shutdown_timeout=0,
        )

        # Set useful tags/context
        try:
            import anki  # type: ignore

            anki_version = getattr(anki, "version", None) or getattr(aqt, "appVersion", "unknown")
        except Exception:
            anki_version = getattr(aqt, "appVersion", "unknown") if aqt else "unknown"

        with sentry_sdk.configure_scope() as scope:  # type: ignore[attr-defined]
            scope.set_tag("addon", "ankicollab")
            scope.set_tag("python", f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
            scope.set_tag("platform", platform.platform())
            scope.set_tag("anki_version", anki_version)
    except Exception as e:
        # Never let Sentry break the add-on
        LOGGER.warning(f"Sentry init skipped: {e}")
        return

def _parse_version_tuple(ver: str) -> tuple[int, int, int]:
    parts = [p for p in ver.split(".") if p.isdigit()]
    while len(parts) < 3:
        parts.append("0")
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except Exception:
        return (0, 0, 0)


def obsolete_version_of_sentry_sdk() -> bool:
    """Disable telemetry for very old sentry-sdk versions to avoid conflicts.

    Mirrors HyperTTS’ precaution but stays minimal.
    """
    try:
        ver = getattr(sentry_sdk, "__version__", None) or getattr(sentry_sdk, "VERSION", "0.0.0")
        return _parse_version_tuple(str(ver)) < (1, 5, 5)
    except Exception:
        return False


def is_sentry_enabled() -> bool:
    """Return True if Sentry is initialized and allowed to send events.

    Checks config flags and the active Sentry client.
    """
    try:
        # Config flag
        cfg = mw.addonManager.getConfig(__name__) or {}
        settings = cfg.get("settings", {})
        if not bool(settings.get("error_reporting_enabled", False)):
            return False

        # SDK presence
        try:
            from sentry_sdk import Hub  # type: ignore
            return Hub.current.client is not None
        except Exception:
            return False
    except Exception as e:
        LOGGER.debug(f"Sentry status check failed: {e}")
        return False

def get_sentry_status() -> Tuple[bool, str]:
    """Return (enabled, reason) for UI diagnostics.

    enabled means client is present and config allows reporting.
    reason explains why disabled.
    """
    try:
        # Config
        cfg = mw.addonManager.getConfig(__name__) or {}
        settings = cfg.get("settings", {})
        if not bool(settings.get("error_reporting_enabled", False)):
            return False, "Opt-out via settings"
        try:
            from sentry_sdk import Hub  # type: ignore
            return (Hub.current.client is not None, "Enabled" if Hub.current.client else "Not initialized")
        except Exception:
            return False, "SDK not available"
    except Exception as e:
        return False, f"Status error: {e}"
