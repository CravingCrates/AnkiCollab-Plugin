"""Notetype identity checks against the real Anki backend.

The deck payload carries notetype ids from the *sender's* collection. They have
no meaning locally; the UUID is the only safe link. A no-op merge must still
replace the payload id with the local one, otherwise note processing writes a
foreign ``mid``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import (  # noqa: E402
    col,  # noqa: F401  (pytest fixture)
    make_notetype,
    values_by_name,
    add_note,
)

from ankicollab_under_test.crowd_anki.representation.note_model import (  # noqa: E402
    NoteModel,
)


def test_no_change_save_replaces_the_payload_id_with_the_local_id(col):
    notetype = make_notetype(col, "Identity", ["A", "B"], uuid="identity-uuid")
    note = add_note(col, notetype, col.decks.id("Identity"), ["a", "b"])

    remote = dict(notetype)
    remote["id"] = 987_654_321  # sender-side id, does not exist locally
    model = NoteModel(remote)

    updated, mapping = model.save_to_collection(col)

    assert updated is None
    assert mapping == [0, 1]
    assert model.anki_dict["id"] == notetype["id"]
    assert values_by_name(col, note.id) == {"A": "a", "B": "b"}
