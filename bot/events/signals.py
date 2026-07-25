from collections.abc import Callable
from typing import Any, TypeVar

from aiosignal import Signal

F = TypeVar("F", bound=Callable[..., Any])


def signal_handler(signal: Signal) -> Callable[[F], F]:
    def decorator(func: F) -> F:
        signal.append(func)
        return func

    return decorator


def freeze_signals() -> None:
    for sig in [
        on_link_received, on_link_sent,
        on_yt_video_sent, on_yt_video_fail,
        on_social_video_sent, on_social_video_fail,
    ]:
        sig.freeze()


on_yt_video_sent = Signal(
    "on_yt_video_sent(link: str, chat_id: int, chat_type: str, bot: Bot, video: YouTubeVideoData, fresh: bool)"
)
on_yt_video_fail = Signal(
    "on_yt_video_fail(link: str)"
)
on_social_video_sent = Signal(
    "on_social_video_sent(link: str, chat_id: int, chat_type: str, bot: Bot, video: SocialVideoData, fresh: bool)"
)
on_social_video_fail = Signal(
    "on_social_video_fail(link: str)"
)
on_link_sent = Signal("on_link_sent(link: str, message: Message, origin: LinkOrigin)")
on_link_received = Signal("on_link_received(message: Message, origin: LinkOrigin)")
