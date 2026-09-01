# -*- coding: utf-8 -*-
"""Local control panel for publishing the lecture library to Discord forums.

Run it, open the browser page it prints, drive everything from there.
Nothing here touches a user account: all Discord writes go through a bot
token you create yourself in the Developer Portal.
"""
import json, os, sys, threading, time, webbrowser, queue, io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# Emit UTF-8 regardless of the console codepage, so Hebrew titles and emoji
# print instead of raising.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import library, optimizer
try:
    import keywatch
except Exception:
    keywatch = None
try:
    import autosend
except Exception:
    autosend = None
from discord_api import Discord, DiscordError, TIER_NAME

FROZEN = getattr(sys, "frozen", False)


def _mangled(s):
    """True if a path came back with undecodable bytes (lone surrogates).

    Launching from Git Bash hands Python a cwd in the ANSI codepage, so a
    Hebrew folder name arrives as '\\udc90...' and every later join is junk.
    """
    return any(0xDC80 <= ord(c) <= 0xDCFF for c in s)


def _true_cwd():
    """The real Unicode cwd, straight from Win32, bypassing bad decoding."""
    if os.name == "nt":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(32768)
            if ctypes.windll.kernel32.GetCurrentDirectoryW(32768, buf):
                return buf.value
        except Exception:
            pass
    return os.getcwd()


_here = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
if _mangled(_here):
    _here = _true_cwd()

# state lives next to the exe/script; ui.html rides inside the bundle
HERE = _here
ASSETS = getattr(sys, "_MEIPASS", HERE)
STATE_PATH = os.path.join(HERE, "state.json")
PORT = 7333

# --------------------------------------------------------------- server text
# Text this tool writes INTO Discord, as opposed to the text it shows in its
# own panel. These are defaults: category_name, forum_topic and post_body can
# each be overridden per-install in state.json, which is gitignored, so the
# repo can stay English while your server stays in its own language.
# GUILD_NAME_HINT is a substring used to auto-pick the server; empty means
# take the first one the bot is in.
CATEGORY_NAME = "📚 The Library"
GUILD_NAME_HINT = ""
FORUM_TOPIC = "Course recordings"
POST_BODY = "**%s**\n%s\n\n*The recording will be uploaded here.*"


DEFAULT_STATE = {
    "root": os.path.dirname(HERE),
    "optimized_dir": os.path.join(os.path.dirname(HERE), "_optimized"),
    "token": "",
    "guild_id": "",
    "category_name": CATEGORY_NAME,
    "forum_topic": FORUM_TOPIC,
    "post_body": POST_BODY,
    "done": {},        # key -> {thread_id, forum_id, when}
    "optimized": {},   # key -> {path, size}
    "manual_done": {},
    "forum_map": {},   # course name -> channel id (manual override)
    # None means "the whole library"; a list means only those keys. Kept as a
    # list of keys rather than a per-file flag so a rescan cannot resurrect a
    # selection the user has since cleared.
    "selected": None,
    "auto_create_forums": True,
}

_lock = threading.RLock()
state = dict(DEFAULT_STATE)
rows = []            # library inventory
server_info = {}     # guild/cap/forums/threads
plan_cache = {}

job = {
    "running": False, "cancel": False, "phase": "", "current": "",
    "done_count": 0, "total": 0, "item_pct": 0.0, "log": [],
    "started": 0, "errors": 0,
}


# ---------------------------------------------------------------- state io
def load_state():
    global state
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                state.update(json.load(f))
        except Exception:
            pass
    for k, v in DEFAULT_STATE.items():
        state.setdefault(k, v)


def save_state():
    with _lock:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_PATH)


def log(msg):
    with _lock:
        job["log"].append("%s  %s" % (time.strftime("%H:%M:%S"), msg))
        del job["log"][:-400]
    # The Windows console defaults to a legacy codepage, so printing a title
    # containing non-ASCII characters raises UnicodeEncodeError and takes the
    # whole request down with it. Logging must never be able to break the panel.
    try:
        print(msg)
    except Exception:
        try:
            print(msg.encode("ascii", "replace").decode("ascii"))
        except Exception:
            pass


# ------------------------------------------------------------- discovery
def _on_wait(secs, reason):
    if secs >= 5:
        job["phase_note"] = "waiting %ds (%s)" % (int(secs), reason)
        log("⏳ waiting %ds — %s (Discord allows 50 posts / 5 min, server-wide)"
            % (int(secs), reason))


def dc():
    if not state.get("token"):
        raise DiscordError("no bot token set")
    return Discord(state["token"], on_wait=_on_wait)


def refresh_server():
    """Read the guild: boost tier, forums, and every existing post."""
    global server_info
    d = dc()
    gid = state.get("guild_id")
    if not gid:
        gs = d.guilds()
        pick = next((g for g in gs if GUILD_NAME_HINT in g.get("name", "")), None) or (gs[0] if gs else None)
        if not pick:
            raise DiscordError("the bot is not in any server yet — invite it first")
        gid = pick["id"]
        state["guild_id"] = gid
        save_state()

    g = d.guild(gid)
    chans = d.channels(gid)
    cats = {c["id"]: c["name"] for c in chans if c["type"] == 4}
    forums = [c for c in chans if c["type"] in (15, 16)]

    active = d.active_threads(gid)
    by_parent = {}
    for t in active:
        by_parent.setdefault(t.get("parent_id"), []).append(t)

    finfo = []
    for c in forums:
        threads = list(by_parent.get(c["id"], []))
        try:
            for t in d.archived_threads(c["id"]):
                if not any(x["id"] == t["id"] for x in threads):
                    threads.append(t)
        except DiscordError as e:
            log("could not list archived posts in #%s: %s" % (c["name"], e))
        finfo.append({
            "id": c["id"], "name": c["name"],
            "category": cats.get(c.get("parent_id"), ""),
            "tags": [{"id": t["id"], "name": t["name"]}
                     for t in (c.get("available_tags") or [])],
            "threads": [{"id": t["id"], "name": t["name"],
                         "messages": t.get("message_count", 0)}
                        for t in threads],
        })

    # Does each post actually contain a video, or is it an empty shell?
    # A post we created is empty until a reply carries the file, so a reply
    # count settles it. A post made by hand holds the file in its opening
    # message, which has to be read once (then cached).
    ours = set(state.get("threads", {}).values())
    cache = state.setdefault("starter_files", {})
    checked = 0
    for f in finfo:
        for t in f["threads"]:
            if t["id"] in ours:
                t["has_file"] = t["messages"] > 0
                continue
            if t["id"] in cache:
                t["has_file"] = cache[t["id"]]
                continue
            if t["messages"] > 0:
                t["has_file"] = True
                continue
            if checked >= 400:
                t["has_file"] = True      # don't guess "empty" without looking
                continue
            try:
                m = d.starter_message(t["id"])
                t["has_file"] = bool(m.get("attachments"))
            except DiscordError:
                t["has_file"] = True
            cache[t["id"]] = t["has_file"]
            checked += 1
    if checked:
        save_state()

    # Drop remembered posts that no longer exist. Deleting posts in Discord
    # must not leave this thinking they are still there, or step 3 skips
    # recreating them and step 4 links to nothing.
    live = {t["id"] for f in finfo for t in f["threads"]}
    remembered = state.get("threads", {})
    gone = [k for k, tid in remembered.items() if tid not in live]
    for k in gone:
        remembered.pop(k, None)
        state.get("done", {}).pop(k, None)
        state.get("manual_done", {}).pop(k, None)
    if gone:
        log("%d posts were deleted on the server — queued again" % len(gone))
        save_state()

    server_info = {
        "guild_id": gid, "guild_name": g.get("name"),
        "tier": g.get("premium_tier", 0),
        "tier_name": TIER_NAME.get(g.get("premium_tier", 0), "?"),
        "boosts": g.get("premium_subscription_count", 0),
        "cap_mb": d.upload_cap_mb(g),
        "members": g.get("approximate_member_count"),
        "categories": cats, "forums": finfo,
    }
    return server_info


def norm_course(name):
    """Words only, lowercased.

    Discord rewrites channel names: spaces become hyphens and emoji/symbols
    are kept, so a course named 'Information Security' is stored as
    'information-security'. Every separator has to collapse to a space
    or the two forms never line up.
    """
    out = []
    for ch in name:
        out.append(ch.lower() if ch.isalnum() else " ")
    return " ".join("".join(out).split())


def squash(name):
    return norm_course(name).replace(" ", "")


def _score(course, chan):
    """0..100 similarity between a folder name and a channel name."""
    a, b = squash(course), squash(chan)
    if not a or not b:
        return 0
    if a == b:
        return 100
    if a in b or b in a:
        shorter, longer = (a, b) if len(a) < len(b) else (b, a)
        return 80 + int(15 * len(shorter) / len(longer))
    ta, tb = set(norm_course(course).split()), set(norm_course(chan).split())
    if not ta or not tb:
        return 0
    inter = ta & tb
    if not inter:
        return 0
    return int(70 * len(inter) / len(ta | tb))


def find_forum(course):
    """The forum channel for a course: manual override first, else best match."""
    forums = server_info.get("forums", [])
    pinned = state.get("forum_map", {}).get(course)
    if pinned:
        for f in forums:
            if f["id"] == pinned:
                return f
    best, best_score = None, 0
    for f in forums:
        s = _score(course, f["name"])
        if s > best_score:
            best, best_score = f, s
    return best if best_score >= 45 else None


def forum_matches():
    """What each course resolved to — so a wrong guess is visible, not silent."""
    forums = server_info.get("forums", [])
    out = []
    for course in sorted({r["course"] for r in rows}):
        f = find_forum(course)
        out.append({
            "course": course,
            "forum_id": f["id"] if f else "",
            "forum_name": f["name"] if f else "",
            "pinned": course in state.get("forum_map", {}),
            "score": _score(course, f["name"]) if f else 0,
            "posts": len(f["threads"]) if f else 0,
        })
    return out


NITRO_MB = 500
HAND_CEILING = 480   # aim under Nitro's 500 so mux overhead can't overshoot
WORKERS = 3          # concurrent ffmpeg jobs; measured best on this 28-thread CPU


def upload_ceiling():
    """The largest file our actual upload path can carry.

    A bot is capped by the server's boost tier (20 MB unboosted), which is
    useless for hour-long lectures. When that is lower than what the owner
    can send by hand with Nitro, we target the hand-upload ceiling instead
    and let the bot do structure only.
    """
    cap = server_info.get("cap_mb", 0)
    return cap if cap >= HAND_CEILING else HAND_CEILING


def selection():
    """The chosen file keys, or None when the whole library is in scope."""
    sel = state.get("selected")
    return None if sel is None else set(sel)


def in_scope(key):
    sel = selection()
    return sel is None or key in sel


def build_plan():
    """Decide, per file, what still needs doing."""
    global plan_cache
    cap = server_info.get("cap_mb", 0)
    out = {}
    for r in rows:
        key = r["key"]
        rec = {"key": key, "course": r["course"], "file": r["file"],
               "title": r["title"], "type": r["type"], "size": r["size"],
               "num": r["num"], "part": r["part"]}

        opt = state["optimized"].get(key)
        eff_path, eff_size = r["path"], r["size"]
        # Only fall back to the compressed copy when the original genuinely
        # does not fit. Otherwise a stray compressed file would replace a
        # perfectly good original with a lower-quality one.
        if (opt and os.path.exists(opt["path"])
                and r["size"] > HAND_CEILING * 1048576):
            eff_path, eff_size = opt["path"], opt["size"]
        rec["eff_path"], rec["eff_size"] = eff_path, eff_size

        existing = rec_existing(r)
        rec["thread_id"] = (state.get("threads", {}).get(key)
                            or (existing or {}).get("id"))

        if key in state["done"] or key in state["manual_done"]:
            rec["status"] = "done"
        elif existing and (existing.get("has_file") is not False):
            rec["status"] = "already"
        elif eff_size > HAND_CEILING * 1048576:
            rec["status"] = "needs_optimize"
        elif cap and eff_size <= cap * 1048576:
            rec["status"] = "bot_ready"
        else:
            rec["status"] = "manual"

        rec["over_nitro"] = eff_size > NITRO_MB * 1048576
        out[key] = rec
    plan_cache = out
    return out


def rec_existing(r):
    """Was this already posted by hand? Match on (type, number, part)."""
    f = find_forum(r["course"])
    if not f:
        return None
    for t in f["threads"]:
        ttype, _rank, num, part, _topic = library.detect(t["name"])
        if ttype == r["type"] and num == r["num"] and part == r["part"]:
            return t
    return None


def reset_progress(clear_threads):
    """Forget local progress. Used after deleting posts in Discord."""
    state["done"] = {}
    state["manual_done"] = {}
    state["starter_files"] = {}
    if clear_threads:
        state["threads"] = {}
    save_state()


# ------------------------------------------------------------------ jobs
def worker(mode):
    try:
        job.update(running=True, cancel=False, done_count=0, errors=0,
                   started=time.time(), item_pct=0.0)
        if mode in ("structure", "all"):
            do_structure()
        if mode in ("optimize", "all"):
            do_optimize()
        if mode in ("threads", "all"):
            do_threads()
        if mode == "upload":
            do_upload()
        log("✔ finished" if not job["cancel"] else "■ stopped")
    except Exception as e:
        log("✖ %s" % e)
        job["errors"] += 1
    finally:
        job.update(running=False, phase="", current="")


def do_structure():
    """Create one forum channel per course, with tags."""
    job["phase"] = "building structure"
    if not state.get("auto_create_forums", True):
        log("creating forums is switched off - skipping step 1")
        return
    d = dc()
    gid = server_info["guild_id"]

    cat_id = None
    want = state.get("category_name") or CATEGORY_NAME
    for cid, nm in server_info.get("categories", {}).items():
        if norm_course(nm) == norm_course(want):
            cat_id = cid
    if not cat_id:
        log("creating category %s" % want)
        cat_id = d.create_category(gid, want)["id"]

    # Only courses that actually have a chosen file need a forum.
    courses = sorted({r["course"] for r in rows if in_scope(r["key"])})
    if selection() is not None:
        log("selection active: %d course(s)" % len(courses))
    job["total"] = len(courses)
    for i, course in enumerate(courses, 1):
        if job["cancel"]:
            return
        job["current"] = course
        job["done_count"] = i
        if find_forum(course):
            log("forum for %s already exists" % course)
            continue
        tags = sorted({library.TYPE_LABEL.get(r["type"], library.TYPE_LABEL["other"])
                       for r in rows if r["course"] == course})
        log("creating forum #%s (tags: %s)" % (course, ", ".join(tags)))
        try:
            d.create_forum(gid, course[:100], parent_id=cat_id,
                           topic=state.get("forum_topic") or FORUM_TOPIC, tags=tags)
        except DiscordError as e:
            log("  ✖ %s" % e)
            job["errors"] += 1
        time.sleep(1.0)
    refresh_server()


def do_optimize():
    """Shrink everything that exceeds the relevant ceiling."""
    ceiling = upload_ceiling()
    job["phase"] = "compressing (target %d MB)" % ceiling
    todo = [p for p in build_plan().values()
            if p["status"] == "needs_optimize" and in_scope(p["key"])]
    todo.sort(key=lambda p: -p["size"])   # slowest first
    job["total"] = len(todo)
    job["done_count"] = 0
    if not todo:
        log("nothing needs compressing")
        return
    if not optimizer.available():
        log("✖ ffmpeg not found — install with: winget install Gyan.FFmpeg")
        job["errors"] += 1
        return

    # A single 720p encode leaves most of a 28-thread CPU idle, and the
    # 1080p60 sources are the slow ones, so start those first and let the
    # quick 1280-wide files fill the other slots.
    from concurrent.futures import ThreadPoolExecutor

    workers = min(WORKERS, len(todo))
    log("compressing %d files to fit %d MB, %d at a time"
        % (len(todo), ceiling, workers))
    inflight = {}
    counter = {"n": 0}

    def one(p):
        dst = os.path.join(state["optimized_dir"], p["course"], p["file"])
        if (os.path.exists(dst) and 0 < os.path.getsize(dst) <= ceiling * 1048576
                and optimizer.complete(p["eff_path"], dst)):
            with _lock:
                state["optimized"][p["key"]] = {"path": dst,
                                                "size": os.path.getsize(dst)}
                save_state()
            log("skip (already done) %s" % p["file"][:58])
        elif not job["cancel"]:
            log("▶ %s  (%.0f MB)" % (p["file"][:58], p["size"] / 1048576))
            with _lock:
                inflight[p["key"]] = 0.0
            ok, msg = optimizer.shrink(
                p["eff_path"], dst, ceiling,
                on_progress=lambda f, k=p["key"]: inflight.__setitem__(k, f),
                cancel=lambda: job["cancel"],
                log=lambda m, f=p["file"]: log("   [%s] %s" % (f[:28], m)))
            with _lock:
                inflight.pop(p["key"], None)
                if ok:
                    state["optimized"][p["key"]] = {
                        "path": dst, "size": os.path.getsize(dst)}
                    save_state()
                    log("   ✔ %s → %s" % (p["file"][:46], msg))
                else:
                    log("   ✖ %s — %s" % (p["file"][:46], msg))
                    job["errors"] += 1
        with _lock:
            counter["n"] += 1
            job["done_count"] = counter["n"]
            job["current"] = ", ".join(
                k.split("/")[-1][:26] for k in list(inflight)[:3]) or "…"
            job["item_pct"] = (sum(inflight.values()) / len(inflight)
                               if inflight else 0.0)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, todo))
    job["item_pct"] = 0.0


def do_threads():
    """Create every forum post up front, titled, tagged and in order.

    The bot cannot carry the video itself at a 20 MB cap, but it can build
    the whole skeleton. That turns each remaining upload into: click the
    post, Ctrl+V, Enter -- no typing, no naming, no sorting.
    """
    job["phase"] = "creating threads"
    d = dc()
    state.setdefault("threads", {})
    plan = build_plan()
    todo = [p for p in plan.values()
            if p["status"] in ("manual", "needs_optimize")
            and not p.get("thread_id") and in_scope(p["key"])]
    todo.sort(key=lambda p: (p["course"], p["title"]))
    # Thread creation is limited to 50 per 5 minutes PER CHANNEL, so going
    # course-by-course drains one bucket and then stalls. Round-robin across
    # the forums keeps every bucket working at once.
    by_course = {}
    for p in todo:
        by_course.setdefault(p["course"], []).append(p)
    order, lists = [], list(by_course.values())
    while any(lists):
        for lst in lists:
            if lst:
                order.append(lst.pop(0))
    todo = order

    job["total"] = len(todo)
    job["done_count"] = 0
    if not todo:
        log("every post already exists")
        return

    log("creating %d posts across %d forums — Discord allows 50 per 5 min "
        "server-wide, so expect ~%d min of waiting"
        % (len(todo), len(by_course), -(-len(todo) // 50) * 5))
    for i, p in enumerate(todo, 1):
        if job["cancel"]:
            return
        job["current"] = p["title"]
        job["done_count"] = i - 1
        f = find_forum(p["course"])
        if not f:
            # With forum creation off this is a deliberate skip, not a fault:
            # the user asked to publish into forums that already exist.
            if state.get("auto_create_forums", True):
                log("✖ no forum for %s — run step 1 first" % p["course"])
                job["errors"] += 1
            else:
                log("skipped %s — no forum for %s (creating forums is off)"
                    % (p["title"][:34], p["course"]))
            continue
        tag_ids = [t["id"] for t in f["tags"]
                   if t["name"] == library.TYPE_LABEL.get(p["type"])]
        body = (state.get("post_body") or POST_BODY) % (p["title"], p["course"])
        try:
            th = d.create_post_textonly(f["id"], p["title"], body, tag_ids)
            state["threads"][p["key"]] = th.get("id")
            save_state()
        except DiscordError as e:
            log("   ✖ %s — %s" % (p["title"][:40], e))
            job["errors"] += 1
        job["done_count"] = i
        time.sleep(0.3)   # bucket tracking handles the real pacing now
    refresh_server()


def do_upload():
    """Bot-upload everything that fits the server cap."""
    cap = server_info.get("cap_mb", 0)
    job["phase"] = "uploading (bot limit %d MB)" % cap
    d = dc()
    plan = build_plan()
    todo = [p for p in plan.values()
            if p["status"] == "bot_ready" and in_scope(p["key"])]
    todo.sort(key=lambda p: (p["course"], p["title"]))
    job["total"] = len(todo)
    job["done_count"] = 0
    if not todo:
        log("nothing is under the bot's %d MB cap — see the manual list" % cap)
        return

    for i, p in enumerate(todo, 1):
        if job["cancel"]:
            return
        job["current"] = p["file"]
        job["done_count"] = i - 1
        job["item_pct"] = 0.0
        f = find_forum(p["course"])
        if not f:
            log("✖ no forum for %s — run 'build structure' first" % p["course"])
            job["errors"] += 1
            continue
        tag_ids = [t["id"] for t in f["tags"]
                   if t["name"] == library.TYPE_LABEL.get(p["type"])]
        body = "**%s**\n%s" % (p["title"], p["course"])
        log("▲ %s → #%s (%.0f MB)"
            % (p["title"][:50], f["name"], p["eff_size"] / 1048576))
        try:
            th = d.create_post(
                f["id"], p["title"], p["eff_path"], content=body,
                tag_ids=tag_ids,
                progress=lambda s, t: job.__setitem__("item_pct", s / t if t else 0))
            state["done"][p["key"]] = {"thread_id": th.get("id"),
                                       "forum_id": f["id"], "when": time.time()}
            save_state()
            log("   ✔ posted")
        except DiscordError as e:
            log("   ✖ %s" % e)
            job["errors"] += 1
        job["done_count"] = i
        time.sleep(1.2)          # stay well inside the rate limit
    job["item_pct"] = 0.0


def start_job(mode):
    if job["running"]:
        return False
    threading.Thread(target=worker, args=(mode,), daemon=True).start()
    return True


# -------------------------------------------------------- hand-upload aid
def clip_file(path):
    """Put a real file on the Windows clipboard, so Ctrl+V in Discord
    attaches it. Turns each upload into two keystrokes."""
    import subprocess
    ps = ("Set-Clipboard -LiteralPath %s"
          % ("'" + path.replace("'", "''") + "'"))
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-Command", ps],
                       capture_output=True, text=True,
                       creationflags=0x08000000 if os.name == "nt" else 0)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "clipboard copy failed").strip()[:200])


# Advance the queue the instant Enter is pressed in Discord, rather than
# waiting for the upload to finish (a 400 MB file would stall ~50s).
enter_state = {"key": None, "fired": 0}


def _enter_fired():
    k = enter_state.get("key")
    if not k:
        return
    p = plan_cache.get(k) or {}
    state["manual_done"][k] = time.time()
    save_state()
    enter_state["fired"] += 1
    enter_state["key"] = None
    log("⏎ sent — %s" % (p.get("title", k)[:52]))


ENTER = (keywatch.EnterWatcher(_enter_fired) if keywatch else None)


def open_in_app(gid, tid):
    """Jump the Discord DESKTOP app to a post.

    The https:// link goes through the browser and then hands off, which is
    slow. The discord:// scheme is registered by the desktop client and
    switches an already-running Discord straight to the channel.
    """
    if not (gid and tid):
        return False
    try:
        os.startfile("discord://-/channels/%s/%s" % (gid, tid))
        return True
    except Exception as e:
        log("could not open the Discord app: %s" % e)
        return False


def manual_queue():
    """Pending hand-uploads, in the order they should be done."""
    plan = build_plan()
    gid = server_info.get("guild_id", "")
    items = [p for p in plan.values()
             if p["status"] == "manual" and in_scope(p["key"])]
    items.sort(key=lambda p: (p["course"], p["title"]))
    out = []
    for p in items:
        tid = p.get("thread_id")
        out.append({
            "key": p["key"], "course": p["course"], "title": p["title"],
            "size": p["eff_size"], "thread_id": tid,
            "url": ("https://discord.com/channels/%s/%s" % (gid, tid)) if tid else "",
            "optimized": p["eff_path"] != os.path.join(
                state["root"], p["course"], p["file"]),
        })
    return out

# ------------------------------------------------------------- auto upload
# Pressing Ctrl+V / Enter by hand means a person has to sit there for every one
# of ~200 files. The panel can type those two keys itself, but only after it
# has PROVEN the right post is open - the cost of getting that wrong is a
# lecture posted under someone else's title.
#
# Files are kept in flight rather than sent one at a time: Discord uploads in
# the background, so the next paste happens immediately and only the size of
# the in-flight window (default 3) throttles it. An upload counts as confirmed
# when the post's message_count turns non-zero, which is the only honest proof
# the file actually arrived.
auto = {
    "running": False, "cancel": False, "sent": 0, "confirmed": 0,
    "current": "", "error": "", "note": "", "target": 0,
    "window": 3, "paste_wait": 2.5,
    "inflight": {},          # key -> {tid, title, t}
    "failed": [],            # keys that never showed up in Discord
    "skip": [],              # set aside after repeated send failures
    "tries": {},             # key -> send attempts this run
}
AUTO_TIMEOUT = 1800          # 30 min for one upload to land, then call it lost
_esc = None


def _auto_abort():
    with _lock:
        if auto["running"]:
            auto["cancel"] = True
            auto["note"] = "\u05d1\u05d5\u05d8\u05dc (Esc)"
    log("Esc - stopping")


def _thread_filled(tid):
    try:
        th = dc().get("/channels/%s" % tid)
        return (th.get("message_count", 0) or 0) > 0
    except Exception:
        return None          # unknown: leave it in flight and re-check later


def _auto_reap():
    """Drop finished uploads out of the in-flight window."""
    for key, info in list(auto["inflight"].items()):
        filled = _thread_filled(info["tid"])
        if filled:
            auto["inflight"].pop(key, None)
            auto["confirmed"] += 1
            log("\u2714 uploaded: %s" % info["title"][:52])
        elif filled is False and time.time() > info["deadline"]:
            auto["inflight"].pop(key, None)
            auto["failed"].append(key)
            state["manual_done"].pop(key, None)
            save_state()
            log("\u2716 never arrived: %s - back in the queue"
                % info["title"][:44])


def _auto_send_one(item, paste_wait):
    """Clipboard -> right post -> Ctrl+V -> Enter. False means stop the batch."""
    key, tid = item["key"], item["thread_id"]
    p = build_plan().get(key)
    if not (p and tid):
        auto["error"] = "no post for %s" % item["title"][:40]
        return False
    auto["current"] = item["title"]
    try:
        clip_file(p["eff_path"])
    except Exception as e:
        auto["error"] = "clipboard failed: %s" % e
        return False

    hwnd = autosend.discord_window()
    if not hwnd:
        auto["error"] = "Discord window not found - open the app"
        return False
    # Raise Discord BEFORE navigating: a protocol launch from a background
    # process often navigates the client without ever bringing it forward,
    # and the keystrokes must land in a window we can see is the right one.
    autosend.focus(hwnd)
    open_in_app(server_info.get("guild_id", ""), tid)
    ok, seen = autosend.wait_for_post(item["title"], 15, hwnd)
    if not ok:
        auto["error"] = "the right post never opened (%s)" % seen[:70]
        return False

    autosend.paste()
    # Discord stages the attachment and builds a preview; Enter too early sends
    # nothing and strands the file in the composer.
    time.sleep(max(0.8, paste_wait))

    # The file is staged in Discord's composer at this point, so a stolen
    # foreground is recoverable - and it happens routinely, because the
    # discord:// stub exits a few seconds after launch and Windows hands focus
    # back to whatever the user last typed in.
    ok, seen = autosend.regain(item["title"], hwnd, 8)
    if not ok:
        auto["error"] = "lost the window before sending (%s)" % seen[:60]
        return False

    # Holding the WINDOW is not the same as holding the MESSAGE BOX: after a
    # foreground bounce Discord often focuses the post body, where Enter does
    # nothing at all and the file just sits there staged - that is exactly how
    # one tutorial thread ended up with two unsent copies. Click the box,
    # then send.
    autosend.click_composer(hwnd)
    time.sleep(0.15)
    autosend.enter()
    with _lock:
        state["manual_done"][key] = time.time()
        save_state()
        # Give an upload roughly six seconds per megabyte before calling it
        # lost - a flat timeout either fails 400 MB files or hides small ones.
        mb = max(1, int(item.get("size", 0) / 1048576))
        auto["inflight"][key] = {"tid": tid, "title": item["title"],
                                 "t": time.time(),
                                 "deadline": time.time() + max(300, mb * 6)}
        auto["sent"] += 1
    log("\u2191 sent (%d): %s" % (auto["sent"], item["title"][:48]))
    return True


def _auto_loop(count, window, paste_wait):
    global _esc
    last_reap = 0.0
    misses = 0
    try:
        while not auto["cancel"]:
            if time.time() - last_reap > 4:
                _auto_reap()
                last_reap = time.time()
            if auto["sent"] >= auto["target"]:
                break
            if len(auto["inflight"]) >= window:
                time.sleep(1.5)
                continue
            q = manual_queue()
            item = next((i for i in q if i["key"] not in auto["inflight"]
                         and i["key"] not in auto["skip"]), None)
            if not item:
                auto["note"] = "queue empty"
                break
            if _auto_send_one(item, paste_wait):
                misses = 0
                time.sleep(0.4)
                continue
            # A miss is usually transient - the mouse was grabbed, Discord was
            # slow. Retry the file once, then set it aside and carry on; only
            # give up when the failures look systematic.
            key = item["key"]
            auto["tries"][key] = auto["tries"].get(key, 0) + 1
            misses += 1
            log("! %s - %s (try %d)"
                % (item["title"][:36], auto["error"], auto["tries"][key]))
            state["manual_done"].pop(key, None)
            if auto["tries"][key] >= 2:
                auto["skip"].append(key)
                log("  set aside: %s" % item["title"][:46])
            if misses >= 4:
                auto["error"] = "4 failures in a row - stopping (%s)" % auto["error"]
                break
            auto["error"] = ""
            time.sleep(2)

        # let whatever is still uploading finish, so the count is honest
        auto["current"] = ""
        deadline = time.time() + AUTO_TIMEOUT
        while auto["inflight"] and time.time() < deadline and not auto["cancel"]:
            auto["note"] = "waiting for %d uploads" % len(auto["inflight"])
            _auto_reap()
            time.sleep(5)
    except Exception as e:
        auto["error"] = "%s: %s" % (type(e).__name__, e)
        log("auto mode failed: %s" % auto["error"])
    finally:
        if _esc:
            _esc.disarm()
        auto["running"] = False
        auto["current"] = ""
        log("auto mode finished - sent %d, confirmed %d, failed %d"
            % (auto["sent"], auto["confirmed"], len(auto["failed"])))


def auto_start(count, window, paste_wait):
    global _esc
    if autosend is None:
        return {"error": "autosend module not loaded"}
    if auto["running"]:
        return {"error": "already running"}
    if not autosend.discord_window():
        return {"error": "Discord app is not open"}
    pending = len(manual_queue())
    if not pending:
        return {"error": "nothing in the queue"}
    if ENTER:
        ENTER.disarm()          # the panel types the keys now, not the user
    auto.update({"running": True, "cancel": False, "sent": 0, "confirmed": 0,
                 "current": "", "error": "", "note": "", "failed": [],
                 "inflight": {}, "skip": [], "tries": {},
                 "window": max(1, int(window)),
                 "paste_wait": float(paste_wait),
                 "target": min(int(count) or pending, pending)})
    _esc = autosend.EscapeWatcher(_auto_abort)
    _esc.arm()
    log("auto mode: %d files, up to %d in flight (Esc stops)"
        % (auto["target"], auto["window"]))
    threading.Thread(target=_auto_loop,
                     args=(auto["target"], auto["window"], auto["paste_wait"]),
                     daemon=True).start()
    return auto_status()


def auto_status():
    return {"running": auto["running"], "sent": auto["sent"],
            "confirmed": auto["confirmed"], "inflight": len(auto["inflight"]),
            "current": auto["current"], "error": auto["error"],
            "note": auto["note"], "target": auto["target"],
            "failed": len(auto["failed"]), "skipped": len(auto["skip"]),
            "esc": bool(_esc and _esc.available)}



# ---------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            with open(os.path.join(ASSETS, "ui.html"), encoding="utf-8") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/state":
            return self._send(200, snapshot())
        if path == "/api/progress":
            with _lock:
                return self._send(200, {
                    "running": job["running"], "phase": job["phase"],
                    "current": job["current"], "done": job["done_count"],
                    "total": job["total"], "item_pct": job["item_pct"],
                    "errors": job["errors"], "log": job["log"][-120:]})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            body = {}
        try:
            return self._send(200, self.route(path, body))
        except DiscordError as e:
            return self._send(200, {"error": str(e)})
        except Exception as e:
            return self._send(200, {"error": "%s: %s" % (type(e).__name__, e)})

    def route(self, path, body):
        global rows
        if path == "/api/settings":
            for k in ("root", "optimized_dir", "category_name", "guild_id",
                      "auto_create_forums"):
                if k in body:
                    state[k] = body[k]
            save_state()
            return snapshot()

        if path == "/api/scan":
            rows = library.scan(state["root"])
            log("scanned %d videos in %s" % (len(rows), state["root"]))
            return snapshot()

        if path == "/api/connect":
            state["token"] = (body.get("token") or "").strip()
            state["guild_id"] = body.get("guild_id") or ""
            save_state()
            d = dc()
            me = d.me()
            gs = d.guilds()
            log("connected as bot %s" % me.get("username"))
            if not state["guild_id"] and len(gs) == 1:
                state["guild_id"] = gs[0]["id"]
                save_state()
            if state["guild_id"]:
                refresh_server()
            return dict(snapshot(), bot=me.get("username"),
                        guilds=[{"id": g["id"], "name": g["name"]} for g in gs])

        if path == "/api/refresh":
            refresh_server()
            return snapshot()

        if path == "/api/run":
            mode = body.get("mode", "all")
            if not start_job(mode):
                return {"error": "a job is already running"}
            return {"ok": True}

        if path == "/api/stop":
            job["cancel"] = True
            log("stop requested")
            return {"ok": True}

        if path == "/api/reset":
            reset_progress(bool(body.get("threads")))
            if state.get("token") and state.get("guild_id"):
                refresh_server()
            log("local progress reset")
            return snapshot()

        if path == "/api/map":
            course, cid = body.get("course"), body.get("channel_id")
            state.setdefault("forum_map", {})
            if course and cid:
                state["forum_map"][course] = cid
            elif course:
                state["forum_map"].pop(course, None)
            save_state()
            return snapshot()

        if path == "/api/manual/prepare":
            k = body.get("key")
            p = build_plan().get(k)
            if not p:
                return {"error": "unknown file"}
            clip_file(p["eff_path"])
            gid = server_info.get("guild_id", "")
            tid = p.get("thread_id")
            opened = False
            if tid and body.get("open", True):
                opened = open_in_app(gid, tid)
            watching = False
            if ENTER and body.get("watch_enter", True) and tid:
                enter_state["key"] = k
                ENTER.arm()
                watching = ENTER.available
            log("📋 %s" % p["title"][:55])
            return {"ok": True, "title": p["title"],
                    "file": os.path.basename(p["eff_path"]),
                    "size": p["eff_size"], "opened": opened,
                    "watching_enter": watching,
                    "url": ("https://discord.com/channels/%s/%s" % (gid, tid))
                           if tid else ""}

        if path == "/api/manual/signal":
            return {"fired": enter_state["fired"],
                    "armed": bool(ENTER and ENTER.armed),
                    "available": bool(ENTER and ENTER.available),
                    "error": (ENTER.error if ENTER else "keywatch unavailable")}

        if path == "/api/manual/disarm":
            if ENTER:
                ENTER.disarm()
            enter_state["key"] = None
            return {"ok": True}

        if path == "/api/select":
            keys = body.get("keys", None)
            state["selected"] = None if keys is None else list(keys)
            save_state()
            return snapshot()

        if path == "/api/auto/start":
            return auto_start(body.get("count", 0), body.get("window", 3),
                              body.get("paste_wait", 2.5))

        if path == "/api/auto/stop":
            auto["cancel"] = True
            auto["note"] = "stopped"
            return auto_status()

        if path == "/api/auto/status":
            return auto_status()

        if path == "/api/manual/check":
            k = body.get("key")
            p = build_plan().get(k)
            tid = (p or {}).get("thread_id")
            if not tid:
                return {"filled": False}
            try:
                th = dc().get("/channels/%s" % tid)
                filled = (th.get("message_count", 0) or 0) > 0
            except DiscordError as e:
                return {"filled": False, "error": str(e)}
            if filled:
                state["manual_done"][k] = time.time()
                save_state()
                log("✔ %s" % (p["title"][:55]))
            return {"filled": filled}

        if path == "/api/manual_done":
            k = body.get("key")
            if k:
                state["manual_done"][k] = time.time()
                save_state()
            return snapshot()

        if path == "/api/manual_undo":
            state["manual_done"].pop(body.get("key", ""), None)
            save_state()
            return snapshot()

        return {"error": "unknown endpoint " + path}


def snapshot():
    plan = build_plan() if rows and server_info else {}
    counts = {}
    for p in plan.values():
        counts[p["status"]] = counts.get(p["status"], 0) + 1
    courses = {}
    for r in rows:
        c = courses.setdefault(r["course"], {"name": r["course"], "files": [],
                                             "bytes": 0})
        item = dict(plan.get(r["key"], {}), key=r["key"], file=r["file"],
                    title=r["title"], size=r["size"], type=r["type"])
        item.setdefault("status", "unknown")
        item["selected"] = in_scope(r["key"])
        c["files"].append(item)
        c["bytes"] += r["size"]
    return {
        "settings": {k: state[k] for k in
                     ("root", "optimized_dir", "category_name", "guild_id")},
        "auto_create_forums": bool(state.get("auto_create_forums", True)),
        "selected": state.get("selected"),
        "selected_count": (len(rows) if selection() is None
                           else len([r for r in rows if in_scope(r["key"])])),
        "has_token": bool(state.get("token")),
        "server": server_info,
        "counts": counts,
        "total_files": len(rows),
        "total_bytes": sum(r["size"] for r in rows),
        "courses": [courses[k] for k in sorted(courses)],
        "ffmpeg": optimizer.available(),
        "running": job["running"],
        "queue": manual_queue() if (rows and server_info) else [],
        "matches": forum_matches() if (rows and server_info) else [],
        "all_forums": [{"id": f["id"], "name": f["name"],
                        "category": f.get("category", ""),
                        "posts": len(f["threads"])}
                       for f in server_info.get("forums", [])],
        "ceiling": upload_ceiling() if server_info else 0,
    }


def main():
    load_state()
    global rows
    rows = library.scan(state["root"])
    print("=" * 60)
    print(" Discord Library Panel")
    print(" library : %s  (%d videos)" % (state["root"], len(rows)))
    print(" ffmpeg  : %s" % ("found" if optimizer.available() else "MISSING"))
    print(" open    : http://127.0.0.1:%d" % PORT)
    print("=" * 60)
    if state.get("token") and state.get("guild_id"):
        try:
            refresh_server()
            print(" server  : %s (cap %d MB)"
                  % (server_info["guild_name"], server_info["cap_mb"]))
        except Exception as e:
            print(" server  : not reachable (%s)" % e)
    # A second copy silently binding the same port means the OLD code keeps
    # answering after an update - it cost real debugging time, so fail loudly.
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print("")
        print(" !! Port %d is already in use - another panel is still running." % PORT)
        print("    Close the other window (or end python.exe in Task Manager)")
        print("    and start this again, otherwise the old version answers.")
        input("    Press Enter to exit...")
        return
    threading.Timer(0.8, lambda: webbrowser.open("http://127.0.0.1:%d" % PORT)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
