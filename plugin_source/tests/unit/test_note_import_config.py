"""Mutation-hardening tests for ``Note.handle_import_config_changes``.

The existing tests cover the two happy protection strategies.  These tests
target the remaining branches: bailing out on malformed notes, the old-model
lookup fallbacks, field padding/truncation bookkeeping, the tag-driven
protection strategies ("All"/"Tags"/named fields), personal-tag merging and
the optional-tag filter.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from tests.conftest import make_note_dict, make_notetype, MockAnkiNote

from crowd_anki.representation.note import Note
from crowd_anki.representation.note_model import NoteModel
from var_defs import PREFIX_OPTIONAL_TAGS, PREFIX_PROTECTED_FIELDS


@pytest.fixture
def note_log(caplog):
    """Capture ``ankicollab.note`` warnings without changing production logging.

    The add-on deliberately keeps ``propagate = False`` so records do not get
    duplicated into Sentry's root logging integration. Only tests opt in.
    """
    logger = logging.getLogger("ankicollab.note")
    previous = logger.propagate
    logger.propagate = True
    try:
        yield caplog
    finally:
        logger.propagate = previous


def _config(
    personal=(), optional_tags=None, has_optional_tags=False, personal_tags=None
):
    """A MagicMock ImportConfig with the attributes the code reads."""
    cfg = MagicMock()
    cfg.is_personal_field = MagicMock(
        side_effect=lambda model_name, field_name: (model_name, field_name) in personal
    )
    cfg.personal_tags = list(personal_tags or [])
    cfg.has_optional_tags = has_optional_tags
    cfg.optional_tags = list(optional_tags or [])
    return cfg


def _note(
    remote_fields,
    remote_tags=(),
    old_fields=None,
    old_tags=(),
    notetype=None,
    old_notetype=None,
):
    """Build a Note whose remote side is ``remote_fields`` and local side is ``old_fields``."""
    note = Note.from_json(
        make_note_dict(fields=list(remote_fields), tags=list(remote_tags))
    )
    mock = MockAnkiNote()
    mock.fields = list(old_fields if old_fields is not None else remote_fields)
    mock.tags = list(old_tags)
    if old_notetype is None:
        old_notetype = notetype if notetype is not None else make_notetype()
    mock.note_type = MagicMock(return_value=old_notetype)
    note.anki_object = mock
    return note


def _nm(notetype):
    return NoteModel(notetype)


# ──────────────────────────────────────────────────────────────────────
# Bail-out guards
# ──────────────────────────────────────────────────────────────────────


class TestBailOutGuards:
    def test_missing_anki_object_dict_returns_immediately(self):
        note = Note.from_json(make_note_dict())
        del note.anki_object_dict
        note.anki_object = MockAnkiNote()
        # Must not raise.
        note.handle_import_config_changes(_config(), _nm(make_notetype()))

    def test_empty_anki_object_dict_returns_immediately(self):
        note = Note.from_json(make_note_dict())
        note.anki_object_dict = {}
        note.anki_object = MockAnkiNote()
        note.handle_import_config_changes(_config(), _nm(make_notetype()))
        assert note.anki_object_dict == {}

    def test_local_note_without_fields_returns_immediately(self):
        note = Note.from_json(make_note_dict(fields=["new a", "new b"]))
        local = MockAnkiNote()
        del local.fields
        note.anki_object = local
        note.handle_import_config_changes(_config(), _nm(make_notetype()))
        # Untouched: the guard fires before any field work.
        assert note.anki_object_dict["fields"] == ["new a", "new b"]


class _LegacyNote:
    """A pre-2.1.46 style local note: only ``model()`` is available."""

    def __init__(self, fields, tags=(), model=None, guid="legacy-guid"):
        self.fields = list(fields)
        self.tags = list(tags)
        self.guid = guid
        self._old_model = model or {}

    def model(self):
        return self._old_model


class TestOldModelLookup:
    def test_old_model_is_read_via_note_type(self):
        nt = make_notetype(fields=["Front", "Back"])
        old_nt = make_notetype(fields=["Front", "Back"], model_id=99)
        note = _note(
            ["new front", "new back"],
            old_fields=["old front", "old back"],
            notetype=nt,
            old_notetype=old_nt,
        )
        cfg = _config(personal=[("Basic", "Front")])

        note.handle_import_config_changes(cfg, _nm(nt))

        # Name-based lookup found "Front" in the old model and restored it.
        assert note.anki_object_dict["fields"][0] == "old front"

    def test_old_model_falls_back_to_model_attribute(self):
        nt = make_notetype(fields=["Front", "Back"])
        old_nt = make_notetype(fields=["Front", "Back"])
        note = _note(["new front", "new back"], notetype=nt)
        note.anki_object = _LegacyNote(["old front", "old back"], model=old_nt)
        cfg = _config(personal=[("Basic", "Front")])

        note.handle_import_config_changes(cfg, _nm(nt))

        assert note.anki_object_dict["fields"][0] == "old front"

    def test_a_broken_old_model_does_not_break_the_import(self):
        nt = make_notetype(fields=["Front", "Back"])
        note = _note(
            ["new front", "new back"],
            old_fields=["old front", "old back"],
            notetype=nt,
        )
        note.anki_object.note_type = MagicMock(side_effect=RuntimeError("no model"))
        cfg = _config(personal=[("Basic", "Front")])

        note.handle_import_config_changes(cfg, _nm(nt))

        # Without a usable old model the protected field keeps the remote value.
        assert note.anki_object_dict["fields"][0] == "new front"

    def test_old_model_without_flds_is_ignored(self):
        nt = make_notetype(fields=["Front", "Back"])
        note = _note(
            ["new front", "new back"],
            old_fields=["old front", "old back"],
            notetype=nt,
            old_notetype={"name": "NoFields"},
        )
        note.handle_import_config_changes(
            _config(personal=[("Basic", "Front")]), _nm(nt)
        )
        assert note.anki_object_dict["fields"][0] == "new front"


class TestProtectedFieldNameResolution:
    def test_original_name_is_used_for_the_personal_field_lookup(self):
        nt = make_notetype(name="Renamed", fields=["Front", "Back"])
        nt["original_name"] = "Original"
        note = _note(
            ["new front", "new back"],
            old_fields=["old front", "old back"],
            notetype=nt,
        )
        cfg = _config(personal=[("Original", "Front")])

        note.handle_import_config_changes(cfg, _nm(nt))

        assert note.anki_object_dict["fields"][0] == "old front"

    def test_only_fields_below_the_shared_length_are_considered(self):
        """``max_fields`` is the shorter of remote fields and notetype fields."""
        nt = make_notetype(fields=["A", "B", "C"])
        note = _note(
            ["new a", "new b"],  # only 2 remote fields
            old_fields=["old a", "old b"],
            notetype=nt,
        )
        cfg = _config(personal=[("Basic", "A"), ("Basic", "C")])

        note.handle_import_config_changes(cfg, _nm(nt))

        # A is protected; C is beyond max_fields so it is never consulted.
        called_fields = [c.args[1] for c in cfg.is_personal_field.call_args_list]
        assert called_fields == ["A", "B"]
        assert note.anki_object_dict["fields"][0] == "old a"


# ──────────────────────────────────────────────────────────────────────
# Field padding / truncation / local-only fields
# ──────────────────────────────────────────────────────────────────────


class TestFieldCountReconciliation:
    def test_extra_remote_fields_are_truncated(self):
        nt = make_notetype(fields=["A", "B"])
        note = _note(
            ["new a", "new b", "new c"],
            old_fields=["old a", "old b"],
            notetype=nt,
        )
        # Skip the "append local extras" path by using a local note with the
        # same number of fields as the remote payload.
        note.anki_object.fields = ["old a", "old b", "old c"]
        note.handle_import_config_changes(_config(), _nm(nt))

        # Anki rejects a note whose field count does not match its notetype.
        assert note.anki_object_dict["fields"] == ["new a", "new b"]

    def test_missing_remote_fields_are_padded_with_empty_strings(self):
        nt = make_notetype(fields=["A", "B", "C"])
        note = _note(["new a"], old_fields=["old a"], notetype=nt)

        note.handle_import_config_changes(_config(), _nm(nt))

        assert note.anki_object_dict["fields"] == ["new a", "", ""]

    def test_local_only_fields_are_kept_by_name(self):
        # The notetype has room for the extra local fields.
        nt = make_notetype(fields=["A", "B", "C", "D"])
        note = _note(
            ["new a", "new b"],
            old_fields=["old a", "old b", "old c", "mine"],
            notetype=nt,
        )
        nm = _nm(nt)
        nm.local_only_field_names = ["C", "D"]
        note.handle_import_config_changes(_config(), nm)

        assert note.anki_object_dict["fields"] == [
            "new a",
            "new b",
            "old c",
            "mine",
        ]

    def test_local_only_values_are_found_by_name_in_the_notes_notetype(self):
        nt = make_notetype(fields=["A", "B", "C", "D"])  # merged layout
        old_nt = make_notetype(fields=["D", "C", "B", "A"])  # note's own layout
        note = _note(
            ["new a", "new b"],
            old_fields=["mine d", "mine c", "old b", "old a"],
            notetype=nt,
            old_notetype=old_nt,
        )
        nm = _nm(nt)
        nm.local_only_field_names = ["C", "D"]
        note.handle_import_config_changes(_config(), nm)

        assert note.anki_object_dict["fields"] == [
            "new a",
            "new b",
            "mine c",
            "mine d",
        ]

    def test_local_only_field_missing_from_the_note_is_empty(self):
        nt = make_notetype(fields=["A", "B", "C", "D"])
        old_nt = make_notetype(fields=["A", "B"])  # note never had C/D
        note = _note(
            ["new a", "new b"],
            old_fields=["old a", "old b"],
            notetype=nt,
            old_notetype=old_nt,
        )
        nm = _nm(nt)
        nm.local_only_field_names = ["C", "D"]
        note.handle_import_config_changes(_config(), nm)

        assert note.anki_object_dict["fields"] == ["new a", "new b", "", ""]

    def test_local_value_beyond_the_notes_fields_is_left_empty(self, note_log):
        nt = make_notetype(fields=["A", "B", "C", "D"])
        note = _note(
            ["new a", "new b"],
            old_fields=["old a", "old b"],  # fewer values than the notetype
            notetype=nt,
        )
        nm = _nm(nt)
        nm.local_only_field_names = ["C", "D"]
        with note_log.at_level(logging.WARNING):
            note.handle_import_config_changes(_config(), nm)

        assert note.anki_object_dict["fields"] == ["new a", "new b", "", ""]
        # One up-front warning covers every out-of-range position.
        assert "only the values present can be preserved" in note_log.text

    def test_local_only_field_is_found_when_its_casing_differs(self):
        # The note's own notetype spells the field "extra", the merged one "Extra".
        nt = make_notetype(fields=["A", "B", "Extra"])
        old_nt = make_notetype(fields=["a", "b", "extra"], model_id=99)
        note = _note(
            ["new a", "new b"],
            old_fields=["old a", "old b", "mine"],
            notetype=nt,
            old_notetype=old_nt,
        )
        nm = _nm(nt)
        nm.local_only_field_names = ["Extra"]
        note.handle_import_config_changes(_config(), nm)

        assert note.anki_object_dict["fields"] == ["new a", "new b", "mine"]

    def test_exact_field_name_wins_over_a_case_variant(self):
        # "extra" precedes "Extra" in the note's notetype; the exact match must win.
        nt = make_notetype(fields=["A", "B", "Extra"])
        old_nt = make_notetype(fields=["A", "B", "extra", "Extra"], model_id=99)
        note = _note(
            ["new a", "new b"],
            old_fields=["old a", "old b", "lower", "exact"],
            notetype=nt,
            old_notetype=old_nt,
        )
        nm = _nm(nt)
        nm.local_only_field_names = ["Extra"]
        note.handle_import_config_changes(_config(), nm)

        assert note.anki_object_dict["fields"] == ["new a", "new b", "exact"]

    def test_ambiguous_case_variant_is_not_used(self):
        # Two local fields differing only in casing: neither may be guessed.
        nt = make_notetype(fields=["A", "B", "EXTRA"])
        old_nt = make_notetype(fields=["A", "B", "extra", "Extra"], model_id=99)
        note = _note(
            ["new a", "new b"],
            old_fields=["old a", "old b", "lower", "exact"],
            notetype=nt,
            old_notetype=old_nt,
        )
        nm = _nm(nt)
        nm.local_only_field_names = ["EXTRA"]
        note.handle_import_config_changes(_config(), nm)

        assert note.anki_object_dict["fields"] == ["new a", "new b", ""]

    def test_missing_local_only_field_is_logged(self, note_log):
        nt = make_notetype(fields=["A", "B", "C"])
        old_nt = make_notetype(fields=["A", "B"], model_id=99)
        note = _note(
            ["new a", "new b"],
            old_fields=["old a", "old b"],
            notetype=nt,
            old_notetype=old_nt,
        )
        nm = _nm(nt)
        nm.local_only_field_names = ["C"]
        with note_log.at_level(logging.WARNING):
            note.handle_import_config_changes(_config(), nm)

        assert note.anki_object_dict["fields"] == ["new a", "new b", ""]
        assert "'C' has no unambiguous local counterpart" in note_log.text

    def test_out_of_range_positions_are_reported_once(self, note_log):
        """The note/notetype mismatch is reported once, not once per field."""
        nt = make_notetype(fields=["A", "B", "C", "D"])
        note = _note(["new a", "new b"], old_fields=["old a", "old b"], notetype=nt)
        nm = _nm(nt)
        nm.local_only_field_names = ["C", "D"]
        with note_log.at_level(logging.WARNING):
            note.handle_import_config_changes(_config(), nm)

        assert note_log.text.count("only the values present can be preserved") == 1
        assert "no unambiguous local counterpart" not in note_log.text

    def test_senders_own_extra_fields_never_overwrite_local_ones(self):
        """A contributor's local fields arrive in the note JSON; ours must win."""
        nt = make_notetype(fields=["A", "B", "C", "D"])
        note = _note(
            ["new a", "new b", "their c", "their d"],
            old_fields=["old a", "old b", "mine c", "mine d"],
            notetype=nt,
        )
        nm = _nm(nt)
        nm.local_only_field_names = ["C", "D"]
        note.handle_import_config_changes(_config(), nm)

        assert note.anki_object_dict["fields"] == [
            "new a",
            "new b",
            "mine c",
            "mine d",
        ]

    def test_sender_extras_do_not_shift_local_only_values(self):
        """Remote note may carry fewer extras than we have; ours stay aligned."""
        nt = make_notetype(fields=["A", "B", "C", "D"])
        note = _note(
            ["new a", "new b", "their c"],
            old_fields=["old a", "old b", "mine c", "mine d"],
            notetype=nt,
        )
        nm = _nm(nt)
        nm.local_only_field_names = ["C", "D"]
        note.handle_import_config_changes(_config(), nm)

        assert note.anki_object_dict["fields"] == [
            "new a",
            "new b",
            "mine c",
            "mine d",
        ]


# ──────────────────────────────────────────────────────────────────────
# Tag-driven protection
# ──────────────────────────────────────────────────────────────────────


class TestTagDrivenProtection:
    def _protected_tag(self, name):
        return f"{PREFIX_PROTECTED_FIELDS}::{name}"

    def test_protect_tag_is_copied_into_the_remote_note(self):
        nt = make_notetype(fields=["A", "B"])
        tag = self._protected_tag("A")
        note = _note(
            ["new a", "new b"],
            remote_tags=["keep"],
            old_fields=["old a", "old b"],
            old_tags=[tag],
            notetype=nt,
        )
        note.handle_import_config_changes(_config(), _nm(nt))

        assert tag in note.anki_object_dict["tags"]
        assert "keep" in note.anki_object_dict["tags"]

    def test_named_field_protection_preserves_local_content(self):
        nt = make_notetype(fields=["A", "B"])
        note = _note(
            ["new a", "new b"],
            old_fields=["old a", "old b"],
            old_tags=[self._protected_tag("A")],
            notetype=nt,
        )
        note.handle_import_config_changes(_config(), _nm(nt))

        assert note.anki_object_dict["fields"] == ["old a", "new b"]

    def test_named_field_protection_accepts_underscore_spacing(self):
        nt = make_notetype(fields=["Extra Info", "B"])
        note = _note(
            ["new info", "new b"],
            old_fields=["old info", "old b"],
            old_tags=[self._protected_tag("Extra_Info")],
            notetype=nt,
        )
        note.handle_import_config_changes(_config(), _nm(nt))

        assert note.anki_object_dict["fields"] == ["old info", "new b"]

    def test_named_field_protection_works_for_field_index_zero(self):
        """Index 0 must not be mistaken for "not found" when the name has an underscore."""
        nt = make_notetype(fields=["My_Field", "B"])
        note = _note(
            ["new a", "new b"],
            old_fields=["old a", "old b"],
            old_tags=[self._protected_tag("My_Field")],
            notetype=nt,
        )
        note.handle_import_config_changes(_config(), _nm(nt))

        assert note.anki_object_dict["fields"] == ["old a", "new b"]

    def test_named_field_protection_uses_the_field_name(self):
        nt = make_notetype(fields=["A", "B"])
        note = _note(
            ["new a", "new b"],
            old_fields=["old a", "old b"],
            old_tags=[self._protected_tag("B")],
            notetype=nt,
        )
        note.handle_import_config_changes(_config(), _nm(nt))

        assert note.anki_object_dict["fields"] == ["new a", "old b"]

    def test_named_field_protection_ignores_field_name_casing(self):
        """A remote case-only rename must not silently disable protection."""
        nt = make_notetype(fields=["front", "Back"])  # merged/remote casing
        note = _note(
            ["new front", "new back"],
            old_fields=["my front", "old back"],
            old_tags=[self._protected_tag("Front")],  # user's old casing
            notetype=nt,
        )
        note.handle_import_config_changes(_config(), _nm(nt))

        assert note.anki_object_dict["fields"] == ["my front", "new back"]

    def test_a_field_missing_from_the_local_note_keeps_the_remote_value(self):
        nt = make_notetype(fields=["A", "B"])
        old_nt = make_notetype(fields=["B"])  # local note never had A
        note = _note(
            ["new a", "new b"],
            old_fields=["old b"],
            old_tags=[self._protected_tag("A")],
            notetype=nt,
            old_notetype=old_nt,
        )
        note.handle_import_config_changes(_config(), _nm(nt))

        # Nothing to preserve from, so the imported value must survive intact.
        assert note.anki_object_dict["fields"] == ["new a", "new b"]

    def test_a_protected_field_added_by_the_merge_keeps_the_remote_value(self):
        """After the merge the local note is padded with "" for the new field.

        That padding is not a user value; protection must not replace the
        imported content with it.
        """
        nt = make_notetype(fields=["A", "B"])
        note = _note(
            ["remote a", "remote b"],
            old_fields=["", "old b"],  # note is already merged/remapped
            notetype=nt,
        )
        nm = _nm(nt)
        nm.local_field_names_before_merge = {"b"}
        cfg = _config(personal=[("Basic", "A")])

        note.handle_import_config_changes(cfg, nm)

        assert note.anki_object_dict["fields"] == ["remote a", "remote b"]

    def test_a_protected_field_that_existed_locally_keeps_its_empty_value(self):
        """An empty field the user really had is still a value to protect."""
        nt = make_notetype(fields=["A", "B"])
        note = _note(
            ["remote a", "remote b"],
            old_fields=["", "old b"],
            notetype=nt,
        )
        nm = _nm(nt)
        nm.local_field_names_before_merge = {"a", "b"}
        cfg = _config(personal=[("Basic", "A")])

        note.handle_import_config_changes(cfg, nm)

        assert note.anki_object_dict["fields"] == ["", "remote b"]

    def test_a_migrated_notes_value_survives_for_a_field_the_target_lacked(self):
        """A migration can bring real content into a field the target added.

        That content is a user value even though the target notetype itself did
        not have the field before the merge.
        """
        nt = make_notetype(fields=["A", "B"])
        note = _note(
            ["remote a", "remote b"],
            old_fields=["migrated a", "old b"],
            notetype=nt,
        )
        nm = _nm(nt)
        nm.local_field_names_before_merge = {"b"}
        cfg = _config(personal=[("Basic", "A")])

        note.handle_import_config_changes(cfg, nm)

        assert note.anki_object_dict["fields"] == ["migrated a", "remote b"]

    def test_new_notes_never_apply_protection(self):
        nt = make_notetype(fields=["A", "B"])
        note = _note(
            ["remote a", "remote b"],
            old_fields=["", ""],
            notetype=nt,
        )
        nm = _nm(nt)
        nm.local_field_names_before_merge = {"a", "b"}
        cfg = _config(personal=[("Basic", "A")])

        note.handle_import_config_changes(cfg, nm, is_new=True)

        assert note.anki_object_dict["fields"] == ["remote a", "remote b"]

    def test_tags_protection_copies_all_local_tags(self):
        nt = make_notetype(fields=["A", "B"])
        note = _note(
            ["new a", "new b"],
            remote_tags=["remote"],
            old_fields=["old a", "old b"],
            old_tags=["local-one", "local-two", self._protected_tag("Tags")],
            notetype=nt,
        )
        note.handle_import_config_changes(_config(), _nm(nt))

        assert set(note.anki_object_dict["tags"]) >= {
            "local-one",
            "local-two",
            self._protected_tag("Tags"),
        }

    def test_all_protection_with_name_based_mapping(self):
        nt = make_notetype(fields=["A", "B"])
        note = _note(
            ["new a", "new b"],
            old_fields=["old a", "old b"],
            old_tags=["local-tag", self._protected_tag("All")],
            notetype=nt,
        )
        note.handle_import_config_changes(_config(), _nm(nt))

        assert note.anki_object_dict["fields"] == ["old a", "old b"]
        assert "local-tag" in note.anki_object_dict["tags"]

    def test_all_protection_copies_each_field_by_name(self):
        nt = make_notetype(fields=["B", "A"])  # merged layout
        old_nt = make_notetype(fields=["A", "B"])  # note's own layout
        note = _note(
            ["new b", "new a"],
            old_fields=["old a", "old b"],
            old_tags=[self._protected_tag("All")],
            notetype=nt,
            old_notetype=old_nt,
        )
        note.handle_import_config_changes(_config(), _nm(nt))

        assert note.anki_object_dict["fields"] == ["old b", "old a"]

    def test_all_protection_ignores_values_beyond_the_local_fields(self):
        nt = make_notetype(fields=["A", "B"])
        note = _note(
            ["new a", "new b"],
            old_fields=["old a"],  # fewer values than the notetype
            old_tags=[self._protected_tag("All")],
            notetype=nt,
        )
        note.handle_import_config_changes(_config(), _nm(nt))

        assert note.anki_object_dict["fields"] == ["old a", "new b"]

    def test_unrelated_tags_are_left_alone(self):
        nt = make_notetype(fields=["A", "B"])
        note = _note(
            ["new a", "new b"],
            remote_tags=["alpha"],
            old_fields=["old a", "old b"],
            old_tags=["beta"],
            notetype=nt,
        )
        note.handle_import_config_changes(_config(), _nm(nt))

        assert note.anki_object_dict["tags"] == ["alpha"]

    def test_prefix_match_is_not_segment_aware(self):
        """Any tag *starting with* the prefix is treated as a protection tag."""
        nt = make_notetype(fields=["A", "B"])
        lookalike = "AnkiCollab_ProtectXXX::A"
        note = _note(
            ["new a", "new b"],
            remote_tags=["alpha"],
            old_fields=["old a", "old b"],
            old_tags=[lookalike],
            notetype=nt,
        )
        note.handle_import_config_changes(_config(), _nm(nt))

        # Documented behaviour: the lookalike tag is preserved and "A" is
        # treated as the protected field name.
        assert lookalike in note.anki_object_dict["tags"]
        assert note.anki_object_dict["fields"] == ["old a", "new b"]


# ──────────────────────────────────────────────────────────────────────
# Personal / optional tags
# ──────────────────────────────────────────────────────────────────────


class TestPersonalTagMerging:
    def test_exact_and_nested_personal_tags_are_preserved(self):
        nt = make_notetype(fields=["A", "B"])
        note = _note(
            ["new a", "new b"],
            remote_tags=["remote"],
            old_fields=["old a", "old b"],
            old_tags=["leech", "leech::child", "unrelated", "MARKED"],
            notetype=nt,
        )
        note.handle_import_config_changes(
            _config(personal_tags=["leech", "marked"]), _nm(nt)
        )

        tags = note.anki_object_dict["tags"]
        assert "leech" in tags
        assert "leech::child" in tags
        assert "MARKED" in tags  # matched case-insensitively
        assert "unrelated" not in tags

    def test_no_personal_tags_leaves_remote_tags_untouched(self):
        nt = make_notetype(fields=["A", "B"])
        note = _note(
            ["new a", "new b"],
            remote_tags=["remote"],
            old_fields=["old a", "old b"],
            old_tags=["leech"],
            notetype=nt,
        )
        note.handle_import_config_changes(_config(personal_tags=[]), _nm(nt))
        assert note.anki_object_dict["tags"] == ["remote"]


class TestOptionalTagFiltering:
    def test_disabled_optional_tags_are_kept_verbatim(self):
        nt = make_notetype(fields=["A", "B"])
        optional = f"{PREFIX_OPTIONAL_TAGS}::hard"
        note = _note(
            ["new a", "new b"],
            remote_tags=[optional, "plain"],
            old_fields=["old a", "old b"],
            notetype=nt,
        )
        note.handle_import_config_changes(
            _config(has_optional_tags=False, optional_tags=[]), _nm(nt)
        )
        assert set(note.anki_object_dict["tags"]) == {optional, "plain"}

    def test_enabled_optional_tags_are_filtered_by_selection(self):
        nt = make_notetype(fields=["A", "B"])
        keep = f"{PREFIX_OPTIONAL_TAGS}::hard"
        drop = f"{PREFIX_OPTIONAL_TAGS}::easy"
        note = _note(
            ["new a", "new b"],
            remote_tags=[keep, drop, "plain"],
            old_fields=["old a", "old b"],
            notetype=nt,
        )
        note.handle_import_config_changes(
            _config(has_optional_tags=True, optional_tags=["hard"]), _nm(nt)
        )
        assert set(note.anki_object_dict["tags"]) == {keep, "plain"}

    def test_enabled_optional_tags_without_tags_key_is_a_no_op(self):
        nt = make_notetype(fields=["A", "B"])
        note = _note(["new a", "new b"], old_fields=["old a", "old b"], notetype=nt)
        del note.anki_object_dict["tags"]
        note.handle_import_config_changes(
            _config(has_optional_tags=True, optional_tags=["hard"]), _nm(nt)
        )
        assert "tags" not in note.anki_object_dict
