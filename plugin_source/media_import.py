import os
import shutil
from typing import Optional

import aqt
import anki
import aqt.utils
from aqt.operations import QueryOp

from aqt.qt import *
from aqt import mw

try:
    from anki.utils import is_win, is_lin
except ImportError:
    from anki.utils import isWin as is_win
    from anki.utils import isLin as is_lin

def copy_content(input_path: str) -> None:
    counter = 0
    if os.path.isdir(input_path):
        for root, dirs, files in os.walk(input_path):
            for file in files:
                src_path = os.path.join(root, file)
                dst_path = os.path.join(mw.col.media.dir(), file)
                shutil.copy2(src_path, dst_path)
                counter += 1
    return counter
                

def on_success(count: int) -> None:
    mw.col.media.check()
    mw.progress.finish()
    aqt.utils.showInfo(f"AnkiCollab: {count} Media Files imported.")
        
def import_media(path: str):
    op = QueryOp(
        parent=mw,
        op=lambda _: copy_content(path),
        success=on_success,
    )
    op.with_progress("Importing...").run_in_background()

class FileDialog:
    @classmethod
    def create(cls) -> QFileDialog:
        dialog = QFileDialog()
        dialog.setNameFilter(file_name_filter())
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, False)
        if is_win or is_lin: # AnkiHub sanity check.
            dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        return dialog

def file_name_filter() -> str:
    exts_filter = ""
    for ext_list in (aqt.editor.pics, aqt.editor.audio):
        for ext in ext_list:
            exts_filter += f"*.{ext} "
    exts_filter = exts_filter[:-1]
    return f"Image & Audio Files ({exts_filter})"

def get_directory() -> Optional[str]:
    dialog = FileDialog.create()
    dialog.setFileMode(QFileDialog.FileMode.Directory)
    if dialog.exec():
        path = dialog.selectedFiles()[0]
        if isinstance(path, str): # check if path is a valid string
            return path
    return None

def on_media_btn() -> None:
    path = get_directory()
    if path is not None:
        path_str = str(path)
        import_media(path_str)
