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

# decoder = json.Decoder(type=List[UpdateInfoResponse])
# x = decoder.decode(b'[{"protected_fields":[],"deck":{"crowdanki_uuid":"904dc43a-451d-11f0-8d33-38dead5cad9f","children":[],"desc":"","name":"Dev","note_models":[{"crowdanki_uuid":"d7aa98a3-24fb-11f0-bc1a-38dead5cad9f","css":".card {\\n    font-family: arial;\\n    font-size: 20px;\\n    text-align: center;\\n    color: black;\\n    background-color: white;\\n}\\n","flds":[{"description":"","font":"Arial","id":5739662822009796422,"name":"Front","ord":0,"rtl":false,"size":20,"sticky":false,"tag":0},{"description":"","font":"Arial","id":5034123465608242268,"name":"Back","ord":1,"rtl":false,"size":20,"sticky":false,"tag":0}],"latexPost":"\\\\end{document}","latexPre":"\\\\documentclass[12pt]{article}\\n\\\\special{papersize=3in,5in}\\n\\\\usepackage[utf8]{inputenc}\\n\\\\usepackage{amssymb,amsmath}\\n\\\\pagestyle{empty}\\n\\\\setlength{\\\\parindent}{0in}\\n\\\\begin{document}\\n","latexsvg":false,"name":"Basic-59c91","originalStockKind":1,"req":[{"card_ord":0,"kind":"any","field_ords":[0]}],"sortf":0,"tmpls":[{"afmt":"{{FrontSide}}\\n\\n<hr id=answer>\\n\\n{{Back}}","bafmt":"","bfont":"","bqfmt":"","bsize":0,"id":2048077723559359642,"name":"Card 1","ord":0,"qfmt":"{{Front}}"}],"type":0}],"notes":[{"fields":["Test2",""],"guid":"rZp|N=m*|J","note_model_uuid":"d7aa98a3-24fb-11f0-bc1a-38dead5cad9f","tags":[]}]},"changelog":"","deck_hash":"bluebird-illinois-juliet-stream-helium-november","optional_tags":["Test"],"deleted_notes":[],"stats_enabled":true}]')
# print(x[0].stats_enabled)
