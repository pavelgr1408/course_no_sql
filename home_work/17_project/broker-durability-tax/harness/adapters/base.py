"""
base.py — КОНТРАКТ адаптера. НЕ РЕДАКТИРОВАТЬ.
Каждый брокер реализует подкласс BrokerAdapter в adapters/<broker>.py.
Адаптер реализует ТОЛЬКО подключение и операции publish/consume.
Всё остальное (payload, warmup, замер, перцентили, УСЛОВИЕ ОСТАНОВКИ) делает harness —
адаптер туда не лезет.

ЖЁСТКИЕ ИНВАРИАНТЫ, обязательные для ЛЮБОГО адаптера:
  1. publish(payload) на T1/T2 ДОЛЖЕН блокироваться до подтверждения гарантии этого tier
     ПО КАЖДОМУ СООБЩЕНИЮ (не батчем). На T0 — fire-and-forget без ожидания.
  2. publish принимает РОВНО те bytes, что дал harness. Запрещено: оборачивать в JSON,
     добавлять поля, менять кодировку, добавлять заголовки/ключи сверх требуемых tier.
  3. Время операции меряет harness вокруг вызова publish()/consume_one(). Адаптер сам
     время НЕ считает и перцентили НЕ считает.
  4. Топология фиксирована и одинакова: 3 ноды, RF=3, кворум=2. Адаптер не меняет её между tier.
     (Для NATS физических нод тоже 3; параметр, который меняется по tier, — число реплик
     stream'а R=1/R=3 — это аналог acks/quorum, а не изменение числа нод.)
  5. Подключение — по ВНУТРЕННЕМУ адресу docker-сети, не через localhost.
  6. consume_one(timeout) — это таймаут ОДНОГО poll, а НЕ сигнал «поток кончился».
     Если за timeout сообщения нет — вернуть None. Решение «прогон закончен» принимает
     ТОЛЬКО harness (по startup-/idle-таймауту). Адаптер не должен сам решать остановку.
"""
from abc import ABC, abstractmethod


class BrokerAdapter(ABC):
    name = "abstract"

    @abstractmethod
    def setup(self, tier: int):
        """Создать топик/очередь/stream под нужный tier. Топология при этом не меняется."""

    @abstractmethod
    def connect_producer(self, tier: int):
        """Подключить producer в режиме подтверждения, СООТВЕТСТВУЮЩЕМ tier."""

    @abstractmethod
    def publish(self, payload: bytes):
        """
        Отправить ОДНО сообщение = payload (ровно эти bytes).
        T0: вернуться сразу (fire-and-forget).
        T1/T2: ВЕРНУТЬСЯ ТОЛЬКО ПОСЛЕ подтверждения гарантии tier для ЭТОГО сообщения.
        Ничего не возвращает. Не батчить.
        """

    @abstractmethod
    def connect_consumer(self, tier: int):
        """Подписаться / назначить партиции. Вызывается ДО старта producer."""

    @abstractmethod
    def consume_one(self, timeout: float):
        """
        Получить ОДНО сообщение и подтвердить (ack) по семантике tier.
        Вернуть payload (bytes) или None по таймауту ОДНОГО poll.
        """

    @abstractmethod
    def teardown(self):
        """Очистить ресурсы между прогонами."""
