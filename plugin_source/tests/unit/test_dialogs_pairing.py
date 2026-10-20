"""Tests for the pairing half of the login dialog.

Only the decisions are covered: the widgets and the threads need a running Anki,
which is the reason the state machine was kept out of them in the first place.
"""

import requests

from dialogs import interpret_poll, pairing_url, _connection_error_text
from var_defs import WEBSITE_BASE_URL


class TestPairingUrl:
    def test_points_at_the_code_page(self):
        assert pairing_url("ABC-234") == f"{WEBSITE_BASE_URL}/pair?code=ABC-234"

    def test_the_code_is_carried_as_a_query_parameter(self):
        # The server reads it from there to fill the field in; if this ever became
        # a path segment the page would quietly lose its one-click behaviour.
        assert "?code=" in pairing_url("ABC-234")


class TestInterpretPoll:
    def test_an_approved_session_is_approved(self):
        assert interpret_poll(200, {"status": "approved", "token": "t"}) == "approved"

    def test_a_pending_session_keeps_waiting(self):
        assert interpret_poll(200, {"status": "pending"}) == "pending"

    def test_a_denial_is_not_a_timeout(self):
        # A denial and an expiry are reported differently to the user, so collapsing
        # them here would leave someone waiting on a code that is already dead.
        assert interpret_poll(403, {"status": "rejected"}) == "rejected"

    def test_a_finished_session_is_expired(self):
        assert interpret_poll(404, {"status": "expired"}) == "expired"
        # Already collected by an earlier poll: also unusable, also needs a new code.
        assert interpret_poll(410, {"status": "consumed"}) == "expired"

    def test_an_unreachable_server_never_ends_the_pairing(self):
        # A 500 or a rate limit says nothing about the pairing itself, so the token
        # is kept and the next tick tries again.
        assert interpret_poll(500, {}) == "retry"
        assert interpret_poll(429, {}) == "retry"
        assert interpret_poll(0, {}) == "retry"

    def test_an_unexpected_200_keeps_waiting_rather_than_giving_up(self):
        assert interpret_poll(200, {"status": "something_new"}) == "retry"
        assert interpret_poll(200, "not a dict") == "retry"
        assert interpret_poll(200, None) == "retry"


class TestConnectionErrorText:
    def test_a_timeout_says_so(self):
        assert "time" in _connection_error_text(requests.Timeout())

    def test_anything_else_is_just_unreachable(self):
        assert "Could not reach" in _connection_error_text(OSError("boom"))

    def test_a_missing_error_still_reads_as_a_sentence(self):
        assert _connection_error_text().endswith("try again.")
