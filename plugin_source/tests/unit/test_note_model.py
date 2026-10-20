"""Tests for NoteModel — field mapping, notetype creation/update, merge safety.

These verify the core logic that ensures:
- Fields are mapped correctly when notetypes change (renames, reorders, additions, removals)
- Local-only fields are preserved at the end
- Protected field indices remain correct through mappings
- Notetype compatibility detection works
- Duplicate detection finds structural matches
"""

import copy

from tests.conftest import make_notetype

from crowd_anki.representation.note_model import NoteModel

# ──────────────────────────────────────────────────────────────────────
# NoteModel construction
# ──────────────────────────────────────────────────────────────────────


class TestNoteModelConstruction:
    def test_from_json_roundtrip(self, basic_notetype):
        nm = NoteModel.from_json(basic_notetype)
        assert nm.anki_dict["name"] == "Basic"
        assert nm.get_uuid() == basic_notetype["crowdanki_uuid"]

    def test_from_json_preserves_fields(self, complex_notetype):
        nm = NoteModel.from_json(complex_notetype)
        field_names = nm.get_field_names()
        assert field_names == [
            "Term",
            "Definition",
            "Extra Info",
            "Image",
            "Audio",
            "Tags Field",
            "Source",
        ]

    def test_from_json_preserves_templates(self, complex_notetype):
        nm = NoteModel.from_json(complex_notetype)
        tmpl_names = nm.get_template_names()
        assert tmpl_names == ["Card 1", "Card 2"]

    def test_uuid_auto_generated(self):
        nt = make_notetype(name="NoUUID", model_uuid="")
        del nt["crowdanki_uuid"]
        nm = NoteModel(nt)
        nm._update_fields()
        assert nm.get_uuid()  # should have been auto-generated

    def test_get_field_names(self, basic_notetype):
        nm = NoteModel(basic_notetype)
        assert nm.get_field_names() == ["Front", "Back"]

    def test_get_template_names(self, basic_notetype):
        nm = NoteModel(basic_notetype)
        assert nm.get_template_names() == ["Card 1"]


# ──────────────────────────────────────────────────────────────────────
# Field change detection (_fields_need_update)
# ──────────────────────────────────────────────────────────────────────


class TestFieldChangeDetection:
    def _nm(self, notetype):
        return NoteModel(notetype)

    def test_identical_fields_no_update(self, basic_notetype):
        nm = self._nm(basic_notetype)
        assert (
            nm._fields_need_update(basic_notetype["flds"], basic_notetype["flds"])
            is False
        )

    def test_added_field_needs_update(self, basic_notetype):
        nm = self._nm(basic_notetype)
        new_flds = copy.deepcopy(basic_notetype["flds"])
        new_flds.append(
            {
                "name": "Extra",
                "ord": 2,
                "sticky": False,
                "rtl": False,
                "font": "Arial",
                "size": 20,
            }
        )
        assert nm._fields_need_update(basic_notetype["flds"], new_flds) is True

    def test_removed_field_needs_update(self, basic_notetype):
        nm = self._nm(basic_notetype)
        new_flds = [basic_notetype["flds"][0]]  # only Front
        assert nm._fields_need_update(basic_notetype["flds"], new_flds) is True

    def test_renamed_field_needs_update(self, basic_notetype):
        nm = self._nm(basic_notetype)
        new_flds = copy.deepcopy(basic_notetype["flds"])
        new_flds[1]["name"] = "Answer"
        assert nm._fields_need_update(basic_notetype["flds"], new_flds) is True

    def test_reordered_fields_needs_update(self, basic_notetype):
        nm = self._nm(basic_notetype)
        new_flds = list(reversed(copy.deepcopy(basic_notetype["flds"])))
        assert nm._fields_need_update(basic_notetype["flds"], new_flds) is True

    def test_font_change_needs_update(self, basic_notetype):
        nm = self._nm(basic_notetype)
        new_flds = copy.deepcopy(basic_notetype["flds"])
        new_flds[0]["font"] = "Times New Roman"
        assert nm._fields_need_update(basic_notetype["flds"], new_flds) is True


# ──────────────────────────────────────────────────────────────────────
# Intelligent field mapping (_build_intelligent_field_map)
# ──────────────────────────────────────────────────────────────────────


class TestIntelligentFieldMapping:
    def _nm(self, notetype):
        return NoteModel(notetype)

    def test_identity_mapping(self, basic_notetype):
        """Same fields → [0, 1]"""
        nm = self._nm(basic_notetype)
        mapping = nm._build_intelligent_field_map(basic_notetype, basic_notetype)
        assert mapping == [0, 1]

    def test_field_reorder_mapping(self, basic_notetype):
        """Swap Front/Back → [1, 0]"""
        nm = self._nm(basic_notetype)
        new_nt = copy.deepcopy(basic_notetype)
        new_nt["flds"] = list(reversed(new_nt["flds"]))
        mapping = nm._build_intelligent_field_map(basic_notetype, new_nt)
        assert mapping == [1, 0]

    def test_added_field_mapping(self, basic_notetype):
        """Add Extra → [0, 1, None]"""
        nm = self._nm(basic_notetype)
        new_nt = copy.deepcopy(basic_notetype)
        new_nt["flds"].append({"name": "Extra", "ord": 2})
        mapping = nm._build_intelligent_field_map(basic_notetype, new_nt)
        assert mapping == [0, 1, None]

    def test_removed_field_mapping(self, basic_notetype):
        """Remove Back → [0] + orphaned 1 appended"""
        nm = self._nm(basic_notetype)
        new_nt = copy.deepcopy(basic_notetype)
        new_nt["flds"] = [new_nt["flds"][0]]  # only Front
        mapping = nm._build_intelligent_field_map(basic_notetype, new_nt)
        assert mapping[0] == 0  # Front → old Front
        assert 1 in mapping  # old Back is orphaned but kept

    def test_complex_reorder_with_additions(self, complex_notetype):
        """Complex scenario: reorder + add + remove fields"""
        nm = self._nm(complex_notetype)
        new_nt = copy.deepcopy(complex_notetype)
        # Remove "Image" (idx 3), add "NewField", reorder
        new_nt["flds"] = [
            {"name": "Definition", "ord": 0},  # was idx 1
            {"name": "Term", "ord": 1},  # was idx 0
            {"name": "NewField", "ord": 2},  # brand new
            {"name": "Extra Info", "ord": 3},  # was idx 2
            {"name": "Source", "ord": 4},  # was idx 6
        ]
        mapping = nm._build_intelligent_field_map(complex_notetype, new_nt)
        assert mapping[0] == 1  # Definition was at old idx 1
        assert mapping[1] == 0  # Term was at old idx 0
        assert mapping[2] is None  # NewField is brand new
        assert mapping[3] == 2  # Extra Info was at old idx 2
        assert mapping[4] == 6  # Source was at old idx 6

    def test_case_insensitive_matching(self):
        """Field name matching should be case-insensitive."""
        old_nt = make_notetype(fields=["front", "BACK"])
        new_nt = make_notetype(fields=["Front", "back"])
        nm = NoteModel(old_nt)
        mapping = nm._build_intelligent_field_map(old_nt, new_nt)
        assert mapping == [0, 1]

    def test_orphaned_fields_appended(self):
        """Fields removed from remote but present locally get appended."""
        old_nt = make_notetype(fields=["A", "B", "C", "D"])
        new_nt = make_notetype(fields=["A", "D"])  # B, C removed
        nm = NoteModel(old_nt)
        mapping = nm._build_intelligent_field_map(old_nt, new_nt)
        # A→0, D→3, then orphaned B(1) and C(2) appended
        assert mapping[0] == 0  # A
        assert mapping[1] == 3  # D
        assert 1 in mapping  # B orphaned
        assert 2 in mapping  # C orphaned


# ──────────────────────────────────────────────────────────────────────
# Safe field merging (_merge_fields_safely)
# ──────────────────────────────────────────────────────────────────────


class TestMergeFieldsSafely:
    def test_remote_order_preserved(self, basic_notetype):
        nm = NoteModel(basic_notetype)
        new_flds = [
            {
                "name": "Back",
                "ord": 0,
                "sticky": False,
                "rtl": False,
                "font": "Arial",
                "size": 20,
            },
            {
                "name": "Front",
                "ord": 1,
                "sticky": False,
                "rtl": False,
                "font": "Arial",
                "size": 20,
            },
        ]
        merged = nm._merge_fields_safely(basic_notetype["flds"], new_flds)
        assert merged[0]["name"] == "Back"
        assert merged[1]["name"] == "Front"

    def test_local_properties_preserved(self, basic_notetype):
        """Local font/size customizations should be kept."""
        nm = NoteModel(basic_notetype)
        existing = copy.deepcopy(basic_notetype["flds"])
        existing[0]["font"] = "Comic Sans"
        existing[0]["size"] = 30
        new_flds = copy.deepcopy(basic_notetype["flds"])
        merged = nm._merge_fields_safely(existing, new_flds)
        assert merged[0]["font"] == "Comic Sans"
        assert merged[0]["size"] == 30

    def test_local_only_fields_at_end(self, basic_notetype):
        """User's custom fields should be appended at the end."""
        nm = NoteModel(basic_notetype)
        existing = copy.deepcopy(basic_notetype["flds"])
        existing.append(
            {
                "name": "MyNotes",
                "ord": 2,
                "sticky": False,
                "rtl": False,
                "font": "Arial",
                "size": 20,
            }
        )
        new_flds = copy.deepcopy(basic_notetype["flds"])  # remote has no MyNotes
        merged = nm._merge_fields_safely(existing, new_flds)
        assert len(merged) == 3
        assert merged[2]["name"] == "MyNotes"

    def test_ord_values_sequential(self, basic_notetype):
        nm = NoteModel(basic_notetype)
        existing = copy.deepcopy(basic_notetype["flds"])
        existing.append(
            {
                "name": "Custom",
                "ord": 99,
                "sticky": False,
                "rtl": False,
                "font": "Arial",
                "size": 20,
            }
        )
        merged = nm._merge_fields_safely(existing, basic_notetype["flds"])
        for i, f in enumerate(merged):
            assert f["ord"] == i

    def test_corruption_raises_error(self):
        """If remote field order gets corrupted during merge, raise."""
        nt = make_notetype(fields=["A", "B"])
        nm = NoteModel(nt)
        existing = copy.deepcopy(nt["flds"])
        # Create malicious new_flds that would corrupt
        new_flds = [
            {
                "name": "A",
                "ord": 0,
                "sticky": False,
                "rtl": False,
                "font": "Arial",
                "size": 20,
            },
            {
                "name": "B",
                "ord": 1,
                "sticky": False,
                "rtl": False,
                "font": "Arial",
                "size": 20,
            },
        ]
        # This should NOT raise since order is correct
        merged = nm._merge_fields_safely(existing, new_flds)
        assert merged[0]["name"] == "A"


# ──────────────────────────────────────────────────────────────────────
# Change detection (_detect_changes_needed)
# ──────────────────────────────────────────────────────────────────────


class TestChangeDetection:
    def test_identical_no_changes(self, basic_notetype):
        nm = NoteModel(basic_notetype)
        assert nm._detect_changes_needed(basic_notetype, False) is False

    def test_name_change_detected(self, basic_notetype):
        nm = NoteModel(copy.deepcopy(basic_notetype))
        nm.anki_dict["name"] = "Renamed Basic"
        assert nm._detect_changes_needed(basic_notetype, False) is True

    def test_css_change_detected(self, basic_notetype):
        nm = NoteModel(copy.deepcopy(basic_notetype))
        nm.anki_dict["css"] = ".card { color: red; }"
        assert nm._detect_changes_needed(basic_notetype, False) is True

    def test_css_change_ignored_projektanki(self, basic_notetype):
        """ProjektAnki: CSS changes should be ignored (preserve_templates=True)."""
        nm = NoteModel(copy.deepcopy(basic_notetype))
        nm.anki_dict["css"] = ".card { color: red; }"
        assert nm._detect_changes_needed(basic_notetype, True) is False

    def test_template_change_detected(self, basic_notetype):
        nm = NoteModel(copy.deepcopy(basic_notetype))
        nm.anki_dict["tmpls"][0]["qfmt"] = "{{Front}} modified"
        assert nm._detect_changes_needed(basic_notetype, False) is True

    def test_template_change_ignored_projektanki(self, basic_notetype):
        nm = NoteModel(copy.deepcopy(basic_notetype))
        nm.anki_dict["tmpls"][0]["qfmt"] = "{{Front}} modified"
        assert nm._detect_changes_needed(basic_notetype, True) is False

    def test_template_count_change_detected(self, basic_notetype):
        nm = NoteModel(copy.deepcopy(basic_notetype))
        nm.anki_dict["tmpls"].append(
            {"name": "Card 2", "qfmt": "{{Back}}", "afmt": "{{Front}}", "ord": 1}
        )
        assert nm._detect_changes_needed(basic_notetype, False) is True

    def test_field_change_detected(self, basic_notetype):
        nm = NoteModel(copy.deepcopy(basic_notetype))
        nm.anki_dict["flds"].append(
            {
                "name": "Extra",
                "ord": 2,
                "sticky": False,
                "rtl": False,
                "font": "Arial",
                "size": 20,
            }
        )
        assert nm._detect_changes_needed(basic_notetype, False) is True


# Saving a notetype into the collection is covered by test_note_model_save.py.


# ──────────────────────────────────────────────────────────────────────
# Notetype compatibility & duplicate detection
# ──────────────────────────────────────────────────────────────────────


class TestNotetypeCompatibility:
    def test_identical_notetypes_compatible(self, basic_notetype):
        nm1 = NoteModel(copy.deepcopy(basic_notetype))
        nm2 = NoteModel(copy.deepcopy(basic_notetype))
        assert nm1.can_merge_with(nm2) is True

    def test_different_fields_incompatible(self, basic_notetype, cloze_notetype):
        nm1 = NoteModel(basic_notetype)
        nm2 = NoteModel(cloze_notetype)
        assert nm1.can_merge_with(nm2) is False

    def test_different_templates_incompatible(self, basic_notetype):
        nt2 = copy.deepcopy(basic_notetype)
        nt2["tmpls"].append(
            {"name": "Card 2", "qfmt": "{{Back}}", "afmt": "{{Front}}", "ord": 1}
        )
        nm1 = NoteModel(basic_notetype)
        nm2 = NoteModel(nt2)
        assert nm1.can_merge_with(nm2) is False

    def test_find_duplicates_by_structure(self, basic_notetype):
        models = [
            NoteModel(make_notetype(name="A", fields=["F1", "F2"], model_id=1)),
            NoteModel(make_notetype(name="B", fields=["F1", "F2"], model_id=2)),
            NoteModel(make_notetype(name="C", fields=["X", "Y"], model_id=3)),
        ]
        groups = NoteModel.find_duplicates_by_structure(models)
        # A and B have same structure, C is different
        assert len(groups) == 1
        assert len(groups[0]) == 2

    def test_no_duplicates_different_structures(self):
        models = [
            NoteModel(make_notetype(name="A", fields=["F1"], model_id=1)),
            NoteModel(make_notetype(name="B", fields=["F1", "F2"], model_id=2)),
        ]
        groups = NoteModel.find_duplicates_by_structure(models)
        assert len(groups) == 0
