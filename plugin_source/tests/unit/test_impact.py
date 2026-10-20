"""Tests for impact.py: the formatting helpers, the never-raise fetch contract and the nudge gate."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import config_manager
import impact


def make_stats(**overrides):
    """A minimal API payload shaped like GET /GetMyImpactStats."""
    stats = {
        "generated_at": "2026-10-08T21:15:00+00:00",
        "tracking_since": None,
        "totals": {
            "submitted": 42,
            "accepted": 30,
            "declined": 7,
            "pending": 3,
            "unrecorded": 2,
            "reviewed": 37,
            "acceptance_rate": 0.81,
            "reach": 2310,
            "decks_contributed_to": 3,
        },
        "milestone": {
            "accepted": 30,
            "current": {"threshold": 25, "name": "Core Contributor"},
            "next": {"threshold": 50, "name": "Deck Steward"},
            "progress_to_next": 0.2,
            "remaining_to_next": 20,
        },
        "latest_accepted": {
            "deck_name": "Anatomy",
            "commit_id": 12345,
            "accepted_at": "2026-10-06T09:12:00+00:00",
            "accepted_by": "maintainer_name",
        },
        "decks": [
            {
                "deck_hash": "abc",
                "deck_name": "Anatomy",
                "subscribed_at": "2026-06-06T10:00:00+00:00",
                "subscribed_days": 124,
                "community": {
                    "subscribers": 210,
                    "net_subscribers_30d": 12,
                    "accepted_30d": 37,
                    "tracking_since": "2026-10-08",
                },
                "mine": {
                    "submitted": 12,
                    "accepted": 9,
                    "declined": 2,
                    "pending": 1,
                    "unrecorded": 0,
                    "reviewed": 11,
                    "acceptance_rate": 0.818,
                    "reach": 1890,
                },
            }
        ],
    }
    stats.update(overrides)
    return stats


class TestReachText:
    def test_states_the_reach(self):
        assert "2,310" in impact.reach_text(make_stats())

    def test_is_encouraging_when_nobody_is_reached_yet(self):
        stats = make_stats()
        stats["totals"]["reach"] = 0
        text = impact.reach_text(stats)
        assert "0" not in text
        assert "reach learners" in text

    @pytest.mark.parametrize("stats", [None, {}, {"totals": None}])
    def test_survives_a_missing_payload(self, stats):
        assert impact.reach_text(stats)


class TestMilestoneText:
    def test_first_step_is_offered_at_zero(self):
        stats = make_stats()
        stats["milestone"] = {
            "accepted": 0,
            "current": None,
            "next": {"threshold": 1, "name": "Contributor"},
            "progress_to_next": 0.0,
            "remaining_to_next": 1,
        }
        text = impact.milestone_text(stats)
        assert "Contributor" in text
        assert "starts you off" in text

    def test_counts_down_to_the_next_tier(self):
        text = impact.milestone_text(make_stats())
        assert "20 more" in text
        assert "Deck Steward" in text

    def test_top_tier_has_nothing_left_to_reach(self):
        stats = make_stats()
        stats["milestone"] = {
            "accepted": 500,
            "current": {"threshold": 500, "name": "Cornerstone"},
            "next": None,
            "progress_to_next": None,
            "remaining_to_next": None,
        }
        assert "Cornerstone" in impact.milestone_text(stats)
        assert "highest" in impact.milestone_text(stats)


class TestMilestoneFraction:
    def test_uses_the_reported_fraction(self):
        assert impact.milestone_fraction(make_stats()) == pytest.approx(0.2)

    def test_clamps_out_of_range_values(self):
        stats = make_stats()
        stats["milestone"]["progress_to_next"] = 7.5
        assert impact.milestone_fraction(stats) == 1.0
        stats["milestone"]["progress_to_next"] = -3
        assert impact.milestone_fraction(stats) == 0.0

    def test_top_tier_shows_a_full_bar(self):
        stats = make_stats()
        stats["milestone"] = {
            "accepted": 500,
            "current": {"threshold": 500, "name": "Cornerstone"},
            "next": None,
        }
        assert impact.milestone_fraction(stats) == 1.0

    def test_survives_a_missing_payload(self):
        assert impact.milestone_fraction(None) == 0.0


class TestAcceptanceText:
    def test_hidden_when_the_server_sends_null(self):
        stats = make_stats()
        stats["totals"]["acceptance_rate"] = None
        assert impact.acceptance_text(stats) is None

    def test_renders_a_percentage(self):
        assert (
            impact.acceptance_text(make_stats())
            == "81% of reviewed suggestions were accepted"
        )


class TestSubscriptionAgeText:
    def test_unknown_age_is_not_zero_days(self):
        text = impact.subscription_age_text(None)
        assert "0" not in text
        assert "tracking" in text

    def test_today(self):
        assert impact.subscription_age_text(0) == "Subscribed today"

    def test_singular_and_plural(self):
        assert impact.subscription_age_text(1) == "Subscribed for 1 day"
        assert impact.subscription_age_text(124) == "Subscribed for 124 days"


class TestNudgeLine:
    def test_silent_when_nobody_was_reached(self):
        stats = make_stats()
        stats["totals"]["reach"] = 0
        assert impact.nudge_line(stats) is None

    def test_silent_for_a_missing_payload(self):
        assert impact.nudge_line(None) is None

    def test_states_the_reach(self):
        line = impact.nudge_line(make_stats())
        assert line is not None
        assert "2,310" in line

    def test_reach_says_when_it_spans_decks(self):
        # Reach is summed per deck, so it is not a count of distinct people.
        stats = make_stats()
        stats["totals"]["decks_contributed_to"] = 3
        assert "across 3 decks" in impact.nudge_line(stats)
        stats["totals"]["decks_contributed_to"] = 1
        assert "across" not in impact.nudge_line(stats)


class TestMilestoneText:
    @staticmethod
    def _stats(accepted, remaining):
        stats = make_stats()
        stats["milestone"] = {
            "accepted": accepted,
            "current": {"threshold": 1, "name": "Contributor"},
            "next": {"threshold": 5, "name": "Regular Contributor"},
            "progress_to_next": 0.5,
            "remaining_to_next": remaining,
        }
        return stats

    def test_one_short_is_singular(self):
        text = impact.milestone_text(self._stats(4, 1))
        assert "One more accepted suggestion reaches Regular Contributor" in text

    def test_already_at_the_tier_does_not_claim_one_away(self):
        text = impact.milestone_text(self._stats(5, 0))
        assert "away" not in text
        assert "You have reached Regular Contributor." in text

    def test_several_short_stays_plural(self):
        assert "3 more accepted suggestions" in impact.milestone_text(self._stats(2, 3))


class TestShouldShowNudge:
    NOW = datetime(2026, 10, 8, 21, 0, tzinfo=timezone.utc)

    def test_disabled_never_shows(self):
        assert impact.should_show_nudge(False, self.NOW, None) is False

    def test_first_time_shows(self):
        assert impact.should_show_nudge(True, self.NOW, None) is True

    def test_not_twice_in_one_day(self):
        an_hour_ago = (self.NOW - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        assert impact.should_show_nudge(True, self.NOW, an_hour_ago) is False

    def test_shows_again_after_a_day(self):
        two_days_ago = (self.NOW - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        assert impact.should_show_nudge(True, self.NOW, two_days_ago) is True

    def test_exactly_one_day_counts_as_due(self):
        a_day_ago = (self.NOW - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        assert impact.should_show_nudge(True, self.NOW, a_day_ago) is True

    def test_an_iso_timestamp_is_understood(self):
        two_days_ago = (self.NOW - timedelta(days=2)).isoformat()
        assert impact.should_show_nudge(True, self.NOW, two_days_ago) is True

    def test_an_unreadable_timestamp_does_not_silence_the_feature_forever(self):
        assert impact.should_show_nudge(True, self.NOW, "not a timestamp") is True


class TestFetchImpact:
    @staticmethod
    def _response(status_code, payload=None, json_error=None):
        response = MagicMock()
        response.status_code = status_code
        if json_error is not None:
            response.json.side_effect = json_error
        else:
            response.json.return_value = payload
        return response

    def test_returns_the_payload_on_success(self):
        payload = make_stats()
        with patch.object(
            impact.api_client, "get", return_value=self._response(200, payload)
        ) as get:
            assert impact.fetch_impact() == payload
        assert get.call_args.kwargs["auth"] is True

    def test_a_missing_endpoint_is_not_an_error(self):
        with patch.object(impact.api_client, "get", return_value=self._response(404)):
            assert impact.fetch_impact() is None

    def test_other_error_statuses_return_none(self):
        with patch.object(impact.api_client, "get", return_value=self._response(500)):
            assert impact.fetch_impact() is None

    def test_invalid_json_returns_none(self):
        with patch.object(
            impact.api_client,
            "get",
            return_value=self._response(200, json_error=ValueError("bad json")),
        ):
            assert impact.fetch_impact() is None

    def test_unexpected_shapes_return_none(self):
        with patch.object(
            impact.api_client,
            "get",
            return_value=self._response(200, ["not", "a", "dict"]),
        ):
            assert impact.fetch_impact() is None

    def test_a_network_failure_never_raises(self):
        with patch.object(
            impact.api_client, "get", side_effect=RuntimeError("offline")
        ):
            assert impact.fetch_impact() is None


class TestMaybeShowNudge:
    def _config(self, **settings):
        config_manager.write_config({"settings": dict(settings)})

    def test_nothing_happens_when_logged_out(self):
        self._config(impact_nudge_enabled=True, last_impact_nudge="")
        with (
            patch.object(impact.auth_manager, "is_logged_in", return_value=False),
            patch.object(impact, "tooltip") as tooltip,
            patch.object(impact, "_fetch_in_background") as fetch,
        ):
            impact.maybe_show_nudge()
        tooltip.assert_not_called()
        fetch.assert_not_called()

    def test_disabled_by_config(self):
        self._config(impact_nudge_enabled=False, last_impact_nudge="")
        with (
            patch.object(impact.auth_manager, "is_logged_in", return_value=True),
            patch.object(impact, "tooltip") as tooltip,
            patch.object(impact, "_fetch_in_background") as fetch,
        ):
            impact.maybe_show_nudge()
        tooltip.assert_not_called()
        fetch.assert_not_called()

    def test_shows_once_after_a_delay_and_stays_quiet_afterwards(self):
        self._config(impact_nudge_enabled=True, last_impact_nudge="")
        with (
            patch.object(impact.auth_manager, "is_logged_in", return_value=True),
            patch.object(
                impact,
                "_fetch_in_background",
                side_effect=lambda on_done: on_done(make_stats()),
            ),
            patch.object(impact, "tooltip") as tooltip,
            patch.object(impact.QTimer, "singleShot") as single_shot,
        ):
            impact.maybe_show_nudge()

            single_shot.assert_called_once()
            delay, callback = single_shot.call_args.args
            assert delay == impact.NUDGE_DELAY_MS
            tooltip.assert_not_called()

            # The callback is what shows the tooltip, so it must run inside this patch context.
            callback()
            tooltip.assert_called_once()
            assert "2,310" in tooltip.call_args.args[0]

        with (
            patch.object(impact.auth_manager, "is_logged_in", return_value=True),
            patch.object(impact, "tooltip") as tooltip2,
            patch.object(impact, "_fetch_in_background") as fetch2,
            patch.object(impact.QTimer, "singleShot") as single_shot2,
        ):
            impact.maybe_show_nudge()
        tooltip2.assert_not_called()
        single_shot2.assert_not_called()
        fetch2.assert_not_called()

    def test_the_timestamp_is_written_without_baking_in_shipped_defaults(self):
        # The stored config must be written back, not the merged view.
        config_manager.write_config({"settings": {"pull_on_startup": True}})
        with (
            patch.object(impact.auth_manager, "is_logged_in", return_value=True),
            patch.object(impact, "_fetch_in_background"),
            patch.object(impact.QTimer, "singleShot"),
            patch.object(impact.config_manager, "set_value") as set_value,
            patch.object(impact.config_manager, "write_config") as write_config,
        ):
            impact.maybe_show_nudge()

        write_config.assert_not_called()
        set_value.assert_called_once()
        key, value = set_value.call_args.args
        assert key == "settings"
        # The user's own stored setting survives, and the timestamp was added.
        assert value["pull_on_startup"] is True
        assert value[impact.NUDGE_TIMESTAMP_KEY]
        assert set(value) == {"pull_on_startup", impact.NUDGE_TIMESTAMP_KEY}

    def test_the_sync_path_never_fetches_on_the_main_thread(self):
        # `maybe_show_nudge` runs inside a finished sync's callback, on the UI thread. A direct
        # `fetch_impact()` there froze Anki for up to the request timeout, on every sync, for
        # anyone with no reach yet (the nudge produces no line, so the daily gate never closed).
        self._config(impact_nudge_enabled=True, last_impact_nudge="")
        with (
            patch.object(impact.auth_manager, "is_logged_in", return_value=True),
            patch.object(impact, "fetch_impact") as blocking,
            patch.object(impact, "_fetch_in_background") as background,
        ):
            impact.maybe_show_nudge()

        blocking.assert_not_called()
        background.assert_called_once()

    def test_the_gate_closes_even_when_there_is_nothing_to_say(self):
        # Otherwise a user with no reach pays a fetch on every single sync, forever.
        self._config(impact_nudge_enabled=True, last_impact_nudge="")
        stats = make_stats()
        stats["totals"]["reach"] = 0
        with (
            patch.object(impact.auth_manager, "is_logged_in", return_value=True),
            patch.object(
                impact,
                "_fetch_in_background",
                side_effect=lambda on_done: on_done(stats),
            ),
            patch.object(impact, "tooltip") as tooltip,
            patch.object(impact.config_manager, "set_value") as set_value,
        ):
            impact.maybe_show_nudge()
        tooltip.assert_not_called()
        assert set_value.call_args.args[1][impact.NUDGE_TIMESTAMP_KEY]

    def test_nothing_shown_when_there_is_no_reach_to_report(self):
        self._config(impact_nudge_enabled=True, last_impact_nudge="")
        stats = make_stats()
        stats["totals"]["reach"] = 0
        with (
            patch.object(impact.auth_manager, "is_logged_in", return_value=True),
            patch.object(
                impact,
                "_fetch_in_background",
                side_effect=lambda on_done: on_done(stats),
            ),
            patch.object(impact, "tooltip") as tooltip,
        ):
            impact.maybe_show_nudge()
        tooltip.assert_not_called()

    def test_a_failing_fetch_is_silent(self):
        self._config(impact_nudge_enabled=True, last_impact_nudge="")
        with (
            patch.object(impact.auth_manager, "is_logged_in", return_value=True),
            patch.object(
                impact,
                "_fetch_in_background",
                side_effect=lambda on_done: on_done(None),
            ),
            patch.object(impact, "tooltip") as tooltip,
        ):
            impact.maybe_show_nudge()
        tooltip.assert_not_called()

    def test_it_never_raises_even_if_config_explodes(self):
        with (
            patch.object(
                impact.config_manager, "get_config", side_effect=RuntimeError("boom")
            ),
            patch.object(impact, "tooltip") as tooltip,
        ):
            impact.maybe_show_nudge()
        tooltip.assert_not_called()


class TestNumbersPresentation:
    """The wording and labels here are product requirements: never shame the contributor."""

    def test_no_label_ever_shames_the_contributor(self):
        labels = " ".join(label for _key, label in impact.NUMBER_ROWS).lower()
        for shaming in ("reject", "refus", "declin", "fail", "denied", "bad", "poor"):
            assert shaming not in labels
        assert "Feedback given" in [label for _key, label in impact.NUMBER_ROWS]

    def test_the_unrecorded_row_explains_itself(self):
        labels = dict((key, label) for key, label in impact.NUMBER_ROWS)
        assert "not recorded" in labels["unrecorded"].lower()

    def test_a_deck_line_names_the_deck_and_keeps_its_facts(self):
        deck = {
            "deck_name": "Anatomy",
            "subscribed_days": 40,
            "mine": {"accepted": 2, "declined": 1},
            "community": {"subscribers": 1234},
        }
        detail = impact.deck_detail_text(deck)
        assert "2 accepted" in detail
        assert "1 feedback received" in detail
        assert "1,234 subscribers" in detail
        assert "Subscribed for 40 days" in detail
        assert "Anatomy" not in detail

    def test_a_deck_without_a_known_start_date_says_so(self):
        deck = {"deck_name": "Anatomy", "mine": {}, "community": {}}
        assert "before we started tracking" in impact.deck_detail_text(deck)

    def test_an_acceptance_date_is_readable(self):
        assert impact.format_accepted_at("2026-10-06T09:12:00+00:00") == "6 Oct 2026"
        assert (
            impact.format_accepted_at("2026-01-31T23:59:59.123456789+00:00")
            == "31 Jan 2026"
        )

    def test_an_unreadable_or_missing_date_never_raises(self):
        assert impact.format_accepted_at(None) == ""
        assert impact.format_accepted_at("") == ""
        assert impact.format_accepted_at("not a date") == "not a date"
