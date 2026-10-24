"""The app's side of the worker (worker.py): a QProcess, and replies delivered
on Qt's own thread, so the UI never waits on YouTube."""
import json
import os
import socket
import sys
from typing import Callable, Optional

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal


def clean_environment() -> None:
    """The phone's /etc/environment sends everything through the USB tunnel's
    SOCKS proxy (127.0.0.1:1080). On Wi-Fi, with no tunnel, every request
    would fail: keep the proxy only while it answers."""
    proxy = os.environ.get("all_proxy") or os.environ.get("ALL_PROXY") or ""
    if "127.0.0.1:1080" not in proxy:
        return
    try:
        with socket.create_connection(("127.0.0.1", 1080), timeout=0.3):
            return
    except OSError:
        pass
    for key in ("all_proxy", "ALL_PROXY", "http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        os.environ.pop(key, None)


class Worker(QObject):
    event = Signal(dict)            # {"event": ...} lines: yt-dlp updating, ready

    def __init__(self, parent=None):
        super().__init__(parent)
        self._next = 1
        self._calls: dict = {}
        self._buffer = b""
        self._proc: Optional[QProcess] = None
        self.version = ""
        self.ready = False
        self._start()

    def _start(self):
        clean_environment()
        here = os.path.dirname(os.path.abspath(__file__))     # the app's folder, or its archive
        proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        for key in ("all_proxy", "ALL_PROXY"):
            if key not in os.environ:
                env.remove(key)
        proc.setProcessEnvironment(env)
        proc.setProcessChannelMode(QProcess.SeparateChannels)
        proc.readyReadStandardOutput.connect(self._read)
        proc.readyReadStandardError.connect(lambda: sys.stderr.write(
            bytes(proc.readAllStandardError()).decode(errors="replace")))
        proc.finished.connect(self._died)
        code = f"import sys; sys.path.insert(0, {here!r}); import worker; worker.main()"
        proc.start(sys.executable, ["-c", code])
        self._proc = proc

    def _died(self, code, status):
        print("tube: worker stopped", code, status, file=sys.stderr, flush=True)
        self.ready = False
        calls, self._calls = self._calls, {}
        for cb, err, _ in calls.values():
            if err:
                err("the worker stopped")
        QTimer.singleShot(1000, self._start)

    def _read(self):
        self._buffer += bytes(self._proc.readAllStandardOutput())
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "event" in msg:
                if msg["event"] == "ytdlp":
                    self.version = msg.get("version", self.version)
                    self.ready = msg.get("state") == "ready" or self.ready
                self.event.emit(msg)
                continue
            rid = msg.get("id")
            if rid not in self._calls:
                continue
            cb, err, progress = self._calls[rid]
            if "progress" in msg:
                if progress:
                    progress(msg["progress"])
                continue
            del self._calls[rid]
            if "error" in msg:
                if err:
                    err(msg["error"])
            elif cb:
                cb(msg.get("data"))

    def call(self, op: str, on_done: Optional[Callable] = None, on_error: Optional[Callable] = None,
             on_progress: Optional[Callable] = None, **args) -> int:
        rid = self._next
        self._next += 1
        self._calls[rid] = (on_done, on_error, on_progress)
        line = json.dumps({"id": rid, "op": op, **args}) + "\n"
        self._proc.write(line.encode())
        return rid

    def cancel(self, rid: int):
        self._proc.write((json.dumps({"op": "cancel", "target": rid}) + "\n").encode())

    def forget(self, rid: int):
        """Nobody waits for this reply any more (its page was left)."""
        self._calls.pop(rid, None)

    def close(self):
        if self._proc:
            self._proc.finished.disconnect(self._died)
            self._proc.kill()
            self._proc.waitForFinished(500)
