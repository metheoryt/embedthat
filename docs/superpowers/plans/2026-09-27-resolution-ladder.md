# Resolution Ladder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver videos at the best of 1080p / 720p / 480p that fits its per-rung size cap in one file, and split only when even 480p does not fit in the upload limit -- on both the YouTube and the social path.

**Architecture:** A new `bot/util/ladder.py` holds the rule (rungs, per-rung caps clipped to the upload limit, the fit check, candidate selection by short side, the scale filters). The YouTube path (`pick_stream`) loops rungs outermost, estimating from `stream.filesize` before downloading. The social path sends a ready h264 file with sound as downloaded whenever one exists (a callable yt-dlp format selector decides per entry); only entries without one take the merged ladder, whose re-encode is bounded by a per-rung bitrate ceiling so its size is known in advance.

**Tech Stack:** Python 3.12, pytubefix, yt-dlp 2026.08.19 (`YoutubeDL.build_format_selector`), ffmpeg / x264, dramatiq + redis, pydantic-settings.

**Spec:** `docs/superpowers/specs/2026-09-27-resolution-ladder-design.md` (amended alongside this plan -- read both).

## Global Constraints

- There is **no test suite**. The gate per task is: no NEW pyright/ruff findings versus the Task 0 baseline (compare finding sets, not totals), the import smoke test through all four doors (`main.py`, `bot.worker.actors`, `bot.events`, `bot.events.handlers.stats`), plus the task's own `python -` assertion script.
- Rungs `(1080, 720, 480)`, measured by the **short side**. Caps per file: 1080 -> 1000 MB, 720 -> 1500 MB, 480 -> 2000 MB, decimal MB, each clipped to `settings.max_upload_size_bytes`. Margin 0.98.
- Social: a ready file (mp4, h264, 4:2:0, with sound) is never re-encoded. Merged encode (fallback only): `-preset veryfast`, maxrate 6000/3000/1500 kbit/s with bufsize 2x, audio `-b:a 128k`. Duration thresholds for a merged rung: 1080 <= 600 s, 720 <= 1800 s, 480 unlimited.
- `process_social_link` `time_limit` 45 min; `_SOCIAL_WAITERS_TTL` 3 h; `video_cache_tag` `"l2"` locally, `""` on the cloud (unchanged).
- Splitting stays at 480, against the upload limit, as today.
- Never edit in the main checkout `/home/me/my/embedthat`. This worktree only (`/home/me/orca/workspaces/embedthat/resolution-ladder`, branch `work/resolution-ladder`).
- **Never read secret VALUES into context.** Key names only: `cut -d= -f1 <file>`. Never `cat`, `grep` without `-q`, or `docker compose config` a `.env`. Mask the token in any `telegram-bot-api` log or volume listing: `sed -E 's#[0-9]{6,}:[A-Za-z0-9_-]{30,}#<token>#g'`.
- Never `git stash`. Never `docker compose down -v` on the prod project.
- Reach the prod host as `ssh latitude.gg.ez`.
- Artifacts in English. Commit AND push after each task. Prefixes: `ladder:`, `youtube:`, `social:`, `config:`, `spec:`, `release: X.Y.Z`.
- Deploy only by `v*` tag; watch the `docker-publish` run BY ID.

## Review Focus

1. **A ready file under the lowest rung while a bigger one exists** (a long VK video whose ready 720 and 480 both miss their caps) -- must go out as the lowest ready file that reaches 480, split after download if needed, never as a ready 360. Pinned by `_ready_file`'s order (`reaching` before `below`) and reviewed against the spec's order in Task 3.
2. **An HEVC single file** (TikTok's `bytevc1_720p`) -- must never be chosen: nothing re-encodes a single file, and HEVC is not reliably playable in every Telegram client. Pinned by `_ready_info`'s codec check, `_NO_HEVC` in the fallback, and the TikTok line of the Task 3 probe.
3. **A portrait video** (1080x1920) -- must keep its short side on both paths, never 270x480 or 608x1080. Pinned by the `scale_filter` / `rung_candidates` asserts in Task 1, the ffmpeg run in Task 3, and the reel in Task 5.
4. **A rollback to the cloud server** (`BOT_API_URL` unset, 50 MB) -- all caps collapse to 50 MB and the ladder still ends in "480, split". Pinned by the cloud assertions in Task 1.
5. **The download pass sees different formats than the probe** (the cookie retry in `extract_info` re-extracts with the jar; CDNs vary between calls) -- the ready choice is re-made inside the download pass (callable selector), and the fallback chains from the chosen rung down to the lowest, loose, rung, so the worst case is a lower rung, never "Requested format is not available". Pinned by the `_download_spec` asserts in Task 3 Step 7.
6. **A merged social video with no duration in its metadata** (every Instagram probe on 2026-09-27) -- must not get an unbounded 1080 re-encode: ffprobe reads it from the URL; if that fails too, rungs with a duration ceiling are skipped. Reached only by entries without a ready file.
7. **A carousel mixing a ready item with silent video-only items** (Instagram `DdLlGOuGdlE`) -- the ready item goes as is; the silent ones take the ladder, as today; the merger rung comes from the silent ones only. Pinned by the carousel line of the Task 3 probe.

---

### Task 0: Record the lint baseline

**Files:** none in the repo. Baseline lives in `BASE=/tmp/embedthat-ladder-baseline/`.

- [ ] **Step 1: Confirm the tree is clean and at the plan commit**

Run: `git status --short && git log --oneline -1`
Expected: no status output; HEAD is the commit that added this plan.

- [ ] **Step 2: Capture pyright and ruff finding sets and write the gate**

```bash
BASE=/tmp/embedthat-ladder-baseline; mkdir -p $BASE
uv sync --frozen
uv run --frozen pyright --outputjson > $BASE/pyright.json || true
uv run --frozen ruff check . --output-format json > $BASE/ruff.json || true
jq -r '.generalDiagnostics[] | "\(.file)|\(.rule // "-")|\(.message)"' $BASE/pyright.json | sed "s|$PWD/||" | sort > $BASE/pyright.set
jq -r '.[] | "\(.filename)|\(.code)|\(.message)"' $BASE/ruff.json | sed "s|$PWD/||" | sort > $BASE/ruff.set
cat > $BASE/gate.sh <<'EOF'
#!/usr/bin/env bash
# Usage: bash /tmp/embedthat-ladder-baseline/gate.sh   (from the worktree root)
set -u
BASE=/tmp/embedthat-ladder-baseline
uv run --frozen pyright --outputjson > $BASE/pyright.now.json || true
uv run --frozen ruff check . --output-format json > $BASE/ruff.now.json || true
jq -r '.generalDiagnostics[] | "\(.file)|\(.rule // "-")|\(.message)"' $BASE/pyright.now.json | sed "s|$PWD/||" | sort > $BASE/pyright.now.set
jq -r '.[] | "\(.filename)|\(.code)|\(.message)"' $BASE/ruff.now.json | sed "s|$PWD/||" | sort > $BASE/ruff.now.set
echo "== NEW pyright =="; comm -13 $BASE/pyright.set $BASE/pyright.now.set
echo "== NEW ruff =="; comm -13 $BASE/ruff.set $BASE/ruff.now.set
echo "== import smoke =="
BOT_TOKEN=1:x DUMP_CHAT_ID=0 uv run --frozen python -c "import main, bot.worker.actors, bot.events, bot.events.handlers.stats; print('imports ok')"
EOF
bash $BASE/gate.sh
```

Expected: both NEW sections empty, `imports ok`. If the import smoke fails on the clean tree, record the exact error and treat it as the baseline. Nothing to commit.

If a later task's gate shows new `reportUnknown*` findings at a yt-dlp or pytubefix boundary, the accepted fix is a boundary `cast`, as `bot/util/ytdlp.py::_extract` does -- do not chase the library's types.

---

### Task 1: The ladder module

**Files:**
- Create: `bot/util/ladder.py`
- Modify: `bot/config.py` (`max_video_resolution` default)

**Interfaces:**
- Produces: `RUNG_CAPS_MB: dict[int, int]`, `rungs() -> tuple[int, ...]`, `lowest_rung() -> int`, `rung_cap(rung: int) -> int`, `fits(rung: int, size: int | None) -> bool`, `rung_candidates[T](dims: Mapping[T, tuple[int, int]]) -> list[tuple[int, T]]`, `scale_filter(width: int, height: int, rung: int) -> str | None`, `scale_expression(rung: int) -> str`.

- [ ] **Step 1: Write the assertion script (it fails: no module yet)**

Save as `/tmp/embedthat-ladder-baseline/t1.py`:

```python
import os

from bot.util import ladder as L

local = bool(os.environ.get("BOT_API_URL"))
if local:
    assert L.rungs() == (1080, 720, 480), L.rungs()
    assert L.lowest_rung() == 480
    assert L.rung_cap(1080) == 1_000_000_000
    assert L.rung_cap(720) == 1_500_000_000
    assert L.rung_cap(480) == 2_000_000_000
    assert L.fits(1080, 979_000_000) and not L.fits(1080, 981_000_000)
    assert L.fits(720, None)
else:
    # rollback to the cloud: every cap collapses to the 50 MB upload limit
    assert {L.rung_cap(r) for r in L.rungs()} == {50 * 1024 * 1024}

assert L.scale_filter(1920, 1080, 720) == "scale=-2:720"
assert L.scale_filter(1080, 1920, 720) == "scale=720:-2"  # portrait keeps its short side
assert L.scale_filter(1080, 1080, 720) == "scale=-2:720"
assert L.scale_filter(1280, 720, 720) is None
assert L.scale_filter(640, 360, 480) is None

dims = {"2160": (3840, 2160), "1080": (1920, 1080), "720": (1280, 720), "360": (640, 360)}
assert L.rung_candidates(dims) == [(1080, "1080"), (720, "720"), (480, "720")], L.rung_candidates(dims)
assert L.rung_candidates({"720": (1280, 720), "360": (640, 360)}) == [(720, "720"), (480, "720")]
assert L.rung_candidates({"360": (640, 360), "240": (426, 240)}) == [(480, "360")]
assert L.rung_candidates({"p": (1080, 1920)}) == [(1080, "p"), (720, "p"), (480, "p")]
assert L.rung_candidates({}) == []
print("t1 ok", "local" if local else "cloud")
```

Run: `BOT_TOKEN=1:x DUMP_CHAT_ID=0 BOT_API_URL=http://x uv run --frozen python /tmp/embedthat-ladder-baseline/t1.py`
Expected: `ModuleNotFoundError: No module named 'bot.util.ladder'` (or `ImportError`).

- [ ] **Step 2: Create `bot/util/ladder.py`**

```python
"""The resolution ladder: 1080 -> 720 -> 480, and only then split.

The one place the rule lives, for both the YouTube (pytubefix) and the social
(yt-dlp) path. A rung is the SHORT side of the frame: a portrait 1080x1920 is a
1080 rung, and pinning the height instead turns it into 270x480.
Spec: docs/superpowers/specs/2026-09-27-resolution-ladder-design.md.
"""

from collections.abc import Mapping

from bot.config import settings

# Cap per file, decimal MB like Settings.max_upload_size_bytes. A long video drops
# to a lighter rung well before the hard limit: a 2 GB file is slow to upload,
# slow to fetch on a phone and heavy on the server's disk. Highest rung first.
RUNG_CAPS_MB: dict[int, int] = {1080: 1000, 720: 1500, 480: 2000}

# Room for estimate error and container overhead -- the margin pick_stream
# always used against the upload limit.
_MARGIN = 0.98


def rungs() -> tuple[int, ...]:
    """Rungs highest first, no higher than Settings.max_video_resolution; never empty."""
    allowed = tuple(r for r in RUNG_CAPS_MB if r <= settings.max_video_resolution)
    return allowed or (min(RUNG_CAPS_MB),)


def lowest_rung() -> int:
    return rungs()[-1]


def rung_cap(rung: int) -> int:
    """Bytes one file at `rung` may take -- clipped to the upload limit, so on the
    cloud server (50 MB) every rung collapses to it and a rollback keeps working."""
    return min(RUNG_CAPS_MB[rung] * 1000 * 1000, settings.max_upload_size_bytes)


def fits(rung: int, size: int | None) -> bool:
    """An unknown size is allowed: the post-download check decides, and splits."""
    return size is None or size <= rung_cap(rung) * _MARGIN


def rung_candidates[T](dims: Mapping[T, tuple[int, int]]) -> list[tuple[int, T]]:
    """One source per rung, highest rung first: the smallest source whose short
    side reaches the rung (copied at the rung, scaled down above it).

    A rung no source reaches is skipped, never filled with a smaller source --
    that source is judged at its own rung, against its own cap. Only the lowest
    rung falls back to the best source below it, so a 360p-only video still
    downloads.
    """
    short = {item: min(w, h) for item, (w, h) in dims.items()}
    out: list[tuple[int, T]] = []
    for rung in rungs():
        reaching = [item for item in short if short[item] >= rung]
        if reaching:
            out.append((rung, min(reaching, key=short.__getitem__)))
    lowest = lowest_rung()
    if not out or out[-1][0] != lowest:
        below = [item for item in short if short[item] < lowest]
        if below:
            out.append((lowest, max(below, key=short.__getitem__)))
    return out


def scale_filter(width: int, height: int, rung: int) -> str | None:
    """`-vf` value bringing the short side down to `rung`, or None if it is already there."""
    if min(width, height) <= rung:
        return None
    return f"scale=-2:{rung}" if width >= height else f"scale={rung}:-2"


def scale_expression(rung: int) -> str:
    """`scale_filter` for when the dimensions are known only to ffmpeg (the yt-dlp
    merger). Verified 2026-09-27 on ffmpeg 7.1 in the worker image:
    1080x1920 -> 720x1280, 1920x1080 -> 1280x720, 1080x1350 -> 720x900,
    1440x1440 -> 720x720, 640x360 untouched (at rung 720)."""
    return (
        f"scale='if(gte(iw,ih),-2,min({rung},iw))'"
        f":'if(gte(iw,ih),min({rung},ih),-2)'"
    )
```

- [ ] **Step 3: Raise the ceiling in `bot/config.py`**

Replace `    max_video_resolution: int = 480` with:

```python
    # Top of the resolution ladder (bot/util/ladder.py), by the short side.
    max_video_resolution: int = 1080
```

- [ ] **Step 4: Run the assertions, local and cloud**

```bash
BOT_TOKEN=1:x DUMP_CHAT_ID=0 BOT_API_URL=http://x uv run --frozen python /tmp/embedthat-ladder-baseline/t1.py
BOT_TOKEN=1:x DUMP_CHAT_ID=0 BOT_API_URL= uv run --frozen python /tmp/embedthat-ladder-baseline/t1.py
BOT_TOKEN=1:x DUMP_CHAT_ID=0 BOT_API_URL=http://x MAX_VIDEO_RESOLUTION=720 uv run --frozen python -c "from bot.util import ladder as L; assert L.rungs() == (720, 480), L.rungs(); print('ceiling ok')"
```

Expected: `t1 ok local`, `t1 ok cloud`, `ceiling ok`.

- [ ] **Step 5: Gate, commit, push**

Run: `bash /tmp/embedthat-ladder-baseline/gate.sh` -- both NEW sections empty, `imports ok`.

```bash
git add bot/util/ladder.py bot/config.py
git commit -m "ladder: rungs, per-rung caps and short-side scaling in one module

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push
```

---

### Task 2: YouTube -- rungs outermost

**Files:**
- Modify: `bot/util/youtube/video.py` (`pick_stream`, `check_download_adaptive`; new `_download_video_stream`, `_merge`)

**Interfaces:**
- Consumes: `ladder.rung_candidates`, `ladder.fits`, `ladder.scale_filter` (Task 1).
- Produces: `pick_stream(video, output_path: Path, min_res: int) -> tuple[Stream, int, Path]` (the `max_res` parameter is gone); `check_download_adaptive(video, output_path: str, min_res: int = 360) -> tuple[Stream, list[Path]]`. The only caller, `bot/worker/pipeline.py::_handle_youtube_video`, passes `video=` and `output_path=` and needs no change.

- [ ] **Step 1: Confirm nothing else passes `max_res`**

Run: `git grep -n "max_res\|pick_stream\|check_download_adaptive" -- bot`
Expected: only `bot/util/youtube/video.py`, `bot/worker/pipeline.py` (calls `check_download_adaptive(video=..., output_path=...)`) and `bot/util/social/download.py` (its own `max_res`, rewritten in Task 3).

- [ ] **Step 2: Import the ladder**

In `bot/util/youtube/video.py`, below `from bot.config import settings`, add:

```python
from bot.util.ladder import fits, rung_candidates, scale_filter
```

- [ ] **Step 3: Replace `pick_stream` (the whole function) with the helpers and the new loop**

```python
def _download_video_stream(video: YouTubeVideoData, stream: Stream, output_path: Path) -> Path:
    filename = Path(f'{video.yt.video_id}.{stream.resolution}.{stream.codecs[0]}.video.mp4')
    path = output_path / filename
    if not path.exists():
        log.info('downloading %s video stream', filename)
        path = Path(stream.download(output_path=str(output_path), filename=str(filename)))
    log.info('%s size %dMb', path, path.stat().st_size // 1024 // 1024)
    return path


def _merge(
    video: YouTubeVideoData,
    stream: Stream,
    dims: tuple[int, int],
    rung: int,
    audio_stream_path: str | Path,
    output_path: Path,
) -> Path:
    # The rung is in the name: one source stream can serve two rungs (copied at
    # its own, scaled down below it), and those must not collide.
    merged = output_path / f'{video.yt.video_id}.{rung}.{stream.resolution}.{stream.codecs[0]}.{video.target_lang}.mp4'
    if merged.exists():
        return merged
    video_stream_path = _download_video_stream(video, stream, output_path)
    vf = scale_filter(*dims, rung)
    video_codec_args = ['-c:v', 'copy'] if vf is None else ['-vf', vf, '-c:v', 'libx264']
    log.info('merging %s and %s at %dp (%s)', video_stream_path, audio_stream_path, rung, vf or 'copy')
    command = [
        'ffmpeg',
        '-y',  # Overwrite an output file if exists
        '-i', str(video_stream_path),
        '-i', str(audio_stream_path),
        '-map', '0:v:0',  # Take video from the first input
        '-map', '1:a:0',  # Take audio from the second input
        *video_codec_args,
        '-c:a', 'aac',    # Ensure audio is in the proper format
        '-movflags', '+faststart',
        str(merged)
    ]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log.info("%s merged size: %.3fMb", merged, merged.stat().st_size / 1024 / 1024)
    return merged


def pick_stream(video: YouTubeVideoData, output_path: Path, min_res: int) -> tuple[Stream, int, Path]:
    """The highest rung that fits its own cap in one file; else the lowest rung, split.

    Rungs are the OUTER loop. With parts outermost (the old shape) a long video
    was split at the top rung instead of dropping a rung.
    """
    # Once, whatever rung wins: the audio (and its optional translation) is the
    # same for every rung.
    audio_stream_path = get_audio_stream(video, output_path)
    audio_size = Path(audio_stream_path).stat().st_size

    video_streams = (
        video.yt.streams
        .filter(file_extension='mp4', subtype='mp4', only_video=True)
        .order_by('resolution')
        .desc()
    )
    # filter only supported streams
    video_streams = [
        s for s in video_streams if
        s.resolution
        and int(s.resolution.replace('p', '')) >= min_res
        and 'avc1' in s.codecs[0]
    ]

    # clear streams from those who lie about their resolution
    real_res_to_stream = {}
    for stream in video_streams:
        width, height = get_resolution(stream)
        key = (width, height)
        if key in real_res_to_stream:
            log.info('duplicate res %dx%d stream: %s', width, height, stream)
        real_res_to_stream[key] = stream
    stream_dims = {stream: dims for dims, stream in real_res_to_stream.items()}

    candidates = rung_candidates(stream_dims)
    log.info('rung candidates: %s', candidates)
    if not candidates:
        raise YouTubeError(f'no supported video stream for {video.yt.video_id}')

    lowest_rung, lowest_stream = candidates[-1]
    for rung, stream in candidates:
        estimate = audio_size + stream.filesize
        if not fits(rung, estimate):
            log.info('%dp: %dMb estimated, over its cap -- skipped without download', rung, estimate // 1024 // 1024)
            continue
        merged = _merge(video, stream, stream_dims[stream], rung, audio_stream_path, output_path)
        if fits(rung, merged.stat().st_size):
            log.info('selected %dp (%dMb): %s', rung, merged.stat().st_size // 1024 // 1024, stream)
            return stream, 1, merged
        log.info('%dp merged over its cap, trying the next rung', rung)
        if (rung, stream) != (lowest_rung, lowest_stream):
            merged.unlink()  # the lowest rung's file is kept: the split below reuses it

    # Nothing fits in one file: the lowest rung, split against the upload limit.
    merged = _merge(video, lowest_stream, stream_dims[lowest_stream], lowest_rung, audio_stream_path, output_path)
    n_parts = math.ceil(merged.stat().st_size / (settings.max_upload_size_bytes * 0.98))
    if n_parts > 10:  # 10 max (what an album can fit)
        raise YouTubeError(f'no suitable video stream found for {video.yt.length}s video length')
    log.info('selected %dp split into %d parts (%dMb merged): %s',
             lowest_rung, n_parts, merged.stat().st_size // 1024 // 1024, lowest_stream)
    return lowest_stream, n_parts, merged
```

Notes for the implementer: the old `'-b:a'` comment line is dropped on purpose (dead code). `stream.filesize` is pytubefix's exact content length. The video-stream file of a rejected rung stays in the job's temp dir (deleted with it); only merged files, the big ones, are removed eagerly.

- [ ] **Step 4: Drop `max_res` from `check_download_adaptive`**

Replace its signature and first call:

```python
@translates_youtube_errors
def check_download_adaptive(
    video: YouTubeVideoData,
    output_path: str,
    min_res: int = 360,
) -> tuple[Stream, list[Path]]:
    output_path = Path(output_path)
    # pick one that fits best
    video_stream, n_parts, video_path = pick_stream(video, output_path, min_res)
```

The rest of the function (the split-until-parts-fit loop) is unchanged.

- [ ] **Step 5: Offline check of the decision with stub streams (no network)**

Save as `/tmp/embedthat-ladder-baseline/t2.py`:

```python
"""pick_stream's decision with fake streams: no download, no ffmpeg."""
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bot.util.youtube import video as V

MB = 1000 * 1000


class FakeStream:
    # A plain class, not SimpleNamespace: pick_stream keys dicts by stream, and
    # SimpleNamespace is unhashable.
    def __init__(self, res: int, size_mb: int) -> None:
        self.resolution, self.codecs, self.filesize = f"{res}p", ["avc1.64"], size_mb * MB
        self.w, self.h = res * 16 // 9, res


def stream(res: int, size_mb: int) -> FakeStream:
    return FakeStream(res, size_mb)


def run(streams: list[FakeStream], merged_mb: dict[int, int]) -> tuple[int, int, list[int]]:
    """Returns (rung chosen as the stream's height, n_parts, rungs merged)."""
    merged_rungs: list[int] = []

    def fake_merge(video, s, dims, rung, audio, out):
        merged_rungs.append(rung)
        return SimpleNamespace(stat=lambda: SimpleNamespace(st_size=merged_mb.get(rung, s.filesize // MB) * MB),
                               unlink=lambda: None)

    yt = SimpleNamespace(video_id="x", length=3600,
                         streams=SimpleNamespace(filter=lambda **_: SimpleNamespace(
                             order_by=lambda _: SimpleNamespace(desc=lambda: streams))))
    video = SimpleNamespace(yt=yt, target_lang="orig")
    audio = Path("/tmp/embedthat-ladder-baseline/audio.bin")
    audio.write_bytes(b"\0" * 1000)
    with mock.patch.object(V, "get_audio_stream", return_value=audio), \
         mock.patch.object(V, "get_resolution", side_effect=lambda s: (s.w, s.h)), \
         mock.patch.object(V, "_merge", side_effect=fake_merge):
        s, n, _ = V.pick_stream(video, Path("/tmp"), 360)
    return s.h, n, merged_rungs


# short video: 1080 fits, copied at once, nothing else touched
assert run([stream(1080, 300), stream(720, 150), stream(480, 80)], {}) == (1080, 1, [1080])
# ~1 h: 1080 over its 1000 MB cap -> skipped WITHOUT a merge; 720 fits 1500 MB
assert run([stream(1080, 1400), stream(720, 900), stream(480, 500)], {}) == (720, 1, [720])
# 1080 estimate fits but the merge comes out over the cap -> next rung
assert run([stream(1080, 900), stream(720, 600), stream(480, 300)], {1080: 1100}) == (720, 1, [1080, 720])
# 5 h: even 480 is over 2000 MB -> 480, split, merged once
assert run([stream(1080, 9000), stream(720, 5000), stream(480, 2900)], {}) == (480, 2, [480])
print("t2 ok")
```

Run: `BOT_TOKEN=1:x DUMP_CHAT_ID=0 BOT_API_URL=http://x uv run --frozen python /tmp/embedthat-ladder-baseline/t2.py`
Expected: `t2 ok`. (If the fake `video.yt.streams` chain does not match what `pick_stream` calls, fix the stub, not the code.)

- [ ] **Step 6: Gate, commit, push**

Run: `bash /tmp/embedthat-ladder-baseline/gate.sh` -- no NEW findings (the known `reportUnknown*` pytubefix noise in this file is baseline; a moved line must not show up as new -- the sets carry no line numbers).

```bash
git add bot/util/youtube/video.py
git commit -m "youtube: rungs outermost -- 1080/720/480 by their own caps, split only at 480

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push
```

---

### Task 3: Social -- a ready file first, the merged ladder only as the fallback

**Files:**
- Modify: `bot/util/social/download.py` (`_base_opts`, `_download_positions`, `download_social_video`; new `_format_spec`, `_download_spec`, `_selector_ydl`, `_probe_url`, `_url_duration`, `_ready_info`, `_rung_of`, `_ready_file`, `_select_format`, `_entry_rung`, `_selector`)

**Interfaces:**
- Consumes: `ladder.rungs`, `ladder.lowest_rung`, `ladder.fits`, `ladder.scale_expression` (Task 1).
- Produces: `download_social_video(url: str, output_dir: Path) -> DownloadResult` (the `max_res` parameter is gone; the only caller, `bot/worker/pipeline.py::_handle_social_video`, passes `(video.link, tmp_path)` and needs no change). The private helpers are called by the Task 3 probe script.

**The order** (spec, "Social path"): per video entry,
1. a **ready file** -- mp4, h264, 4:2:0 (or unknown), with sound -- is sent exactly as downloaded, no re-encode: the highest one whose short side is between the lowest and the top rung and whose size fits its own rung's cap; if files reaching the lowest rung exist but none fits, the lowest of them, as is (`_split_oversized` splits it after download if it is over the upload limit); a ready file below the lowest rung only when nothing reaches it;
2. **no ready file** (DASH-only sites, silent Instagram items with no sound): the merged ladder -- strict per-rung selectors, `veryfast` + bitrate ceiling, duration ceilings. The post's merger rung is the minimum over its merged entries only.

Measured 2026-09-27, all fast-start (`moov` before `mdat`): VK `url720` is h264 High 1280x720 yuv420p + AAC, 219 MB, 939 s; Instagram reel formats `1/2/3` are h264 720x1280 yuv420p + AAC, and yt-dlp reports no codec, size or dimensions for them, so they are read with ffprobe from their URL (~0.6 s each).

- [ ] **Step 1: Imports and constants**

In `bot/util/social/download.py`: add `import math` to the stdlib imports, `from collections.abc import Callable, Iterator`, `import yt_dlp` to the third-party ones, and below `from bot.util.ytdlp import extract_info`:

```python
from bot.util.ladder import fits, lowest_rung, rungs, scale_expression
```

Below `_PHOTO_FETCH_TIMEOUT = 30`:

```python
# Fallback only (no ready file): the merger always re-encodes, so the source size
# says nothing about the output. A per-rung bitrate ceiling does: duration x
# (ceiling + audio) bounds the file before a byte is downloaded.
_ENCODE_MAXRATE_KBPS = {1080: 6000, 720: 3000, 480: 1500}
_ENCODE_AUDIO_KBPS = 128
# Encoding time, not size, is what binds a merged download: measured 2026-09-27
# on latitude, x264 veryfast on real footage runs ~32 fps at 1080p and ~72 at
# 720p, under an actor limit of 45 min. The lowest rung has no ceiling.
_ENCODE_MAX_DURATION = {1080: 10 * 60, 720: 30 * 60}
# A single file is sent as downloaded, never re-encoded -- and HEVC does not
# play in every Telegram client (TikTok serves bytevc1 at 720p, 2026-09-27).
_NO_HEVC = "[vcodec!~='^(h265|hevc|hev1|hvc1|bytevc1)']"
# The options the fallback selector is compiled with: a merged pick's `ext`
# comes from the compiling instance's `merge_output_format`.
_SELECTOR_OPTS: dict[str, Any] = {"quiet": True, "merge_output_format": "mp4"}
```

- [ ] **Step 2: Strict per-rung spec, the chained download spec, `_base_opts`**

Replace `_base_opts` with:

```python
def _format_spec(rung: int) -> str:
    """Strict: a rung the source does not reach selects nothing, so the caller
    moves down a rung. Only the lowest rung keeps the old loose tail -- at a
    higher one it would catch every source below the rung and send it
    un-re-encoded. Short side >= rung means both sides >= rung; every video
    format with a codec probed on Instagram, VK and TikTok carried a width
    (2026-09-27), so no `>=?`."""
    fit = f"[height>={rung}][width>={rung}]"
    spec = f"worstvideo[ext=mp4]{fit}+bestaudio[ext=m4a]/worst[ext=mp4]{fit}{_NO_HEVC}"
    if rung == lowest_rung():
        spec += "/best[ext=mp4]/best"
    return spec


def _download_spec(rung: int) -> str:
    """The fallback the download pass asks for: the chosen rung, then every rung
    below it, ending in the lowest rung's loose tail. The download is a second
    extraction (with the cookie jar, if the site walled the anonymous one) and may
    see other formats than the probe; the worst case is then a lower rung, never
    "Requested format is not available". Safe for size and time: a lower source
    is smaller than the chosen rung's bound, and the scale expression only
    shrinks."""
    return "/".join(_format_spec(r) for r in rungs() if r <= rung)


def _base_opts(fmt: str | Callable[[dict[str, Any]], Iterator[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "format": fmt,
        "merge_output_format": "mp4",
        "quiet": True,
        # `noplaylist` is deliberately absent. A carousel URL *is* the playlist, so
        # the flag never narrowed anything: with it set, all 13 entries of a 13-item
        # post still came back, 12 were downloaded and 11 thrown away.
        # `playlist_items` is the option that actually selects.
        "ignore_no_formats_error": True,
    }
```

- [ ] **Step 3: Ready files (below `_entries_of`)**

```python
def _probe_url(fmt: dict[str, Any]) -> dict[str, Any]:
    """ffprobe of a format's URL -- the header only, ~0.6 s. {} when it fails."""
    url = fmt.get("url")
    if not url:
        return {}
    headers = "".join(f"{k}: {v}\r\n" for k, v in (fmt.get("http_headers") or {}).items())
    try:
        # kwargs become ffprobe flags; -timeout is ffprobe's HTTP timeout, in microseconds
        return ffmpeg.probe(
            url,
            v="error",
            show_entries="stream=codec_type,codec_name,width,height,pix_fmt:format=duration,size",
            headers=headers,
            timeout=30_000_000,
        )
    except Exception as e:
        log.warning("ffprobe of format %s failed: %s", fmt.get("format_id"), e)
        return {}


def _url_duration(fmt: dict[str, Any]) -> int:
    """Instagram gives no duration in the probe, for carousel items and single
    reels alike (2026-09-27). 0 when unknown."""
    try:
        return int(float(_probe_url(fmt)["format"]["duration"]))
    except (KeyError, TypeError, ValueError):
        return 0


def _ready_info(fmt: dict[str, Any]) -> tuple[int, int | None] | None:
    """(short side, size) if `fmt` is a ready file -- mp4, h264, 4:2:0, with sound,
    playable everywhere as downloaded -- else None. yt-dlp's own metadata is used
    when complete (TikTok); otherwise the URL is probed (Instagram's `1/2/3`)."""
    if fmt.get("ext") != "mp4" or "none" in (fmt.get("vcodec"), fmt.get("acodec")):
        return None
    vcodec, width, height = fmt.get("vcodec"), fmt.get("width"), fmt.get("height")
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    pix_fmt = None
    if not (vcodec and width and height and fmt.get("acodec")):
        probe = _probe_url(fmt)
        streams = probe.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video is None or not any(s.get("codec_type") == "audio" for s in streams):
            return None
        vcodec, width, height = video.get("codec_name"), video.get("width"), video.get("height")
        pix_fmt = video.get("pix_fmt")
        size = size or int((probe.get("format") or {}).get("size") or 0) or None
    if not str(vcodec).startswith(("avc1", "h264")) or pix_fmt not in (None, "yuv420p"):
        return None
    if not (width and height):
        return None
    return min(width, height), size


def _rung_of(short: int) -> int:
    """The rung a ready file of this short side is judged at."""
    return next((r for r in rungs() if short >= r), lowest_rung())


def _ready_file(formats: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The ready file to send, by the order in the spec. Walked best-first (yt-dlp
    lists formats worst first), so among equal short sides the best-ranked one
    wins -- a bitrate bump over the old `worst` for TikTok -- and the walk stops at
    the first ready file that fits, which usually means one ffprobe."""
    top, lowest = rungs()[0], lowest_rung()
    reaching: list[tuple[int, dict[str, Any]]] = []
    below: list[tuple[int, dict[str, Any]]] = []
    for fmt in reversed(formats):
        info = _ready_info(fmt)
        if info is None:
            continue
        short, size = info
        if short > top:
            continue  # would need a downscale, i.e. a re-encode
        if short < lowest:
            below.append((short, fmt))
            continue
        if fits(_rung_of(short), size):
            return fmt
        reaching.append((short, fmt))
    if reaching:
        # None fits its cap: the lightest of them as is, split after download if
        # it is over the upload limit -- still no re-encode.
        return min(reaching, key=lambda item: item[0])[1]
    if below:
        return max(below, key=lambda item: item[0])[1]
    return None
```

- [ ] **Step 4: The merged ladder, for entries without a ready file**

```python
def _selector_ydl() -> yt_dlp.YoutubeDL:
    """Compiles format selectors only -- no network."""
    return yt_dlp.YoutubeDL(_SELECTOR_OPTS)


def _select_format(ydl: yt_dlp.YoutubeDL, entry: dict[str, Any], rung: int) -> dict[str, Any] | None:
    """What yt-dlp would download for `entry` at `rung`, from the probe's info --
    no second extraction. The ctx mirrors YoutubeDL.process_video_result."""
    formats = entry.get("formats") or []
    ctx = {
        "formats": formats,
        "has_merged_format": any("none" not in (f.get("acodec"), f.get("vcodec")) for f in formats),
        "incomplete_formats": all(f.get("vcodec") == "none" for f in formats)
        or all(f.get("acodec") == "none" for f in formats),
    }
    return next(iter(ydl.build_format_selector(_format_spec(rung))(ctx)), None)


def _entry_rung(ydl: yt_dlp.YoutubeDL, entry: dict[str, Any]) -> int:
    """The highest rung this entry reaches and fits in one file at its own cap,
    for an entry with no ready file."""
    duration = int(entry.get("duration") or 0)
    for rung in rungs():
        fmt = _select_format(ydl, entry, rung)
        if fmt is None:
            continue  # the source does not reach this rung
        if fmt.get("requested_formats"):  # merged, so re-encoded
            if not duration:
                duration = _url_duration(fmt["requested_formats"][0])
            ceiling = _ENCODE_MAX_DURATION.get(rung, math.inf)
            # Unknown length: only a rung without a ceiling -- encoding time is
            # the one limit that kills the job instead of splitting it.
            if duration > ceiling or (not duration and ceiling != math.inf):
                continue
            kbps = _ENCODE_MAXRATE_KBPS[rung] + _ENCODE_AUDIO_KBPS
            estimate = int(duration * kbps * 1000 / 8) if duration else None
        else:  # one file, sent as downloaded
            estimate = fmt.get("filesize") or fmt.get("filesize_approx")
        if fits(rung, estimate):
            return rung
    return lowest_rung()


def _selector(rung: int) -> Callable[[dict[str, Any]], Iterator[dict[str, Any]]]:
    """The download pass's format selector (yt-dlp accepts a callable `format`):
    per entry, a ready file if there is one, else the chained merged ladder from
    `rung` down. Deciding per entry inside the download pass itself is what keeps
    the probe and the download from disagreeing about ready files."""
    fallback = _selector_ydl().build_format_selector(_download_spec(rung))

    def select(ctx: dict[str, Any]) -> Iterator[dict[str, Any]]:
        ready = _ready_file(ctx.get("formats") or [])
        if ready is not None:
            yield ready
            return
        yield from fallback(ctx)

    return select
```

- [ ] **Step 5: `_download_positions` takes the merger rung**

Change the signature to `def _download_positions(url: str, positions: list[int], output_dir: Path, rung: int) -> dict[str, Path]:`, replace `opts = _base_opts(max_res)` with `opts = _base_opts(_selector(rung))`, and replace the comment + `postprocessor_args` block with:

```python
    # Only a merged download (no ready file) reaches the merger. Re-encode to
    # H.264/AAC with iOS-compatible settings:
    # - yuv420p: iOS requires 8-bit 4:2:0 chroma
    # - faststart: moves moov atom to front so iOS can start playback immediately
    # - profile main: avoids B-frame issues on some decoders
    # - scale: this merger step is already a mandatory re-encode, so the short-side
    #   cap is free
    # - veryfast + maxrate: ~2x the speed of the default preset and a size known
    #   before download (a smaller file than `medium` under the same ceiling,
    #   measured 2026-09-27); audio pinned so it enters the estimate as a constant
    maxrate = _ENCODE_MAXRATE_KBPS[rung]
    opts["postprocessor_args"] = {
        "merger": [
            "-vcodec", "libx264",
            "-preset", "veryfast",
            "-profile:v", "main",
            "-pix_fmt", "yuv420p",
            "-maxrate", f"{maxrate}k",
            "-bufsize", f"{2 * maxrate}k",
            "-vf", scale_expression(rung),
            "-acodec", "aac",
            "-b:a", f"{_ENCODE_AUDIO_KBPS}k",
            "-movflags", "+faststart",
        ],
    }
```

- [ ] **Step 6: `download_social_video` -- the probe classifies, the merged entries set the rung**

Change the signature to `def download_social_video(url: str, output_dir: Path) -> DownloadResult:`. Replace `probe_opts = _base_opts(max_res)` with:

```python
    # A plain string spec for the probe: its own pick is never used (the decision
    # below reads the formats), and a callable here would ffprobe every entry twice.
    probe_opts = _base_opts(_download_spec(lowest_rung()))
```

Replace the start of the `if video_positions:` block so it reads:

```python
    downloaded: dict[str, Path] = {}
    if video_positions:
        # One yt-dlp call downloads every position with one set of merger options,
        # so the post gets one merger rung: the lowest any entry WITHOUT a ready
        # file needs. Ready files ignore it -- nothing re-encodes them.
        with _selector_ydl() as ydl:
            merged_rungs = [
                _entry_rung(ydl, entries[pos])
                for pos in video_positions
                if _ready_file(entries[pos].get("formats") or []) is None
            ]
        rung = min(merged_rungs, default=rungs()[0])
        log.info("%s: %d ready, %d merged at %dp", url, len(video_positions) - len(merged_rungs), len(merged_rungs), rung)
        downloaded = _download_positions(url, video_positions, output_dir, rung)
```

and in the retry line `downloaded.update(_download_positions(url, retry, output_dir, max_res))` replace `max_res` with `rung`.

- [ ] **Step 7: Probe the reference links (network, no download)**

Save as `/tmp/embedthat-ladder-baseline/t3.py`:

```python
import sys

import yt_dlp

from bot.util.social import download as D

spec = D._download_spec(1080)
assert spec.count("worstvideo") == 3 and spec.endswith("/best[ext=mp4]/best"), spec
assert "/best" not in D._format_spec(720), D._format_spec(720)

# per link: what each probed entry must resolve to -- ("ready", format-id prefixes) or ("ladder", rung)
expected = {
    "https://vkvideo.ru/video-164579181_456242552": [("ready", ("url720",))],
    "https://www.tiktok.com/@scout2015/video/6718335390845095173": [("ready", ("h264_540p_",))],
    "https://www.instagram.com/reel/DdwBDiPKh4a": [("ready", ("1", "2", "3"))],
    # carousel: the item with sound is ready; the silent VP9 items take the ladder, as today
    "https://www.instagram.com/p/DdLlGOuGdlE/": [("ready", ("1", "2", "3")), ("ladder", 1080), ("ladder", 1080)],
}
ok = True
for url, want in expected.items():
    with yt_dlp.YoutubeDL({**D._base_opts(D._download_spec(D.lowest_rung())), "playlist_items": "1,2,3"}) as probe:
        info = probe.extract_info(url, download=False)
    entries = [e for e in D._entries_of(info) if e and e.get("formats")]
    got = []
    with D._selector_ydl() as ydl:  # the same instance production uses
        for e in entries:
            ready = D._ready_file(e["formats"])
            got.append(("ready", ready["format_id"]) if ready else ("ladder", D._entry_rung(ydl, e)))
    match = len(got) == len(want) and all(
        g[0] == w[0] and (g[1].startswith(w[1]) if w[0] == "ready" else g[1] == w[1])
        for g, w in zip(got, want)
    )
    print("OK " if match else "BAD", url, got)
    ok &= match
sys.exit(0 if ok else 1)
```

Run: `BOT_TOKEN=1:x DUMP_CHAT_ID=0 BOT_API_URL=http://x uv run --frozen python /tmp/embedthat-ladder-baseline/t3.py`
Expected: four `OK` lines, exit 0. The TikTok id is the best-ranked `h264_540p_*` (a higher bitrate than today's `worst`), never `bytevc1_*`. If Instagram answers with a login wall from this host, rerun those two links inside the dev worker in Task 5 and note it.

- [ ] **Step 8: The merger's scale expression on a portrait source, in the image**

```bash
docker run --rm --entrypoint sh embedthat:dev -c 'ffmpeg -hide_banner -loglevel error -f lavfi -i testsrc2=size=1080x1920:rate=1 -t 1 -vf "scale='"'"'if(gte(iw,ih),-2,min(720,iw))'"'"':'"'"'if(gte(iw,ih),min(720,ih),-2)'"'"'" -y /tmp/o.mp4 && ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 /tmp/o.mp4'
```

Expected: `720,1280`. (If `embedthat:dev` is absent, use `metheoryt/embedthat:latest`.)

- [ ] **Step 9: Gate, commit, push**

Run: `bash /tmp/embedthat-ladder-baseline/gate.sh` -- no NEW findings.

```bash
git add bot/util/social/download.py
git commit -m "social: a ready h264 file first, the merged ladder as the fallback

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push
```

---

### Task 4: Time limit, waiter TTL, cache reset

**Files:**
- Modify: `bot/worker/actors.py:437` (`process_social_link` `time_limit`)
- Modify: `bot/handlers.py:65` (`_SOCIAL_WAITERS_TTL`)
- Modify: `bot/config.py` (`video_cache_tag`)

- [ ] **Step 1: Social actor limit, as YouTube's**

In `bot/worker/actors.py`, in the `@dramatiq.actor(...)` of `process_social_link`, replace `    time_limit=25 * 60_000,` with:

```python
    time_limit=45 * 60_000,  # a merged 1080/720 re-encode of up to 10/30 min (bot/util/social/download.py)
```

- [ ] **Step 2: Waiters outlive the retry budget**

In `bot/handlers.py` replace `_SOCIAL_WAITERS_TTL = 90 * 60  # ~1.5h` with:

```python
_SOCIAL_WAITERS_TTL = 3 * 60 * 60  # vs. 3 attempts x 45 min + backoff (~2.5h), as YouTube
```

- [ ] **Step 3: New local cache keys**

In `bot/config.py`, `video_cache_tag`: append to its comment and change the return:

```python
        # `l2` since the resolution ladder (2026-09-27): every `ytl:`/`dl2l:` entry
        # was cached at 480p or split, and is fetched again at the new rungs. The
        # old keys are deleted once by hand after the deploy (no key has a TTL).
        return "l2" if self.bot_api_url else ""
```

- [ ] **Step 4: Check**

```bash
BOT_TOKEN=1:x DUMP_CHAT_ID=0 BOT_API_URL=http://x uv run --frozen python -c "
from bot.util.youtube.schema import youtube_cache_key
from bot.config import settings
assert youtube_cache_key('abc') == 'ytl2:abc', youtube_cache_key('abc')
from bot.handlers import _SOCIAL_WAITERS_TTL; assert _SOCIAL_WAITERS_TTL == 10800
from bot.worker.actors import process_social_link; assert process_social_link.options['time_limit'] == 45 * 60_000
print('t4 ok')"
BOT_TOKEN=1:x DUMP_CHAT_ID=0 BOT_API_URL= uv run --frozen python -c "
from bot.util.youtube.schema import youtube_cache_key; assert youtube_cache_key('abc') == 'yt:abc'; print('cloud keys untouched')"
```

Expected: `t4 ok`, `cloud keys untouched`. (Importing `bot.worker.actors` binds the redis broker but does not connect at import; if it does on this box, drop that assert and check the line by `git diff` instead.)

- [ ] **Step 5: Gate, commit, push**

Run: `bash /tmp/embedthat-ladder-baseline/gate.sh`.

```bash
git add bot/worker/actors.py bot/handlers.py bot/config.py
git commit -m "config: social limit 45 min, waiters 3 h, local video keys l2 for the ladder

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push
```

---

### Task 5: Rehearsal on the debug bot

Needs the user for the Telegram side (sending links to `@assinstantbot`). The debug bot is on the cloud since the 2026-09-26 rehearsal; this puts it on the local server and leaves it there.

**Files:**
- Create (not committed): `.env` (copy), `/tmp/embedthat-ladder-baseline/local.yml`
- Modify: spec (results), `.claude/memory/project.md`

- [ ] **Step 1: Nothing else polls the debug bot**

Run: `docker ps --format '{{.Names}} {{.Image}}' | grep -i embedthat`
If a dev stack from another checkout is up, ask the user before stopping it.

- [ ] **Step 2: `.env` in without reading it; the override**

```bash
cp /home/me/my/embedthat/.env .env
cut -d= -f1 .env | grep -vE '^\s*(#|$)'
git status --short   # must not list .env
cat > /tmp/embedthat-ladder-baseline/local.yml <<'EOF'
services:
    bot:
        environment: { REDIS_URL: "redis://redis", BOT_API_URL: "http://telegram-bot-api:8081" }
    worker:
        environment: { REDIS_URL: "redis://redis", BOT_API_URL: "http://telegram-bot-api:8081" }
EOF
```

If host port 6379 is taken by another dev redis, add `redis: { ports: !reset [] }` to `local.yml`.

- [ ] **Step 3: Log the debug bot out of the cloud, start the local stack**

```bash
O=/tmp/embedthat-ladder-baseline
docker compose run --rm --no-deps -T -e BOT_API_URL= -e REDIS_URL=redis://redis bot python -c "
import asyncio
from bot.util.tg import make_bot
async def go():
    b = make_bot()
    try:
        print('logOut:', await b.log_out())
    finally:
        await b.session.close()
asyncio.run(go())"
docker compose -f compose.yml -f $O/local.yml up -d --build redis telegram-bot-api bot worker
docker compose logs --tail 30 telegram-bot-api | sed -E 's#[0-9]{6,}:[A-Za-z0-9_-]{30,}#<token>#g'
docker compose logs --tail 30 bot worker
```

Expected: `logOut: True` (or an error saying it is already logged out -- fine); bot polling with no `Unauthorized`.

- [ ] **Step 4: The five cases**

Ask the user to send, one at a time, and after each run
`docker compose logs --since 10m worker | grep -E "rung|selected|skipped|merging|dump chat|split|p for|TimeLimit|retrying"`:

1. A portrait Instagram reel -> one file at 720x1280, `1 ready, 0 merged`, no `libx264` anywhere in the job's log.
2. The VK video `https://vkvideo.ru/video-164579181_456242552` -> one file at 1280x720 (~219 MB), `1 ready, 0 merged`, no `libx264`; record the job's wall time (download + upload only now).
3. A YouTube video of 45-90 min -> the log shows `1080p: ...Mb estimated, over its cap -- skipped without download` and `selected 720p`; one file. **Record the upload wall time** (from `sending 1 part(s) to dump chat` to the next line): the 2026-09-26 rehearsal pushed ~140 MB in 13 s (~10 MB/s), which puts 2 GB at ~3.5 min against the 30-min `UPLOAD_TIMEOUT` in `bot/util/tg.py`; a much slower number here is a finding to raise before release.
4. A short YouTube video -> `selected 1080p`, `(copy)` in the merge line, no `libx264`.
5. A TikTok -> `1 ready`, an `h264_540p_*` file, delivered, plays on the user's phone.

For each, record size and resolution of what arrived (`docker compose logs` merge/size lines, or the user reading it off the message).

- [ ] **Step 5: Record results; update project memory**

Add a `## Rehearsal (2026-09-27)` section to the spec with the five results and wall times. In `.claude/memory/project.md` replace the bullet starting `- The 480p cap picks the **smallest** stream at or above 480p` with:

```markdown
- The resolution ladder (`bot/util/ladder.py`, 2026-09-27) picks, per rung, the
  **smallest** source whose short side reaches it and scales it down -- not the
  largest one under it, otherwise a 360p/720p-only video is needlessly delivered
  lower. A rung the source does not reach is skipped; only the lowest rung falls
  back to a smaller source. Per-rung caps: 1080 <= 1000 MB, 720 <= 1500 MB,
  480 <= 2000 MB; split only at 480.
- The social path sends a **ready file** (mp4, h264, 4:2:0, with sound) as
  downloaded whenever one exists -- Instagram reels at 720 via formats `1/2/3`,
  which yt-dlp reports without codec or size (read by ffprobe from the URL), VK
  via `url720`. Only entries without one are merged and re-encoded, and there the
  yt-dlp selector must stay **strict** above the lowest rung: its loose `/best`
  tail catches any source below the rung and sends it un-re-encoded.
```

- [ ] **Step 6: Commit, push**

```bash
git add docs/superpowers/specs/2026-09-27-resolution-ladder-design.md .claude/memory/project.md
git commit -m "spec, memory: resolution ladder rehearsal results

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push
```

---

### Task 6: Release, reset the local cache, check production

**Files:**
- Modify: `pyproject.toml` (`version`), `uv.lock` (via `uv lock`)

- [ ] **Step 1: Merge to main**

With the user's yes: fast-forward `main` to this branch from the main checkout's origin, never by editing the main checkout:

```bash
git fetch origin
git push origin work/resolution-ladder:main   # fast-forward only; refuses if main moved
```

If it refuses, rebase this branch on `origin/main`, rerun the gate, push, retry.

- [ ] **Step 2: Bump and tag**

```bash
sed -i 's/^version = "0.4.24"$/version = "0.4.25"/' pyproject.toml
uv lock
git add pyproject.toml uv.lock
git commit -m "release: 0.4.25

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push origin HEAD:main
git tag v0.4.25 && git push origin v0.4.25
gh run list --workflow docker-publish.yml --event push --limit 5 --json databaseId,headBranch,status
```

Take the run whose `headBranch` is `v0.4.25`, then `gh run watch <id> --exit-status`. Expected: success.

- [ ] **Step 3: Wait for Tugtainer, confirm the version**

Up to 15 min after the run. Then:

```bash
ssh latitude.gg.ez 'docker exec embedthat-worker-1 sh -c "grep ^version /app/pyproject.toml"'
```

Expected: `version = "0.4.25"`.

- [ ] **Step 4: Delete the old local video keys (count first)**

```bash
ssh latitude.gg.ez 'cd ~/my/vps/homeserver/embedthat && for p in "ytl:*" "dl2l:*" "ytl2:*" "dl2l2:*"; do echo "$p $(docker compose -f compose.prod.yml exec -T redis redis-cli --scan --pattern "$p" | wc -l)"; done'
```

Show the counts to the user. On a yes:

```bash
ssh latitude.gg.ez 'cd ~/my/vps/homeserver/embedthat && for p in "ytl:*" "dl2l:*"; do docker compose -f compose.prod.yml exec -T redis sh -c "redis-cli --scan --pattern \"$p\" | xargs -r -n 500 redis-cli del" ; done'
```

Rerun the count: `ytl:*` and `dl2l:*` are 0; `ytl2:*` / `dl2l2:*` untouched; the cloud `yt:*` / `dl2:*` untouched (count them before and after too).

- [ ] **Step 5: Production checks**

Ask the user to send the production bot the VK link and the 45-90 min YouTube link from Task 5. Then:

```bash
ssh latitude.gg.ez 'docker logs --since 30m embedthat-worker-1 2>&1 | grep -E "rung|selected|skipped|p for|split|TimeLimit|CRITICAL"'
```

Expected: VK at 720, YouTube at 720 with 1080 skipped without download, no split, no `TimeLimit`, no CRITICAL. Append the result to the spec's rehearsal section, commit (`spec: ladder in production`), push.
