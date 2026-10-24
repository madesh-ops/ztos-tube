"""Tube's list rows, in the phone's style: a video is its picture at the left,
the title in Segoe Semilight beside it, and the channel, views and age in
grey under it. Rows lean under a finger like every other ztOS row (they are
ztos._Pressable), and draw themselves once into a picture."""
import hashlib
import os
import re
import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QWidget

import ztos
from ztos import FONT_REGULAR, FONT_SEMILIGHT, FONT_SEMIBOLD, GREY

CACHE = os.path.expanduser("~/.cache/ztos/com.zerone.tube/thumbs")
THUMB_W, THUMB_H = 128, 72


# ---- words for numbers ------------------------------------------------------------------

def duration(seconds) -> str:
    s = int(seconds or 0)
    if s <= 0:
        return ""
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def count(n, word: str) -> str:
    if n is None:
        return ""
    n = int(n)
    for div, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= div:
            v = n / div
            text = f"{v:.1f}".rstrip("0").rstrip(".") if v < 10 else f"{int(v)}"
            return f"{text}{suffix} {word}"
    return f"{n} {word if n != 1 else word.rstrip('s')}"


def age(video: dict) -> str:
    ts = video.get("published")
    if not ts and video.get("upload_date"):
        try:
            ts = time.mktime(time.strptime(str(video["upload_date"]), "%Y%m%d"))
        except ValueError:
            ts = None
    if not ts:
        return ""
    d = max(0, time.time() - ts)
    for span, word in ((31536000, "year"), (2592000, "month"), (604800, "week"), (86400, "day"),
                       (3600, "hour"), (60, "minute")):
        if d >= span:
            n = int(d // span)
            return f"{n} {word}{'s' if n != 1 else ''} ago"
    return "just now"


def details(video: dict, channel: bool = True) -> str:
    parts = [video.get("channel") if channel else "", count(video.get("views"), "views"), age(video)]
    if video.get("live"):
        parts.append("live")
    return " • ".join(p for p in parts if p)


# ---- pictures off the network, kept on disk ---------------------------------------------------

class Thumbs(QObject):
    """Thumbnails and avatars: memory, then ~/.cache, then the network."""

    def __init__(self):
        super().__init__()
        self.net = QNetworkAccessManager(self)
        self.memory: dict = {}
        self.waiting: dict = {}
        os.makedirs(CACHE, exist_ok=True)

    def _path(self, url: str) -> str:
        return os.path.join(CACHE, hashlib.sha1(url.encode()).hexdigest() + ".jpg")

    def get(self, url: str, on_ready: Callable[[QImage], None]):
        if not url:
            return
        url = small_avatar(url)
        if url in self.memory:
            on_ready(self.memory[url])
            return
        path = self._path(url)
        if os.path.exists(path):
            image = QImage(path)
            if not image.isNull():
                self._keep(url, image)
                on_ready(image)
                return
        if url in self.waiting:
            self.waiting[url].append(on_ready)
            return
        self.waiting[url] = [on_ready]
        reply = self.net.get(QNetworkRequest(QUrl(url)))
        reply.finished.connect(lambda r=reply, u=url: self._done(r, u))

    def _done(self, reply: QNetworkReply, url: str):
        callbacks = self.waiting.pop(url, [])
        data = bytes(reply.readAll())
        reply.deleteLater()
        if reply.error() != QNetworkReply.NoError or not data:
            return
        image = QImage.fromData(data)
        if image.isNull():
            return
        # a row's picture is kept at the phone's 3x of the row; a poster (hq720)
        # at the screen's width
        wide = 1080 if "/hq720" in url else THUMB_W * 3
        if image.width() > wide:
            image = image.scaledToWidth(wide, Qt.SmoothTransformation)
        image.save(self._path(url), "JPG", 85)
        self._keep(url, image)
        for cb in callbacks:
            try:
                cb(image)
            except RuntimeError:
                pass        # its row was deleted meanwhile

    def _keep(self, url: str, image: QImage):
        if len(self.memory) > 400:
            for key in list(self.memory)[:100]:
                del self.memory[key]
        self.memory[url] = image


thumbs = Thumbs()


def poster_of(url: str) -> str:
    """The big picture of a video whose row has the small one."""
    return url.replace("/mqdefault.jpg", "/hq720.jpg") if "i.ytimg.com/vi/" in url else ""


def small_avatar(url: str) -> str:
    """A channel's picture at 240 pixels: yt-dlp gives the original ("=s0"),
    which can be too big for Qt to open at all."""
    if "googleusercontent.com" in url or "ggpht.com" in url:
        return re.sub(r"=s\d+[^/]*$", "=s240-c-k-c0x00ffffff-no-rj", url)
    return url


def draw_cover(p: QPainter, image: Optional[QImage], rect: QRectF, round_: bool = False):
    """A picture filling rect, cut to its shape; a grey box until it arrives."""
    p.save()
    if round_:
        clip = QPainterPath()
        clip.addEllipse(rect)
        p.setClipPath(clip)
    if image is None:
        p.fillRect(rect, QColor(0x26, 0x26, 0x26))
    else:
        src_ratio = image.width() / max(1, image.height())
        dst_ratio = rect.width() / rect.height()
        if src_ratio > dst_ratio:
            w = image.height() * dst_ratio
            src = QRectF((image.width() - w) / 2, 0, w, image.height())
        else:
            h = image.width() / dst_ratio
            src = QRectF(0, (image.height() - h) / 2, image.width(), h)
        p.drawImage(rect, image, src)
    p.restore()


def wrap(fm: QFontMetricsF, text: str, width: float, lines: int) -> list:
    """Up to `lines` lines of text, the last cut with an ellipsis."""
    words, out, line = text.split(), [], ""
    for i, word in enumerate(words):
        trial = f"{line} {word}".strip()
        if fm.horizontalAdvance(trial) <= width or not line:
            line = trial
            continue
        out.append(line)
        line = word
        if len(out) == lines - 1:
            line = " ".join(words[i:])
            break
    if line:
        out.append(line)
    out = out[:lines]
    if out:
        out[-1] = fm.elidedText(out[-1], Qt.ElideRight, width)
    return out


# ---- rows ------------------------------------------------------------------------------

class VideoRow(ztos._Pressable):
    """A video in a list."""

    def __init__(self, video: dict, on_tap: Optional[Callable] = None, on_hold: Optional[Callable] = None,
                 show_channel: bool = True, note: str = "", progress: float = 0.0):
        super().__init__()
        self.video = video
        self.show_channel = show_channel
        self.note = note            # e.g. "downloaded"
        self.progress = progress    # 0..1: how much was watched (a red line under the picture)
        self.image: Optional[QImage] = None
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(THUMB_H + 16)
        if on_tap:
            self.tapped.connect(on_tap)
        if on_hold:
            self.held.connect(on_hold)
        thumbs.get(video.get("thumb") or "", self._image)

    def _image(self, image: QImage):
        self.image = image
        self.redraw()
        self.content_changed()

    def set_note(self, note: str):
        self.note = note
        self.redraw()
        self.content_changed()

    def draw(self, p: QPainter):
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing | QPainter.SmoothPixmapTransform)
        top = 4.0
        pic = QRectF(0, top, THUMB_W, THUMB_H)
        draw_cover(p, self.image, pic)
        badge = "LIVE" if self.video.get("live") else duration(self.video.get("duration"))
        if badge:
            p.setFont(ztos._font(FONT_SEMIBOLD, 10.5))
            fm = QFontMetricsF(p.font())
            w = fm.horizontalAdvance(badge) + 8
            r = QRectF(pic.right() - w - 3, pic.bottom() - 18, w, 15)
            p.fillRect(r, QColor(228, 20, 0) if self.video.get("live") else QColor(0, 0, 0, 200))
            p.setPen(QColor("white"))
            p.drawText(r, Qt.AlignCenter, badge)
        if self.progress > 0.01:
            p.fillRect(QRectF(pic.left(), pic.bottom() - 3, pic.width(), 3), QColor(255, 255, 255, 70))
            p.fillRect(QRectF(pic.left(), pic.bottom() - 3, pic.width() * min(1.0, self.progress), 3), ztos.accent())
        x = THUMB_W + 12
        w = self.width() - x
        p.setPen(QColor("white"))
        p.setFont(ztos._font(FONT_SEMILIGHT, 16))
        fm = QFontMetricsF(p.font())
        y = top - 2
        for line in wrap(fm, self.video.get("title") or "", w, 2):
            p.drawText(QPointF(x, y + fm.ascent()), line)
            y += fm.height() - 1
        p.setFont(ztos._font(FONT_REGULAR, 12))
        fm2 = QFontMetricsF(p.font())
        y += 3
        lines = []
        if self.show_channel and self.video.get("channel"):
            lines.append(self.video["channel"])
        rest = " • ".join(x for x in (count(self.video.get("views"), "views"), age(self.video)) if x)
        if rest and not self.note:      # (a note takes the second grey line's place: the row has room for two)
            lines.append(rest)
        for i, line in enumerate(lines[:2]):
            p.setPen(GREY)
            p.drawText(QPointF(x, y + fm2.ascent()), fm2.elidedText(line, Qt.ElideRight, w))
            y += fm2.height()
        if self.note:
            p.setPen(ztos.accent())
            p.drawText(QPointF(x, y + fm2.ascent()), fm2.elidedText(self.note, Qt.ElideRight, w))


class ChannelRow(ztos._Pressable):
    """A channel: its round picture, its name, a detail."""

    def __init__(self, name: str, detail: str = "", avatar: str = "", on_tap: Optional[Callable] = None,
                 on_hold: Optional[Callable] = None):
        super().__init__()
        self.name, self.detail = name, detail
        self.image: Optional[QImage] = None
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(64)
        if on_tap:
            self.tapped.connect(on_tap)
        if on_hold:
            self.held.connect(on_hold)
        if avatar:
            thumbs.get(avatar, self._image)

    def _image(self, image: QImage):
        self.image = image
        self.redraw()
        self.content_changed()

    def set_detail(self, detail: str):
        self.detail = detail
        self.redraw()
        self.content_changed()

    def draw(self, p: QPainter):
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing | QPainter.SmoothPixmapTransform)
        r = QRectF(0, 8, 48, 48)
        if self.image is not None:
            draw_cover(p, self.image, r, round_=True)
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(ztos.accent())
            p.drawEllipse(r)
            p.setPen(QColor("white"))
            p.setFont(ztos._font(FONT_SEMILIGHT, 22))
            p.drawText(r, Qt.AlignCenter, (self.name or "?")[:1].upper())
        x, w = 62, self.width() - 62
        p.setPen(QColor("white"))
        p.setFont(ztos._font(FONT_SEMILIGHT, 21))
        fm = QFontMetricsF(p.font())
        top = 8 if self.detail else (self.height() - fm.height()) / 2
        p.drawText(QPointF(x, top + fm.ascent()), fm.elidedText(self.name, Qt.ElideRight, w))
        if self.detail:
            p.setPen(GREY)
            p.setFont(ztos._font(FONT_REGULAR, 12))
            fm2 = QFontMetricsF(p.font())
            p.drawText(QPointF(x, 38 + fm2.ascent()), fm2.elidedText(self.detail, Qt.ElideRight, w))


class Word(ztos._Pressable):
    """One of a row of filters: a lowercase word, the chosen one in the accent."""

    def __init__(self, text: str, on_tap: Callable):
        super().__init__()
        self.text = text
        self.chosen = False
        self.setFont(ztos._font(FONT_SEMILIGHT, 19))
        fm = QFontMetricsF(self.font())
        self.setFixedSize(int(fm.horizontalAdvance(text)) + 18, 36)
        self.tapped.connect(on_tap)

    def choose(self, on: bool):
        if self.chosen != on:
            self.chosen = on
            self.redraw()
            self.content_changed()

    def draw(self, p: QPainter):
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        p.setFont(self.font())
        p.setPen(ztos.accent() if self.chosen else GREY)
        p.drawText(QRectF(0, 0, self.width(), self.height()), Qt.AlignLeft | Qt.AlignVCenter, self.text)


class Words(QWidget):
    """A row of Word filters; on_choose gets the chosen key."""

    def __init__(self, choices: list, chosen: str, on_choose: Callable[[str], None]):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 6)
        row.setSpacing(0)
        self.words = {}
        for key, text in choices:
            w = Word(text, lambda k=key: self._pick(k, on_choose))
            row.addWidget(w)
            self.words[key] = w
        row.addStretch(1)
        self.set(chosen)

    def set(self, key: str):
        for k, w in self.words.items():
            w.choose(k == key)

    def _pick(self, key: str, on_choose):
        ztos.haptic("tick")
        self.set(key)
        on_choose(key)


def note(text: str):
    """Grey text across the list: "no videos yet", an error."""
    label = ztos._label(text, FONT_REGULAR, 14, GREY)
    label.setWordWrap(True)
    label.setContentsMargins(0, 8, 0, 8)
    return label
