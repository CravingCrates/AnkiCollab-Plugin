"""Randomized end-to-end schema/protection scenarios.

Each case runs two real exports and two real imports through ``deck_initializer``
and ``Deck``: version 1 establishes a local notetype, version 2 reorders/adds/
removes fields, and a random subset of existing fields is protected by a local
tag before the update.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

from anki.collection import Collection

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import (  # noqa: E402
    add_note,
    export_deck,
    import_deck,
    make_notetype,
    values_by_name,
)

POOL = ["Anchor"] + [f"Field {i}" for i in range(8)] + [f"Local {i}" for i in range(3)]


def _random_shape(rng):
    remote = ["Anchor"] + rng.sample(
        [name for name in POOL if name != "Anchor"], rng.randint(0, 5)
    )
    local = list(remote)
    if len(local) > 1:
        removable = [name for name in local if name != "Anchor"]
        if removable:
            for name in rng.sample(removable, rng.randint(0, len(removable))):
                local.remove(name)
    extras = [name for name in POOL if name not in remote]
    for name in rng.sample(extras, rng.randint(0, 2)):
        local.insert(rng.randint(0, len(local)), name)
    # The source template references the first local field, so keep the anchor
    # first; every other field still gets a random order.
    local.remove("Anchor")
    rng.shuffle(local)
    local.insert(0, "Anchor")
    return remote, local


def _set_fields(collection, notetype_id, names):
    notetype = collection.models.get(notetype_id)
    existing = {field["name"]: field for field in notetype["flds"]}
    fields = []
    for name in names:
        field = existing.get(name) or collection.models.new_field(name)
        fields.append(field)
    for index, field in enumerate(fields):
        field["ord"] = index
    notetype["flds"] = fields
    collection.models.update_dict(notetype)


def _write_note(collection, note_id, values):
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


def _note_id(collection, guid):
    return collection.db.scalar("select id from notes where guid = ?", guid)


def test_random_schema_and_protection_roundtrips(tmp_path, monkeypatch):
    rng = random.Random(20261024)
    source = Collection(str(tmp_path / "source.anki2"))
    target = Collection(str(tmp_path / "target.anki2"))
    try:
        for case in range(8):
            remote_names, local_names = _random_shape(rng)
            model_uuid = f"fuzz-e2e-{case}"
            model_name = f"Fuzz E2E {case}"
            deck_name = f"Fuzz Source {case}"

            notetype = make_notetype(source, model_name, local_names, uuid=model_uuid)
            local_values = {name: f"case{case}-local-{name}" for name in local_names}
            guid = f"fuzz-e2e-guid-{case}"
            note = add_note(
                source,
                notetype,
                source.decks.id(deck_name),
                [local_values[name] for name in local_names],
                guid=guid,
            )
            _deck, payload_v1 = export_deck(source, deck_name, monkeypatch)
            import_deck(target, payload_v1, monkeypatch)

            target_note_id = _note_id(target, guid)
            assert target_note_id is not None

            existing_remote = [name for name in remote_names if name in local_names]
            protected = rng.sample(
                existing_remote, rng.randint(0, len(existing_remote))
            )
            tags = [
                f"AnkiCollab_Protect::{name.replace(' ', '_')}" for name in protected
            ]
            _write_note_and_tags(target, target_note_id, local_values, tags)

            _set_fields(source, notetype["id"], remote_names)
            remote_values = {name: f"case{case}-remote-{name}" for name in remote_names}
            _write_note(source, note.id, remote_values)
            _deck, payload_v2 = export_deck(source, deck_name, monkeypatch)
            import_deck(target, payload_v2, monkeypatch)

            local_only = [name for name in local_names if name not in set(remote_names)]
            expected = {}
            for name in remote_names:
                if name in protected and name in local_names:
                    expected[name] = local_values[name]
                else:
                    expected[name] = remote_values[name]
            for name in local_only:
                expected[name] = local_values[name]

            assert values_by_name(target, target_note_id) == expected

            # The same update is idempotent.
            import_deck(target, payload_v2, monkeypatch)
            assert values_by_name(target, target_note_id) == expected
    finally:
        source.close()
        target.close()
