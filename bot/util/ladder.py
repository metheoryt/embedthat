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
