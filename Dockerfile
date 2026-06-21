ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        sqlite3 \
        postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# Override with --build-arg PIP_INDEX_URL=... if you use a private PyPI mirror.
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir -r requirements.txt --index-url "${PIP_INDEX_URL}"

COPY migrate_sqlite_to_pg.py openwebui-migrate.sh ./
RUN chmod +x openwebui-migrate.sh migrate_sqlite_to_pg.py

ENV SOURCE_SQLITE=/data/webui.db \
    WORK_DIR=/data/work \
    CLEAN_SQLITE=/data/work/webui-clean.db \
    PG_CONTAINER= \
    BATCH_SIZE=5000 \
    MIGRATION_TOOL=python

ENTRYPOINT ["/app/openwebui-migrate.sh"]
CMD ["help"]
