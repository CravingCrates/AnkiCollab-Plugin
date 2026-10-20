"""Mutation-hardening tests for ``Note.handle_import_config_changes``.

The existing tests cover the two happy protection strategies.  These tests
target the remaining branches: bailing out on malformed notes, the old-model
lookup fallbacks, field padding/truncation bookkeeping, the tag-driven
protection strategies ("All"/"Tags"/named fields), personal-tag merging and
the optional-tag filter.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from tests.conftest import make_note_dict, make_notetype, MockAnkiNote

from crowd_anki.representation.note import Note
from crowd_anki.representation.note_model import NoteModel
from var_defs import PREFIX_OPTIONAL_TAGS, PREFIX_PROTECTED_FIELDS


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
        note.handle_import_config_changes(_config(), _nm(make_notetype()), [0, 1])

    def test_empty_anki_object_dict_returns_immediately(self):
        note = Note.from_json(make_note_dict())
        note.anki_object_dict = {}
        note.anki_object = MockAnkiNote()
        note.handle_import_config_changes(_config(), _nm(make_notetype()), [0, 1])
        assert note.anki_object_dict == {}

    def test_local_note_without_fields_returns_immediately(self):
        note = Note.from_json(make_note_dict(fields=["new a", "new b"]))
        local = MockAnkiNote()
        del local.fields
        note.anki_object = local
        note.handle_import_config_changes(_config(), _nm(make_notetype()), [0, 1])
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

        note.handle_import_config_changes(cfg, _nm(nt), None)

        # Name-based lookup found "Front" in the old model and restored it.
        assert note.anki_object_dict["fields"][0] == "old front"

    def test_old_model_falls_back_to_model_attribute(self):
        nt = make_notetype(fields=["Front", "Back"])
        old_nt = make_notetype(fields=["Front", "Back"])
        note = _note(["new front", "new back"], notetype=nt)
        note.anki_object = _LegacyNote(["old front", "old back"], model=old_nt)
        cfg = _config(personal=[("Basic", "Front")])

        note.handle_import_config_changes(cfg, _nm(nt), None)

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

        note.handle_import_config_changes(cfg, _nm(nt), None)

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
            _config(personal=[("Basic", "Front")]), _nm(nt), None
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

        note.handle_import_config_changes(cfg, _nm(nt), None)

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

        note.handle_import_config_changes(cfg, _nm(nt), None)

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
        note.handle_import_config_changes(_config(), _nm(nt), None)

        # Anki rejects a note whose field count does not match its notetype.
        assert note.anki_object_dict["fields"] == ["new a", "new b"]

    def test_missing_remote_fields_are_padded_with_empty_strings(self):
        nt = make_notetype(fields=["A", "B", "C"])
        note = _note(["new a"], old_fields=["old a"], notetype=nt)

        note.handle_import_config_changes(_config(), _nm(nt), None)

        assert note.anki_object_dict["fields"] == ["new a", "", ""]

    def test_local_only_fields_are_appended_in_order(self):
        # The notetype has room for the extra local fields.
        nt = make_notetype(fields=["A", "B", "C", "D"])
        note = _note(
            ["new a", "new b"],
            old_fields=["old a", "old b", "old c", "mine"],
            notetype=nt,
        )
        note.handle_import_config_changes(_config(), _nm(nt), None)

        assert note.anki_object_dict["fields"] == [
            "new a",
            "new b",
            "old c",
            "mine",
        ]

    def test_local_only_fields_use_the_explicit_mapping(self):
        nt = make_notetype(fields=["A", "B", "C", "D"])
        note = _note(
            ["new a", "new b"],
            old_fields=["old a", "old b", "old c", "mine"],
            notetype=nt,
        )
        note.handle_import_config_changes(_config(), _nm(nt), [0, 1, 3, 2])

        assert note.anki_object_dict["fields"] == [
            "new a",
            "new b",
            "mine",
            "old c",
        ]

    def test_local_only_fields_with_unmapped_index_are_skipped(self):
        nt = make_notetype(fields=["A", "B", "C", "D"])
        note = _note(
            ["new a", "new b"],
            old_fields=["old a", "old b", "old c", "mine"],
            notetype=nt,
        )
        # Only index 3 has an old counterpart.
        note.handle_import_config_changes(_config(), _nm(nt), [0, 1, None, 3])

        # The unmapped slot is padded so Anki's field count still matches.
        assert note.anki_object_dict["fields"] == ["new a", "new b", "mine", ""]

    def test_local_only_field_mapping_out_of_range_is_skipped(self):
        nt = make_notetype(fields=["A", "B", "C", "D"])
        note = _note(
            ["new a", "new b"],
            old_fields=["old a", "old b", "old c", "mine"],
            notetype=nt,
        )
        note.handle_import_config_changes(_config(), _nm(nt), [0, 1, 9, 3])

        assert note.anki_object_dict["fields"] == ["new a", "new b", "mine", ""]


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
        note.handle_import_config_changes(_config(), _nm(nt), None)

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
        note.handle_import_config_changes(_config(), _nm(nt), None)

        assert note.anki_object_dict["fields"] == ["old a", "new b"]

    def test_named_field_protection_accepts_underscore_spacing(self):
        nt = make_notetype(fields=["Extra Info", "B"])
        note = _note(
            ["new info", "new b"],
            old_fields=["old info", "old b"],
            old_tags=[self._protected_tag("Extra_Info")],
            notetype=nt,
        )
        note.handle_import_config_changes(_config(), _nm(nt), None)

        assert note.anki_object_dict["fields"] == ["old info", "new b"]

    def test_named_field_protection_with_explicit_mapping(self):
        nt = make_notetype(fields=["A", "B"])
        note = _note(
            ["new a", "new b"],
            old_fields=["old a", "old b"],
            old_tags=[self._protected_tag("B")],
            notetype=nt,
        )
        # new B (index 1) came from old index 0
        note.handle_import_config_changes(_config(), _nm(nt), [1, 0])

        assert note.anki_object_dict["fields"] == ["new a", "old a"]

    def test_an_unusable_mapping_keeps_the_remote_value(self):
        nt = make_notetype(fields=["A", "B"])
        note = _note(
            ["new a", "new b"],
            old_fields=["old a", "old b"],
            old_tags=[self._protected_tag("A")],
            notetype=nt,
        )
        note.handle_import_config_changes(_config(), _nm(nt), [None, 1])

        # Nothing to preserve from, so the imported value must survive intact.
        assert note.anki_object_dict["fields"] == ["new a", "new b"]

    def test_tags_protection_copies_all_local_tags(self):
        nt = make_notetype(fields=["A", "B"])
        note = _note(
            ["new a", "new b"],
            remote_tags=["remote"],
            old_fields=["old a", "old b"],
            old_tags=["local-one", "local-two", self._protected_tag("Tags")],
            notetype=nt,
        )
        note.handle_import_config_changes(_config(), _nm(nt), None)

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
        note.handle_import_config_changes(_config(), _nm(nt), None)

        assert note.anki_object_dict["fields"] == ["old a", "old b"]
        assert "local-tag" in note.anki_object_dict["tags"]

    def test_all_protection_with_explicit_mapping(self):
        nt = make_notetype(fields=["A", "B"])
        note = _note(
            ["new a", "new b"],
            old_fields=["old a", "old b"],
            old_tags=[self._protected_tag("All")],
            notetype=nt,
        )
        # Remote A <- old B, remote B <- old A.
        note.handle_import_config_changes(_config(), _nm(nt), [1, 0])

        assert note.anki_object_dict["fields"] == ["old b", "old a"]

    def test_all_protection_ignores_out_of_range_mapping_entries(self):
        nt = make_notetype(fields=["A", "B"])
        note = _note(
            ["new a", "new b"],
            old_fields=["old a", "old b"],
            old_tags=[self._protected_tag("All")],
            notetype=nt,
        )
        # The second mapping entry is None and the first points out of range.
        note.handle_import_config_changes(_config(), _nm(nt), [5, None])

        assert note.anki_object_dict["fields"] == ["new a", "new b"]

    def test_unrelated_tags_are_left_alone(self):
        nt = make_notetype(fields=["A", "B"])
        note = _note(
            ["new a", "new b"],
            remote_tags=["alpha"],
            old_fields=["old a", "old b"],
            old_tags=["beta"],
            notetype=nt,
        )
        note.handle_import_config_changes(_config(), _nm(nt), None)

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
        note.handle_import_config_changes(_config(), _nm(nt), None)

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
            _config(personal_tags=["leech", "marked"]), _nm(nt), None
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
        note.handle_import_config_changes(_config(personal_tags=[]), _nm(nt), None)
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
            _config(has_optional_tags=False, optional_tags=[]), _nm(nt), None
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
            _config(has_optional_tags=True, optional_tags=["hard"]), _nm(nt), None
        )
        assert set(note.anki_object_dict["tags"]) == {keep, "plain"}

    def test_enabled_optional_tags_without_tags_key_is_a_no_op(self):
        nt = make_notetype(fields=["A", "B"])
        note = _note(["new a", "new b"], old_fields=["old a", "old b"], notetype=nt)
        del note.anki_object_dict["tags"]
        note.handle_import_config_changes(
            _config(has_optional_tags=True, optional_tags=["hard"]), _nm(nt), None
        )
        assert "tags" not in note.anki_object_dict
