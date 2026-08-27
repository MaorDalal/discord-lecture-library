# -*- coding: utf-8 -*-
"""ffmpeg wrapper: shrink a lecture recording under a hard size ceiling.

Strategy, in order of preference (cheapest first):
  1. Audio is 128 kbps stereo across this whole library. Speech only needs
     64 kbps mono, and on a 3-hour recording that alone saves ~90 MB. If the
     video stream already fits the budget, we COPY it -- no quality loss and
     roughly a minute per file.
  2. Otherwise re-encode: cap width at 1280 and framerate at 15 (these are
     slide/screen captures, not motion), x264 CRF 30 with a maxrate ceiling
     derived from the budget.
  3. If the result still overshoots, redo it two-pass at an exact bitrate.
"""
import json, os, re, subprocess, sys, threading, time

WINGET = (r"C:\Users\i7\AppData\Local\Microsoft\WinGet\Packages"
          r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
          r"\ffmpeg-9.0.1-full_build\bin")

AUDIO_KBPS = 64
SAFETY = 0.94
# Measured on this library: veryfast is ~1.5x quicker than medium and
# produced a slightly SMALLER file, because the maxrate cap governs size
# here, not the preset. Slide content hides the quality difference.
PRESET = "veryfast"
NOWIN = 0x08000000 if os.name == "nt" else 0     # CREATE_NO_WINDOW


def _find(exe):
    cand = os.path.join(WINGET, exe)
    if os.path.exists(cand):
        return cand
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, exe)
        if os.path.exists(p):
            return p
    base = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    if os.path.isdir(base):
        for dirpath, _dn, files in os.walk(base):
            if exe in files:
                return os.path.join(dirpath, exe)
    return None


FFMPEG = _find("ffmpeg.exe")
FFPROBE = _find("ffprobe.exe")


def available():
    return bool(FFMPEG and FFPROBE)


def probe(path):
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", creationflags=NOWIN).stdout
    d = json.loads(out)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    dur = float(d["format"].get("duration") or 0)
    total = int(d["format"].get("bit_rate") or 0) / 1000
    vb = int((v or {}).get("bit_rate") or 0) / 1000
    ab = int((a or {}).get("bit_rate") or 0) / 1000
    if not vb:
        vb = max(total - (ab or 128), 0)
    fps = 30.0
    try:
        n, den = v["r_frame_rate"].split("/")
        fps = float(n) / float(den) if float(den) else 30.0
    except Exception:
        pass
    return {"duration": dur, "vbitrate": vb, "abitrate": ab,
            "width": int((v or {}).get("width") or 0),
            "height": int((v or {}).get("height") or 0), "fps": fps}


def _run(cmd, dur, on_progress, cancel):
    """Run ffmpeg with -progress on stdout; report 0..1."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, encoding="utf-8", errors="replace",
                         creationflags=NOWIN)
    try:
        for line in p.stdout:
            if cancel and cancel():
                p.kill()
                return -1
            m = re.match(r"out_time_ms=(\d+)", line.strip())
            if m and dur > 0 and on_progress:
                on_progress(min(int(m.group(1)) / 1e6 / dur, 1.0))
    finally:
        p.wait()
    return p.returncode


def complete(src, dst, tol=0.99):
    """Is `dst` a FULL encode of `src`, or a stump from an interrupted run?

    Size alone cannot tell them apart: a run killed halfway leaves a file
    that is comfortably under the ceiling and looks finished. Comparing
    durations is the only honest check, and skipping it would mean quietly
    publishing truncated lectures.
    """
    if not (os.path.exists(dst) and os.path.getsize(dst) > 0):
        return False
    try:
        d_out = probe(dst)["duration"]
        d_src = probe(src)["duration"]
    except Exception:
        return False
    return d_src > 0 and d_out >= d_src * tol


def shrink(src, dst, budget_mb, on_progress=None, cancel=None, log=None):
    """Encode `src` to `dst` under budget_mb. Returns (ok, message)."""
    def say(m):
        if log:
            log(m)

    if not available():
        return False, "ffmpeg not found"

    info = probe(src)
    dur = info["duration"]
    if dur <= 0:
        return False, "could not read duration"

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    total_kbps = budget_mb * 8 * 1024 / dur * SAFETY
    vbudget = max(total_kbps - AUDIO_KBPS, 24)

    vf = []
    if info["width"] > 1280:
        vf.append("scale=1280:-2")
    if info["fps"] > 15:
        vf.append("fps=15")
    vfs = ",".join(vf)

    base = [FFMPEG, "-v", "error", "-y", "-i", src]
    prog = ["-progress", "pipe:1", "-nostats"]
    audio = ["-c:a", "aac", "-b:a", "%dk" % AUDIO_KBPS, "-ac", "1"]
    tail = ["-movflags", "+faststart", dst]

    if info["vbitrate"] <= vbudget and not vfs:
        say("video already fits — copying stream, shrinking audio only")
        rc = _run(base + prog + ["-c:v", "copy"] + audio + tail,
                  dur, on_progress, cancel)
    else:
        say("re-encode: CRF 30, cap %d kbps%s" % (vbudget, ", " + vfs if vfs else ""))
        rc = _run(base + prog + (["-vf", vfs] if vfs else []) +
                  ["-c:v", "libx264", "-preset", PRESET, "-crf", "30",
                   "-maxrate", "%dk" % vbudget, "-bufsize", "%dk" % (vbudget * 2)]
                  + audio + tail, dur, on_progress, cancel)

    if rc == -1:
        return False, "cancelled"
    if rc != 0 or not os.path.exists(dst):
        return False, "ffmpeg failed (code %s)" % rc

    got = os.path.getsize(dst) / 1048576
    if got > budget_mb:
        say("landed at %.0f MB — retrying two-pass for a hard fit" % got)
        kbps = int(vbudget)
        logf = dst + ".pass"
        p1 = base + (["-vf", vfs] if vfs else []) + [
            "-c:v", "libx264", "-preset", PRESET, "-b:v", "%dk" % kbps,
            "-pass", "1", "-passlogfile", logf, "-an", "-f", "mp4", os.devnull]
        subprocess.run(p1, capture_output=True, creationflags=NOWIN)
        rc = _run(base + prog + (["-vf", vfs] if vfs else []) +
                  ["-c:v", "libx264", "-preset", PRESET, "-b:v", "%dk" % kbps,
                   "-pass", "2", "-passlogfile", logf] + audio + tail,
                  dur, on_progress, cancel)
        for ext in ("-0.log", "-0.log.mbtree", ""):
            try:
                os.remove(logf + ext)
            except OSError:
                pass
        if rc != 0:
            return False, "two-pass failed"
        got = os.path.getsize(dst) / 1048576

    if not complete(src, dst):
        return False, "output is shorter than the source — encode incomplete"
    return True, "%.0f MB" % got
