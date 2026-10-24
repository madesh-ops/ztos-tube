"""Downloads in progress, and telling the pages about them."""
from PySide6.QtCore import QObject, Signal

import library
import ztos


class Downloads(QObject):
    changed = Signal()

    def __init__(self, worker):
        super().__init__()
        self.worker = worker
        self.active: dict = {}      # request id -> {"video", "kind", "progress"}

    def start(self, video: dict, kind: str, height: int = 720):
        for item in self.active.values():
            if item["video"]["id"] == video["id"] and item["kind"] == kind:
                return
        rid = self.worker.call("download", lambda data, v=video: self._done(rid, data),
                               lambda err: self._failed(rid, err),
                               lambda p: self._progress(rid, p),
                               video_id=video["id"], kind=kind, height=height)
        self.active[rid] = {"video": video, "kind": kind, "progress": 0.0}
        ztos.notify("Downloading", video.get("title", ""))
        self.changed.emit()

    def cancel(self, rid: int):
        if rid in self.active:
            self.worker.cancel(rid)

    def _progress(self, rid: int, p: float):
        if rid in self.active:
            self.active[rid]["progress"] = p
            self.changed.emit()

    def _done(self, rid: int, data: dict):
        item = self.active.pop(rid, None)
        if data and data.get("path"):
            if item:
                data["thumb"] = item["video"].get("thumb") or data.get("thumb")
            library.add_download(data)
            ztos.notify("Downloaded", data.get("title", ""))
        self.changed.emit()

    def _failed(self, rid: int, err: str):
        item = self.active.pop(rid, None)
        if err != "cancelled":
            ztos.notify("Download failed", (item["video"].get("title", "") + ": " if item else "") + err[:80])
        self.changed.emit()
