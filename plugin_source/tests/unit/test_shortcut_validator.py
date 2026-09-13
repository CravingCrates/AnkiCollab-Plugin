"""Tests for shortcut_validator.py — shortcut collision and modifier rules.

The validator runs on every shortcut the user saves, so a wrongly-accepted
conflict or a wrongly-rejected valid shortcut is a user-visible regression.
Only the pure logic is exercised here: the real QKeySequence is swapped for a
tiny stub, since the tests run outside Anki.
"""

import pytest

import shortcut_validator as sv
from shortcut_validator import Context, Severity


class _FakeSequence:
    """Minimal stand-in for QKeySequence."""

    def __init__(self, text="", empty=False):
        self._text = text
        self._empty = empty

    def isEmpty(self):
        return self._empty

    def toString(self, _format):
        return self._text


class TestModifierCount:
    @pytest.mark.parametrize(
        "combo,count",
        [
            ("Ctrl+Alt+U", 2),
            ("Ctrl+Alt+Shift+U", 3),
            ("Meta+Ctrl+F", 2),
            ("Ctrl+Shift+P", 2),
            ("Ctrl+U", 1),
            ("U", 0),
        ],
    )
    def test_counts_modifiers(self, combo, count):
        assert sv._modifier_count(combo) == count


class TestExtractFirstCombination:
    def test_single_combo_returned_unchanged(self):
        assert (
            sv._extract_first_combination(_FakeSequence("Ctrl+Alt+U")) == "Ctrl+Alt+U"
        )

    def test_multi_combo_returns_first_only(self):
        assert (
            sv._extract_first_combination(_FakeSequence("Ctrl+Alt+U, Ctrl+Alt+I"))
            == "Ctrl+Alt+U"
        )


class TestValidateShortcut:
    def test_empty_shortcut_is_valid(self):
        result = sv.validate_shortcut(_FakeSequence(empty=True), Context.MAIN_WINDOW)
        assert result.is_valid is True
        assert result.severity is None
        assert result.message == ""

    def test_single_modifier_is_blocked(self):
        result = sv.validate_shortcut(_FakeSequence("Ctrl+U"), Context.MAIN_WINDOW)
        assert result.is_valid is False
        assert result.severity is Severity.BLOCKED

    def test_two_modifiers_are_allowed(self):
        result = sv.validate_shortcut(_FakeSequence("Ctrl+Alt+U"), Context.MAIN_WINDOW)
        assert result.is_valid is True
        assert result.severity is None

    @pytest.mark.parametrize("combo", ["Ctrl+Alt+N", "Ctrl+Shift+D"])
    def test_main_window_blocked_shortcuts(self, combo):
        result = sv.validate_shortcut(_FakeSequence(combo), Context.MAIN_WINDOW)
        assert result.is_valid is False
        assert result.severity is Severity.BLOCKED
        assert result.conflicting_action

    @pytest.mark.parametrize("combo", ["Ctrl+Alt+T", "Ctrl+Shift+K"])
    def test_browser_blocked_shortcuts(self, combo):
        result = sv.validate_shortcut(_FakeSequence(combo), Context.BROWSER)
        assert result.is_valid is False
        assert result.severity is Severity.BLOCKED
        assert result.conflicting_action

    def test_warning_severity_reports_conflict(self, monkeypatch):
        monkeypatch.setitem(sv._SYSTEM_WARNING, "Ctrl+Alt+Q", "Test: fake conflict")
        result = sv.validate_shortcut(_FakeSequence("Ctrl+Alt+Q"), Context.MAIN_WINDOW)
        assert result.is_valid is True
        assert result.severity is Severity.WARNING
        assert "fake conflict" in result.message

    def test_multi_combo_validates_first_combo(self):
        # First combo has a single modifier -> blocked, even though the second
        # combo would be valid.
        result = sv.validate_shortcut(
            _FakeSequence("Ctrl+U, Ctrl+Alt+U"), Context.MAIN_WINDOW
        )
        assert result.is_valid is False
        assert result.severity is Severity.BLOCKED
