"""
adapters/nats.py — тонкий адаптер по образцу kafka.py (контракт base.py).
Делает ТОЛЬКО подключение + publish/consume_one. Harness-логику не дублирует.

ГЛАВНАЯ ОСОБЕННОСТЬ: nats-py АСИНХРОННЫЙ, а harness СИНХРОННЫЙ. Поэтому здесь поднимается
ФОНОВЫЙ event loop в отдельной нити, а каждый publish/consume_one блокирующе ждёт результат
корутины через run_coroutine_threadsafe(...).result(). Так контракт остаётся синхронным и
per-message блокировка по tier сохраняется. Накладной расход моста мал на фоне сетевого RTT.

TIER:
  T0 = core NATS publish (без JetStream), fire-and-forget. Flush — один раз в teardown
       (per-message flush убил бы смысл T0). Это честный «пол», аналог Kafka acks=0.
  T1 = JetStream stream R=1, ждём PubAck (durability на одной ноде).
  T2 = JetStream stream R=3, ждём PubAck (Raft-репликация 3 нод) — настоящий кворум.

ВАЖНО: число реплик stream'а (R) нельзя менять на лету. Между T1 и T2 кластер пересоздаётся
(docker compose down -v && up) — это и так требование методики (холодный старт между прогонами).
"""
import asyncio
import threading
import nats
from nats.js.api import StreamConfig
from adapters.base import BrokerAdapter

SERVERS = ["nats://nats1:4222", "nats://nats2:4222", "nats://nats3:4222"]  # docker-сеть
STREAM = "BENCH"
SUBJECT = "bench.msg"
DURABLE = "bench-cons"


class _Loop:
    """Фоновый asyncio-loop в отдельной нити + блокирующий вызов корутин из синхронного кода."""
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thr = threading.Thread(target=self._run, daemon=True)
        self.thr.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro, timeout=None):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout)


class Adapter(BrokerAdapter):
    name = "nats"

    def __init__(self):
        self._lp = _Loop()

    def setup(self, tier):
        self._tier = tier

        async def _s():
            nc = await nats.connect(servers=SERVERS, connect_timeout=10, max_reconnect_attempts=30)
            if tier > 0:
                js = nc.jetstream()
                replicas = 1 if tier == 1 else 3
                try:
                    await js.add_stream(StreamConfig(
                        name=STREAM, subjects=[SUBJECT], num_replicas=replicas))
                except Exception:
                    pass  # stream уже есть (R сменить нельзя — для смены нужен холодный старт)
            await nc.close()

        self._lp.run(_s(), timeout=90)

    def connect_producer(self, tier):
        self._tier = tier

        async def _c():
            self._nc = await nats.connect(servers=SERVERS, connect_timeout=10,
                                          max_reconnect_attempts=30)
            self._js = self._nc.jetstream() if tier > 0 else None

        self._lp.run(_c(), timeout=30)

    def publish(self, payload: bytes):
        if self._tier == 0:
            async def _p():
                await self._nc.publish(SUBJECT, payload)   # fire-and-forget
            self._lp.run(_p(), timeout=30)
        else:
            async def _p():
                await self._js.publish(SUBJECT, payload)   # ждём PubAck (per-message)
            self._lp.run(_p(), timeout=30)

    def connect_consumer(self, tier):
        self._tier = tier

        async def _c():
            self._nc = await nats.connect(servers=SERVERS, connect_timeout=10,
                                          max_reconnect_attempts=30)
            if tier == 0:
                self._sub = await self._nc.subscribe(SUBJECT)   # core NATS
            else:
                self._js = self._nc.jetstream()
                self._psub = await self._js.pull_subscribe(SUBJECT, durable=DURABLE)

        self._lp.run(_c(), timeout=30)

    def consume_one(self, timeout: float):
        if self._tier == 0:
            async def _g():
                try:
                    m = await self._sub.next_msg(timeout=timeout)
                    return m.data
                except Exception:
                    return None
            return self._lp.run(_g(), timeout=timeout + 5)
        else:
            async def _g():
                try:
                    msgs = await self._psub.fetch(1, timeout=timeout)
                except Exception:
                    return None
                if not msgs:
                    return None
                m = msgs[0]
                await m.ack()              # per-message ack
                return m.data
            return self._lp.run(_g(), timeout=timeout + 5)

    def teardown(self):
        async def _t():
            try:
                await self._nc.drain()     # дослать «хвост» T0 + аккуратно закрыть
            except Exception:
                pass
        try:
            self._lp.run(_t(), timeout=30)
        except Exception:
            pass
