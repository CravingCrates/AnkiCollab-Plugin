"""
HTTP contract tests: verify the *real production functions* send requests
that match what the Rust backend (Axum) expects.

Previously these tests hand-built the expected payload dict and fired it
through ``api_client`` directly — meaning a production function that stopped
building the correct payload (e.g. ``subscribe_to_deck()``) would never be
caught.  Now each test calls the actual production function that builds the
payload, mocks the HTTP transport with ``requests-mock``, and inspects the
outgoing request (method, URL, headers, body).

These are NOT end-to-end tests — all HTTP traffic is intercepted.
"""

import base64
import gzip
import json
from unittest.mock import MagicMock, patch

import pytest
import requests_mock as rmock

import config_manager
from auth_manager import auth_manager
from api_client import _ApiClient
from dialogs import AddChangelogDialog
from export_manager import (
    _create_deck_op,
    _submit_deck_op,
    get_server_missing_media,
)
from gear_menu_setup import on_deck_browser_will_show_options_menu
from hooks import create_note_links_handler, remove_notes
from identifier import (
    get_user_hash,
    subscribe_to_deck,
    unsubscribe_from_deck,
)
from stats import ReviewHistory
from crowd_anki.representation.deck import Deck

# ── Backend struct field contracts ────────────────────────────────────
# Mirrors plugin-backend/src/structs.rs — kept in sync manually.
# If the backend adds/removes a required field, the corresponding test
# below must be updated.

SUBSCRIPTION_FIELDS = {"deck_hash"}  # SubscriptionRequest
SUBMIT_CHANGELOG_FIELDS = {"deck_hash", "changelog"}  # SubmitChangelog
CREATE_DECK_LINK_FIELDS = {"subscriber_deck_hash", "base_deck_hash"}
CREATE_NOTE_LINK_FIELDS = {"subscriber_deck_hash", "base_deck_hash", "note_guids"}
NOTE_REMOVAL_FIELDS = {"remote_deck", "note_guids", "commit_text", "force_overwrite"}
STATS_INFO_FIELDS = {"deck_hash", "review_history"}  # StatsInfo (gzip)
SUBMIT_CARD_FIELDS = {  # SubmitCardReq (gzip)
    "remote_deck",
    "deck_path",
    "new_name",
    "deck",
    "rationale",
    "commit_text",
    "force_overwrite",
}
CREATE_DECK_FIELDS = {"deck"}  # CreateDeckReq (gzip)


# ── Helpers ───────────────────────────────────────────────────────────

TOKEN = "test_token_abc123"


@pytest.fixture(autouse=True)
def auth_token():
    """Make the real AuthManager return a valid token for every test here."""
    with patch.object(auth_manager, "get_token", return_value=TOKEN):
        yield TOKEN


def _decode_gzip_body(raw_body: str) -> dict:
    """Reverse the base64 → gunzip → JSON pipeline used by post_gzip."""
    return json.loads(gzip.decompress(base64.b64decode(raw_body)))


def _make_deck_repr() -> Deck:
    deck = Deck(lambda *a, **kw: None, {"name": "My Deck", "id": 1})
    # Deck.serialization_dict() reads metadata.models / metadata.deck_configs
    deck.metadata = MagicMock()
    deck.metadata.models = {}
    deck.metadata.deck_configs = {}
    return deck


class _RunOpQueryOp:
    """Fake QueryOp that executes ``op`` synchronously (no Qt, no threading).

    Used to drive production functions that submit their payload through a
    ``QueryOp`` — we still exercise the real payload-building closure ``op``.
    """

    def __init__(self, parent, op, success=None):
        self._op = op

    def with_progress(self, *a, **k):
        return self

    def run_in_background(self):
        self._op(None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Contract tests — JSON endpoints (real payload builders)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAddSubscription:
    """POST /AddSubscription — AuthenticatedUser + Json<SubscriptionRequest>
    Payload built by identifier.subscribe_to_deck()."""

    def test_payload_matches_backend_struct(self, auth_token):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, json="ok", status_code=200)
            assert subscribe_to_deck("abc123") is True
            sent = m.last_request.json()
            assert set(sent.keys()) == SUBSCRIPTION_FIELDS
            assert sent["deck_hash"] == "abc123"

    def test_sends_bearer_header(self, auth_token):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, json="ok")
            subscribe_to_deck("abc123")
            assert m.last_request.headers["Authorization"] == f"Bearer {TOKEN}"

    def test_method_is_post(self, auth_token):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, json="ok")
            subscribe_to_deck("abc123")
            assert m.last_request.method == "POST"

    def test_no_user_hash_in_payload(self, auth_token):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, json="ok")
            subscribe_to_deck("abc123")
            assert "user_hash" not in m.last_request.json()


class TestRemoveSubscription:
    """POST /RemoveSubscription — payload built by unsubscribe_from_deck()."""

    def test_payload_matches_backend_struct(self, auth_token):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, json="ok", status_code=200)
            assert unsubscribe_from_deck("xyz") is True
            assert set(m.last_request.json().keys()) == SUBSCRIPTION_FIELDS

    def test_no_user_hash_in_payload(self, auth_token):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, json="ok")
            unsubscribe_from_deck("xyz")
            assert "user_hash" not in m.last_request.json()


class TestSubmitChangelog:
    """POST /submitChangelog — payload built by AddChangelogDialog.publish()."""

    def _publish(self, changelog):
        dialog = AddChangelogDialog(deck_hash="h", parent=None)
        dialog.changelog_input.toPlainText.return_value = changelog
        dialog.publish()
        return dialog

    def test_payload_matches_backend_struct(self, auth_token):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, json="ok", status_code=200)
            self._publish("Fixed typo")
            sent = m.last_request.json()
            assert set(sent.keys()) == SUBMIT_CHANGELOG_FIELDS
            assert sent["deck_hash"] == "h"
            assert sent["changelog"] == "Fixed typo"

    def test_no_token_in_payload(self, auth_token):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, json="ok")
            self._publish("text")
            assert "token" not in m.last_request.json()


class TestCreateDeckLink:
    """POST /CreateDeckLink — payload built by the closure in
    gear_menu_setup.on_deck_browser_will_show_options_menu -> create_deck_link."""

    def _capture_create_deck_link(self):
        callbacks = []
        with (
            patch("gear_menu_setup.qconnect", lambda signal, fn: callbacks.append(fn)),
            patch("gear_menu_setup.auth_manager.is_logged_in", return_value=True),
        ):
            on_deck_browser_will_show_options_menu(MagicMock(), 1)
        # Wiring order in the menu setup: export, download, reset, upload, create_link
        assert callbacks, "no menu callbacks captured"
        return callbacks[-1]

    def _run(self, auth_token):
        create_deck_link = self._capture_create_deck_link()
        with (
            patch("gear_menu_setup.get_deck_hash_from_did", return_value="sub_hash"),
            patch(
                "gear_menu_setup.QInputDialog.getText", return_value=("base_hash", True)
            ),
            patch("gear_menu_setup.QueryOp", _RunOpQueryOp),
        ):
            create_deck_link()

    def test_payload_matches_backend_struct(self, auth_token):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, text="Success", status_code=200)
            self._run(auth_token)
            sent = m.last_request.json()
            assert set(sent.keys()) == CREATE_DECK_LINK_FIELDS
            assert sent["subscriber_deck_hash"] == "sub_hash"
            assert sent["base_deck_hash"] == "base_hash"

    def test_no_token_in_payload(self, auth_token):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, text="Success")
            self._run(auth_token)
            assert "token" not in m.last_request.json()


class TestCreateNewNoteLink:
    """POST /CreateNewNoteLink — payload built by
    hooks.create_note_links_handler()."""

    def _link(self, mw_mock, nids, guids):
        mw_mock.col.get_note.side_effect = lambda nid: MagicMock(
            cards=MagicMock(return_value=[])
        )
        with (
            patch("hooks.auth_manager.is_logged_in", return_value=True),
            patch("hooks.get_guids_from_noteids", return_value=guids),
            patch("hooks.QueryOp", _RunOpQueryOp),
        ):
            create_note_links_handler(
                MagicMock(), nids, subscriber_hash="sub_hash", base_hash="base_hash"
            )

    def test_payload_matches_backend_struct(self, mw_mock, auth_token):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, json="ok", status_code=200)
            self._link(mw_mock, [101, 102], ["g1", "g2"])
            sent = m.last_request.json()
            assert set(sent.keys()) == CREATE_NOTE_LINK_FIELDS
            assert sent["subscriber_deck_hash"] == "sub_hash"
            assert sent["base_deck_hash"] == "base_hash"
            assert sent["note_guids"] == ["g1", "g2"]

    def test_no_token_in_payload(self, mw_mock, auth_token):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, json="ok")
            self._link(mw_mock, [1], ["g1"])
            assert "token" not in m.last_request.json()


class TestRequestRemoval:
    """POST /requestRemoval — payload built by hooks.remove_notes()."""

    @pytest.fixture
    def ready(self, mw_mock):
        mw_mock.col.get_note.side_effect = lambda nid: MagicMock(
            cards=MagicMock(return_value=[MagicMock()])
        )
        with (
            patch("hooks.auth_manager.is_logged_in", return_value=True),
            patch("hooks.get_deck_hash_from_card", return_value=("target_hash", None)),
            patch("hooks.get_guids_from_noteids", return_value=["g1"]),
            patch("hooks.get_commit_info", return_value=(11, "removing")),
            patch("hooks.askUser", return_value=False),
        ):
            yield

    def test_payload_matches_backend_struct(self, mw_mock, auth_token, ready):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, json="ok", status_code=200)
            remove_notes([101])
            sent = m.last_request.json()
            assert set(sent.keys()) == NOTE_REMOVAL_FIELDS
            assert sent["remote_deck"] == "target_hash"
            assert sent["note_guids"] == ["g1"]
            assert sent["force_overwrite"] is False

    def test_no_token_in_payload(self, mw_mock, auth_token, ready):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, json="ok")
            remove_notes([101])
            assert "token" not in m.last_request.json()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Contract tests — gzip endpoints (real payload builders)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestUploadDeckStats:
    """POST /UploadDeckStats — payload built by stats.upload_review_history()."""

    def _upload(self, mw_mock):
        config = {
            "settings": {},
            "hash_up": {"deckId": 1, "timestamp": "2025-01-01 00:00:00"},
        }
        config_manager.write_config(config)
        mw_mock.col.decks.children.return_value = []
        mw_mock.col.db.execute.return_value = [(100, 5, 1, "guid", 1)]
        mw_mock.col.decks.name.return_value = "Deck"
        mw_mock.col.db.first.return_value = (0, 10)  # 100% retention
        ReviewHistory("hash_up").upload_review_history(0)

    def test_decompressed_payload_matches_backend_struct(self, mw_mock, auth_token):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, text="ok")
            self._upload(mw_mock)
            body = _decode_gzip_body(m.last_request.text)
            assert set(body.keys()) == STATS_INFO_FIELDS
            assert body["deck_hash"] == "hash_up"

    def test_no_user_hash_in_payload(self, mw_mock, auth_token):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, text="ok")
            self._upload(mw_mock)
            body = _decode_gzip_body(m.last_request.text)
            assert "user_hash" not in body

    def test_content_type_is_text_plain(self, mw_mock, auth_token):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, text="ok")
            self._upload(mw_mock)
            assert m.last_request.headers["Content-Type"] == "text/plain"


class TestSubmitCard:
    """POST /submitCard — payload built by export_manager._submit_deck_op()."""

    def _submit(self, mw_mock):
        mw_mock.col.decks.name.return_value = "Parent::Child"
        with (
            patch("export_manager.get_deck_hash_from_did", return_value="hash"),
            patch("export_manager.get_local_deck_from_hash", return_value="Child"),
            patch("export_manager.get_maintainer_data", return_value=("token", False)),
            patch("export_manager.get_personal_tags", return_value=[]),
        ):
            _submit_deck_op(
                deck=_make_deck_repr(),
                did=1,
                rationale=11,
                commit_text="update",
                media_files_info=[],
                media_file_paths={},
            )

    def test_decompressed_payload_matches_backend_struct(self, mw_mock, auth_token):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, text="ok")
            self._submit(mw_mock)
            body = _decode_gzip_body(m.last_request.text)
            assert set(body.keys()) == SUBMIT_CARD_FIELDS
            assert body["remote_deck"] == "hash"
            assert body["deck_path"] == "Parent::Child"
            assert body["new_name"] == "Child"
            assert body["rationale"] == 11
            assert body["commit_text"] == "update"
            assert body["force_overwrite"] is False
            # deck is a serialized JSON string
            assert json.loads(body["deck"])["name"] == "My Deck"

    def test_no_token_in_payload(self, mw_mock, auth_token):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, text="ok")
            self._submit(mw_mock)
            body = _decode_gzip_body(m.last_request.text)
            assert "token" not in body


class TestCreateDeck:
    """POST /createDeck — payload built by export_manager._create_deck_op()."""

    def _create(self):
        with patch("export_manager.get_personal_tags", return_value=[]):
            _create_deck_op(_make_deck_repr())

    def test_decompressed_payload_matches_backend_struct(self, mw_mock, auth_token):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, json={"id": "deck_id"})
            self._create()
            body = _decode_gzip_body(m.last_request.text)
            assert set(body.keys()) == CREATE_DECK_FIELDS
            assert json.loads(body["deck"])["name"] == "My Deck"

    def test_no_username_in_payload(self, mw_mock, auth_token):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, json={"id": "deck_id"})
            self._create()
            body = _decode_gzip_body(m.last_request.text)
            assert "username" not in body


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Contract tests — empty-body / GET / auth endpoints (real builders)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCheckUserToken:
    """POST /CheckUserToken — Option<AuthenticatedUser> (no body needed).
    Called by export_manager.get_maintainer_data()."""

    def test_sends_bearer_no_body(self, auth_token):
        from export_manager import get_maintainer_data

        with rmock.Mocker() as m:
            m.post(rmock.ANY, text="true", status_code=200)
            token, _auto = get_maintainer_data("hash")
            assert m.last_request.headers["Authorization"] == f"Bearer {TOKEN}"
            assert m.last_request.body is None or m.last_request.body == b""
            assert token == TOKEN


class TestGetUserHashFromToken:
    """POST /GetUserHashFromToken — payload built by identifier.get_user_hash()."""

    def test_sends_bearer_no_body(self, auth_token):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, json="abc123hash", status_code=200)
            assert get_user_hash() == "abc123hash"
            assert m.last_request.headers["Authorization"] == f"Bearer {TOKEN}"


class TestGetMissingMedia:
    """GET /media/missing/{deck_hash} — built by get_server_missing_media()."""

    def test_sends_bearer_auth(self, auth_token):
        with rmock.Mocker() as m:
            m.get(rmock.ANY, json=["a.png"], status_code=200)
            deck_hash, missing = get_server_missing_media("abc123")
            assert deck_hash == "abc123"
            assert missing == ["a.png"]
            assert m.last_request.headers["Authorization"] == f"Bearer {TOKEN}"

    def test_deck_hash_in_url_path(self, auth_token):
        with rmock.Mocker() as m:
            m.get(rmock.ANY, json=[], status_code=200)
            get_server_missing_media("my_deck_hash_42")
            assert "/media/missing/my_deck_hash_42" in m.last_request.url


class TestRemoveToken:
    """POST /removeToken — built by auth_manager.logout()."""

    def test_uses_post_not_get(self):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, text="ok")
            auth_manager.auth_data = {"token": TOKEN}
            try:
                auth_manager.logout()
            finally:
                auth_manager.auth_data = {}
            assert m.last_request.method == "POST"

    def test_token_not_in_url_path(self):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, text="ok")
            auth_manager.auth_data = {"token": TOKEN}
            try:
                auth_manager.logout()
            finally:
                auth_manager.auth_data = {}
            assert TOKEN not in m.last_request.path
            assert m.last_request.headers["Authorization"] == f"Bearer {TOKEN}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Contract tests — login (backend contract only)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestLogin:
    """POST /login — Json<auth::Login>.

    The addon's login is handled by a webview (no Python function builds this
    payload), so this remains a pure backend-contract check: the login webview
    must POST JSON (email/password), never form data.
    """

    def test_sends_json_not_form_data(self):
        import requests

        with rmock.Mocker() as m:
            m.post(rmock.ANY, json={"token": "t", "refresh_token": "r"})
            payload = {"email": "user@test.com", "password": "pass123"}
            requests.post("https://example.com/login", json=payload, timeout=10)
            assert m.last_request.headers["Content-Type"] == "application/json"
            body = m.last_request.json()
            assert "email" in body
            assert "password" in body


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Contract tests — fields that must NOT be sent (removed from structs.rs)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRemovedFields:
    """Verify payloads built by the REAL functions contain no fields removed
    from the backend structs (token, user_hash, username, user_token)."""

    REMOVED_FIELDS = {"token", "user_hash", "username", "user_token"}

    def _assert_no_removed(self, payload_keys, endpoint):
        sent = set(payload_keys)
        assert sent.isdisjoint(
            self.REMOVED_FIELDS
        ), f"{endpoint} still sends removed fields: {sent & self.REMOVED_FIELDS}"

    def test_json_endpoints(self, mw_mock, auth_token):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, json="ok", status_code=200)
            subscribe_to_deck("h")
            self._assert_no_removed(m.last_request.json().keys(), "/AddSubscription")

        with rmock.Mocker() as m:
            m.post(rmock.ANY, json="ok", status_code=200)
            unsubscribe_from_deck("h")
            self._assert_no_removed(m.last_request.json().keys(), "/RemoveSubscription")

        with rmock.Mocker() as m:
            m.post(rmock.ANY, json="ok", status_code=200)
            AddChangelogDialog(deck_hash="h", parent=None).publish()
            self._assert_no_removed(m.last_request.json().keys(), "/submitChangelog")

        with rmock.Mocker() as m:
            m.post(rmock.ANY, json="ok", status_code=200)
            mw_mock.col.get_note.side_effect = lambda nid: MagicMock(
                cards=MagicMock(return_value=[])
            )
            with (
                patch("hooks.auth_manager.is_logged_in", return_value=True),
                patch("hooks.get_guids_from_noteids", return_value=["g1"]),
                patch("hooks.QueryOp", _RunOpQueryOp),
            ):
                create_note_links_handler(
                    MagicMock(), [1], subscriber_hash="a", base_hash="b"
                )
            self._assert_no_removed(m.last_request.json().keys(), "/CreateNewNoteLink")

    def test_gzip_endpoints(self, mw_mock, auth_token):
        with rmock.Mocker() as m:
            m.post(rmock.ANY, json={"id": "x"})
            with patch("export_manager.get_personal_tags", return_value=[]):
                _create_deck_op(_make_deck_repr())
            self._assert_no_removed(
                _decode_gzip_body(m.last_request.text).keys(), "/createDeck"
            )

        with rmock.Mocker() as m:
            m.post(rmock.ANY, text="ok")
            mw_mock.col.decks.name.return_value = "P::C"
            with (
                patch("export_manager.get_deck_hash_from_did", return_value="h"),
                patch("export_manager.get_local_deck_from_hash", return_value="C"),
                patch(
                    "export_manager.get_maintainer_data", return_value=("token", False)
                ),
                patch("export_manager.get_personal_tags", return_value=[]),
            ):
                _submit_deck_op(
                    deck=_make_deck_repr(),
                    did=1,
                    rationale=11,
                    commit_text="",
                    media_files_info=[],
                    media_file_paths={},
                )
            self._assert_no_removed(
                _decode_gzip_body(m.last_request.text).keys(), "/submitCard"
            )


# ── api_client-level checks (transport layer only) ────────────────────
# These are NOT payload-contract tests (those live above) — they verify the
# transport wrapper itself is intact.


class TestApiClientTransport:
    def test_post_json_sends_content_type(self, auth_token):
        c = _ApiClient()
        with rmock.Mocker() as m:
            m.post(rmock.ANY, json="ok")
            c.post_json("/anything", {"a": 1})
            assert m.last_request.headers["Content-Type"] == "application/json"

    def test_post_empty_sends_no_body(self, auth_token):
        c = _ApiClient()
        with rmock.Mocker() as m:
            m.post(rmock.ANY, json=True)
            c.post_empty("/CheckUserToken")
            assert m.last_request.body is None or m.last_request.body == b""
