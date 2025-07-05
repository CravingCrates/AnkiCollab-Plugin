from collections import namedtuple

from anki import Collection
from .json_serializable import JsonSerializableAnkiDict
from ..anki.overrides.change_model_dialog import ChangeModelDialog
from ..utils import utils
from ..utils.uuid import UuidFetcher


class NoteModel(JsonSerializableAnkiDict):
    ModelMap = namedtuple("ModelMap", ["field_map", "template_map"])
    export_filter_set = JsonSerializableAnkiDict.export_filter_set | \
                        {"did"  # uuid
                         }

    def __init__(self, anki_model=None):
        super(NoteModel, self).__init__(anki_model)

    @classmethod
    def from_collection(cls, collection, model_id):
        anki_dict = collection.models.get(model_id)
        note_model = NoteModel(anki_dict)
        note_model._update_fields()

        return note_model

    #kinda legacy now
    @staticmethod
    def check_semantically_identical(first_model, second_model):
        field_names = ("flds", "tmpls", )
        keys_by_field = {
            "flds": ["name", "ord"],
            "tmpls": ["name", "ord"]
        }
        for field in field_names:
            # would be nice to allow field reordering and mapping it correctly, but i cba to implement it now
            first_items = sorted(first_model.anki_dict[field], key=lambda x: x.get("ord", 0))
            second_items = sorted(second_model.anki_dict[field], key=lambda x: x.get("ord", 0))
            
            if len(first_items) != len(second_items):
                return False
                
            for first_fld, second_fld in zip(first_items, second_items):
                for key in keys_by_field[field]:
                    if (first_fld.get(key) or 0) != (second_fld.get(key) or 0):
                        return False
        return True

    @staticmethod
    def check_fields_compatible(first_model, second_model):
        """Check if fields are compatible (same names and order) - required for note type updates"""
        first_fields = first_model.anki_dict.get("flds", [])
        second_fields = second_model.anki_dict.get("flds", [])
        
        if len(first_fields) != len(second_fields):
            return False
        
        first_fields = sorted(first_fields, key=lambda x: x.get("ord", 0))
        second_fields = sorted(second_fields, key=lambda x: x.get("ord", 0))
            
        for first_fld, second_fld in zip(first_fields, second_fields):
            if (first_fld.get("name") or "") != (second_fld.get("name") or ""):
                return False
            if (first_fld.get("ord") or 0) != (second_fld.get("ord") or 0):
                return False
        return True

    # See i got complaints that Ankizin users can't use the notetype addon because it gets overwritten
    # by the server version, so we need to preserve user templates for projektanki note types
    def merge_preserving_templates(self, existing_model_dict):
        """Merge server model with existing model, preserving user templates but enforcing server fields"""
        result = existing_model_dict.copy()
        
        # Critical fields that we force from server
        critical_fields = ["flds", "name", "type", "original_stock_kind", "req", "sortf"]
        
        # Update critical fields from server version
        for field in critical_fields:
            if field in self.anki_dict:
                result[field] = self.anki_dict[field]
        
        # Preserve existing templates (tmpls) - don't overwrite user customizations
        # Only update template count if server has more templates
        if "tmpls" in self.anki_dict and "tmpls" in result:
            server_templates = self.anki_dict["tmpls"]
            existing_templates = result["tmpls"]

            # If server has different number of templates, update existing
            if len(server_templates) != len(existing_templates):                
                result["tmpls"] = self.anki_dict["tmpls"]
        elif "tmpls" in self.anki_dict and "tmpls" not in result:
            # If existing model has no templates, use server templates
            result["tmpls"] = self.anki_dict["tmpls"]
        
        return result

    def save_to_collection(self, collection: Collection):
        # Todo regenerate cards on update
        # look into template manipulation in "models"

        note_model_dict = UuidFetcher(collection).get_model(self.get_uuid()) or \
                          collection.models.new(self.anki_dict["name"])

        new_model = note_model_dict["id"] == 0

        if new_model:
            self.anki_dict = utils.merge_dicts(note_model_dict, self.anki_dict)
            collection.models.add(self.anki_dict)
        else:
            # For existing models, check if we should preserve templates
            note_type_name = self.anki_dict.get("name", "").lower()
            should_preserve_templates = "projektanki" in note_type_name
            
            if should_preserve_templates:
                existing_model = NoteModel(note_model_dict)
                if not self.check_fields_compatible(existing_model, self):
                    # Fields are incompatible - user needs to handle this via change model dialog
                    self.anki_dict = utils.merge_dicts(note_model_dict, self.anki_dict)
                    collection.models.update(self.anki_dict)
                    self.update_cards(collection, note_model_dict)
                else:
                    # Fields are compatible - preserve user templates for projektanki
                    self.anki_dict = self.merge_preserving_templates(note_model_dict)
                    collection.models.update(self.anki_dict)
            else:
                # For non-projektanki note types, we force the server version completely
                self.anki_dict = utils.merge_dicts(note_model_dict, self.anki_dict)
                collection.models.update(self.anki_dict)
                self.update_cards(collection, note_model_dict)

    def make_current(self, collection):
        # Sync through setting global "current" model makes me sad too, but it's ingrained on many levels down
        collection.models.set_current(self.anki_dict)
        collection.decks.current()['mid'] = self.anki_dict["id"]

    def update_cards(self, collection, old_model):
        # Check if this is a projektanki note type that should preserve templates
        note_type_name = self.anki_dict.get("name", "").lower()
        should_preserve_templates = "projektanki" in note_type_name
        
        if should_preserve_templates:
            # Only trigger change model dialog if fields are incompatible for projektanki
            # Templates differences are preserved, so we don't need to trigger for template changes
            old_model_obj = NoteModel.from_json(old_model)
            if self.check_fields_compatible(old_model_obj, self):
                return
        else:
            # For non-projektanki note types, we can later change it so it checks templates too
            if self.check_semantically_identical(NoteModel.from_json(old_model), self):
                return
            
        self.make_current(collection)

        old_model["name"] += " *old"

        # todo: check if we are in "ui mode"
        # todo: handle canceled
        # todo: think on "mixed update" handling

        # todo signals instead of direct dialog creation?
        ChangeModelDialog(collection, collection.models.nids(old_model), old_model).exec()
