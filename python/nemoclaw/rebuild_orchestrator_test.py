# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any

import pytest

import nemoclaw.registry as reg_mod
from nemoclaw.policy_manager import PolicyAddResult
from nemoclaw.rebuild_orchestrator import (
    RebuildOrchestrator,
    RebuildOrchestratorError,
    RebuildResult,
    _build_spec_from_entry,
)
from nemoclaw.registry import SandboxEntry


# ---------------------------------------------------------------------------
# Registry isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / ".nemoclaw" / "sandboxes.json"
    monkeypatch.setattr(reg_mod, "REGISTRY_FILE", f)
    monkeypatch.setattr(reg_mod, "_LOCK_DIR", pathlib.Path(str(f) + ".lock"))
    monkeypatch.setattr(reg_mod, "_LOCK_OWNER", pathlib.Path(str(f) + ".lock") / "owner")


def _register(name: str = "box", **kwargs: Any) -> SandboxEntry:
    entry = SandboxEntry(name=name, **kwargs)
    reg_mod.register_sandbox(entry)
    return entry


# ---------------------------------------------------------------------------
# Fake clients
# ---------------------------------------------------------------------------

@dataclass
class _FakeSandboxRef:
    id: str
    name: str
    phase: int = 2  # PHASE_READY


class _FakeSandboxClient:
    def __init__(
        self,
        *,
        fail_delete: bool = False,
        fail_create: bool = False,
        fail_wait_ready: bool = False,
    ) -> None:
        self._fail_delete = fail_delete
        self._fail_create = fail_create
        self._fail_wait_ready = fail_wait_ready
        self.deleted: list[str] = []
        self.created: list[Any] = []
        self.waited_ready: list[str] = []
        self.waited_deleted: list[str] = []

    def delete(self, sandbox_name: str) -> bool:
        if self._fail_delete:
            raise RuntimeError("delete failed")
        self.deleted.append(sandbox_name)
        return True

    def wait_deleted(self, sandbox_name: str, *, timeout_seconds: float = 60.0) -> None:
        self.waited_deleted.append(sandbox_name)

    def create(self, *, spec: Any = None) -> _FakeSandboxRef:
        if self._fail_create:
            raise RuntimeError("create failed")
        self.created.append(spec)
        return _FakeSandboxRef(id="new-sb-123", name="box")

    def wait_ready(self, sandbox_name: str, *, timeout_seconds: float = 300.0) -> _FakeSandboxRef:
        if self._fail_wait_ready:
            raise RuntimeError("wait_ready timed out")
        self.waited_ready.append(sandbox_name)
        return _FakeSandboxRef(id="new-sb-123", name=sandbox_name)


class _FakeProviderClient:
    def __init__(
        self,
        *,
        provider_names: list[str] | None = None,
        fail_list: bool = False,
        fail_attach: bool = False,
    ) -> None:
        self._provider_names = provider_names or []
        self._fail_list = fail_list
        self._fail_attach = fail_attach
        self.listed: list[str] = []
        self.attached: list[tuple[str, str]] = []

    def list_sandbox_providers(self, sandbox_name: str) -> list[Any]:
        if self._fail_list:
            raise RuntimeError("list failed")
        self.listed.append(sandbox_name)

        class _Ref:
            def __init__(self, n: str) -> None:
                self.name = n

        return [_Ref(n) for n in self._provider_names]

    def attach(self, sandbox_name: str, provider_name: str) -> bool:
        if self._fail_attach:
            raise RuntimeError("attach failed")
        self.attached.append((sandbox_name, provider_name))
        return True


class _FakePolicyManager:
    def __init__(self, *, fail_add: bool = False) -> None:
        self._fail_add = fail_add
        self.added: list[tuple[str, str]] = []

    def add_preset(self, sandbox_name: str, preset_name: str) -> PolicyAddResult:
        if self._fail_add:
            return PolicyAddResult(ok=False, preset=preset_name, error="policy merge failed")
        self.added.append((sandbox_name, preset_name))
        return PolicyAddResult(ok=True, preset=preset_name, applied_rules=[preset_name])

    def list_applied_presets(self, sandbox_name: str) -> list[str]:
        entry = reg_mod.get_sandbox(sandbox_name)
        return list(entry.policies) if entry else []


# ---------------------------------------------------------------------------
# Error: missing SandboxClient
# ---------------------------------------------------------------------------

def test_rebuild_requires_sandbox_client() -> None:
    mgr = RebuildOrchestrator()
    with pytest.raises(RebuildOrchestratorError, match="SandboxClient"):
        mgr.rebuild("box")


# ---------------------------------------------------------------------------
# Preflight: sandbox not in registry
# ---------------------------------------------------------------------------

def test_rebuild_missing_sandbox_returns_error() -> None:
    sc = _FakeSandboxClient()
    mgr = RebuildOrchestrator(sandbox_client=sc)
    result = mgr.rebuild("ghost")
    assert result.ok is False
    assert "not found" in result.error
    assert sc.deleted == []


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_rebuild_calls_delete_and_create() -> None:
    _register("box")
    sc = _FakeSandboxClient()
    mgr = RebuildOrchestrator(sandbox_client=sc)
    result = mgr.rebuild("box")
    assert result.ok is True
    assert "box" in sc.deleted
    assert len(sc.created) == 1


def test_rebuild_calls_wait_ready() -> None:
    _register("box")
    sc = _FakeSandboxClient()
    mgr = RebuildOrchestrator(sandbox_client=sc)
    mgr.rebuild("box")
    assert "box" in sc.waited_ready


def test_rebuild_result_sandbox_name() -> None:
    _register("box")
    sc = _FakeSandboxClient()
    mgr = RebuildOrchestrator(sandbox_client=sc)
    result = mgr.rebuild("box")
    assert result.sandbox_name == "box"


def test_rebuild_steps_recorded() -> None:
    _register("box")
    sc = _FakeSandboxClient()
    mgr = RebuildOrchestrator(sandbox_client=sc)
    result = mgr.rebuild("box")
    step_names = [s.name for s in result.steps]
    assert "preflight" in step_names
    assert "delete" in step_names
    assert "create" in step_names
    assert "wait_ready" in step_names


def test_rebuild_all_steps_ok() -> None:
    _register("box")
    sc = _FakeSandboxClient()
    mgr = RebuildOrchestrator(sandbox_client=sc)
    result = mgr.rebuild("box")
    assert all(s.ok for s in result.steps)


# ---------------------------------------------------------------------------
# Provider snapshot and re-attachment
# ---------------------------------------------------------------------------

def test_rebuild_snapshots_providers() -> None:
    _register("box")
    sc = _FakeSandboxClient()
    pc = _FakeProviderClient(provider_names=["prov-a", "prov-b"])
    mgr = RebuildOrchestrator(sandbox_client=sc, provider_client=pc)
    mgr.rebuild("box")
    assert "box" in pc.listed


def test_rebuild_reattaches_providers() -> None:
    _register("box")
    sc = _FakeSandboxClient()
    pc = _FakeProviderClient(provider_names=["prov-a", "prov-b"])
    mgr = RebuildOrchestrator(sandbox_client=sc, provider_client=pc)
    result = mgr.rebuild("box")
    assert set(result.reattached_providers) == {"prov-a", "prov-b"}
    attached_names = {name for _, name in pc.attached}
    assert attached_names == {"prov-a", "prov-b"}


def test_rebuild_no_provider_client_skips_attachment() -> None:
    _register("box")
    sc = _FakeSandboxClient()
    mgr = RebuildOrchestrator(sandbox_client=sc)  # no provider client
    result = mgr.rebuild("box")
    assert result.ok is True
    assert result.reattached_providers == []


def test_rebuild_provider_list_failure_is_non_fatal() -> None:
    _register("box")
    sc = _FakeSandboxClient()
    pc = _FakeProviderClient(fail_list=True)
    mgr = RebuildOrchestrator(sandbox_client=sc, provider_client=pc)
    result = mgr.rebuild("box")
    # Snapshot step failed but rebuild continued
    snapshot_step = next((s for s in result.steps if s.name == "snapshot_providers"), None)
    assert snapshot_step is not None
    assert snapshot_step.ok is False
    # No attach step was added (no providers to attach)
    assert result.reattached_providers == []


def test_rebuild_provider_attach_failure_marks_step_failed() -> None:
    _register("box")
    sc = _FakeSandboxClient()
    pc = _FakeProviderClient(provider_names=["prov-a"], fail_attach=True)
    mgr = RebuildOrchestrator(sandbox_client=sc, provider_client=pc)
    result = mgr.rebuild("box")
    assert result.ok is False
    attach_step = next((s for s in result.steps if s.name == "attach_providers"), None)
    assert attach_step is not None
    assert attach_step.ok is False


# ---------------------------------------------------------------------------
# Policy preset restoration
# ---------------------------------------------------------------------------

def test_rebuild_restores_presets() -> None:
    _register("box", policies=["slack", "telegram"])
    sc = _FakeSandboxClient()
    pm = _FakePolicyManager()
    mgr = RebuildOrchestrator(sandbox_client=sc, policy_manager=pm)
    result = mgr.rebuild("box")
    assert set(result.restored_presets) == {"slack", "telegram"}
    added_pairs = {(sb, p) for sb, p in pm.added}
    assert ("box", "slack") in added_pairs
    assert ("box", "telegram") in added_pairs


def test_rebuild_no_policy_manager_skips_presets() -> None:
    _register("box", policies=["slack"])
    sc = _FakeSandboxClient()
    mgr = RebuildOrchestrator(sandbox_client=sc)  # no policy manager
    result = mgr.rebuild("box")
    assert result.ok is True
    assert result.restored_presets == []


def test_rebuild_preset_failure_marks_step_failed() -> None:
    _register("box", policies=["slack"])
    sc = _FakeSandboxClient()
    pm = _FakePolicyManager(fail_add=True)
    mgr = RebuildOrchestrator(sandbox_client=sc, policy_manager=pm)
    result = mgr.rebuild("box")
    assert result.ok is False
    preset_step = next((s for s in result.steps if s.name == "apply_presets"), None)
    assert preset_step is not None
    assert preset_step.ok is False


def test_rebuild_no_presets_skips_apply_step() -> None:
    _register("box")  # no policies
    sc = _FakeSandboxClient()
    pm = _FakePolicyManager()
    mgr = RebuildOrchestrator(sandbox_client=sc, policy_manager=pm)
    mgr.rebuild("box")
    assert pm.added == []


# ---------------------------------------------------------------------------
# Delete failure
# ---------------------------------------------------------------------------

def test_rebuild_delete_failure_returns_error() -> None:
    _register("box")
    sc = _FakeSandboxClient(fail_delete=True)
    mgr = RebuildOrchestrator(sandbox_client=sc)
    result = mgr.rebuild("box")
    assert result.ok is False
    assert "delete failed" in result.error
    assert sc.created == []


# ---------------------------------------------------------------------------
# Create failure (post-delete — point of no return)
# ---------------------------------------------------------------------------

def test_rebuild_create_failure_returns_error() -> None:
    _register("box")
    sc = _FakeSandboxClient(fail_create=True)
    mgr = RebuildOrchestrator(sandbox_client=sc)
    result = mgr.rebuild("box")
    assert result.ok is False
    assert "create failed after delete" in result.error


def test_rebuild_create_failure_after_delete_records_both_steps() -> None:
    _register("box")
    sc = _FakeSandboxClient(fail_create=True)
    mgr = RebuildOrchestrator(sandbox_client=sc)
    result = mgr.rebuild("box")
    step_names = [s.name for s in result.steps]
    assert "delete" in step_names
    assert "create" in step_names
    delete_step = next(s for s in result.steps if s.name == "delete")
    assert delete_step.ok is True
    create_step = next(s for s in result.steps if s.name == "create")
    assert create_step.ok is False


# ---------------------------------------------------------------------------
# Wait ready failure (non-fatal)
# ---------------------------------------------------------------------------

def test_rebuild_wait_ready_failure_is_non_fatal() -> None:
    _register("box")
    sc = _FakeSandboxClient(fail_wait_ready=True)
    mgr = RebuildOrchestrator(sandbox_client=sc)
    result = mgr.rebuild("box")
    # wait_ready failure marks that step failed but rebuild continues
    wait_step = next((s for s in result.steps if s.name == "wait_ready"), None)
    assert wait_step is not None
    assert wait_step.ok is False
    # The overall result is failed because of the step failure
    assert result.ok is False


# ---------------------------------------------------------------------------
# _build_spec_from_entry
# ---------------------------------------------------------------------------

def test_build_spec_from_entry_uses_image_tag() -> None:
    entry = SandboxEntry(name="box", image_tag="registry.example.com/nemoclaw:v1.2.3")
    spec = _build_spec_from_entry(entry)
    assert spec.template.image == "registry.example.com/nemoclaw:v1.2.3"


def test_build_spec_from_entry_gpu_flag() -> None:
    entry = SandboxEntry(name="box", gpu_enabled=True)
    spec = _build_spec_from_entry(entry)
    assert spec.gpu is True


def test_build_spec_from_entry_no_image_tag() -> None:
    entry = SandboxEntry(name="box")
    spec = _build_spec_from_entry(entry)
    assert spec.template.image == ""


# ---------------------------------------------------------------------------
# Custom spec passthrough
# ---------------------------------------------------------------------------

def test_rebuild_uses_caller_provided_spec() -> None:
    _register("box")
    sc = _FakeSandboxClient()
    mgr = RebuildOrchestrator(sandbox_client=sc)

    from openshell._proto import openshell_pb2
    custom_spec = openshell_pb2.SandboxSpec(
        template=openshell_pb2.SandboxTemplate(image="custom:latest"),
        gpu=True,
    )
    mgr.rebuild("box", spec=custom_spec)
    assert sc.created[0] is custom_spec
