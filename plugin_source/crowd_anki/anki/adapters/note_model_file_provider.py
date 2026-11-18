import os
import re

from dataclasses import dataclass, field
from functional import seq
from typing import Any, Iterable, Set, Tuple

from anki.utils import point_version

from .file_provider import FileProvider

ANKI_VERSION_23_10_00 = 231000

UNDERSCORED_CSS_IMPORTS = re.compile(
    r"""(?xi)(?:@import\s+"(_[^"]*\.css)"|'(_[^']*\.css)')|(?:url\(\s*"(_[^"]+)"|'(_[^']+)'|(_.+?)\s*\))"""
)

UNDERSCORED_REFERENCES = re.compile(
    r"""(?x)\[sound:(_[^]]+)\]|"(_[^"]+)"|'(_[^']+)'|\b(?:src|data)=(_[^ >]+)"""
)

ALLOWED_NOTETYPE_EXTENSIONS = {
    "jpg",
    "jpeg",
    "png",
    "gif",
    "webp",
    "svg",
    "bmp",
    "tif",
    "tiff",
    "mp3",
    "ogg",
    "js",
    "css",
    "ttf",
    "otf",
    "woff",
    "woff2",
}


def is_allowed_notetype_filename(filename: str) -> bool:
    """Validate a notetype static media filename against allowed characters, length, and extensions."""
    if not filename.startswith("_"):
        return False
    if len(filename) < 4 or len(filename) > 255:
        return False
    if "/" in filename or "\\" in filename or ".." in filename:
        return False
    if filename.endswith(".") or filename.endswith(" "):
        return False
    if not all(c.isalnum() or c in ".-_ ()+,%&" for c in filename):
        return False
    if not any(c.isalnum() for c in filename):
        return False
    ext = os.path.splitext(filename)[1].lstrip(".").lower()
    return ext in ALLOWED_NOTETYPE_EXTENSIONS


def extract_static_media_references(
    css: str, templates: Iterable[Tuple[str, str]]
) -> Set[str]:
    """Extract static media references (_*) from CSS and template format strings.
    Matches Anki's exact regex logic and filters candidates through is_allowed_notetype_filename.
    """
    references = set()

    def scan_text(text: str):
        if not text:
            return
        for regex in (UNDERSCORED_CSS_IMPORTS, UNDERSCORED_REFERENCES):
            for match in regex.finditer(text):
                for candidate in match.groups():
                    if candidate:
                        trimmed = candidate.strip()
                        if is_allowed_notetype_filename(trimmed):
                            references.add(trimmed)

    scan_text(css)
    for qfmt, afmt in templates:
        scan_text(qfmt)
        scan_text(afmt)

    return references


@dataclass
class NoteModelFileProvider(FileProvider):
    anki_collection: Any
    model_ids: Iterable[int]
    models: Iterable = field(init=False)

    def __post_init__(self):
        self.models = (
            seq(self.model_ids)
            .map(self.anki_collection.models.get)
            .filter(lambda m: m is not None)
            .to_list()
        )

    def get_files(self, existing_only: bool = True) -> Set[str]:
        referenced = self._referenced_static_media(existing_only=existing_only)
        if existing_only:
            media_dir = self.anki_collection.media.dir()
            existing_files = (
                set(os.listdir(media_dir)) if os.path.exists(media_dir) else set()
            )
            return {
                file_name
                for file_name in referenced
                if is_allowed_notetype_filename(file_name)
                and file_name in existing_files
            }
        return {
            file_name
            for file_name in referenced
            if is_allowed_notetype_filename(file_name)
        }

    def _referenced_static_media(self, existing_only: bool = True) -> Set[str]:
        if point_version() >= ANKI_VERSION_23_10_00:
            extract = self.anki_collection.media.extract_static_media_files
            return {name for model in self.models for name in extract(int(model["id"]))}
        if existing_only:
            media_dir = self.anki_collection.media.dir()
            if not os.path.exists(media_dir):
                return set()
            return {
                file_name
                for file_name in os.listdir(media_dir)
                if file_name.startswith("_") and self.belongs_to_any_model(file_name)
            }
        references = set()
        for model in self.models:
            css = model.get("css", "")
            templates = [
                (t.get("qfmt", ""), t.get("afmt", "")) for t in model.get("tmpls", [])
            ]
            references.update(extract_static_media_references(css, templates))
        return references

    def belongs_to_any_model(self, file_name: str) -> bool:
        return any(_model_references(model, file_name) for model in self.models)


def _model_references(model: dict, file_name: str) -> bool:
    if file_name in model.get("css", ""):
        return True
    for template in model.get("tmpls", []):
        if file_name in template.get("qfmt", "") or file_name in template.get(
            "afmt", ""
        ):
            return True
    return False
