"""
moderation_dialog.py - PyQt6 Native Moderation & Democratic Voting Interface
for AnkiCollab.
Provides Anki-like card preview (Recto, <hr id="answer">, Verso), dual-mode
HTML source diff inspection, and links to Community Moderation Settings.
Strictly adheres to minimalist, sober, and emoji-free UI guidelines.
"""

from __future__ import annotations

import difflib
import html
import re
from typing import Any, Dict, List, Optional

from aqt import mw
from aqt.qt import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QComboBox,
    QTextBrowser,
    QTabWidget,
    QWidget,
    Qt,
)
from aqt.utils import showInfo

from .community_moderation import (
    get_moderation_queue,
    get_moderator_status,
    submit_vote,
    apply_suggestion_locally,
    sync_reviewer_eligibility,
    get_self_specialization,
)
from .utils import DeckManager
from .ui.colors import get_button_style, get_dialog_style


def get_local_note_fields(note_guid: str) -> List[str]:
    """Retrieve the field contents of a local note by its GUID."""
    if not mw or not mw.col or not note_guid:
        return []
    try:
        nid = mw.col.db.scalar("SELECT id FROM notes WHERE guid = ?", note_guid)
        if nid:
            note = mw.col.get_note(nid)
            return list(note.fields)
    except Exception:
        pass
    return []


def generate_word_diff_html(old_text: str, new_text: str) -> str:
    """Generate subtle word-level diff highlights."""
    old_words = re.findall(r"<[^>]+>|\S+|\s+", old_text)
    new_words = re.findall(r"<[^>]+>|\S+|\s+", new_text)

    matcher = difflib.SequenceMatcher(None, old_words, new_words)
    diff_out = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            diff_out.append("".join(new_words[j1:j2]))
        elif tag == "insert":
            inserted = "".join(new_words[j1:j2])
            diff_out.append(
                f'<span style="background-color: #d4edda; color: #155724; '
                f'font-weight: bold; border-radius: 2px; padding: 1px 3px;">{inserted}</span>'
            )
        elif tag == "delete":
            deleted = "".join(old_words[i1:i2])
            diff_out.append(
                f'<span style="background-color: #f8d7da; color: #721c24; '
                f'text-decoration: line-through; border-radius: 2px; padding: 1px 3px;">{deleted}</span>'
            )
        elif tag == "replace":
            deleted = "".join(old_words[i1:i2])
            inserted = "".join(new_words[j1:j2])
            diff_out.append(
                f'<span style="background-color: #f8d7da; color: #721c24; '
                f'text-decoration: line-through; border-radius: 2px; padding: 1px 3px;">{deleted}</span> '
                f'<span style="background-color: #d4edda; color: #155724; '
                f'font-weight: bold; border-radius: 2px; padding: 1px 3px;">{inserted}</span>'
            )
    return "".join(diff_out)


class ModerationDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("AnkiCollab - Community Moderation")
        self.setMinimumSize(880, 660)
        self.setStyleSheet(get_dialog_style())

        self.queue: List[Dict[str, Any]] = []
        self.current_index: int = 0
        self.current_deck_hash: str = ""
        self.status_info: Dict[str, Any] = {}

        self.init_ui()
        self.load_subscribed_decks()

    def init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        # Deck selector row
        deck_row = QHBoxLayout()
        deck_label = QLabel("Deck:")
        deck_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        deck_row.addWidget(deck_label)

        self.deck_combo = QComboBox()
        self.deck_combo.currentIndexChanged.connect(self.on_deck_changed)
        deck_row.addWidget(self.deck_combo, 1)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setStyleSheet(get_button_style("default"))
        self.refresh_btn.clicked.connect(self.reload_current_deck)
        deck_row.addWidget(self.refresh_btn)

        self.settings_btn = QPushButton("Moderation Settings...")
        self.settings_btn.setStyleSheet(get_button_style("default"))
        self.settings_btn.clicked.connect(self.on_open_settings)
        deck_row.addWidget(self.settings_btn)

        main_layout.addLayout(deck_row)

        # Header metadata frame
        self.meta_header = QLabel("Loading moderation status...")
        self.meta_header.setStyleSheet(
            "padding: 8px 12px; background-color: rgba(128,128,128,0.08); "
            "border: 1px solid rgba(128,128,128,0.2); border-radius: 4px; font-size: 12px;"
        )
        self.meta_header.setWordWrap(True)
        main_layout.addWidget(self.meta_header)

        # Queue progress
        self.progress_label = QLabel("0 suggestions in queue")
        self.progress_label.setStyleSheet("font-size: 11px; color: gray;")
        main_layout.addWidget(self.progress_label)

        # Dual-mode Tab Widget: Anki Card Preview vs Raw HTML Code Diff
        self.tab_widget = QTabWidget()
        self.tab_widget.setStyleSheet(
            "QTabWidget::pane { border: 1px solid rgba(128,128,128,0.25); border-radius: 4px; background: #ffffff; }"
            "QTabBar::tab { padding: 6px 14px; font-size: 12px; margin-right: 2px; }"
            "QTabBar::tab:selected { font-weight: bold; border-bottom: 2px solid #2b7bb9; }"
        )

        # Tab 1: Anki-like Card Preview (Visual)
        self.card_preview_browser = QTextBrowser()
        self.card_preview_browser.setOpenExternalLinks(False)
        self.card_preview_browser.setStyleSheet("border: none; padding: 12px;")
        self.tab_widget.addTab(self.card_preview_browser, "Anki Card Preview (Rendered)")

        # Tab 2: Raw HTML Source Code Diff
        self.html_source_browser = QTextBrowser()
        self.html_source_browser.setOpenExternalLinks(False)
        self.html_source_browser.setStyleSheet(
            "border: none; padding: 12px; font-family: Consolas, monospace; font-size: 12px; background-color: #fafbfc;"
        )
        self.tab_widget.addTab(self.html_source_browser, "Raw HTML Source (Diff)")

        main_layout.addWidget(self.tab_widget, 1)

        # Action buttons row
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        self.approve_btn = QPushButton("Approve & Apply to My Deck")
        self.approve_btn.setStyleSheet(get_button_style("success"))
        self.approve_btn.clicked.connect(self.on_approve_clicked)
        btn_layout.addWidget(self.approve_btn)

        self.reject_btn = QPushButton("Reject")
        self.reject_btn.setStyleSheet(get_button_style("danger"))
        self.reject_btn.clicked.connect(self.on_reject_clicked)
        btn_layout.addWidget(self.reject_btn)

        self.flag_btn = QPushButton("Flag for Maintainer")
        self.flag_btn.setStyleSheet(get_button_style("warning"))
        self.flag_btn.clicked.connect(self.on_flag_clicked)
        btn_layout.addWidget(self.flag_btn)

        self.skip_btn = QPushButton("Skip")
        self.skip_btn.setStyleSheet(get_button_style("default"))
        self.skip_btn.clicked.connect(self.on_skip_clicked)
        btn_layout.addWidget(self.skip_btn)

        main_layout.addLayout(btn_layout)

    def load_subscribed_decks(self) -> None:
        self.deck_combo.clear()
        manager = DeckManager()
        subscribed = manager.get_subscribed_decks()

        if not subscribed:
            self.deck_combo.addItem("No subscribed decks found", "")
            self.set_buttons_enabled(False)
            empty_html = "<div style='padding:30px; text-align:center; color:gray;'>You are not subscribed to any collaborative decks.</div>"
            self.card_preview_browser.setHtml(empty_html)
            self.html_source_browser.setHtml(empty_html)
            return

        for deck_hash in subscribed:
            local_name = manager.get_deck_name_by_hash(deck_hash) or deck_hash
            self.deck_combo.addItem(local_name, deck_hash)

        if self.deck_combo.count() > 0:
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

        # Synchronize eligibility metrics
        sync_res = sync_reviewer_eligibility(self.current_deck_hash)
        self.status_info = get_moderator_status(self.current_deck_hash)

        is_eligible = self.status_info.get("is_eligible", False)
        reason = self.status_info.get("reason", "")
        rem_votes = self.status_info.get("remaining_weekly_votes", 0)
        limit_votes = self.status_info.get("weekly_votes_limit", 20)
        mode = self.status_info.get("mode", "consensus_strict")
        threshold = self.status_info.get("approval_threshold", 5)

        mode_names = {
            "consensus_strict": "Strict Consensus (Unanimous)",
            "qualified_majority": "Qualified Majority (80%)",
            "lazy_consensus": "Optimistic with Veto",
            "reputation_weighted": "Proof-of-Review Weighted",
            "peer_council": "Peer Council",
        }
        mode_label = mode_names.get(mode, mode)

        if not is_eligible:
            self.meta_header.setText(
                f"Status: Ineligible for moderation\nReason: {reason}\n"
                f"Mode: {mode_label} | Quota: {rem_votes} / {limit_votes} weekly votes remaining"
            )
            self.set_buttons_enabled(False)
            inelig_html = (
                f"<div style='padding:30px; text-align:center; color:gray;'>"
                f"<h3>Moderation Not Available</h3>"
                f"<p>{html.escape(reason)}</p>"
                f"</div>"
            )
            self.card_preview_browser.setHtml(inelig_html)
            self.html_source_browser.setHtml(inelig_html)
            self.progress_label.setText("0 suggestions available")
            return

        specialized_tags = get_self_specialization(self.current_deck_hash)
        spec_text = f" | Dedicated Tags: {', '.join(specialized_tags)}" if specialized_tags else ""

        self.meta_header.setText(
            f"Governance Mode: {mode_label} (Threshold: {threshold} votes) | "
            f"Weekly Quota: {rem_votes} / {limit_votes} remaining (7-day sliding window){spec_text}"
        )

        self.queue = get_moderation_queue(self.current_deck_hash)
        self.current_index = 0
        self.render_current_item()

    def set_buttons_enabled(self, enabled: bool) -> None:
        self.approve_btn.setEnabled(enabled)
        self.reject_btn.setEnabled(enabled)
        self.flag_btn.setEnabled(enabled)
        self.skip_btn.setEnabled(enabled)

    def render_current_item(self) -> None:
        if not self.queue or self.current_index >= len(self.queue):
            self.set_buttons_enabled(False)
            self.progress_label.setText("No pending suggestions in queue")
            done_html = (
                "<div style='padding:40px; text-align:center; color:gray;'>"
                "<h3>Queue Complete</h3>"
                "<p>No pending suggestions require moderation for this deck at this time.</p>"
                "</div>"
            )
            self.card_preview_browser.setHtml(done_html)
            self.html_source_browser.setHtml(done_html)
            return

        self.set_buttons_enabled(True)
        item = self.queue[self.current_index]
        total = len(self.queue)
        pos = self.current_index + 1
        self.progress_label.setText(f"Suggestion {pos} of {total}")

        stype = item.get("suggestion_type", "")
        approves = item.get("approve_count", 0)
        rejects = item.get("reject_count", 0)
        threshold = item.get("threshold", 5)
        guid = item.get("note_guid", "")

        # Fetch local fields if available, otherwise fallback to item payload
        local_fields = get_local_note_fields(guid)
        recto_base = local_fields[0] if len(local_fields) > 0 else (item.get("recto") or "")
        verso_base = local_fields[1] if len(local_fields) > 1 else (item.get("verso") or "")

        # ------------------------------------------------------------------
        # Render Tab 1: Anki Card Preview (Visual Question, <hr id="answer">, Answer)
        # ------------------------------------------------------------------
        if stype == "field":
            pos_num = item.get("field_position", 0)
            old_c = item.get("old_content") or ""
            new_c = item.get("new_content") or ""

            if pos_num == 0:
                current_recto = old_c if old_c else recto_base
                proposed_recto = generate_word_diff_html(old_c, new_c) if old_c else new_c
                current_verso = verso_base
                proposed_verso = verso_base
            elif pos_num == 1:
                current_recto = recto_base
                proposed_recto = recto_base
                current_verso = old_c if old_c else verso_base
                proposed_verso = generate_word_diff_html(old_c, new_c) if old_c else new_c
            else:
                current_recto = recto_base
                proposed_recto = recto_base
                current_verso = f"{verso_base}<br><small style='color:gray;'>Field {pos_num}:</small> {old_c}"
                proposed_verso = f"{verso_base}<br><small style='color:gray;'>Field {pos_num}:</small> {new_c}"

            card_preview_html = f"""
            <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #212529;">
              <div style="margin-bottom: 12px; padding: 6px 12px; background-color: #f1f3f5; border-radius: 4px; font-size: 12px;">
                <b>Field Modification:</b> Position {pos_num} | <b>Votes:</b> {approves} / {threshold} Approvals, {rejects} Objections | <b>Note GUID:</b> <code>{html.escape(guid)}</code>
              </div>

              <table style="width: 100%; border-collapse: separate; border-spacing: 12px 0;">
                <tr>
                  <td style="width: 50%; vertical-align: top; padding: 0;">
                    <div style="font-size: 12px; font-weight: bold; color: #721c24; margin-bottom: 6px; text-transform: uppercase;">
                      Current Card (Official Deck)
                    </div>
                    <div style="border: 1px solid #e0e0e0; border-radius: 6px; padding: 16px; background-color: #ffffff; box-shadow: 0 1px 3px rgba(0,0,0,0.05); min-height: 220px;">
                      <div class="card-question" style="font-size: 15px; line-height: 1.5; color: #212529; text-align: center; margin-bottom: 12px;">
                        {current_recto or '<i style="color:gray;">(Empty Recto)</i>'}
                      </div>
                      <hr id="answer" style="border: 0; height: 1px; background-color: #cccccc; margin: 16px 0;" />
                      <div class="card-answer" style="font-size: 15px; line-height: 1.5; color: #212529; text-align: center;">
                        {current_verso or '<i style="color:gray;">(Empty Verso)</i>'}
                      </div>
                    </div>
                  </td>

                  <td style="width: 50%; vertical-align: top; padding: 0;">
                    <div style="font-size: 12px; font-weight: bold; color: #155724; margin-bottom: 6px; text-transform: uppercase;">
                      Proposed Card (Community Submission)
                    </div>
                    <div style="border: 1px solid #b8daff; border-radius: 6px; padding: 16px; background-color: #fafcff; box-shadow: 0 1px 3px rgba(0,0,0,0.05); min-height: 220px;">
                      <div class="card-question" style="font-size: 15px; line-height: 1.5; color: #212529; text-align: center; margin-bottom: 12px;">
                        {proposed_recto}
                      </div>
                      <hr id="answer" style="border: 0; height: 1px; background-color: #cccccc; margin: 16px 0;" />
                      <div class="card-answer" style="font-size: 15px; line-height: 1.5; color: #212529; text-align: center;">
                        {proposed_verso}
                      </div>
                    </div>
                  </td>
                </tr>
              </table>
            </div>
            """

            # ------------------------------------------------------------------
            # Render Tab 2: Raw HTML Source Code Diff
            # ------------------------------------------------------------------
            escaped_old_html = html.escape(old_c)
            escaped_new_html = html.escape(new_c)

            html_source_diff = f"""
            <div style="font-family: Consolas, 'Liberation Mono', Menlo, monospace; font-size: 12px;">
              <div style="margin-bottom: 10px; font-family: -apple-system, sans-serif; font-size: 12px; color: #555555;">
                Raw HTML source view for Position {pos_num}. Review tags, clozes (<code>{{{{c1::...}}}}</code>), and inline styling.
              </div>

              <div style="margin-bottom: 14px;">
                <div style="font-weight: bold; color: #dc3545; margin-bottom: 4px;">--- Current Version HTML (Raw Source):</div>
                <pre style="background: #fff5f5; border: 1px solid #f5c6cb; border-radius: 4px; padding: 10px; margin: 0; white-space: pre-wrap; word-break: break-all;">{escaped_old_html or '(empty)'}</pre>
              </div>

              <div>
                <div style="font-weight: bold; color: #28a745; margin-bottom: 4px;">+++ Proposed Version HTML (Raw Source):</div>
                <pre style="background: #f0fff4; border: 1px solid #c3e6cb; border-radius: 4px; padding: 10px; margin: 0; white-space: pre-wrap; word-break: break-all;">{escaped_new_html or '(empty)'}</pre>
              </div>
            </div>
            """

        elif stype == "tag":
            tag_name = item.get("tag", "")
            action = item.get("tag_action", True)
            action_desc = "Add tag" if action else "Remove tag"
            action_color = "#155724" if action else "#721c24"
            badge_bg = "#d4edda" if action else "#f8d7da"

            card_preview_html = f"""
            <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #212529;">
              <div style="margin-bottom: 12px; padding: 8px 12px; background-color: {badge_bg}; border: 1px solid {action_color}; border-radius: 4px; font-size: 13px; color: {action_color};">
                <b>Proposed Tag Action:</b> {action_desc} <code>{html.escape(tag_name)}</code> | <b>Votes:</b> {approves} / {threshold} Approvals
              </div>

              <div style="border: 1px solid #e0e0e0; border-radius: 6px; padding: 20px; background-color: #ffffff; box-shadow: 0 1px 3px rgba(0,0,0,0.05); max-width: 650px; margin: 0 auto;">
                <div style="font-size: 11px; text-transform: uppercase; color: #888888; margin-bottom: 8px; font-weight: bold;">
                  Target Card Preview
                </div>
                <div class="card-question" style="font-size: 15px; line-height: 1.5; color: #212529; text-align: center; margin-bottom: 12px;">
                  {recto_base or '<i style="color:gray;">(Question)</i>'}
                </div>
                <hr id="answer" style="border: 0; height: 1px; background-color: #cccccc; margin: 16px 0;" />
                <div class="card-answer" style="font-size: 15px; line-height: 1.5; color: #212529; text-align: center;">
                  {verso_base or '<i style="color:gray;">(Answer)</i>'}
                </div>
              </div>
            </div>
            """

            html_source_diff = f"""
            <div style="font-family: Consolas, monospace; font-size: 12px;">
              <p>Tag modification on note <code>{html.escape(guid)}</code>:</p>
              <pre style="background: #f8f9fa; border: 1px solid #e9ecef; border-radius: 4px; padding: 12px;">Action: {action_desc}\nTag: {html.escape(tag_name)}</pre>
            </div>
            """
        else:
            card_preview_html = f"<p>Unknown suggestion type: {stype}</p>"
            html_source_diff = "<p>No HTML diff available.</p>"

        self.card_preview_browser.setHtml(card_preview_html)
        self.html_source_browser.setHtml(html_source_diff)

    def on_open_settings(self) -> None:
        from .moderation_settings_dialog import ModerationSettingsDialog
        dlg = ModerationSettingsDialog(self, initial_deck_hash=self.current_deck_hash)
        dlg.exec()
        self.reload_current_deck()

    def on_approve_clicked(self) -> None:
        if self.current_index >= len(self.queue):
            return
        item = self.queue[self.current_index]

        early_adopted = True
        try:
            from aqt import mw
            if getattr(mw, "pm", None) and hasattr(mw.pm, "profile"):
                early_adopted = bool(mw.pm.profile.get("ankicollab_early_adoption", True))
        except Exception:
            pass

        # Optimistic Local Adoption: immediate apply to local collection if enabled
        if early_adopted:
            apply_suggestion_locally(
                note_guid=item.get("note_guid", ""),
                field_position=item.get("field_position"),
                new_content=item.get("new_content"),
                tag=item.get("tag"),
                tag_action=item.get("tag_action"),
                old_content=item.get("old_content"),
            )

        res = submit_vote(
            deck_hash=self.current_deck_hash,
            suggestion_type=item.get("suggestion_type", ""),
            suggestion_id=item.get("suggestion_id", 0),
            vote="approve",
            early_adopted=early_adopted,
        )

        self.handle_vote_result(res)

    def on_reject_clicked(self) -> None:
        if self.current_index >= len(self.queue):
            return
        item = self.queue[self.current_index]

        res = submit_vote(
            deck_hash=self.current_deck_hash,
            suggestion_type=item.get("suggestion_type", ""),
            suggestion_id=item.get("suggestion_id", 0),
            vote="reject",
            early_adopted=False,
        )

        self.handle_vote_result(res)

    def on_flag_clicked(self) -> None:
        if self.current_index >= len(self.queue):
            return
        item = self.queue[self.current_index]

        res = submit_vote(
            deck_hash=self.current_deck_hash,
            suggestion_type=item.get("suggestion_type", ""),
            suggestion_id=item.get("suggestion_id", 0),
            vote="flag",
            early_adopted=False,
        )

        self.handle_vote_result(res)

    def on_skip_clicked(self) -> None:
        self.current_index += 1
        self.render_current_item()

    def handle_vote_result(self, res: Dict[str, Any]) -> None:
        if res.get("success"):
            rem = res.get("remaining_weekly_votes")
            if rem is not None:
                limit = self.status_info.get("weekly_votes_limit", 20)
                self.meta_header.setText(
                    f"Weekly Quota: {rem} / {limit} remaining (7-day sliding window)"
                )
            self.current_index += 1
            self.render_current_item()
        else:
            showInfo(res.get("message", "Failed to submit vote."))
