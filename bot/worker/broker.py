import dramatiq
import redis
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import CurrentMessage
from redis.backoff import ExponentialBackoff
from redis.retry import Retry

from bot.config import settings

# The client is built here rather than passed as `url=`: dramatiq turns a url
# into a bare `ConnectionPool.from_url(url)` and redis-py ignores every
# connection kwarg once a pool is supplied, so `health_check_interval` and
# `retry` only take effect on a client we construct ourselves. Without them a
# pooled connection that outlived a redis restart raises `Broken pipe` on the
# next enqueue (the default is zero retries) and the job is lost -- one such
# message was dropped on 2026-09-26, when `embedthat-redis-1` was recreated
# under a bot container that kept running.
broker = RedisBroker(
    client=redis.Redis.from_url(
        str(settings.redis_dsn),
        health_check_interval=30,
        retry=Retry(ExponentialBackoff(cap=1, base=0.1), 3),
    )
)
# lets actor code see which attempt it is on -- see `_is_final_attempt` in actors.py
broker.add_middleware(CurrentMessage())
dramatiq.set_broker(broker)
