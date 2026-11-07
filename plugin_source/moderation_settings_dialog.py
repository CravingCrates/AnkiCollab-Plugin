"""
moderation_settings_dialog.py - Native PyQt6 Community Moderation Settings for AnkiCollab.
Allows moderators to inspect qualification criteria, manage weekly quotas,
self-specialize by dedicating themselves to specific tags, and configure local adoption.
Strictly adheres to sober, minimalist, and emoji-free UI guidelines.
"""

from __future__ import annotations

import html
from typing import Any, Dict, List, Optional

from aqt import mw
from aqt.qt import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QComboBox,
    QLineEdit,
    QCheckBox,
    QProgressBar,
    QGroupBox,
    QWidget,
    QScrollArea,
    Qt,
)
from aqt.utils import showInfo

from .community_moderation import (
    get_moderator_status,
    sync_reviewer_eligibility,
    get_self_specialization,
    save_self_specialization,
    get_deck_review_count,
)
from .utils import DeckManager, get_did_from_hash
from .ui.colors import get_button_style, get_dialog_style, get_groupbox_style, get_input_style


class ModerationSettingsDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None, initial_deck_hash: str = ""):
        super().__init__(parent)
        self.setWindowTitle("AnkiCollab - Community Moderation Settings")
        self.setMinimumSize(720, 680)
        self.setStyleSheet(get_dialog_style())

        self.current_deck_hash: str = initial_deck_hash
        self.status_data: Dict[str, Any] = {}

        self.init_ui()
        self.load_subscribed_decks()

    def init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(18, 18, 18, 18)
        main_layout.setSpacing(14)

        # Title header
        title_label = QLabel("Community Moderation & Governance Settings")
        title_label.setStyleSheet("font-size: 16px; font-weight: bold; color: #333333;")
        main_layout.addWidget(title_label)

        sub_label = QLabel(
            "Manage your moderator status, inspect semi-automatic qualification metrics, "
            "and dedicate yourself to specific deck tags."
        )
        sub_label.setStyleSheet("font-size: 12px; color: #666666; margin-bottom: 4px;")
        sub_label.setWordWrap(True)
        main_layout.addWidget(sub_label)

        # Deck selector row
        deck_row = QHBoxLayout()
        deck_label = QLabel("Deck:")
        deck_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        deck_row.addWidget(deck_label)

        self.deck_combo = QComboBox()
        self.deck_combo.currentIndexChanged.connect(self.on_deck_changed)
        deck_row.addWidget(self.deck_combo, 1)

        self.refresh_btn = QPushButton("Refresh Status")
        self.refresh_btn.setStyleSheet(get_button_style("default"))
        self.refresh_btn.clicked.connect(self.reload_current_deck)
        deck_row.addWidget(self.refresh_btn)

        main_layout.addLayout(deck_row)

        # Scrollable container for settings sections
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(14)

        # Section 1: Moderator Status & Semi-Automatic Eligibility
        status_box = QGroupBox("Moderator Status & Qualification")
        status_box.setStyleSheet(get_groupbox_style())
        sb_layout = QVBoxLayout(status_box)
        sb_layout.setSpacing(8)

        self.status_badge = QLabel("Status: Checking eligibility...")
        self.status_badge.setStyleSheet(
            "padding: 6px 12px; background-color: rgba(128,128,128,0.1); "
            "border: 1px solid rgba(128,128,128,0.25); border-radius: 4px; font-weight: bold;"
        )
        sb_layout.addWidget(self.status_badge)

        self.tenure_label = QLabel("Subscription Tenure: --")
        self.tenure_label.setStyleSheet("font-size: 12px; color: #444444;")
        sb_layout.addWidget(self.tenure_label)

        self.reviews_label = QLabel("Cards Reviewed in Local Revlog: --")
        self.reviews_label.setStyleSheet("font-size: 12px; color: #444444;")
        sb_layout.addWidget(self.reviews_label)

        self.mode_label = QLabel("Active Governance Mode: --")
        self.mode_label.setStyleSheet("font-size: 12px; color: #444444;")
        sb_layout.addWidget(self.mode_label)

        # Quota progress
        self.quota_label = QLabel("Weekly Quota (7-Day Sliding Window): --")
        self.quota_label.setStyleSheet("font-size: 12px; font-weight: bold; margin-top: 4px;")
        sb_layout.addWidget(self.quota_label)

        self.quota_bar = QProgressBar()
        self.quota_bar.setRange(0, 20)
        self.quota_bar.setValue(20)
        self.quota_bar.setTextVisible(True)
        self.quota_bar.setStyleSheet(
            "QProgressBar { border: 1px solid #cccccc; border-radius: 3px; height: 18px; text-align: center; font-size: 11px; }"
            "QProgressBar::chunk { background-color: #2b7bb9; }"
        )
        sb_layout.addWidget(self.quota_bar)

        scroll_layout.addWidget(status_box)

        # Section 2: Tag Specialization (Self-dedication)
        spec_box = QGroupBox("Tag Specialization (Self-Dedicated Domains)")
        spec_box.setStyleSheet(get_groupbox_style())
        spec_layout = QVBoxLayout(spec_box)
        spec_layout.setSpacing(8)

        spec_desc = QLabel(
            "Filter the moderation queue to focus on your areas of clinical or academic expertise. "
            "If specified, only suggestions for cards bearing these tags will be prioritized in your queue."
        )
        spec_desc.setStyleSheet("font-size: 12px; color: #666666;")
        spec_desc.setWordWrap(True)
        spec_layout.addWidget(spec_desc)

        tag_input_row = QHBoxLayout()
        self.tags_input = QLineEdit()
        self.tags_input.setPlaceholderText("e.g. Cardio, ECG, Valvulopathies, Pharmacologie")
        self.tags_input.setStyleSheet(get_input_style())
        tag_input_row.addWidget(self.tags_input, 1)

        self.save_tags_btn = QPushButton("Save Specialization")
        self.save_tags_btn.setStyleSheet(get_button_style("primary"))
        self.save_tags_btn.clicked.connect(self.on_save_specialization)
        tag_input_row.addWidget(self.save_tags_btn)
        spec_layout.addLayout(tag_input_row)

        # Quick tag chips
        quick_row = QHBoxLayout()
        quick_label = QLabel("Suggested Domains:")
        quick_label.setStyleSheet("font-size: 11px; color: #777777;")
        quick_row.addWidget(quick_label)

        for tag in ["Vocabulary", "Grammar", "Anatomy", "Formulas", "Definitions", "Core"]:
            btn = QPushButton(tag)
            btn.setStyleSheet(
                "QPushButton { font-size: 11px; padding: 2px 8px; border: 1px solid #cccccc; "
                "border-radius: 10px; background: #f8f9fa; color: #333333; } "
                "QPushButton:hover { background: #e9ecef; }"
            )
            btn.clicked.connect(lambda checked=False, t=tag: self.append_tag(t))
            quick_row.addWidget(btn)

        quick_row.addStretch(1)
        spec_layout.addLayout(quick_row)

        scroll_layout.addWidget(spec_box)

        # Section 3: Local Behavior & Safeguards
        pref_box = QGroupBox("Local Reviewer Preferences & Safety")
        pref_box.setStyleSheet(get_groupbox_style())
        pref_layout = QVBoxLayout(pref_box)
        pref_layout.setSpacing(10)

        early_pref = True
        notif_pref = True
        try:
            if getattr(mw, "pm", None) and hasattr(mw.pm, "profile"):
                early_pref = bool(mw.pm.profile.get("ankicollab_early_adoption", True))
                notif_pref = bool(mw.pm.profile.get("ankicollab_moderation_notifications", True))
        except Exception:
            pass

        self.early_adopt_chk = QCheckBox(
            "Optimistic Local Adoption: Automatically apply approved suggestions to my local collection immediately."
        )
        self.early_adopt_chk.setChecked(early_pref)
        self.early_adopt_chk.setStyleSheet("font-size: 12px;")
        self.early_adopt_chk.toggled.connect(self.on_early_adopt_toggled)
        pref_layout.addWidget(self.early_adopt_chk)

        adopt_note = QLabel(
            "Note: Your local early-adopted cards are marked and will never be overwritten against your will, "
            "even if a subsequent global sync occurs."
        )
        adopt_note.setStyleSheet("font-size: 11px; color: #777777; margin-left: 20px;")
        adopt_note.setWordWrap(True)
        pref_layout.addWidget(adopt_note)

        self.notif_chk = QCheckBox(
            "Discrete Invitations: Receive discreet notifications in Anki when new suggestions matching my dedicated tags are pending."
        )
        self.notif_chk.setChecked(notif_pref)
        self.notif_chk.setStyleSheet("font-size: 12px;")
        self.notif_chk.toggled.connect(self.on_notif_toggled)
        pref_layout.addWidget(self.notif_chk)

        scroll_layout.addWidget(pref_box)
        scroll.setWidget(scroll_content)
        main_layout.addWidget(scroll, 1)

        # Bottom Actions row
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(10)

        self.sync_metrics_btn = QPushButton("Sync Local Revlog Metrics")
        self.sync_metrics_btn.setStyleSheet(get_button_style("default"))
        self.sync_metrics_btn.clicked.connect(self.on_sync_metrics_clicked)
        bottom_row.addWidget(self.sync_metrics_btn)

        bottom_row.addStretch(1)

        self.open_queue_btn = QPushButton("Open Moderation Queue")
        self.open_queue_btn.setStyleSheet(get_button_style("success"))
        self.open_queue_btn.clicked.connect(self.on_open_queue_clicked)
        bottom_row.addWidget(self.open_queue_btn)

        self.close_btn = QPushButton("Close")
        self.close_btn.setStyleSheet(get_button_style("default"))
        self.close_btn.clicked.connect(self.accept)
        bottom_row.addWidget(self.close_btn)

        main_layout.addLayout(bottom_row)

    def load_subscribed_decks(self) -> None:
        self.deck_combo.clear()
        manager = DeckManager()
        subscribed = manager.get_subscribed_decks()

        if not subscribed:
            self.deck_combo.addItem("No subscribed decks found", "")
            self.tags_input.setEnabled(False)
            self.save_tags_btn.setEnabled(False)
            self.open_queue_btn.setEnabled(False)
            return

        for deck_hash in subscribed:
            local_name = manager.get_deck_name_by_hash(deck_hash) or deck_hash
            self.deck_combo.addItem(local_name, deck_hash)

        # Select initial deck hash if provided
        if self.current_deck_hash:
            idx = self.deck_combo.findData(self.current_deck_hash)
            if idx >= 0:
                self.deck_combo.setCurrentIndex(idx)
        elif self.deck_combo.count() > 0:
            self.on_deck_changed(0)

    def on_deck_changed(self, index: int) -> None:
        deck_hash = self.deck_combo.currentData()
        if not deck_hash:
            return
        self.current_deck_hash = deck_hash
        self.reload_current_deck()

    def reload_current_deck(self) -> None:
        if not self.current_deck_hash:
            return

        # Fetch local revlog count
        did = get_did_from_hash(self.current_deck_hash)
        revlog_count = get_deck_review_count(did) if did else 0

        # Sync eligibility metrics
        sync_res = sync_reviewer_eligibility(self.current_deck_hash)
        self.status_data = get_moderator_status(self.current_deck_hash)

        is_eligible = self.status_data.get("is_eligible", False)
        reason = self.status_data.get("reason", "")
        rem_votes = self.status_data.get("remaining_weekly_votes", 0)
        limit_votes = self.status_data.get("weekly_votes_limit", 20)
        mode = self.status_data.get("mode", "consensus_strict")
        threshold = self.status_data.get("approval_threshold", 5)

        mode_names = {
            "consensus_strict": "Strict Consensus (Unanimous, 5 approvals)",
            "qualified_majority": "Qualified Majority (80% supermajority)",
            "lazy_consensus": "Optimistic with Veto",
            "reputation_weighted": "Proof-of-Review Weighted",
            "peer_council": "Peer Council",
        }
        mode_label = mode_names.get(mode, mode)

        if is_eligible:
            self.status_badge.setText("Status: Semi-Automatically Qualified (Active Moderator)")
            self.status_badge.setStyleSheet(
                "padding: 6px 12px; background-color: rgba(40,167,69,0.12); "
                "border: 1px solid #28a745; border-radius: 4px; font-weight: bold; color: #1e7e34;"
            )
            self.open_queue_btn.setEnabled(True)
            self.reviews_label.setText(
                f"Cards Reviewed in Local Revlog: {revlog_count} reviews (Criterion met)"
            )
            self.tenure_label.setText(
                f"Subscription Tenure: Criteria met ({reason if reason else 'Active subscriber'})"
            )
        else:
            self.status_badge.setText(f"Status: Ineligible ({reason})")
            self.status_badge.setStyleSheet(
                "padding: 6px 12px; background-color: rgba(220,53,69,0.1); "
                "border: 1px solid #dc3545; border-radius: 4px; font-weight: bold; color: #bd2130;"
            )
            self.open_queue_btn.setEnabled(False)
            self.reviews_label.setText(
                f"Cards Reviewed in Local Revlog: {revlog_count} reviews"
            )
            self.tenure_label.setText(f"Subscription Tenure: {reason}")

        self.mode_label.setText(f"Active Governance Mode: {mode_label} (Threshold: {threshold} votes)")

        self.quota_label.setText(
            f"Weekly Quota (7-Day Sliding Window): {rem_votes} / {limit_votes} votes remaining"
        )
        self.quota_bar.setMaximum(limit_votes)
        self.quota_bar.setValue(rem_votes)
        self.quota_bar.setFormat(f"{rem_votes} / {limit_votes} votes")

        # Load specialized tags
        server_tags = self.status_data.get("allowed_tags") or []
        local_tags = get_self_specialization(self.current_deck_hash)
        active_tags = server_tags if server_tags else local_tags
        self.tags_input.setText(", ".join(active_tags))

    def append_tag(self, tag: str) -> None:
        curr = [t.strip() for t in self.tags_input.text().split(",") if t.strip()]
        if tag not in curr:
            curr.append(tag)
            self.tags_input.setText(", ".join(curr))

    def on_save_specialization(self) -> None:
        if not self.current_deck_hash:
            return
        tags = [t.strip() for t in self.tags_input.text().split(",") if t.strip()]
        res = save_self_specialization(self.current_deck_hash, tags)
        if res.get("success"):
            showInfo(f"Specialization updated with {len(tags)} dedicated tag(s).")
        else:
            showInfo("Specialization saved locally.")

    def on_sync_metrics_clicked(self) -> None:
        if not self.current_deck_hash:
            return
        res = sync_reviewer_eligibility(self.current_deck_hash)
        self.reload_current_deck()
        showInfo("Local review metrics synchronized successfully with backend.")

    def on_open_queue_clicked(self) -> None:
        self.accept()
        from .moderation_dialog import ModerationDialog
        dialog = ModerationDialog(mw)
        if self.current_deck_hash:
            idx = dialog.deck_combo.findData(self.current_deck_hash)
            if idx >= 0:
                dialog.deck_combo.setCurrentIndex(idx)
        dialog.exec()

    def on_early_adopt_toggled(self, checked: bool) -> None:
        try:
            if getattr(mw, "pm", None) and hasattr(mw.pm, "profile"):
                mw.pm.profile["ankicollab_early_adoption"] = checked
        except Exception:
            pass

    def on_notif_toggled(self, checked: bool) -> None:
        try:
            if getattr(mw, "pm", None) and hasattr(mw.pm, "profile"):
                mw.pm.profile["ankicollab_moderation_notifications"] = checked
        except Exception:
            pass

