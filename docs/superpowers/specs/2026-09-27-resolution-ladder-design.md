# Resolution ladder: 1080 -> 720 -> 480 -> split

**Date:** 2026-09-27
**Status:** design, approved in chat 2026-09-27; amended after review (strict
per-rung selectors, `veryfast`, waiter TTL), with per-rung size caps, and with
ready files first on the social path

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

Each rung has its own size cap -- the "golden middle" agreed 2026-09-27: a long
video should drop to a lighter rung well before it hits the hard limit, because a
2 GB file is slow to upload, slow to fetch on a phone and heavy on the server's
disk.

| rung | cap per file |
|------|--------------|
| 1080 | 1000 MB      |
| 720  | 1500 MB      |
| 480  | 2000 MB (the upload limit) |

Each cap is clipped to `Settings.max_upload_size_bytes`, so on the cloud server
(50 MB) all three collapse to 50 MB and the rule still works after a rollback.
The caps live in code next to `RUNGS`, not in settings.

Pick the highest rung whose **estimated** size fits **its own cap** in one file
(and, on the social path, whose re-encode fits the time budget below). A rung the
source does not reach (short side below it) is skipped, not substituted with a
lower stream: that stream is judged at its own rung, against its own cap.
Estimates are made before downloading; only the chosen rung is downloaded. If no
rung fits, take 480 and split it against the upload limit, exactly as today.

**Splitting stays at 480** (asked and settled 2026-09-27). A split happens only
when 480 does not fit in 2 GB, i.e. for videos over ~4 hours; there 720 would be
5+ GB and 1080 10+ GB to download, merge and upload inside the 45-minute actor
limit, and nobody watches a 4-hour video in ten 2 GB parts.

After download the real size is checked again as a safety net. A miss goes to
the existing split, never to a second download or a second encode.

## Shared pieces

One small module holds what both paths use, so the rule lives in one place:

`bot/util/ladder.py`:

- `RUNG_CAPS_MB = {1080: 1000, 720: 1500, 480: 2000}` and the top rung from `Settings.max_video_resolution`
  (default raised from 480 to 1080; prod sets no override -- checked 2026-09-27).
  A rung above the setting is dropped, so the setting still works as a ceiling.
- `rung_cap(rung) -> int`, clipped to the upload limit, and
  `fits(rung, size | None) -> bool` (an unknown size fits; the post-download check
  decides). Planning replaced a single `choose_rung` with this pair: the YouTube
  loop has to act between rungs (merge, re-check, move on), so the order and the
  caps are shared and each path walks the rungs itself.
- `rung_candidates(dims) -> [(rung, source)]`: per rung, the smallest source
  whose short side reaches it; unreached rungs skipped; only the lowest rung
  falls back to the best source below it. Used by the YouTube path.
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
  is >= the rung -- copied when it equals the rung, re-encoded down with
  `scale_filter` when above. No such stream = the rung is skipped. Only the 480
  rung falls back to the best stream below it (copied), so a 360p-only video
  still downloads as today. Real resolution keeps coming from `get_resolution` (streams that lie
  about their resolution are still filtered out), sorted by short side.
- Estimate = `stream.filesize` + audio size, against `rung_cap(rung) * 0.98` --
  the check `pick_stream` already makes before downloading, now per rung. The loop order is
  what changes: **rungs outer, one part each**; only the 480 rung may take 2..10
  parts. Today `n_parts` is the outer loop and `tier1 or tier2` discards every
  lower native stream, which is exactly the "split at 1080" bug.
- A rung that downloads and merges but comes out over its cap (only possible
  when it was re-encoded -- YouTube serves avc1 up to 1080p, so this is rare)
  has its files deleted before the next rung is tried.
- `check_download_adaptive`'s split-until-parts-fit loop is unchanged.

## Social path (`bot/util/social/download.py`, `bot/worker/pipeline.py`)

**A ready file first (agreed 2026-09-27).** Short clips -- TikToks, Instagram
reels -- are not re-encoded; the ladder is for long videos. So per video entry:

1. A **ready file** -- mp4, h264, 4:2:0, with sound, playable everywhere as
   downloaded -- is sent as is. The highest one whose short side is between the
   lowest and the top rung and whose size fits its own rung's cap; if ready files
   reaching the lowest rung exist but none fits, the lowest of them, as is (split
   after download if it is over the upload limit); a ready file below the lowest
   rung only when nothing reaches it. Never above the top rung (that would need a
   downscale, i.e. a re-encode).
2. **No ready file** (DASH-only sites; silent Instagram carousel items, which
   have no sound): the merged ladder below.

Measured 2026-09-27, all fast-start (`moov` before `mdat`), so no remux is needed:
VK `url720` is h264 High 1280x720 yuv420p + AAC (219 MB, 939 s -- the video that
used to be split now goes as is); Instagram reel formats `1/2/3` are h264 720x1280
yuv420p + AAC, reported by yt-dlp with no codec, size or dimensions, so they are
read by ffprobe from their URL (~0.6 s, best-first, stopping at the first that
fits). Consequences, accepted: **Instagram reels go at 720** (their 1080 exists
only as VP9 DASH, which would need the re-encode); TikTok stays at its h264 540p,
now the best-ranked bitrate rather than `worst`.

The ready choice is made per entry inside the download pass itself (yt-dlp takes
a callable `format`), so the probe and the download cannot disagree about it.

### Fallback: the merged ladder (entries without a ready file)

**Rung = strict selector.** Today's selector ends in `/best[ext=mp4]/best`, a
fallback with no resolution filter and no merge. At 480 almost everything
matches the first alternative, so it rarely fires; at 1080 it would catch every
source that tops out lower. The VK video from 2026-09-26 is exactly that
(probed 2026-09-27: its best stream is 1280x720): the 1080 selector would skip
both `>=1080` alternatives and take a pre-muxed file with no re-encode -- no
yuv420p, no main profile, no bitrate ceiling. So each rung gets a strict
selector with no loose tail:

- rung R (1080, 720): `worstvideo[ext=mp4][height>=R][width>=R]+bestaudio[ext=m4a]/worst[ext=mp4][height>=R][width>=R][vcodec!~='^(h265|hevc|hev1|hvc1|bytevc1)']`
  -- the single-file alternative excludes HEVC because nothing re-encodes it:
  TikTok serves its 720p only as `bytevc1` (probed 2026-09-27), so a TikTok stays
  at its h264 540p, as today.
- rung 480: the same, followed by today's loose tail `/best[ext=mp4]/best`, so a
  source below 480 still downloads as it does today.

"The selector matches nothing" means "try the next rung". Strictness is for the
decision only: the download pass is a second extraction (with the cookie jar if
the anonymous one hit a login wall) and may see other formats, so it asks for the
chosen rung, then each rung below it, ending in the loose tail -- a mismatch costs
a rung, never the post. Selection is re-applied
to the probe pass's info for each rung without re-extracting (the plan pins the
exact yt-dlp call); the selected format tells us both whether the rung exists and
which kind of download it is:

- **Merged** (separate video + audio): the yt-dlp merger always re-encodes, so
  the source size says nothing about the output. The encode gets a per-rung
  bitrate ceiling, which makes the output size known in advance:

  | rung | `-maxrate` | `-bufsize` | fits its cap up to |
  |------|-----------|-----------|--------------------|
  | 1080 | 6 Mbit/s  | 12M       | ~21 min (1000 MB)  |
  | 720  | 3 Mbit/s  | 6M        | ~63 min (1500 MB)  |
  | 480  | 1.5 Mbit/s| 3M        | ~2.7 h (2000 MB)   |

  Audio is pinned to `-b:a 128k` so it enters the estimate as a constant.
  Estimate = duration x (maxrate + 128 kbit/s). CRF stays at the x264 default;
  the ceiling only bounds the peaks, so short simple clips stay small.
- **Single file** (one format with audio, no merge, no re-encode): estimate =
  the selected format's `filesize`, else `filesize_approx`. Unknown size = no
  estimate: the rung is allowed and the post-download check decides (a miss is
  split, as today).

**Encoder speed.** The merger gets `-preset veryfast`. Measured 2026-09-27 on
latitude, in the worker image, on a real 60 s 720p VK clip, with the host busy
after a reboot (load ~11 on 8 threads -- a realistic day for a box that also
runs immich): `medium` 40 s (~37 fps), `veryfast` 21 s (~72 fps), and the
`veryfast` file was *smaller* (9.3 vs 10.1 MB) under the same 3 Mbit/s ceiling.
Synthetic `testsrc2` read 2.7x faster than real footage and is not used for
sizing. Scaled by pixel count from the real clip, `veryfast` gives roughly:
1080p ~32 fps, 720p ~72, 480p ~140.

**Unknown duration.** Instagram gives none in the probe, for carousel items and
single reels alike (2026-09-27). The duration is then read by ffprobe from the
selected video format's URL (0.6 s on a reel). If that fails too, rungs with a
duration ceiling are skipped: encoding time is the one limit that kills the job
instead of splitting it.

**Upload time.** The 2026-09-26 rehearsal pushed ~140 MB through the local server
and on to Telegram in 13 s (~10 MB/s): 2 GB is ~3.5 min against the worker's
30-minute upload timeout (`bot/util/tg.py`). Re-measured on a large file in the
rehearsal.

**Time budget (merged only).** A rung is allowed only if the duration is within
its threshold: **1080 up to 10 min, 720 up to 30 min, 480 beyond**. Worst cases
at 30 fps source: 10 min at 1080 ~9 min of encoding, 30 min at 720 ~13 min,
90 min at 480 ~19 min. The worker runs 4 threads (`--processes 1 --threads 4`),
so parallel jobs share the encoder's CPU and these stretch; the margin to the
actor limit below is for that, and for the download and the upload.

- `process_social_link`'s `time_limit` goes from 25 to **45 min**, like
  `process_youtube_link`. Known limit: a 3-hour VK video at 480 still overruns;
  that fails as it would today.
- **Waiters:** `_SOCIAL_WAITERS_TTL` (90 min) goes to **3 h**, like
  `_YOUTUBE_WAITERS_TTL`. With 3 attempts x 45 min plus backoff the retry budget
  is ~2.5 h; at 90 min a job that succeeded on its last retry would deliver to an
  expired waiter list and the user would get silence.
- **Carousels:** one yt-dlp call downloads all positions with one set of merger
  options, so the post gets one merger rung -- the lowest any entry *without a
  ready file* needs. Ready items ignore it: nothing re-encodes them.
- **Short side in yt-dlp:** `[height>=R][width>=R]` above, without `>=?`: every
  video format probed on Instagram, VK and TikTok carried a width (2026-09-27);
  the formats without one (Instagram's `0..3`, VK's `url720`) carry no codec or
  height either and were already excluded by today's `[height>=480]`.
- **Known, unchanged:** silent Instagram carousel items are VP9 video-only
  single files -- no merge, so no re-encode -- exactly as at 480 today, now at
  their 1080. Instagram carousel entries carry no duration in the probe, so their
  merged estimate is unknown and allowed. The merger's `scale=-2:'min(R,ih)'` becomes the
  short-side expression, verified with one ffmpeg run on a portrait clip.
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

1. A portrait Instagram reel arrives as its ready file, 720x1280, with no
   re-encode (no `libx264` in the log).
2. The 939-second VK video from 2026-09-26 arrives as its ready `url720` file,
   1280x720, ~219 MB, one file, no re-encode.
3. A YouTube video of ~45-90 min, whose 1080 stream does not fit in 1000 MB
   but whose 720 fits in 1500 MB, arrives as one
   file at 720; the worker log shows 1080 skipped **without** a download.
4. A short YouTube video arrives at 1080, copied (no `libx264` in the log).
5. `choose_rung` and `scale_filter` checked in isolation with a few inputs
   (landscape, portrait, square, below the lowest rung, nothing fits).
6. ruff and pyright: no new findings versus the baseline.

## Rollout

Release as usual (tag = deploy). Then the one-off `SCAN` + `DEL` of the old
local keys on latitude, then re-run checks 2 and 3 against the production bot.
