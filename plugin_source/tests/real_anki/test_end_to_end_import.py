"""Real export -> JSON -> import orchestration.

These tests run the actual ``deck_initializer``/``Deck`` code against two real
``anki.Collection`` instances: one source collection is exported, the resulting
payload is imported into a fresh target, and the target is inspected by field
name and card count.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from anki.collection import Collection  # noqa: E402

from support import (  # noqa: E402
    Config,
    Note,
    add_note,
    col,  # noqa: F401  (pytest fixture)
    export_deck,
    import_deck,
    import_deck_layer,
    make_notetype,
    values_by_name,
)


@pytest.fixture
def target_col(tmp_path):
    collection = Collection(str(tmp_path / "target.anki2"))
    try:
        yield collection
    finally:
        collection.close()


def _counts(collection):
    notes = collection.db.scalar("select count() from notes")
    cards = collection.db.scalar("select count() from cards")
    return notes, cards


def _single_note_id(collection):
    return collection.db.scalar("select id from notes")


def _note_id_by_guid(collection, guid):
    return collection.db.scalar("select id from notes where guid = ?", guid)


def _card_count_for_note(collection, note_id):
    return collection.db.scalar("select count() from cards where nid = ?", note_id)


def _set_fields(collection, notetype_id, field_names):
    """Reorder/add fields on a notetype (does not touch notes)."""
    notetype = collection.models.get(notetype_id)
    existing = {field["name"]: field for field in notetype["flds"]}
    fields = []
    for name in field_names:
        field = existing.get(name) or collection.models.new_field(name)
        fields.append(field)
    for index, field in enumerate(fields):
        field["ord"] = index
    notetype["flds"] = fields
    collection.models.update_dict(notetype)


def _write_note_by_name(collection, note_id, values):
    note = collection.get_note(note_id)
    model = collection.models.get(note.mid)
    note.fields = [values.get(field["name"], "") for field in model["flds"]]
    collection.update_note(note)


def _write_note_and_tags(collection, note_id, values, tags):
    note = collection.get_note(note_id)
    model = collection.models.get(note.mid)
    note.fields = [values.get(field["name"], "") for field in model["flds"]]
    note.tags = list(tags)
    collection.update_note(note)


def _add_second_card_template(collection, notetype_id, index):
    notetype = collection.models.get(notetype_id)
    template = collection.models.new_template(f"Card {index}")
    template["qfmt"] = f"{{{{A}}}}<br>Card {index}"
    template["afmt"] = "{{FrontSide}}"
    collection.models.add_template(notetype, template)
    collection.models.update_dict(notetype)


def test_fresh_export_import_and_repeat(col, target_col, monkeypatch):
    notetype = make_notetype(col, "Basic", ["Front", "Back"], uuid="basic-e2e")
    add_note(col, notetype, col.decks.id("Source"), ["front", "back"])

    _deck, payload = export_deck(col, "Source", monkeypatch)
    assert sum(len(n.get("fields", [])) > 0 for n in payload["notes"]) == 1

    import_deck(target_col, payload, monkeypatch)
    assert _counts(target_col) == (1, 1)

    import_deck(target_col, payload, monkeypatch)
    assert _counts(target_col) == (1, 1)

    # The imported collection can be exported again without changing shape.
    _roundtrip_deck, roundtrip = export_deck(target_col, "Source", monkeypatch)
    assert _serialized_note_count(roundtrip) == 1

    assert values_by_name(target_col, _single_note_id(target_col)) == {
        "Front": "front",
        "Back": "back",
    }


def test_schema_evolution_preserves_imported_and_untouched_notes(
    col, target_col, monkeypatch
):
    notetype = make_notetype(col, "Evolving", ["A", "B", "C"], uuid="evolving-e2e")
    source_note = add_note(col, notetype, col.decks.id("Source"), ["a1", "b1", "c1"])
    _deck, payload = export_deck(col, "Source", monkeypatch)
    import_deck(target_col, payload, monkeypatch)

    target_model = target_col.models.by_name("Evolving")
    untouched = target_col.new_note(target_model)
    untouched.fields = ["ta", "tb", "tc"]
    target_col.add_note(untouched, target_col.decks.id("Untouched"))
    untouched_id = untouched.id

    # Reorder C,A,B and insert D in the middle of the remote schema.
    _set_fields(col, notetype["id"], ["C", "A", "B", "D"])
    _write_note_by_name(
        col,
        source_note.id,
        {"C": "c2", "A": "a2", "B": "b2", "D": "d2"},
    )
    _deck, payload2 = export_deck(col, "Source", monkeypatch)
    import_deck(target_col, payload2, monkeypatch)

    imported_id = _note_id_by_guid(target_col, source_note.guid)
    assert values_by_name(target_col, imported_id) == {
        "C": "c2",
        "A": "a2",
        "B": "b2",
        "D": "d2",
    }
    assert values_by_name(target_col, untouched_id) == {
        "C": "tc",
        "A": "ta",
        "B": "tb",
        "D": "",
    }
    assert _counts(target_col) == (2, 2)


def test_field_rename_keeps_the_old_value_in_a_local_only_field(
    col, target_col, monkeypatch
):
    notetype = make_notetype(col, "Rename", ["Front", "Back"], uuid="rename-e2e")
    source_note = add_note(col, notetype, col.decks.id("Source"), ["old front", "back"])
    _deck, payload = export_deck(col, "Source", monkeypatch)
    import_deck(target_col, payload, monkeypatch)

    _set_fields(col, notetype["id"], ["Prompt", "Back"])
    _write_note_by_name(
        col, source_note.id, {"Prompt": "new prompt", "Back": "new back"}
    )
    _deck, payload2 = export_deck(col, "Source", monkeypatch)
    import_deck(target_col, payload2, monkeypatch)

    target_id = _note_id_by_guid(target_col, source_note.guid)
    values = values_by_name(target_col, target_id)
    assert values["Prompt"] == "new prompt"
    assert values["Back"] == "new back"
    # The old value is not guessed into the renamed field; it survives under
    # its old name as a local-only field.
    assert values["Front"] == "old front"


def test_local_custom_field_survives_an_update(col, target_col, monkeypatch):
    notetype = make_notetype(col, "Custom Fields", ["A", "B"], uuid="custom-e2e")
    source_note = add_note(col, notetype, col.decks.id("Source"), ["a1", "b1"])
    _deck, payload = export_deck(col, "Source", monkeypatch)
    import_deck(target_col, payload, monkeypatch)

    target_model = target_col.models.by_name("Custom Fields")
    custom = target_col.models.new_field("My Custom")
    custom["ord"] = len(target_model["flds"])
    target_col.models.add_field(target_model, custom)
    target_col.models.update_dict(target_model)

    target_id = _note_id_by_guid(target_col, source_note.guid)
    target_note = target_col.get_note(target_id)
    model = target_col.models.get(target_note.mid)
    custom_index = [f["name"] for f in model["flds"]].index("My Custom")
    fields = list(target_note.fields)
    fields[custom_index] = "local-only value"
    target_note.fields = fields
    target_col.update_note(target_note)

    _write_note_by_name(col, source_note.id, {"A": "a2", "B": "b1"})
    _deck, payload2 = export_deck(col, "Source", monkeypatch)
    import_deck(target_col, payload2, monkeypatch)

    assert values_by_name(target_col, target_id) == {
        "A": "a2",
        "B": "b1",
        "My Custom": "local-only value",
    }


def test_maintainer_and_tag_protection_survive_a_real_import(
    col, target_col, monkeypatch
):
    notetype = make_notetype(col, "Protected", ["A", "B", "C"], uuid="protected-e2e")
    source_note = add_note(col, notetype, col.decks.id("Source"), ["a", "b", "c"])
    _deck, payload = export_deck(col, "Source", monkeypatch)
    import_deck(target_col, payload, monkeypatch)

    target_id = _note_id_by_guid(target_col, source_note.guid)
    _write_note_and_tags(
        target_col,
        target_id,
        {"A": "local-a", "B": "local-b", "C": "local-c"},
        ["AnkiCollab_Protect::A"],
    )

    _write_note_by_name(
        col, source_note.id, {"A": "remote-a", "B": "remote-b", "C": "remote-c"}
    )
    _deck, payload2 = export_deck(col, "Source", monkeypatch)
    import_deck(
        target_col,
        payload2,
        monkeypatch,
        config=Config(personal_fields=[("Protected", "B")]),
    )

    assert values_by_name(target_col, target_id) == {
        "A": "local-a",  # user tag
        "B": "local-b",  # maintainer-protected
        "C": "remote-c",
    }


def test_protection_handles_space_encoded_field_names(col, target_col, monkeypatch):
    notetype = make_notetype(
        col, "Spaced", ["A", "Eigene Notizen", "B"], uuid="spaced-e2e"
    )
    source_note = add_note(col, notetype, col.decks.id("Source"), ["a", "mine", "b"])
    _deck, payload = export_deck(col, "Source", monkeypatch)
    import_deck(target_col, payload, monkeypatch)

    target_id = _note_id_by_guid(target_col, source_note.guid)
    _write_note_and_tags(
        target_col,
        target_id,
        {"A": "a", "Eigene Notizen": "my note", "B": "b"},
        ["AnkiCollab_Protect::Eigene_Notizen"],
    )

    _write_note_by_name(
        col,
        source_note.id,
        {"A": "remote-a", "Eigene Notizen": "remote note", "B": "remote-b"},
    )
    _deck, payload2 = export_deck(col, "Source", monkeypatch)
    import_deck(target_col, payload2, monkeypatch)

    assert values_by_name(target_col, target_id) == {
        "A": "remote-a",
        "Eigene Notizen": "my note",
        "B": "remote-b",
    }


def test_protect_all_keeps_every_local_field(col, target_col, monkeypatch):
    notetype = make_notetype(col, "Protect All", ["A", "B"], uuid="all-e2e")
    source_note = add_note(col, notetype, col.decks.id("Source"), ["a", "b"])
    _deck, payload = export_deck(col, "Source", monkeypatch)
    import_deck(target_col, payload, monkeypatch)

    target_id = _note_id_by_guid(target_col, source_note.guid)
    _write_note_and_tags(
        target_col,
        target_id,
        {"A": "local-a", "B": "local-b"},
        ["AnkiCollab_Protect::All"],
    )

    # A field introduced by the remote version has no local value; even under
    # Protect All it must receive the imported content.
    _set_fields(col, notetype["id"], ["A", "B", "C"])
    _write_note_by_name(
        col,
        source_note.id,
        {"A": "remote-a", "B": "remote-b", "C": "remote-c"},
    )
    _deck, payload2 = export_deck(col, "Source", monkeypatch)
    import_deck(target_col, payload2, monkeypatch)

    assert values_by_name(target_col, target_id) == {
        "A": "local-a",
        "B": "local-b",
        "C": "remote-c",
    }


def test_protect_tags_keeps_local_tags(col, target_col, monkeypatch):
    notetype = make_notetype(col, "Protect Tags", ["A"], uuid="tags-e2e")
    source_note = add_note(
        col, notetype, col.decks.id("Source"), ["a"], tags=["remote-tag"]
    )
    _deck, payload = export_deck(col, "Source", monkeypatch)
    import_deck(target_col, payload, monkeypatch)

    target_id = _note_id_by_guid(target_col, source_note.guid)
    _write_note_and_tags(
        target_col,
        target_id,
        {"A": "local-a"},
        ["local-tag", "AnkiCollab_Protect::Tags"],
    )

    _write_note_by_name(col, source_note.id, {"A": "remote-a"})
    _deck, payload2 = export_deck(col, "Source", monkeypatch)
    import_deck(target_col, payload2, monkeypatch)

    tags = set(target_col.get_note(target_id).tags)
    assert "local-tag" in tags
    assert "AnkiCollab_Protect::Tags" in tags
    assert "remote-tag" not in tags


def test_a_new_note_receives_maintainer_protected_content(col, target_col, monkeypatch):
    notetype = make_notetype(col, "New Protected", ["A", "B"], uuid="new-prot-e2e")
    add_note(col, notetype, col.decks.id("Source"), ["remote-a", "remote-b"])
    _deck, payload = export_deck(col, "Source", monkeypatch)

    import_deck(
        target_col,
        payload,
        monkeypatch,
        config=Config(personal_fields=[("New Protected", "A")]),
    )

    assert values_by_name(target_col, _single_note_id(target_col)) == {
        "A": "remote-a",
        "B": "remote-b",
    }


def test_cancel_before_note_writes_leaves_no_notes_or_temp_deck(
    col, target_col, monkeypatch
):
    notetype = make_notetype(col, "Cancel", ["A", "B"], uuid="cancel-e2e")
    add_note(col, notetype, col.decks.id("Source"), ["a", "b"])
    _deck, payload = export_deck(col, "Source", monkeypatch)

    deck_initializer, _Deck = import_deck_layer()
    from ankicollab_under_test.crowd_anki.representation.deck import (
        DeckImportCancelled,
    )

    with pytest.raises(DeckImportCancelled):
        import_deck(target_col, payload, monkeypatch, cancel=True)

    assert _counts(target_col) == (0, 0)
    temp_decks = [
        entry.name
        for entry in target_col.decks.all_names_and_ids()
        if entry.name.startswith("_ankicollab_import_")
    ]
    assert temp_decks == []


def test_a_bulk_write_failure_does_not_leave_partial_notes(
    col, target_col, monkeypatch
):
    notetype = make_notetype(col, "Bulk Failure", ["A", "B"], uuid="bulk-fail-e2e")
    add_note(col, notetype, col.decks.id("Source"), ["a", "b"])
    _deck, payload = export_deck(col, "Source", monkeypatch)

    def explode(*args, **kwargs):
        raise RuntimeError("simulated disk failure")

    monkeypatch.setattr(Note, "bulk_add_notes", explode)

    with pytest.raises(ImportError, match="simulated disk failure"):
        import_deck(target_col, payload, monkeypatch)

    assert _counts(target_col) == (0, 0)
    temp_decks = [
        entry.name
        for entry in target_col.decks.all_names_and_ids()
        if entry.name.startswith("_ankicollab_import_")
    ]
    assert temp_decks == []


def test_related_twin_migration_preserves_local_fields(col, target_col, monkeypatch):
    remote_model = make_notetype(col, "Twin", ["A", "B"], uuid="remote-twin-e2e")
    source_note = add_note(
        col, remote_model, col.decks.id("Source"), ["remote-a", "remote-b"]
    )
    _deck, payload = export_deck(col, "Source", monkeypatch)

    local_model = make_notetype(
        target_col, "Twin", ["X", "Y", "Custom"], uuid="local-twin-e2e"
    )
    local_note = target_col.new_note(local_model)
    local_note.guid = source_note.guid
    local_note.fields = ["x-local", "y-local", "custom-local"]
    target_col.add_note(local_note, target_col.decks.id("Local"))

    import_deck(target_col, payload, monkeypatch)

    target_note_id = _note_id_by_guid(target_col, source_note.guid)
    assert target_note_id == local_note.id
    assert values_by_name(target_col, target_note_id) == {
        "A": "remote-a",
        "B": "remote-b",
        "X": "x-local",
        "Y": "y-local",
        "Custom": "custom-local",
    }
    # The user's original local notetype is not destroyed.
    assert target_col.models.by_name("Twin") is not None
    assert target_col.models.by_name("Twin (AnkiCollab)") is not None


def _serialized_note_count(node):
    return len(node.get("notes", [])) + sum(
        _serialized_note_count(child) for child in node.get("children", [])
    )


def test_card_scheduling_survives_an_import_update(col, target_col, monkeypatch):
    notetype = make_notetype(col, "Scheduling E2E", ["A", "B"], uuid="sched-e2e")
    source_note = add_note(col, notetype, col.decks.id("Source"), ["a1", "b1"])
    _deck, payload = export_deck(col, "Source", monkeypatch)
    import_deck(target_col, payload, monkeypatch)

    target_id = _note_id_by_guid(target_col, source_note.guid)
    card_id = target_col.get_note(target_id).card_ids()[0]
    card = target_col.get_card(card_id)
    card.type = 2
    card.queue = 2
    card.due = 22_222
    card.ivl = 11
    card.factor = 2_600
    card.reps = 4
    card.lapses = 2
    target_col.update_card(card)

    _write_note_by_name(col, source_note.id, {"A": "a2", "B": "b1"})
    _deck, payload2 = export_deck(col, "Source", monkeypatch)
    import_deck(target_col, payload2, monkeypatch)

    after = target_col.get_card(card_id)
    assert (after.type, after.queue) == (2, 2)
    assert (after.due, after.ivl, after.factor) == (22_222, 11, 2_600)
    assert (after.reps, after.lapses) == (4, 2)


def test_duplicate_guid_occurrences_in_a_payload_import_once(
    col, target_col, monkeypatch
):
    import copy

    notetype = make_notetype(col, "Legacy Duplicates", ["A"], uuid="dup-e2e")
    add_note(col, notetype, col.decks.id("Source"), ["a"])
    _deck, payload = export_deck(col, "Source", monkeypatch)

    duplicate = copy.deepcopy(payload["notes"][0])
    payload["children"].append(
        {
            "name": "Source::Duplicate",
            "crowdanki_uuid": "duplicate-deck-e2e",
            "id": 999,
            "notes": [duplicate],
            "children": [],
            "note_models": [],
            "deck_configurations": [],
        }
    )

    import_deck(target_col, payload, monkeypatch)

    assert _counts(target_col) == (1, 1)


def test_multi_deck_cards_export_and_import_at_scale(col, target_col, monkeypatch):
    """The reported shape: 118 notes x 6 cards spread over 6 decks."""
    notetype = make_notetype(col, "Scale", ["A", "B"], uuid="scale-e2e")
    for index in range(2, 7):
        _add_second_card_template(col, notetype["id"], index)
    notetype = col.models.get(notetype["id"])

    col.decks.id("Scale Source")
    child_decks = [col.decks.id(f"Scale Source::Deck {index}") for index in range(6)]
    notes = []
    for index in range(118):
        note = add_note(col, notetype, child_decks[0], [f"a{index}", f"b{index}"])
        card_ids = list(note.card_ids())
        assert len(card_ids) == 6
        for card_id, deck_id in zip(card_ids, child_decks):
            col.set_deck([card_id], deck_id)
        notes.append(note)

    _deck, payload = export_deck(col, "Scale Source", monkeypatch)
    assert _serialized_note_count(payload) == 118

    import_deck(target_col, payload, monkeypatch)
    assert _counts(target_col) == (118, 708)

    import_deck(target_col, payload, monkeypatch)
    assert _counts(target_col) == (118, 708)

    sample_id = _note_id_by_guid(target_col, notes[57].guid)
    assert values_by_name(target_col, sample_id) == {
        "A": "a57",
        "B": "b57",
    }
