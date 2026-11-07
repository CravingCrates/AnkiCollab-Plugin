"""Unit tests for update_review.py (Diff Import style update review)."""

import pytest
from unittest.mock import MagicMock, patch

from update_review import (
    NoteFieldDiff,
    NoteUpdateItem,
    UpdatePlan,
    build_update_plan,
)
from crowd_anki.importer.import_dialog import ImportConfig


class TestNoteFieldDiff:
    def test_diff_unchanged(self):
        diff = NoteFieldDiff(
            name="Front",
            old_val="Hello World",
            new_val="Hello World",
            changed=False,
        )
        colors = {"text_muted": "#888888"}
        html_out = diff.diff_html(colors)
        assert "Hello World" in html_out
        assert "Local:" not in html_out

    def test_diff_changed_html_escaping(self):
        diff = NoteFieldDiff(
            name="Back",
            old_val="<b>Old</b> & text",
            new_val="<b>New</b> & text",
            changed=True,
        )
        colors = {"text_muted": "#888888"}
        html_out = diff.diff_html(colors)
        assert "&lt;b&gt;Old&lt;/b&gt; &amp; text" in html_out
        assert "&lt;b&gt;New&lt;/b&gt; &amp; text" in html_out
        assert "Local:" in html_out
        assert "AnkiCollab:" in html_out


class TestNoteUpdateItem:
    def test_has_changes_field(self):
        item = NoteUpdateItem(
            guid="guid1",
            nid=101,
            deck_name="Default",
            notetype_name="Basic",
            is_suspended=False,
            field_diffs=[
                NoteFieldDiff("Front", "Old", "New", changed=True),
                NoteFieldDiff("Back", "Same", "Same", changed=False),
            ],
        )
        assert item.has_changes is True
        assert item.tags_changed is False

    def test_tags_changed_none(self):
        item = NoteUpdateItem(
            guid="guid_tc_none",
            nid=105,
            deck_name="Default",
            notetype_name="Basic",
            is_suspended=False,
        )
        assert item.tags_changed is False

    def test_has_no_changes(self):
        item = NoteUpdateItem(
            guid="guid2",
            nid=102,
            deck_name="Default",
            notetype_name="Basic",
            is_suspended=False,
            field_diffs=[
                NoteFieldDiff("Front", "Same", "Same", changed=False),
            ],
        )
        assert item.has_changes is False

    def test_has_changes_tags_only(self):
        item = NoteUpdateItem(
            guid="guid3",
            nid=103,
            deck_name="Default",
            notetype_name="Basic",
            is_suspended=False,
            field_diffs=[],
            tags_added=["new_tag"],
        )
        assert item.has_changes is True
        assert item.tags_changed is True

    def test_tags_changed_none(self):
        item = NoteUpdateItem(
            guid="guid_tc_none",
            nid=105,
            deck_name="Default",
            notetype_name="Basic",
            is_suspended=False,
        )
        assert item.tags_changed is False

    def test_display_title_strips_html(self):
        item = NoteUpdateItem(
            guid="guid4",
            nid=104,
            deck_name="Default",
            notetype_name="Basic",
            is_suspended=False,
            field_diffs=[
                NoteFieldDiff("Front", "<div><b>Anatomy Question</b></div>", "<div><b>Anatomy Question</b></div>", changed=False),
            ],
        )
        assert item.display_title == "Anatomy Question"


class TestUpdatePlanApplyToConfig:
    def test_apply_keep_entire_local_note(self):
        plan = UpdatePlan(deck_hash="hash123")
        item = NoteUpdateItem(
            guid="guid_keep",
            nid=1,
            deck_name="Deck",
            notetype_name="Basic",
            is_suspended=False,
            note_choice="local",
        )
        plan.items.append(item)

        config = ImportConfig()
        plan.apply_to_config(config)

        assert "guid_keep" in config.skip_update_guids
        assert "guid_keep" not in config.transient_protections

    def test_apply_field_by_field_decisions(self):
        plan = UpdatePlan(deck_hash="hash123")
        f1 = NoteFieldDiff("Front", "OldF", "NewF", changed=True, choice="local", permanent_protect=False)
        f2 = NoteFieldDiff("Back", "OldB", "NewB", changed=True, choice="remote", permanent_protect=False)
        f3 = NoteFieldDiff("Notes", "OldN", "NewN", changed=True, choice="local", permanent_protect=True)

        item = NoteUpdateItem(
            guid="guid_field_choice",
            nid=2,
            deck_name="Deck",
            notetype_name="Basic",
            is_suspended=False,
            field_diffs=[f1, f2, f3],
            tags_choice="local",
        )
        plan.items.append(item)

        config = ImportConfig()
        plan.apply_to_config(config)

        assert "guid_field_choice" not in config.skip_update_guids
        # Transient protections include Front, Notes, and Tags
        assert "Front" in config.transient_protections["guid_field_choice"]
        assert "Notes" in config.transient_protections["guid_field_choice"]
        assert "Tags" in config.transient_protections["guid_field_choice"]
        assert "Back" not in config.transient_protections["guid_field_choice"]

        # Permanent protections include Notes
        assert "Notes" in config.permanent_protections["guid_field_choice"]
        assert "Front" not in config.permanent_protections.get("guid_field_choice", [])

    def test_apply_unsuspended_only_auto_accept_suspended(self):
        plan = UpdatePlan(deck_hash="hash123", only_unsuspended=True, auto_accept_suspended=True)
        item = NoteUpdateItem(
            guid="guid_suspended",
            nid=10,
            deck_name="Deck",
            notetype_name="Basic",
            is_suspended=True,
            note_choice="remote",
        )
        plan.items.append(item)
        config = ImportConfig()
        plan.apply_to_config(config)

        # Because auto_accept_suspended is True, guid_suspended is NOT skipped
        assert "guid_suspended" not in config.skip_update_guids

    def test_apply_unsuspended_only_keep_local_suspended(self):
        plan = UpdatePlan(deck_hash="hash123", only_unsuspended=True, auto_accept_suspended=False)
        item = NoteUpdateItem(
            guid="guid_suspended",
            nid=10,
            deck_name="Deck",
            notetype_name="Basic",
            is_suspended=True,
            note_choice="remote",
        )
        plan.items.append(item)
        config = ImportConfig()
        plan.apply_to_config(config)

        # Because auto_accept_suspended is False, suspended cards are kept local
        assert "guid_suspended" in config.skip_update_guids


class TestBuildUpdatePlan:
    def test_build_update_plan_handles_none_col(self):
        mock_deck = MagicMock()
        mock_deck._collect_all_notes = MagicMock()
        config = ImportConfig(deck_hash="hash1")

        plan = build_update_plan(mock_deck, None, config)
        assert plan.total_changed == 0
        assert plan.deck_hash == "hash1"

    def test_build_update_plan_suspended_detection(self):
        mock_deck = MagicMock()
        remote_note = MagicMock()
        remote_note.get_uuid.return_value = "guid_susp"
        remote_note.anki_object_dict = {
            "fields": ["Remote Question", "Remote Answer"],
            "tags": ["tag1"],
        }
        mock_deck._collect_all_notes = lambda notes_list, mapping, parent, home: notes_list.append(remote_note)

        mock_col = MagicMock()
        # db.all calls: 1st for notes, 2nd for cards queue
        def mock_db_all(query, *args):
            if "FROM notes" in query:
                return [("guid_susp", 42)]
            elif "FROM cards" in query:
                return [(42, -1), (42, -1)]  # All cards queue -1 => suspended
            return []

        mock_col.db.all.side_effect = mock_db_all

        mock_local_note = MagicMock()
        mock_local_note.fields = ["Local Question", "Local Answer"]
        mock_local_note.tags = ["tag1"]
        mock_local_note.note_type.return_value = {
            "name": "Basic",
            "flds": [{"name": "Front"}, {"name": "Back"}],
        }
        mock_col.get_note.return_value = mock_local_note

        config = ImportConfig(deck_hash="test_deck")
        plan = build_update_plan(mock_deck, mock_col, config)

        assert plan.total_changed == 1
        item = plan.items[0]
        assert item.guid == "guid_susp"
        assert item.is_suspended is True
        assert plan.total_unsuspended == 0
        assert len(item.field_diffs) == 2
        assert item.field_diffs[0].changed is True
        assert item.field_diffs[0].old_val == "Local Question"
        assert item.field_diffs[0].new_val == "Remote Question"


class TestImportManagerReviewIntegration:
    def test_should_review_flags(self):
        from import_manager import (
            should_review_updates_before_applying,
            should_review_unsuspended_only,
        )

        with patch("import_manager.mw") as mock_mw:
            # Default empty settings
            mock_mw.addonManager.getConfig.return_value = {"settings": {}}
            assert should_review_updates_before_applying() is False
            assert should_review_unsuspended_only() is False

            # Enabled in settings
            mock_mw.addonManager.getConfig.return_value = {
                "settings": {
                    "review_updates_before_applying": True,
                    "review_unsuspended_only": True,
                }
            }
            assert should_review_updates_before_applying() is True
            assert should_review_unsuspended_only() is True

    @patch("import_manager.start_update_review")
    @patch("import_manager.install_update")
    @patch("import_manager.ChangelogDialog")
    @patch("import_manager.update_deck_stats_enabled")
    def test_show_changelog_review_button_clicked(
        self, mock_stats, mock_dialog_cls, mock_install, mock_start_review
    ):
        from import_manager import show_changelog_popup

        mock_dialog_instance = MagicMock()
        mock_dialog_instance.exec.return_value = 3  # "Review Changes" button
        mock_dialog_instance.should_update_suspended.return_value = True
        mock_dialog_cls.return_value = mock_dialog_instance

        sub = {
            "changelog": "Bugfixes and new cards",
            "deck_hash": "hash_abc",
            "stats_enabled": False,
        }

        show_changelog_popup(sub)

        mock_start_review.assert_called_once_with(sub, update_timestamp_after=True, auto_accept_suspended=True)
        mock_install.assert_not_called()

    @patch("import_manager.start_update_review")
    @patch("import_manager.install_update")
    @patch("import_manager.ChangelogDialog")
    @patch("import_manager.update_deck_stats_enabled")
    @patch("import_manager.should_review_updates_before_applying", return_value=True)
    def test_show_changelog_accepted_with_review_setting_on(
        self, mock_should_rev, mock_stats, mock_dialog_cls, mock_install, mock_start_review
    ):
        from import_manager import show_changelog_popup

        mock_dialog_instance = MagicMock()
        mock_dialog_instance.exec.return_value = 1  # Accepted
        mock_dialog_instance.should_update_suspended.return_value = True
        mock_dialog_cls.return_value = mock_dialog_instance

        sub = {
            "changelog": "New cards",
            "deck_hash": "hash_xyz",
            "stats_enabled": False,
        }

        show_changelog_popup(sub)

        mock_start_review.assert_called_once_with(sub, update_timestamp_after=True, auto_accept_suspended=True)
        mock_install.assert_not_called()

    @patch("import_manager.start_update_review")
    @patch("import_manager.install_update")
    @patch("import_manager.ChangelogDialog")
    @patch("import_manager.update_deck_stats_enabled")
    @patch("import_manager.should_review_updates_before_applying", return_value=False)
    def test_show_changelog_accepted_without_accepting_suspended(
        self, mock_should_rev, mock_stats, mock_dialog_cls, mock_install, mock_start_review
    ):
        from import_manager import show_changelog_popup

        mock_dialog_instance = MagicMock()
        mock_dialog_instance.exec.return_value = 1  # Accepted
        mock_dialog_instance.should_update_suspended.return_value = False
        mock_dialog_cls.return_value = mock_dialog_instance

        sub = {
            "changelog": "New cards",
            "deck_hash": "hash_xyz",
            "stats_enabled": False,
        }

        show_changelog_popup(sub)

        # When auto_accept_suspended is False, it routes through review to protect suspended cards
        mock_start_review.assert_called_once_with(sub, update_timestamp_after=True, auto_accept_suspended=False)
        mock_install.assert_not_called()
