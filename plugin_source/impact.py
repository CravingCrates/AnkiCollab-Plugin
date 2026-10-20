"""
AnkiCollab "My Impact" — the contributor-facing half of the impact & gratitude statistics.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from aqt import mw
from aqt.qt import (
    QAction,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTimer,
    QVBoxLayout,
    QWidget,
    Qt,
)
from aqt.utils import tooltip

from . import config_manager
from .api_client import api_client
from .auth_manager import auth_manager
from .ui.colors import (
    get_button_style,
    get_colors,
    get_dialog_style,
    get_info_box_style,
    get_scrollarea_style,
)
from .utils import get_logger

logger = get_logger("ankicollab.impact")

IMPACT_ENDPOINT = "/GetMyImpactStats"
IMPACT_TIMEOUT_SECONDS = 15

NUDGE_ENABLED_KEY = "impact_nudge_enabled"
NUDGE_TIMESTAMP_KEY = "last_impact_nudge"
NUDGE_MIN_SECONDS = 24 * 60 * 60
NUDGE_PERIOD_MS = 6000
NUDGE_DELAY_MS = 2200
_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

NUMBER_ROWS = (
    ("accepted", "Accepted"),
    ("declined", "Feedback given"),
    ("pending", "Waiting for review"),
    ("submitted", "Submitted in total"),
    ("unrecorded", "Reviewed, outcome not recorded"),
)


# --------------------------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------------------------


def fetch_impact() -> Optional[Dict[str, Any]]:
    """Fetch the caller's impact statistics, or ``None`` when they are unavailable.

    A missing endpoint (404), an expired session, a network failure or unparseable JSON are all
    ordinary outcomes for an add-on that ships ahead of the server, so all of them are logged at
    most and never raised.
    """
    try:
        response = api_client.get(
            IMPACT_ENDPOINT, auth=True, timeout=IMPACT_TIMEOUT_SECONDS
        )
    except (
        Exception
    ) as err:  # noqa: BLE001 - a stats call must never surface an exception
        logger.warning("Impact stats request failed: %s", err)
        return None

    if response.status_code == 404:
        # Server predates this feature. Expected while the rollout is in progress.
        logger.info("Impact stats endpoint is not available on this server")
        return None

    if response.status_code != 200:
        logger.warning("Impact stats returned status %s", response.status_code)
        return None

    try:
        payload = response.json()
    except ValueError:
        logger.warning("Impact stats response was not valid JSON")
        return None

    if not isinstance(payload, dict):
        logger.warning("Impact stats response had an unexpected shape")
        return None

    return payload


def _as_int(value: Any) -> int:
    """Coerce a possibly-missing JSON number to an int, defaulting to 0."""
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def reach_text(stats: Optional[Dict[str, Any]]) -> str:
    """One line about how many learners the caller's accepted changes reach."""
    reach = _as_int(((stats or {}).get("totals") or {}).get("reach"))
    if reach <= 0:
        return "Your accepted suggestions will reach learners as soon as a deck has subscribers."
    return f"Your accepted suggestions reach {reach:,} learners."


def milestone_text(stats: Optional[Dict[str, Any]]) -> str:
    """What the next milestone is, and how far away it is."""
    milestone = (stats or {}).get("milestone") or {}
    current = milestone.get("current") or {}
    next_tier = milestone.get("next") or {}

    if not next_tier:
        name = current.get("name")
        if name:
            return f"You have reached {name} — the highest level there is."
        return "You have reached the highest level there is."

    label = next_tier.get("name") or "the next level"
    remaining = _as_int(milestone.get("remaining_to_next"))
    if _as_int(milestone.get("accepted")) <= 0:
        return f"One accepted suggestion starts you off as {label}."
    if remaining <= 0:
        return f"You have reached {label}."
    if remaining == 1:
        return f"One more accepted suggestion reaches {label}."
    return f"{remaining} more accepted suggestions to reach {label}."


def milestone_fraction(stats: Optional[Dict[str, Any]]) -> float:
    """Progress towards the next milestone as 0.0-1.0, for the progress bar."""
    milestone = (stats or {}).get("milestone") or {}
    value = milestone.get("progress_to_next")
    if value is None:
        return 1.0 if milestone.get("current") and not milestone.get("next") else 0.0
    try:
        fraction = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(max(fraction, 0.0), 1.0)


def acceptance_text(stats: Optional[Dict[str, Any]]) -> Optional[str]:
    """The acceptance rate, or ``None`` when there are too few reviews to be meaningful."""
    totals = (stats or {}).get("totals") or {}
    rate = totals.get("acceptance_rate")
    if rate is None:
        return None
    try:
        return f"{float(rate) * 100:.0f}% of reviewed suggestions were accepted"
    except (TypeError, ValueError):
        return None


def subscription_age_text(days: Optional[int]) -> str:
    """How long a subscription has existed, honestly handling the unknown case."""
    if days is None:
        return "Subscribed before we started tracking"
    if days <= 0:
        return "Subscribed today"
    if days == 1:
        return "Subscribed for 1 day"
    return f"Subscribed for {days} days"


_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def format_accepted_at(value: Optional[str]) -> str:
    """A readable date for an acceptance, falling back to whatever the server sent.

    The endpoint returns RFC 3339 with nanoseconds — correct, and unreadable inside a sentence.
    """
    if not value:
        return ""
    parsed = _parse_timestamp(value)
    if parsed is None:
        return value
    return f"{parsed.day} {_MONTHS[parsed.month - 1]} {parsed.year}"


def deck_detail_text(deck: Dict[str, Any]) -> str:
    """The facts about one subscribed deck, without its name."""
    mine = deck.get("mine") or {}
    community = deck.get("community") or {}
    parts = [
        f"{_as_int(mine.get('accepted'))} accepted",
        f"{_as_int(mine.get('declined'))} feedback received",
        f"{_as_int(community.get('subscribers')):,} subscribers",
    ]
    days = deck.get("subscribed_days")
    parts.append(subscription_age_text(None if days is None else _as_int(days)))
    return ", ".join(parts)


def deck_lines(stats: Optional[Dict[str, Any]]) -> List[str]:
    """One compact line per deck the caller subscribes to."""
    lines: List[str] = []
    for deck in (stats or {}).get("decks") or []:
        if not isinstance(deck, dict):
            continue
        name = deck.get("deck_name") or "Unnamed deck"
        lines.append(f"{name} — {deck_detail_text(deck)}")
    return lines


def nudge_line(stats: Optional[Dict[str, Any]]) -> Optional[str]:
    """The single non-blocking line shown after a sync, or ``None`` when there is nothing to say.

    Only ever shown when the caller has actually reached somebody — an empty or zero-reach account
    gets silence rather than a hollow congratulation.
    """
    reach = _as_int(((stats or {}).get("totals") or {}).get("reach"))
    if reach <= 0:
        return None
    decks = _as_int(((stats or {}).get("totals") or {}).get("decks_contributed_to"))
    across = f" across {decks} decks" if decks > 1 else ""
    return f"Your suggestions now reach {reach:,} learners{across}."


def should_show_nudge(enabled: bool, now: datetime, last_shown: Optional[str]) -> bool:
    """Whether the post-sync nudge is due: enabled, and not already shown in the last day."""
    if not enabled:
        return False
    if not last_shown:
        return True

    previous = _parse_timestamp(last_shown)
    if previous is None:
        # An unreadable timestamp must not silence the feature forever.
        return True

    return (now - previous).total_seconds() >= NUDGE_MIN_SECONDS


def _parse_timestamp(value: str) -> Optional[datetime]:
    """Parse a stored timestamp, tolerating both our own format and ISO strings."""
    for parser in (
        lambda: datetime.strptime(value, _TIMESTAMP_FORMAT),
        lambda: datetime.fromisoformat(value),
    ):
        try:
            parsed = parser()
        except (TypeError, ValueError):
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


# --------------------------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------------------------


class ImpactDialog(QDialog):
    """A small, read-only summary of the caller's own contribution statistics."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        colors = get_colors()
        self.setWindowTitle("My Impact")
        self.setMinimumWidth(460)
        self.setModal(True)
        self.setStyleSheet(get_dialog_style())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)

        # The one number worth reading first, in the largest body type on the dialog.
        self._headline = QLabel("Loading your impact…")
        self._headline.setWordWrap(True)
        self._headline.setStyleSheet(
            f"font-size: 15px; color: {colors['text_primary']};"
        )
        layout.addWidget(self._headline)

        # Progress towards the next milestone: a quiet 6px bar and one explanation.
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(6)
        self._progress.setAccessibleName("Progress towards the next milestone")
        self._progress.setStyleSheet(
            f"QProgressBar {{ background-color: {colors['border']}; border: none;"
            f" border-radius: 3px; }}"
            f"QProgressBar::chunk {{ background-color: {colors['primary']};"
            f" border-radius: 3px; }}"
        )
        layout.addWidget(self._progress)

        self._milestone = QLabel("")
        self._milestone.setWordWrap(True)
        self._milestone.setStyleSheet(
            f"font-size: 12px; color: {colors['text_secondary']};"
        )
        layout.addWidget(self._milestone)

        self._separators = [self._add_separator(layout)]

        # Label/value pairs. The labels stay quiet and the numbers align right, so they can be
        # compared down the column instead of parsed out of a sentence.
        self._grid = QGridLayout()
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(16)
        self._grid.setVerticalSpacing(6)
        self._rows: Dict[str, Any] = {}
        for row, (key, label) in enumerate(NUMBER_ROWS):
            name = QLabel(label)
            name.setStyleSheet(f"font-size: 12px; color: {colors['text_secondary']};")
            value = QLabel("—")
            value.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            value.setStyleSheet(
                f"font-size: 13px; font-weight: 600; color: {colors['text_primary']};"
            )
            self._grid.addWidget(name, row, 0)
            self._grid.addWidget(value, row, 1)
            self._rows[key] = (name, value)
        self._grid.setColumnStretch(1, 1)
        layout.addLayout(self._grid)

        self._rate = QLabel("")
        self._rate.setWordWrap(True)
        self._rate.setStyleSheet(f"font-size: 12px; color: {colors['text_secondary']};")
        layout.addWidget(self._rate)

        self._separators.append(self._add_separator(layout))

        self._latest = QLabel("")
        self._latest.setWordWrap(True)
        self._latest.setStyleSheet(
            f"font-size: 12px; color: {colors['text_secondary']};"
        )
        layout.addWidget(self._latest)

        self._decks_heading = QLabel("Decks you subscribe to")
        self._decks_heading.setStyleSheet(
            f"font-size: 12px; font-weight: 600; color: {colors['text_primary']};"
        )
        layout.addWidget(self._decks_heading)

        # Bounded on purpose: one subscriber can have many decks, and a dialog that grows past the
        # bottom of the screen is worse than a short list that scrolls. Same pattern as the
        # field-picker dialog.
        self._decks_container = QWidget()
        self._decks = QVBoxLayout(self._decks_container)
        self._decks.setContentsMargins(8, 8, 8, 8)
        self._decks.setSpacing(10)
        self._decks_scroll = QScrollArea()
        self._decks_scroll.setWidgetResizable(True)
        self._decks_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._decks_scroll.setMaximumHeight(260)
        self._decks_scroll.setStyleSheet(get_scrollarea_style())
        self._decks_scroll.setWidget(self._decks_container)
        layout.addWidget(self._decks_scroll)

        self._info = QLabel("")
        self._info.setWordWrap(True)
        self._info.setVisible(False)
        layout.addWidget(self._info)

        # Spare height belongs at the bottom. Without this, Qt hands it to a word-wrapped label and
        # opens a gap in the middle of the numbers.
        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._close_button = QPushButton("Close")
        self._close_button.setStyleSheet(get_button_style("primary"))
        self._close_button.setDefault(True)
        self._close_button.clicked.connect(self.accept)
        buttons.addWidget(self._close_button)
        layout.addLayout(buttons)

    @staticmethod
    def _add_separator(layout: QVBoxLayout) -> QFrame:
        """A hairline divider. `QFrame.Shape.HLine` is the only spelling that works on Qt5 and Qt6."""
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setStyleSheet(f"color: {get_colors()['border']};")
        layout.addWidget(separator)
        return separator

    def _data_widgets(self) -> List[Any]:
        """Everything that only makes sense once statistics have arrived."""
        widgets: List[Any] = [self._progress, self._milestone, self._rate, self._latest]
        widgets.extend(self._separators)
        widgets.append(self._decks_heading)
        widgets.append(self._decks_scroll)
        for name, value in self._rows.values():
            widgets.extend((name, value))
        return widgets

    def _clear_decks(self) -> None:
        while self._decks.count():
            item = self._decks.takeAt(0)
            child_layout = item.layout()
            if child_layout is not None:
                self._clear_layout(child_layout)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    @staticmethod
    def _clear_layout(layout: QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    @staticmethod
    def _muted(text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(
            f"font-size: 12px; color: {get_colors()['text_secondary']};"
        )
        return label

    def _add_deck(self, deck: Dict[str, Any]) -> None:
        block = QVBoxLayout()
        block.setContentsMargins(0, 0, 0, 0)
        block.setSpacing(2)
        name = QLabel(deck.get("deck_name") or "Unnamed deck")
        name.setWordWrap(True)
        name.setStyleSheet(
            f"font-size: 13px; font-weight: 600;"
            f" color: {get_colors()['text_primary']};"
        )
        block.addWidget(name)
        block.addWidget(self._muted(deck_detail_text(deck)))
        self._decks.addLayout(block)

    def show_stats(self, stats: Optional[Dict[str, Any]]) -> None:
        """Render fetched statistics, or the friendly unavailable state."""
        if stats is None:
            self._show_unavailable()
            return

        self._set_loaded(True)

        totals = stats.get("totals") or {}
        self._headline.setText(reach_text(stats))
        self._progress.setValue(int(round(milestone_fraction(stats) * 100)))
        self._milestone.setText(milestone_text(stats))

        for key, _label in NUMBER_ROWS:
            self._rows[key][1].setText(f"{_as_int(totals.get(key)):,}")

        # Shown only when non-zero: an empty "outcome not recorded" row raises a question whose
        # answer ("nothing was missed") does not deserve the space. Applied after _set_loaded, which
        # makes every row visible.
        self._set_row_visible("unrecorded", _as_int(totals.get("unrecorded")) > 0)

        rate = acceptance_text(stats)
        self._rate.setText(
            rate
            or "No acceptance rate yet — it appears once five suggestions have been reviewed."
        )

        latest = stats.get("latest_accepted")
        if latest:
            maintainer = latest.get("accepted_by")
            where = latest.get("deck_name") or "a deck"
            when = format_accepted_at(latest.get("accepted_at"))
            who = f" by {maintainer}" if maintainer else ""
            on = f" on {when}" if when else ""
            self._latest.setText(f"Latest acceptance: {where}{who}{on}")
        else:
            self._latest.setText(
                "No accepted suggestions yet — your first one will show up here."
            )

        self._clear_decks()
        decks = [deck for deck in (stats.get("decks") or []) if isinstance(deck, dict)]
        if decks:
            for deck in decks:
                self._add_deck(deck)
        else:
            self._decks.addWidget(
                self._muted("You are not subscribed to any decks yet.")
            )

    def _set_loaded(self, loaded: bool) -> None:
        """Swap between the data view and the single-line unavailable view."""
        if not loaded:
            self._clear_decks()
        for widget in self._data_widgets():
            widget.setVisible(loaded)
        self._decks_heading.setVisible(loaded)
        self._info.setVisible(not loaded)

    def _set_row_visible(self, key: str, visible: bool) -> None:
        for widget in self._rows[key]:
            widget.setVisible(visible)

    def _show_unavailable(self) -> None:
        self._headline.setText("Your impact statistics aren't available right now.")
        self._info.setText(
            "The server could not be reached. Your sync is unaffected, and these numbers will be "
            "here next time."
        )
        self._info.setStyleSheet(get_info_box_style())
        self._set_loaded(False)


def _on_fetch_done(stats: Optional[Dict[str, Any]]) -> None:
    """QueryOp success callback: runs on the main thread."""
    dialog = ImpactDialog(parent=mw)
    dialog.show_stats(stats)
    dialog.exec()


def open_impact_dialog() -> None:
    """Entry point for the menu action: fetch in the background, then show the dialog."""
    from aqt.operations import QueryOp
    from anki.utils import point_version

    op = QueryOp(parent=mw, op=lambda _col: fetch_impact(), success=_on_fetch_done)
    if point_version() >= 231000:
        op = op.without_collection()
    op.run_in_background()


def _show_nudge_line(line: str) -> None:
    """Show the nudge tooltip. Runs from a timer, so it guards itself as well."""
    try:
        tooltip(line, parent=mw, period=NUDGE_PERIOD_MS)
    except Exception as err:  # noqa: BLE001 - a nudge must never break a finished sync
        logger.warning("Could not show the impact nudge: %s", err)


def _fetch_in_background(on_done) -> None:
    """Fetch impact statistics without blocking the sync completion callback."""
    from anki.utils import point_version
    from aqt.operations import QueryOp

    op = QueryOp(parent=mw, op=lambda _col: fetch_impact(), success=on_done)
    if point_version() >= 231000:
        op = op.without_collection()
    op.run_in_background()


def maybe_show_nudge() -> None:
    """Show at most one impact line per day, after a successful sync.

    Called from the sync completion path. It never raises: a failure here must not disturb a sync
    that has already finished successfully.
    """
    try:
        settings = config_manager.get_config().get("settings", {})
        if not auth_manager.is_logged_in():
            return

        now = datetime.now(timezone.utc)
        if not should_show_nudge(
            bool(settings.get(NUDGE_ENABLED_KEY, True)),
            now,
            settings.get(NUDGE_TIMESTAMP_KEY),
        ):
            return

        # Record before showing: if anything below fails, the user is not shown it twice.
        # `set_value` writes the *stored* settings, not the merged view, so reading the config
        # never bakes shipped defaults into the user's file.
        stored_settings = config_manager.get_stored_config().get("settings", {})
        if not isinstance(stored_settings, dict):
            stored_settings = {}
        config_manager.set_value(
            "settings",
            {**stored_settings, NUDGE_TIMESTAMP_KEY: now.strftime(_TIMESTAMP_FORMAT)},
        )

        def on_done(stats: Optional[Dict[str, Any]]) -> None:
            line = nudge_line(stats)
            if line is None:
                return
            # Deferred so this does not overwrite the sync's own "All synced." tooltip.
            QTimer.singleShot(NUDGE_DELAY_MS, lambda: _show_nudge_line(line))

        _fetch_in_background(on_done)
    except Exception as err:  # noqa: BLE001 - a nudge must never break a finished sync
        logger.warning("Could not show the impact nudge: %s", err)


class _ImpactMenu:
    """Owns this module's menu action, following the notifications center pattern."""

    def __init__(self) -> None:
        self._action: Optional[QAction] = None
        self._attached = False

    def attach(self, parent_menu) -> None:
        if self._attached:
            return

        self._action = QAction("My Impact", mw)
        self._action.setToolTip("See how many learners your suggestions have reached")
        self._action.triggered.connect(open_impact_dialog)

        if hasattr(parent_menu, "addAction"):
            parent_menu.addAction(self._action)

        self._attached = True

    def set_visible(self, visible: bool) -> None:
        if self._action is not None:
            self._action.setVisible(visible)


_menu = _ImpactMenu()


def init_impact_menu(parent_menu) -> None:
    """Attach the "My Impact" action to an existing menu (called from ``menu.menu_init``)."""
    _menu.attach(parent_menu)


def set_impact_visibility(visible: bool) -> None:
    """Show or hide the action, mirroring the rest of the menu's login state."""
    _menu.set_visible(visible)
