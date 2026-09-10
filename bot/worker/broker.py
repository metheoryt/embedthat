import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import CurrentMessage

from bot.config import settings

broker = RedisBroker(url=str(settings.redis_dsn))
# lets actor code see which attempt it is on -- see `_is_final_attempt` in actors.py
broker.add_middleware(CurrentMessage())
dramatiq.set_broker(broker)
