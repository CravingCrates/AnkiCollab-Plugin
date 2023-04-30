# This file is part of this GitHub repository: https://github.com/abdnh/anki-media-exporter
# All Credit goes to abdnh

"""Media Exporter classes."""

from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Generator

from anki.collection import Collection, SearchNode
from anki.decks import DeckId
from anki.notes import Note


def get_note_media(col: Collection, note: Note, field: str | None) -> list[str]:
    "Return a list of used media files in `note`."
    if field:
        flds = note[field]
    else:
        flds = "".join(note.fields)
    return col.media.files_in_str(note.mid, flds)


class MediaExporter(ABC):
    """Abstract media exporter."""

    col: Collection
    field: str
    exts: set | None = None

    @abstractmethod
    def file_lists(self) -> Generator[list[str], None, None]:
        """Return a generator that yields a list of media files for each note that should be imported."""

    def export(
        self, folder: Path | str
    ) -> Generator[tuple[int, list[str]], None, None]:
        """
        Export media files in `self.did` to `folder`,
        including only files that has extensions in `self.exts` if it's not None.
        Returns a generator that yields the total media files exported so far and filenames as they are exported.
        """

        media_dir = self.col.media.dir()
        seen = set()
        exported = set()
        for filenames in self.file_lists():
            for filename in filenames:
                if filename in seen:
                    continue
                seen.add(filename)
                if (
                    self.exts is not None
                    and os.path.splitext(filename)[1][1:] not in self.exts
                ):
                    continue
                src_path = os.path.join(media_dir, filename)
                if not os.path.exists(src_path):
                    continue
                dest_path = os.path.join(folder, filename)
                shutil.copyfile(src_path, dest_path)
                exported.add(filename)
            yield len(exported), filenames


class NoteMediaExporter(MediaExporter):
    """Exporter for a list of notes."""

    def __init__(
        self,
        col: Collection,
        notes: list[Note],
        field: str | None = None,
        exts: set | None = None,
    ):
        self.col = col
        self.notes = notes
        self.field = field
        self.exts = exts

    def file_lists(self) -> Generator[list[str], None, None]:
        "Return a generator that yields a list of media files for each note in `self.notes`"

        for note in self.notes:
            yield get_note_media(self.col, note, self.field)


class DeckMediaExporter(MediaExporter):
    "Exporter for all media in a deck."

    def __init__(
        self,
        col: Collection,
        did: DeckId,
        field: str | None = None,
        exts: set | None = None,
    ):
        self.col = col
        self.did = did
        self.field = field
        self.exts = exts

    def file_lists(self) -> Generator[list[str], None, None]:
        "Return a generator that yields a list of media files for each note in the deck with the ID `self.did`"
        search_params = [SearchNode(deck=self.col.decks.name(self.did))]
        if self.field:
            search_params.append(SearchNode(field_name=self.field))
        search = self.col.build_search_string(*search_params)
        for nid in self.col.find_notes(search):
            note = self.col.get_note(nid)
            yield get_note_media(self.col, note, self.field)
