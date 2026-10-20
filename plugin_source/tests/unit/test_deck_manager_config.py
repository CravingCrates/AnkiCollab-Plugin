"""DeckManager must write to the profile it was built for, and only its own keys."""

import config_manager
import utils


class TestDeckManager:
    def test_reads_the_active_profile(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        config_manager.write_config(
            {"settings": {"push_counter": 1}, "deckA": {"deckId": 1}}
        )

        manager = utils.DeckManager()

        assert manager.get_by_hash("deckA") == {"deckId": 1}
        assert "settings" not in dict(manager)

    def test_save_preserves_keys_it_does_not_own(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        config_manager.write_config(
            {"settings": {"push_counter": 1}, "deckA": {"deckId": 1}}
        )

        with utils.DeckManager() as manager:
            manager._filtered_items["deckA"]["timestamp"] = "2026-01-01 00:00:00"
            manager._filtered_items["deckB"] = {"deckId": 2}

        stored = config_manager._read_json(
            config_manager.profile_dir("Medicine") / "config.json"
        )
        assert stored["settings"] == {"push_counter": 1}
        assert stored["deckA"]["timestamp"] == "2026-01-01 00:00:00"
        assert stored["deckB"] == {"deckId": 2}

    def test_save_does_not_materialise_shipped_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_manager, "_shipped_defaults", lambda: {"shipped": 1})
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        config_manager.write_config({"deckA": {"deckId": 1}})

        with utils.DeckManager() as manager:
            manager._filtered_items["deckA"]["deckId"] = 42

        stored = config_manager._read_json(
            config_manager.profile_dir("Medicine") / "config.json"
        )
        assert "shipped" not in stored
        assert stored == {"deckA": {"deckId": 42}}

    def test_a_profile_switch_between_open_and_save_writes_nothing(
        self, tmp_path, caplog
    ):
        import logging

        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        config_manager.write_config({"deckA": {"deckId": 1}})

        manager = utils.DeckManager()
        manager._filtered_items["deckA"]["deckId"] = 99
        config_manager.activate_for_tests("Languages")
        config_manager.write_config({"deckB": {"deckId": 2}})

        with caplog.at_level(logging.WARNING, logger="ankicollab"):
            manager.save()

        languages = config_manager._read_json(
            config_manager.profile_dir("Languages") / "config.json"
        )
        assert languages == {"deckB": {"deckId": 2}}
        medicine = config_manager._read_json(
            config_manager.profile_dir("Medicine") / "config.json"
        )
        assert medicine == {"deckA": {"deckId": 1}}
        assert any("profile" in record.getMessage() for record in caplog.records)

    def test_save_without_a_profile_is_a_no_op_not_a_crash(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        manager = utils.DeckManager()
        manager.save()  # must not raise
