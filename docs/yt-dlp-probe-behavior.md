# yt-dlp probe behavior (verified)

Facts established by live probes while building the audio pipeline, not read off
the docs. They are the reason `bot/util/audio/download.py:probe_link()` is shaped
the way it is — each one is counter-intuitive enough that "simplifying" the
function reintroduces a bug.

## Classification depends on `formats`, which flat extraction does not return

- A **single-item URL always resolves `formats`**, regardless of `extract_flat`.
  `extract_flat="in_playlist"` only flattens *playlist entries*; the top-level
  item is still fully extracted. So a single track can be classified from the
  first probe alone.
- A **playlist entry under `extract_flat="in_playlist"` carries only**
  `id`, `url`, `ie_key` (plus, extractor-permitting, `title` / `uploader` /
  `duration`). It never carries `formats`, `vcodec` or reliable `duration`.
- Therefore classifying a *playlist* as audio-only requires **one extra deep
  probe of the first entry** (`_deep_probe()` with `noplaylist: True`). There is
  no way to skip it: audio-vs-video is decided by `vcodec`, and no flat entry
  has one.

Per-extractor entry shapes seen:

| extractor | `webpage_url` on flat entries | `title` on flat entries |
|---|---|---|
| SoundCloud | `None` — only `url` is usable | absent |
| Bandcamp | present | present |

Hence the `e.get("url") or e.get("webpage_url")` order in `probe_link()`, and the
`id`/URL guard that skips (with a WARNING) any entry missing either.

## `extract_info()` returns `None` instead of raising

For URLs it cannot resolve at all — e.g. a private `https://t.me/c/...` link —
`ydl.extract_info()` returns `None` rather than raising `DownloadError`. Every
call site must `None`-guard and raise `AudioDownloadError` *before* the
`cast(dict, ...)`; the old unchecked cast produced `AttributeError` CRITICALs
that burned every dramatiq retry and paged the admin for an ordinary bad link.

## Audio-only is a property of the format list, never of the domain

`_is_audio_only()` reads `info["formats"] or [info]` and asks whether *any*
format has `vcodec` not in `(None, "none")`. This is deliberately generic — a
domain allowlist cannot know what a link holds before yt-dlp probes it, and
extractors add audio-only support over time.

Three things moved under this function on 2026-09-15/16 and the paragraph above
no longer describes it:

- `_is_audio_only()` no longer judges `info["formats"] or [info]` at the top
  level. A deep probe of an Instagram post returns a **playlist wrapper with no
  formats of its own**, and that fallback read it as "no video track" — a
  carousel of twelve videos classified as audio. It now unwraps `entries` and
  judges the first entry that actually has formats; a post made only of stills
  has none and is not audio either.
- Both probes carry **`ignore_no_formats_error`**, because one still inside a
  carousel otherwise kills the whole probe — and since `probe_link()` runs
  *before* anything is downloaded, that took the social-video path down with it
  ("Couldn't process this link", never reaching the downloader). The flag turns
  an unextractable entry into a `None` in `entries`, so the list can have holes
  and is filtered. The tolerance is playlist-only: a lone item with no formats
  raises `No media found` rather than being misclassified as audio.
- Neither probe constructs `YoutubeDL` any more. All four call sites go through
  `bot/util/ytdlp.py::extract_info()`, which runs **anonymously and retries with
  the cookie jar only on a login wall** — so a probe of a public post now sends
  no session at all.
<!-- conflicts-with: "`_is_audio_only()` reads `info[\"formats\"] or [info]` and asks whether *any* format has `vcodec` not in `(None, \"none\")`" -->
<!-- src: embedthat 4ef256c | 2026-09-17 -->

## Downloaded file path

After a download, `info["requested_downloads"][0]["filepath"]` reliably gives the
resulting file path — more reliable than reconstructing it from the output
template, which post-processors rewrite (extension changes, merges).

## Related

- `.claude/memory/project.md` — cache invariants, dramatiq `throws=` contract,
  and the rest of the repo-local memory.
- `CLAUDE.md` — the audio pipeline's place in the overall architecture.
