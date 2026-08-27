# -*- coding: utf-8 -*-
"""Scanning and parsing of the lecture library."""
import os, re, json

HEB_PART = {"א": 1, "ב": 2, "ג": 3, "ד": 4, "ה": 5}
PART_HE = {v: k for k, v in HEB_PART.items()}

TYPES = [
    ("הרצאה", "lecture",  1, "הרצאה"),
    ("תרגול", "tutorial", 2, "תרגול"),
    ("סדנא",  "workshop", 3, "סדנה"),
    ("סדנה",  "workshop", 3, "סדנה"),
    ("תגבור", "boost",    4, "תגבור"),
    ("מרתון", "marathon", 5, "מרתון"),
    ("חזרה",  "review",   6, "חזרה"),
]

TYPE_HE = {
    "lecture": "הרצאה", "tutorial": "תרגול", "workshop": "סדנה",
    "boost": "תגבור", "marathon": "מרתון", "review": "חזרה", "other": "אחר",
}
TYPE_EMOJI = {
    "lecture": "📘", "tutorial": "✏️", "workshop": "🛠️",
    "boost": "🚀", "marathon": "🏃", "review": "🔁", "other": "📄",
}


def detect(name):
    """-> (type, rank, number, part, topic)"""
    base = os.path.splitext(name)[0]

    ttype, rank, num, kw_end = "other", 9, None, 0
    for kw, canon, r, _he in TYPES:
        m = re.search(re.escape(kw) + r"\s*(\d+)", base)
        if m:
            ttype, rank, num, kw_end = canon, r, int(m.group(1)), m.end()
            break
        m = re.search(re.escape(kw), base)
        if m:
            ttype, rank, kw_end = canon, r, m.end()
            m2 = re.search(r"(\d+)\s*" + re.escape(kw), base)
            if m2:
                num = int(m2.group(1))
            break

    topic = None
    sep = base.find(" - ", kw_end)
    if sep != -1:
        topic = base[sep + 3:].strip()

    part = None
    mp = re.search(r"חלק\s+([א-ת])(?![א-ת])", base)
    if mp:
        part = HEB_PART.get(mp.group(1))
    else:
        mp = re.search(r"חלק\s+(\d+)", base)
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
    """Clean, sortable Hebrew forum-post title."""
    bits = [TYPE_EMOJI.get(row["type"], "📄"), TYPE_HE.get(row["type"], "")]
    if row["num"]:
        bits.append(str(row["num"]))
    if row["part"]:
        # keep the library's own convention: חלק א / ב / ג, not חלק 1
        bits.append("חלק " + PART_HE.get(row["part"], str(row["part"])))
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
