# UBI9 Python 3.11 — supported base for OpenShift, and already trusted in most RHOSP estates.
FROM registry.access.redhat.com/ubi9/python-311:latest

# OpenShift's restricted-v2 SCC runs the container as an arbitrary UID in group 0. Nothing below
# may assume a fixed user id; every path the app writes to must be group-writable.
USER 0

WORKDIR /opt/app-root/src

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ".[agents]" \
    && rm -rf /root/.cache

# Config is expected to arrive from ConfigMaps; these are the defaults baked in as a fallback.
COPY config/ ./config/

# /opt/app-root/data is the PVC mount point. Group-write permissions are what make the arbitrary
# UID able to open the SQLite files.
RUN mkdir -p /opt/app-root/data \
    && chgrp -R 0 /opt/app-root \
    && chmod -R g=u /opt/app-root

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TSAGENT_CONFIG_DIR=/opt/app-root/src/config \
    TSAGENT_DB_PATH=/opt/app-root/data/cases.db \
    TSAGENT_CHECKPOINT_PATH=/opt/app-root/data/checkpoints.db

USER 1001
EXPOSE 8080

CMD ["uvicorn", "tsagent.main:app", "--host", "::", "--port", "8080"]
