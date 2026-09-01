# -*- coding: utf-8 -*-
"""Scanning and parsing of the lecture library.

The vocabulary this parser matches lives in `patterns.json` beside this file,
not in the code. That is deliberate: the words in a filename are a property of
the library you point the tool at, not of the tool. Editing them is the normal
way to adapt it, and doing so in data rather than in code means you never have
to read this module to do it.

Drop a `patterns.local.json` next to `patterns.json` to override it without
touching a tracked file; it is gitignored.
"""

import json
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_patterns():
    """Read patterns.local.json if present, else patterns.json."""
    for name in ("patterns.local.json", "patterns.json"):
        path = os.path.join(_HERE, name)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
    raise FileNotFoundError(
        "no patterns.json next to library.py - the parser has no vocabulary")


_P = _load_patterns()

PART_PREFIX = _P["part_prefix"]
PART_LETTERS = _P["part_letters"]                       # letter -> number
PART_LABEL = {v: k for k, v in PART_LETTERS.items()}    # number -> letter

# (match, key, rank) in declaration order - first hit wins, so if two entries
# share a key (two spellings of the same word) put both in the list.
TYPES = [(t["match"], t["key"], t["rank"]) for t in _P["types"]]

TYPE_LABEL = {t["key"]: t["label"] for t in _P["types"]}
TYPE_EMOJI = {t["key"]: t["emoji"] for t in _P["types"]}
TYPE_LABEL["other"] = _P["other"]["label"]
TYPE_EMOJI["other"] = _P["other"]["emoji"]

# "part b" - one of the configured letters, not glued to another
# word character. The numeric form ("part 2") is the fallback.
_letters = "|".join(sorted((re.escape(k) for k in PART_LETTERS), key=len, reverse=True))
_PART_LETTER_RE = re.compile(re.escape(PART_PREFIX) + r"\s+(" + _letters + r")(?!\w)", re.I)
_PART_NUMBER_RE = re.compile(re.escape(PART_PREFIX) + r"\s+(\d+)", re.I)


def detect(name):
    """-> (type, rank, number, part, topic)"""
    base = os.path.splitext(name)[0]

    ttype, rank, num, kw_end = "other", 9, None, 0
    for kw, canon, r in TYPES:
        m = re.search(re.escape(kw) + r"\s*(\d+)", base, re.I)
        if m:
            ttype, rank, num, kw_end = canon, r, int(m.group(1)), m.end()
            break
        m = re.search(re.escape(kw), base, re.I)
        if m:
            ttype, rank, kw_end = canon, r, m.end()
            m2 = re.search(r"(\d+)\s*" + re.escape(kw), base, re.I)
            if m2:
                num = int(m2.group(1))
            break

    topic = None
    sep = base.find(" - ", kw_end)
    if sep != -1:
        topic = base[sep + 3:].strip()

    part = None
    mp = _PART_LETTER_RE.search(base)
    if mp:
        part = PART_LETTERS.get(mp.group(1))
    else:
        mp = _PART_NUMBER_RE.search(base)
        if mp:
            part = int(mp.group(1))

    if ttype == "other":
        mw = re.match(r"^web\s+(\d+)\s+(\d+)\s*$", base.strip(), re.I)
        if mw:
            ttype, rank, num, part = "lecture", 1, int(mw.group(1)), int(mw.group(2))

    if num is None and part is not None:
        num = 0
    return ttype, rank, num, part, topic


def thread_title(row):
    """Clean, sortable forum-post title."""
    bits = [TYPE_EMOJI.get(row["type"], TYPE_EMOJI["other"]),
            TYPE_LABEL.get(row["type"], "")]
    if row["num"]:
        bits.append(str(row["num"]))
    if row["part"]:
        # keep the library's own convention - lettered parts, not "part 1"
        bits.append(PART_PREFIX + " " + PART_LABEL.get(row["part"], str(row["part"])))
    title = " ".join(b for b in bits if b)
    if row.get("topic"):
        title += " — " + row["topic"]
    return title[:100]


def sort_key(r):
    return (r["rank"],
            r["num"] if r["num"] is not None else 9999,
            r["part"] if r["part"] is not None else 0,
            r["file"])


def scan(root):
    rows = []
    if not os.path.isdir(root):
        return rows
    for course in sorted(os.listdir(root)):
        cdir = os.path.join(root, course)
        if not os.path.isdir(cdir) or course.startswith(("_", ".")):
            continue
        for fn in sorted(os.listdir(cdir)):
            p = os.path.join(cdir, fn)
            if not os.path.isfile(p):
                continue
            if os.path.splitext(fn)[1].lower() != ".mp4":
                continue
            ttype, rank, num, part, topic = detect(fn)
            row = dict(course=course, file=fn, path=p,
                       size=os.path.getsize(p), type=ttype, rank=rank,
                       num=num, part=part, topic=topic)
            row["title"] = thread_title(row)
            row["key"] = course + "/" + fn
            rows.append(row)
    rows.sort(key=lambda r: (r["course"], sort_key(r)))
    return rows
