FROM python:3.12-slim

# Системные зависимости минимальны: confluent-kafka и redis ставятся колёсами (wheels),
# librdkafka уже внутри колеса confluent-kafka для arm64/amd64.
RUN pip install --no-cache-dir \
        "confluent-kafka==2.5.3" \
        "pika==1.3.2" \
        "redis==5.0.8" \
        "nats-py==2.9.0"

WORKDIR /app
# Код harness монтируется томом из ./harness (см. compose) — образ пересобирать не нужно
# при правке адаптера. Клиент просто «живёт» в сети брокера и ждёт docker compose exec.
CMD ["sleep", "infinity"]
