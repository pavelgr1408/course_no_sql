"""
metrics.py — ЭТАЛОННЫЙ модуль. НЕ РЕДАКТИРОВАТЬ при работе над отдельным брокером.
Здесь зафиксировано всё, что должно быть ОДИНАКОВЫМ для всех четырёх брокеров:
генерация payload, отбрасывание warmup, расчёт перцентилей и throughput, формат вывода.

Брокеро-специфичной логики тут нет и быть не должно. Она — только в adapters/<broker>.py.
"""
import time
import statistics

# ---- ЗАФИКСИРОВАННЫЕ КОНСТАНТЫ ЭКСПЕРИМЕНТА (одинаковы для всех брокеров) ----
DEFAULT_COUNT = 200_000      # всего сообщений за прогон
DEFAULT_WARMUP = 50_000      # первые N — разогрев, в статистику НЕ идут
DEFAULT_SIZE = 1024          # размер payload в БАЙТАХ (ровно столько на проводе как тело)


def make_payload(size_bytes: int, seq: int) -> bytes:
    """
    Единый payload для ВСЕХ брокеров: ровно size_bytes байт.
    Первые 16 байт — порядковый номер (для проверки целостности/порядка),
    остальное — детерминированный наполнитель. Никакого JSON, никаких заголовков.
    Это гарантирует, что 'на проводе' у всех брокеров одинаковое тело одинакового размера.
    """
    prefix = f"{seq:016d}".encode("ascii")        # ровно 16 байт
    if size_bytes < len(prefix):
        return prefix[:size_bytes]
    filler = b"x" * (size_bytes - len(prefix))
    return prefix + filler


class LatencyCollector:
    """
    Единый сборщик. Адаптер вызывает .record(t) на КАЖДУЮ операцию (publish или consume).
    Модуль сам отбрасывает warmup и считает перцентили — адаптер в это не вмешивается.
    """
    def __init__(self, warmup: int = DEFAULT_WARMUP):
        self.warmup = warmup
        self._lat_ms = []     # latency каждой измеряемой операции, в миллисекундах
        self._n_seen = 0
        self._t_start = None  # perf_counter в момент первой ИЗМЕРЯЕМОЙ операции
        self._t_end = None

    def record(self, latency_seconds: float):
        self._n_seen += 1
        if self._n_seen <= self.warmup:
            return  # warmup — игнорируем полностью
        if self._t_start is None:
            self._t_start = time.perf_counter()
        self._lat_ms.append(latency_seconds * 1000.0)
        self._t_end = time.perf_counter()

    def result(self) -> dict:
        if not self._lat_ms:
            raise RuntimeError("Нет измеренных операций после warmup — проверь count > warmup "
                               "(для T0 с потерями уменьши --warmup)")
        s = sorted(self._lat_ms)
        n = len(s)
        measured_seconds = (self._t_end - self._t_start) if self._t_start else 0.0
        throughput = n / measured_seconds if measured_seconds > 0 else 0.0
        return {
            "measured_messages": n,
            "throughput_msg_s": round(throughput, 1),
            "p50_ms": round(s[int(n * 0.50)], 3),
            "p95_ms": round(s[int(n * 0.95)], 3),
            "p99_ms": round(s[min(int(n * 0.99), n - 1)], 3),
            "avg_ms": round(statistics.mean(s), 3),
            "min_ms": round(s[0], 3),
            "max_ms": round(s[-1], 3),
        }


def median_of_runs(run_results: list) -> dict:
    """Медиана по 3–5 повторам одного и того же прогона + разброс (min–max)."""
    def med(key):
        vals = [r[key] for r in run_results]
        return round(statistics.median(vals), 3)
    def spread(key):
        vals = [r[key] for r in run_results]
        return [round(min(vals), 3), round(max(vals), 3)]
    keys = ["throughput_msg_s", "p50_ms", "p95_ms", "p99_ms", "avg_ms"]
    return {
        "runs": len(run_results),
        **{f"{k}_median": med(k) for k in keys},
        **{f"{k}_minmax": spread(k) for k in keys},
    }


def print_result(broker: str, tier: int, role: str, res: dict):
    """Единый формат вывода — одинаковый для всех брокеров."""
    print(f"\n=== {broker.upper()} | tier T{tier} | {role} ===")
    for k, v in res.items():
        print(f"  {k:22}: {v}")


def emit_csv_row(broker: str, tier: int, run: int, role: str, res: dict) -> str:
    """Строка для results.csv — машиночитаемо, единый формат."""
    return (f"{broker},T{tier},{run},{role},"
            f"{res['throughput_msg_s']},{res['p50_ms']},{res['p95_ms']},{res['p99_ms']}")
