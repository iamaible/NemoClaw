# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# NemoClaw Python SDK image.
#
# Build context must be the workspace root so both packages are reachable:
#
#   docker build -f NemoClaw/python/Dockerfile \
#     -t ghcr.io/nvidia/nemoclaw/python-sdk:latest .
#
# For CI where openshell is already published to an internal registry, pass:
#
#   --build-arg OPENSHELL_INDEX_URL=https://pypi.internal/simple
#
# Stages
# ------
#   builder  — installs all Python deps into /opt/venv via uv
#   runtime  — minimal python:3.12-slim image with the venv copied in

ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.6.14

# ── Stage 1: Build ────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS builder

ARG UV_VERSION
# Optional: override to pull openshell from an internal PyPI index instead of
# the local monorepo source copy.
ARG OPENSHELL_INDEX_URL=""

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install uv==${UV_VERSION}

WORKDIR /build

# Copy openshell SDK source (local monorepo dependency).
# When OPENSHELL_INDEX_URL is set the build installs from that index instead;
# the COPY is still needed for the editable-install path resolution in
# pyproject.toml, but uv will prefer the index.
COPY OpenShell/python/ /build/openshell/

# Copy nemoclaw package manifests first so Docker cache is reused when only
# source files change.
COPY NemoClaw/python/pyproject.toml NemoClaw/python/uv.lock NemoClaw/python/README.md /build/nemoclaw/
COPY NemoClaw/python/nemoclaw/ /build/nemoclaw/nemoclaw/

# Create a venv and install all dependencies.
RUN uv venv /opt/venv

# pyproject.toml carries a [tool.uv.sources] entry with the monorepo-relative
# path "../../Openshell/python".  Inside the container that resolves to
# /Openshell/python which doesn't exist, so rewrite it to the container path
# before uv reads it.  The OPENSHELL_INDEX_URL branch skips this path entirely.
RUN sed -i 's|path = "../../Openshell/python"|path = "/build/openshell"|' \
    /build/nemoclaw/pyproject.toml

RUN if [ -n "${OPENSHELL_INDEX_URL}" ]; then \
        VIRTUAL_ENV=/opt/venv uv pip install \
            --no-cache \
            --extra-index-url "${OPENSHELL_INDEX_URL}" \
            --directory /build/nemoclaw \
            .; \
    else \
        VIRTUAL_ENV=/opt/venv uv pip install \
            --no-cache \
            --directory /build/nemoclaw \
            .; \
    fi

# ── Stage 2: Runtime ─────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim

LABEL org.opencontainers.image.title="NemoClaw Python SDK" \
      org.opencontainers.image.description="Blueprint orchestration and sandbox management for Kubernetes." \
      org.opencontainers.image.vendor="NVIDIA Corporation" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/NVIDIA/NemoClaw"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    VIRTUAL_ENV="/opt/venv"

# Non-root user: uid 1000 matches the default runAsUser in the Helm chart.
RUN useradd -r -u 1000 -g users -d /var/nemoclaw -s /sbin/nologin nemoclaw

# Copy installed venv from builder — no pip or build tools in the final image.
COPY --from=builder /opt/venv /opt/venv

# State directory: snapshots and the sandbox registry live here.
# The Helm chart mounts a PVC at this path; the emptyDir fallback works for
# ephemeral / read-only use cases.
RUN mkdir -p /var/nemoclaw/.nemoclaw/snapshots && \
    chown -R nemoclaw:users /var/nemoclaw

USER 1000
WORKDIR /var/nemoclaw

# `python -m nemoclaw` prints version info and exits 0.
# Override CMD in the Helm chart for long-running controller workloads.
CMD ["python", "-m", "nemoclaw"]
