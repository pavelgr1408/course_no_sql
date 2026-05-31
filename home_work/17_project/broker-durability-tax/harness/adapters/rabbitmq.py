"""
adapters/rabbitmq.py — тонкий адаптер по образцу kafka.py (контракт base.py).
Делает ТОЛЬКО подключение + publish/consume_one. Harness-логику не дублирует.

ОПЕРАЦИОННЫЕ ЗАМЕТКИ (грабли RabbitMQ + pika, уже обойдены здесь):
  * heartbeat=0 — В ПЛОТНОМ per-message цикле pika может не успеть обслужить heartbeat и
    брокер рвёт соединение («ConnectionClosed»). Для замерочного клиента heartbeat отключаем.
  * Quorum queue (T2) требует СФОРМИРОВАННЫЙ кластер из 3 нод (см. compose). Если кластер
    ещё собирается — declare упадёт; _connect() ретраит подключение до --startup-timeout-окна.
  * Очередь объявляется ПОД tier (classic/quorum, durable/transient). Между tier'ами кластер
    пересоздаётся (docker compose down -v && up), поэтому конфликта redeclare не будет.
  * Consumer: basic_consume + локальный буфер + process_data_events(time_limit) даёт честный
    блокирующий consume_one(timeout). ack — per-message на T1/T2; на T0 auto_ack (at-most-once).
  * Publisher confirms (T1/T2): confirm_delivery() + basic_publish блокирует до ack/nack
    КАЖДОГО сообщения. На T0 confirms выключены, delivery_mode=1 (transient).
"""
import time
import collections
import pika
from adapters.base import BrokerAdapter

HOSTS = ["rabbit1", "rabbit2", "rabbit3"]   # внутренние адреса docker-сети (НЕ localhost)
QUEUE = "bench"
CREDS = pika.PlainCredentials("guest", "guest")

# tier -> (durable_queue, quorum, delivery_mode, confirms)
TIER = {
    0: dict(durable=False, quorum=False, dm=1, confirms=False),
    1: dict(durable=True,  quorum=False, dm=2, confirms=True),
    2: dict(durable=True,  quorum=True,  dm=2, confirms=True),
}


def _connect():
    last = None
    deadline = time.time() + 90  # кластер/ноды могут ещё подниматься
    while time.time() < deadline:
        for h in HOSTS:
            try:
                params = pika.ConnectionParameters(
                    host=h, heartbeat=0, blocked_connection_timeout=300, credentials=CREDS)
                return pika.BlockingConnection(params)
            except Exception as e:
                last = e
        time.sleep(2)
    raise last


def _declare(ch, tier):
    t = TIER[tier]
    args = {"x-queue-type": "quorum"} if t["quorum"] else {}
    ch.queue_declare(queue=QUEUE, durable=t["durable"], arguments=args)


class Adapter(BrokerAdapter):
    name = "rabbitmq"

    def setup(self, tier):
        self._tier = tier
        conn = _connect()
        ch = conn.channel()
        _declare(ch, tier)
        conn.close()

    def connect_producer(self, tier):
        self._tier = tier
        self._pconn = _connect()
        self._pch = self._pconn.channel()
        _declare(self._pch, tier)
        if TIER[tier]["confirms"]:
            self._pch.confirm_delivery()   # с этого момента basic_publish блокирует до ack
        self._props = pika.BasicProperties(delivery_mode=TIER[tier]["dm"])

    def publish(self, payload: bytes):
        # тело = payload без изменений; default exchange, routing_key = имя очереди
        self._pch.basic_publish(exchange="", routing_key=QUEUE,
                                body=payload, properties=self._props)
        # при confirms=True (T1/T2) вызов вернётся только после ack брокера (per-message)

    def connect_consumer(self, tier):
        self._ctier = tier
        self._cconn = _connect()
        self._cch = self._cconn.channel()
        _declare(self._cch, tier)
        self._cch.basic_qos(prefetch_count=500)
        self._buf = collections.deque()

        def on_msg(ch, method, props, body):
            self._buf.append((method.delivery_tag, body))

        self._cch.basic_consume(QUEUE, on_message_callback=on_msg,
                                auto_ack=(tier == 0))   # T0 = at-most-once

    def consume_one(self, timeout: float):
        deadline = time.perf_counter() + timeout
        while not self._buf:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return None
            try:
                self._cconn.process_data_events(time_limit=remaining)  # качаем I/O до timeout
            except Exception:
                return None
        tag, body = self._buf.popleft()
        if self._ctier > 0:
            self._cch.basic_ack(tag)     # per-message ack (at-least-once)
        return body

    def teardown(self):
        for c in ("_pconn", "_cconn"):
            conn = getattr(self, c, None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
