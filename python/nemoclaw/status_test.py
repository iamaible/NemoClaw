# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any

import pytest

import nemoclaw.registry as reg_mod
from nemoclaw.registry import SandboxEntry
from nemoclaw.status import (
    PHASE_ERROR,
    PHASE_PROVISIONING,
    PHASE_READY,
    PHASE_UNKNOWN,
    InferenceHealthStatus,
    LiveGatewayStatus,
    SandboxStatus,
    StatusAggregator,
    _probe_inference_health,
    phase_name,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry_file = tmp_path / ".nemoclaw" / "sandboxes.json"
    monkeypatch.setattr(reg_mod, "REGISTRY_FILE", registry_file)
    monkeypatch.setattr(reg_mod, "_LOCK_DIR", pathlib.Path(str(registry_file) + ".lock"))
    monkeypatch.setattr(reg_mod, "_LOCK_OWNER", pathlib.Path(str(registry_file) + ".lock") / "owner")


def _register(name: str = "my-box", **kwargs: Any) -> SandboxEntry:
    entry = SandboxEntry(name=name, **kwargs)
    reg_mod.register_sandbox(entry)
    return entry


# ---------------------------------------------------------------------------
# Fake SandboxClient
# ---------------------------------------------------------------------------

@dataclass
class _FakeSandboxRef:
    id: str
    name: str
    phase: int


class _FakeSandboxClient:
    def __init__(
        self,
        phase: int = PHASE_READY,
        sandbox_id: str = "sb-abc",
        raise_exc: Exception | None = None,
    ) -> None:
        self._phase = phase
        self._sandbox_id = sandbox_id
        self._raise = raise_exc

    def get(self, sandbox_name: str) -> _FakeSandboxRef:
        if self._raise is not None:
            raise self._raise
        return _FakeSandboxRef(id=self._sandbox_id, name=sandbox_name, phase=self._phase)


# ---------------------------------------------------------------------------
# HTTP probe helper
# ---------------------------------------------------------------------------

def _probe_ok(_url: str, _timeout: float) -> bool:
    return True


def _probe_fail(_url: str, _timeout: float) -> bool:
    return False


# ---------------------------------------------------------------------------
# phase_name
# ---------------------------------------------------------------------------

def test_phase_name_ready() -> None:
    assert phase_name(PHASE_READY) == "Ready"


def test_phase_name_unknown_fallback() -> None:
    assert phase_name(999) == "Unknown"


# ---------------------------------------------------------------------------
# InferenceHealthStatus
# ---------------------------------------------------------------------------

def test_inference_health_ok() -> None:
    status = InferenceHealthStatus(ok=True, probed=True, endpoint="http://x", detail="ok")
    assert status.ok is True
    assert status.failure_label == ""


def test_inference_health_fail_label() -> None:
    status = InferenceHealthStatus(
        ok=False, probed=True, endpoint="http://x", detail="bad", failure_label="unreachable"
    )
    assert status.failure_label == "unreachable"


# ---------------------------------------------------------------------------
# LiveGatewayStatus
# ---------------------------------------------------------------------------

def test_live_gateway_is_ready() -> None:
    gw = LiveGatewayStatus(reachable=True, sandbox_phase=PHASE_READY)
    assert gw.is_ready is True
    assert gw.is_live is True


def test_live_gateway_provisioning_is_live_not_ready() -> None:
    gw = LiveGatewayStatus(reachable=True, sandbox_phase=PHASE_PROVISIONING)
    assert gw.is_ready is False
    assert gw.is_live is True


def test_live_gateway_error_not_live() -> None:
    gw = LiveGatewayStatus(reachable=True, sandbox_phase=PHASE_ERROR)
    assert gw.is_live is False


def test_live_gateway_unreachable() -> None:
    gw = LiveGatewayStatus(reachable=False)
    assert gw.is_ready is False
    assert gw.is_live is False


# ---------------------------------------------------------------------------
# _probe_inference_health
# ---------------------------------------------------------------------------

def test_probe_known_remote_provider_reachable() -> None:
    result = _probe_inference_health("nvidia-prod", http_probe=_probe_ok)
    assert result is not None
    assert result.ok is True
    assert result.probed is True
    assert "nvidia" in result.endpoint


def test_probe_known_remote_provider_unreachable() -> None:
    result = _probe_inference_health("openai-api", http_probe=_probe_fail)
    assert result is not None
    assert result.ok is False
    assert result.failure_label == "unreachable"


def test_probe_skip_compatible_endpoint() -> None:
    result = _probe_inference_health("compatible-endpoint", http_probe=_probe_fail)
    assert result is not None
    assert result.probed is False
    assert result.ok is True


def test_probe_nim_local_skipped() -> None:
    result = _probe_inference_health("nim-local", http_probe=_probe_fail)
    assert result is not None
    assert result.probed is False


def test_probe_unknown_provider_returns_none() -> None:
    result = _probe_inference_health("totally-unknown-xyz", http_probe=_probe_fail)
    assert result is None


def test_probe_endpoint_override() -> None:
    result = _probe_inference_health(
        "nvidia-prod",
        endpoint_override="http://custom-host/v1/models",
        http_probe=_probe_ok,
    )
    assert result is not None
    assert "custom-host" in result.endpoint


# ---------------------------------------------------------------------------
# StatusAggregator — no client (registry-only)
# ---------------------------------------------------------------------------

def test_status_registry_not_found_returns_not_found() -> None:
    agg = StatusAggregator()
    status = agg.get_status("ghost")
    assert status.registry_found is False
    assert status.name == "ghost"


def test_status_registry_only_no_gateway() -> None:
    _register("box", model="nvidia/nemotron-super", provider="nvidia-prod")
    agg = StatusAggregator()
    status = agg.get_status("box")
    assert status.registry_found is True
    assert status.model == "nvidia/nemotron-super"
    assert status.provider == "nvidia-prod"
    assert status.gateway is None
    assert status.inference is None


def test_status_populates_policies() -> None:
    _register("box", policies=["slack", "discord"])
    agg = StatusAggregator()
    status = agg.get_status("box")
    assert set(status.policies) == {"slack", "discord"}


def test_status_populates_gpu_fields() -> None:
    _register("box", gpu_enabled=True, host_gpu_detected=True)
    agg = StatusAggregator()
    status = agg.get_status("box")
    assert status.gpu_enabled is True
    assert status.host_gpu_detected is True


# ---------------------------------------------------------------------------
# StatusAggregator — with live gateway client
# ---------------------------------------------------------------------------

def test_status_with_ready_gateway() -> None:
    _register("box", provider="nvidia-prod")
    client = _FakeSandboxClient(phase=PHASE_READY)
    agg = StatusAggregator(client, http_probe=_probe_ok)
    status = agg.get_status("box")
    assert status.gateway is not None
    assert status.gateway.reachable is True
    assert status.gateway.sandbox_phase == PHASE_READY
    assert status.is_ready is True


def test_status_with_provisioning_gateway_no_inference_probe() -> None:
    _register("box", provider="nvidia-prod")
    client = _FakeSandboxClient(phase=PHASE_PROVISIONING)
    agg = StatusAggregator(client, http_probe=_probe_fail)
    status = agg.get_status("box")
    # PROVISIONING is_live=True so inference IS probed
    assert status.inference is not None


def test_status_with_error_gateway_no_inference_probe() -> None:
    _register("box", provider="nvidia-prod")
    client = _FakeSandboxClient(phase=PHASE_ERROR)
    agg = StatusAggregator(client, http_probe=_probe_ok)
    status = agg.get_status("box")
    assert status.inference is None  # not live → skip inference


def test_status_gateway_error_captured() -> None:
    _register("box")
    client = _FakeSandboxClient(raise_exc=RuntimeError("connection refused"))
    agg = StatusAggregator(client)
    status = agg.get_status("box")
    assert status.gateway is not None
    assert status.gateway.reachable is False
    assert "connection refused" in status.gateway.error


def test_status_skip_inference_probe_flag() -> None:
    _register("box", provider="nvidia-prod")
    client = _FakeSandboxClient(phase=PHASE_READY)
    agg = StatusAggregator(client, http_probe=_probe_ok)
    status = agg.get_status("box", skip_inference_probe=True)
    assert status.inference is None


def test_status_inference_reachable() -> None:
    _register("box", provider="nvidia-prod")
    client = _FakeSandboxClient(phase=PHASE_READY)
    agg = StatusAggregator(client, http_probe=_probe_ok)
    status = agg.get_status("box")
    assert status.inference is not None
    assert status.inference.ok is True
    assert status.inference.probed is True


def test_status_inference_unreachable() -> None:
    _register("box", provider="openai-api")
    client = _FakeSandboxClient(phase=PHASE_READY)
    agg = StatusAggregator(client, http_probe=_probe_fail)
    status = agg.get_status("box")
    assert status.inference is not None
    assert status.inference.ok is False


def test_status_inference_skipped_for_no_provider() -> None:
    _register("box")  # no provider set
    client = _FakeSandboxClient(phase=PHASE_READY)
    agg = StatusAggregator(client, http_probe=_probe_ok)
    status = agg.get_status("box")
    assert status.inference is None


# ---------------------------------------------------------------------------
# get_status_all
# ---------------------------------------------------------------------------

def test_get_status_all_returns_all() -> None:
    _register("alpha")
    _register("beta")
    agg = StatusAggregator()
    results = agg.get_status_all()
    names = {s.name for s in results}
    assert names == {"alpha", "beta"}


def test_get_status_all_empty_registry() -> None:
    agg = StatusAggregator()
    assert agg.get_status_all() == []
