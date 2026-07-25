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

## Downloaded file path

After a download, `info["requested_downloads"][0]["filepath"]` reliably gives the
resulting file path — more reliable than reconstructing it from the output
template, which post-processors rewrite (extension changes, merges).

## Related

- `.claude/memory/project.md` — cache invariants, dramatiq `throws=` contract,
  and the rest of the repo-local memory.
- `CLAUDE.md` — the audio pipeline's place in the overall architecture.
