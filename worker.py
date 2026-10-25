"""Tube's worker: everything that talks to YouTube, in a process of its own.

The app starts this with the phone's python3 and talks to it in JSON lines:
a request {"id": n, "op": "search", ...} on stdin, and on stdout {"id": n,
"data": ...} or {"id": n, "error": "..."}, with {"id": n, "progress": x}
for downloads, and {"event": ...} for what nobody asked about.

It keeps a copy of yt-dlp of its own, as NewPipe keeps its extractor up to
date: YouTube changes often, and the distribution's package (2025.12 on the
phone) already gets 403 Forbidden for every stream. The copy is the release's
zipapp, unpacked, so Python can cache its bytecode (imported from the zip it
takes 9 s on the A53s, unpacked about 2 s). It is checked once a day.

Resolving a video's streams runs YouTube's player JavaScript (in Node) and
takes 13 s on the A53s and 6 s on the A73s, so the worker keeps to the big
cores; the app starts resolving when a video's page opens, not when play is
tapped.
"""
import json
import os
import shutil
import sys
import threading
import time
import traceback
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor

APP_ID = "com.zerone.tube"
DATA = os.path.expanduser(f"~/.local/share/ztos/data/{APP_ID}")
YTDLP_DIR = os.path.join(DATA, "yt-dlp")
YTDLP_VERSION = os.path.join(DATA, "yt-dlp.version")
YTDLP_CHECKED = os.path.join(DATA, "yt-dlp.checked")
RELEASE_API = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
RELEASE_ZIP = "https://github.com/yt-dlp/yt-dlp/releases/download/{tag}/yt-dlp"
CHECK_EVERY = 24 * 3600
VIDEO_DIR = os.path.expanduser("~/Videos/Tube")
AUDIO_DIR = os.path.expanduser("~/Music/Tube")
# The quality ladder the player offers, named as YouTube names it. Up to
# 1080p: the screen is 1080 wide, and the M20's video codec decodes H.264 and
# VP9 1080p60 several times faster than real time (more is wasted, and 4K does
# not fit its buffers).
HEIGHTS = (144, 240, 360, 480, 720, 1080)


def quality(width: int, height: int):
    """A stream's place on the ladder ("720"), or None above 1080p. Named by
    its width as YouTube does, so a film letterboxed to 1920x800 is 1080p, and
    by its shorter side when it stands upright (a 1080x1920 short)."""
    if not width or not height:
        return str(height) if height in HEIGHTS else None
    side = width * 9 / 16 if width >= height else width
    if side > 1080 * 1.2:
        return None
    return str(min(HEIGHTS, key=lambda h: abs(h - side)))

_out = threading.Lock()
_busy = 0
_busy_lock = threading.Lock()
_restart_when_idle = False
_cancelled: set = set()
yt_dlp = None


def send(msg: dict):
    line = json.dumps(msg, separators=(",", ":"))
    with _out:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def log(*args):
    print("tube-worker:", *args, file=sys.stderr, flush=True)


# ---- the big cores ---------------------------------------------------------------

def big_cores() -> set:
    """The CPUs with the most capacity (the A73s on the M20), or every CPU."""
    caps = {}
    for cpu in os.sched_getaffinity(0):
        try:
            with open(f"/sys/devices/system/cpu/cpu{cpu}/cpu_capacity") as f:
                caps[cpu] = int(f.read())
        except (OSError, ValueError):
            pass
    if not caps:
        # no capacities: the cores whose part is the rarer one are the big ones
        parts = {}
        try:
            cpu = None
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("processor"):
                        cpu = int(line.split(":")[1])
                    elif line.startswith("CPU part") and cpu is not None:
                        parts.setdefault(line.split(":")[1].strip(), set()).add(cpu)
        except (OSError, ValueError):
            pass
        if len(parts) == 2:
            return min(parts.values(), key=len)
        return set(os.sched_getaffinity(0))
    top = max(caps.values())
    return {c for c, v in caps.items() if v == top}


# ---- yt-dlp: our own copy ----------------------------------------------------------

def installed_version() -> str:
    try:
        with open(YTDLP_VERSION) as f:
            return f.read().strip()
    except OSError:
        return ""


def fetch(url: str, timeout: float = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ztOS-Tube"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def install_ytdlp(force: bool = False) -> bool:
    """Bring the copy up to the latest release. True if a new one went in."""
    os.makedirs(DATA, exist_ok=True)
    tag = json.loads(fetch(RELEASE_API))["tag_name"]
    with open(YTDLP_CHECKED, "w") as f:
        f.write(str(int(time.time())))
    if tag == installed_version() and os.path.isdir(YTDLP_DIR) and not force:
        return False
    send({"event": "ytdlp", "state": "updating", "version": tag})
    blob = fetch(RELEASE_ZIP.format(tag=tag), timeout=120)
    tmp = YTDLP_DIR + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    zip_path = tmp + ".zip"
    with open(zip_path, "wb") as f:
        f.write(blob)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(tmp)
    os.unlink(zip_path)
    old = YTDLP_DIR + ".old"
    shutil.rmtree(old, ignore_errors=True)
    if os.path.isdir(YTDLP_DIR):
        os.rename(YTDLP_DIR, old)
    os.rename(tmp, YTDLP_DIR)
    shutil.rmtree(old, ignore_errors=True)
    with open(YTDLP_VERSION, "w") as f:
        f.write(tag)
    log("yt-dlp", tag, "installed")
    return True


def load_ytdlp():
    global yt_dlp
    if os.path.isdir(os.path.join(YTDLP_DIR, "yt_dlp")):
        sys.path.insert(0, YTDLP_DIR)
    elif not os.environ.get("TUBE_SYSTEM_YTDLP"):
        # first run: nothing of ours yet, and the distribution's is too old
        try:
            install_ytdlp()
            sys.path.insert(0, YTDLP_DIR)
        except Exception as e:
            log("could not get yt-dlp, trying the system's:", e)
    import yt_dlp as module
    yt_dlp = module
    send({"event": "ytdlp", "state": "ready", "version": module.version.__version__})


def check_for_update(force: bool = False):
    global _restart_when_idle
    try:
        with open(YTDLP_CHECKED) as f:
            last = int(f.read())
    except (OSError, ValueError):
        last = 0
    if not force and time.time() - last < CHECK_EVERY:
        return
    try:
        if install_ytdlp():
            # the new one takes over at the next start of this process
            _restart_when_idle = True
            idle_check()
    except Exception as e:
        log("update check failed:", e)


def idle_check():
    if _restart_when_idle and _busy == 0:
        log("restarting for the new yt-dlp")
        sys.stdout.flush()
        os.execv(sys.executable, [sys.executable] + sys.argv)


# ---- what YouTube says, made plain ----------------------------------------------------

def base_opts(hls: bool = False, **more) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        # 160 machine-translated caption languages cost 5 s of URL building;
        # HLS is only wanted for live streams (it has nothing else for them)
        "extractor_args": {"youtube": {"skip": ["translated_subs"] + ([] if hls else ["hls"])}},
    }
    if shutil.which("node"):
        opts["js_runtimes"] = {"node": {}}
    opts.update(more)
    return opts


def thumb_of(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


def best_thumb(thumbs) -> str:
    """The picture closest to 300 pixels wide, of a list of yt-dlp's."""
    best, score = "", None
    for t in thumbs or []:
        url = t.get("url") or ""
        if not url:
            continue
        w = t.get("width") or 0
        s = abs(w - 300) if w else 1000
        if score is None or s < score:
            best, score = url, s
    if best.startswith("//"):
        best = "https:" + best
    return best


def entry(e: dict) -> dict:
    """A video in a list."""
    vid = e.get("id") or ""
    return {
        "id": vid,
        "title": e.get("title") or "",
        "channel": e.get("channel") or e.get("uploader") or "",
        "channel_id": e.get("channel_id") or e.get("uploader_id") or "",
        "duration": e.get("duration") or 0,
        "views": e.get("view_count"),
        "published": e.get("timestamp") or e.get("release_timestamp"),
        "upload_date": e.get("upload_date"),
        "live": e.get("live_status") == "is_live",
        "thumb": thumb_of(vid),
    }


def listing(url: str, count: int) -> dict:
    opts = base_opts(extract_flat="in_playlist", playlistend=count, noplaylist=False)
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=False)
    items = [entry(e) for e in info.get("entries") or [] if e and e.get("id") and
             (e.get("ie_key") in (None, "Youtube") or len(e.get("id", "")) == 11)]
    return {
        "title": info.get("title") or "",
        "channel": info.get("channel") or info.get("uploader") or "",
        "channel_id": info.get("channel_id") or "",
        "avatar": best_thumb([t for t in info.get("thumbnails") or [] if "avatar" in (t.get("id") or "")]
                             or info.get("thumbnails")),
        "subscribers": info.get("channel_follower_count"),
        "description": info.get("description") or "",
        "items": items,
    }


def op_search(query: str, count: int = 20):
    data = listing(f"ytsearch{count}:{query}", count)
    return data["items"]


def op_playlist(url: str, count: int = 50):
    return listing(url, count)


def op_channel(channel_id: str, count: int = 30):
    if channel_id.startswith(("http://", "https://")):
        url = channel_id.rstrip("/")
    elif channel_id.startswith("@"):
        url = f"https://www.youtube.com/{channel_id}"
    else:
        url = f"https://www.youtube.com/channel/{channel_id}"
    if not url.endswith("/videos"):
        url += "/videos"
    data = listing(url, count)
    for item in data["items"]:
        item["channel"] = item["channel"] or data["channel"]
        item["channel_id"] = item["channel_id"] or data["channel_id"]
    return data


def op_video(video_id: str, live: bool = False):
    """A video's details, and its streams: every height of H.264 we play, and
    the best AAC audio. The URLs hold for about six hours. A live stream's
    are HLS (which is all it has: the list skips HLS otherwise)."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    info = None
    if not live:
        # any format string that cannot fail: the streams are picked below
        try:
            with yt_dlp.YoutubeDL(base_opts(format="bv*+ba/b")) as y:
                info = y.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as e:
            if "No video formats" not in str(e):
                raise
            # (a stream that went live after the list was read)
    if info is None:
        with yt_dlp.YoutubeDL(base_opts(hls=True, format="bv*+ba/b")) as y:
            info = y.extract_info(url, download=False)
    # live: HLS, its picture and sound apart as well (the sound's codec unnamed)
    protocols = ("https", "http") + (("m3u8_native", "m3u8") if info.get("live_status") == "is_live" else ())
    # picture: H.264 or VP9 (both decoded by the phone's video codec where it
    # has one; never AV1), up to 1080p, by quality and by frame rate (up to 30
    # and up to 60 apart). Sound: AAC, or Opus where there is no AAC.
    video, video60, audio, opus, muxed = {}, {}, None, None, None
    for f in info.get("formats") or []:
        url = f.get("url")
        if not url or f.get("protocol") not in protocols:
            continue
        vcodec, acodec = f.get("vcodec") or "none", f.get("acodec") or "none"
        rate = f.get("tbr") or f.get("abr") or (int(f["format_id"]) if str(f.get("format_id")).isdigit() else 0)
        if vcodec != "none" and acodec != "none":
            if vcodec.startswith(("avc1", "vp9", "vp09")) and (muxed is None or rate > muxed[1]):
                muxed = (url, rate)
        elif vcodec.startswith(("avc1", "vp9", "vp09")):
            q = quality(f.get("width") or 0, f.get("height") or 0)
            fps = f.get("fps") or 30
            if q is None or fps > 60:
                continue
            into = video if fps <= 30 else video60
            # H.264 before VP9 at the same quality, then the higher bitrate
            score = (vcodec.startswith("avc1"), rate)
            if q not in into or score > into[q][1]:
                into[q] = (url, score)
        elif vcodec == "none" and (acodec.startswith("mp4a") or (acodec == "none" and f.get("protocol", "").startswith("m3u8"))):
            if audio is None or rate > audio[1]:
                audio = (url, rate)
        elif vcodec == "none" and acodec == "opus":
            if opus is None or rate > opus[1]:
                opus = (url, rate)
    audio = audio or opus
    if (video or video60) and audio is not None:
        muxed = None
    elif muxed is None and info.get("live_status") == "is_live":
        muxed = (info.get("url"), 0)    # a live stream's single HLS
    out = entry(info)
    out.update({
        "description": info.get("description") or "",
        "likes": info.get("like_count"),
        "subscribers": info.get("channel_follower_count"),
        "streams": {h: u for h, (u, _) in video.items()},
        "streams60": {h: u for h, (u, _) in video60.items()},
        "audio": audio[0] if audio else None,
        "muxed": muxed[0] if muxed else None,
        "headers": info.get("http_headers") or {},
        "chapters": [{"title": c.get("title"), "start": c.get("start_time")} for c in info.get("chapters") or []],
        "resolved": int(time.time()),
    })
    return out


# ---- the subscription feed: the channels' RSS, or their pages if that fails ----------

ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"
MEDIA = "{http://search.yahoo.com/mrss/}"


def rss(channel_id: str) -> list:
    xml = fetch(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}", timeout=15)
    root = ET.fromstring(xml)
    name = root.findtext(f"{ATOM}title") or ""
    items = []
    for e in root.findall(f"{ATOM}entry"):
        vid = e.findtext(f"{YT}videoId") or ""
        published = e.findtext(f"{ATOM}published") or ""
        try:
            ts = int(time.mktime(time.strptime(published[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone)
        except ValueError:
            ts = None
        views = None
        stats = e.find(f"{MEDIA}group/{MEDIA}community/{MEDIA}statistics")
        if stats is not None:
            try:
                views = int(stats.get("views"))
            except (TypeError, ValueError):
                pass
        items.append({"id": vid, "title": e.findtext(f"{ATOM}title") or "", "channel": name,
                      "channel_id": channel_id, "duration": 0, "views": views, "published": ts,
                      "upload_date": None, "live": False, "thumb": thumb_of(vid)})
    return items


def one_feed(channel_id: str) -> list:
    try:
        return rss(channel_id)
    except Exception:
        # YouTube's RSS answers 404 now and then: read the channel page instead
        return op_channel(channel_id, 12)["items"]


def op_feed(channels: list):
    items, failed = [], []
    with ThreadPoolExecutor(6) as pool:
        for cid, result in zip(channels, pool.map(lambda c: _safe(one_feed, c), channels)):
            if isinstance(result, Exception):
                failed.append(cid)
            else:
                items.extend(result)
    items.sort(key=lambda v: v.get("published") or 0, reverse=True)
    return {"items": items, "failed": failed}


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        return e


# ---- downloads ---------------------------------------------------------------------

def op_download(req_id: int, video_id: str, kind: str = "video", height: int = 720, fps: int = 30):
    if kind == "audio":
        folder = AUDIO_DIR
        fmt = "ba[acodec^=mp4a]/ba"
    else:
        folder = VIDEO_DIR
        # as op_video plays: H.264 (or VP9) up to the quality by the shorter
        # side (a letterboxed film is narrower than it is wide), never AV1
        size = f"[width<={height * 16 // 9 + 8}]" if height else ""
        pic = f"[vcodec~='^(avc1|vp0?9)'][fps<={fps}]"
        fmt = (f"bv*{size}{pic}+ba[acodec^=mp4a]/bv*{size}{pic}+ba/"
               f"b{size}[vcodec~='^(avc1|vp0?9)']/b{size}")
    os.makedirs(folder, exist_ok=True)
    result = {}
    last = [0.0]

    def hook(d):
        if req_id in _cancelled:
            raise yt_dlp.utils.DownloadCancelled("cancelled")
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total and time.time() - last[0] > 0.5:
                last[0] = time.time()
                info = d.get("info_dict") or {}
                # a video downloads its picture, then its sound: halves of the bar
                part = 0.5 if kind == "video" and (info.get("vcodec") or "none") == "none" else 0.0
                share = 0.5 if kind == "video" else 1.0
                send({"id": req_id, "progress": round(part + share * d.get("downloaded_bytes", 0) / total, 3)})
        elif d.get("status") == "finished":
            result["file"] = d.get("filename")

    def done_hook(d):
        if d.get("status") == "finished" and d.get("info_dict", {}).get("filepath"):
            result["file"] = d["info_dict"]["filepath"]

    opts = base_opts(format=fmt, outtmpl=os.path.join(folder, "%(title).120B [%(id)s].%(ext)s"),
                     merge_output_format="mp4", progress_hooks=[hook], postprocessor_hooks=[done_hook],
                     writethumbnail=False, overwrites=False, continuedl=True)
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
    path = result.get("file") or (info.get("requested_downloads") or [{}])[0].get("filepath")
    out = entry(info)
    out.update({"path": path, "kind": kind,
                "size": os.path.getsize(path) if path and os.path.exists(path) else 0})
    return out


# ---- the loop -------------------------------------------------------------------------

def handle(req: dict):
    rid, op = req.get("id"), req.get("op")
    args = {k: v for k, v in req.items() if k not in ("id", "op")}
    try:
        if op == "search":
            data = op_search(**args)
        elif op == "playlist":
            data = op_playlist(**args)
        elif op == "channel":
            data = op_channel(**args)
        elif op == "video":
            data = op_video(**args)
        elif op == "feed":
            data = op_feed(**args)
        elif op == "download":
            data = op_download(rid, **args)
        elif op == "update":
            # a new copy restarts this process once the reply is out (finally)
            check_for_update(force=True)
            data = {"version": installed_version(), "restarting": _restart_when_idle}
        else:
            raise ValueError(f"unknown op {op}")
        send({"id": rid, "data": data})
    except Exception as e:
        message = str(e)
        if "cancelled" in message.lower() and rid in _cancelled:
            message = "cancelled"
        else:
            log(op, "failed:", traceback.format_exc(limit=3))
        # yt-dlp's messages start with "ERROR: [youtube] id:", which says nothing
        message = message.replace("ERROR: ", "").split(": ", 1)[-1] if "[youtube" in message else message
        send({"id": rid, "error": message[:300]})
    finally:
        _cancelled.discard(rid)
        _busy_dec()
        idle_check()


def _busy_dec():
    global _busy
    with _busy_lock:
        _busy -= 1


def main():
    global _busy
    big = big_cores()
    try:
        os.sched_setaffinity(0, big)    # Node, which this starts, inherits it
    except OSError:
        pass
    load_ytdlp()
    threading.Thread(target=check_for_update, daemon=True).start()
    pool = ThreadPoolExecutor(4)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if req.get("op") == "cancel":
            _cancelled.add(req.get("target"))
            continue
        with _busy_lock:
            _busy += 1
        pool.submit(handle, req)
    pool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
