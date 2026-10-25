"""Tube: YouTube without an account or ads, NewPipe's idea in the phone's own
design. What's hot (YouTube's charts), your subscriptions' newest videos,
search, history and downloads, side by side in a pivot; videos play in the
app, in the background, or from a download.

YouTube is read by yt-dlp in a process of its own (worker.py, client.py);
playback is libmpv drawing into a GL surface (player.py)."""
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QVBoxLayout, QWidget

import ztos

app = ztos.App()

import client  # noqa: E402  (the worker starts at once: yt-dlp takes seconds to load)
import library  # noqa: E402
import state  # noqa: E402

state.app = app
state.worker = client.Worker()

import downloads as downloads_mod  # noqa: E402
import player as playback  # noqa: E402
from pages import (ChannelsPage, SettingsPage, VideoPage, freeze_video, open_channel,  # noqa: E402
                   open_video, video_menu)
from widgets import VideoRow, Words, note  # noqa: E402

state.downloads = downloads_mod.Downloads(state.worker)

CHARTS = {
    "global": ("PL4fGSI1pDJn5kI81J1fYWK5eZRl1zJ5kM", "global"),
    "india": ("PL4fGSI1pDJn40WjZ6utkIuj2rNg-7iGsq", "india"),
    "us": ("PL4fGSI1pDJn6O1LS0XSdF3RyO0Rq_LDeI", "us"),
    "uk": ("PL4fGSI1pDJn688ebB8czINn0_nov50e3A", "uk"),
}
FEED_FRESH = 30 * 60


class Rows(QWidget):
    """A list inside a page that is filled again as a whole."""

    def __init__(self):
        super().__init__()
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(0)

    def clear(self):
        while self.box.count():
            item = self.box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def add(self, w: QWidget) -> QWidget:
        self.box.addWidget(w)
        return w

    def videos(self, items: list, empty: str, show_channel: bool = True, extra_menu=None, note_of=None,
               progress_of=None):
        self.clear()
        if not items:
            self.add(note(empty))
        for v in items:
            row = VideoRow(v, on_tap=lambda v=v: open_video(v), show_channel=show_channel,
                           note=note_of(v) if note_of else "", progress=progress_of(v) if progress_of else 0.0)
            row.held.connect(lambda row=row: video_menu(row, extra_menu(row.video) if extra_menu else ()))
            self.add(row)
        page = self.parentWidget()
        while page is not None and not isinstance(page, ztos.Page):
            page = page.parentWidget()
        if page is not None:
            page._changed(explicit=True)


class Hot(ztos.Page):
    """YouTube took its Trending page away in 2025; its weekly charts stay."""

    def __init__(self):
        super().__init__("what's hot")
        self.region = library.setting("region")
        self.loaded_region = None
        self.add(Words([(k, v[1]) for k, v in CHARTS.items()], self.region, self._choose))
        self.caption = self.section("TOP MUSIC VIDEOS THIS WEEK")
        self.rows = self.add(Rows())
        self.request = None

    def shown(self):
        if self.loaded_region != self.region:
            self.load()

    def _choose(self, key: str):
        self.region = key
        library.set_setting("region", key)
        self.load()

    def load(self):
        if self.request:
            state.worker.forget(self.request)
        self.busy(True)
        self.loaded_region = self.region
        playlist = CHARTS[self.region][0]
        self.request = state.worker.call("playlist", self._got, self._failed,
                                         url=f"https://www.youtube.com/playlist?list={playlist}", count=50)

    def _got(self, data: dict):
        self.request = None
        self.busy(False)
        self.rows.videos(data.get("items") or [], "The chart is empty.")

    def _failed(self, err: str):
        self.request = None
        self.busy(False)
        self.loaded_region = None
        self.rows.clear()
        self.rows.add(note(f"Couldn't load the chart: {err}"))


class Subscriptions(ztos.Page):
    def __init__(self):
        super().__init__("subscriptions")
        self.channels = self.entry("channels", "", self._open_channels)
        self.space(4)
        self.rows = self.add(Rows())
        self.request = None
        self.loaded = False
        self.loaded_ids = set()     # the channels the list shows
        self._count()

    def _count(self):
        n = len(library.subscriptions())
        self.channels.set_detail(f"{n} channel{'s' if n != 1 else ''}" if n else "none yet: tap to import from NewPipe or YouTube")

    def _open_channels(self):
        ztos.haptic("tap")
        app.push(ChannelsPage(self._subs_changed))

    def _subs_changed(self):
        self._count()
        self.load(force=True)

    def _ids(self) -> set:
        return {s["id"] for s in library.subscriptions()}

    def shown(self):
        self._count()
        if not self.loaded:
            cache = library.feed_cache()
            if cache.get("items"):
                self.rows.videos(cache["items"], "")
                self.loaded_ids = set(cache.get("channels") or (v.get("channel_id") for v in cache["items"]))
            self.loaded = True
            if time.time() - cache.get("at", 0) > FEED_FRESH:
                self.load()
                return
        if self._ids() != self.loaded_ids:
            self.load(force=True)       # subscribed or unsubscribed on another page

    def load(self, force: bool = False):
        subs = library.subscriptions()
        if not subs:
            self.loaded_ids = set()
            self.rows.clear()
            self.rows.add(note("Subscribe to channels and their new videos show up here."))
            return
        if self.request and not force:
            return
        if self.request:
            state.worker.forget(self.request)
        self.busy(True)
        self.asked_ids = {s["id"] for s in subs}
        self.request = state.worker.call("feed", self._got, self._failed, channels=[s["id"] for s in subs])

    def _got(self, data: dict):
        self.request = None
        self.busy(False)
        items = data.get("items") or []
        self.loaded_ids = self.asked_ids
        library.set_feed(items, sorted(self.asked_ids))
        self.rows.videos(items[:120], "No videos from your channels.")
        if data.get("failed"):
            ztos.notify("Subscriptions", f"{len(data['failed'])} channels couldn't be read")

    def _failed(self, err: str):
        self.request = None
        self.busy(False)
        ztos.notify("Subscriptions", f"couldn't refresh: {err[:80]}")


class Search(ztos.Page):
    def __init__(self):
        super().__init__("search")
        self.box = self.textbox("", placeholder="search YouTube", on_submit=self.search)
        self.status = self.text("", size=12, color=ztos.GREY)
        self.status.hide()
        self.rows = self.add(Rows())
        self.request = None

    def _say(self, text: str):
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def search(self, query: str):
        query = query.strip()
        if not query:
            return
        ztos.osk(False)
        ztos.haptic("tap")
        if self.request:
            state.worker.forget(self.request)
        self.busy(True)
        self._say(f"searching for “{query}”...")
        self.request = state.worker.call("search", self._got, self._failed, query=query, count=25)

    def _got(self, items: list):
        self.request = None
        self.busy(False)
        self._say("")
        self.rows.videos(items, "Nothing found.")

    def _failed(self, err: str):
        self.request = None
        self.busy(False)
        self._say(f"search failed: {err}")


class History(ztos.Page):
    def __init__(self):
        super().__init__("history")
        self.rows = self.add(Rows())

    def shown(self):
        self.fill()

    def fill(self):
        items = library.history()
        self.rows.videos(items, "Videos you watch show up here.",
                         extra_menu=lambda v: [("remove from history", lambda: self._forget(v["id"])),
                                               ("clear all history", self._clear)],
                         progress_of=lambda v: (v.get("position") or 0) / v["duration"] if v.get("duration") else 0)

    def _forget(self, video_id: str):
        library.forget(video_id)
        self.fill()

    def _clear(self):
        ztos.message("Clear history?", "Every video you watched, and where you stopped in it.",
                     ("clear", "cancel"), lambda i: (library.forget(), self.fill()) if i == 0 else None)


class Downloaded(ztos.Page):
    def __init__(self):
        super().__init__("downloads")
        self.rows = self.add(Rows())
        state.downloads.changed.connect(self._changed_downloads)
        self._pending = False

    def shown(self):
        self.fill()

    def _changed_downloads(self):
        # progress arrives twice a second: redraw at most that often, and only when seen
        if self.isVisible() and not self._pending:
            self._pending = True
            QTimer.singleShot(500, self.fill)

    def fill(self):
        self._pending = False
        self.rows.clear()
        active = list(state.downloads.active.items())
        done = library.downloads()
        if not active and not done:
            self.rows.add(note("Hold a video and choose download, or tap download on its page. "
                               "Videos go to Videos/Tube, sound to Music/Tube."))
        for rid, item in active:
            pct = int(item["progress"] * 100)
            row = VideoRow(item["video"], note=f"downloading {item['kind']}: {pct}%")
            row.held.connect(lambda row=row, rid=rid: ztos.menu(row, [("cancel", lambda: state.downloads.cancel(rid))]))
            self.rows.add(row)
        for d in done:
            mb = d.get("size", 0) / 1048576
            size = f"{mb:.1f} MB" if mb < 10 else f"{mb:.0f} MB"
            row = VideoRow(d, on_tap=lambda d=d: open_video(d, local=d),
                           note=("sound" if d.get("kind") == "audio" else "video") + f" • {size}")
            row.held.connect(lambda row=row, d=d: ztos.menu(row, [("delete", lambda: self._delete(d))]))
            self.rows.add(row)
        self._changed(explicit=True)

    def _delete(self, d: dict):
        library.remove_download(d)
        self.fill()


class Home(ztos.Pivot):
    def __init__(self):
        self.hot, self.subs, self.find = Hot(), Subscriptions(), Search()
        self.history, self.saved = History(), Downloaded()
        super().__init__([self.hot, self.subs, self.find, self.history, self.saved])
        self.b_now = ztos.RoundButton("", "now playing", self._now_playing)
        self.b_play = ztos.RoundButton("play", "play", lambda: playback.get().toggle())
        self.bar(self.b_now, self.b_play, ztos.RoundButton("refresh", "refresh", self._refresh),
                 ztos.RoundButton("settings", "settings", lambda: app.push(SettingsPage())))
        p = playback.get()
        p.paused.connect(self._paused)
        p.changed.connect(lambda v: self._paused(p.is_paused))
        self._paused(True)

    def _paused(self, paused: bool):
        has = bool(playback.get().video)
        self.b_play.set_glyph("play" if paused or not has else "pause")
        self.b_play.set_label("play" if paused or not has else "pause")

    def _now_playing(self):
        v = playback.get().video
        if v:
            open_video(v)
        else:
            ztos.notify("Tube", "nothing is playing")

    def _refresh(self):
        ztos.haptic("tap")
        page = self.current()
        if page is self.hot:
            self.hot.load()
        elif page is self.subs:
            self.subs.load(force=True)
        elif page is self.history:
            self.history.fill()
        elif page is self.saved:
            self.saved.fill()


def _app_state(s):
    """The app left the screen: sound only (or a pause); back: the picture again."""
    p = playback.get()
    if not p.video:
        return
    page = app.page
    if s == Qt.ApplicationActive:
        if isinstance(page, VideoPage) and page._mine() and not page.audio_only:
            p.want_video(True)
            page._attach()
    elif p.with_video:
        freeze_video()      # (the shell's turn away pictures the app's last frame)
        if library.setting("background"):
            p.want_video(False)
        else:
            p.pause()


def _now_playing_tile(video: dict):
    """Start's tile while something plays: its picture edge to edge, the title
    on it, as the Music tile did. Paused, ended or stopped: the plain tile."""
    if not video or playback.get().is_paused:
        ztos.tile_clear()
        return
    import os
    import widgets
    thumb, image = video.get("thumb") or "", ""
    for url in (widgets.poster_of(thumb), thumb):
        if url and os.path.exists(widgets.thumbs._path(url)):
            image = widgets.thumbs._path(url)
            break
    ztos.tile_update("now playing", [video.get("title") or "", video.get("channel") or ""], image=image)


def _quit():
    # Closed from the app switcher, the app ends even if mpv does not let go
    # (a stuck decoder once kept it, and its sound, alive): three seconds for
    # the goodbyes, then out.
    import os
    import threading
    bail = threading.Timer(3.0, os._exit, (0,))
    bail.daemon = True      # (a clean exit does not wait for it)
    bail.start()
    ztos.tile_clear()
    playback.get().close()
    state.worker.close()


QGuiApplication.instance().applicationStateChanged.connect(_app_state)
playback.get().changed.connect(_now_playing_tile)
playback.get().paused.connect(lambda _paused: _now_playing_tile(playback.get().video))
playback.get().ended.connect(ztos.tile_clear)
app.qt.aboutToQuit.connect(_quit)
app.run(Home())
