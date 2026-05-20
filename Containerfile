FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc python3-dev libqpid-proton-cpp12-dev \
    && pip install --no-cache-dir python-qpid-proton psycopg2-binary \
    && apt-get purge -y gcc python3-dev libqpid-proton-cpp12-dev \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY signal_logger.py .

ENV PYTHONUNBUFFERED=1

# Env vars for PostgreSQL backend (override at runtime or via k8s Secret)
ENV PG_HOST=""
ENV PG_PORT="5432"
ENV PG_DB="sdr_scanner"
ENV PG_USER="sdr"
ENV PG_PASS=""

ENTRYPOINT ["python3", "signal_logger.py"]
CMD ["--broker", "amqp://activemq-service.sdr-system.svc.cluster.local:5672"]
