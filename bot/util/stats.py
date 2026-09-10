import asyncio
from datetime import date, timedelta
from typing import TypedDict

from bot.config import settings
from bot.util.redis import redis_client
from bot.worker.broker import broker as dramatiq_broker


class QueueStats(TypedDict):
    pending: int
    failed: int


class PeriodStats(TypedDict):
    requests: int
    success: int
    fail: int
    unique_users: int
    private: int
    groups: int
    platforms: dict[str, int]
    langs: dict[str, int]


async def _queue_stats() -> QueueStats:
    namespace = dramatiq_broker.namespace
    pending = await redis_client.hlen(f"{namespace}:default.msgs")
    failed = await redis_client.zcard(f"{namespace}:default.XQ")
    return {"pending": pending, "failed": failed}


async def _period_stats(dates: list[str]) -> PeriodStats:
    all_success_keys: list[str] = []
    all_lang_keys: list[str] = []
    for d in dates:
        all_success_keys.extend(await redis_client.keys(f"stats:{d}:success:*"))
        all_lang_keys.extend(await redis_client.keys(f"stats:{d}:lang:*"))
    platforms = sorted({k.split(":")[-1] for k in all_success_keys})
    langs = sorted({k.split(":")[-1] for k in all_lang_keys})

    counter_keys: list[str] = []
    for d in dates:
        counter_keys += [
            f"stats:{d}:requests",
            f"stats:{d}:fail:youtube",
            f"stats:{d}:fail:social",
            f"stats:{d}:chat:private",
            f"stats:{d}:chat:group",
            f"stats:{d}:chat:supergroup",
            f"stats:{d}:chat:channel",
        ]
        for platform in platforms:
            counter_keys.append(f"stats:{d}:success:{platform}")
        for lang in langs:
            counter_keys.append(f"stats:{d}:lang:{lang}")

    counter_map: dict[str, int] = {}
    if counter_keys:
        values = await redis_client.mget(counter_keys)
        counter_map = {k: int(v or 0) for k, v in zip(counter_keys, values)}

    user_ids: set[str] = set()
    for d in dates:
        members = await redis_client.smembers(f"stats:{d}:users")
        user_ids |= members

    def s(key_suffix: str) -> int:
        return sum(counter_map.get(f"stats:{d}:{key_suffix}", 0) for d in dates)

    platform_counts = {p: s(f"success:{p}") for p in platforms}
    lang_counts = {l: s(f"lang:{l}") for l in langs}

    return {
        "requests": s("requests"),
        "success": sum(platform_counts.values()),
        "fail": s("fail:youtube") + s("fail:social"),
        "unique_users": len(user_ids),
        "private": s("chat:private"),
        "groups": s("chat:group") + s("chat:supergroup") + s("chat:channel"),
        "platforms": platform_counts,
        "langs": lang_counts,
    }


def _date_range(start: date, end: date) -> list[str]:
    result: list[str] = []
    d = start
    while d <= end:
        result.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return result


def _fmt_section(title: str, stats: PeriodStats) -> str:
    lines = [
        title,
        f"  Requests:     {stats['requests']}  (✓ {stats['success']}  ✗ {stats['fail']})",
        f"  Unique users: {stats['unique_users']}",
        f"  Private: {stats['private']} | Groups: {stats['groups']}",
    ]
    if stats["platforms"]:
        lines.append("  " + " | ".join(
            f"{p.capitalize()}: {c}"
            for p, c in sorted(stats["platforms"].items(), key=lambda x: -x[1])
        ))
    if stats["langs"]:
        lines.append("  " + " | ".join(
            f"{l}: {c}"
            for l, c in sorted(stats["langs"].items(), key=lambda x: -x[1])
        ))
    return "\n".join(lines)


async def build_stats_report() -> str:
    today = settings.now().date()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    today_stats, week_stats, month_stats, queue_stats = await asyncio.gather(
        _period_stats([today.strftime("%Y-%m-%d")]),
        _period_stats(_date_range(week_start, today)),
        _period_stats(_date_range(month_start, today)),
        _queue_stats(),
    )

    today_label = f"Today ({today.strftime('%b %-d')})"
    week_label = f"This week ({week_start.strftime('%b %-d')}–{today.strftime('%b %-d')})"
    month_label = f"This month ({today.strftime('%B')})"

    return "\n".join([
        "📊 Stats",
        "",
        _fmt_section(today_label, today_stats),
        "",
        _fmt_section(week_label, week_stats),
        "",
        _fmt_section(month_label, month_stats),
        "",
        f"🔧 Queue: pending {queue_stats['pending']} | failed {queue_stats['failed']}",
    ])
