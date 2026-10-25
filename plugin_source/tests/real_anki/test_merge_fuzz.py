"""Randomized merge/import scenarios against the real Anki backend.

The merge rules are simple per field (remote value for remote fields, local
value for local-only fields), but the combinations (reorder / add / remove /
interleaved local fields / re-import) are where offsets go wrong. This walks a
few dozen generated shapes and asserts the only stable property that matters:
each value ends up under its own field *name*, for the imported note and for
untouched notes of the same notetype.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import (  # noqa: E402
    Config,
    add_note,
    col,  # noqa: F401  (pytest fixture)
    import_note,
    make_notetype,
    merge_remote,
    values_by_name,
)

POOL = [f"Field {i}" for i in range(12)] + [f"Local {i}" for i in range(4)]


def _random_shape(rng):
    remote_names = rng.sample(POOL, rng.randint(1, 8))

    local_names = list(remote_names)
    # Drop some remote fields locally: they are re-added by the remote merge.
    if len(local_names) > 1:
        for name in rng.sample(local_names, rng.randint(0, len(local_names) - 1)):
            local_names.remove(name)

    # Add local-only fields, interleaved anywhere (not just at the end).
    extra_pool = [name for name in POOL if name not in remote_names]
    for name in rng.sample(extra_pool, rng.randint(0, 3)):
        local_names.insert(rng.randint(0, len(local_names)), name)

    if not local_names:
        local_names = [rng.choice([name for name in POOL if name not in remote_names])]

    rng.shuffle(local_names)
    return remote_names, local_names


def _expected_local_only(remote_names, local_names):
    remote = set(remote_names)
    return [name for name in local_names if name not in remote]


def test_random_merge_shapes_preserve_values_by_name(col):
    rng = random.Random(20261022)  # deterministic failure reproduction
    deck_id = col.decks.id("Fuzz")
    other_deck_id = col.decks.id("Fuzz unrelated")

    for case in range(40):
        remote_names, local_names = _random_shape(rng)
        notetype = make_notetype(
            col, f"Fuzz {case}", local_names, uuid=f"fuzz-uuid-{case}"
        )
        local_values = {name: f"case{case}-local-{name}" for name in local_names}
        note = add_note(
            col,
            notetype,
            deck_id,
            [local_values[name] for name in local_names],
        )
        untouched = add_note(
            col,
            notetype,
            other_deck_id,
            [f"case{case}-other-{name}" for name in local_names],
        )

        model = merge_remote(col, notetype, remote_names)
        local_only = _expected_local_only(remote_names, local_names)
        assert model.local_only_field_names == local_only

        # Notes outside the imported deck are only touched by the notetype
        # merge, never by the import config/protection logic.
        untouched_values = values_by_name(col, untouched.id)
        assert set(untouched_values) == set(remote_names) | set(local_only)
        for name in local_names:
            assert untouched_values[name] == f"case{case}-other-{name}"
        for name in remote_names:
            if name not in local_names:
                assert untouched_values[name] == ""

        remote_values = {name: f"case{case}-remote-{name}" for name in remote_names}
        import_note(
            col, model, note.id, remote_values=[remote_values[n] for n in remote_names]
        )
        values = values_by_name(col, note.id)

        assert set(values) == set(remote_names) | set(local_only)
        for name in remote_names:
            assert values[name] == remote_values[name]
        for name in local_only:
            assert values[name] == local_values[name]

        # Importing the same note again must not move or duplicate anything.
        import_note(
            col, model, note.id, remote_values=[remote_values[n] for n in remote_names]
        )
        assert values_by_name(col, note.id) == values


def test_random_protected_fields_keep_local_values(col):
    rng = random.Random(7319)
    deck_id = col.decks.id("Fuzz protected")

    for case in range(25):
        remote_names, local_names = _random_shape(rng)
        notetype = make_notetype(
            col, f"Fuzz protected {case}", local_names, uuid=f"fuzz-prot-{case}"
        )
        local_values = {name: f"case{case}-local-{name}" for name in local_names}
        note = add_note(
            col, notetype, deck_id, [local_values[name] for name in local_names]
        )

        model = merge_remote(col, notetype, remote_names)
        remote_values = {name: f"case{case}-remote-{name}" for name in remote_names}
        protected = rng.sample(remote_names, rng.randint(0, len(remote_names)))
        config = Config(
            personal_fields=[(notetype["name"], field) for field in protected]
        )

        import_note(
            col,
            model,
            note.id,
            remote_values=[remote_values[n] for n in remote_names],
            config=config,
        )
        values = values_by_name(col, note.id)

        for name in remote_names:
            if name in protected and name in local_names:
                assert values[name] == local_values[name]
            else:
                # Protection never invents a value for a field the note did not
                # have before the merge (new field) - the imported value stays.
                assert values[name] == remote_values[name]
        for name in _expected_local_only(remote_names, local_names):
            assert values[name] == local_values[name]
