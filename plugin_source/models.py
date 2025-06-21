from __future__ import annotations
from msgspec import Struct
from typing import List

class NoteModelFieldInfo(Struct):
    id: int
    name: str
    protected: bool

class NoteModel(Struct):
    id: int
    fields: List[NoteModelFieldInfo]
    name: str

# class NotetypeField(Struct):
#     description: str
#     font: str
#     id: Optional[int]
#     name: str
#     ord: int
#     rtl: bool
#     size: int
#     sticky: bool
#     tag: Optional[int]
#
# class CardRequirement(Struct):
#     card_ord: int
#     kind: str
#     field_ords: List[int]
#
# class NotetypeTemplate(Struct):
#     afmt: str
#     bafmt: str
#     bfont: str
#     bqfmt: str
#     bsize: int
#     id: Optional[int]
#     name: str
#     ord: int
#     qfmt: str
#
#
# class Notetype(Struct):
#     crowdanki_uuid: str
#     css: str
#     flds: List[NotetypeField]
#     latexPost: str
#     latexPre: str
#     name: str
#     originalStockKind: Optional[int]
#     req: List[CardRequirement]
#     sortf: int
#     tmpls: List[NotetypeTemplate]
#     _type: int
#
# class Note(Struct):
#     fields: List[str]
#     guid: str
#     note_model_uuid: str
#     tags: List[str]
#
# class AnkiDeck(Struct):
#     crowdanki_uuid: str
#     children: List[AnkiDeck]
#     desc: str
#     name: str
#     note_models: Optional[List[Notetype]]
#     notes: List[Note]

class UpdateInfoResponse(Struct):
    protected_fields: List[NoteModel]
    deck: dict
    changelog: str
    deck_hash: str
    optional_tags: List[str]
    deleted_notes: List[str]
    stats_enabled: bool
