# -*- coding: utf-8 -*-
"""Minimal Discord REST client (bot token only, stdlib only).

Handles the two upload paths:
  * <25 MiB  -> plain multipart on the thread-create request
  * >=25 MiB -> Create-Attachment-Upload-URL flow (PUT to Google storage
                first, then reference `uploaded_filename`), which is how the
                official client sends big files. Without this, anything over
                25 MiB fails with "Request entity too large" regardless of
                the server's boost tier.
"""
import json, os, time, uuid, urllib.request, urllib.error

API = "https://discord.com/api/v10"
UA = "DiscordBot (https://github.com/local/library-uploader, 1.0)"

# Boost tier -> per-file cap in MB for a BOT (bots never get Nitro).
TIER_LIMIT_MB = {0: 20, 1: 20, 2: 50, 3: 100}
TIER_NAME = {0: "ללא בוסט", 1: "רמה 1", 2: "רמה 2", 3: "רמה 3"}

CHANNEL_FORUM = 15


class DiscordError(Exception):
    pass


class Discord:
    def __init__(self, token, on_wait=None):
        self.token = token.strip()
        self._last = {}
        # bucket hash -> (remaining, unix time the window resets)
        self.limits = {}
        # route key -> bucket hash, learned from response headers
        self.routes = {}
        # called as on_wait(seconds, reason) so a long pause is visible
        # instead of looking like a freeze
        self.on_wait = on_wait

    def _sleep(self, secs, reason):
        if secs <= 0:
            return
        if self.on_wait:
            self.on_wait(secs, reason)
        end = time.time() + secs
        while time.time() < end:
            time.sleep(min(1.0, end - time.time()))

    def _note_limits(self, key, hdrs):
        """Record the bucket by Discord's own hash, not by URL.

        Forum-post creation shares ONE bucket across every channel in a
        guild (measured: consecutive creates in three different forums
        returned remaining 47, 46, 45). Keying on the path would let each
        forum believe it had a private quota and walk straight into 429s.
        """
        try:
            bucket = hdrs.get("x-ratelimit-bucket")
            if bucket:
                self.routes[key] = bucket
            rem = hdrs.get("x-ratelimit-remaining")
            rst = hdrs.get("x-ratelimit-reset-after")
            if bucket and rem is not None and rst is not None:
                self.limits[bucket] = (int(rem), time.time() + float(rst))
        except (TypeError, ValueError):
            pass

    def _await_slot(self, key):
        """Wait out a drained bucket BEFORE spending a request on a 429."""
        bucket = self.routes.get(key)
        info = self.limits.get(bucket) if bucket else None
        if not info:
            return
        remaining, reset_at = info
        if remaining <= 0:
            wait = reset_at - time.time()
            if wait > 0:
                self._sleep(wait + 0.4, "rate limit")

    # ---------- low level ----------
    def _req(self, method, path, body=None, ctype="application/json",
             raw=False, full_url=None, headers=None, retries=6):
        url = full_url or (API + path)
        key = "%s:%s" % (method, (path or url).split("?")[0])
        if not full_url:
            self._await_slot(key)
        for attempt in range(retries):
            data = body
            if body is not None and ctype == "application/json" and not raw:
                data = json.dumps(body).encode("utf-8")
            h = {"User-Agent": UA}
            if not full_url:
                h["Authorization"] = "Bot " + self.token
            if data is not None:
                h["Content-Type"] = ctype
            if headers:
                h.update(headers)
            req = urllib.request.Request(url, data=data, method=method, headers=h)
            try:
                with urllib.request.urlopen(req, timeout=900) as r:
                    txt = r.read().decode("utf-8", "replace")
                    if not full_url:
                        self._note_limits(key, r.headers)
                    return json.loads(txt) if txt.strip() else {}
            except urllib.error.HTTPError as e:
                payload = e.read().decode("utf-8", "replace")
                if e.code == 429:
                    wait = 2.0
                    try:
                        wait = float(json.loads(payload).get("retry_after", 2.0))
                    except Exception:
                        pass
                    hdr = e.headers.get("retry-after")
                    if hdr:
                        try:
                            wait = max(wait, float(hdr))
                        except ValueError:
                            pass
                    scope = e.headers.get("x-ratelimit-scope", "")
                    # sleep the FULL window; capping this at 60s just burns
                    # retries and makes a 5-minute wait look like a hang
                    self._sleep(wait + 0.5, "429 %s" % (scope or "limit"))
                    continue
                if e.code in (500, 502, 503, 504) and attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise DiscordError("HTTP %d on %s %s: %s"
                                   % (e.code, method, path or url, payload[:400]))
            except urllib.error.URLError as e:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise DiscordError("network error: %s" % e)
        raise DiscordError("gave up after %d retries: %s %s" % (retries, method, path))

    def get(self, p):
        return self._req("GET", p)

    def post(self, p, b):
        return self._req("POST", p, b)

    def patch(self, p, b):
        return self._req("PATCH", p, b)

    # ---------- discovery ----------
    def me(self):
        return self.get("/users/@me")

    def guilds(self):
        return self.get("/users/@me/guilds")

    def guild(self, gid):
        return self.get("/guilds/%s?with_counts=true" % gid)

    def channels(self, gid):
        return self.get("/guilds/%s/channels" % gid)

    def active_threads(self, gid):
        r = self.get("/guilds/%s/threads/active" % gid)
        return r.get("threads", []) if isinstance(r, dict) else []

    def archived_threads(self, cid):
        out, before = [], None
        while True:
            p = "/channels/%s/threads/archived/public?limit=100" % cid
            if before:
                p += "&before=" + before
            r = self.get(p)
            if not isinstance(r, dict):
                break
            batch = r.get("threads", [])
            out.extend(batch)
            if not r.get("has_more") or not batch:
                break
            before = batch[-1].get("thread_metadata", {}).get("archive_timestamp")
            if not before:
                break
        return out

    def starter_message(self, tid):
        """A forum post's opening message. Its id equals the thread id."""
        return self.get("/channels/%s/messages/%s" % (tid, tid))

    def upload_cap_mb(self, g):
        return TIER_LIMIT_MB.get(g.get("premium_tier", 0), 20)

    # ---------- structure ----------
    def create_forum(self, gid, name, parent_id=None, topic=None, tags=None):
        body = {"name": name, "type": CHANNEL_FORUM}
        if parent_id:
            body["parent_id"] = parent_id
        if topic:
            body["topic"] = topic[:1024]
        if tags:
            body["available_tags"] = [{"name": t[:20]} for t in tags[:20]]
        return self.post("/guilds/%s/channels" % gid, body)

    def create_category(self, gid, name):
        return self.post("/guilds/%s/channels" % gid, {"name": name, "type": 4})

    def set_tags(self, cid, tags):
        """Full replace - callers must send the complete desired list."""
        return self.patch("/channels/%s" % cid,
                          {"available_tags": [{"name": t[:20]} if isinstance(t, str)
                                              else t for t in tags[:20]]})

    # ---------- uploading ----------
    def _slot(self, cid, filename, size):
        """Reserve a CDN upload slot for a large attachment."""
        r = self.post("/channels/%s/attachments" % cid,
                      {"files": [{"id": "0", "filename": filename, "file_size": size}]})
        a = r["attachments"][0]
        return a["upload_url"], a["upload_filename"]

    def _put_file(self, url, path, progress=None):
        size = os.path.getsize(path)
        sent = [0]

        class Reader:
            def __init__(self, fh):
                self.fh = fh

            def read(self, n=-1):
                chunk = self.fh.read(1024 * 512 if n in (-1, None) else n)
                if chunk:
                    sent[0] += len(chunk)
                    if progress:
                        progress(sent[0], size)
                return chunk

        with open(path, "rb") as fh:
            req = urllib.request.Request(
                url, data=Reader(fh), method="PUT",
                headers={"Content-Type": "application/octet-stream",
                         "Content-Length": str(size), "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=3600) as r:
                r.read()

    @staticmethod
    def _multipart(payload, field, filename, blob):
        b = "----lib" + uuid.uuid4().hex
        out = bytearray()

        def w(s):
            out.extend(s.encode("utf-8") if isinstance(s, str) else s)

        w("--%s\r\n" % b)
        w('Content-Disposition: form-data; name="payload_json"\r\n')
        w("Content-Type: application/json\r\n\r\n")
        w(json.dumps(payload))
        w("\r\n--%s\r\n" % b)
        w('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
          % (field, filename.replace('"', "")))
        w("Content-Type: video/mp4\r\n\r\n")
        w(blob)
        w("\r\n--%s--\r\n" % b)
        return bytes(out), "multipart/form-data; boundary=" + b

    def create_post(self, forum_id, title, path, content="",
                    tag_ids=None, progress=None):
        """Create a forum thread whose first message carries `path`.

        Picks the right upload strategy for the file size automatically.
        """
        filename = os.path.basename(path)
        size = os.path.getsize(path)
        payload = {"name": title[:100], "auto_archive_duration": 10080,
                   "message": {"content": content[:2000]}}
        if tag_ids:
            payload["applied_tags"] = list(tag_ids)[:5]

        # 25 MiB is the hard cap on a single API request body.
        if size < 24 * 1024 * 1024:
            with open(path, "rb") as fh:
                blob = fh.read()
            if progress:
                progress(size, size)
            payload["message"]["attachments"] = [{"id": 0, "filename": filename}]
            body, ctype = self._multipart(payload, "files[0]", filename, blob)
            return self._req("POST", "/channels/%s/threads" % forum_id,
                             body=body, ctype=ctype, raw=True)

        upload_url, uploaded = self._slot(forum_id, filename, size)
        self._put_file(upload_url, path, progress)
        payload["message"]["attachments"] = [
            {"id": "0", "filename": filename, "uploaded_filename": uploaded}]
        return self.post("/channels/%s/threads" % forum_id, payload)

    def create_post_textonly(self, forum_id, title, content, tag_ids=None):
        payload = {"name": title[:100], "auto_archive_duration": 10080,
                   "message": {"content": content[:2000]}}
        if tag_ids:
            payload["applied_tags"] = list(tag_ids)[:5]
        return self.post("/channels/%s/threads" % forum_id, payload)
