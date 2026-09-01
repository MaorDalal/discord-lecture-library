# Discord Library Panel

A local control panel that publishes a folder of lecture recordings into a
Discord server as forum posts — one forum per course, one post per lecture,
the video attached to the post.

Everything runs on your machine. The bot token you create yourself does the
structural work (categories, forums, posts); the videos are sent from your own
Discord client, because a bot is capped at the server's boost tier (20 MB
unboosted) while a Nitro account can send 500 MB.

```
Start Panel.bat          →  opens http://127.0.0.1:7333
```

## What it does

The library is scanned from the parent folder: one subfolder per course, MP4s
inside. Course, lecture number, part and type (lecture, tutorial, workshop, marathon, review) are
parsed from the filenames, so posts come out titled and tagged in order.

| Step | What happens |
|------|--------------|
| **1 · Build forums** | Creates the category and one forum channel per course, with tags. Switch **Create forums automatically** off to publish only into forums that already exist. |
| **2 · Compress large files** | Re-encodes anything over 480 MB with ffmpeg to fit under Nitro's limit. Output goes to `_optimized/`, originals are never touched. |
| **3 · Create threads** | Creates every forum post up front — titled, tagged, ordered. Paced around Discord's 50-posts-per-5-minutes limit. |
| **4 · Manual upload** | Puts the file on the clipboard, opens the post in the Discord desktop app, and the video goes up with **Ctrl+V → Enter**. |

## Uploading without babysitting it

Step 4 has two modes.

**Continuous mode** — you press Ctrl+V and Enter; the panel detects the Enter and
immediately opens the next post with the next file already on the clipboard.
It does not wait for the upload to finish, so the previous file keeps
uploading in the background while you send the next one.

**🤖 Automatic** — the panel types Ctrl+V and Enter itself. For each file it
opens the post, **confirms from the Discord window title that the right post is
open**, clicks the message box, pastes and sends — then moves straight on,
keeping a few uploads in flight at once. **Esc aborts immediately.**

Nothing is ever assumed to have worked: a file counts as uploaded only when
that post's `message_count` turns non-zero. Anything that never arrives is
un-marked and returns to the queue.

## Selecting what to publish

Tick individual files or whole courses in **Courses**. Every step then applies
to the selection only — step 1 creates forums only for the courses involved,
step 3 creates only those posts, and the upload queue holds only those files.
With nothing ticked, the whole library is in scope.

## Adapting it to your own library

Two things are specific to the library you point this at, and both are meant to
be edited:

- **`patterns.json` holds the filename vocabulary.** It is what turns a filename
  into a type, a number and a part. Those strings are an input format, not
  interface text - replace them with the words your own recordings use. Drop a
  `patterns.local.json` beside it to override without editing a tracked file.
- **`panel.py` holds the text written into Discord.** The category name, forum
  topic, post body and fallback tag sit in one block at the top of the file.
  Change those five values to match the server you are publishing to.

## Requirements

- Windows, Python 3.11+
- [ffmpeg](https://www.gyan.dev/ffmpeg/builds/) on `PATH` (only needed for step 2)
- Discord desktop app, signed in
- A bot token: Developer Portal → New Application → Bot → Reset Token, then
  invite it to your server with *Manage Channels* and *Send Messages*

Paste the token into the panel. It is stored in `state.json` next to the
script, together with your progress — **that file is gitignored and must stay
that way.**

## Files

| | |
|---|---|
| `panel.py` | HTTP server, job runner, all four steps |
| `ui.html` | the whole interface |
| `discord_api.py` | REST client with rate-limit bucket tracking |
| `library.py` | filename → course / type / number / part |
| `patterns.json` | the filename vocabulary the parser matches |
| `optimizer.py` | ffmpeg two-pass shrink with a completeness check |
| `keywatch.py` | fires when *you* press Enter inside Discord |
| `autosend.py` | types Ctrl+V / Enter into Discord for the automatic mode |
