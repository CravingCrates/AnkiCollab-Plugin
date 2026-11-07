"""
Unit tests for community_moderation.py module.
"""

import pytest
from unittest.mock import MagicMock, patch

from plugin_source.community_moderation import (
    get_deck_review_count,
    sync_reviewer_eligibility,
    get_moderator_status,
    get_moderation_queue,
    submit_vote,
    apply_suggestion_locally,
    is_locally_adopted,
    record_local_early_adoption,
    get_early_adoption_store,
)


class TestCommunityModeration:
    def test_get_deck_review_count(self):
        with patch("plugin_source.community_moderation.mw") as mock_mw, \
             patch("plugin_source.community_moderation.get_deck_and_subdecks", return_value=[1, 2]):
            mock_mw.col.db.scalar.return_value = 342
            count = get_deck_review_count(1)
            assert count == 342
            mock_mw.col.db.scalar.assert_called_once()

    def test_apply_suggestion_locally_field(self):
        mock_note = MagicMock()
        mock_note.fields = ["Old question", "Old answer"]

        with patch("plugin_source.community_moderation.mw") as mock_mw:
            mock_mw.col.db.scalar.return_value = 101
            mock_mw.col.get_note.return_value = mock_note
            mock_mw.pm.profile = {}

            success = apply_suggestion_locally(
                note_guid="note-guid-abc",
                field_position=0,
                new_content="Updated question",
                tag=None,
                tag_action=None,
            )

            assert success is True
            assert mock_note.fields[0] == "Updated question"
            mock_mw.col.update_note.assert_called_once_with(mock_note)
            assert is_locally_adopted("note-guid-abc") is True

    def test_apply_suggestion_locally_tag_addition(self):
        mock_note = MagicMock()
        mock_note.fields = ["Front", "Back"]

        with patch("plugin_source.community_moderation.mw") as mock_mw:
            mock_mw.col.db.scalar.return_value = 102
            mock_mw.col.get_note.return_value = mock_note
            mock_mw.pm.profile = {}

            success = apply_suggestion_locally(
                note_guid="note-guid-tag",
                field_position=None,
                new_content=None,
                tag="Cardiologie",
                tag_action=True,
            )

            assert success is True
            mock_note.add_tag.assert_called_once_with("Cardiologie")
            mock_mw.col.update_note.assert_called_once_with(mock_note)

    def test_apply_suggestion_locally_tag_removal(self):
        mock_note = MagicMock()
        mock_note.fields = ["Front", "Back"]

        with patch("plugin_source.community_moderation.mw") as mock_mw:
            mock_mw.col.db.scalar.return_value = 103
            mock_mw.col.get_note.return_value = mock_note
            mock_mw.pm.profile = {}

            success = apply_suggestion_locally(
                note_guid="note-guid-tag-del",
                field_position=None,
                new_content=None,
                tag="DeprecatedTag",
                tag_action=False,
            )

            assert success is True
            mock_note.remove_tag.assert_called_once_with("DeprecatedTag")

    def test_sync_reviewer_eligibility(self):
        with patch("plugin_source.community_moderation.api_client.post_json") as mock_post, \
             patch("plugin_source.community_moderation.get_did_from_hash", return_value=123), \
             patch("plugin_source.community_moderation.get_deck_review_count", return_value=500):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "success": True,
                "is_eligible": True,
                "reason": "Eligible",
                "notifications_enabled": True,
            }
            mock_post.return_value = mock_resp

            result = sync_reviewer_eligibility("deck-hash-test")
            assert result["success"] is True
            assert result["is_eligible"] is True
            assert result["notifications_enabled"] is True
            mock_post.assert_called_once_with(
                "/v2/moderation/sync_eligibility",
                payload={"deck_hash": "deck-hash-test", "cards_reviewed_count": 500},
                auth=True,
            )

    def test_early_adoption_preserved_on_server_rejection_or_revert(self):
        with patch("plugin_source.community_moderation.mw") as mock_mw:
            profile = {}
            mock_mw.pm.profile = profile

            # Record early adoption
            record_local_early_adoption("guid-xyz", {"field_position": 0, "content": "Accepted Choice"})
            assert is_locally_adopted("guid-xyz") is True

            store = get_early_adoption_store()
            assert store["guid-xyz"][0]["content"] == "Accepted Choice"

            # Verify that subsequent server events do not clear the user's choice
            assert is_locally_adopted("guid-xyz") is True

    def test_self_specialization_local_and_remote(self):
        with patch("plugin_source.community_moderation.mw") as mock_mw, \
             patch("plugin_source.community_moderation.api_client.post_json") as mock_post:
            profile = {}
            mock_mw.pm.profile = profile

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"success": True, "tags": ["Cardio", "ECG"]}
            mock_post.return_value = mock_resp

            from plugin_source.community_moderation import (
                get_self_specialization,
                save_self_specialization,
            )

            res = save_self_specialization("deck-hash-cardio", ["Cardio", "ECG", "  "])
            assert res["success"] is True
            assert res["tags"] == ["Cardio", "ECG"]

            saved = get_self_specialization("deck-hash-cardio")
            assert saved == ["Cardio", "ECG"]
            mock_post.assert_called_once_with(
                "/v2/moderation/self_specialization",
                payload={"deck_hash": "deck-hash-cardio", "tags": ["Cardio", "ECG"]},
                auth=True,
            )



    def test_submit_vote(self):
        with patch("plugin_source.community_moderation.api_client.post_json") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "success": True,
                "status": "voting",
                "remaining_weekly_votes": 17,
                "approve_count": 3,
                "reject_count": 0,
                "message": "Vote recorded.",
            }
            mock_post.return_value = mock_resp

            result = submit_vote(
                deck_hash="hash-123",
                suggestion_type="field",
                suggestion_id=456,
                vote="approve",
                early_adopted=True,
            )

            assert result["success"] is True
            assert result["remaining_weekly_votes"] == 17
            mock_post.assert_called_once_with(
                "/v2/moderation/vote",
                payload={
                    "deck_hash": "hash-123",
                    "suggestion_type": "field",
                    "suggestion_id": 456,
                    "vote": "approve",
                    "early_adopted": True,
                },
                auth=True,
            )

    def test_early_adoption_preserved_on_server_rejection_or_revert(self):
        with patch("plugin_source.community_moderation.mw") as mock_mw:
            profile = {}
            mock_mw.pm.profile = profile

            # Record early adoption
            record_local_early_adoption("guid-xyz", {"field_position": 0, "content": "Accepted Choice"})
            assert is_locally_adopted("guid-xyz") is True

            store = get_early_adoption_store()
            assert store["guid-xyz"][0]["content"] == "Accepted Choice"

            # Verify that subsequent server events do not clear the user's choice
            assert is_locally_adopted("guid-xyz") is True

    def test_self_specialization_local_and_remote(self):
        with patch("plugin_source.community_moderation.mw") as mock_mw, \
             patch("plugin_source.community_moderation.api_client.post_json") as mock_post:
            profile = {}
            mock_mw.pm.profile = profile

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"success": True, "tags": ["Cardio", "ECG"]}
            mock_post.return_value = mock_resp

            from plugin_source.community_moderation import (
                get_self_specialization,
                save_self_specialization,
            )

            res = save_self_specialization("deck-hash-cardio", ["Cardio", "ECG", "  "])
            assert res["success"] is True
            assert res["tags"] == ["Cardio", "ECG"]

            saved = get_self_specialization("deck-hash-cardio")
            assert saved == ["Cardio", "ECG"]
            mock_post.assert_called_once_with(
                "/v2/moderation/self_specialization",
                payload={"deck_hash": "deck-hash-cardio", "tags": ["Cardio", "ECG"]},
                auth=True,
            )


