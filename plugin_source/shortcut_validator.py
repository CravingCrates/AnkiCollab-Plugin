# Shortcut validation now lives in a separate module
# Only 2+ modifier shortcuts are tracked here since single-modifier shortcuts are not validated in any case

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict

from anki.utils import is_mac, is_win
from aqt.qt import QKeySequence


class Severity(Enum):
    # Severity of a shortcut collision.
    BLOCKED = "blocked"  # Cannot be saved — hard error
    WARNING = "warning"  # Can be saved after user confirmation


class Context(Enum):
    # The Anki window context in which the shortcut will be active.
    MAIN_WINDOW = "main"
    BROWSER = "browser"


@dataclass(frozen=True)
class ValidationResult:
    # Result of validation
    is_valid: bool
    severity: Severity | None
    message: str
    conflicting_action: str = ""


# Main-window blocked shortcuts (deck overview + reviewer states)
_ANKI_MAIN_BLOCKED: Dict[str, str] = {
    "Ctrl+Alt+N": "Anki: Forget current card",
    "Ctrl+Alt+E": "Anki: Create copy of current card",
    "Ctrl+Alt+I": "Anki: Show previous card info",
    "Ctrl+Shift+D": "Anki: Set due date",
    "Ctrl+Shift+I": "Anki: Import file",
    "Ctrl+Shift+E": "Anki: Export",
    "Ctrl+Shift+A": "Anki: Manage Add-ons",
    "Ctrl+Shift+N": "Anki: Manage Note Types",
    "Ctrl+Shift+P": "Anki: Switch Profile",
}

# Browser-specific shortcuts
_ANKI_BROWSER_BLOCKED: Dict[str, str] = {
    "Ctrl+Alt+T": "Anki Browser: Toggle cards/notes mode",
    "Ctrl+Alt+F": "Anki Browser: Find duplicates",
    "Ctrl+Shift+K": "Anki Browser: Remove tags",
    "Ctrl+Shift+D": "Anki Browser: Change note type / model",
    "Ctrl+Shift+J": "Anki Browser: Unsuspend card(s)",
    "Ctrl+Shift+R": "Anki Browser: Reset card(s)",
    "Ctrl+Shift+C": "Anki Browser: Copy (card list) / Cloze deletion (editor)",
    "Ctrl+Shift+M": "Anki Browser: Add card type",
    "Ctrl+Shift+N": "Anki Browser: Add field",
    "Ctrl+Shift+F": "Anki Browser: Find and replace",
    "Ctrl+Shift+G": "Anki Browser: Find previous",
    "Ctrl+Shift+1": "Anki Browser: Toggle sidebar",
    "Ctrl+Shift+P": "Anki Browser: Toggle preview pane",
    "Ctrl+Alt+Shift+C": "Anki Browser: Cloze deletion — same number (editor)",
}

# System-level shortcuts (blocked level — user cannot override)
_SYSTEM_BLOCKED: Dict[str, str] = {}

if is_mac:
    _SYSTEM_BLOCKED.update(
        {
            "Meta+Shift+3": "macOS: Screenshot (full screen)",
            "Meta+Shift+4": "macOS: Screenshot (selection)",
            "Meta+Shift+5": "macOS: Screenshot / recording panel",
            "Meta+Ctrl+Q": "macOS: Lock Screen",
        }
    )

if is_win:
    _SYSTEM_BLOCKED.update(
        {
            "Ctrl+Alt+Del": "Windows: Security screen",
            "Ctrl+Shift+Esc": "Windows: Task Manager",
        }
    )

if not is_mac and not is_win:
    # Linux — common desktop environment defaults
    _SYSTEM_BLOCKED.update(
        {
            "Ctrl+Alt+Del": "Linux: Logout / shutdown dialog",
            "Ctrl+Alt+L": "Linux: Lock screen (KDE / GNOME)",
            "Ctrl+Alt+T": "Linux: Open terminal (GNOME / Ubuntu)",
            "Ctrl+Alt+Esc": "Linux: Kill window / System monitor (KDE)",
        }
    )


# System-level shortcuts (warning level — user can override)
_SYSTEM_WARNING: Dict[str, str] = {}

if is_mac:
    _SYSTEM_WARNING.update(
        {
            "Meta+Ctrl+F": "macOS: fill screen",
            "Meta+Alt+Esc": "macOS: Force quit applications",
            "Meta+Alt+H": "macOS: Hide other applications",
            "Meta+Alt+M": "macOS: Minimize all windows of front app",
            "Ctrl+Alt+Left": "macOS: Window management — left half / space left",
            "Ctrl+Alt+Right": "macOS: Window management — right half / space right",
            "Ctrl+Alt+Up": "macOS: Window management — maximize / space up",
            "Ctrl+Alt+Down": "macOS: Window management — minimize / space down",
            "Ctrl+Alt+F": "macOS: Window management — fill screen (^⌥F)",
            "Ctrl+Fn+C": "macOS: Window management — centre (^🌐C)",
            "Ctrl+Alt+R": "macOS: Window management — restore previous size (^⌥R)",
        }
    )

if is_win:
    _SYSTEM_WARNING.update(
        {
            "Meta+Ctrl+D": "Windows: New virtual desktop",
            "Meta+Ctrl+Left": "Windows: Switch virtual desktop left",
            "Meta+Ctrl+Right": "Windows: Switch virtual desktop right",
            "Meta+Ctrl+F4": "Windows: Close virtual desktop",
            "Meta+Shift+M": "Windows: Restore minimized windows",
        }
    )

if not is_mac and not is_win:
    # Linux — common desktop environment / window manager defaults
    _SYSTEM_WARNING.update(
        {
            # GNOME / KDE / Xfce workspace switching
            "Ctrl+Alt+Left": "Linux: Switch workspace left (GNOME/KDE/Xfce)",
            "Ctrl+Alt+Right": "Linux: Switch workspace right (GNOME/KDE/Xfce)",
            "Ctrl+Alt+Up": "Linux: Switch workspace up (GNOME/KDE)",
            "Ctrl+Alt+Down": "Linux: Switch workspace down (GNOME/KDE)",
            "Ctrl+Alt+Shift+Left": "Linux: Move window to left workspace",
            "Ctrl+Alt+Shift+Right": "Linux: Move window to right workspace",
            "Ctrl+Alt+Shift+Up": "Linux: Move window to upper workspace",
            "Ctrl+Alt+Shift+Down": "Linux: Move window to lower workspace",
            # GNOME 3+ workspace shortcuts
            "Meta+Ctrl+Left": "Linux: Switch workspace left (GNOME)",
            "Meta+Ctrl+Right": "Linux: Switch workspace right (GNOME)",
            "Meta+Ctrl+Up": "Linux: Switch workspace up (GNOME)",
            "Meta+Ctrl+Down": "Linux: Switch workspace down (GNOME)",
            # GNOME / Pop!_OS window tiling
            "Meta+Shift+Up": "Linux: Maximize window (GNOME tile)",
            "Meta+Shift+Down": "Linux: Restore / unmaximize window (GNOME tile)",
            "Meta+Shift+Left": "Linux: Tile window left",
            "Meta+Shift+Right": "Linux: Tile window right",
            # i3 / sway / Regolith tiling window managers
            "Meta+Shift+Space": "Linux: Toggle floating window (i3/sway)",
            "Meta+Ctrl+Shift+Left": "Linux: Move window to left workspace (i3/sway)",
            "Meta+Ctrl+Shift+Right": "Linux: Move window to right workspace (i3/sway)",
        }
    )


# ---------------------------------------------------------------------------
# internal helpers


def _extract_first_combination(seq: QKeySequence) -> str:
    """Return the first key combination in portable text format.

    Multi-key sequences like "Ctrl+Alt+U, Ctrl+Alt+I" only have their
    first combination validated (the secondary is a fallback).
    """
    text = seq.toString(QKeySequence.SequenceFormat.PortableText)
    return text.split(",", 1)[0].strip()


def _modifier_count(combo: str) -> int:
    modifiers = {"Ctrl", "Alt", "Shift", "Meta", "Fn"}
    parts = [p.strip() for p in combo.split("+") if p.strip()]
    return sum(1 for p in parts if p in modifiers)


# ---------------------------------------------------------------------------
# main public function


def validate_shortcut(seq: QKeySequence, context: Context) -> ValidationResult:
    # Validate a shortcut with set rules and known (potential) collisions.
    # Empty shortcut is always valid (no shortcut configured)
    if seq.isEmpty():
        return ValidationResult(True, None, "")

    combo = _extract_first_combination(seq)

    # Must have at least 2 modifier keys (Ctrl, Alt, Shift, Meta) to be valid,
    # imo two-button shortcuts are too simply pressed by accident and conflict
    # with built-in shortcuts too often
    if _modifier_count(combo) < 2:
        return ValidationResult(
            False,
            Severity.BLOCKED,
            f"'{combo}' must use at least two modifier keys "
            f"(e.g. Ctrl+Alt+U). Single-modifier shortcuts are too likely "
            f"to conflict with Anki's built-in shortcuts.",
        )

    # Build the effective blacklists for this context
    blocked: Dict[str, str] = dict(_SYSTEM_BLOCKED)
    warning: Dict[str, str] = dict(_SYSTEM_WARNING)

    if context == Context.MAIN_WINDOW:
        blocked.update(_ANKI_MAIN_BLOCKED)
    elif context == Context.BROWSER:
        blocked.update(_ANKI_BROWSER_BLOCKED)

    if combo in blocked:
        return ValidationResult(
            False,
            Severity.BLOCKED,
            f"'{combo}' conflicts with {blocked[combo]}.\n\n"
            f"Please choose a different shortcut.",
            blocked[combo],
        )

    if combo in warning:
        return ValidationResult(
            True,  # technically valid, but needs confirmation
            Severity.WARNING,
            f"'{combo}' may conflict with {warning[combo]}.\n\n"
            f"Do you want to use it anyway?",
            warning[combo],
        )

    return ValidationResult(True, None, "")
