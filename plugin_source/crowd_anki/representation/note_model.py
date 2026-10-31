from collections import namedtuple
import copy
import logging
import re
from typing import Optional, List, Dict, Any, Set, Tuple

from anki.collection import Collection
from anki.models import ChangeNotetypeRequest, NotetypeDict, NotetypeId
from .json_serializable import JsonSerializableAnkiDict
from ..utils import utils
from ..utils.uuid import UuidFetcher
from ...utils import get_logger

logger = get_logger("ankicollab.note_model")

NOTE_REMAP_CHUNK_SIZE = 1000

# Everything after a comment containing this marker belongs to the user: it
# survives deck updates and is never uploaded with the deck. Either spelling
# of USER_CUSTOMISATION is accepted.
PROTECTED_CONTENT_MARKER = "USER_CUSTOMI[SZ]ATION"

_HTML_MARKER_RE = re.compile(rf"<!--\s*{PROTECTED_CONTENT_MARKER}(?:(?!-->)[\s\S])*-->")
_CSS_MARKER_RE = re.compile(rf"/\*\s*{PROTECTED_CONTENT_MARKER}(?:(?!\*/)[\s\S])*\*/")


def _strip_protected_content(content: str, marker_re: re.Pattern) -> str:
    """Drops everything after the marker comment, keeping the marker itself."""
    match = marker_re.search(content)
    return content[: match.end()] if match else content


def _carry_over_protected_content(
    local: str, incoming: str, marker_re: re.Pattern
) -> str:
    """Returns ``incoming`` with the user's content below the local marker kept."""
    local_match = marker_re.search(local)
    if not local_match:
        return incoming

    incoming_match = marker_re.search(incoming)
    if incoming_match:
        return incoming[: incoming_match.end()] + local[local_match.end() :]

    # The deck dropped its marker; keep the user's marker and content at the end
    if not local[local_match.end() :].strip():
        return incoming
    return incoming.rstrip() + "\n\n" + local[local_match.start() :]


def _carry_over_protected_templates(
    local_templates: List[Dict], incoming_templates: List[Dict]
) -> List[Dict]:
    """Pairs templates by name, falling back to position for renamed ones."""
    local_by_name = {tmpl.get("name"): tmpl for tmpl in local_templates}
    incoming_names = {tmpl.get("name") for tmpl in incoming_templates}

    merged_templates = copy.deepcopy(incoming_templates)
    for i, tmpl in enumerate(merged_templates):
        local_tmpl = local_by_name.get(tmpl.get("name"))
        if (
            local_tmpl is None
            and i < len(local_templates)
            and local_templates[i].get("name") not in incoming_names
        ):
            local_tmpl = local_templates[i]
        if local_tmpl is None:
            continue

        for key in ["qfmt", "afmt"]:
            if isinstance(tmpl.get(key), str) and isinstance(local_tmpl.get(key), str):
                tmpl[key] = _carry_over_protected_content(
                    local_tmpl[key], tmpl[key], _HTML_MARKER_RE
                )
    return merged_templates


class NotetypeSaveError(RuntimeError):
    """A notetype could not be persisted safely.

    Continuing the import without a field mapping would silently move note
    content into the wrong fields, so this is always fatal.
    """


class NoteModel(JsonSerializableAnkiDict):
    ModelMap = namedtuple("ModelMap", ["field_map", "template_map"])

    export_filter_set = JsonSerializableAnkiDict.export_filter_set | {
        "did",  # uuid
        "local_only_field_names",
        "local_field_names_before_merge",
    }
    import_filter_set = JsonSerializableAnkiDict.import_filter_set | {
        "local_only_field_names",
        "local_field_names_before_merge",
    }

    def __init__(self, anki_model=None):
        super(NoteModel, self).__init__(anki_model)
        # Ensure anki_dict is properly initialized
        if self.anki_dict is None:
            self.anki_dict = {}
        self.local_only_field_names: List[str] = []
        self.local_field_names_before_merge: Optional[Set[str]] = None

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

    def save_to_collection(
        self, collection: Collection
    ) -> Tuple[Optional[NotetypeDict], Optional[List[int]]]:
        """
        Saves the note model to the Anki collection using modern, robust approaches.

        Args:
            collection: The Anki collection to save to

        Returns:
            Tuple[Optional[NotetypeDict], Optional[List[int]]]: (note_type, field_mapping)
            - note_type: The updated notetype dictionary if changes were made, None otherwise
            - field_mapping: List mapping new field indices to old field indices
                           (only returned if notetype was updated, None otherwise)

        Raises:
            NotetypeSaveError: if the notetype could not be saved. Callers must
                abort the import: without a mapping, note content would land in
                the wrong fields.
        """
        try:
            existing_model_dict = UuidFetcher(collection).get_model(self.get_uuid())
            logger.info(
                f"Saving notetype {self.anki_dict.get('name', 'unknown')} with UUID {self.get_uuid()}, existing model: {existing_model_dict is not None}"
            )
            if existing_model_dict:
                result = self._update_existing_notetype(collection, existing_model_dict)
            else:
                result = self._create_new_notetype(collection), None

            if result[0] is None and result[1] is None:
                raise NotetypeSaveError(
                    f"Failed to save notetype '{self.anki_dict.get('name', 'unknown')}': no field mapping was captured"
                )
            return result

        except NotetypeSaveError:
            raise
        except Exception as e:
            logger.error(
                f"Failed to save notetype {self.anki_dict.get('name', 'unknown')}: {e}",
                exc_info=True,
            )
            raise NotetypeSaveError(
                f"Failed to save notetype '{self.anki_dict.get('name', 'unknown')}': {e}"
            ) from e

    def _update_existing_notetype(
        self, collection: Collection, existing_model_dict: NotetypeDict
    ) -> Tuple[Optional[NotetypeDict], Optional[List[int]]]:
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
            self.local_only_field_names = []
            self.local_field_names_before_merge = {
                field["name"].lower() for field in existing_model_dict.get("flds", [])
            }

            if not should_preserve_templates:
                # Done before change detection so the user's section is not
                # mistaken for a deck change on every sync
                self._keep_protected_user_content(existing_model_dict)

            # Check if any changes are actually needed
            changes_needed = self._detect_changes_needed(
                existing_model_dict, should_preserve_templates
            )

            if not changes_needed:
                # The UUID is the only authoritative local reference. The
                # payload's ``id`` belongs to the sender's collection and must
                # never be used to look up (or write to) our notetypes/notes.
                self.anki_dict["id"] = existing_model_dict["id"]
                self.anki_dict["flds"] = existing_model_dict["flds"]
                return None, list(range(len(existing_model_dict["flds"])))

            # Capture field mapping BEFORE updating the notetype
            trivial_mapping = False
            field_mapping = None
            if self._fields_need_update(
                existing_model_dict["flds"], self.anki_dict["flds"]
            ):
                # Build the field mapping from old to new structure
                field_mapping = self._build_intelligent_field_map(
                    existing_model_dict, self.anki_dict
                )
                remote_names = {
                    field["name"].lower() for field in self.anki_dict["flds"]
                }
                self.local_only_field_names = [
                    field["name"]
                    for field in existing_model_dict["flds"]
                    if field["name"].lower() not in remote_names
                ]
                logger.info(
                    f"Generated field mapping for {existing_model_dict['name']}: {field_mapping}"
                )
            else:
                trivial_mapping = True
                logger.info(
                    f"No field changes detected for {existing_model_dict['name']}, using trivial mapping"
                )

            # Create updated notetype dict
            updated_notetype = copy.deepcopy(existing_model_dict)

            # Handle field updates with careful content preservation
            if field_mapping is not None:
                updated_fields = self._merge_fields_safely(
                    existing_model_dict["flds"], self.anki_dict["flds"]
                )
                updated_notetype["flds"] = updated_fields
                logger.info(
                    f"Updated fields for notetype {existing_model_dict['name']}"
                )
                logger.info(f"New field order: {[f['name'] for f in updated_fields]}")

            # Handle template and CSS updates based on type
            if should_preserve_templates:
                # For projektanki: keep templates but allow minor updates
                logger.info(
                    f"Preserving templates for projektanki notetype {existing_model_dict['name']}"
                )
            else:
                # For other notetypes: completely overwrite templates and CSS
                if "tmpls" in self.anki_dict:
                    updated_notetype["tmpls"] = copy.deepcopy(self.anki_dict["tmpls"])
                if "css" in self.anki_dict:
                    updated_notetype["css"] = self.anki_dict["css"]
                logger.info(
                    f"Updated templates and CSS for notetype {existing_model_dict['name']}"
                )

            # Update other properties (name, etc.) while preserving ID
            for key, value in self.anki_dict.items():
                if key not in ["id", "flds", "tmpls", "css"]:
                    updated_notetype[key] = value

            collection.models.update_dict(updated_notetype)

            self.anki_dict["id"] = updated_notetype["id"]
            self.anki_dict["flds"] = updated_notetype["flds"]

            # Anki appends a suffix when the new name is already taken. Keep the
            # server name for config lookups, but remember the real local name so
            # the next import does not try to rename the notetype again.
            actual_name = updated_notetype.get("name")
            if actual_name and actual_name != self.anki_dict.get("name"):
                self.anki_dict.setdefault("original_name", self.anki_dict.get("name"))
                self.anki_dict["name"] = actual_name

            if field_mapping is not None:
                self._remap_notes_after_field_change(
                    collection,
                    updated_notetype["id"],
                    existing_model_dict["flds"],
                    updated_notetype["flds"],
                )

            if trivial_mapping and not field_mapping:
                field_mapping = list(range(len(updated_notetype["flds"])))

            logger.info(
                f"Successfully updated notetype {updated_notetype['name']} (ID: {updated_notetype['id']})"
            )
            return updated_notetype, field_mapping

        except NotetypeSaveError:
            raise
        except Exception as e:
            logger.error(f"Failed to update existing notetype: {e}", exc_info=True)
            raise NotetypeSaveError(
                f"Failed to update notetype '{existing_model_dict.get('name', 'unknown')}': {e}"
            ) from e

    def _keep_protected_user_content(self, existing_model: NotetypeDict) -> None:
        """
        Copies the user's content below the USER_CUSTOMISATION markers of the local
        notetype into the incoming CSS and templates.
        """
        if isinstance(self.anki_dict.get("css"), str) and isinstance(
            existing_model.get("css"), str
        ):
            self.anki_dict["css"] = _carry_over_protected_content(
                existing_model["css"], self.anki_dict["css"], _CSS_MARKER_RE
            )
        if "tmpls" in self.anki_dict:
            self.anki_dict["tmpls"] = _carry_over_protected_templates(
                existing_model.get("tmpls", []), self.anki_dict["tmpls"]
            )

    def serialization_dict(self):
        # Never upload the maintainer's own content below the markers
        result = super(NoteModel, self).serialization_dict()
        if isinstance(result.get("css"), str):
            result["css"] = _strip_protected_content(result["css"], _CSS_MARKER_RE)
        if "tmpls" in result:
            result["tmpls"] = copy.deepcopy(result["tmpls"])
            for tmpl in result["tmpls"]:
                for key in ["qfmt", "afmt"]:
                    if isinstance(tmpl.get(key), str):
                        tmpl[key] = _strip_protected_content(tmpl[key], _HTML_MARKER_RE)
        return result

    @staticmethod
    def _remap_notes_after_field_change(
        collection: Collection,
        notetype_id: int,
        old_fields: List[Dict],
        new_fields: List[Dict],
    ) -> None:
        """Move existing note values when the notetype's field order changed.

        Anki stores note field values positionally; ``models.update_dict()``
        rewrites the notetype but does not move the data, so a reorder/insert
        would silently reinterpret every note of that notetype. Remap by field
        name through Anki's own notetype-change machinery.
        """
        old_names = [field["name"].lower() for field in old_fields]
        if [f["name"].lower() for f in new_fields[: len(old_names)]] == old_names:
            return  # fields were only appended - existing positions did not move

        old_index = {name: index for index, name in enumerate(old_names)}
        field_map = [old_index.get(field["name"].lower(), -1) for field in new_fields]
        if field_map == list(range(len(old_names))):
            return

        logger.info(
            f"Field order changed for notetype {notetype_id}, remapping note values: {field_map}"
        )
        note_ids = collection.db.list("select id from notes where mid = ?", notetype_id)

        for start in range(0, len(note_ids), NOTE_REMAP_CHUNK_SIZE):
            request = ChangeNotetypeRequest(
                note_ids=note_ids[start : start + NOTE_REMAP_CHUNK_SIZE],
                old_notetype_id=notetype_id,
                new_notetype_id=notetype_id,
                current_schema=collection.db.scalar("select scm from col"),
                new_fields=field_map,
            )
            collection.models.change_notetype_of_notes(request)

    def _create_new_notetype(self, collection: Collection) -> Optional[NotetypeDict]:
        """
        Creates a new notetype in the collection.

        Returns:
            Optional[NotetypeDict]: The created notetype dictionary, or None if creation failed
        """
        try:
            # Ensure the notetype has a UUID
            self._update_fields()

            note_model_dict = UuidFetcher(collection).get_model(
                self.get_uuid()
            ) or collection.models.new(self.anki_dict["name"])

            new_model = note_model_dict["id"] == 0

            if new_model:
                self.local_only_field_names = []
                self.local_field_names_before_merge = set()
                self.anki_dict = utils.merge_dicts(note_model_dict, self.anki_dict)
                self.anki_dict["id"] = 0  # Force ID to 0 for new models
                collection.models.add(self.anki_dict)

                # Ensure the ID is properly set after adding the model
                if "id" not in self.anki_dict or self.anki_dict["id"] == 0:
                    # Fetch the model again to get the assigned ID
                    saved_model = collection.models.by_name(self.anki_dict["name"])
                    if saved_model:
                        self.anki_dict["id"] = saved_model["id"]
                        logger.info(
                            f"Assigned ID {saved_model['id']} to new notetype '{self.anki_dict['name']}'"
                        )

                return self.anki_dict
            else:
                # Model already exists, just update our dict and return it
                self.local_only_field_names = []
                self.local_field_names_before_merge = {
                    field["name"].lower() for field in note_model_dict.get("flds", [])
                }
                self.anki_dict = note_model_dict
                return self.anki_dict

        except Exception as e:
            logger.error(f"Failed to create new notetype: {e}")
            return None

    def _detect_changes_needed(
        self, existing_model: NotetypeDict, preserve_templates: bool
    ) -> bool:
        """
        Detects if any changes are needed between existing and new notetype.
        """
        # Check name changes
        if existing_model.get("name") != self.anki_dict.get("name"):
            logger.info(
                f"Change detection found: name changed from '{existing_model.get('name')}' to '{self.anki_dict.get('name')}'"
            )
            return True

        # Check field changes
        if self._fields_need_update(
            existing_model.get("flds", []), self.anki_dict.get("flds", [])
        ):
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

            for i, (existing_tmpl, new_tmpl) in enumerate(
                zip(existing_templates, new_templates)
            ):
                for key in ["qfmt", "afmt", "name"]:
                    if existing_tmpl.get(key) != new_tmpl.get(key):
                        return True

        return False

    def _fields_need_update(
        self, existing_fields: List[Dict], new_fields: List[Dict]
    ) -> bool:
        """
        Checks if field updates are needed.
        """
        if len(existing_fields) != len(new_fields):
            return True

        existing_field_names = [f["name"] for f in existing_fields]
        new_field_names = [f["name"] for f in new_fields]

        logger.info(
            f"Comparing fields: existing={existing_field_names}, new={new_field_names}"
        )

        # Check if field names or order changed
        if existing_field_names != new_field_names:
            return True

        # Check if any field properties changed (except ord which we manage)
        for existing_field, new_field in zip(existing_fields, new_fields):
            for key in ["name", "sticky", "rtl", "font", "size"]:
                if existing_field.get(key) != new_field.get(key):
                    return True

        return False

    def _merge_fields_safely(
        self, existing_fields: List[Dict], new_fields: List[Dict]
    ) -> List[Dict]:
        """
        Safely merges fields while ensuring remote field order is preserved and local-only fields are at the end.

        CRITICAL: Remote field order must be preserved to ensure correct content mapping.
        Local-only fields are added at the end to maintain cross-user compatibility.
        """
        logger.info(
            f"Merging fields: {len(existing_fields)} existing -> {len(new_fields)} new"
        )

        # Create a deep copy of new fields to work with (preserves remote order)
        merged_fields = copy.deepcopy(new_fields)

        # Create mapping of existing field names to their properties
        existing_field_map = {field["name"].lower(): field for field in existing_fields}

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
                logger.info(
                    f"Merged remote field '{field['name']}' at position {i} with local properties"
                )
            else:
                logger.info(f"Added new remote field '{field['name']}' at position {i}")

        new_field_names = {field["name"].lower() for field in merged_fields}
        local_only_fields = [
            field
            for field in existing_fields
            if field["name"].lower() not in new_field_names
        ]

        if local_only_fields:
            logger.info(
                f"Preserving {len(local_only_fields)} local-only fields at the end"
            )
            next_ord = len(merged_fields)
            for local_field in local_only_fields:
                local_field_copy = copy.deepcopy(local_field)
                local_field_copy["ord"] = next_ord
                merged_fields.append(local_field_copy)
                logger.info(
                    f"Added local-only field '{local_field_copy['name']}' at position {next_ord}"
                )
                next_ord += 1

        # Validation: Ensure remote fields are in correct order
        for i, field in enumerate(merged_fields[: len(new_fields)]):
            expected_name = new_fields[i]["name"]
            if field["name"] != expected_name:
                raise ValueError(
                    f"Field order corruption: expected '{expected_name}' at position {i}, got '{field['name']}'"
                )

        logger.info(
            f"Field merge completed: {len(merged_fields)} total fields (remote order preserved)"
        )
        return merged_fields

    def can_merge_with(self, other_notetype: "NoteModel") -> bool:
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
    def find_duplicates_by_structure(
        notemodels: List["NoteModel"],
    ) -> List[List["NoteModel"]]:
        """
        Groups notemodels by structure to identify potential duplicates.
        """
        structure_groups = {}

        for notemodel in notemodels:
            field_names = tuple(
                sorted(f["name"] for f in notemodel.anki_dict.get("flds", []))
            )
            template_names = tuple(
                sorted(t["name"] for t in notemodel.anki_dict.get("tmpls", []))
            )
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

    @staticmethod
    def _build_intelligent_field_map(
        old_notetype: NotetypeDict, new_notetype: NotetypeDict
    ) -> List[Optional[int]]:
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

        remaining_unmatched = sorted(unmatched_old_indices - set(field_map))
        if remaining_unmatched:
            old_field_names = [old_fields[i]["name"] for i in remaining_unmatched]
            logger.info(
                f"Orphaned old fields not mapped: {remaining_unmatched} -> {old_field_names}"
            )

            # Append unmatched old fields with their indices at the end
            for old_index in remaining_unmatched:
                field_map.append(old_index)

        return field_map
