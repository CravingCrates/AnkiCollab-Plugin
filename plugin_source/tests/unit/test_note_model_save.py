"""Mutation-hardening tests for ``NoteModel.save_to_collection`` /
``_update_existing_notetype``.

The pre-existing tests only asserted ``col.models.add.called or
col.models.update_dict.called`` and guarded the result behind ``if result:``,
so almost every branch decision in the persistence path survived mutation.
These tests pin the exact return values, the exact notetype dict handed to
``collection.models.update_dict`` and every branch of the change detector.
"""

from __future__ import annotations

import copy
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import create_mock_collection, make_notetype

from crowd_anki.representation.note_model import NoteModel, NotetypeSaveError
from crowd_anki.utils.constants import UUID_FIELD_NAME


def _nm(**kwargs):
    return NoteModel(make_notetype(**kwargs))


def _with_fetcher(existing):
    """Patch ``UuidFetcher`` so ``get_model`` returns ``existing``."""
    fetcher = MagicMock(name="UuidFetcher")
    fetcher.return_value.get_model.return_value = existing
    return patch("crowd_anki.representation.note_model.UuidFetcher", fetcher), fetcher


# ──────────────────────────────────────────────────────────────────────
# save_to_collection — dispatch
# ──────────────────────────────────────────────────────────────────────


class TestSaveToCollectionDispatch:
    def test_new_notetype_is_created_and_returns_no_field_mapping(self):
        col = create_mock_collection()
        nm = _nm(fields=["Front", "Back"], model_uuid="new-uuid")
        created = {"id": 5, "name": "Basic"}

        fetcher_patch, fetcher = _with_fetcher(None)
        with (
            fetcher_patch,
            patch.object(
                NoteModel, "_create_new_notetype", return_value=created
            ) as create,
        ):
            result = nm.save_to_collection(col)

        assert result == (created, None)
        assert fetcher.return_value.get_model.call_args.args == ("new-uuid",)
        create.assert_called_once_with(col)

    def test_existing_notetype_is_updated(self):
        col = create_mock_collection()
        nm = _nm(model_uuid="existing-uuid")
        existing = {"id": 1, "name": "Basic", "flds": []}
        sentinel = ({"id": 1}, [0])

        fetcher_patch, fetcher = _with_fetcher(existing)
        with (
            fetcher_patch,
            patch.object(
                NoteModel, "_update_existing_notetype", return_value=sentinel
            ) as update,
        ):
            result = nm.save_to_collection(col)

        assert result == sentinel
        assert fetcher.return_value.get_model.call_args.args == ("existing-uuid",)
        assert update.call_args.args == (col, existing)

    def test_a_lookup_failure_is_fatal(self):
        col = create_mock_collection()
        nm = _nm(model_uuid="boom-uuid")
        fetcher = MagicMock()
        fetcher.return_value.get_model.side_effect = RuntimeError("db gone")

        with patch("crowd_anki.representation.note_model.UuidFetcher", fetcher):
            with pytest.raises(NotetypeSaveError):
                nm.save_to_collection(col)

    def test_an_update_failure_is_fatal(self):
        col = create_mock_collection()
        nm = _nm(model_uuid="boom-uuid")
        fetcher_patch, _fetcher = _with_fetcher({"id": 1, "flds": []})

        with (
            fetcher_patch,
            patch.object(
                NoteModel,
                "_update_existing_notetype",
                side_effect=ValueError("nope"),
            ),
        ):
            with pytest.raises(NotetypeSaveError):
                nm.save_to_collection(col)

    def test_a_creation_failure_is_fatal(self):
        col = create_mock_collection()
        nm = _nm(model_uuid="boom-uuid")
        fetcher_patch, _fetcher = _with_fetcher(None)

        with (
            fetcher_patch,
            patch.object(
                NoteModel, "_create_new_notetype", side_effect=ValueError("nope")
            ),
        ):
            with pytest.raises(NotetypeSaveError):
                nm.save_to_collection(col)

    def test_a_creation_returning_none_is_fatal(self):
        col = create_mock_collection()
        nm = _nm(model_uuid="boom-uuid")
        fetcher_patch, _fetcher = _with_fetcher(None)

        with (
            fetcher_patch,
            patch.object(NoteModel, "_create_new_notetype", return_value=None),
        ):
            with pytest.raises(NotetypeSaveError):
                nm.save_to_collection(col)


# ──────────────────────────────────────────────────────────────────────
# _update_existing_notetype
# ──────────────────────────────────────────────────────────────────────


class TestUpdateExistingNotetypeNoChanges:
    def test_returns_trivial_mapping_without_touching_the_collection(self):
        col = create_mock_collection()
        nt = make_notetype(fields=["Front", "Back"], model_id=1)
        existing = copy.deepcopy(nt)
        nm = NoteModel(copy.deepcopy(nt))

        result = nm._update_existing_notetype(col, existing)

        assert result == (None, [0, 1])
        col.models.update_dict.assert_not_called()

    def test_trivial_mapping_spans_all_existing_fields(self):
        col = create_mock_collection()
        nt = make_notetype(fields=["A", "B", "C", "D"], model_id=1)
        nm = NoteModel(copy.deepcopy(nt))

        _updated, mapping = nm._update_existing_notetype(col, copy.deepcopy(nt))

        assert mapping == [0, 1, 2, 3]


class TestUpdateExistingNotetypeTemplatesAndCss:
    def _existing(self, **overrides):
        nt = make_notetype(
            name="Basic",
            fields=["Front", "Back"],
            css=".card { color: black; }",
            model_id=1,
        )
        nt.update(overrides)
        return nt

    def test_css_is_overwritten_for_regular_notetypes(self):
        col = create_mock_collection()
        existing = self._existing()
        incoming = copy.deepcopy(existing)
        incoming["css"] = ".card { color: red; }"
        incoming["name"] = "Basic renamed"
        nm = NoteModel(incoming)

        updated, mapping = nm._update_existing_notetype(col, existing)

        assert updated is not None
        assert updated["css"] == ".card { color: red; }"
        assert updated["name"] == "Basic renamed"
        assert updated["id"] == existing["id"]
        col.models.update_dict.assert_called_once_with(updated)
        assert mapping == [0, 1]

    def test_templates_are_overwritten_for_regular_notetypes(self):
        col = create_mock_collection()
        existing = self._existing()
        incoming = copy.deepcopy(existing)
        incoming["tmpls"] = [
            {
                "name": "Card 1",
                "qfmt": "{{Back}}",
                "afmt": "{{Front}}",
                "ord": 0,
            }
        ]
        nm = NoteModel(incoming)

        updated, _mapping = nm._update_existing_notetype(col, existing)

        assert updated["tmpls"][0]["qfmt"] == "{{Back}}"

    def test_field_update_preserves_remote_order_and_local_properties(self):
        col = create_mock_collection()
        existing = self._existing()
        existing["flds"][0]["font"] = "Comic Sans"
        existing["flds"][0]["size"] = 30
        incoming = copy.deepcopy(existing)
        incoming["flds"] = [
            {"name": "Back", "ord": 0, "font": "Arial", "size": 20},
            {"name": "Front", "ord": 1, "font": "Arial", "size": 20},
            {"name": "Hint", "ord": 2, "font": "Arial", "size": 20},
        ]

        updated, mapping = NoteModel(incoming)._update_existing_notetype(col, existing)

        assert [field["name"] for field in updated["flds"]] == [
            "Back",
            "Front",
            "Hint",
        ]
        assert [field["ord"] for field in updated["flds"]] == [0, 1, 2]
        assert updated["flds"][1]["font"] == "Comic Sans"
        assert updated["flds"][1]["size"] == 30
        assert mapping == [1, 0, None]
        col.models.update_dict.assert_called_once_with(updated)

    def test_absent_tmpls_and_css_are_left_alone(self):
        col = create_mock_collection()
        existing = self._existing()
        incoming = copy.deepcopy(existing)
        incoming["name"] = "Renamed"
        del incoming["tmpls"]
        del incoming["css"]
        nm = NoteModel(incoming)

        updated, _mapping = nm._update_existing_notetype(col, existing)

        assert updated["tmpls"] == existing["tmpls"]
        assert updated["css"] == existing["css"]

    def test_projektanki_templates_and_css_are_preserved(self):
        col = create_mock_collection()
        existing = self._existing(name="ProjektAnki Basic")
        incoming = copy.deepcopy(existing)
        # A field change forces the update, but templates/CSS must survive.
        incoming["flds"] = list(incoming["flds"]) + [
            {
                "name": "Hint",
                "ord": 2,
                "sticky": False,
                "rtl": False,
                "font": "Arial",
                "size": 20,
            }
        ]
        incoming["css"] = ".card { color: red; }"
        incoming["tmpls"] = [
            {"name": "Card 1", "qfmt": "CHANGED", "afmt": "CHANGED", "ord": 0}
        ]
        nm = NoteModel(incoming)

        updated, _mapping = nm._update_existing_notetype(col, existing)

        assert updated["css"] == ".card { color: black; }"
        assert updated["tmpls"][0]["qfmt"] == "{{Front}}"

    def test_projektanki_ignores_cosmetic_css_only_changes(self):
        """With templates preserved, a CSS-only change is not a change."""
        col = create_mock_collection()
        existing = self._existing(name="ProjektAnki Basic")
        incoming = copy.deepcopy(existing)
        incoming["css"] = ".card { color: red; }"
        nm = NoteModel(incoming)

        assert nm._update_existing_notetype(col, existing) == (None, [0, 1])
        col.models.update_dict.assert_not_called()

    def test_name_match_for_projektanki_is_case_insensitive(self):
        col = create_mock_collection()
        existing = self._existing(name="PROJEKTANKI deck")
        incoming = copy.deepcopy(existing)
        incoming["css"] = ".card { color: red; }"
        nm = NoteModel(incoming)

        # CSS change alone is ignored because the name matches projektanki.
        assert nm._update_existing_notetype(col, existing) == (None, [0, 1])


class TestUpdateExistingNotetypeFields:
    def test_changed_fields_are_merged_and_mapped(self):
        col = create_mock_collection()
        existing = make_notetype(fields=["Front", "Back"], model_id=1)
        incoming = make_notetype(fields=["Back", "Front", "Extra"], model_id=1)
        nm = NoteModel(incoming)

        updated, mapping = nm._update_existing_notetype(col, existing)

        assert updated is not None
        # Remote order wins and the local-only fields stay behind.
        assert [f["name"] for f in updated["flds"]] == ["Back", "Front", "Extra"]
        assert mapping == [1, 0, None]
        col.models.update_dict.assert_called_once_with(updated)

    def test_unchanged_fields_still_produce_a_trivial_mapping(self):
        col = create_mock_collection()
        existing = make_notetype(fields=["Front", "Back"], model_id=1)
        incoming = copy.deepcopy(existing)
        incoming["name"] = "Renamed"  # forces an update, fields untouched
        nm = NoteModel(incoming)

        _updated, mapping = nm._update_existing_notetype(col, existing)

        assert mapping == [0, 1]

    def test_self_id_is_refreshed_from_the_updated_notetype(self):
        col = create_mock_collection()
        existing = make_notetype(fields=["Front", "Back"], model_id=1)
        incoming = copy.deepcopy(existing)
        incoming["name"] = "Renamed"
        incoming["id"] = 999  # must NOT be applied
        nm = NoteModel(incoming)

        updated, _mapping = nm._update_existing_notetype(col, existing)

        assert updated["id"] == existing["id"]
        assert nm.anki_dict["id"] == existing["id"]

    def test_unchanged_notetype_uses_the_local_id_not_the_payload_id(self):
        """A no-op merge must still expose the *local* notetype id.

        The payload's id is the sender's local id. If it survives into note
        processing, notes are written with a mid that does not exist (or, worse,
        belongs to a different notetype) in this collection.
        """
        col = create_mock_collection()
        existing = make_notetype(fields=["Front", "Back"], model_id=7)
        incoming = copy.deepcopy(existing)
        incoming["id"] = 999_999
        nm = NoteModel(incoming)

        result = nm._update_existing_notetype(col, existing)

        assert result == (None, [0, 1])
        assert nm.anki_dict["id"] == 7
        assert nm.anki_dict["flds"] == existing["flds"]
        col.models.update_dict.assert_not_called()

    def test_renamed_notetype_records_the_actual_local_name(self):
        """Anki suffixes a name on collision; the next import must not rename again."""
        col = create_mock_collection()
        existing = make_notetype(fields=["Front", "Back"], model_id=1)
        incoming = copy.deepcopy(existing)
        incoming["name"] = "Renamed"

        def collide(notetype):
            notetype["name"] = "Renamed-abcde"

        col.models.update_dict.side_effect = collide
        nm = NoteModel(incoming)

        nm._update_existing_notetype(col, existing)

        assert nm.anki_dict["name"] == "Renamed-abcde"
        # Config/protected-field lookups still use the server-side name.
        assert nm.anki_dict["original_name"] == "Renamed"

    def test_extra_keys_are_copied_but_id_fields_templates_css_are_not(self):
        col = create_mock_collection()
        existing = make_notetype(fields=["Front", "Back"], model_id=1)
        existing["custom_key"] = "old"
        existing["mod"] = 111
        incoming = copy.deepcopy(existing)
        incoming["name"] = "Renamed"
        incoming["custom_key"] = "new"
        incoming["mod"] = 222
        incoming["type"] = 3
        nm = NoteModel(incoming)

        updated, _mapping = nm._update_existing_notetype(col, existing)

        assert updated["custom_key"] == "new"
        assert updated["mod"] == 222
        assert updated["type"] == 3
        assert updated["id"] == existing["id"]

    def test_a_rejected_update_is_fatal(self):
        col = create_mock_collection()
        existing = make_notetype(fields=["Front", "Back"], model_id=1)
        incoming = copy.deepcopy(existing)
        incoming["name"] = "Renamed"
        nm = NoteModel(incoming)
        col.models.update_dict.side_effect = ValueError("locked")

        with pytest.raises(NotetypeSaveError):
            nm._update_existing_notetype(col, existing)


class TestNoteValueRemap:
    """Anki stores note values positionally; update_dict() does not move them.

    Whenever the merged field order differs from the local order the existing
    notes of that notetype must be remapped by name, or every note outside the
    imported deck silently shows its content under the wrong field names.
    """

    def test_appended_field_does_not_remap(self):
        col = create_mock_collection()
        col.db.list.return_value = [10]
        existing = make_notetype(fields=["A", "B"], model_id=1)
        incoming = make_notetype(fields=["A", "B", "C"], model_id=1)

        NoteModel(incoming)._update_existing_notetype(col, existing)

        col.models.change_notetype_of_notes.assert_not_called()

    def test_reordered_fields_remap_all_notes_by_name(self):
        col = create_mock_collection()
        col.db.list.return_value = [10, 11]
        col.db.scalar.return_value = 987
        existing = make_notetype(fields=["A", "B", "C"], model_id=1)
        incoming = make_notetype(fields=["C", "A", "B"], model_id=1)

        with patch(
            "crowd_anki.representation.note_model.ChangeNotetypeRequest"
        ) as request_type:
            NoteModel(incoming)._update_existing_notetype(col, existing)

        kwargs = request_type.call_args.kwargs
        assert kwargs["note_ids"] == [10, 11]
        assert kwargs["new_fields"] == [2, 0, 1]
        assert kwargs["old_notetype_id"] == 1
        assert kwargs["new_notetype_id"] == 1
        assert kwargs["current_schema"] == 987
        col.models.change_notetype_of_notes.assert_called_once_with(
            request_type.return_value
        )

    def test_inserted_field_is_empty_and_values_shift_correctly(self):
        col = create_mock_collection()
        col.db.list.return_value = [10]
        existing = make_notetype(fields=["A", "B", "Local"], model_id=1)
        incoming = make_notetype(fields=["A", "New", "B"], model_id=1)

        with patch(
            "crowd_anki.representation.note_model.ChangeNotetypeRequest"
        ) as request_type:
            NoteModel(incoming)._update_existing_notetype(col, existing)

        # A<-0, New<-none, B<-1, Local kept at the end
        assert request_type.call_args.kwargs["new_fields"] == [0, -1, 1, 2]

    def test_notes_are_remapped_in_chunks(self):
        col = create_mock_collection()
        col.db.list.return_value = list(range(2500))
        existing = make_notetype(fields=["A", "B"], model_id=1)
        incoming = make_notetype(fields=["B", "A"], model_id=1)

        with patch(
            "crowd_anki.representation.note_model.ChangeNotetypeRequest"
        ) as request_type:
            NoteModel(incoming)._update_existing_notetype(col, existing)

        sizes = [len(call.kwargs["note_ids"]) for call in request_type.call_args_list]
        assert sizes == [1000, 1000, 500]

    def test_a_remap_failure_is_fatal(self):
        col = create_mock_collection()
        col.db.list.return_value = [10]
        col.models.change_notetype_of_notes.side_effect = RuntimeError("locked")
        existing = make_notetype(fields=["A", "B"], model_id=1)
        incoming = make_notetype(fields=["B", "A"], model_id=1)

        with pytest.raises(NotetypeSaveError):
            NoteModel(incoming)._update_existing_notetype(col, existing)

    def test_local_only_field_names_are_captured(self):
        col = create_mock_collection()
        existing = make_notetype(fields=["A", "B", "Mein Schatz"], model_id=1)
        incoming = make_notetype(fields=["A", "B"], model_id=1)
        nm = NoteModel(incoming)

        nm._update_existing_notetype(col, existing)

        assert nm.local_only_field_names == ["Mein Schatz"]

    def test_local_only_field_names_respect_case_insensitive_names(self):
        col = create_mock_collection()
        existing = make_notetype(fields=["A", "b"], model_id=1)
        incoming = make_notetype(fields=["a", "B"], model_id=1)
        nm = NoteModel(incoming)

        nm._update_existing_notetype(col, existing)

        assert nm.local_only_field_names == []


class TestCreateNewNotetype:
    def test_new_model_is_added_and_id_is_reassigned(self):
        col = create_mock_collection()
        nm = _nm(name="Fresh", fields=["Front"], model_uuid="fresh-uuid")
        fetcher = MagicMock()
        fetcher.return_value.get_model.return_value = None
        seen = {}

        def _add(model):
            # Record the state at call time.  A backend that does not stamp the
            # ID forces the ``by_name`` fallback below.
            seen["id"] = model["id"]
            seen["name"] = model["name"]
            seen["uuid"] = model[UUID_FIELD_NAME]

        with (
            patch("crowd_anki.representation.note_model.UuidFetcher", fetcher),
            patch.object(
                col.models,
                "new",
                return_value={
                    "id": 0,
                    "name": "Fresh",
                    "flds": [],
                    "tmpls": [],
                    "css": "",
                    "crowdanki_uuid": "",
                },
            ),
            patch.object(col.models, "add", side_effect=_add),
            patch.object(
                col.models,
                "by_name",
                return_value={"id": 42, "name": "Fresh"},
            ) as by_name,
        ):
            result = nm._create_new_notetype(col)
            by_name.assert_called_once_with("Fresh")

        assert result is nm.anki_dict
        assert seen == {"id": 0, "name": "Fresh", "uuid": "fresh-uuid"}
        # The freshly assigned ID is read back so downstream notes can use it.
        assert nm.anki_dict["id"] == 42

    def test_existing_model_is_reused(self):
        col = create_mock_collection()
        nm = _nm(name="Existing", model_uuid="exist-uuid")
        existing = make_notetype(name="Existing", model_id=7, model_uuid="exist-uuid")
        fetcher = MagicMock()
        fetcher.return_value.get_model.return_value = existing

        with patch("crowd_anki.representation.note_model.UuidFetcher", fetcher):
            result = nm._create_new_notetype(col)

        assert result is existing
        col.models.add.assert_not_called()

    def test_a_failed_creation_returns_nothing(self):
        col = create_mock_collection()
        nm = _nm(model_uuid="x")
        fetcher = MagicMock()
        fetcher.return_value.get_model.side_effect = RuntimeError("nope")

        with patch("crowd_anki.representation.note_model.UuidFetcher", fetcher):
            assert nm._create_new_notetype(col) is None
