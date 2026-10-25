"""Mutation-driven tests for the highest-blast-radius export paths.

Targets the before-run survivors / uncovered lines in export_manager.py:

- ``_compute_media_reference_updates``: multi-field occurrences, nothing to
  update, overlapping mappings, missing notes.
- ``update_notetype_media_references``: normal update, no matching notetypes,
  empty mapping, notetypes with no references.
- ``_submit_deck_op``: success (with/without media), payload shape, error paths.

The existing test_export_manager.py already covers regexes, media validation,
batch boundaries, and the async/sync threshold.
"""

import json
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from export_manager import (
    _compute_media_reference_updates,
    update_notetype_media_references,
    _submit_deck_op,
)
from crowd_anki.representation.deck import Deck, DeckMetadata


def _enter(patchers):
    stack = ExitStack()
    for p in patchers:
        stack.enter_context(p)
    return stack


# ──────────────────────────────────────────────────────────────────────
# _compute_media_reference_updates
# ──────────────────────────────────────────────────────────────────────


class TestComputeMediaReferenceUpdates:
    def _col(self, note=None):
        col = MagicMock()
        col.media.dir.return_value = "/media"
        col.db.first.return_value = (note.id if note else 1,)
        col.get_note.return_value = note
        return col

    def test_empty_mapping(self):
        col = self._col()
        result = _compute_media_reference_updates(col, {}, [("old.mp3", "g1")])
        assert result == {"updates": [], "count": 0}

    def test_filename_in_multiple_fields(self):
        note = MagicMock()
        note.id = 10
        note.fields = ["[sound:old.mp3] and text", "[sound:old.mp3] again"]
        note.mod = 5
        col = self._col(note)
        with patch(
            "export_manager._filter_valid_filename_mapping",
            return_value={"old.mp3": "new.mp3"},
        ):
            result = _compute_media_reference_updates(
                col, {"old.mp3": "new.mp3"}, [("old.mp3", "g1")]
            )
        assert result["count"] == 1
        upd = result["updates"][0]
        assert upd["note_id"] == 10
        assert upd["note_guid"] == "g1"
        assert upd["fields"] == ["[sound:new.mp3] and text", "[sound:new.mp3] again"]
        assert upd["old_fields"] == [
            "[sound:old.mp3] and text",
            "[sound:old.mp3] again",
        ]

    def test_overlapping_mappings(self):
        """old->mid and mid->new must both apply in sequence."""
        note = MagicMock()
        note.id = 10
        note.fields = ["[sound:old.mp3]"]
        note.mod = 5
        col = self._col(note)
        mapping = {"old.mp3": "mid.mp3", "mid.mp3": "new.mp3"}
        with patch(
            "export_manager._filter_valid_filename_mapping", return_value=mapping
        ):
            result = _compute_media_reference_updates(col, mapping, [("old.mp3", "g1")])
        assert result["count"] == 1
        assert result["updates"][0]["fields"] == ["[sound:mid.mp3]"]

    def test_nothing_needs_updating(self):
        note = MagicMock()
        note.id = 10
        note.fields = ["no media here"]
        note.mod = 5
        col = self._col(note)
        with patch(
            "export_manager._filter_valid_filename_mapping",
            return_value={"old.mp3": "new.mp3"},
        ):
            result = _compute_media_reference_updates(
                col, {"old.mp3": "new.mp3"}, [("old.mp3", "g1")]
            )
        assert result == {"updates": [], "count": 0}

    def test_missing_note_row_skipped(self):
        col = self._col()
        col.db.first.return_value = None
        with patch(
            "export_manager._filter_valid_filename_mapping",
            return_value={"old.mp3": "new.mp3"},
        ):
            result = _compute_media_reference_updates(
                col, {"old.mp3": "new.mp3"}, [("old.mp3", "g1")]
            )
        assert result == {"updates": [], "count": 0}

    def test_duplicate_file_note_pairs_are_applied_once(self):
        note = MagicMock()
        note.id = 10
        note.fields = ["[sound:old.mp3]"]
        note.mod = 5
        col = self._col(note)
        with patch(
            "export_manager._filter_valid_filename_mapping",
            return_value={"old.mp3": "new.mp3"},
        ):
            result = _compute_media_reference_updates(
                col,
                {"old.mp3": "new.mp3"},
                [("old.mp3", "g1"), ("old.mp3", "g1")],
            )
        assert result["count"] == 1
        assert result["updates"][0]["old_filenames"] == ["old.mp3"]

    def test_unmapped_file_note_pair_is_ignored(self):
        note = MagicMock()
        note.id = 10
        note.fields = ["[sound:old.mp3]"]
        note.mod = 5
        col = self._col(note)
        with patch(
            "export_manager._filter_valid_filename_mapping",
            return_value={"other.mp3": "new.mp3"},
        ):
            result = _compute_media_reference_updates(
                col,
                {"other.mp3": "new.mp3"},
                [("old.mp3", "g1")],
            )
        assert result == {"updates": [], "count": 0}


# ──────────────────────────────────────────────────────────────────────
# update_notetype_media_references
# ──────────────────────────────────────────────────────────────────────


class TestUpdateNotetypeMediaReferences:
    def _notetype(
        self,
        name="Basic",
        css=".card { background: url(old.png) }",
        qfmt="{{Front}}",
        afmt="{{Back}}",
    ):
        return {
            "id": 1,
            "name": name,
            "css": css,
            "tmpls": [{"name": "Card 1", "qfmt": qfmt, "afmt": afmt, "ord": 0}],
            "crowdanki_uuid": "nt-1",
        }

    def test_empty_mapping_returns_zero(self, mw_mock):
        mw_mock.col.models.all.return_value = [self._notetype()]
        result = update_notetype_media_references({})
        assert result == 0
        mw_mock.col.models.save.assert_not_called()

    def test_updates_css_and_templates(self, mw_mock):
        mw_mock.col.models.all.return_value = [self._notetype()]
        result = update_notetype_media_references({"old.png": "new.png"})
        assert result == 1
        saved = mw_mock.col.models.save.call_args.args[0]
        assert "new.png" in saved["css"]
        assert saved["tmpls"][0]["qfmt"] == "{{Front}}"
        assert saved["tmpls"][0]["afmt"] == "{{Back}}"

    def test_updates_each_template_format_and_saves_only_changed_object(self, mw_mock):
        modified = self._notetype(
            css=".card {}", qfmt="[sound:old.mp3]", afmt="{{Back}} old.mp3"
        )
        untouched = self._notetype(name="Keep", css=".card {}", qfmt="{{Front}}")
        mw_mock.col.models.all.return_value = [modified, untouched]

        assert update_notetype_media_references({"old.mp3": "new.mp3"}) == 1

        mw_mock.col.models.save.assert_called_once_with(modified)
        assert modified["tmpls"][0]["qfmt"] == "[sound:new.mp3]"
        assert modified["tmpls"][0]["afmt"] == "{{Back}} new.mp3"
        assert untouched["tmpls"][0]["qfmt"] == "{{Front}}"

    def test_no_matching_notetypes(self, mw_mock):
        mw_mock.col.models.all.return_value = [self._notetype(css=".card {}", qfmt="x")]
        result = update_notetype_media_references({"old.png": "new.png"})
        assert result == 0
        mw_mock.col.models.save.assert_not_called()

    def test_notetype_without_css_or_templates_untouched(self, mw_mock):
        nt = {"id": 2, "name": "NoRefs", "css": "", "crowdanki_uuid": "nt-2"}
        mw_mock.col.models.all.return_value = [nt]
        result = update_notetype_media_references({"old.png": "new.png"})
        assert result == 0
        mw_mock.col.models.save.assert_not_called()

    def test_partial_match_only_modified_saved(self, mw_mock):
        modified = self._notetype(name="Mod", css="url(old.png)")
        untouched = self._notetype(name="Keep", css=".card {}")
        mw_mock.col.models.all.return_value = [modified, untouched]
        result = update_notetype_media_references({"old.png": "new.png"})
        assert result == 1
        # Only the modified notetype is saved.
        saved = mw_mock.col.models.save.call_args.args[0]
        assert saved["name"] == "Mod"


# ──────────────────────────────────────────────────────────────────────
# _submit_deck_op
# ──────────────────────────────────────────────────────────────────────


class TestSubmitDeckOp:
    def _setup(self, mw_mock):
        mw_mock.col.decks.name.return_value = "Some::Deck"
        deck = Deck(MagicMock(), {"name": "Deck", "crowdanki_uuid": "d1", "id": 1})
        deck.metadata = DeckMetadata(models={}, deck_configs={})
        deck.collection = mw_mock.col
        return deck

    def _patch_helpers(self, deck_hash="h1", token="tok", force=False):
        return (
            patch("export_manager.get_deck_hash_from_did", return_value=deck_hash),
            patch("export_manager.get_personal_tags", return_value=[]),
            patch("export_manager.get_local_deck_from_hash", return_value="LocalName"),
            patch("export_manager.get_maintainer_data", return_value=(token, force)),
        )

    def test_success_with_media_returns_upload_info(self, mw_mock):
        deck = self._setup(mw_mock)
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        with (
            _enter(self._patch_helpers()),
            patch("api_client.api_client.post_gzip", return_value=resp) as mock_post,
        ):
            result = _submit_deck_op(
                deck, 1, 5, "commit", [{"filename": "a.mp3"}], {"a.mp3": "/a.mp3"}
            )
        assert result is not None
        deck_hash, bulk_op_id, media_info, paths, silent = result
        assert deck_hash == "h1"
        assert media_info == [{"filename": "a.mp3"}]
        assert paths == {"a.mp3": "/a.mp3"}
        assert silent is False
        # Payload shape is asserted, not just that the mock was called.
        payload = mock_post.call_args.args[1]
        assert payload["remote_deck"] == "h1"
        assert payload["deck_path"] == "Some::Deck"
        assert payload["new_name"] == "LocalName"
        assert payload["rationale"] == 5
        assert payload["commit_text"] == "commit"
        assert payload["force_overwrite"] is False
        # deck is JSON-serialized.
        assert json.loads(payload["deck"])["name"] == "Deck"

    def test_success_without_media_returns_none(self, mw_mock):
        deck = self._setup(mw_mock)
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        with (
            _enter(self._patch_helpers()),
            patch("api_client.api_client.post_gzip", return_value=resp) as mock_post,
        ):
            result = _submit_deck_op(deck, 1, 5, "commit", [], {})
        assert result is None

    def test_force_overwrite_sets_other_rationale(self, mw_mock):
        deck = self._setup(mw_mock)
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        with (
            _enter(self._patch_helpers(token="tok", force=True)),
            patch("api_client.api_client.post_gzip", return_value=resp) as mock_post,
        ):
            _submit_deck_op(deck, 1, 5, "commit", [], {})
        payload = mock_post.call_args.args[1]
        assert payload["rationale"] == 10
        assert payload["commit_text"] == ""

    def test_missing_deck_hash_raises(self, mw_mock):
        deck = self._setup(mw_mock)
        with _enter(self._patch_helpers(deck_hash=None)):
            with pytest.raises(ValueError, match="deck hash"):
                _submit_deck_op(deck, 1, 5, "commit", [], {})

    def test_media_without_token_raises(self, mw_mock):
        deck = self._setup(mw_mock)
        with _enter(self._patch_helpers(token=None)):
            with pytest.raises(ValueError, match="Login required"):
                _submit_deck_op(deck, 1, 5, "commit", [{"filename": "a.mp3"}], {})

    def test_rationale_missing_raises(self, mw_mock):
        deck = self._setup(mw_mock)
        with _enter(self._patch_helpers()):
            with pytest.raises(ValueError, match="rationale"):
                _submit_deck_op(deck, 1, None, "commit", [], {})

    def test_notetype_error_response(self, mw_mock):
        deck = self._setup(mw_mock)
        resp = MagicMock()
        resp.text = "Notetype Error: abc-123"
        resp.status_code = 400
        with (
            _enter(self._patch_helpers()),
            patch("api_client.api_client.post_gzip", side_effect=_http_err(resp)),
        ):
            with pytest.raises(ValueError, match="Notetype Error"):
                _submit_deck_op(deck, 1, 5, "commit", [], {})

    def test_subdecks_not_allowed_response(self, mw_mock):
        deck = self._setup(mw_mock)
        resp = MagicMock()
        resp.text = "Subdecks are not allowed"
        resp.status_code = 400
        with (
            _enter(self._patch_helpers()),
            patch("api_client.api_client.post_gzip", side_effect=_http_err(resp)),
        ):
            with pytest.raises(ValueError, match="does not allow new subdecks"):
                _submit_deck_op(deck, 1, 5, "commit", [], {})

    def test_deck_does_not_exist_response(self, mw_mock):
        deck = self._setup(mw_mock)
        resp = MagicMock()
        resp.text = "Deck does not exist"
        resp.status_code = 400
        with (
            _enter(self._patch_helpers()),
            patch("api_client.api_client.post_gzip", side_effect=_http_err(resp)),
        ):
            with pytest.raises(ValueError, match="does not exist on the cloud"):
                _submit_deck_op(deck, 1, 5, "commit", [], {})

    def test_unknown_http_error(self, mw_mock):
        deck = self._setup(mw_mock)
        resp = MagicMock()
        resp.text = "Something else"
        resp.status_code = 500
        with (
            _enter(self._patch_helpers()),
            patch("api_client.api_client.post_gzip", side_effect=_http_err(resp)),
        ):
            with pytest.raises(RuntimeError, match="Submission failed"):
                _submit_deck_op(deck, 1, 5, "commit", [], {})


def _http_err(response):
    import requests

    e = requests.exceptions.HTTPError("http error")
    e.response = response
    return e
