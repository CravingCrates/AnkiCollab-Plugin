from __future__ import annotations

import json
import os
from urllib.parse import parse_qs, unquote
from typing import Any, Dict, List, Optional

import aqt
from aqt import mw, gui_hooks
from aqt.qt import (
    QAction,
    QDialog,
    QIcon,
    QUrl,
    QThread,
    QTimer,
    QVBoxLayout,
    QWidget,
    pyqtSignal,
)
from aqt.theme import theme_manager

try:
    from aqt.qt import QWebEnginePage, QWebEngineView
except Exception:
    QWebEnginePage = None
    QWebEngineView = None

from aqt.utils import showInfo

from .api_client import api_client
from .auth_manager import auth_manager
from .utils import get_logger

logger = get_logger("ankicollab.notifications")


if QWebEnginePage is not None:
    class _NotificationPage(QWebEnginePage):
        def __init__(self, dialog: "NotificationCenterDialog", parent=None) -> None:
            super().__init__(parent)
            self._dialog = dialog

        def acceptNavigationRequest(self, url, nav_type, is_main_frame):  # type: ignore[override]
            if url.scheme() == "ankicollab-refresh":
                self._dialog._handle_refresh()
                return False
            if url.scheme() == "ankicollab-open-guid":
                self._dialog._handle_open_guid(url)
                return False
            return super().acceptNavigationRequest(url, nav_type, is_main_frame)
else:
    _NotificationPage = None


class _UnreadFetchThread(QThread):
    fetched = pyqtSignal(object)

    def run(self) -> None:
        payload: Dict[str, Any] = {"ok": False, "unread_count": 0, "groups": []}
        try:
            response = api_client.get("/GetNotifications", auth=True, timeout=12)
            if response.status_code == 200:
                data = response.json()
                payload = {
                    "ok": True,
                    "unread_count": int(data.get("unread_count", 0)),
                    "groups": data.get("groups", []) or [],
                }
        except Exception as err:
            logger.warning("Notification unread fetch failed: %s", err)
        self.fetched.emit(payload)


class _CenterFetchThread(QThread):
    fetched = pyqtSignal(object)

    def __init__(self, unread_cache: Optional[Dict[str, Any]] = None) -> None:
        super().__init__()
        # use a dict so we can safely copy and avoid mutating the caller's cache
        self._unread_cache = unread_cache or {}

    def run(self) -> None:
        if self._unread_cache:
            unread_payload: Dict[str, Any] = dict(self._unread_cache)
        else:
            unread_payload: Dict[str, Any] = {"unread_count": 0, "groups": []}
        history_payload: Dict[str, Any] = {"total": 0, "offset": 0, "limit": 200, "items": []}
        commit_snapshots: Dict[str, Any] = {}

        try:
            unread_response = api_client.get("/GetNotifications", auth=True, timeout=12)
            if unread_response.status_code == 200:
                unread_payload = unread_response.json()
        except Exception as err:
            logger.warning("Failed to fetch unread notifications for center: %s", err)

        try:
            history_response = api_client.get("/GetNotificationsHistory?offset=0&limit=200", auth=True, timeout=12)
            if history_response.status_code == 200:
                history_payload = history_response.json()
        except Exception as err:
            logger.warning("Failed to fetch history notifications for center: %s", err)

        commit_ids: List[int] = []
        for item in history_payload.get("items", [])[:20]:
            commit_id = item.get("commit_id")
            if isinstance(commit_id, int) and commit_id not in commit_ids:
                commit_ids.append(commit_id)

        for commit_id in commit_ids:
            try:
                snapshot_response = api_client.get(
                    "/GetCommitSnapshot/" + str(commit_id),
                    auth=True,
                    timeout=8,
                )
                if snapshot_response.status_code == 200:
                    commit_snapshots[str(commit_id)] = snapshot_response.json()
            except Exception as err:
                logger.warning("Failed to fetch commit snapshot %s: %s", commit_id, err)

        self.fetched.emit(
            {
                "unread": unread_payload,
                "history": history_payload,
                "commit_snapshots": commit_snapshots,
            }
        )


_POLL_INTERVAL_MS = 5 * 60 * 1000  # 5 minutes


class NotificationCenterManager:
    def __init__(self) -> None:
        self._action: Optional[QAction] = None
        self._fetch_thread: Optional[_UnreadFetchThread] = None
        self._center_fetch_thread: Optional[_CenterFetchThread] = None
        self._center_dialog: Optional[NotificationCenterDialog] = None
        self._unread_payload: Dict[str, Any] = {"ok": False, "unread_count": 0, "groups": []}
        self._menu_attached = False
        self._poll_timer: Optional[QTimer] = None

    def attach(self, parent_menu: QWidget) -> None:
        if self._menu_attached:
            return

        self._action = QAction("Notifications", mw)
        self._action.setToolTip("Open AnkiCollab notification center")
        self._action.triggered.connect(self.open_center)

        if hasattr(parent_menu, "addAction"):
            parent_menu.addAction(self._action)

        self._menu_attached = True

        self._poll_timer = QTimer()
        self._poll_timer.timeout.connect(self.schedule_refresh)
        self._poll_timer.start(_POLL_INTERVAL_MS)

    def set_visible(self, visible: bool) -> None:
        if self._action is not None:
            self._action.setVisible(visible)

    def schedule_refresh(self) -> None:
        if not auth_manager.is_logged_in():
            self._set_unread_count(0)
            return

        if self._fetch_thread is not None and self._fetch_thread.isRunning():
            return

        self._fetch_thread = _UnreadFetchThread()
        self._fetch_thread.fetched.connect(self._on_unread_fetched)
        self._fetch_thread.start()

    def _on_unread_fetched(self, payload: Dict[str, Any]) -> None:
        self._unread_payload = payload
        self._set_unread_count(int(payload.get("unread_count", 0)))

    def _set_unread_count(self, count: int) -> None:
        if self._action is not None:
            count = max(0, count)
            if count == 0:
                suffix = ""
            elif count <= 10:
                suffix = f" ({count})"
            elif count <= 99:
                suffix = " (10+)"
            else:
                suffix = " (^_^)"

            label = "Notifications" + suffix
            self._action.setText(label)
            self._action.setIcon(QIcon())

    def open_center(self) -> None:
        if not auth_manager.is_logged_in():
            showInfo("Please log in to view notifications.")
            return

        if self._center_dialog is not None:
            self._center_dialog.raise_()
            self._center_dialog.activateWindow()
            return

        unread_payload = self._unread_payload if self._unread_payload.get("ok") else {"unread_count": 0, "groups": []}
        history_payload: Dict[str, Any] = {"total": 0, "offset": 0, "limit": 200, "items": []}

        self._center_dialog = NotificationCenterDialog(
            unread_payload,
            history_payload,
            {},
            parent=mw,
            loading=True,
            on_refresh=self._refresh_center_payload,
        )

        self._refresh_center_payload()

        self._center_dialog.exec()

        payload_after_close = self._center_dialog.last_payload()
        self._center_dialog = None
        self._mark_as_read(payload_after_close)
        self.schedule_refresh()

    def _mark_as_read(self, payload: Dict[str, Any]) -> None:
        unread_payload = payload.get("unread", {"groups": []}) if isinstance(payload, dict) else {"groups": []}

        all_ids: List[int] = []
        for group in unread_payload.get("groups", []):
            for item in group.get("notifications", []):
                note_id = item.get("id")
                if isinstance(note_id, int):
                    all_ids.append(note_id)

        if all_ids:
            try:
                api_client.post_json("/MarkNotificationsRead", {"ids": all_ids}, timeout=10, auth=True)
            except Exception as err:
                logger.warning("Failed to mark notifications as read: %s", err)

    def _on_center_payload(self, payload: Dict[str, Any]) -> None:
        if self._center_dialog is not None:
            self._center_dialog.update_payload(payload, loading=False)

    def _refresh_center_payload(self) -> None:
        if self._center_fetch_thread is not None and self._center_fetch_thread.isRunning():
            return
        if self._center_dialog is not None:
            self._center_dialog.set_loading(True)

        self._center_fetch_thread = _CenterFetchThread(self._unread_payload)
        self._center_fetch_thread.fetched.connect(self._on_center_payload)
        self._center_fetch_thread.start()

class NotificationCenterDialog(QDialog):
  def __init__(
    self,
    unread_payload: Dict[str, Any],
    history_payload: Dict[str, Any],
    commit_snapshots: Dict[str, Any],
    parent: Optional[QWidget] = None,
    loading: bool = False,
    on_refresh: Optional[Any] = None,
  ) -> None:
        super().__init__(parent)
        self.setWindowTitle("AnkiCollab Notifications")
        self.resize(880, 640)
        self._web = None
        self._web_loaded = False
        self._on_refresh = on_refresh
        self._payload = {
            "unread": unread_payload,
            "history": history_payload,
            "commit_snapshots": commit_snapshots,
            "media_dir": mw.col.media.dir() if getattr(mw, "col", None) else "",
            "loading": loading,
            "dark_mode": bool(getattr(theme_manager, "night_mode", False)),
        }

        layout = QVBoxLayout(self)

        if QWebEngineView is None:
            from aqt.qt import QTextBrowser

            fallback = QTextBrowser(self)
            fallback.setPlainText("QWebEngineView is unavailable in this Anki build.")
            layout.addWidget(fallback)
            return

        web = QWebEngineView(self)
        if _NotificationPage is not None:
            web.setPage(_NotificationPage(self, web))
        layout.addWidget(web)
        self._web = web

        index_path = os.path.join(
            os.path.dirname(__file__),
            "notifications_webview",
            "index.html",
        )

        if os.path.exists(index_path):
            def on_load_finished(ok: bool) -> None:
                if not ok:
                    return
                self._web_loaded = True
                self._render_payload()

            web.loadFinished.connect(on_load_finished)
            web.load(QUrl.fromLocalFile(index_path))
        else:
            from aqt.qt import QTextBrowser

            fallback = QTextBrowser(self)
            fallback.setPlainText("Notification webview assets are missing.")
            layout.addWidget(fallback)

  def _render_payload(self) -> None:
        if self._web is None or not self._web_loaded:
            return
        # json.dumps does not escape U+2028 (LINE SEPARATOR) or U+2029
        # (PARAGRAPH SEPARATOR), which were forbidden inside JS string literals
        # before ES2019. Escape them defensively to stay compatible with any
        # QWebEngine version.
        payload_json = (
            json.dumps(self._payload)
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029")
        )
        script = (
            "if (window.AnkiCollabNotifications) {"
            "  window.AnkiCollabNotifications.render(" + payload_json + ");"
            "}"
        )
        page = self._web.page()
        if page is not None:
            page.runJavaScript(script)

  def update_payload(self, payload: Dict[str, Any], loading: bool = False) -> None:
        self._payload = {
            "unread": payload.get("unread", {"unread_count": 0, "groups": []}),
            "history": payload.get("history", {"total": 0, "offset": 0, "limit": 200, "items": []}),
            "commit_snapshots": payload.get("commit_snapshots", {}),
            "media_dir": mw.col.media.dir() if getattr(mw, "col", None) else "",
            "loading": loading,
            "dark_mode": bool(getattr(theme_manager, "night_mode", False)),
        }
        self._render_payload()

  def last_payload(self) -> Dict[str, Any]:
        return self._payload

  def set_loading(self, loading: bool) -> None:
      self._payload["loading"] = loading
      self._render_payload()

  def _handle_refresh(self) -> None:
      if callable(self._on_refresh):
        self._on_refresh()

  def _handle_open_guid(self, url: QUrl) -> None:
      if not getattr(mw, "col", None) or not getattr(mw.col, "db", None):
          return

      query_guid = parse_qs(url.query() or "").get("guid", [""])[0]
      guid = unquote(query_guid).strip()
      if not guid:
          return

      try:
          nid = mw.col.db.scalar("SELECT id FROM notes WHERE guid = ?", guid)
      except Exception as err:
          logger.warning("Failed to resolve note GUID %s: %s", guid, err)
          return

      if not nid:
          return

      self.accept()

      def _open_browser() -> None:
          browser = aqt.dialogs.open("Browser", aqt.mw)
          browser.form.searchEdit.lineEdit().setText(f"nid:{nid}")
          browser.onSearchActivated()

      if hasattr(mw, "taskman") and hasattr(mw.taskman, "run_on_main"):
          mw.taskman.run_on_main(_open_browser)
      else:
          QTimer.singleShot(0, _open_browser)


notification_center = NotificationCenterManager()


def init_notification_center(parent_menu: QWidget) -> None:
    notification_center.attach(parent_menu)


def refresh_notifications() -> None:
    notification_center.schedule_refresh()


def set_notification_visibility(visible: bool) -> None:
    notification_center.set_visible(visible)


def register_sync_refresh_hook() -> None:
    def _on_sync_finished(*_args: Any, **_kwargs: Any) -> None:
        notification_center.schedule_refresh()

    gui_hooks.sync_did_finish.append(_on_sync_finished)
