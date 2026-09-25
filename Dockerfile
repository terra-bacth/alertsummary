# Multi-stage build: the React dashboard is compiled in a Node stage and served by the API
# process, so one image / one Deployment / one Route carries both.
#
# Every external location is a build arg, so disconnected clusters can point at a mirror
# without editing this file:
#   podman build \
#     --build-arg PYTHON_IMAGE=mirror.example.com/ubi9/python-311:latest \
#     --build-arg NODE_IMAGE=mirror.example.com/ubi9/nodejs-20:latest \
#     --build-arg PIP_INDEX_URL=https://nexus.example.com/repository/pypi/simple \
#     --build-arg NPM_REGISTRY=https://nexus.example.com/repository/npm/ \
#     -t tsagent:0.1.0 .
# The same args are exposed by the chart's BuildConfig (build.args).

ARG PYTHON_IMAGE=registry.access.redhat.com/ubi9/python-311:latest
ARG NODE_IMAGE=registry.access.redhat.com/ubi9/nodejs-20:latest

# ---------------------------------------------------------------------------
# Stage 1: dashboard
# ---------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS ui
ARG NPM_REGISTRY=""
USER 0
WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN if [ -n "$NPM_REGISTRY" ]; then npm config set registry "$NPM_REGISTRY"; fi \
    && npm ci --no-audit --no-fund
COPY web/ ./
# Same-origin by default: the UI calls /api/... on whatever host served it.
ARG VITE_TSAGENT_API=""
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: runtime (UBI9 Python 3.11 — supported on OpenShift)
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE}
ARG PIP_INDEX_URL=""
ARG PIP_TRUSTED_HOST=""
ARG VERSION=0.1.0

LABEL name="tsagent" \
      version="${VERSION}" \
      summary="AlertSummarizationAgent — multi-agent RHOSP alarm troubleshooting" \
      io.k8s.display-name="tsagent" \
      io.k8s.description="AlertManager webhook -> correlation -> PromQL/SSH investigation -> RCA report" \
      io.openshift.expose-services="8080:http" \
      io.openshift.tags="rhosp,alertmanager,sre,llm"

# OpenShift's restricted-v2 SCC runs the container as an arbitrary UID in group 0. Nothing below
# may assume a fixed user id; every path the app writes to must be group-writable.
USER 0

WORKDIR /opt/app-root/src

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN if [ -z "$PIP_INDEX_URL" ]; then unset PIP_INDEX_URL; fi \
    && if [ -z "$PIP_TRUSTED_HOST" ]; then unset PIP_TRUSTED_HOST; fi \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ".[agents]" \
    && rm -rf /root/.cache

# Defaults baked in as a fallback only; on OpenShift the ConfigMaps mounted at
# /etc/tsagent/config take over (TSAGENT_CONFIG_DIR). Private keys are excluded by .dockerignore.
COPY config/ ./config/
COPY --from=ui /build/dist ./web/dist

# /opt/app-root/data is the PVC mount point. Group-write permissions are what make the arbitrary
# UID able to open the SQLite files.
RUN mkdir -p /opt/app-root/data \
    && chgrp -R 0 /opt/app-root \
    && chmod -R g=u /opt/app-root

# Every TSAGENT_* value below is only a default — override any of them from the Deployment
# (the chart's `env:` block renders them into a ConfigMap).
#  - XDG_CACHE_HOME: the root filesystem is read-only on OpenShift; caches go to the /tmp emptyDir.
#  - LITELLM_LOCAL_MODEL_COST_MAP: LiteLLM otherwise fetches its price map from GitHub at import
#    time, which the egress NetworkPolicy blocks and a disconnected cluster cannot reach anyway.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TSAGENT_CONFIG_DIR=/opt/app-root/src/config \
    TSAGENT_DB_PATH=/opt/app-root/data/cases.db \
    TSAGENT_CHECKPOINT_PATH=/opt/app-root/data/checkpoints.db \
    TSAGENT_UI_DIR=/opt/app-root/src/web/dist \
    TSAGENT_HOST=:: \
    TSAGENT_PORT=8080 \
    XDG_CACHE_HOME=/tmp/.cache \
    LITELLM_LOCAL_MODEL_COST_MAP=True \
    LITELLM_TELEMETRY=False

USER 1001
EXPOSE 8080

CMD ["python", "-m", "tsagent"]
