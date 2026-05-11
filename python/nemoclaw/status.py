# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
StatusAggregator: multi-source status for a NemoClaw sandbox.

Sources
-------
1. Registry  — local ``~/.nemoclaw/sandboxes.json`` via :mod:`nemoclaw.registry`
2. Gateway   — live gRPC probe via ``openshell.SandboxClient``
3. Inference — HTTP reachability probe to the provider's health endpoint
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from nemoclaw import registry as _registry

if TYPE_CHECKING:
    from openshell.sandbox import SandboxClient


# ---------------------------------------------------------------------------
# Phase constants (mirrors openshell SandboxPhase proto enum)
# ---------------------------------------------------------------------------

PHASE_UNSPECIFIED = 0
PHASE_PROVISIONING = 1
PHASE_READY = 2
PHASE_ERROR = 3
PHASE_DELETING = 4
PHASE_UNKNOWN = 5

_PHASE_NAMES: dict[int, str] = {
    PHASE_UNSPECIFIED: "Unspecified",
    PHASE_PROVISIONING: "Provisioning",
    PHASE_READY: "Ready",
    PHASE_ERROR: "Error",
    PHASE_DELETING: "Deleting",
    PHASE_UNKNOWN: "Unknown",
}

_LIVE_PHASES = {PHASE_READY, PHASE_PROVISIONING}

# ---------------------------------------------------------------------------
# Known remote provider health endpoints
# ---------------------------------------------------------------------------

_REMOTE_HEALTH_ENDPOINTS: dict[str, str] = {
    "nvidia-prod": "https://integrate.api.nvidia.com/v1/models",
    "nvidia-nim": "https://integrate.api.nvidia.com/v1/models",
    "nvidia-inference": "https://integrate.api.nvidia.com/v1/models",
    "openai-api": "https://api.openai.com/v1/models",
    "anthropic-prod": "https://api.anthropic.com/v1/models",
    "gemini-api": "https://generativelanguage.googleapis.com/v1/models",
}

# Providers whose endpoint URL is user-controlled; skip HTTP probe.
_SKIP_PROBE_PROVIDERS = {
    "compatible-endpoint",
    "compatible-anthropic-endpoint",
    "nim-local",
}

# Local providers use a localhost endpoint; probe it directly.
_LOCAL_PROVIDER_PREFIXES = ("vllm-local", "ollama-local", "local-")

_DEFAULT_HTTP_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Return types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InferenceHealthStatus:
    """Result of an HTTP reachability probe to the provider endpoint."""
    ok: bool
    probed: bool
    endpoint: str
    detail: str
    failure_label: str = ""  # "unreachable" | "unhealthy" | ""


@dataclass(frozen=True)
class LiveGatewayStatus:
    """Result of a live gRPC probe to the OpenShell gateway."""
    reachable: bool
    sandbox_id: str = ""
    sandbox_phase: int = PHASE_UNKNOWN
    sandbox_phase_name: str = "Unknown"
    error: str = ""

    @property
    def is_ready(self) -> bool:
        return self.reachable and self.sandbox_phase == PHASE_READY

    @property
    def is_live(self) -> bool:
        return self.reachable and self.sandbox_phase in _LIVE_PHASES


@dataclass(frozen=True)
class SandboxStatus:
    """Aggregated status from registry, live gateway, and inference probe."""
    # --- registry fields ---
    name: str
    model: str = ""
    provider: str = ""
    policies: list[str] = field(default_factory=list)
    custom_policy_names: list[str] = field(default_factory=list)
    policy_tier: str | None = None
    gpu_enabled: bool = False
    host_gpu_detected: bool = False
    openshell_version: str = ""
    openshell_driver: str = ""
    image_tag: str | None = None
    agent: str | None = None
    agent_version: str | None = None
    messaging_channels: list[str] = field(default_factory=list)
    disabled_channels: list[str] = field(default_factory=list)
    # --- live sources ---
    gateway: LiveGatewayStatus | None = None
    inference: InferenceHealthStatus | None = None
    # --- computed ---
    registry_found: bool = True

    @property
    def is_ready(self) -> bool:
        return self.gateway is not None and self.gateway.is_ready


# ---------------------------------------------------------------------------
# HTTP probe helper
# ---------------------------------------------------------------------------

HttpProbe = Callable[[str, float], bool]
"""Signature: (url, timeout_seconds) -> bool (True = any HTTP response received)."""


def _default_http_probe(url: str, timeout: float = _DEFAULT_HTTP_TIMEOUT) -> bool:
    """Return True if the endpoint responds with any HTTP status (including 4xx/5xx)."""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout):  # noqa: S310
            return True
    except urllib.error.HTTPError:
        # Any HTTP error still means the endpoint is up
        return True
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


# ---------------------------------------------------------------------------
# Inference health probe
# ---------------------------------------------------------------------------

def _probe_inference_health(
    provider: str,
    endpoint_override: str = "",
    *,
    http_probe: HttpProbe = _default_http_probe,
    timeout: float = _DEFAULT_HTTP_TIMEOUT,
) -> InferenceHealthStatus | None:
    """
    Probe the provider's health endpoint.

    Returns ``None`` for completely unrecognised providers.
    Returns a ``probed=False`` status for providers whose URL is unknown
    (compatible-* and nim-local).
    """
    if provider in _SKIP_PROBE_PROVIDERS:
        return InferenceHealthStatus(
            ok=True,
            probed=False,
            endpoint=endpoint_override,
            detail="Endpoint URL is user-controlled; skipping reachability check.",
        )

    is_local = any(provider.startswith(pfx) for pfx in _LOCAL_PROVIDER_PREFIXES)
    if is_local:
        url = endpoint_override or "http://localhost:8000/v1/models"
    else:
        url = endpoint_override or _REMOTE_HEALTH_ENDPOINTS.get(provider, "")

    if not url:
        return None

    reachable = http_probe(url, timeout)
    if reachable:
        return InferenceHealthStatus(
            ok=True,
            probed=True,
            endpoint=url,
            detail=f"{provider} endpoint is reachable at {url}.",
        )
    return InferenceHealthStatus(
        ok=False,
        probed=True,
        endpoint=url,
        detail=f"{provider} endpoint at {url} is unreachable. Check your network connection.",
        failure_label="unreachable",
    )


# ---------------------------------------------------------------------------
# StatusAggregator
# ---------------------------------------------------------------------------

class StatusAggregator:
    """
    Collects sandbox status from registry, live gateway, and inference probe.

    Parameters
    ----------
    sandbox_client:
        An initialised :class:`openshell.sandbox.SandboxClient`.  When
        ``None``, gateway probes are skipped and ``gateway`` in the result
        will be ``None``.
    inference_timeout:
        HTTP connect+read timeout for the inference health probe (seconds).
    http_probe:
        Injectable callable for testing; defaults to :func:`_default_http_probe`.
    """

    def __init__(
        self,
        sandbox_client: "SandboxClient | None" = None,
        *,
        inference_timeout: float = _DEFAULT_HTTP_TIMEOUT,
        http_probe: HttpProbe = _default_http_probe,
    ) -> None:
        self._client = sandbox_client
        self._inference_timeout = inference_timeout
        self._http_probe = http_probe

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_status(
        self,
        sandbox_name: str,
        *,
        skip_inference_probe: bool = False,
    ) -> SandboxStatus:
        """
        Return aggregated status for *sandbox_name*.

        The call never raises — all errors are captured in the returned
        struct.
        """
        entry = _registry.get_sandbox(sandbox_name)
        if entry is None:
            return SandboxStatus(name=sandbox_name, registry_found=False)

        gateway = self._probe_gateway(sandbox_name)

        inference: InferenceHealthStatus | None = None
        if not skip_inference_probe and gateway is not None and gateway.is_live:
            inference = self._probe_inference(entry)

        return SandboxStatus(
            name=entry.name,
            model=entry.model or "",
            provider=entry.provider or "",
            policies=list(entry.policies),
            custom_policy_names=[cp.name for cp in entry.custom_policies],
            policy_tier=entry.policy_tier,
            gpu_enabled=entry.gpu_enabled,
            host_gpu_detected=entry.host_gpu_detected,
            openshell_version=entry.openshell_version or "",
            openshell_driver=entry.openshell_driver or "",
            image_tag=entry.image_tag,
            agent=entry.agent,
            agent_version=entry.agent_version,
            messaging_channels=list(entry.messaging_channels),
            disabled_channels=list(entry.disabled_channels),
            gateway=gateway,
            inference=inference,
            registry_found=True,
        )

    def get_status_all(
        self,
        *,
        skip_inference_probe: bool = False,
    ) -> list[SandboxStatus]:
        """Return status for every sandbox in the registry."""
        sandboxes, _ = _registry.list_sandboxes()
        return [
            self.get_status(sb.name, skip_inference_probe=skip_inference_probe)
            for sb in sandboxes
        ]

    # ------------------------------------------------------------------
    # Internal probes
    # ------------------------------------------------------------------

    def _probe_gateway(self, sandbox_name: str) -> LiveGatewayStatus | None:
        if self._client is None:
            return None
        try:
            sandbox_ref = self._client.get(sandbox_name)
            phase = sandbox_ref.phase
            return LiveGatewayStatus(
                reachable=True,
                sandbox_id=sandbox_ref.id,
                sandbox_phase=phase,
                sandbox_phase_name=_PHASE_NAMES.get(phase, "Unknown"),
            )
        except Exception as exc:  # noqa: BLE001
            return LiveGatewayStatus(
                reachable=False,
                error=_summarise_error(exc),
            )

    def _probe_inference(
        self, entry: "_registry.SandboxEntry"
    ) -> InferenceHealthStatus | None:
        provider = entry.provider or ""
        if not provider:
            return None
        return _probe_inference_health(
            provider,
            http_probe=self._http_probe,
            timeout=self._inference_timeout,
        )


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def phase_name(phase: int) -> str:
    """Return the human-readable name for a sandbox phase integer."""
    return _PHASE_NAMES.get(phase, "Unknown")


def _summarise_error(exc: Exception) -> str:
    try:
        import grpc  # type: ignore[import-untyped]
        if isinstance(exc, grpc.RpcError):
            return f"gRPC {exc.code().name}: {exc.details()}"  # type: ignore[union-attr]
    except ImportError:
        pass
    return str(exc)
