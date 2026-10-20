"""Behavioural tests for config_manager.

Focused on things that can lose a user's settings, login or data: name
mangling, merge semantics, and the on-disk I/O contract.
"""

import hashlib
import unicodedata
from pathlib import Path

import pytest

import config_manager


class TestProfileDirName:
    def test_is_deterministic(self):
        assert config_manager.profile_dir_name(
            "Medicine"
        ) == config_manager.profile_dir_name("Medicine")

    def test_hash_is_first_12_hex_of_sha256(self):
        expected = hashlib.sha256("Medicine".encode("utf-8")).hexdigest()[:12]
        assert config_manager.profile_dir_name("Medicine").endswith("-" + expected)

    def test_slug_keeps_only_ascii_word_chars(self):
        assert config_manager.profile_dir_name("My Profile!").startswith("myprofile-")

    def test_slug_falls_back_when_empty(self):
        assert config_manager.profile_dir_name("医学").startswith("profile-")

    def test_slug_is_truncated_to_20_chars(self):
        assert config_manager.profile_dir_name("a" * 60).startswith("a" * 20 + "-")

    def test_reserved_windows_names_stay_distinct(self):
        assert config_manager.profile_dir_name("CON").startswith("con-")
        assert config_manager.profile_dir_name(
            "CON"
        ) != config_manager.profile_dir_name("con")

    def test_path_separators_cannot_escape_the_directory(self):
        for name in ("a/b", "a\\b", "a:b", "..", "../../etc"):
            result = config_manager.profile_dir_name(name)
            assert "/" not in result
            assert "\\" not in result
            assert ":" not in result
            assert not result.startswith(".")

    def test_trailing_dots_and_spaces_are_dropped_from_the_slug(self):
        assert config_manager.profile_slug("Profile.") == "profile"
        assert config_manager.profile_slug("Profile. ") == "profile"
        assert not config_manager.profile_dir_name("Profile.").startswith(".")

    def test_nfc_and_nfd_names_share_one_directory(self):
        composed = unicodedata.normalize("NFC", "Café")
        decomposed = unicodedata.normalize("NFD", "Café")
        assert composed != decomposed
        assert config_manager.profile_dir_name(
            composed
        ) == config_manager.profile_dir_name(decomposed)


class TestDeepMerge:
    def test_user_values_win(self):
        assert config_manager.deep_merge({"a": 1}, {"a": 2}) == {"a": 2}

    def test_defaults_only_keys_survive(self):
        assert config_manager.deep_merge({"a": 1, "b": 2}, {"a": 9}) == {"a": 9, "b": 2}

    def test_user_only_keys_survive(self):
        assert config_manager.deep_merge({}, {"x": 1}) == {"x": 1}

    def test_dicts_merge_recursively(self):
        result = config_manager.deep_merge({"s": {"a": 1, "b": 2}}, {"s": {"b": 3}})
        assert result == {"s": {"a": 1, "b": 3}}

    def test_lists_and_scalars_replace_wholesale(self):
        assert config_manager.deep_merge({"l": [1, 2, 3]}, {"l": [9]}) == {"l": [9]}
        assert config_manager.deep_merge({"d": {"a": 1}}, {"d": 5}) == {"d": 5}

    def test_inputs_are_never_mutated(self):
        defaults = {"s": {"a": 1}}
        user = {"s": {"b": 2}}
        config_manager.deep_merge(defaults, user)
        assert defaults == {"s": {"a": 1}}
        assert user == {"s": {"b": 2}}

    def test_non_dict_user_is_ignored(self):
        assert config_manager.deep_merge({"a": 1}, None) == {"a": 1}


class TestAtomicJson:
    def test_round_trip_preserves_non_ascii(self, tmp_path):
        target = tmp_path / "config.json"
        config_manager._write_json(target, {"a": "ü"})
        assert config_manager._read_json(target) == {"a": "ü"}
        assert "ü" in target.read_text(encoding="utf-8")

    def test_no_temp_file_left_behind(self, tmp_path):
        target = tmp_path / "config.json"
        config_manager._write_json(target, {"a": 1})
        assert [p.name for p in tmp_path.iterdir()] == ["config.json"]

    def test_writes_indented_json(self, tmp_path):
        target = tmp_path / "config.json"
        config_manager._write_json(target, {"a": 1})
        assert "\n" in target.read_text(encoding="utf-8")

    def test_reads_utf8_bom(self, tmp_path):
        target = tmp_path / "config.json"
        target.write_text('\ufeff{"a": 1}', encoding="utf-8")
        assert config_manager._read_json(target) == {"a": 1}

    def test_missing_file_reads_as_empty(self, tmp_path):
        assert config_manager._read_json(tmp_path / "nope.json") == {}

    def test_corrupt_file_is_quarantined_not_deleted(self, tmp_path):
        target = tmp_path / "config.json"
        target.write_text("{not json", encoding="utf-8")
        assert config_manager._read_json(target) == {}
        leftovers = list(tmp_path.glob("config.json.corrupt-*"))
        assert len(leftovers) == 1
        assert leftovers[0].read_text(encoding="utf-8") == "{not json"

    def test_non_object_json_is_quarantined(self, tmp_path):
        target = tmp_path / "config.json"
        target.write_text("[1, 2, 3]", encoding="utf-8")
        assert config_manager._read_json(target) == {}
        assert list(tmp_path.glob("config.json.corrupt-*"))

    def test_unreadable_file_returns_empty_without_raising(self, tmp_path, monkeypatch):
        target = tmp_path / "config.json"
        target.write_text("{}", encoding="utf-8")

        def boom(*args, **kwargs):
            raise OSError("denied")

        monkeypatch.setattr("builtins.open", boom)
        assert config_manager._read_json(target) == {}

    def test_replace_retries_on_permission_error(self, tmp_path, monkeypatch):
        target = tmp_path / "config.json"
        target.write_text("{}", encoding="utf-8")
        calls = {"n": 0}
        real_replace = config_manager.os.replace

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(13, "locked by an indexer")
            return real_replace(src, dst)

        monkeypatch.setattr(config_manager.os, "replace", flaky)
        monkeypatch.setattr(config_manager.time, "sleep", lambda _seconds: None)
        config_manager._write_json(target, {"a": 1})
        assert calls["n"] == 3
        assert config_manager._read_json(target) == {"a": 1}

    def test_gives_up_and_cleans_up_when_target_stays_locked(
        self, tmp_path, monkeypatch
    ):
        target = tmp_path / "config.json"

        def always_locked(src, dst):
            raise PermissionError(13, "locked")

        monkeypatch.setattr(config_manager.os, "replace", always_locked)
        monkeypatch.setattr(config_manager.time, "sleep", lambda _seconds: None)
        with pytest.raises(PermissionError):
            config_manager._write_json(target, {"a": 1})
        assert not target.exists()
        assert list(tmp_path.iterdir()) == []

    def test_writes_raise_when_the_directory_is_not_writable(
        self, tmp_path, monkeypatch
    ):
        """A read-only user_files must not crash Anki; only writes fail."""
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        config_manager.write_config({"before": 1})

        def boom(*args, **kwargs):
            raise PermissionError(13, "read-only filesystem")

        monkeypatch.setattr(config_manager.tempfile, "mkstemp", boom)

        with pytest.raises(OSError):
            config_manager.write_config({"after": 2})

        # Reads keep working, and the failed write did not corrupt the cache.
        assert config_manager.get_config() == {"before": 1}

    def test_a_newer_schema_is_read_and_unknown_keys_are_kept(self, tmp_path, caplog):
        target = tmp_path / "config.json"
        target.write_text('{"schema": 99, "unknownKey": 1}', encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="ankicollab"):
            data = config_manager._read_json(target)

        assert data == {"schema": 99, "unknownKey": 1}
        assert any("schema" in record.getMessage().lower() for record in caplog.records)


class TestAddonRoot:
    def test_mock_addons_folder_empty_string_falls_back_to_file(self, mw_mock):
        """The test harness returns "" from addonsFolder(); that must not become the root."""
        assert (
            config_manager._addon_root()
            == Path(config_manager.__file__).resolve().parent
        )

    def test_aqt_failure_falls_back_to_file(self, monkeypatch):
        import aqt

        monkeypatch.setattr(aqt, "mw", None, raising=False)
        assert (
            config_manager._addon_root()
            == Path(config_manager.__file__).resolve().parent
        )

    def test_prefers_ankis_snake_case_api(self, mw_mock):
        manager = mw_mock.addonManager
        assert (
            config_manager._addon_id(manager) == manager.addon_from_module.return_value
        )
        manager.addon_from_module.assert_called_once_with(config_manager.__name__)
        manager.addonFromModule.assert_not_called()

    def test_falls_back_to_the_legacy_api_on_older_anki(self, mw_mock):
        """Anki 2.1.50 -- the oldest supported version -- has only addonFromModule."""
        manager = mw_mock.addonManager
        manager.addon_from_module = None
        assert config_manager._addon_id(manager) == manager.addonFromModule.return_value
        manager.addonFromModule.assert_called_once_with(config_manager.__name__)


class TestUserFilesDir:
    def test_override_wins(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "uf")
        assert config_manager.user_files_dir() == tmp_path / "uf"

    def test_defaults_to_addon_root_user_files(self, tmp_path, monkeypatch):
        config_manager.reset_for_tests(None)
        monkeypatch.setattr(config_manager, "_addon_root", lambda: tmp_path)
        assert config_manager.user_files_dir() == tmp_path / "user_files"


class TestShippedDefaults:
    def test_this_repo_ships_no_defaults(self):
        """Assumption A1: config.json is {} so defaults are empty."""
        config_manager.reset_for_tests()
        assert config_manager._shipped_defaults() == {}

    def test_falls_back_to_config_json_in_the_addon_root(self, tmp_path, monkeypatch):
        (tmp_path / "config.json").write_text('{"a": 1}', encoding="utf-8")
        monkeypatch.setattr(config_manager, "_addon_root", lambda: tmp_path)
        assert config_manager._load_shipped_defaults() == {"a": 1}

    def test_missing_config_json_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_manager, "_addon_root", lambda: tmp_path)
        assert config_manager._load_shipped_defaults() == {}

    def test_defaults_are_not_shared_with_callers(self, tmp_path, monkeypatch):
        (tmp_path / "config.json").write_text('{"s": {"a": 1}}', encoding="utf-8")
        monkeypatch.setattr(config_manager, "_addon_root", lambda: tmp_path)
        first = config_manager._shipped_defaults()
        first["s"]["a"] = 999
        assert config_manager._shipped_defaults() == {"s": {"a": 1}}


class TestProfileDir:
    def test_profile_dir_lives_under_profiles(self, tmp_path):
        config_manager.reset_for_tests(tmp_path)
        expected = tmp_path / "profiles" / config_manager.profile_dir_name("Medicine")
        assert config_manager.profile_dir("Medicine") == expected


class TestState:
    def test_write_state_stamps_schema(self, tmp_path):
        config_manager.reset_for_tests(tmp_path)
        config_manager._write_state({"legacy_migration": "done"})
        assert config_manager._read_state() == {"schema": 1, "legacy_migration": "done"}

    def test_read_state_missing_is_empty(self, tmp_path):
        config_manager.reset_for_tests(tmp_path)
        assert config_manager._read_state() == {}


class TestLegacyMigration:
    """Migration may never lose data and may never block a profile from opening."""

    def _legacy_config(self, mw_mock, config):
        mw_mock.addonManager.addonMeta.return_value = {"config": config}
        return mw_mock

    def _legacy_auth_file(self, root=None):
        """The legacy auth path the migration will actually look at.

        ``root`` is accepted and ignored: the module owns the rule for where
        legacy files live, and the test must not be able to disagree with it.
        Computing the path here (e.g. from ``_addon_root()``, which resolves to
        the real add-on folder under the test harness) would let the test pass
        while the migration read and deleted a *different* file -- in this
        suite, the developer's live ``user_files/auth.json``.
        """
        path = config_manager._legacy_auth_paths()[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_fresh_install_marks_done_without_creating_a_profile(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager._run_legacy_migration(
            "Medicine", config_manager.profile_dir("Medicine")
        )
        assert config_manager._read_state()["legacy_migration"] == "done"
        assert not config_manager.profile_dir("Medicine").exists()

    def test_config_only_is_migrated_and_the_key_is_removed(self, tmp_path, mw_mock):
        config_manager.reset_for_tests(tmp_path / "user_files")
        self._legacy_config(mw_mock, {"settings": {"push_counter": 7}, "auth": {}})
        directory = config_manager.profile_dir("Medicine")

        config_manager._run_legacy_migration("Medicine", directory)

        stored = config_manager._read_json(directory / "config.json")
        assert stored["settings"]["push_counter"] == 7
        assert "auth" not in stored
        assert "config" not in mw_mock.addonManager.writeAddonMeta.call_args.args[1]
        assert config_manager._read_state()["legacy_migration"] == "done"

    def test_auth_only_is_migrated_and_the_legacy_file_is_deleted(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        config_manager.reset_for_tests(tmp_path / "user_files")
        legacy = self._legacy_auth_file()
        legacy.write_text('{"token": "abc"}', encoding="utf-8")
        directory = config_manager.profile_dir("Medicine")

        config_manager._run_legacy_migration("Medicine", directory)

        assert config_manager._read_json(directory / "auth.json") == {"token": "abc"}
        assert not legacy.exists()

    def test_secrets_in_the_legacy_config_auth_section_survive(self, tmp_path, mw_mock):
        config_manager.reset_for_tests(tmp_path / "user_files")
        self._legacy_config(
            mw_mock,
            {"auth": {"auto_approve": True, "token": "old", "expires_timestamp": 1.0}},
        )
        directory = config_manager.profile_dir("Medicine")

        config_manager._run_legacy_migration("Medicine", directory)

        assert config_manager._read_json(directory / "auth.json") == {
            "auto_approve": True,
            "token": "old",
            "expires_timestamp": 1.0,
        }

    def test_existing_profile_data_is_never_overwritten(
        self, tmp_path, mw_mock, monkeypatch
    ):
        config_manager.reset_for_tests(tmp_path / "user_files")
        self._legacy_config(mw_mock, {"settings": {"push_counter": 7}})
        directory = config_manager.profile_dir("Medicine")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "config.json").write_text('{"mine": true}', encoding="utf-8")
        legacy = self._legacy_auth_file()
        legacy.write_text('{"token": "abc"}', encoding="utf-8")
        monkeypatch.setattr(config_manager, "_verify_migrated_files", lambda *a: False)

        config_manager._run_legacy_migration("Medicine", directory)

        assert config_manager._read_json(directory / "config.json") == {"mine": True}
        assert legacy.exists()
        assert config_manager._read_state() == {}

    def test_crash_between_write_and_state_completes_on_the_next_run(
        self, tmp_path, mw_mock
    ):
        """Spec section 8 step 7: files already present, legacy data still there."""
        config_manager.reset_for_tests(tmp_path / "user_files")
        self._legacy_config(mw_mock, {"settings": {"push_counter": 7}})
        directory = config_manager.profile_dir("Medicine")

        # Simulate the first run dying after step 3: files written, no state.
        config_manager._write_migrated_files(
            directory, {"settings": {"push_counter": 7}}, None
        )
        assert config_manager._read_state() == {}

        config_manager._run_legacy_migration("Medicine", directory)

        assert "config" not in mw_mock.addonManager.writeAddonMeta.call_args.args[1]
        assert config_manager._read_state()["legacy_migration"] == "done"

    def test_migration_runs_only_once(self, tmp_path, mw_mock):
        config_manager.reset_for_tests(tmp_path / "user_files")
        self._legacy_config(mw_mock, {"settings": {"push_counter": 7}})
        directory = config_manager.profile_dir("Medicine")
        config_manager._run_legacy_migration("Medicine", directory)
        mw_mock.addonManager.writeAddonMeta.reset_mock()
        legacy = self._legacy_auth_file()
        legacy.write_text('{"token": "later"}', encoding="utf-8")

        config_manager._run_legacy_migration("Medicine", directory)

        assert legacy.exists(), "a completed migration must not re-consume legacy data"
        mw_mock.addonManager.writeAddonMeta.assert_not_called()

    def test_verification_failure_aborts_without_deleting(
        self, tmp_path, mw_mock, monkeypatch
    ):
        config_manager.reset_for_tests(tmp_path / "user_files")
        self._legacy_config(mw_mock, {"settings": {"push_counter": 7}})
        monkeypatch.setattr(config_manager, "_verify_migrated_files", lambda *a: False)

        config_manager._run_legacy_migration(
            "Medicine", config_manager.profile_dir("Medicine")
        )

        mw_mock.addonManager.writeAddonMeta.assert_not_called()
        assert "config" in mw_mock.addonManager.addonMeta.return_value

    def test_failure_never_propagates(self, tmp_path, mw_mock, monkeypatch):
        config_manager.reset_for_tests(tmp_path / "user_files")
        self._legacy_config(mw_mock, {"settings": {"push_counter": 7}})
        reached = []

        def boom(*args, **kwargs):
            reached.append(True)
            raise OSError("disk on fire")

        monkeypatch.setattr(config_manager, "_write_migrated_files", boom)

        config_manager._run_legacy_migration(
            "Medicine", config_manager.profile_dir("Medicine")
        )

        assert reached == [True]
        assert config_manager._read_state() == {}

    def test_an_empty_legacy_auth_file_is_still_consumed(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        legacy = self._legacy_auth_file()
        legacy.write_text("{}", encoding="utf-8")

        config_manager._run_legacy_migration(
            "Medicine", config_manager.profile_dir("Medicine")
        )

        assert not legacy.exists()
        assert config_manager._read_state()["legacy_migration"] == "done"

    def test_a_corrupt_legacy_auth_file_does_not_shadow_a_valid_one(self, tmp_path):
        """A corrupt first candidate must not hide (and then get) a valid second one."""
        config_manager.reset_for_tests(tmp_path / "user_files")
        corrupt, valid = config_manager._legacy_auth_paths()
        corrupt.parent.mkdir(parents=True, exist_ok=True)
        corrupt.write_text("{not json", encoding="utf-8")
        valid.parent.mkdir(parents=True, exist_ok=True)
        valid.write_text('{"token": "real"}', encoding="utf-8")
        directory = config_manager.profile_dir("Medicine")

        config_manager._run_legacy_migration("Medicine", directory)

        assert config_manager._read_json(directory / "auth.json") == {"token": "real"}
        assert config_manager._read_state()["legacy_migration"] == "done"

    def test_a_failure_while_marking_done_is_swallowed(
        self, tmp_path, monkeypatch, mw_mock
    ):
        """A read-only user_files must not propagate out of migration."""
        config_manager.reset_for_tests(tmp_path / "user_files")
        mw_mock.addonManager.addonMeta.return_value = {"config": {"settings": {"a": 1}}}

        def boom(_state):
            raise PermissionError(13, "read-only filesystem")

        monkeypatch.setattr(config_manager, "_write_state", boom)

        config_manager._run_legacy_migration(
            "Medicine", config_manager.profile_dir("Medicine")
        )  # must not raise

        stored = config_manager._read_json(
            config_manager.profile_dir("Medicine") / "config.json"
        )
        assert stored["settings"]["a"] == 1


import logging
import threading


class TestNoActiveProfile:
    def test_reads_return_defaults(self, tmp_path, monkeypatch):
        config_manager.reset_for_tests(tmp_path / "user_files")
        monkeypatch.setattr(config_manager, "_shipped_defaults", lambda: {"a": 1})
        assert config_manager.get_config() == {"a": 1}
        assert config_manager.get_value("a") == 1
        assert config_manager.get_value("missing", "fallback") == "fallback"

    def test_reads_are_copies(self, tmp_path, monkeypatch):
        config_manager.reset_for_tests(tmp_path / "user_files")
        monkeypatch.setattr(
            config_manager, "_shipped_defaults", lambda: {"s": {"a": 1}}
        )
        config_manager.get_config()["s"]["a"] = 999
        assert config_manager.get_config() == {"s": {"a": 1}}

    def test_no_profile_is_logged_once(self, tmp_path, monkeypatch):
        config_manager.reset_for_tests(tmp_path / "user_files")
        warnings = []
        monkeypatch.setattr(
            config_manager.logger,
            "warning",
            lambda message, *args: warnings.append(message % args if args else message),
        )
        for _ in range(5):
            config_manager.get_config()
        assert len([message for message in warnings if "profile" in message]) == 1

    def test_writes_raise(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        with pytest.raises(config_manager.NoActiveProfileError):
            config_manager.write_config({"a": 1})
        with pytest.raises(config_manager.NoActiveProfileError):
            config_manager.set_value("a", 1)

    def test_a_background_thread_never_activates_lazily(self, tmp_path, mw_mock):
        config_manager.reset_for_tests(tmp_path / "user_files")
        monkeypatch_name = "Medicine"
        mw_mock.pm.name = monkeypatch_name
        seen = []

        def worker():
            seen.append(config_manager.current_profile_name())

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        assert seen == [None]
        assert not config_manager.profile_dir(monkeypatch_name).exists()


class TestActivation:
    def test_activation_creates_the_profile_file(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        directory = config_manager.profile_dir("Medicine")
        assert config_manager._read_json(directory / "profile.json") == {
            "schema": 1,
            "profile_name": "Medicine",
        }
        assert config_manager.current_profile_name() == "Medicine"

    def test_get_config_merges_defaults_under_stored_values(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            config_manager, "_shipped_defaults", lambda: {"a": 1, "s": {"x": 1, "y": 2}}
        )
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        config_manager.write_config({"s": {"y": 9}})
        assert config_manager.get_config() == {"a": 1, "s": {"x": 1, "y": 9}}

    def test_reads_do_not_write_merged_defaults_to_disk(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_manager, "_shipped_defaults", lambda: {"a": 1})
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        config_manager.get_config()
        assert not (config_manager.profile_dir("Medicine") / "config.json").exists()

    def test_write_config_stores_exactly_what_it_is_given(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_manager, "_shipped_defaults", lambda: {"a": 1})
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        config_manager.write_config({"b": 2})
        assert config_manager._read_json(
            config_manager.profile_dir("Medicine") / "config.json"
        ) == {"b": 2}

    def test_set_value_does_not_materialise_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_manager, "_shipped_defaults", lambda: {"shipped": 1})
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        config_manager.set_value("mine", 2)
        stored = config_manager._read_json(
            config_manager.profile_dir("Medicine") / "config.json"
        )
        assert stored == {"mine": 2}
        assert config_manager.get_config() == {"shipped": 1, "mine": 2}

    def test_a_dropped_mock_profile_name_is_not_treated_as_a_profile(
        self, tmp_path, mw_mock
    ):
        """The unit-test harness leaves mw.pm.name as a MagicMock."""
        config_manager.reset_for_tests(tmp_path / "user_files")
        assert config_manager.current_profile_name() is None

    def test_lazy_activation_on_the_main_thread_activates_and_notifies(
        self, tmp_path, monkeypatch
    ):
        config_manager.reset_for_tests(tmp_path / "user_files")
        seen = []
        config_manager.add_profile_changed_listener(seen.append)
        monkeypatch.setattr(config_manager, "_profile_name_from_mw", lambda: "Medicine")

        assert config_manager.get_config() == {}

        assert config_manager.current_profile_name() == "Medicine"
        assert config_manager.profile_dir("Medicine").is_dir()
        assert seen == ["Medicine"]

    def test_a_read_only_user_files_does_not_break_reads(self, tmp_path, monkeypatch):
        """Spec section 6: reads keep working when user_files is not writable."""
        config_manager.reset_for_tests(tmp_path / "user_files")
        monkeypatch.setattr(config_manager, "_profile_name_from_mw", lambda: "Medicine")

        def boom(*args, **kwargs):
            raise PermissionError(13, "read-only filesystem")

        monkeypatch.setattr(config_manager.tempfile, "mkstemp", boom)

        assert config_manager.get_config() == {}
        with pytest.raises(OSError):
            config_manager.write_config({"a": 1})


class TestAutoApprove:
    """The maintainer preference lives with the settings, not the credentials."""

    def test_defaults_to_false(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        assert config_manager.get_auto_approve() is False

    def test_reads_from_the_settings_section(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        config_manager.write_config({"settings": {"auto_approve": True}})

        assert config_manager.get_auto_approve() is True
        stored = config_manager._read_json(
            config_manager.profile_dir("Medicine") / "config.json"
        )
        assert stored["settings"]["auto_approve"] is True

    def test_is_not_read_from_auth(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        config_manager.write_auth({"token": "t", "auto_approve": True})

        assert config_manager.get_auto_approve() is False

    def test_activation_moves_the_value_out_of_auth(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        config_manager.write_auth({"token": "t", "auto_approve": True})

        config_manager.activate_for_tests("Medicine")

        assert config_manager.get_auto_approve() is True
        assert config_manager.get_auth() == {"token": "t"}

    def test_a_value_already_in_config_wins(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        config_manager.write_config({"settings": {"auto_approve": False}})
        config_manager.write_auth({"token": "t", "auto_approve": True})

        config_manager.activate_for_tests("Medicine")

        assert config_manager.get_auto_approve() is False
        assert "auto_approve" not in config_manager.get_auth()

    def test_clearing_the_credentials_keeps_the_setting(self, tmp_path):
        """A failed refresh clears auth; the preference must survive it."""
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        config_manager.write_config({"settings": {"auto_approve": True}})

        config_manager.write_auth({})  # what get_token() does on refresh failure

        assert config_manager.get_auto_approve() is True

    def test_migration_is_best_effort_when_files_cannot_be_written(
        self, tmp_path, monkeypatch
    ):
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        config_manager.write_auth({"token": "t", "auto_approve": True})

        def boom(*args, **kwargs):
            raise PermissionError(13, "read-only filesystem")

        monkeypatch.setattr(config_manager, "_write_json", boom)

        # Must not raise: an unwritable user_files cannot block activation.
        config_manager.activate_for_tests("Medicine")


class TestProfileIsolation:
    def test_two_profiles_keep_separate_configs(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        config_manager.write_config({"who": "medicine"})
        config_manager.activate_for_tests("Languages")
        config_manager.write_config({"who": "languages"})

        config_manager.activate_for_tests("Medicine")
        assert config_manager.get_config() == {"who": "medicine"}
        config_manager.activate_for_tests("Languages")
        assert config_manager.get_config() == {"who": "languages"}

    def test_switching_backends_does_not_touch_the_other_profile(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        config_manager.write_config({"who": "medicine"})
        config_manager.activate_for_tests("Languages")
        config_manager.set_value("extra", 1)

        medicine = config_manager._read_json(
            config_manager.profile_dir("Medicine") / "config.json"
        )
        assert medicine == {"who": "medicine"}


class TestListeners:
    def test_listener_fires_on_activation_and_can_be_removed(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        seen = []
        callback = seen.append

        config_manager.add_profile_changed_listener(callback)
        config_manager.activate_for_tests("Medicine")
        assert seen == ["Medicine"]

        config_manager.remove_profile_changed_listener(callback)
        config_manager.activate_for_tests("Languages")
        assert seen == ["Medicine"]

    def test_a_failing_listener_does_not_break_activation(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        healthy = []

        def boom(_name):
            raise RuntimeError("listener bug")

        config_manager.add_profile_changed_listener(boom)
        config_manager.add_profile_changed_listener(healthy.append)
        config_manager.activate_for_tests("Medicine")
        assert config_manager.current_profile_name() == "Medicine"
        assert healthy == ["Medicine"]


class TestThreadSafety:
    def test_concurrent_read_modify_write_loses_nothing(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")

        def worker(index):
            for _ in range(20):
                config_manager.set_value(f"k{index}", index)
                config_manager.get_config()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        stored = config_manager._read_json(
            config_manager.profile_dir("Medicine") / "config.json"
        )
        assert stored == {f"k{i}": i for i in range(8)}


class _PosixOsShim:
    """An ``os`` stand-in that reports ``posix`` without breaking ``pathlib``.

    ``config_manager.os`` is the real stdlib module, and ``pathlib.Path.__new__``
    re-reads ``os.name`` on every call, so patching ``os.name`` directly makes
    ``Path()`` refuse to build a ``PosixPath`` on Windows and ``_write_json``
    dies building its temp path. Delegating everything except ``name`` and
    ``chmod`` leaves the code path under test intact.
    """

    def __init__(self, real_os, chmod):
        self.name = "posix"
        self.chmod = chmod
        self._real_os = real_os

    def __getattr__(self, attribute):
        return getattr(self._real_os, attribute)


class TestAuth:
    def test_round_trip(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        config_manager.write_auth({"token": "abc", "auto_approve": True})
        assert config_manager.get_auth() == {"token": "abc", "auto_approve": True}

    def test_get_auth_returns_a_deep_copy(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        payload = {"nested": {"k": 1}}
        config_manager.write_auth(payload)

        config_manager.get_auth()["nested"]["k"] = 999
        assert config_manager.get_auth() == {"nested": {"k": 1}}

        payload["nested"]["k"] = 42
        assert config_manager.get_auth() == {"nested": {"k": 1}}
        assert config_manager._read_json(
            config_manager.profile_dir("Medicine") / "auth.json"
        ) == {"nested": {"k": 1}}

    def test_clearing_auth_without_a_profile_raises(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        with pytest.raises(config_manager.NoActiveProfileError):
            config_manager.clear_auth()

    def test_auth_is_per_profile(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        config_manager.write_auth({"token": "medicine"})
        config_manager.activate_for_tests("Languages")
        assert config_manager.get_auth() == {}
        config_manager.write_auth({"token": "languages"})
        config_manager.activate_for_tests("Medicine")
        assert config_manager.get_auth() == {"token": "medicine"}

    def test_clear_auth_removes_the_file_and_the_cache(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        config_manager.write_auth({"token": "abc"})
        config_manager.clear_auth()
        assert config_manager.get_auth() == {}
        assert not (config_manager.profile_dir("Medicine") / "auth.json").exists()

    def test_writing_auth_without_a_profile_raises(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        with pytest.raises(config_manager.NoActiveProfileError):
            config_manager.write_auth({"token": "abc"})

    def test_auth_file_is_private_on_posix(self, tmp_path, monkeypatch):
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        modes = []
        monkeypatch.setattr(
            config_manager,
            "os",
            _PosixOsShim(config_manager.os, lambda path, mode: modes.append(mode)),
        )
        config_manager.write_auth({"token": "abc"})
        assert modes == [0o600]

    def test_no_profile_reads_as_empty(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        assert config_manager.get_auth() == {}


class TestDeviceId:
    def test_is_generated_once_and_persisted(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        first = config_manager.get_device_id()
        assert first
        assert config_manager._read_json(
            config_manager.profile_dir("Medicine") / "device.json"
        ) == {"schema": 1, "device_id": first}

        config_manager.activate_for_tests("Languages")
        config_manager.activate_for_tests("Medicine")
        assert config_manager.get_device_id() == first

    def test_is_per_profile(self, tmp_path):
        # Activation alone does not create device.json -- get_device_id does.
        # The original test only activated Medicine, so the read below raised
        # KeyError on a file that had never been written.
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        medicine_id = config_manager.get_device_id()
        config_manager.activate_for_tests("Languages")
        languages_id = config_manager.get_device_id()

        assert medicine_id != languages_id
        assert config_manager._read_json(
            config_manager.profile_dir("Medicine") / "device.json"
        ) == {"schema": 1, "device_id": medicine_id}

    def test_an_unwritable_user_files_does_not_fail_the_login(
        self, tmp_path, monkeypatch
    ):
        """Spec section 6: never fail a login over the device id."""
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")

        def boom(*args, **kwargs):
            raise PermissionError(13, "read-only filesystem")

        monkeypatch.setattr(config_manager.tempfile, "mkstemp", boom)

        first = config_manager.get_device_id()
        assert first
        assert config_manager.get_device_id() == first

    def test_without_a_profile_it_returns_a_transient_id_instead_of_raising(
        self, tmp_path, caplog
    ):
        config_manager.reset_for_tests(tmp_path / "user_files")
        with caplog.at_level(logging.WARNING, logger="ankicollab"):
            first = config_manager.get_device_id()
            second = config_manager.get_device_id()
        assert first and first == second
        assert not config_manager.user_files_dir().exists()
        assert any("device id" in record.getMessage() for record in caplog.records)


class TestNoSecretsInLogs:
    def test_tokens_never_reach_the_log(self, tmp_path, caplog, monkeypatch):
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        # utils.get_logger("ankicollab") (called by other modules at import
        # time) sets propagate = False on this logger, which would leave
        # caplog.text empty and let the negative assertion below pass without
        # examining anything.
        monkeypatch.setattr(config_manager.logger, "propagate", True)
        secret = "super-secret-token-value"
        with caplog.at_level(logging.DEBUG, logger="ankicollab"):
            config_manager.logger.warning("probe-record")
            assert "probe-record" in caplog.text  # capture really is working
            config_manager.write_auth({"token": secret, "refresh_token": secret})
            config_manager.get_auth()
            config_manager.clear_auth()
        assert secret not in caplog.text


class _FakeAnkiHook:
    """Mirrors Anki's generated hook: append/remove/count, deliberately not iterable."""

    def __init__(self):
        self._hooks = []

    def append(self, callback):
        self._hooks.append(callback)

    def remove(self, callback):
        self._hooks.remove(callback)

    def count(self):
        return len(self._hooks)


class TestInit:
    def _hook_lists(self, monkeypatch):
        import aqt

        did_open = []
        will_close = []
        monkeypatch.setattr(aqt.gui_hooks, "profile_did_open", did_open, raising=False)
        monkeypatch.setattr(
            aqt.gui_hooks, "profile_will_close", will_close, raising=False
        )
        return did_open, will_close

    def test_registers_each_hook_exactly_once(self, tmp_path, monkeypatch):
        config_manager.reset_for_tests(tmp_path / "user_files")
        did_open, will_close = self._hook_lists(monkeypatch)

        config_manager.init()
        config_manager.init()

        assert len(did_open) == 1
        assert len(will_close) == 1

    def test_overrides_the_addon_config_button(self, tmp_path, monkeypatch, mw_mock):
        config_manager.reset_for_tests(tmp_path / "user_files")
        self._hook_lists(monkeypatch)

        config_manager.init()

        assert mw_mock.addonManager.setConfigAction.call_args.args[0] == (
            mw_mock.addonManager.addon_from_module.return_value
        )

    def test_activates_a_profile_that_is_already_open(
        self, tmp_path, monkeypatch, mw_mock
    ):
        config_manager.reset_for_tests(tmp_path / "user_files")
        self._hook_lists(monkeypatch)
        mw_mock.pm.name = "Medicine"

        config_manager.init()

        # Stop lazy activation from masking whether init() did the work:
        # _activate_if_possible reads the same mocked profile name.
        monkeypatch.setattr(config_manager, "_profile_name_from_mw", lambda: None)
        assert config_manager.current_profile_name() == "Medicine"

    def test_the_did_open_hook_activates_the_profile(self, tmp_path, monkeypatch):
        import aqt

        config_manager.reset_for_tests(tmp_path / "user_files")
        did_open = []
        monkeypatch.setattr(aqt.gui_hooks, "profile_did_open", did_open, raising=False)
        monkeypatch.setattr(aqt.gui_hooks, "profile_will_close", [], raising=False)

        # init() runs before any profile is open, so it must not activate one.
        # Without this the profile would already be active and the assertions
        # below would pass even if the hook were never registered or never fired.
        monkeypatch.setattr(config_manager, "_profile_name_from_mw", lambda: None)
        config_manager.init()
        assert config_manager.current_profile_name() is None

        # Anki now opens a profile and fires the hook.
        monkeypatch.setattr(config_manager, "_profile_name_from_mw", lambda: "Medicine")
        did_open[0]()

        assert config_manager.current_profile_name() == "Medicine"

    def test_will_close_keeps_the_profile_active(self, tmp_path, monkeypatch, mw_mock):
        """A sync-on-close step may still need the config during shutdown."""
        config_manager.reset_for_tests(tmp_path / "user_files")
        _did_open, will_close = self._hook_lists(monkeypatch)
        # DEVIATION FROM BRIEF: the brief omits this call, but without it
        # nothing ever appends _on_profile_will_close to the list, so
        # will_close[0]() raises IndexError and the test cannot pass. See
        # docs/superpowers/reports/task-6-report.md.
        config_manager.init()
        config_manager.activate_for_tests("Medicine")

        will_close[0]()

        monkeypatch.setattr(config_manager, "_profile_name_from_mw", lambda: None)
        assert config_manager.current_profile_name() == "Medicine"

    def test_survives_a_broken_hook_api(self, tmp_path, monkeypatch, mw_mock):
        class Broken:
            @property
            def profile_did_open(self):
                raise RuntimeError("no hooks here")

        import aqt

        config_manager.reset_for_tests(tmp_path / "user_files")
        monkeypatch.setattr(aqt, "gui_hooks", Broken(), raising=False)

        config_manager.init()  # must not raise
        assert mw_mock.addonManager.setConfigAction.called

    def test_a_reloaded_module_replaces_its_stale_hook(self, tmp_path, monkeypatch):
        """A reload re-executes the module, so its handler is a NEW object."""
        did_open, will_close = self._hook_lists(monkeypatch)

        def stale_handler():  # what the previous module generation registered
            pass

        stale_handler._ankicollab_config_manager_hook = True
        did_open.append(stale_handler)

        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.init()

        ours = [
            fn
            for fn in did_open
            if getattr(fn, "_ankicollab_config_manager_hook", False)
        ]
        assert ours == [config_manager._on_profile_did_open]
        assert stale_handler not in did_open
        assert len(will_close) == 1

    def test_registers_on_ankis_real_hook_shape(self, tmp_path, monkeypatch):
        """Anki's hook objects are not iterable; registration must still work."""
        import aqt

        did_open = _FakeAnkiHook()
        will_close = _FakeAnkiHook()
        monkeypatch.setattr(aqt.gui_hooks, "profile_did_open", did_open, raising=False)
        monkeypatch.setattr(
            aqt.gui_hooks, "profile_will_close", will_close, raising=False
        )
        config_manager.reset_for_tests(tmp_path / "user_files")

        config_manager.init()

        assert did_open.count() == 1
        assert will_close.count() == 1

    def test_a_reload_replaces_its_stale_hook_on_ankis_real_shape(
        self, tmp_path, monkeypatch
    ):
        import aqt

        did_open = _FakeAnkiHook()
        will_close = _FakeAnkiHook()
        monkeypatch.setattr(aqt.gui_hooks, "profile_did_open", did_open, raising=False)
        monkeypatch.setattr(
            aqt.gui_hooks, "profile_will_close", will_close, raising=False
        )

        def stale_handler():  # what the previous module generation registered
            pass

        stale_handler._ankicollab_config_manager_hook = True
        did_open.append(stale_handler)
        config_manager.reset_for_tests(tmp_path / "user_files")

        config_manager.init()

        assert did_open.count() == 1
        assert did_open._hooks == [config_manager._on_profile_did_open]

    def test_the_config_callback_never_propagates(self, tmp_path, monkeypatch, mw_mock):
        import menu

        config_manager.reset_for_tests(tmp_path / "user_files")
        self._hook_lists(monkeypatch)

        def _boom(_parent):
            raise RuntimeError("dialog exploded")

        monkeypatch.setattr(menu, "show_global_settings_dialog", _boom, raising=False)
        config_manager.init()

        _addon, callback = mw_mock.addonManager.setConfigAction.call_args.args
        assert callable(callback)
        # The settings dialog can fail; the callback must report and return
        # False, never raise into Anki's add-on manager.
        assert callback() is False

    def test_init_tolerates_an_anki_without_set_config_action(
        self, tmp_path, monkeypatch, mw_mock
    ):
        config_manager.reset_for_tests(tmp_path / "user_files")
        self._hook_lists(monkeypatch)
        monkeypatch.setattr(
            mw_mock.addonManager, "setConfigAction", None, raising=False
        )

        config_manager.init()  # must not raise
