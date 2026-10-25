"""Tests for export_manager — media reference handling, personal tags, validation.

These verify:
- Media regex patterns match correctly (sound, img, audio, source, object tags)
- Media reference updates replace old filenames with new ones
- Media file validation (_is_valid_media_file)
- Filename mapping filtering (_filter_valid_filename_mapping)
- Personal tag retrieval (get_personal_tags)
- Deck submission payload preparation
"""

import os
import re
import copy
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

import config_manager

from tests.conftest import (
    make_notetype,
    make_note_dict,
    make_deck_json,
    create_mock_collection,
    MockAnkiNote,
)

from export_manager import (
    _is_valid_media_file,
    _filter_valid_filename_mapping,
    COMPILED_SOUND_REGEXES,
    COMPILED_HTML_MEDIA_REGEXES,
    ALL_COMPILED_MEDIA_REGEXES,
)

from utils import (
    get_personal_tags,
)

# ──────────────────────────────────────────────────────────────────────
# Media regex pattern matching
# ──────────────────────────────────────────────────────────────────────


class TestSoundRegex:
    """Sound references: [sound:filename.mp3]"""

    def test_basic_sound_tag(self):
        text = "[sound:audio_01.mp3]"
        for regex in COMPILED_SOUND_REGEXES:
            m = regex.search(text)
            if m:
                assert m.group("fname") == "audio_01.mp3"
                return
        pytest.fail("No sound regex matched")

    def test_sound_with_spaces(self):
        text = "[sound:my file.mp3]"
        for regex in COMPILED_SOUND_REGEXES:
            m = regex.search(text)
            if m:
                assert m.group("fname") == "my file.mp3"
                return
        pytest.fail("No sound regex matched")

    def test_sound_case_insensitive(self):
        text = "[Sound:TEST.OGG]"
        for regex in COMPILED_SOUND_REGEXES:
            m = regex.search(text)
            if m:
                assert m.group("fname") == "TEST.OGG"
                return
        pytest.fail("No sound regex matched")

    def test_multiple_sounds_in_field(self):
        text = "[sound:a.mp3] some text [sound:b.ogg]"
        fnames = []
        for regex in COMPILED_SOUND_REGEXES:
            for m in regex.finditer(text):
                fnames.append(m.group("fname"))
        assert "a.mp3" in fnames
        assert "b.ogg" in fnames


class TestHtmlMediaRegex:
    """HTML media: <img src="...">, <audio src="...">, <source src="...">, <object data="...">"""

    def test_img_quoted_src(self):
        text = '<img src="image.png">'
        for regex in COMPILED_HTML_MEDIA_REGEXES:
            m = regex.search(text)
            if m and m.group("fname") == "image.png":
                return
        pytest.fail("No HTML media regex matched img src")

    def test_img_single_quoted(self):
        text = "<img src='photo.jpg'>"
        for regex in COMPILED_HTML_MEDIA_REGEXES:
            m = regex.search(text)
            if m and m.group("fname") == "photo.jpg":
                return
        pytest.fail("No regex matched single-quoted img src")

    def test_img_unquoted_src(self):
        text = "<img src=image_no_quotes.png>"
        for regex in COMPILED_HTML_MEDIA_REGEXES:
            m = regex.search(text)
            if m and m.group("fname") == "image_no_quotes.png":
                return
        pytest.fail("No regex matched unquoted img src")

    def test_audio_source_tag(self):
        text = '<source src="audio.ogg" type="audio/ogg">'
        found = False
        for regex in COMPILED_HTML_MEDIA_REGEXES:
            m = regex.search(text)
            if m and m.group("fname") == "audio.ogg":
                found = True
        assert found, "source tag should match"

    def test_object_data_quoted(self):
        text = '<object data="diagram.svg" type="image/svg+xml">'
        found = False
        for regex in COMPILED_HTML_MEDIA_REGEXES:
            m = regex.search(text)
            if m and m.group("fname") == "diagram.svg":
                found = True
        assert found, "object data should match"

    def test_img_with_extra_attributes(self):
        text = '<img class="card-img" src="photo.webp" alt="card">'
        found = False
        for regex in COMPILED_HTML_MEDIA_REGEXES:
            m = regex.search(text)
            if m and m.group("fname") == "photo.webp":
                found = True
        assert found, "img with extra attrs should match"


class TestMediaRegexReplacement:
    """Test that regex-based replacement works for media reference updates."""

    def test_replace_sound_filename(self):
        text = "[sound:old_audio.mp3]"
        old, new = "old_audio.mp3", "new_audio.mp3"
        for regex in ALL_COMPILED_MEDIA_REGEXES:
            replacer = lambda m: m.group(0).replace(old, new)
            result = regex.sub(replacer, text)
            if result != text:
                assert "[sound:new_audio.mp3]" == result
                return
        pytest.fail("No regex replaced the sound tag")

    def test_replace_img_filename(self):
        text = '<img src="old.png">'
        old, new = "old.png", "new.webp"
        for regex in ALL_COMPILED_MEDIA_REGEXES:
            replacer = lambda m: m.group(0).replace(old, new)
            result = regex.sub(replacer, text)
            if result != text:
                assert "new.webp" in result
                return
        pytest.fail("No regex replaced the img tag")

    def test_no_false_replacement(self):
        """Replacing a filename that doesn't exist should leave text unchanged."""
        text = "[sound:correct_file.mp3]"
        old, new = "nonexistent.mp3", "replacement.mp3"
        for regex in ALL_COMPILED_MEDIA_REGEXES:
            replacer = lambda m: m.group(0).replace(old, new)
            result = regex.sub(replacer, text)
            assert result == text, "Should not replace when filename doesn't match"

    def test_multiple_references_in_field(self):
        text = '<img src="a.png"> some text <img src="b.png">'
        old, new = "a.png", "a_opt.webp"
        updated = text
        for regex in ALL_COMPILED_MEDIA_REGEXES:
            replacer = lambda m: m.group(0).replace(old, new)
            updated = regex.sub(replacer, updated)
        assert "a_opt.webp" in updated
        assert "b.png" in updated  # b.png unchanged


# ──────────────────────────────────────────────────────────────────────
# Media file validation
# ──────────────────────────────────────────────────────────────────────


class TestIsValidMediaFile:
    def test_nonexistent_file(self):
        assert _is_valid_media_file("/no/such/file.png") is False

    def test_none_path(self):
        assert _is_valid_media_file(None) is False

    def test_valid_file(self, tmp_path):
        f = tmp_path / "image.png"
        f.write_bytes(b"\x89PNG" + b"\x00" * 200)
        assert _is_valid_media_file(str(f)) is True

    def test_too_small_file(self, tmp_path):
        f = tmp_path / "tiny.png"
        f.write_bytes(b"\x89PNG" + b"\x00" * 10)  # < 100 bytes
        assert _is_valid_media_file(str(f)) is False

    def test_exactly_100_bytes(self, tmp_path):
        f = tmp_path / "border.png"
        f.write_bytes(b"\x00" * 100)
        assert _is_valid_media_file(str(f)) is True

    def test_empty_string(self):
        assert _is_valid_media_file("") is False


# ──────────────────────────────────────────────────────────────────────
# Filename mapping filter
# ──────────────────────────────────────────────────────────────────────


class TestFilterValidFilenameMapping:
    def test_empty_mapping(self):
        assert _filter_valid_filename_mapping({}) == {}

    def test_none_mapping(self):
        assert _filter_valid_filename_mapping(None) == {}

    def test_valid_mapping(self, tmp_path):
        new_file = tmp_path / "new.webp"
        new_file.write_bytes(b"\x00" * 200)
        mapping = {"old.png": "new.webp"}
        result = _filter_valid_filename_mapping(mapping, media_dir=str(tmp_path))
        assert result == {"old.png": "new.webp"}

    def test_invalid_file_filtered(self, tmp_path):
        mapping = {"old.png": "missing.webp"}
        result = _filter_valid_filename_mapping(mapping, media_dir=str(tmp_path))
        assert result == {}

    def test_mixed_valid_invalid(self, tmp_path):
        good = tmp_path / "good.webp"
        good.write_bytes(b"\x00" * 200)
        mapping = {"a.png": "good.webp", "b.png": "missing.webp"}
        result = _filter_valid_filename_mapping(mapping, media_dir=str(tmp_path))
        assert result == {"a.png": "good.webp"}

    def test_file_at_minimum_size_is_valid(self, tmp_path):
        boundary = tmp_path / "boundary.webp"
        boundary.write_bytes(b"\x00" * 100)
        assert _filter_valid_filename_mapping(
            {"old.png": "boundary.webp"}, media_dir=str(tmp_path)
        ) == {"old.png": "boundary.webp"}


# ──────────────────────────────────────────────────────────────────────
# Personal tags for export
# ──────────────────────────────────────────────────────────────────────


class TestGetPersonalTags:
    def test_default_protected_tags_when_deck_not_found(self, mw_mock):
        """When deck hash is not in config, return defaults."""
        config_manager.write_config({"settings": {}})
        tags = get_personal_tags("nonexistent_hash")
        assert "leech" in tags
        assert "marked" in tags
        assert "missing-media" in tags
        assert "AnkiCollab_Protect" in tags
        assert "AnkiCollab_Personal" in tags

    def test_custom_personal_tags(self, mw_mock):
        """When config has personal_tags for a deck, use those."""
        cfg = {
            "my_hash": {
                "personal_tags": ["custom_tag_1", "custom_tag_2"],
            },
        }
        config_manager.write_config(cfg)
        tags = get_personal_tags("my_hash")
        assert "custom_tag_1" in tags
        assert "custom_tag_2" in tags
        assert "AnkiCollab_Protect" in tags
        assert "AnkiCollab_Personal" in tags

    def test_deck_without_personal_tags_key_gets_defaults(self, mw_mock):
        """When a deck entry exists but no personal_tags key, defaults are set."""
        config = {"hash_abc": {"deckId": 1}}
        config_manager.write_config(config)
        tags = get_personal_tags("hash_abc")
        assert "leech" in tags
        assert "marked" in tags

    def test_none_config(self, mw_mock):
        config_manager.write_config({})
        tags = get_personal_tags("any_hash")
        assert "leech" in tags
        assert "AnkiCollab_Protect" in tags
        assert "AnkiCollab_Personal" in tags
