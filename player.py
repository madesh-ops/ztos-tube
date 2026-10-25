"""Playback: one libmpv for the whole app, drawing into a native GL surface.

The surface is a QOpenGLWindow in a window container: a Wayland subsurface
that the shell composites itself, so a frame never passes through Qt's
raster window (the SDK keeps that for its rows). mpv decodes on the CPU (the
M20 has no video decoder driver yet), so streams are H.264 at 30 fps or
fewer, up to 720p by default.

When no page shows the video (it was left, or the app went to the
background) the player plays the sound alone: the video track goes off, so
nothing is decoded or drawn, and the sound carries on without a break (the
picture's stream is still read). ztOS's perfd leaves an app that plays sound
running, so this is background play.
"""
import ctypes
import locale
import os
import sys
import time
from typing import Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtOpenGL import QOpenGLWindow
from PySide6.QtWidgets import QWidget

import library

locale.setlocale(locale.LC_NUMERIC, "C")    # libmpv refuses to start otherwise
import mpv  # noqa: E402

_egl = ctypes.CDLL("libEGL.so.1")
_egl.eglGetProcAddress.restype = ctypes.c_void_p
_egl.eglGetProcAddress.argtypes = [ctypes.c_char_p]


@mpv.MpvGlGetProcAddressFn
def _gl_proc(_, name):
    return _egl.eglGetProcAddress(name) or 0


class Player(QObject):
    position = Signal(float, float)     # seconds, duration
    paused = Signal(bool)
    buffering = Signal(bool)
    ended = Signal()
    changed = Signal(dict)              # a new video
    failed = Signal(str)
    mode = Signal(bool)                 # True: picture and sound, False: sound only
    _frame = Signal()

    def __init__(self):
        super().__init__()
        # hwdec: the phone's video codec where there is one (the M20's MFC, a
        # V4L2 decoder: frames are copied back for mpv's GL), software otherwise
        # (32 frames for the codec to decode into: FFmpeg's 20 are mostly the
        # stream's own reference frames at 1080p60, which left mpv one or two
        # and the picture a frame every few seconds)
        self.mpv = mpv.MPV(vo="libmpv", hwdec="v4l2m2m-copy", vd_lavc_o="num_capture_buffers=32",
                           ytdl=False, osc=False, input_default_bindings=False,
                           cache="yes", demuxer_max_bytes="48MiB", demuxer_readahead_secs=60,
                           keep_open="yes", audio_client_name="Tube", idle="yes",
                           osd_font_size=32, osd_bar_h=2.5, video_sync="audio",
                           # the G71 drops an eighth of the frames scaling 720p to the
                           # whole screen with mpv's lanczos; bilinear drops none
                           scale="bilinear", cscale="bilinear", dscale="bilinear", dither_depth="no",
                           correct_downscaling=False, linear_downscaling=False, sigmoid_upscaling=False,
                           hdr_compute_peak=False)
        self.video: Optional[dict] = None
        self.height = ""                # the stream's height, "720"
        self.with_video = True
        self.is_paused = True
        self.pos = 0.0
        self.dur = 0.0
        self._last_saved = 0.0
        self._last_told = 0.0
        self._waiting_start: Optional[float] = None
        self._loading = False
        self._loaded_video = False      # the file loaded has a picture to turn on and off
        self.last_frame = None          # the picture as it was last, for a page's turn (pages.py)
        self.surface: Optional["VideoSurface"] = None
        self.mpv.observe_property("time-pos", self._on_time)
        self.mpv.observe_property("duration", self._on_duration)
        self.mpv.observe_property("pause", lambda _n, v: self._emit_pause(bool(v)))
        self.mpv.observe_property("paused-for-cache", lambda _n, v: self.buffering.emit(bool(v)))
        self.mpv.observe_property("eof-reached", self._on_eof)
        self.mpv.register_event_callback(self._on_event)
        self.position.connect(self._maybe_save)     # (queued: this runs on the app's thread)

    # -- from mpv's thread: only signals cross over --------------------------------------
    def _on_time(self, _name, value):
        if value is None:
            return
        self.pos = float(value)
        now = time.monotonic()
        if now - self._last_told >= 0.25:     # (mpv tells every frame: the seek bar needs less)
            self._last_told = now
            self.position.emit(self.pos, self.dur)

    def _maybe_save(self, _pos, _dur):
        if time.time() - self._last_saved > 10 and self.video:
            self._last_saved = time.time()
            self.save_position()

    def _on_duration(self, _name, value):
        if value:
            self.dur = float(value)
            self.position.emit(self.pos, self.dur)

    def _emit_pause(self, paused: bool):
        self.is_paused = paused
        self.paused.emit(paused)

    def _on_eof(self, _name, value):
        if value:
            self.ended.emit()

    def _on_event(self, event):
        try:
            kind = event.event_id.value if hasattr(event.event_id, "value") else event.event_id
        except Exception:
            return
        if kind == mpv.MpvEventID.END_FILE:
            data = getattr(event, "data", None)
            reason = getattr(data, "reason", None)
            if reason is not None and getattr(reason, "value", reason) == 4:     # error
                self.failed.emit("the stream could not be played")

    # -- what the pages call ------------------------------------------------------------------
    def surface_ready(self):
        if self._waiting_start is not None and self.video:
            self._load(self._waiting_start)

    def heights(self) -> list:
        if not self.video:
            return []
        return sorted(set(self.video.get("streams") or {}) | set(self.video.get("streams60") or {}), key=int)

    def _stream(self, height: str) -> Optional[str]:
        """The picture at that quality: at 60 frames a second where there is
        one and the setting wants it, else at 30 or fewer."""
        v = self.video or {}
        smooth, plain = v.get("streams60") or {}, v.get("streams") or {}
        if library.setting("fps60"):
            return smooth.get(height) or plain.get(height)
        return plain.get(height) or smooth.get(height)

    def _pick_height(self) -> str:
        wanted = int(library.setting("quality") or 720)
        hs = [int(h) for h in self.heights()]
        if not hs:
            return ""
        under = [h for h in hs if h <= wanted]
        return str(max(under) if under else min(hs))

    def play(self, video: dict, start: Optional[float] = None, with_video: bool = True):
        """A resolved video (worker op "video"), or a downloaded one ("path")."""
        if self.video and self.video.get("id") != video.get("id"):
            self.save_position()
        self.video = video
        self.with_video = with_video
        self.height = self._pick_height()
        self.pos = 0.0
        self.dur = float(video.get("duration") or 0)
        if start is None:
            start = library.resume_point(video["id"]) if library.setting("resume") else 0.0
        self.mpv["force-media-title"] = video.get("title") or ""
        headers = video.get("headers") or {}
        if headers.get("User-Agent"):
            self.mpv["user-agent"] = headers["User-Agent"]
        self._load(start)
        self.mpv.pause = False
        self.is_paused = False      # (mpv says nothing when it already was playing)
        library.watched(video, start, self.dur)
        self.changed.emit(video)
        self.mode.emit(self.with_video)
        self.paused.emit(False)

    def _load(self, start: float):
        if self.with_video and (self.surface is None or self.surface.ctx is None):
            # mpv drops the picture of a file that starts before there is
            # somewhere to draw it: start once the surface has its GL
            self._waiting_start = start
            return
        self._waiting_start = None
        v = self.video
        # (the track as an option of this file: set as a property it can be
        # lost to the load that is still under way)
        options = {"vid": "auto" if self.with_video else "no"}
        if not v.get("live"):
            options["start"] = f"{max(0.0, start):.2f}"     # (a live stream plays from now)
        if v.get("path"):
            self.mpv.loadfile(v["path"], **options)
            self._loaded_video = v.get("kind") != "audio"
        elif not self.with_video and v.get("audio"):
            self.mpv.loadfile(v["audio"], **options)
            self._loaded_video = False
        elif v.get("audio") and self.height and self._stream(self.height):
            self.mpv.loadfile(self._stream(self.height), audio_file=v["audio"], **options)
            self._loaded_video = True
        elif v.get("muxed"):
            self.mpv.loadfile(v["muxed"], **options)
            self._loaded_video = True
        else:
            self.failed.emit("no stream to play")

    def set_height(self, height: str):
        if self.video and height != self.height and height in self.heights():
            self.height = height
            if self.with_video:
                self._load(self.pos)

    def want_video(self, on: bool):
        """Picture and sound, or the sound alone. A file that has the picture
        keeps playing: only its video track goes off (no decoding, nothing to
        draw) and on again. Reloading the sound alone at the same place, as it
        once did, stopped the sound and started it again a moment later every
        time the app was left or came back. A sound-only file is reloaded with
        the picture."""
        if not self.video or on == self.with_video:
            return
        self.with_video = on
        if self._loaded_video:
            self.mpv["vid"] = "auto" if on else "no"
        elif on:
            was_paused = self.is_paused
            self._load(self.pos)
            self.mpv.pause = was_paused
        self.mode.emit(on)

    def toggle(self):
        if self.video:
            self.mpv.pause = not self.is_paused

    def pause(self):
        if self.video:
            self.mpv.pause = True

    def seek(self, seconds: float, relative: bool = False):
        if not self.video:
            return
        target = self.pos + seconds if relative else seconds
        if self._loaded_video and self.with_video and not self.video.get("live") and self._hw_decoding():
            # The phone's video codec loses the stream's headers when mpv
            # flushes it for a seek (H.264 in MP4 carries them once, at the
            # start): it then decodes nothing, and the picture and sound stay
            # put. A fresh load at the new place starts from a key frame with
            # the headers, as opening a video does.
            self._load(max(0.0, min(target, self.dur - 1.0) if self.dur else target))
            return
        try:
            self.mpv.seek(seconds, "relative" if relative else "absolute")
        except SystemError:
            pass

    def _hw_decoding(self) -> bool:
        try:
            return bool(self.mpv.hwdec_current) and self.mpv.hwdec_current != "no"
        except Exception:
            return False

    def osd(self, text: str = ""):
        try:
            if text:
                self.mpv.command("show-text", text, 900)
            else:
                self.mpv.command("show-progress")
        except SystemError:
            pass

    def stop(self):
        self.save_position()
        self.video = None
        self.mpv.command("stop")
        self.changed.emit({})

    def save_position(self):
        if self.video and self.pos > 0:
            library.watched(self.video, self.pos, self.dur)

    def playing(self) -> bool:
        return bool(self.video) and not self.is_paused

    def close(self):
        self.save_position()
        if self.surface:
            self.surface.release()
        self.mpv.terminate()


class VideoSurface(QOpenGLWindow):
    """Where mpv draws. Taps: one toggles pause, two on either side jump ten
    seconds; a drag across seeks."""
    tapped = Signal()

    def __init__(self, player: Player):
        super().__init__(QOpenGLWindow.NoPartialUpdate)
        fmt = QSurfaceFormat.defaultFormat()
        fmt.setSwapInterval(1)
        self.setFormat(fmt)
        self.player = player
        self.ctx = None
        player._frame.connect(self._new_frame, Qt.QueuedConnection)
        self._down = None
        self._last_tap = 0.0
        self._tap_timer = QTimer(self, singleShot=True, interval=280)
        self._tap_timer.timeout.connect(self._single_tap)
        self._seek_from = 0.0

    def initializeGL(self):
        self.ctx = mpv.MpvRenderContext(self.player.mpv, "opengl",
                                        opengl_init_params={"get_proc_address": _gl_proc})
        self.ctx.update_cb = self.player._frame.emit
        self.context().aboutToBeDestroyed.connect(self.release)
        QTimer.singleShot(0, self.player.surface_ready)

    def release(self):
        if self.ctx is not None:
            self.ctx.update_cb = None
            self.ctx.free()
            self.ctx = None

    def _new_frame(self):
        """mpv has a frame for us. On screen it is drawn; hidden (a page
        turning, the poster in its place) it is taken without drawing. A frame
        nobody takes stays in the decoder's buffers, and the phone's video
        codec, out of buffers, stops: mpv, and every call into it, then waits
        for good."""
        if self.isExposed():
            self.update()
        elif self.ctx is not None:
            try:
                self.makeCurrent()
                self.ctx.render(skip_rendering=True, block_for_target_time=False,
                                opengl_fbo={"w": 1, "h": 1, "fbo": self.defaultFramebufferObject()})
                self.doneCurrent()
            except Exception:
                pass

    def paintGL(self):
        if self.ctx is None:
            return
        r = self.devicePixelRatio()
        self.ctx.render(flip_y=True, opengl_fbo={"w": int(self.width() * r), "h": int(self.height() * r),
                                                 "fbo": self.defaultFramebufferObject()})

    # touch arrives as mouse events on Wayland's touch emulation
    def mousePressEvent(self, e):
        self._down = e.position()
        self._seek_from = self.player.pos

    def mouseMoveEvent(self, e):
        if self._down is None:
            return
        dx = e.position().x() - self._down.x()
        if abs(dx) > 24:
            # the width of the picture is 90 seconds
            target = max(0.0, self._seek_from + dx / max(1, self.width()) * 90)
            self.player.seek(target)
            self.player.osd()

    def mouseReleaseEvent(self, e):
        if self._down is None:
            return
        moved = abs(e.position().x() - self._down.x()) + abs(e.position().y() - self._down.y())
        x = e.position().x()
        self._down = None
        if moved > 24:
            return
        now = time.time()
        if now - self._last_tap < 0.28:
            self._tap_timer.stop()
            self._last_tap = 0.0
            step = -10 if x < self.width() / 2 else 10
            self.player.seek(step, relative=True)
            self.player.osd(f"{'-' if step < 0 else '+'}10 s")
            return
        self._last_tap = now
        self._tap_timer.start()

    def _single_tap(self):
        self.player.toggle()
        self.player.osd()
        self.tapped.emit()


player: Optional[Player] = None
surface: Optional[VideoSurface] = None
container: Optional[QWidget] = None


def get() -> Player:
    global player
    if player is None:
        player = Player()
    return player


def video_widget() -> QWidget:
    """The one video surface, in a widget to put in a page."""
    global surface, container
    if container is None:
        surface = VideoSurface(get())
        get().surface = surface
        container = QWidget.createWindowContainer(surface)
        container.setFocusPolicy(Qt.NoFocus)
        container.setStyleSheet("background: black;")
    return container
