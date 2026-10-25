"""Data-preservation scenarios against a real Anki backend.

Every test asserts the *name -> value* mapping of a persisted note, never a list
position. Positions are exactly what goes wrong when a notetype's field order
changes, so asserting them would hide the bug instead of catching it.

The regression these tests exist for: `models.update_dict()` rewrites a
notetype but leaves note values where they are. A merge that reorders fields
therefore moves content between field *names* unless the notes are remapped.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import (  # noqa: E402
    Config,
    add_note,
    col,  # noqa: F401  (pytest fixture)
    import_new_note,
    import_note,
    make_notetype,
    merge_remote,
    values_by_name,
)

from ankicollab_under_test.crowd_anki.representation.note_model import (  # noqa: E402
    NoteModel,
)

REMOTE_18 = [f"R{i}" for i in range(18)]


# ---------------------------------------------------------------------------
# The reported bug: local-only fields at 3 and 19
# ---------------------------------------------------------------------------


class TestLocalOnlyFields:
    def test_local_field_content_survives_a_merge(self, col):
        """A local field interleaved at index 3 and one at the end keep content.

        This is the exact shape that lost `Mein schatz`: 18 remote fields, local
        extras at 3 and 19, merged to [18 remote..., local, local].
        """
        local_names = REMOTE_18[:3] + ["Mein schatz"] + REMOTE_18[3:] + ["ankihub_id"]
        notetype = make_notetype(col, "ProjektAnkiCloze", local_names)
        local_values = [f"local-{name}" for name in local_names]
        note = add_note(col, notetype, 1, local_values)

        model = merge_remote(col, notetype, REMOTE_18)
        assert model.local_only_field_names == ["Mein schatz", "ankihub_id"]

        import_note(
            col,
            model,
            note.id,
            remote_values=[f"remote-{name}" for name in REMOTE_18],
        )
        values = values_by_name(col, note.id)

        assert len(values) == 20
        assert values["Mein schatz"] == "local-Mein schatz"
        assert values["ankihub_id"] == "local-ankihub_id"
        for name in REMOTE_18:
            assert values[name] == f"remote-{name}"

    def test_several_notes_keep_their_own_values(self, col):
        local_names = REMOTE_18[:3] + ["Mein schatz"] + REMOTE_18[3:] + ["ankihub_id"]
        notetype = make_notetype(col, "ProjektAnkiCloze", local_names)
        notes = [
            add_note(col, notetype, 1, [f"n{i}-{name}" for name in local_names])
            for i in range(3)
        ]

        model = merge_remote(col, notetype, REMOTE_18)
        for i, note in enumerate(notes):
            import_note(
                col,
                model,
                note.id,
                remote_values=[f"n{i}-remote-{name}" for name in REMOTE_18],
            )

        for i, note in enumerate(notes):
            values = values_by_name(col, note.id)
            assert values["Mein schatz"] == f"n{i}-Mein schatz"
            assert values["ankihub_id"] == f"n{i}-ankihub_id"
            assert values["R5"] == f"n{i}-remote-R5"

    def test_notes_in_other_decks_keep_their_values(self, col):
        """The notetype is global: a bad merge damages notes outside the import."""
        local_names = REMOTE_18[:3] + ["Mein schatz"] + REMOTE_18[3:] + ["ankihub_id"]
        notetype = make_notetype(col, "ProjektAnkiCloze", local_names)
        other_deck = col.decks.id("Unrelated")
        other_note = add_note(
            col, notetype, other_deck, [f"local-{name}" for name in local_names]
        )

        merge_remote(col, notetype, REMOTE_18)

        values = values_by_name(col, other_note.id)
        assert values["Mein schatz"] == "local-Mein schatz"
        assert values["ankihub_id"] == "local-ankihub_id"
        assert values["R0"] == "local-R0"

    def test_sender_local_fields_do_not_overwrite_ours(self, col):
        """Contributors may have their own extra fields; they must not leak."""
        local_names = REMOTE_18[:3] + ["Mein schatz"] + REMOTE_18[3:] + ["ankihub_id"]
        notetype = make_notetype(col, "ProjektAnkiCloze", local_names)
        note = add_note(col, notetype, 1, [f"local-{name}" for name in local_names])

        model = merge_remote(col, notetype, REMOTE_18)
        wire_values = [f"remote-{name}" for name in REMOTE_18] + [
            "their extra 1",
            "their extra 2",
        ]
        import_note(col, model, note.id, remote_values=wire_values)

        values = values_by_name(col, note.id)
        assert len(values) == 20
        assert values["Mein schatz"] == "local-Mein schatz"
        assert values["ankihub_id"] == "local-ankihub_id"
        assert "their extra 1" not in values.values()

    def test_fewer_sender_extras_do_not_shift_our_fields(self, col):
        local_names = REMOTE_18[:3] + ["Mein schatz"] + REMOTE_18[3:] + ["ankihub_id"]
        notetype = make_notetype(col, "ProjektAnkiCloze", local_names)
        note = add_note(col, notetype, 1, [f"local-{name}" for name in local_names])

        model = merge_remote(col, notetype, REMOTE_18)
        wire_values = [f"remote-{name}" for name in REMOTE_18] + ["their extra"]
        import_note(col, model, note.id, remote_values=wire_values)

        values = values_by_name(col, note.id)
        assert values["Mein schatz"] == "local-Mein schatz"
        assert values["ankihub_id"] == "local-ankihub_id"


# ---------------------------------------------------------------------------
# Schema changes that move existing values
# ---------------------------------------------------------------------------


class TestSchemaChanges:
    def test_reorder_keeps_values_under_their_names(self, col):
        names = ["Front", "Back", "Extra"]
        notetype = make_notetype(col, "Reordered", names)
        note = add_note(col, notetype, 1, ["front-value", "back-value", "extra-value"])

        merge_remote(col, notetype, ["Extra", "Front", "Back"])

        assert values_by_name(col, note.id) == {
            "Extra": "extra-value",
            "Front": "front-value",
            "Back": "back-value",
        }

    def test_inserted_remote_field_does_not_shift_values(self, col):
        notetype = make_notetype(col, "Inserted", ["A", "B"])
        note = add_note(col, notetype, 1, ["a-value", "b-value"])

        merge_remote(col, notetype, ["A", "New", "B"])

        assert values_by_name(col, note.id) == {
            "A": "a-value",
            "New": "",
            "B": "b-value",
        }

    def test_removed_remote_field_content_is_kept_as_local_only(self, col):
        notetype = make_notetype(col, "Removed", ["A", "B", "C"])
        note = add_note(col, notetype, 1, ["a-value", "b-value", "c-value"])

        model = merge_remote(col, notetype, ["A", "C"])

        assert model.local_only_field_names == ["B"]
        assert values_by_name(col, note.id) == {
            "A": "a-value",
            "C": "c-value",
            "B": "b-value",
        }

    def test_renamed_remote_field_keeps_the_old_value(self, col):
        """A rename cannot be mapped automatically, so the old value is kept."""
        notetype = make_notetype(col, "Renamed", ["Front", "Back"])
        note = add_note(col, notetype, 1, ["front-value", "back-value"])

        model = merge_remote(col, notetype, ["Prompt", "Back"])

        assert model.local_only_field_names == ["Front"]
        values = values_by_name(col, note.id)
        assert values["Prompt"] == ""
        assert values["Back"] == "back-value"
        assert values["Front"] == "front-value"

    def test_appending_a_field_does_not_move_existing_values(self, col):
        notetype = make_notetype(col, "Appended", ["A", "B"])
        note = add_note(col, notetype, 1, ["a-value", "b-value"])

        merge_remote(col, notetype, ["A", "B", "C"])

        assert values_by_name(col, note.id) == {
            "A": "a-value",
            "B": "b-value",
            "C": "",
        }

    def test_cards_are_not_recreated_by_a_merge(self, col):
        notetype = make_notetype(col, "Cards", ["A", "B", "C"])
        note = add_note(col, notetype, 1, ["a", "b", "c"])
        cards_before = list(note.card_ids())

        merge_remote(col, notetype, ["C", "A", "B"])

        assert list(col.get_note(note.id).card_ids()) == cards_before

    def test_card_scheduling_survives_a_merge(self, col):
        """An untouched note's review state must not reset when fields move."""
        notetype = make_notetype(col, "Scheduling", ["A", "B", "C"])
        note = add_note(col, notetype, 1, ["a", "b", "c"])
        card_id = note.card_ids()[0]
        card = col.get_card(card_id)
        card.type = 2
        card.queue = 2
        card.due = 12_345
        card.ivl = 10
        card.factor = 2_500
        card.reps = 3
        card.lapses = 1
        col.update_card(card)

        merge_remote(col, notetype, ["C", "A", "B"])

        after = col.get_card(card_id)
        assert (after.type, after.queue) == (2, 2)
        assert (after.due, after.ivl, after.factor) == (12_345, 10, 2_500)
        assert (after.reps, after.lapses) == (3, 1)


# ---------------------------------------------------------------------------
# Protected fields, resolved by name on a merged notetype
# ---------------------------------------------------------------------------


class TestProtectionAfterMerge:
    def test_maintainer_protected_field_keeps_local_value(self, col):
        notetype = make_notetype(col, "Protected", ["A", "B", "C"])
        note = add_note(col, notetype, 1, ["local-a", "local-b", "local-c"])

        model = merge_remote(col, notetype, ["C", "A", "B"])
        import_note(
            col,
            model,
            note.id,
            remote_values=["remote-C", "remote-A", "remote-B"],
            config=Config(personal_fields=[("Protected", "A")]),
        )

        values = values_by_name(col, note.id)
        assert values["A"] == "local-a"
        assert values["C"] == "remote-C"
        assert values["B"] == "remote-B"

    def test_protecting_the_first_field_works(self, col):
        notetype = make_notetype(col, "FirstProtected", ["A", "B"])
        note = add_note(col, notetype, 1, ["local-a", "local-b"])

        model = merge_remote(col, notetype, ["B", "A"])
        import_note(
            col,
            model,
            note.id,
            remote_values=["remote-B", "remote-A"],
            config=Config(personal_fields=[("FirstProtected", "B")]),
        )

        values = values_by_name(col, note.id)
        assert values["B"] == "local-b"
        assert values["A"] == "remote-A"

    def test_protect_tag_with_underscore_name(self, col):
        notetype = make_notetype(col, "Underscore", ["Eigene Notizen", "B"])
        note = add_note(
            col,
            notetype,
            1,
            ["local-own", "local-b"],
            tags=["AnkiCollab_Protect::Eigene_Notizen"],
        )

        model = merge_remote(col, notetype, ["B", "Eigene Notizen"])
        import_note(
            col,
            model,
            note.id,
            remote_values=["remote-B", "remote-own"],
        )

        values = values_by_name(col, note.id)
        assert values["Eigene Notizen"] == "local-own"
        assert values["B"] == "remote-B"

    def test_protect_all_after_a_reorder(self, col):
        notetype = make_notetype(col, "ProtectAll", ["A", "B", "Local"])
        note = add_note(
            col,
            notetype,
            1,
            ["local-a", "local-b", "local-local"],
            tags=["AnkiCollab_Protect::All"],
        )

        model = merge_remote(col, notetype, ["B", "A"])
        import_note(
            col,
            model,
            note.id,
            remote_values=["remote-B", "remote-A"],
        )

        assert values_by_name(col, note.id) == {
            "B": "local-b",
            "A": "local-a",
            "Local": "local-local",
        }

    def test_a_new_note_imports_protected_field_content(self, col):
        """There is no user value to protect on a brand-new note."""
        notetype = make_notetype(col, "NewProtected", ["A", "B"])
        model = merge_remote(col, notetype, ["A", "B"])

        note = import_new_note(
            col,
            model,
            ["remote-a", "remote-b"],
            config=Config(personal_fields=[("NewProtected", "A")]),
            deck_id=col.decks.id("NewProtected"),
        )

        assert values_by_name(col, note.id) == {
            "A": "remote-a",
            "B": "remote-b",
        }

    def test_protect_all_still_imports_a_field_the_note_never_had(self, col):
        notetype = make_notetype(col, "ProtectAllNew", ["B", "Local"])
        note = add_note(
            col,
            notetype,
            1,
            ["local-b", "local-local"],
            tags=["AnkiCollab_Protect::All"],
        )

        model = merge_remote(col, notetype, ["A", "B"])
        import_note(col, model, note.id, remote_values=["remote-a", "remote-b"])

        assert values_by_name(col, note.id) == {
            "A": "remote-a",
            "B": "local-b",
            "Local": "local-local",
        }

    def test_an_existing_note_imports_a_protected_field_it_never_had(self, col):
        """The merge padded the new field with ""; that is not a user value."""
        notetype = make_notetype(col, "AddedProtected", ["B"])
        note = add_note(
            col,
            notetype,
            1,
            ["local-b"],
        )

        model = merge_remote(col, notetype, ["A", "B"])
        import_note(
            col,
            model,
            note.id,
            remote_values=["remote-a", "remote-b"],
            config=Config(personal_fields=[("AddedProtected", "A")]),
        )

        assert values_by_name(col, note.id) == {
            "A": "remote-a",
            "B": "remote-b",
        }


# ---------------------------------------------------------------------------
# Idempotency / repeat imports
# ---------------------------------------------------------------------------


class TestRepeatImport:
    def test_merging_twice_changes_nothing(self, col):
        local_names = REMOTE_18[:3] + ["Mein schatz"] + REMOTE_18[3:] + ["ankihub_id"]
        notetype = make_notetype(col, "ProjektAnkiCloze", local_names)
        note = add_note(col, notetype, 1, [f"local-{name}" for name in local_names])

        model = merge_remote(col, notetype, REMOTE_18)
        first = values_by_name(col, note.id)
        merged_order = [field["name"] for field in model.anki_dict["flds"]]

        merge_remote(col, notetype, REMOTE_18)

        assert values_by_name(col, note.id) == first
        assert [field["name"] for field in col.models.get(notetype["id"])["flds"]] == (
            merged_order
        )

    def test_reimporting_the_same_note_values_is_stable(self, col):
        local_names = REMOTE_18[:3] + ["Mein schatz"] + REMOTE_18[3:] + ["ankihub_id"]
        notetype = make_notetype(col, "ProjektAnkiCloze", local_names)
        note = add_note(col, notetype, 1, [f"local-{name}" for name in local_names])

        model = merge_remote(col, notetype, REMOTE_18)
        wire = [f"remote-{name}" for name in REMOTE_18]
        import_note(col, model, note.id, remote_values=wire)
        first = values_by_name(col, note.id)

        import_note(col, model, note.id, remote_values=wire)

        assert values_by_name(col, note.id) == first
        assert first["Mein schatz"] == "local-Mein schatz"
        assert first["ankihub_id"] == "local-ankihub_id"

    def test_a_notetype_can_evolve_over_several_imports(self, col):
        """Remove, re-add and reorder fields across imports without losing values."""
        notetype = make_notetype(col, "Evolving", ["A", "B", "C"])
        note = add_note(col, notetype, 1, ["a", "b", "c"])
        expected = {"A": "a", "B": "b", "C": "c"}

        merge_remote(col, notetype, ["A", "C"])  # B removed remotely
        assert values_by_name(col, note.id) == expected

        merge_remote(col, notetype, ["A", "B", "C"])  # B comes back in the middle
        assert values_by_name(col, note.id) == expected

        merge_remote(col, notetype, ["C", "A"])  # B removed again, order reversed
        assert values_by_name(col, note.id) == expected

    def test_migrating_to_a_notetype_with_a_new_local_field_keeps_its_value(self, col):
        """The migration path used for an incompatible local twin.

        Deck._change_existing_note_types appends the old notetype's local-only
        fields to the target before changing notes. This locks the backend
        behavior that makes the values survive.
        """
        from anki.models import ChangeNotetypeRequest

        old = make_notetype(col, "Old Twin", ["A", "B", "LocalOnly"], uuid="old-u")
        note = add_note(col, old, 1, ["a", "b", "local"])
        target = make_notetype(col, "New Twin", ["A", "B"], uuid="new-u")

        updated = col.models.get(target["id"])
        field = col.models.new_field("LocalOnly")
        field["ord"] = len(updated["flds"])
        updated["flds"].append(field)
        col.models.update_dict(updated)

        old = col.models.get(old["id"])
        target = col.models.get(target["id"])
        field_map = NoteModel._build_intelligent_field_map(old, target)
        field_map = [
            -1 if index is None else index for index in field_map[: len(target["flds"])]
        ]
        col.models.change_notetype_of_notes(
            ChangeNotetypeRequest(
                note_ids=[note.id],
                old_notetype_id=old["id"],
                new_notetype_id=target["id"],
                current_schema=col.db.scalar("select scm from col"),
                new_fields=field_map,
            )
        )

        assert values_by_name(col, note.id) == {
            "A": "a",
            "B": "b",
            "LocalOnly": "local",
        }
