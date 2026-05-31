"""
adapters/redis.py — тонкий адаптер по образцу kafka.py (контракт base.py).
Делает ТОЛЬКО подключение + publish/consume_one. Harness-логику не дублирует.

КРИТИЧНО ПРО ТОПОЛОГИЮ (отличие от черновика ТЗ):
  WAIT 2 ждёт подтверждения от ДВУХ РЕПЛИК ОДНОГО мастера. В схеме «3 master + 3 replica»
  у каждого мастера по ОДНОЙ реплике → WAIT 2 никогда не выполнится и будет висеть до таймаута.
  Поэтому для оси durability берём корректную схему: 1 master + 2 replica (обычная репликация,
  НЕ cluster mode). Это ровно «primary + 2 реплики» из определения T2 и честно даёт WAIT 2.
  3 ноды сохранены; смысл tier'а сохранён.

ОПЕРАЦИОННЫЕ ЗАМЕТКИ:
  * fsync-политика задаётся per-tier через CONFIG SET на мастере (см. setup()):
      T0 → appendfsync no (фактически без durability)
      T1 → appendfsync always (синхронный fsync на лидере; на Docker-macOS ЗАНИЖЕН — в отчёт!)
      T2 → appendfsync everysec + WAIT 2 (durability через 2 реплики)
  * WAIT даёт BEST-EFFORT подтверждение: репликация Redis АСИНХРОННА, это НЕ настоящий кворум
    как Raft (RabbitMQ) / ISR (Kafka). Качественное отличие — фиксируется в отчёте.
  * Redis T0 всё равно имеет 1 RTT (XADD возвращает id). Это честный «пол» in-memory подхода —
    не fire-and-forget уровня Kafka acks=0 / core NATS. Так и подаётся.
"""
import time
import redis
from adapters.base import BrokerAdapter

MASTER = "redis-master"   # внутренний адрес docker-сети (НЕ localhost)
PORT = 6379
STREAM = "bench"
GROUP = "bench-grp"
FIELD = b"d"              # одно поле фиксированного имени; значение = payload без обёрток
WAIT_REPLICAS = 2
WAIT_TIMEOUT_MS = 3000


def _client():
    deadline = time.time() + 90
    last = None
    while time.time() < deadline:
        try:
            r = redis.Redis(host=MASTER, port=PORT, socket_timeout=30)
            r.ping()
            return r
        except Exception as e:
            last = e
            time.sleep(2)
    raise last


class Adapter(BrokerAdapter):
    name = "redis"

    def setup(self, tier):
        self._tier = tier
        r = _client()
        # fsync-политика лидера под tier
        if tier == 0:
            try:
                r.config_set("appendonly", "no")
            except Exception:
                pass
            try:
                r.config_set("appendfsync", "no")
            except Exception:
                pass
        elif tier == 1:
            r.config_set("appendonly", "yes")
            r.config_set("appendfsync", "always")
        else:
            r.config_set("appendonly", "yes")
            r.config_set("appendfsync", "everysec")
        # consumer group (идемпотентно)
        try:
            r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
        r.close()

    def connect_producer(self, tier):
        self._tier = tier
        self._r = _client()

    def publish(self, payload: bytes):
        self._r.xadd(STREAM, {FIELD: payload})   # тело = payload без изменений
        if self._tier == 2:
            # ждём подтверждения 2 реплик ИМЕННО для актуального состояния (per-message)
            self._r.execute_command("WAIT", WAIT_REPLICAS, WAIT_TIMEOUT_MS)
        # T1: durability через appendfsync always на лидере (WAIT не нужен)
        # T0: ничего не ждём

    def connect_consumer(self, tier):
        self._tier = tier
        self._r = _client()
        self._consumer = "c1"

    def consume_one(self, timeout: float):
        block_ms = max(1, int(timeout * 1000))
        resp = self._r.xreadgroup(GROUP, self._consumer, {STREAM: ">"},
                                  count=1, block=block_ms)
        if not resp:
            return None
        _stream, entries = resp[0]
        if not entries:
            return None
        msg_id, fields = entries[0]
        if self._tier > 0:
            self._r.xack(STREAM, GROUP, msg_id)   # per-message ack
        return fields.get(FIELD)

    def teardown(self):
        try:
            self._r.close()
        except Exception:
            pass
