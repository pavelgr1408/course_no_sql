"""
runner.py — ЭТАЛОННЫЙ оркестратор прогона. РЕДАКТИРОВАТЬ ЗАПРЕЩЕНО при работе над отдельным брокером.
Запускает producer и consumer для ОДНОГО брокера на ОДНОМ tier по единым правилам:
  - payload одинаковый (metrics.make_payload),
  - warmup-срез одинаковый,
  - замер времени вокруг publish/consume_one делает ЗДЕСЬ harness, а не адаптер,
  - перцентили и throughput считает metrics.py.

═══════════════════════════════════════════════════════════════════════════════
ЧТО ИЗМЕНЕНО ОТНОСИТЕЛЬНО ПЕРВОЙ ВЕРСИИ (и ПОЧЕМУ это правильно для всех брокеров)
───────────────────────────────────────────────────────────────────────────────
В первой версии consumer делал `break` на ПЕРВОМ пустом poll с таймаутом 5 с.
Это ломалось у каждого брокера, но по-разному:
  - Kafka: subscribe запускает rebalance группы; первые 1–10 с poll() возвращает None
           ещё ДО того, как назначены партиции → consumer выходил с нулём сообщений.
  - NATS/JetStream, RabbitMQ, Redis: если producer стартовал на пару секунд позже
           consumer'а (а он и должен стартовать позже), первый пустой poll убивал прогон.
Это и есть «5 секунд, не успеваю запустить producer».

Теперь поведение consumer'а ОДИНАКОВО для всех брокеров и устойчиво к локальному лагу:
  1. POLL_TIMEOUT (короткий, 1 c) — это таймаут ОДНОГО poll, НЕ условие остановки.
  2. До первого полученного сообщения consumer ждёт producer'а до --startup-timeout (по
     умолчанию 120 c). Пустой poll в этой фазе = «ещё не началось», а НЕ «поток кончился».
  3. После первого сообщения прогон завершается, если новых нет дольше --idle-timeout
     (по умолчанию 15 c) ИЛИ получено count сообщений.
Так измерение steady-state не зависит от того, на сколько секунд разъехались два терминала.
═══════════════════════════════════════════════════════════════════════════════

Использование (ВСЕГДА из контейнера client внутри docker-сети брокера, два терминала):
  # терминал 1 — consumer ПЕРВЫМ:
  python runner.py --broker kafka --tier 2 --role consumer
  # терминал 2 — producer:
  python runner.py --broker kafka --tier 2 --role producer
Адаптер выбирается по --broker из adapters/.
"""
import argparse
import time
import importlib

from metrics import (make_payload, LatencyCollector, print_result,
                     DEFAULT_COUNT, DEFAULT_WARMUP, DEFAULT_SIZE)

# Короткий poll одного вызова consume_one. НЕ условие остановки прогона.
POLL_TIMEOUT = 1.0
# Сколько ждём ПЕРВОЕ сообщение (брокер поднимается, Kafka делает rebalance,
# producer стартует во втором терминале с задержкой). Это НЕ конец потока.
DEFAULT_STARTUP_TIMEOUT = 120.0
# Пауза без новых сообщений ПОСЛЕ начала потока = поток закончился.
DEFAULT_IDLE_TIMEOUT = 15.0


def load_adapter(broker: str):
    mod = importlib.import_module(f"adapters.{broker}")
    return mod.Adapter()  # каждый adapters/<broker>.py определяет класс Adapter(BrokerAdapter)


def run_producer(adapter, tier, count, warmup, size):
    adapter.setup(tier)
    adapter.connect_producer(tier)
    coll = LatencyCollector(warmup=warmup)
    for seq in range(count):
        payload = make_payload(size, seq)
        t0 = time.perf_counter()
        adapter.publish(payload)           # на T1/T2 блокируется до ack — это в адаптере
        coll.record(time.perf_counter() - t0)
    res = coll.result()
    print_result(adapter.name, tier, "producer", res)
    adapter.teardown()
    return res


def run_consumer(adapter, tier, count, warmup, startup_timeout, idle_timeout):
    adapter.setup(tier)
    adapter.connect_consumer(tier)         # ВАЖНО: consumer стартует ПЕРВЫМ (до producer)
    coll = LatencyCollector(warmup=warmup)
    got = 0
    started = False
    t_wait_start = time.perf_counter()
    t_last_msg = None
    while got < count:
        t0 = time.perf_counter()
        msg = adapter.consume_one(timeout=POLL_TIMEOUT)
        dt = time.perf_counter() - t0
        now = time.perf_counter()
        if msg is None:
            if not started:
                # ещё не получили НИ ОДНОГО сообщения — ждём producer'а, это не конец
                if now - t_wait_start > startup_timeout:
                    print(f"[consumer] producer не появился за {startup_timeout:.0f}s — стоп")
                    break
                continue
            # поток уже шёл и прервался — ждём idle_timeout, прежде чем признать конец
            if now - t_last_msg > idle_timeout:
                break
            continue
        if not started:
            started = True
        t_last_msg = now
        coll.record(dt)
        got += 1
    res = coll.result()
    print_result(adapter.name, tier, "consumer", res)
    adapter.teardown()
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--broker", required=True, choices=["kafka", "rabbitmq", "redis", "nats"])
    ap.add_argument("--tier", required=True, type=int, choices=[0, 1, 2])
    ap.add_argument("--role", required=True, choices=["producer", "consumer"])
    ap.add_argument("--count", type=int, default=DEFAULT_COUNT)
    ap.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    ap.add_argument("--size", type=int, default=DEFAULT_SIZE)
    ap.add_argument("--startup-timeout", type=float, default=DEFAULT_STARTUP_TIMEOUT,
                    help="сек: сколько consumer ждёт ПЕРВОЕ сообщение (не конец потока)")
    ap.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT,
                    help="сек: пауза без новых сообщений = конец потока")
    args = ap.parse_args()

    adapter = load_adapter(args.broker)
    if args.role == "producer":
        run_producer(adapter, args.tier, args.count, args.warmup, args.size)
    else:
        run_consumer(adapter, args.tier, args.count, args.warmup,
                     args.startup_timeout, args.idle_timeout)


if __name__ == "__main__":
    main()
