# Resolution ladder: 1080 -> 720 -> 480 -> split

**Date:** 2026-09-27
**Status:** design, approved in chat 2026-09-27

## Goal

Since 2026-09-26 the bot uploads through our own `telegram-bot-api` in local
mode, so the upload limit is 2000 MB instead of 50 MB
(`docs/superpowers/specs/2026-09-26-local-bot-api-server-design.md`). Videos are
still capped at 480p by `Settings.max_video_resolution`. Deliver the best of
1080p, 720p and 480p that fits in one file, and split only when even 480p does
not fit.

Raising the setting alone is wrong: the YouTube loop would then split a long
video at 1080p instead of dropping to 720p, and the rule is "split only if 480p
does not fit".

## Non-goals

- **No change of downloader.** YouTube stays on pytubefix, social stays on
  yt-dlp. Moving YouTube to yt-dlp is a separate decision: yt-dlp needs a JS
  runtime for YouTube that the image does not enable, and pytubefix works today.
- No HEVC, no VAAPI. x264 stays (the reasons are in the 2026-09-26 spec's
  deferred parameters; this spec supersedes the 720p ceiling recorded there).
- Splitting stays in the code as the last resort.
- No change to audio downloads.

## The rule

Rungs, highest first: **1080, 720, 480**. A rung is measured by the **short
side** of the frame, not the height: a portrait 1080x1920 is a 1080 rung, and
must not be scaled to 270x480 or 608x1080.

Pick the highest rung whose **estimated** size fits the upload limit in one file
(and, on the social path, whose re-encode fits the time budget below). Estimates
are made before downloading; only the chosen rung is downloaded. If no rung fits
in one file, take 480 and split it, exactly as today.

After download the real size is checked again as a safety net. A miss goes to
the existing split, never to a second download or a second encode.

## Shared pieces

One small module holds what both paths use, so the rule lives in one place:

- `RUNGS = (1080, 720, 480)` and the top rung from `Settings.max_video_resolution`
  (default raised from 480 to 1080; prod sets no override -- checked 2026-09-27).
  A rung above the setting is dropped, so the setting still works as a ceiling.
- `choose_rung(candidates, limit) -> (rung, needs_split)`: given, per rung, an
  estimated size (and on the social path a yes/no from the time budget), returns
  the highest rung that fits in one file, else `(480, True)`. Pure function; the
  paths feed it their own estimates.
- `scale_filter(width, height, rung) -> str | None`: the ffmpeg `-vf` value that
  brings the short side down to `rung`, keeping the aspect (`scale=-2:R` for
  landscape, `scale=R:-2` for portrait), or `None` when the short side is already
  at or below the rung.
- `split_video` stays where it is and stays shared.

## YouTube path (`bot/util/youtube/video.py`)

- The audio stream (and the optional translation inside `get_audio_stream`) is
  downloaded **once**, before any rung is tried. Today it sits inside
  `pick_stream`, which is fine only because that runs once; the restructure must
  keep it that way.
- For each rung: the candidate is the smallest avc1 stream whose real short side
  is >= the rung (re-encoded down with `scale_filter`), else the best stream
  below the rung (copied). Rungs that resolve to the same stream are one
  candidate. Real resolution keeps coming from `get_resolution` (streams that lie
  about their resolution are still filtered out), sorted by short side.
- Estimate = `stream.filesize` + audio size, against `max_upload_size_bytes * 0.98`
  -- the check `pick_stream` already makes before downloading. The loop order is
  what changes: **rungs outer, one part each**; only the 480 rung may take 2..10
  parts. Today `n_parts` is the outer loop and `tier1 or tier2` discards every
  lower native stream, which is exactly the "split at 1080" bug.
- A rung that downloads and merges but comes out over the limit (only possible
  when it was re-encoded -- YouTube serves avc1 up to 1080p, so this is rare)
  has its files deleted before the next rung is tried.
- `check_download_adaptive`'s split-until-parts-fit loop is unchanged.

## Social path (`bot/util/social/download.py`, `bot/worker/pipeline.py`)

Two kinds of download, estimated differently:

- **Merged** (separate video + audio): the yt-dlp merger always re-encodes, so
  the source size says nothing about the output. The encode gets a per-rung
  bitrate ceiling, which makes the output size known in advance:

  | rung | `-maxrate` | `-bufsize` | fits 2000 MB up to |
  |------|-----------|-----------|--------------------|
  | 1080 | 6 Mbit/s  | 12M       | ~43 min            |
  | 720  | 3 Mbit/s  | 6M        | ~83 min            |
  | 480  | 1.5 Mbit/s| 3M        | ~2.7 h             |

  Audio is pinned to `-b:a 128k` so it enters the estimate as a constant.
  Estimate = duration x (maxrate + 128 kbit/s). CRF stays at the x264 default; the
  ceiling only bounds the peaks, so short simple clips stay small.
- **Single file** (one format with audio, no merge, no re-encode): estimate =
  the selected format's `filesize`, else `filesize_approx`. yt-dlp's format
  selection is re-applied to the probe pass's info for each rung, without
  re-extracting (the plan pins the exact yt-dlp call). Unknown size = no
  estimate: the rung is allowed and the post-download check decides.

**Time budget (merged only).** Measured 2026-09-27 on latitude, in the worker
image, x264 main/yuv420p on a synthetic 30 fps source: 1080p ~48 fps, 720p ~100,
480p ~200. Real footage is slower, call it half. A rung is allowed only if the
duration is within its threshold: **1080 up to 10 min, 720 up to 30 min, 480
beyond**. Worst cases at half speed: 10 min at 1080 ~12 min of encoding, 30 min
at 720 ~18 min, 90 min at 480 ~27 min.

- `process_social_link`'s `time_limit` goes from 25 to **45 min**, like
  `process_youtube_link`. Known limit: a 3-hour VK video at 480 still overruns;
  that fails as it would today.
- **Carousels:** one yt-dlp call downloads all positions with one set of options,
  so the whole batch gets one rung -- the lowest any item needs. Carousel items
  are short, so in practice that is 1080.
- **Short side in yt-dlp:** the selector's `[height>=R]` becomes
  `[height>=R][width>=R]` (short side >= R). Whether a format with missing width
  should pass (`>=?`) is checked against a real Instagram and VK probe in the
  plan, not assumed. The merger's `scale=-2:'min(R,ih)'` becomes the short-side
  expression, verified with one ffmpeg run on a portrait clip.
- `_split_oversized` stays as the fallback after download.

## Cache

`Settings.video_cache_tag` goes from `l` to `l2`, so every local video key
(`ytl:`/`dl2l:`) is fetched again at the new rungs -- including any video that
was cached split. After the deploy, the old `ytl:*` and `dl2l:*` keys are removed
once with `SCAN` + `DEL`: no key in this repo has a TTL, so a prefix change alone
only orphans them. The cloud keys (`yt:`/`dl2:`) stay for a rollback. Cost, same
as every prefix change: a link pasted across the deploy leaves its waiter under
the old key, and that user gets silence.

## Verification

No test suite (`.claude/memory/project.md`, "Verification"). On the dev stack
with the local `telegram-bot-api` and the debug bot:

1. A portrait Instagram reel arrives at its native short side (1080 wide), not
   270x480 or 608x1080.
2. The 939-second VK video from 2026-09-26 arrives as one file at 720 (over the
   10-minute 1080 threshold), within the time limit.
3. A long YouTube video whose 1080 stream does not fit in 2000 MB arrives as one
   file at 720; the worker log shows 1080 skipped **without** a download.
4. A short YouTube video arrives at 1080, copied (no `libx264` in the log).
5. `choose_rung` and `scale_filter` checked in isolation with a few inputs
   (landscape, portrait, square, below the lowest rung, nothing fits).
6. ruff and pyright: no new findings versus the baseline.

## Rollout

Release as usual (tag = deploy). Then the one-off `SCAN` + `DEL` of the old
local keys on latitude, then re-run checks 2 and 3 against the production bot.
