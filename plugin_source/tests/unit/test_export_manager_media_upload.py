"""Tests for the media-upload pipeline in export_manager.py.

Covers what happens between "the user asked to upload media" and "the user is
told what happened":

* ``handle_media_upload`` — batching, progress scaling, cancellation, failure
  aggregation and deduplication, and the guarantee that an unexpected error is
  reported instead of propagating into the QueryOp.
* ``_sync_handle_media_upload`` — the synchronous wrapper used by the
  background operations: the upload must always report a completion state, and
  a crash inside it must not escape.
* ``_handle_media_upload_result`` — the branch selection (cancelled / partial
  failure / success / nothing to do), the silent-mode contract, and the failure
  dialog's data handling, including the bound that keeps a huge failure list
  from being rendered.

The Qt layer is replaced by small recording stand-ins so the dialog code paths
can actually run.  Only behaviour is asserted — never widget styling, layout
constants or user-facing wording.
"""

from __future__ import annotations

import types
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import export_manager
from export_manager import (
    _handle_media_upload_result,
    _sync_handle_media_upload,
    handle_media_upload,
)

# ──────────────────────────────────────────────────────────────────────
# Recording Qt stand-ins (drive the dialog code; not asserted for styling)
# ──────────────────────────────────────────────────────────────────────


class _Signal:
    """A recordable attribute chain (``btn.clicked.connect``)."""

    def __init__(self, owner, path):
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_path", path)

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return _Signal(
            object.__getattribute__(self, "_owner"),
            object.__getattribute__(self, "_path") + (name,),
        )

    def __call__(self, *args, **kwargs):
        owner = object.__getattribute__(self, "_owner")
        path = object.__getattribute__(self, "_path")
        owner.calls.append((".".join(path), args, kwargs))
        return None


class _QtObject:
    """A Qt-widget stand-in that records the arguments of every call."""

    def __init__(self, *args, **kwargs):
        object.__setattr__(self, "ctor_args", args)
        object.__setattr__(self, "calls", [])

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return _Signal(self, (name,))

    def calls_of(self, name):
        return [c for c in self.calls if c[0] == name]

    def last(self, name):
        found = self.calls_of(name)
        assert found, f"{type(self).__name__}.{name}() was never called"
        return found[-1]

    def arg(self, name, index=0):
        return self.last(name)[1][index]

    def called(self, name):
        return bool(self.calls_of(name))


class _FakeListWidget(_QtObject):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._items = []
        self.row_height = 18

    def addItems(self, items):
        self.calls.append(("addItems", (list(items),), {}))
        self._items.extend(items)

    def addItem(self, item):
        self.calls.append(("addItem", (item,), {}))
        self._items.append(item)

    def clear(self):
        self.calls.append(("clear", (), {}))
        self._items.clear()

    def count(self):
        return len(self._items)

    def sizeHintForRow(self, row):
        return self.row_height

    @property
    def items(self):
        return list(self._items)


class _FakeLineEdit(_QtObject):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._text = ""

    def text(self):
        return self._text

    def setText(self, value):
        self._text = value

    def clear(self):
        self._text = ""


class _FakeDialog(_QtObject):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)

    def height(self):
        return 100

    def width(self):
        return 500

    def sizeHint(self):
        return types.SimpleNamespace(height=lambda: 100)


class _QtPatch:
    """Patches export_manager's Qt names with recording factories."""

    FACTORIES = (
        "QDialog",
        "QVBoxLayout",
        "QGroupBox",
        "QLabel",
        "QHBoxLayout",
        "QWidget",
        "QToolButton",
        "QLineEdit",
        "QPushButton",
        "QListWidget",
    )

    def __init__(self):
        self.instances = {}
        self.configure = {}
        self.factories = {name: self._maker(name) for name in self.FACTORIES}
        self.qapp = MagicMock(name="QApplication")
        self.qapp.focusWidget.return_value = None
        self.qapp.clipboard.return_value = MagicMock(name="clipboard")

    def _maker(self, name):
        def _make(*a, **kw):
            if name == "QDialog":
                inst = _FakeDialog(*a, **kw)
            elif name == "QListWidget":
                inst = _FakeListWidget(*a, **kw)
            elif name == "QLineEdit":
                inst = _FakeLineEdit(*a, **kw)
            else:
                inst = _QtObject(*a, **kw)
            hook = self.configure.get(name)
            if hook:
                hook(inst)
            self.instances.setdefault(name, []).append(inst)
            return inst

        return _make

    def of(self, name):
        return self.instances.get(name, [])

    def one(self, name):
        found = self.of(name)
        assert found, f"no {name} was constructed"
        return found[0]

    def activate(self, stack):
        for name, maker in self.factories.items():
            stack.enter_context(patch.object(export_manager, name, maker))
        stack.enter_context(patch.object(export_manager, "QApplication", self.qapp))
        stack.enter_context(patch.object(export_manager, "Qt", MagicMock(name="Qt")))
        stack.enter_context(
            patch.object(
                export_manager, "QAbstractItemView", MagicMock(name="QAbstractItemView")
            )
        )


class _Env:
    """Environment for driving ``_handle_media_upload_result``."""

    def __init__(self, **qt_config):
        self.qt = _QtPatch()
        self.qt.configure.update(qt_config)
        self.warning = MagicMock(name="showWarning")
        self.tooltip = MagicMock(name="tooltip")
        self.critical = MagicMock(name="showCritical")
        self.rate = MagicMock(name="ask_for_rating")

    def __enter__(self):
        self._stack = ExitStack()
        self.qt.activate(self._stack)
        s = self._stack
        s.enter_context(patch("export_manager.aqt.utils.showWarning", self.warning))
        s.enter_context(patch("export_manager.aqt.utils.tooltip", self.tooltip))
        s.enter_context(patch("export_manager.aqt.utils.showCritical", self.critical))
        s.enter_context(patch("export_manager.capture_media_message"))
        s.enter_context(patch("export_manager.ask_for_rating", self.rate))
        return self

    def __exit__(self, *exc):
        return self._stack.__exit__(*exc)


def _failed(filenames, errors=("boom",)):
    return {
        "uploaded": 1,
        "existing": 2,
        "failed": len(filenames),
        "failed_filenames": list(filenames),
        "errors": list(errors),
    }


# ──────────────────────────────────────────────────────────────────────
# _handle_media_upload_result — which branch runs
# ──────────────────────────────────────────────────────────────────────


class TestUploadResultBranches:
    def test_cancelled_upload_warns_and_does_not_open_the_details_dialog(self, mw_mock):
        with _Env() as env:
            _handle_media_upload_result(
                {
                    "uploaded": 2,
                    "existing": 3,
                    "failed": 1,
                    "failed_filenames": ["bad.mp3"],
                    "errors": ["timeout"],
                    "cancelled": True,
                }
            )

        env.warning.assert_called_once()
        assert not env.qt.of("QDialog")
        mw_mock.progress.finish.assert_called_once()
        mw_mock.reset.assert_called_once()
        env.rate.assert_called_once()

    def test_cancelled_takes_priority_over_a_failure_list(self, mw_mock):
        with _Env() as env:
            _handle_media_upload_result(
                {
                    "uploaded": 0,
                    "existing": 0,
                    "failed": 80,
                    "failed_filenames": [f"f-{i}.mp3" for i in range(80)],
                    "errors": ["e1"],
                    "cancelled": True,
                }
            )

        assert not env.qt.of("QDialog")

    def test_cancelled_upload_is_still_reported_when_silent(self, mw_mock):
        """``silent`` suppresses the rating prompt, not the warning."""
        with _Env() as env:
            _handle_media_upload_result({"cancelled": True, "silent": True})

        env.warning.assert_called_once()
        env.rate.assert_not_called()
        mw_mock.reset.assert_not_called()

    def test_partial_failure_without_filenames_warns_instead_of_opening_a_dialog(
        self, mw_mock
    ):
        with _Env() as env:
            _handle_media_upload_result(
                {
                    "uploaded": 1,
                    "existing": 0,
                    "failed": 2,
                    "failed_filenames": [],
                    "errors": [],
                }
            )

        env.warning.assert_called_once()
        assert not env.qt.of("QDialog")

    def test_missing_and_empty_failed_filenames_behave_the_same(self, mw_mock):
        with _Env() as env:
            _handle_media_upload_result({"uploaded": 0, "failed": 1})
        env.warning.assert_called_once()
        assert not env.qt.of("QDialog")

    def test_successful_upload_notifies_and_resets(self, mw_mock):
        with _Env() as env:
            _handle_media_upload_result(
                {"uploaded": 4, "existing": 1, "failed": 0, "errors": []}
            )

        env.tooltip.assert_called_once()
        env.warning.assert_not_called()
        assert not env.qt.of("QDialog")
        mw_mock.reset.assert_called_once()
        env.rate.assert_called_once()

    def test_silent_upload_produces_no_user_facing_notification(self, mw_mock):
        with _Env() as env:
            _handle_media_upload_result(
                {
                    "uploaded": 4,
                    "existing": 1,
                    "failed": 0,
                    "errors": [],
                    "silent": True,
                }
            )

        env.tooltip.assert_not_called()
        env.warning.assert_not_called()
        env.rate.assert_not_called()
        mw_mock.reset.assert_not_called()
        # Progress is always finished, even in silent mode.
        mw_mock.progress.finish.assert_called_once()

    def test_nothing_uploaded_still_completes_the_progress_indicator(self, mw_mock):
        with _Env() as env:
            _handle_media_upload_result(
                {"uploaded": 0, "existing": 2, "failed": 0, "errors": []}
            )

        env.tooltip.assert_called_once()
        mw_mock.progress.finish.assert_called_once()

    def test_empty_result_is_treated_as_nothing_uploaded(self, mw_mock):
        """A result dict missing every key must not raise."""
        with _Env() as env:
            _handle_media_upload_result({})

        env.tooltip.assert_called_once()
        env.warning.assert_not_called()
        mw_mock.reset.assert_called_once()

    def test_a_missing_silent_flag_means_visible_output(self, mw_mock):
        with _Env() as env:
            _handle_media_upload_result({"uploaded": 1})

        env.tooltip.assert_called_once()
        env.rate.assert_called_once()


# ──────────────────────────────────────────────────────────────────────
# _handle_media_upload_result — the failure dialog
# ──────────────────────────────────────────────────────────────────────


class TestUploadFailureDialog:
    def test_dialog_lists_every_failed_filename(self, mw_mock):
        names = ["a.mp3", "b.mp3", "c.mp3"]
        with _Env() as env:
            _handle_media_upload_result(_failed(names))

        dialog = env.qt.one("QDialog")
        assert dialog.called("show")
        assert dialog.called("exec")

        list_widget = env.qt.one("QListWidget")
        assert list_widget.items == names

    def test_large_failure_lists_are_bounded(self, mw_mock):
        """A huge failure list must not be handed to the widget wholesale."""
        names = [f"f-{i}.mp3" for i in range(5000)]
        with _Env() as env:
            _handle_media_upload_result(_failed(names, errors=[]))

        list_widget = env.qt.one("QListWidget")
        assert list_widget.items[:500] == names[:500]
        assert len(list_widget.items) < len(names)

    def test_filtering_narrows_the_visible_filenames(self, mw_mock):
        names = ["alpha.mp3", "beta.mp3", "gamma.mp3"]
        with _Env() as env:
            _handle_media_upload_result(_failed(names, errors=[]))

            edit = env.qt.one("QLineEdit")
            apply_filter = edit.arg("textChanged.connect")
            list_widget = env.qt.one("QListWidget")

            edit.setText("beta")
            apply_filter()
            assert list_widget.items == ["beta.mp3"]

            edit.setText("")
            apply_filter()
            assert list_widget.items == names

    def test_filtering_a_huge_list_stays_bounded(self, mw_mock):
        names = [f"match-{i}.mp3" for i in range(600)]
        with _Env() as env:
            _handle_media_upload_result(_failed(names, errors=[]))

            edit = env.qt.one("QLineEdit")
            apply_filter = edit.arg("textChanged.connect")
            list_widget = env.qt.one("QListWidget")

            edit.setText("match")
            apply_filter()
            assert list_widget.items[:500] == names[:500]
            assert len(list_widget.items) <= 501

    def test_small_failure_lists_start_expanded(self, mw_mock):
        with _Env() as env:
            _handle_media_upload_result(_failed(["a.mp3"]))

        container = env.qt.of("QWidget")[1]
        assert container.last("setVisible")[1][0] is True

    def test_large_failure_lists_start_collapsed(self, mw_mock):
        with _Env() as env:
            _handle_media_upload_result(_failed([f"f{i}.mp3" for i in range(51)]))

        container = env.qt.of("QWidget")[1]
        assert container.last("setVisible")[1][0] is False

    def test_toggling_reveals_and_hides_the_list(self, mw_mock):
        with _Env() as env:
            _handle_media_upload_result(_failed([f"f{i}.mp3" for i in range(60)]))

            arrow = env.qt.one("QToolButton")
            on_toggle = arrow.arg("toggled.connect")
            container = env.qt.of("QWidget")[1]

            on_toggle(True)
            assert container.last("setVisible")[1][0] is True

            on_toggle(False)
            assert container.last("setVisible")[1][0] is False

    def test_copy_all_puts_every_failed_filename_on_the_clipboard(self, mw_mock):
        names = ["a.mp3", "b.mp3"]
        with _Env() as env:
            _handle_media_upload_result(_failed(names, errors=[]))

            copy_btn = [
                b
                for b in env.qt.of("QPushButton")
                if b.ctor_args and b.ctor_args[0] == "Copy All"
            ][0]
            copy_btn.arg("clicked.connect")()

        env.qt.qapp.clipboard.return_value.setText.assert_called_once_with(
            "a.mp3\nb.mp3"
        )

    def test_copy_all_without_a_clipboard_warns_instead_of_crashing(self, mw_mock):
        with _Env() as env:
            env.qt.qapp.clipboard.return_value = None
            _handle_media_upload_result(_failed(["a.mp3"], errors=[]))

            copy_btn = [
                b
                for b in env.qt.of("QPushButton")
                if b.ctor_args and b.ctor_args[0] == "Copy All"
            ][0]
            copy_btn.arg("clicked.connect")()

        env.warning.assert_called_once()

    def test_close_button_closes_the_dialog(self, mw_mock):
        with _Env() as env:
            _handle_media_upload_result(_failed(["a.mp3"], errors=[]))

            close_btn = [
                b
                for b in env.qt.of("QPushButton")
                if b.ctor_args and b.ctor_args[0] == "Close"
            ][0]
            dialog = env.qt.one("QDialog")
            close_btn.arg("clicked.connect")()
            assert dialog.called("accept")


class TestUploadDialogOpenFolder:
    def _open_folder(self, env):
        open_btn = [
            b
            for b in env.qt.of("QPushButton")
            if b.ctor_args and b.ctor_args[0] == "Open Folder"
        ][0]
        open_btn.arg("clicked.connect")()

    def test_no_media_directory_warns_instead_of_crashing(self, mw_mock):
        mw_mock.col.media = None
        with _Env() as env:
            _handle_media_upload_result(_failed(["a.mp3"], errors=[]))
            self._open_folder(env)

        env.warning.assert_called_once()

    def test_missing_media_directory_warns(self, mw_mock, tmp_path):
        mw_mock.col.media.dir.return_value = str(tmp_path / "gone")
        with _Env() as env:
            _handle_media_upload_result(_failed(["a.mp3"], errors=[]))
            self._open_folder(env)

        env.warning.assert_called_once()

    def test_windows_opens_the_media_directory(self, mw_mock, tmp_path):
        mw_mock.col.media.dir.return_value = str(tmp_path)
        with (
            _Env() as env,
            patch.object(export_manager.sys, "platform", "win32"),
            patch("export_manager.os.startfile", create=True) as startfile,
        ):
            _handle_media_upload_result(_failed(["a.mp3"], errors=[]))
            self._open_folder(env)

        startfile.assert_called_once_with(str(tmp_path))

    def test_macos_opens_the_media_directory(self, mw_mock, tmp_path):
        mw_mock.col.media.dir.return_value = str(tmp_path)
        with (
            _Env() as env,
            patch.object(export_manager.sys, "platform", "darwin"),
            patch("export_manager.subprocess.Popen") as popen,
        ):
            _handle_media_upload_result(_failed(["a.mp3"], errors=[]))
            self._open_folder(env)

        popen.assert_called_once_with(["open", str(tmp_path)])

    def test_linux_opens_the_media_directory(self, mw_mock, tmp_path):
        mw_mock.col.media.dir.return_value = str(tmp_path)
        with (
            _Env() as env,
            patch.object(export_manager.sys, "platform", "linux"),
            patch("export_manager.subprocess.Popen") as popen,
        ):
            _handle_media_upload_result(_failed(["a.mp3"], errors=[]))
            self._open_folder(env)

        popen.assert_called_once_with(["xdg-open", str(tmp_path)])

    def test_a_broken_file_manager_does_not_escape(self, mw_mock, tmp_path):
        mw_mock.col.media.dir.return_value = str(tmp_path)
        with (
            _Env() as env,
            patch.object(
                export_manager.os.path, "isdir", side_effect=RuntimeError("boom")
            ),
        ):
            _handle_media_upload_result(_failed(["a.mp3"], errors=[]))
            self._open_folder(env)

        env.warning.assert_called_once()


# ──────────────────────────────────────────────────────────────────────
# handle_media_upload
# ──────────────────────────────────────────────────────────────────────


def _files(n):
    return [{"filename": f"file-{i}.mp3"} for i in range(n)]


class TestHandleMediaUpload:
    @pytest.mark.asyncio
    async def test_no_files_reports_an_empty_summary_without_uploading(self, mw_mock):
        upload = AsyncMock()
        with patch.object(
            export_manager.main.media_manager, "upload_media_bulk", upload
        ):
            result = await handle_media_upload("h", "op", [], {})

        upload.assert_not_awaited()
        assert result == {
            "uploaded": 0,
            "existing": 0,
            "failed": 0,
            "errors": [],
            "cancelled": False,
        }

    @pytest.mark.asyncio
    async def test_files_are_uploaded_in_batches_of_one_hundred(self, mw_mock):
        files = _files(250)
        upload = AsyncMock(return_value={"success": True, "uploaded": 1, "existing": 0})
        with patch.object(
            export_manager.main.media_manager, "upload_media_bulk", upload
        ):
            result = await handle_media_upload("h", "op", files, {})

        slices = [c.kwargs["files_info"] for c in upload.call_args_list]
        assert [len(s) for s in slices] == [100, 100, 50]
        assert slices[2] == files[200:250]
        assert result["uploaded"] == 3
        assert result["failed"] == 0
        # An interactive upload reports back as non-silent by default.
        assert result["silent"] is False

    @pytest.mark.asyncio
    async def test_each_batch_is_scoped_to_its_own_deck_and_operation(self, mw_mock):
        upload = AsyncMock(return_value={"success": True, "uploaded": 1, "existing": 0})
        with patch.object(
            export_manager.main.media_manager, "upload_media_bulk", upload
        ):
            await handle_media_upload("deck-hash", "op-id", _files(1), {})

        kwargs = upload.call_args.kwargs
        assert kwargs["deck_hash"] == "deck-hash"
        assert kwargs["bulk_operation_id"] == "op-id"

    @pytest.mark.asyncio
    async def test_batch_progress_is_scaled_onto_the_whole_upload(self, mw_mock):
        seen = []

        async def _upload(**kwargs):
            # The per-batch callback is only valid while its batch runs.
            kwargs["progress_callback"](0.5)
            return {"success": True, "uploaded": 0, "existing": 0}

        upload = AsyncMock(side_effect=_upload)
        with patch.object(
            export_manager.main.media_manager, "upload_media_bulk", upload
        ):
            await handle_media_upload("h", "op", _files(250), {}, seen.append)

        assert seen == pytest.approx([1 / 6, 1 / 2, 5 / 6, 1.0])

    @pytest.mark.asyncio
    async def test_progress_reporting_is_optional(self, mw_mock):
        upload = AsyncMock(return_value={"success": True, "uploaded": 1, "existing": 0})
        with patch.object(
            export_manager.main.media_manager, "upload_media_bulk", upload
        ):
            await handle_media_upload("h", "op", _files(1), {})
        # Must not raise even though no wrapper was supplied.
        upload.call_args.kwargs["progress_callback"](0.5)

    @pytest.mark.asyncio
    async def test_silent_flag_is_reported_back(self, mw_mock):
        upload = AsyncMock(return_value={"success": True, "uploaded": 1})
        with patch.object(
            export_manager.main.media_manager, "upload_media_bulk", upload
        ):
            result = await handle_media_upload("h", "op", _files(1), {}, silent=True)

        assert result["silent"] is True

    @pytest.mark.asyncio
    async def test_cancellation_stops_before_the_next_batch(self, mw_mock):
        upload = AsyncMock(return_value={"success": True, "uploaded": 5, "existing": 2})
        mw_mock.progress.want_cancel.return_value = True
        progress = MagicMock()
        with patch.object(
            export_manager.main.media_manager, "upload_media_bulk", upload
        ):
            result = await handle_media_upload("h", "op", _files(250), {}, progress)

        assert upload.await_count == 1
        assert result["cancelled"] is True
        # The cancelled batch's counts are not added to the summary.
        assert (result["uploaded"], result["existing"]) == (0, 0)
        # No completion signal when the run was cancelled.
        progress.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancellation_after_a_good_batch_keeps_that_batch(self, mw_mock):
        upload = AsyncMock(return_value={"success": True, "uploaded": 5, "existing": 2})
        mw_mock.progress.want_cancel.side_effect = [False, True, False]
        progress = MagicMock()
        with patch.object(
            export_manager.main.media_manager, "upload_media_bulk", upload
        ):
            result = await handle_media_upload("h", "op", _files(250), {}, progress)

        assert upload.await_count == 2
        assert result["cancelled"] is True
        assert result["uploaded"] == 5
        progress.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_batch_falls_back_to_the_batch_contents(self, mw_mock):
        files = [{"filename": "a.mp3"}, {"filename": "b.mp3"}, "not-a-dict", {}]
        upload = AsyncMock(
            return_value={"success": False, "message": "server exploded"}
        )
        with patch.object(
            export_manager.main.media_manager, "upload_media_bulk", upload
        ):
            result = await handle_media_upload("deck", "op", files, {})

        assert result["uploaded"] == 0
        assert result["failed_filenames"] == ["a.mp3", "b.mp3"]
        assert result["errors"] and "server exploded" in result["errors"][0]

    @pytest.mark.asyncio
    async def test_failed_batch_prefers_the_filenames_the_server_reported(
        self, mw_mock
    ):
        upload = AsyncMock(
            return_value={
                "success": False,
                "message": "partial",
                "failed_filenames": ["x.mp3"],
            }
        )
        with patch.object(
            export_manager.main.media_manager, "upload_media_bulk", upload
        ):
            result = await handle_media_upload("h", "op", _files(3), {})

        assert result["failed_filenames"] == ["x.mp3"]

    @pytest.mark.asyncio
    async def test_repeated_failures_are_reported_once_and_sorted(self, mw_mock):
        upload = AsyncMock(
            side_effect=[
                {
                    "success": True,
                    "uploaded": 1,
                    "existing": 0,
                    "failed_filenames": ["b.mp3", "a.mp3", "b.mp3"],
                    "errors": [],
                },
                {
                    "success": False,
                    "message": "second",
                    "failed_filenames": ["a.mp3", "c.mp3"],
                },
            ]
        )
        progress = MagicMock()
        with patch.object(
            export_manager.main.media_manager, "upload_media_bulk", upload
        ):
            result = await handle_media_upload("h", "op", _files(150), {}, progress)

        assert result["failed"] == 3
        assert result["failed_filenames"] == ["a.mp3", "b.mp3", "c.mp3"]
        progress.assert_called_once_with(1.0)

    @pytest.mark.asyncio
    async def test_partial_batch_error_is_kept_in_the_summary(self, mw_mock):
        upload = AsyncMock(
            return_value={
                "success": True,
                "uploaded": 2,
                "existing": 1,
                "failed_filenames": [],
                "error": "soft warning",
            }
        )
        with patch.object(
            export_manager.main.media_manager, "upload_media_bulk", upload
        ):
            result = await handle_media_upload("h", "op", _files(1), {})

        assert result["errors"] == ["soft warning"]
        assert (result["uploaded"], result["existing"]) == (2, 1)

    @pytest.mark.asyncio
    async def test_an_empty_inner_error_is_not_reported(self, mw_mock):
        upload = AsyncMock(
            return_value={"success": True, "uploaded": 1, "existing": 0, "error": ""}
        )
        with patch.object(
            export_manager.main.media_manager, "upload_media_bulk", upload
        ):
            result = await handle_media_upload("h", "op", _files(1), {})

        assert result["errors"] == []

    @pytest.mark.asyncio
    async def test_an_unexpected_error_is_returned_instead_of_propagating(
        self, mw_mock
    ):
        upload = AsyncMock(side_effect=RuntimeError("network down"))
        with patch.object(
            export_manager.main.media_manager, "upload_media_bulk", upload
        ):
            result = await handle_media_upload("deck", "op", _files(1), {})

        assert result["uploaded"] == 0
        assert result["failed"] == 0
        assert result["cancelled"] is False
        assert len(result["errors"]) == 1 and "network down" in result["errors"][0]

    @pytest.mark.asyncio
    async def test_an_unexpected_error_mid_batch_keeps_reporting_partial_progress(
        self, mw_mock
    ):
        calls = {"n": 0}

        async def _upload(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"success": True, "uploaded": 100, "existing": 0}
            raise RuntimeError("kaput")

        upload = AsyncMock(side_effect=_upload)
        with patch.object(
            export_manager.main.media_manager, "upload_media_bulk", upload
        ):
            result = await handle_media_upload("deck", "op", _files(150), {})

        assert result["uploaded"] == 100
        assert result["cancelled"] is False
        assert len(result["errors"]) == 1 and "kaput" in result["errors"][0]


# ──────────────────────────────────────────────────────────────────────
# _sync_handle_media_upload
# ──────────────────────────────────────────────────────────────────────


class TestSyncHandleMediaUpload:
    def _run(self, mw_mock, result=None, files=2, silent=False, raises=None):
        captured = {}

        def fake_run_async(coro, *a, **kw):
            captured["coro"] = coro
            captured["kwargs"] = kw
            if raises is not None:
                raise raises
            return result

        mw_mock.taskman.run_on_main.side_effect = lambda fn: fn()
        with (
            patch("export_manager._sync_run_async", side_effect=fake_run_async),
            patch("export_manager.complete_media_progress") as complete,
            patch("export_manager.show_media_progress") as show,
            patch("export_manager.update_media_progress") as update,
        ):
            returned = _sync_handle_media_upload(
                "hash",
                "op",
                [{"filename": f"f{i}.mp3"} for i in range(files)],
                {},
                silent,
            )
        return returned, complete, show, update, captured

    def test_runs_the_async_upload_with_the_given_inputs(self, mw_mock):
        result = {"uploaded": 2, "existing": 1, "failed": 0, "errors": []}
        returned, _complete, show, _update, captured = self._run(mw_mock, result)

        assert returned is result
        assert captured["coro"] is handle_media_upload
        kwargs = dict(captured["kwargs"])
        kwargs.pop("progress_callback_wrapper")
        assert kwargs == {
            "deck_hash": "hash",
            "bulk_operation_id": "op",
            "all_files_info": [{"filename": "f0.mp3"}, {"filename": "f1.mp3"}],
            "file_paths": {},
            "silent": False,
        }
        show.assert_called_once_with("upload", 2)

    @pytest.mark.parametrize(
        "result,expected_success",
        [
            ({"uploaded": 2, "existing": 1, "failed": 0, "errors": []}, True),
            ({"uploaded": 1, "existing": 0, "failed": 0, "errors": []}, True),
            ({"uploaded": 0, "existing": 3, "failed": 0, "errors": []}, False),
            ({"uploaded": 0, "existing": 0, "failed": 0, "errors": []}, False),
            ({"uploaded": 5, "existing": 0, "failed": 1, "errors": ["nope"]}, False),
        ],
    )
    def test_completion_state_reflects_whether_files_were_uploaded(
        self, mw_mock, result, expected_success
    ):
        _returned, complete, _show, _update, _captured = self._run(mw_mock, result)

        complete.assert_called_once()
        assert complete.call_args.args[0] is expected_success
        assert complete.call_args.args[1]  # a non-empty status message

    def test_progress_is_reported_against_the_file_count(self, mw_mock):
        mw_mock.taskman.run_on_main.side_effect = lambda fn: fn()
        with (
            patch("export_manager.complete_media_progress"),
            patch("export_manager.show_media_progress"),
            patch("export_manager.update_media_progress") as update,
            patch("export_manager._sync_run_async") as run_async,
        ):
            run_async.side_effect = lambda coro, **kw: kw["progress_callback_wrapper"](
                0.5
            ) or {"uploaded": 1, "existing": 0, "failed": 0, "errors": []}
            _sync_handle_media_upload(
                "hash", "op", [{"filename": f"f{i}"} for i in range(8)], {}, False
            )

        update.assert_called_once_with(0.5, 4)

    def test_a_crash_returns_a_failure_summary_instead_of_raising(self, mw_mock):
        returned, complete, show, _update, _captured = self._run(
            mw_mock, raises=RuntimeError("offline"), files=3, silent=True
        )

        assert returned == {
            "uploaded": 0,
            "existing": 0,
            "failed": 3,
            "errors": ["Upload failed: offline"],
            "cancelled": False,
            "silent": True,
        }
        assert complete.call_args.args[0] is False
        show.assert_called_once_with("upload", 3)

    def test_requires_a_running_anki_instance(self, mw_mock):
        with patch.object(export_manager, "mw", None):
            with pytest.raises(AssertionError):
                _sync_handle_media_upload("h", "o", [], {}, False)

    def test_a_result_missing_optional_keys_still_completes(self, mw_mock):
        returned, complete, _show, _update, _captured = self._run(
            mw_mock, {"uploaded": 3}
        )
        assert returned == {"uploaded": 3}
        assert complete.call_args.args[0] is True
