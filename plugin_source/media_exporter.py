# This file is part of this GitHub repository: https://github.com/abdnh/anki-media-exporter
# All Credit goes to abdnh

"""Media Exporter classes."""

from __future__ import annotations

import os
import re
from typing import List
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Generator

from anki.collection import Collection, SearchNode
from anki.decks import DeckId
from anki.notes import Note
from anki.models import NotetypeDict, TemplateDict 


def gather_media_from_css(css: str) -> List[str]:
    # Regular expression taken from the anki repo https://github.com/ankitects/anki/blob/c2b1ab5eb06935e93aea6af09a224a99f4b971f0/rslib/src/text.rs#L151
    underscored_css_imports_pattern = re.compile(r"""(?xi)
        (?:@import\s+           # import statement with a bare
            "(_[^"]*.css)"      # double quoted
            |                   # or
            '(_[^']*.css)'      # single quoted css filename
        )
        |
        (?:url\(\s*             # a url function with a
            "(_[^"]+)"          # double quoted
            |                   # or
            '(_[^']+)'          # single quoted
            |                   # or
            (_.+)               # unquoted filename
        \s*\))
    """)

    media_files = []

    matches = underscored_css_imports_pattern.findall(css)
    for match in matches:
        for group in match:
            if group and group.startswith("_"):
                media_files.append(group)

    return media_files

def gather_media_from_template_side(template_side: str) -> List[str]:
    # Regular expression taken from the anki repo https://github.com/ankitects/anki/blob/c2b1ab5eb06935e93aea6af09a224a99f4b971f0/rslib/src/text.rs#L169
    underscored_references_pattern = re.compile(r"""(?x)
        \[sound:(_[^]]+)\]  # a filename in an Anki sound tag
        |
        "(_[^"]+)"          # a double quoted
        |
        '(_[^']+)'          # single quoted string
        |
        \b(?:src|data)      # a 'src' or 'data' attribute
        =
        (_[^ >]+)           # an unquoted value
    """)

    media_files = []

    matches = underscored_references_pattern.findall(template_side)
    for match in matches:
        for group in match:
            if group and group.startswith("_"):
                media_files.append(group)

    return media_files

def gather_media_from_template(template: TemplateDict) -> List[str]:
    question_template = template['qfmt']
    answer_template = template['afmt']

    media_files = gather_media_from_template_side(question_template)
    media_files.extend(gather_media_from_template_side(answer_template))

    return media_files

def get_note_media(col: Collection, note: Note, field: str | None) -> list[str]:
    "Return a list of used media files in `note`."
    if field:
        flds = note[field]
    else:
        flds = "".join(note.fields)
    return col.media.files_in_str(note.mid, flds)

def get_notetype_media(notetype: NotetypeDict) -> List[str]:
    css_media = gather_media_from_css(notetype['css'])

    template_media = []
    for template in notetype['tmpls']:
        template_media.extend(gather_media_from_template(template))

    return css_media + template_media


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
            
    def get_list_of_media(self):
        """
        Return a list of media files used by the deck.
        """
        seen = set()
        for filenames in self.file_lists():
            for filename in filenames:
                if filename in seen:
                    continue
                seen.add(filename)
        return seen
        


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

        notetypes_in_selection = set()
        for note in self.notes:
            notetypes_in_selection.add(note.note_type()['name'])
            yield get_note_media(self.col, note, self.field)

        get_notetype_by_name = getattr(self.col.models, "by_name", None)
        if not get_notetype_by_name:
            get_notetype_by_name = self.col.models.byName  # type: ignore[attr-defined]
            
        for notetype_name in notetypes_in_selection:
            notetype = get_notetype_by_name(notetype_name)
            yield get_notetype_media(notetype)

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
        
        notetypes_in_deck = set()
        for nid in self.col.find_notes(search):
            note = self.col.get_note(nid)
            notetypes_in_deck.add(note.note_type()['name'])
            yield get_note_media(self.col, note, self.field)

        get_notetype_by_name = getattr(self.col.models, "by_name", None)
        if not get_notetype_by_name:
            get_notetype_by_name = self.col.models.byName  # type: ignore[attr-defined]
            
        for notetype_name in notetypes_in_deck:
            notetype = get_notetype_by_name(notetype_name)
            yield get_notetype_media(notetype)
