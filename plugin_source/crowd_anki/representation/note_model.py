from collections import namedtuple
import copy
import logging
from typing import Optional, List, Dict, Any, Set, Tuple

from anki.collection import Collection
from anki.models import NotetypeDict, NotetypeId
from .json_serializable import JsonSerializableAnkiDict
from ..utils import utils
from ..utils.uuid import UuidFetcher

logger = logging.getLogger("ankicollab")


class NoteModel(JsonSerializableAnkiDict):
    ModelMap = namedtuple("ModelMap", ["field_map", "template_map"])
    export_filter_set = JsonSerializableAnkiDict.export_filter_set | \
                        {"did"  # uuid
                         }

    def __init__(self, anki_model=None):
        super(NoteModel, self).__init__(anki_model)
        # Ensure anki_dict is properly initialized
        if self.anki_dict is None:
            self.anki_dict = {}

    @classmethod
    def from_collection(cls, collection, entity_id):
        """
        Creates a NoteModel from an existing model in the collection.
        
        Args:
            collection: The Anki collection
            entity_id: The model ID (kept as entity_id for base class compatibility)
        """
        anki_dict = collection.models.get(entity_id)
        note_model = NoteModel(anki_dict)
        note_model._update_fields()
        return note_model

    def save_to_collection(self, collection: Collection) -> Tuple[Optional[NotetypeDict], Optional[List[int]]]:
        """
        Saves the note model to the Anki collection using modern, robust approaches.
        
        Args:
            collection: The Anki collection to save to
            
        Returns:
            Tuple[Optional[NotetypeDict], Optional[List[int]]]: (note_type, field_mapping)
            - note_type: The updated notetype dictionary if changes were made, None otherwise
            - field_mapping: List mapping new field indices to old field indices 
                           (only returned if notetype was updated, None otherwise)
        """
        try:
            existing_model_dict = UuidFetcher(collection).get_model(self.get_uuid())
            logger.info(f"Saving notetype {self.anki_dict.get('name', 'unknown')} with UUID {self.get_uuid()}, existing model: {existing_model_dict is not None}")
            if existing_model_dict:
                return self._update_existing_notetype(collection, existing_model_dict)
            else:
                new_notetype = self._create_new_notetype(collection)
                return new_notetype, None
                
        except Exception as e:
            logger.error(f"Failed to save notetype {self.anki_dict.get('name', 'unknown')}: {e}")
            return None, None

    def _update_existing_notetype(self, collection: Collection, existing_model_dict: NotetypeDict) -> Tuple[Optional[NotetypeDict], Optional[List[int]]]:
        """
        Updates an existing notetype with comprehensive change detection and field mapping.
        
        Returns:
            Tuple[Optional[NotetypeDict], Optional[List[int]]]: (note_type, field_mapping)
            - note_type: The updated notetype dictionary if changes were made, None otherwise
            - field_mapping: List mapping new field indices to old field indices if fields changed or 1:1 mapping 
        """
        try:
            note_type_name = self.anki_dict.get("name", "").lower()
            should_preserve_templates = "projektanki" in note_type_name
            
            # Check if any changes are actually needed
            changes_needed = self._detect_changes_needed(existing_model_dict, should_preserve_templates)
            
            if not changes_needed:
                return None, list(range(len(existing_model_dict["flds"])))
            
            # Capture field mapping BEFORE updating the notetype
            trivial_mapping = False
            field_mapping = None
            if self._fields_need_update(existing_model_dict["flds"], self.anki_dict["flds"]):
                # Build the field mapping from old to new structure
                field_mapping = self._build_intelligent_field_map(existing_model_dict, self.anki_dict)
                logger.info(f"Generated field mapping for {existing_model_dict['name']}: {field_mapping}")
            else:
                trivial_mapping = True
                logger.info(f"No field changes detected for {existing_model_dict['name']}, using trivial mapping")
            
            # Create updated notetype dict
            updated_notetype = copy.deepcopy(existing_model_dict)
            
            # Handle field updates with careful content preservation
            if field_mapping is not None:
                updated_fields = self._merge_fields_safely(existing_model_dict["flds"], self.anki_dict["flds"])
                updated_notetype["flds"] = updated_fields
                logger.info(f"Updated fields for notetype {existing_model_dict['name']}")
                logger.info(f"New field order: {[f['name'] for f in updated_fields]}")
            
            # Handle template and CSS updates based on type
            if should_preserve_templates:
                # For projektanki: keep templates but allow minor updates
                logger.info(f"Preserving templates for projektanki notetype {existing_model_dict['name']}")
            else:
                # For other notetypes: completely overwrite templates and CSS
                if "tmpls" in self.anki_dict:
                    updated_notetype["tmpls"] = copy.deepcopy(self.anki_dict["tmpls"])
                if "css" in self.anki_dict:
                    updated_notetype["css"] = self.anki_dict["css"]
                logger.info(f"Updated templates and CSS for notetype {existing_model_dict['name']}")
            
            # Update other properties (name, etc.) while preserving ID
            for key, value in self.anki_dict.items():
                if key not in ["id", "flds", "tmpls", "css"]:
                        updated_notetype[key] = value
            
            # Apply the update using modern API
            collection.models.update_dict(updated_notetype)
            
            self.anki_dict["id"] = updated_notetype["id"]
            
            if trivial_mapping and not field_mapping:
                field_mapping = list(range(len(updated_notetype["flds"])))
                
            logger.info(f"Successfully updated notetype {updated_notetype['name']} (ID: {updated_notetype['id']})")
            return updated_notetype, field_mapping
            
        except Exception as e:
            logger.error(f"Failed to update existing notetype: {e}")
            return None, None

    def _create_new_notetype(self, collection: Collection) -> Optional[NotetypeDict]:
        """
        Creates a new notetype in the collection.
        
        Returns:
            Optional[NotetypeDict]: The created notetype dictionary, or None if creation failed
        """
        try:
            # Ensure the notetype has a UUID
            self._update_fields()
                            
            note_model_dict = UuidFetcher(collection).get_model(self.get_uuid()) or \
                          collection.models.new(self.anki_dict["name"])

            new_model = note_model_dict["id"] == 0

            if new_model:
                self.anki_dict = utils.merge_dicts(note_model_dict, self.anki_dict)
                self.anki_dict["id"] = 0  # Force ID to 0 for new models
                collection.models.add(self.anki_dict)
                
                # Ensure the ID is properly set after adding the model
                if "id" not in self.anki_dict or self.anki_dict["id"] == 0:
                    # Fetch the model again to get the assigned ID
                    saved_model = collection.models.by_name(self.anki_dict["name"])
                    if saved_model:
                        self.anki_dict["id"] = saved_model["id"]
                        logger.info(f"Assigned ID {saved_model['id']} to new notetype '{self.anki_dict['name']}'")
                        
                return self.anki_dict
            else:
                # Model already exists, just update our dict and return it
                self.anki_dict = note_model_dict
                return self.anki_dict
            
        except Exception as e:
            logger.error(f"Failed to create new notetype: {e}")
            return None

    def _detect_changes_needed(self, existing_model: NotetypeDict, preserve_templates: bool) -> bool:
        """
        Detects if any changes are needed between existing and new notetype.
        """
        # Check name changes
        if existing_model.get("name") != self.anki_dict.get("name"):
            logger.info(f"Change detection found: name changed from '{existing_model.get('name')}' to '{self.anki_dict.get('name')}'")
            return True
        
        # Check field changes
        if self._fields_need_update(existing_model.get("flds", []), self.anki_dict.get("flds", [])):
            logger.info("Change detection found: fields changed")
            return True
        
        # Check template/CSS changes (only for non-projektanki)
        if not preserve_templates:
            if existing_model.get("css") != self.anki_dict.get("css"):
                return True
            
            existing_templates = existing_model.get("tmpls", [])
            new_templates = self.anki_dict.get("tmpls", [])
            if len(existing_templates) != len(new_templates):
                return True
            
            for i, (existing_tmpl, new_tmpl) in enumerate(zip(existing_templates, new_templates)):
                for key in ["qfmt", "afmt", "name"]:
                    if existing_tmpl.get(key) != new_tmpl.get(key):
                        return True
        
        return False

    def _fields_need_update(self, existing_fields: List[Dict], new_fields: List[Dict]) -> bool:
        """
        Checks if field updates are needed.
        """
        if len(existing_fields) != len(new_fields):
            return True
        
        existing_field_names = [f["name"] for f in existing_fields]
        new_field_names = [f["name"] for f in new_fields]
        
        logger.info(f"Comparing fields: existing={existing_field_names}, new={new_field_names}")
        
        # Check if field names or order changed
        if existing_field_names != new_field_names:
            return True
        
        # Check if any field properties changed (except ord which we manage)
        for existing_field, new_field in zip(existing_fields, new_fields):
            for key in ["name", "sticky", "rtl", "font", "size"]:
                if existing_field.get(key) != new_field.get(key):
                    return True
        
        return False

    def _merge_fields_safely(self, existing_fields: List[Dict], new_fields: List[Dict]) -> List[Dict]:
        """
        Safely merges fields while ensuring remote field order is preserved and local-only fields are at the end.
        
        CRITICAL: Remote field order must be preserved to ensure correct content mapping.
        Local-only fields are added at the end to maintain cross-user compatibility.
        """
        logger.info(f"Merging fields: {len(existing_fields)} existing -> {len(new_fields)} new")
        
        # Create a deep copy of new fields to work with (preserves remote order)
        merged_fields = copy.deepcopy(new_fields)
        
        # Create mapping of existing field names to their properties
        existing_field_map = {
            field["name"].lower(): field for field in existing_fields
        }
        
        # Phase 1: Process remote fields in their original order
        for i, field in enumerate(merged_fields):
            # Set correct ord based on position in remote order
            field["ord"] = i
            
            field_name_lower = field["name"].lower()
            if field_name_lower in existing_field_map:
                # Field exists locally - preserve local properties like font, size, etc.
                existing_field = existing_field_map[field_name_lower]
                for key in ["sticky", "rtl", "font", "size"]:
                    if key in existing_field:
                        field[key] = existing_field[key]
                logger.info(f"Merged remote field '{field['name']}' at position {i} with local properties")
            else:
                logger.info(f"Added new remote field '{field['name']}' at position {i}")
        
        # Phase 2: Add local-only fields at the END (critical constraint)
        new_field_names = {field["name"].lower() for field in merged_fields}
        local_only_fields = [
            field for field in existing_fields
            if field["name"].lower() not in new_field_names
        ]
        
        if local_only_fields:
            logger.info(f"Preserving {len(local_only_fields)} local-only fields at the end")
            next_ord = len(merged_fields)
            for local_field in local_only_fields:
                local_field_copy = copy.deepcopy(local_field)
                local_field_copy["ord"] = next_ord
                merged_fields.append(local_field_copy)
                logger.info(f"Added local-only field '{local_field_copy['name']}' at position {next_ord}")
                next_ord += 1
        
        # Validation: Ensure remote fields are in correct order
        for i, field in enumerate(merged_fields[:len(new_fields)]):
            expected_name = new_fields[i]["name"]
            if field["name"] != expected_name:
                raise ValueError(f"Field order corruption: expected '{expected_name}' at position {i}, got '{field['name']}'")
        
        logger.info(f"Field merge completed: {len(merged_fields)} total fields (remote order preserved)")
        return merged_fields

    def can_merge_with(self, other_notetype: 'NoteModel') -> bool:
        """
        Determines if this notetype can be safely merged with another (for duplicate handling).
        """
        if not isinstance(other_notetype, NoteModel):
            return False
        
        # Check if field structures are compatible
        self_fields = [f["name"] for f in self.anki_dict.get("flds", [])]
        other_fields = [f["name"] for f in other_notetype.anki_dict.get("flds", [])]
        
        if set(self_fields) != set(other_fields):
            return False
        
        # Check if template structures are compatible
        self_templates = [t["name"] for t in self.anki_dict.get("tmpls", [])]
        other_templates = [t["name"] for t in other_notetype.anki_dict.get("tmpls", [])]
        
        if set(self_templates) != set(other_templates):
            return False
        
        return True

    @staticmethod
    def find_duplicates_by_structure(notemodels: List['NoteModel']) -> List[List['NoteModel']]:
        """
        Groups notemodels by structure to identify potential duplicates.
        """
        structure_groups = {}
        
        for notemodel in notemodels:
            field_names = tuple(sorted(f["name"] for f in notemodel.anki_dict.get("flds", [])))
            template_names = tuple(sorted(t["name"] for t in notemodel.anki_dict.get("tmpls", [])))
            structure_key = (field_names, template_names)
            
            if structure_key not in structure_groups:
                structure_groups[structure_key] = []
            structure_groups[structure_key].append(notemodel)
        
        # Return only groups with duplicates
        return [group for group in structure_groups.values() if len(group) > 1]

    def get_field_names(self) -> List[str]:
        """Returns list of field names in order."""
        return [field["name"] for field in self.anki_dict.get("flds", [])]

    def get_template_names(self) -> List[str]:
        """Returns list of template names in order."""
        return [template["name"] for template in self.anki_dict.get("tmpls", [])]

    def __str__(self) -> str:
        return f"NoteModel(name='{self.anki_dict.get('name', 'unknown')}', uuid='{self.get_uuid()}', fields={len(self.anki_dict.get('flds', []))})"

    def _build_intelligent_field_map(self, old_notetype: NotetypeDict, new_notetype: NotetypeDict) -> List[int]:
        """
        Builds an intelligent field mapping that preserves content by matching field names.
        Falls back to positional mapping for unmatched fields.
        
        Args:
            old_notetype: The existing notetype (before update)
            new_notetype: The new notetype structure (from remote)
        
        Returns:
            List mapping each new field index to old field index (or None for new fields)
        """
        old_fields = old_notetype.get("flds", [])
        new_fields = new_notetype.get("flds", [])
                
        # Create name-to-index mappings (case-insensitive for robustness)
        old_field_map = {field["name"].lower(): i for i, field in enumerate(old_fields)}
        
        field_map = []
        unmatched_old_indices = set(range(len(old_fields)))
        
        # Phase 1: Map by exact field name matches
        for i, new_field in enumerate(new_fields):
            new_field_name = new_field["name"].lower()
            
            if new_field_name in old_field_map:
                old_index = old_field_map[new_field_name]
                field_map.append(old_index)
                unmatched_old_indices.discard(old_index)
            else:
                # New field coming in from remote
                field_map.append(None)
        
        # Phase 2: Handle orphaned old fields (data would be lost)
        remaining_unmatched = unmatched_old_indices - set(field_map)
        if remaining_unmatched:
            old_field_names = [old_fields[i]["name"] for i in remaining_unmatched]
            logger.info(f"Orphaned old fields not mapped: {remaining_unmatched} -> {old_field_names}")
            
            # Append unmatched old fields with their indices at the end
            for old_index in remaining_unmatched:
                field_map.append(old_index)
                
        return field_map
