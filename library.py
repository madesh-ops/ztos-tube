"""What Tube keeps on the phone: subscriptions, watch history (with where you
stopped), downloads, settings and the last subscription feed. All in
~/.local/share/ztos/data/com.zerone.tube/, as JSON (ztos.store)."""
import csv
import json
import os
import time
from typing import Optional

import ztos

HISTORY_MAX = 300
_store = None


def _db() -> "ztos.Store":
    global _store
    if _store is None:
        _store = ztos.store("library")
        _store.setdefault("subscriptions", [])
        _store.setdefault("history", [])
        _store.setdefault("downloads", [])
        _store.setdefault("settings", {})
    return _store


def save():
    _db().save()


# ---- settings ------------------------------------------------------------------------

DEFAULTS = {
    "quality": "720",           # the highest the player picks on its own
    "background": True,         # keep playing (sound only) when the app is left
    "region": "global",         # "what's hot": which charts
    "resume": True,             # start where you stopped
}


def setting(key: str):
    return _db()["settings"].get(key, DEFAULTS.get(key))


def set_setting(key: str, value):
    _db()["settings"][key] = value
    save()


# ---- subscriptions -----------------------------------------------------------------------

def subscriptions() -> list:
    return list(_db()["subscriptions"])


def is_subscribed(channel_id: str) -> bool:
    return any(s["id"] == channel_id for s in _db()["subscriptions"])


def subscribe(channel_id: str, name: str, avatar: str = ""):
    if not channel_id or is_subscribed(channel_id):
        return
    _db()["subscriptions"].append({"id": channel_id, "name": name, "avatar": avatar, "since": int(time.time())})
    _db()["subscriptions"].sort(key=lambda s: s["name"].lower())
    save()


def unsubscribe(channel_id: str):
    _db()["subscriptions"] = [s for s in _db()["subscriptions"] if s["id"] != channel_id]
    save()


def import_newpipe(path: str) -> int:
    """NewPipe's export (Settings > Content > Export subscriptions), or
    YouTube's own (Google Takeout's subscriptions.csv): returns how many
    channels were new."""
    with open(path, encoding="utf-8-sig") as f:
        if path.lower().endswith(".csv"):
            # Channel Id,Channel Url,Channel Title
            try:
                rows = [r for r in csv.reader(f) if len(r) >= 3 and r[0].startswith("UC")]
            except csv.Error as e:
                raise ValueError(e) from e
            channels = [(r[0], r[2]) for r in rows]
        else:
            channels = []
            for s in json.load(f).get("subscriptions", []):
                url = s.get("url") or ""
                if s.get("service_id", 0) == 0 and "/channel/" in url:
                    channels.append((url.rstrip("/").split("/channel/")[1].split("/")[0], s.get("name")))
    added = 0
    for cid, name in channels:
        if not is_subscribed(cid):
            _db()["subscriptions"].append({"id": cid, "name": name or cid, "avatar": "",
                                           "since": int(time.time())})
            added += 1
    _db()["subscriptions"].sort(key=lambda s: s["name"].lower())
    save()
    return added


def newpipe_exports() -> list:
    """NewPipe's or YouTube's subscription exports lying in the usual folders."""
    found = []
    for folder in ("~/Downloads", "~/Documents", "~"):
        folder = os.path.expanduser(folder)
        try:
            for name in sorted(os.listdir(folder)):
                if name.lower().endswith((".json", ".csv")) and "subscriptions" in name.lower():
                    found.append(os.path.join(folder, name))
        except OSError:
            pass
    return found


# ---- the feed ------------------------------------------------------------------------

def feed_cache() -> dict:
    return ztos.store("feed")


def set_feed(items: list, channels: list):
    s = ztos.store("feed")
    s["at"] = int(time.time())
    s["items"] = items[:200]
    s["channels"] = channels
    s.save()


# ---- history ----------------------------------------------------------------------------

def _lean(video: dict) -> dict:
    keep = ("id", "title", "channel", "channel_id", "duration", "views", "published", "upload_date", "thumb")
    return {k: video.get(k) for k in keep}


def history() -> list:
    return list(_db()["history"])


def watched(video: dict, position: float = 0.0, duration: float = 0.0):
    h = [v for v in _db()["history"] if v["id"] != video.get("id")]
    item = _lean(video)
    item["position"] = position
    if duration:
        item["duration"] = duration
    item["at"] = int(time.time())
    h.insert(0, item)
    _db()["history"] = h[:HISTORY_MAX]
    save()


def resume_point(video_id: str) -> float:
    """Where to start: the last position, unless it was near either end."""
    for v in _db()["history"]:
        if v["id"] == video_id:
            pos, dur = v.get("position") or 0, v.get("duration") or 0
            if pos > 10 and (not dur or pos < dur - 15):
                return pos
            return 0.0
    return 0.0


def forget(video_id: Optional[str] = None):
    _db()["history"] = [] if video_id is None else [v for v in _db()["history"] if v["id"] != video_id]
    save()


# ---- downloads ------------------------------------------------------------------------

def downloads() -> list:
    """Downloaded files that are still there."""
    items = [d for d in _db()["downloads"] if d.get("path") and os.path.exists(d["path"])]
    if len(items) != len(_db()["downloads"]):
        _db()["downloads"] = items
        save()
    return list(items)


def downloaded(video_id: str, kind: Optional[str] = None) -> Optional[dict]:
    for d in downloads():
        if d["id"] == video_id and (kind is None or d.get("kind") == kind):
            return d
    return None


def add_download(item: dict):
    d = [x for x in _db()["downloads"] if not (x["id"] == item["id"] and x.get("kind") == item.get("kind"))]
    d.insert(0, item)
    _db()["downloads"] = d
    save()


def remove_download(item: dict):
    try:
        os.unlink(item["path"])
    except OSError:
        pass
    _db()["downloads"] = [x for x in _db()["downloads"] if x.get("path") != item.get("path")]
    save()
