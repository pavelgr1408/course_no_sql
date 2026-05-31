"""
adapters/kafka.py — РЕФЕРЕНСНЫЙ адаптер (образец). Тонкий слой поверх контракта base.py.
Делает ТОЛЬКО: подключение по docker-сети, publish с блокировкой по tier (per-message),
consume с ack по tier. Payload/warmup/замер/перцентили — это harness, не здесь.

ОПЕРАЦИОННЫЕ ЗАМЕТКИ (почему так, а не иначе):
  * Кластер KRaft поднимается ~20–40 c. setup() поэтому РЕТРАИТ создание топика, а не падает.
  * subscribe() запускает rebalance группы: первые секунды poll() возвращает None ДО
    назначения партиций. Это НЕ ошибка и НЕ конец потока — выживание обеспечивает runner
    (он ждёт первое сообщение до --startup-timeout). Адаптер ничего особого тут не делает.
  * T1/T2: flush() после каждого produce = блокировка до ack ИМЕННО этого сообщения.
    Да, 150k flush медленно — это и есть честная цена гарантии, её и меряем.
  * commit офсета синхронно на каждое сообщение (T1/T2) — аналог per-message ack у остальных
    брокеров. На T0 (at-most-once) commit не делаем.
"""
import time
from confluent_kafka import Producer, Consumer, KafkaException
from confluent_kafka.admin import AdminClient, NewTopic
from adapters.base import BrokerAdapter

# Внутренние адреса docker-сети (НЕ localhost!)
BOOTSTRAP = "kafka-1:29092,kafka-2:29092,kafka-3:29092"
TOPIC = "bench"

# tier -> настройки producer. На T1/T2 publish блокируется per-message (см. publish()).
TIER_PRODUCER = {
    0: {"acks": "0", "linger.ms": 0},
    1: {"acks": "1", "linger.ms": 0},
    2: {"acks": "all", "linger.ms": 0},   # min.insync.replicas=2 задаётся на топике/брокере
}


class Adapter(BrokerAdapter):
    name = "kafka"

    def setup(self, tier):
        # Топология фиксирована: 3 партиции, RF=3 — одинаково для всех tier.
        admin = AdminClient({"bootstrap.servers": BOOTSTRAP})
        nt = NewTopic(TOPIC, num_partitions=3, replication_factor=3,
                      config={"min.insync.replicas": "2"})
        deadline = time.time() + 90  # кластер может ещё подниматься
        while True:
            try:
                fs = admin.create_topics([nt])
                for _, f in fs.items():
                    f.result()
                return
            except KafkaException as e:
                msg = str(e)
                if "TOPIC_ALREADY_EXISTS" in msg or "already exists" in msg:
                    return
                if time.time() > deadline:
                    raise
                time.sleep(2)  # брокеры/контроллер ещё не готовы — ретраим
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(2)

    def connect_producer(self, tier):
        self._tier = tier
        conf = {"bootstrap.servers": BOOTSTRAP, **TIER_PRODUCER[tier]}
        self._p = Producer(conf)

    def publish(self, payload: bytes):
        # payload передаётся БЕЗ изменений (без JSON, без key, без headers)
        if self._tier == 0:
            self._p.produce(TOPIC, value=payload)   # fire-and-forget
            self._p.poll(0)
        else:
            # T1/T2: блокируемся до ack ИМЕННО этого сообщения
            self._p.produce(TOPIC, value=payload)
            self._p.flush()                          # ждёт ack по acks=1 / acks=all

    def connect_consumer(self, tier):
        self._ctier = tier
        self._c = Consumer({
            "bootstrap.servers": BOOTSTRAP,
            "group.id": f"bench-t{tier}",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        })
        self._c.subscribe([TOPIC])

    def consume_one(self, timeout: float):
        msg = self._c.poll(timeout)
        if msg is None or msg.error():
            return None
        if self._ctier > 0:
            self._c.commit(msg, asynchronous=False)  # per-message ack (at-least-once)
        return msg.value()

    def teardown(self):
        try:
            self._p.flush()   # дослать «хвост» T0 (fire-and-forget) перед выходом
        except Exception:
            pass
        try:
            self._c.close()
        except Exception:
            pass
