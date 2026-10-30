"""Tests for the ``ANKICOLLAB_END`` marker.

Content after the marker comment belongs to the user: it must survive deck
updates, must not make an unchanged deck look changed, and must never be
uploaded when the notetype is exported or suggested.
"""

from __future__ import annotations

import copy

from tests.conftest import create_mock_collection, make_notetype

from crowd_anki.representation.note_model import NoteModel

CSS_MARKER = "/*\nANKICOLLAB_END\nText below this comment is yours.\n*/"
HTML_MARKER = "<!--\nANKICOLLAB_END\nText below this comment is yours.\n-->"

USER_CSS = "\n.card { background: hotpink; }"
USER_HTML = "\n<script>var userConfig = { hints: false };</script>"


def _template(name="Card 1", qfmt="{{Front}}", afmt="{{Back}}", ord=0):
    return {"name": name, "qfmt": qfmt, "afmt": afmt, "ord": ord}


def _notetype(css=".card { color: black; }", templates=None):
    return make_notetype(
        name="Malleus",
        fields=["Front", "Back"],
        css=css,
        templates=templates or [_template()],
        model_id=1,
    )


def _update(existing, incoming):
    col = create_mock_collection()
    updated, _mapping = NoteModel(incoming)._update_existing_notetype(
        col, copy.deepcopy(existing)
    )
    return updated, col


class TestUpdateKeepsUserContent:
    def test_user_css_survives_and_deck_css_above_the_marker_applies(self):
        existing = _notetype(css=".card { color: black; }\n" + CSS_MARKER + USER_CSS)
        incoming = _notetype(css=".card { color: navy; }\n" + CSS_MARKER)

        updated, col = _update(existing, incoming)

        assert updated["css"] == ".card { color: navy; }\n" + CSS_MARKER + USER_CSS
        col.models.update_dict.assert_called_once_with(updated)

    def test_user_template_content_survives_on_both_sides(self):
        existing = _notetype(
            templates=[
                _template(
                    qfmt="{{Front}}\n" + HTML_MARKER + USER_HTML,
                    afmt="{{Back}}\n" + HTML_MARKER + USER_HTML,
                )
            ]
        )
        incoming = _notetype(
            templates=[
                _template(
                    qfmt="<b>{{Front}}</b>\n" + HTML_MARKER,
                    afmt="<i>{{Back}}</i>\n" + HTML_MARKER,
                )
            ]
        )

        updated, _col = _update(existing, incoming)

        assert updated["tmpls"][0]["qfmt"] == (
            "<b>{{Front}}</b>\n" + HTML_MARKER + USER_HTML
        )
        assert updated["tmpls"][0]["afmt"] == (
            "<i>{{Back}}</i>\n" + HTML_MARKER + USER_HTML
        )

    def test_unchanged_deck_is_not_an_update_because_of_user_content(self):
        deck_css = ".card { color: black; }\n" + CSS_MARKER
        deck_qfmt = "{{Front}}\n" + HTML_MARKER
        existing = _notetype(
            css=deck_css + USER_CSS,
            templates=[_template(qfmt=deck_qfmt + USER_HTML)],
        )
        incoming = _notetype(css=deck_css, templates=[_template(qfmt=deck_qfmt)])

        updated, col = _update(existing, incoming)

        assert updated is None
        col.models.update_dict.assert_not_called()

    def test_deck_content_below_its_marker_is_replaced_by_the_users(self):
        existing = _notetype(css="a\n" + CSS_MARKER + USER_CSS)
        incoming = _notetype(css="b\n" + CSS_MARKER + "\n.deck-default { }")

        updated, _col = _update(existing, incoming)

        assert updated["css"] == "b\n" + CSS_MARKER + USER_CSS

    def test_user_content_is_appended_when_the_deck_drops_its_marker(self):
        existing = _notetype(css="a\n" + CSS_MARKER + USER_CSS)
        incoming = _notetype(css="b\n")

        updated, _col = _update(existing, incoming)

        assert updated["css"] == "b\n\n" + CSS_MARKER + USER_CSS

        # The next sync of the same deck version must not be a change
        updated_again, col = _update(updated, incoming)
        assert updated_again is None
        col.models.update_dict.assert_not_called()

    def test_marker_text_may_differ_between_deck_and_user(self):
        user_marker = "/* ANKICOLLAB_END */"
        existing = _notetype(css="a\r\n" + user_marker + USER_CSS)
        incoming = _notetype(css="b\n" + CSS_MARKER)

        updated, _col = _update(existing, incoming)

        assert updated["css"] == "b\n" + CSS_MARKER + USER_CSS


class TestUpdateWithoutUserMarker:
    def test_no_marker_anywhere_overwrites_as_before(self):
        existing = _notetype(
            css=".card { background: hotpink; }",
            templates=[_template(qfmt="{{Front}} local edit")],
        )
        incoming = _notetype(
            css=".card { color: navy; }",
            templates=[_template(qfmt="{{Front}}")],
        )

        updated, _col = _update(existing, incoming)

        assert updated["css"] == ".card { color: navy; }"
        assert updated["tmpls"][0]["qfmt"] == "{{Front}}"

    def test_deck_marker_without_a_local_marker_takes_the_deck_version(self):
        existing = _notetype(css=".card { background: hotpink; }")
        incoming = _notetype(css="b\n" + CSS_MARKER + "\n.deck-default { }")

        updated, _col = _update(existing, incoming)

        assert updated["css"] == "b\n" + CSS_MARKER + "\n.deck-default { }"

    def test_marker_outside_a_comment_is_ignored(self):
        existing = _notetype(css="a\nANKICOLLAB_END" + USER_CSS)
        incoming = _notetype(css="b\n" + CSS_MARKER)

        updated, _col = _update(existing, incoming)

        assert updated["css"] == "b\n" + CSS_MARKER

    def test_css_comment_marker_in_a_template_is_ignored(self):
        existing = _notetype(
            templates=[_template(qfmt="a\n<style>" + CSS_MARKER + "</style>user")]
        )
        incoming = _notetype(templates=[_template(qfmt="b\n" + HTML_MARKER)])

        updated, _col = _update(existing, incoming)

        assert updated["tmpls"][0]["qfmt"] == "b\n" + HTML_MARKER


class TestTemplateMatching:
    def test_templates_are_matched_by_name_when_reordered(self):
        existing = _notetype(
            templates=[
                _template("Forward", qfmt="f\n" + HTML_MARKER + "FWD", ord=0),
                _template("Reverse", qfmt="r\n" + HTML_MARKER + "REV", ord=1),
            ]
        )
        incoming = _notetype(
            templates=[
                _template("Reverse", qfmt="r2\n" + HTML_MARKER, ord=0),
                _template("Forward", qfmt="f2\n" + HTML_MARKER, ord=1),
            ]
        )

        updated, _col = _update(existing, incoming)

        assert updated["tmpls"][0]["qfmt"] == "r2\n" + HTML_MARKER + "REV"
        assert updated["tmpls"][1]["qfmt"] == "f2\n" + HTML_MARKER + "FWD"

    def test_renamed_template_falls_back_to_its_position(self):
        existing = _notetype(
            templates=[_template("Card 1", qfmt="a\n" + HTML_MARKER + "MINE")]
        )
        incoming = _notetype(templates=[_template("Recall", qfmt="b\n" + HTML_MARKER)])

        updated, _col = _update(existing, incoming)

        assert updated["tmpls"][0]["qfmt"] == "b\n" + HTML_MARKER + "MINE"

    def test_new_template_gets_no_user_content(self):
        existing = _notetype(
            templates=[_template("Card 1", qfmt="a\n" + HTML_MARKER + "MINE")]
        )
        incoming = _notetype(
            templates=[
                _template("Card 1", qfmt="a\n" + HTML_MARKER, ord=0),
                _template("Card 2", qfmt="c\n" + HTML_MARKER, ord=1),
            ]
        )

        updated, _col = _update(existing, incoming)

        assert updated["tmpls"][0]["qfmt"] == "a\n" + HTML_MARKER + "MINE"
        assert updated["tmpls"][1]["qfmt"] == "c\n" + HTML_MARKER


class TestExportStripsUserContent:
    def test_user_content_is_not_exported_but_the_marker_is(self):
        local = _notetype(
            css="a\n" + CSS_MARKER + USER_CSS,
            templates=[
                _template(
                    qfmt="q\n" + HTML_MARKER + USER_HTML,
                    afmt="b\n" + HTML_MARKER + USER_HTML,
                )
            ],
        )
        nm = NoteModel(copy.deepcopy(local))

        exported = nm.flatten()

        assert exported["css"] == "a\n" + CSS_MARKER
        assert exported["tmpls"][0]["qfmt"] == "q\n" + HTML_MARKER
        assert exported["tmpls"][0]["afmt"] == "b\n" + HTML_MARKER
        # The local notetype keeps the user's content
        assert nm.anki_dict == local

    def test_notetype_without_marker_is_exported_unchanged(self):
        local = _notetype()

        exported = NoteModel(copy.deepcopy(local)).flatten()

        assert exported["css"] == local["css"]
        assert exported["tmpls"] == local["tmpls"]
