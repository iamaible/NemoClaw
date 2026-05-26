# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any

import pytest

import nemoclaw.registry as reg_mod
from nemoclaw.registry import SandboxEntry
from nemoclaw.upgrade_orchestrator import (
    UpgradeOrchestrator,
    UpgradeOrchestratorError,
    UpgradeCheck,
    UpgradeResult,
    SandboxUpgradeResult,
)


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
# Fake sandbox client (delegates to a configurable RebuildOrchestrator fake)
# ---------------------------------------------------------------------------

@dataclass
class _FakeSandboxRef:
    id: str
    name: str
    phase: int = 2


class _FakeSandboxClient:
    def __init__(
        self,
        *,
        fail_delete: bool = False,
        fail_create: bool = False,
        fail_sandboxes: set[str] | None = None,
    ) -> None:
        self._fail_delete = fail_delete
        self._fail_create = fail_create
        self._fail_sandboxes = fail_sandboxes or set()
        self.deleted: list[str] = []
        self.created: list[Any] = []
        self.waited_ready: list[str] = []
        self.waited_deleted: list[str] = []

    def delete(self, sandbox_name: str) -> bool:
        if self._fail_delete or sandbox_name in self._fail_sandboxes:
            raise RuntimeError(f"delete failed for {sandbox_name}")
        self.deleted.append(sandbox_name)
        return True

    def wait_deleted(self, sandbox_name: str, *, timeout_seconds: float = 60.0) -> None:
        self.waited_deleted.append(sandbox_name)

    def create(self, *, spec: Any = None) -> _FakeSandboxRef:
        if self._fail_create:
            raise RuntimeError("create failed")
        self.created.append(spec)
        return _FakeSandboxRef(id="new-sb", name="box")

    def wait_ready(self, sandbox_name: str, *, timeout_seconds: float = 300.0) -> _FakeSandboxRef:
        self.waited_ready.append(sandbox_name)
        return _FakeSandboxRef(id="new-sb", name=sandbox_name)


# ===========================================================================
# check() — no client required
# ===========================================================================

def test_check_no_sandboxes_returns_empty() -> None:
    mgr = UpgradeOrchestrator()
    checks = mgr.check("registry.example.com/nemoclaw:v2.0.0")
    assert checks == []


def test_check_needs_upgrade_when_no_image_tag() -> None:
    _register("box")  # image_tag=None
    mgr = UpgradeOrchestrator()
    checks = mgr.check("registry.example.com/nemoclaw:v2.0.0")
    assert len(checks) == 1
    assert checks[0].needs_upgrade is True
    assert checks[0].current_image_tag is None


def test_check_needs_upgrade_when_different_tag() -> None:
    _register("box", image_tag="registry.example.com/nemoclaw:v1.0.0")
    mgr = UpgradeOrchestrator()
    checks = mgr.check("registry.example.com/nemoclaw:v2.0.0")
    assert checks[0].needs_upgrade is True
    assert checks[0].current_image_tag == "registry.example.com/nemoclaw:v1.0.0"


def test_check_no_upgrade_when_same_tag() -> None:
    _register("box", image_tag="registry.example.com/nemoclaw:v2.0.0")
    mgr = UpgradeOrchestrator()
    checks = mgr.check("registry.example.com/nemoclaw:v2.0.0")
    assert checks[0].needs_upgrade is False


def test_check_multiple_sandboxes() -> None:
    _register("box-a", image_tag="nemoclaw:v1.0.0")
    _register("box-b", image_tag="nemoclaw:v2.0.0")
    _register("box-c")  # no image_tag
    mgr = UpgradeOrchestrator()
    checks = mgr.check("nemoclaw:v2.0.0")
    by_name = {c.sandbox_name: c for c in checks}
    assert by_name["box-a"].needs_upgrade is True
    assert by_name["box-b"].needs_upgrade is False
    assert by_name["box-c"].needs_upgrade is True


def test_check_desired_tag_recorded_in_result() -> None:
    _register("box", image_tag="nemoclaw:v1.0.0")
    mgr = UpgradeOrchestrator()
    checks = mgr.check("nemoclaw:v2.0.0")
    assert checks[0].desired_image_tag == "nemoclaw:v2.0.0"


def test_check_with_explicit_sandbox_names() -> None:
    _register("box-a", image_tag="nemoclaw:v1.0.0")
    _register("box-b", image_tag="nemoclaw:v1.0.0")
    mgr = UpgradeOrchestrator()
    checks = mgr.check("nemoclaw:v2.0.0", sandbox_names=["box-a"])
    assert len(checks) == 1
    assert checks[0].sandbox_name == "box-a"


def test_check_ignores_unknown_names_in_explicit_list() -> None:
    _register("box")
    mgr = UpgradeOrchestrator()
    checks = mgr.check("nemoclaw:v2.0.0", sandbox_names=["box", "ghost"])
    assert len(checks) == 1  # ghost not in registry


# ===========================================================================
# upgrade() — requires client
# ===========================================================================

def test_upgrade_requires_client() -> None:
    _register("box")
    mgr = UpgradeOrchestrator()
    with pytest.raises(UpgradeOrchestratorError, match="SandboxClient"):
        mgr.upgrade("nemoclaw:v2.0.0")


def test_upgrade_skips_already_current() -> None:
    _register("box", image_tag="nemoclaw:v2.0.0")
    sc = _FakeSandboxClient()
    mgr = UpgradeOrchestrator(sandbox_client=sc)
    result = mgr.upgrade("nemoclaw:v2.0.0")
    assert result.ok is True
    assert "box" in result.skipped
    assert sc.deleted == []


def test_upgrade_rebuilds_stale_sandbox() -> None:
    _register("box", image_tag="nemoclaw:v1.0.0")
    sc = _FakeSandboxClient()
    mgr = UpgradeOrchestrator(sandbox_client=sc)
    result = mgr.upgrade("nemoclaw:v2.0.0")
    assert result.ok is True
    assert "box" in result.upgraded
    assert sc.deleted == ["box"]


def test_upgrade_updates_registry_image_tag_on_success() -> None:
    _register("box", image_tag="nemoclaw:v1.0.0")
    sc = _FakeSandboxClient()
    mgr = UpgradeOrchestrator(sandbox_client=sc)
    mgr.upgrade("nemoclaw:v2.0.0")
    entry = reg_mod.get_sandbox("box")
    assert entry is not None
    assert entry.image_tag == "nemoclaw:v2.0.0"


def test_upgrade_does_not_update_registry_on_failure() -> None:
    _register("box", image_tag="nemoclaw:v1.0.0")
    sc = _FakeSandboxClient(fail_delete=True)
    mgr = UpgradeOrchestrator(sandbox_client=sc)
    mgr.upgrade("nemoclaw:v2.0.0")
    entry = reg_mod.get_sandbox("box")
    assert entry is not None
    assert entry.image_tag == "nemoclaw:v1.0.0"


def test_upgrade_result_ok_all_succeeded() -> None:
    _register("box-a", image_tag="nemoclaw:v1.0.0")
    _register("box-b", image_tag="nemoclaw:v1.0.0")
    sc = _FakeSandboxClient()
    mgr = UpgradeOrchestrator(sandbox_client=sc)
    result = mgr.upgrade("nemoclaw:v2.0.0")
    assert result.ok is True
    assert set(result.upgraded) == {"box-a", "box-b"}


def test_upgrade_result_ok_false_on_failure() -> None:
    _register("box", image_tag="nemoclaw:v1.0.0")
    sc = _FakeSandboxClient(fail_delete=True)
    mgr = UpgradeOrchestrator(sandbox_client=sc)
    result = mgr.upgrade("nemoclaw:v2.0.0")
    assert result.ok is False
    assert "box" in result.failed


def test_upgrade_failure_does_not_abort_batch() -> None:
    _register("box-a", image_tag="nemoclaw:v1.0.0")
    _register("box-b", image_tag="nemoclaw:v1.0.0")
    # Only box-a fails delete
    sc = _FakeSandboxClient(fail_sandboxes={"box-a"})
    mgr = UpgradeOrchestrator(sandbox_client=sc)
    result = mgr.upgrade("nemoclaw:v2.0.0")
    assert "box-a" in result.failed
    assert "box-b" in result.upgraded
    assert result.ok is False


def test_upgrade_checks_included_in_result() -> None:
    _register("box-a", image_tag="nemoclaw:v1.0.0")
    _register("box-b", image_tag="nemoclaw:v2.0.0")
    sc = _FakeSandboxClient()
    mgr = UpgradeOrchestrator(sandbox_client=sc)
    result = mgr.upgrade("nemoclaw:v2.0.0")
    assert len(result.checks) == 2


def test_upgrade_results_included_in_result() -> None:
    _register("box", image_tag="nemoclaw:v1.0.0")
    sc = _FakeSandboxClient()
    mgr = UpgradeOrchestrator(sandbox_client=sc)
    result = mgr.upgrade("nemoclaw:v2.0.0")
    assert len(result.results) == 1
    assert result.results[0].sandbox_name == "box"


def test_upgrade_rebuild_result_attached_on_success() -> None:
    _register("box", image_tag="nemoclaw:v1.0.0")
    sc = _FakeSandboxClient()
    mgr = UpgradeOrchestrator(sandbox_client=sc)
    result = mgr.upgrade("nemoclaw:v2.0.0")
    box_result = result.results[0]
    assert box_result.rebuild_result is not None
    assert box_result.rebuild_result.ok is True


def test_upgrade_rebuild_result_attached_on_failure() -> None:
    _register("box", image_tag="nemoclaw:v1.0.0")
    sc = _FakeSandboxClient(fail_delete=True)
    mgr = UpgradeOrchestrator(sandbox_client=sc)
    result = mgr.upgrade("nemoclaw:v2.0.0")
    box_result = result.results[0]
    assert box_result.rebuild_result is not None
    assert box_result.rebuild_result.ok is False


def test_upgrade_desired_tag_in_spec() -> None:
    _register("box", image_tag="nemoclaw:v1.0.0")
    sc = _FakeSandboxClient()
    mgr = UpgradeOrchestrator(sandbox_client=sc)
    mgr.upgrade("nemoclaw:v2.0.0")
    spec = sc.created[0]
    assert spec.template.image == "nemoclaw:v2.0.0"


def test_upgrade_preserves_gpu_flag_in_spec() -> None:
    _register("box", image_tag="nemoclaw:v1.0.0", gpu_enabled=True)
    sc = _FakeSandboxClient()
    mgr = UpgradeOrchestrator(sandbox_client=sc)
    mgr.upgrade("nemoclaw:v2.0.0")
    spec = sc.created[0]
    assert spec.gpu is True


def test_upgrade_no_gpu_by_default_in_spec() -> None:
    _register("box", image_tag="nemoclaw:v1.0.0")
    sc = _FakeSandboxClient()
    mgr = UpgradeOrchestrator(sandbox_client=sc)
    mgr.upgrade("nemoclaw:v2.0.0")
    spec = sc.created[0]
    assert spec.gpu is False


def test_upgrade_desired_image_tag_in_result() -> None:
    sc = _FakeSandboxClient()
    mgr = UpgradeOrchestrator(sandbox_client=sc)
    result = mgr.upgrade("nemoclaw:v2.0.0")
    assert result.desired_image_tag == "nemoclaw:v2.0.0"


def test_upgrade_empty_registry_returns_ok() -> None:
    sc = _FakeSandboxClient()
    mgr = UpgradeOrchestrator(sandbox_client=sc)
    result = mgr.upgrade("nemoclaw:v2.0.0")
    assert result.ok is True
    assert result.upgraded == []
    assert result.skipped == []


# ===========================================================================
# force flag
# ===========================================================================

def test_upgrade_force_rebuilds_already_current() -> None:
    _register("box", image_tag="nemoclaw:v2.0.0")
    sc = _FakeSandboxClient()
    mgr = UpgradeOrchestrator(sandbox_client=sc)
    result = mgr.upgrade("nemoclaw:v2.0.0", force=True)
    assert "box" in result.upgraded
    assert sc.deleted == ["box"]


def test_upgrade_force_false_skips_current() -> None:
    _register("box", image_tag="nemoclaw:v2.0.0")
    sc = _FakeSandboxClient()
    mgr = UpgradeOrchestrator(sandbox_client=sc)
    result = mgr.upgrade("nemoclaw:v2.0.0", force=False)
    assert "box" in result.skipped
    assert sc.deleted == []


# ===========================================================================
# dry_run flag
# ===========================================================================

def test_dry_run_does_not_require_client() -> None:
    _register("box", image_tag="nemoclaw:v1.0.0")
    mgr = UpgradeOrchestrator()  # no sandbox client
    result = mgr.upgrade("nemoclaw:v2.0.0", dry_run=True)
    assert result.dry_run is True
    assert result.ok is True


def test_dry_run_does_not_delete_sandbox() -> None:
    _register("box", image_tag="nemoclaw:v1.0.0")
    sc = _FakeSandboxClient()
    mgr = UpgradeOrchestrator(sandbox_client=sc)
    mgr.upgrade("nemoclaw:v2.0.0", dry_run=True)
    assert sc.deleted == []


def test_dry_run_checks_still_populated() -> None:
    _register("box-a", image_tag="nemoclaw:v1.0.0")
    _register("box-b", image_tag="nemoclaw:v2.0.0")
    mgr = UpgradeOrchestrator()
    result = mgr.upgrade("nemoclaw:v2.0.0", dry_run=True)
    assert len(result.checks) == 2
    by_name = {c.sandbox_name: c for c in result.checks}
    assert by_name["box-a"].needs_upgrade is True
    assert by_name["box-b"].needs_upgrade is False


def test_dry_run_does_not_update_registry() -> None:
    _register("box", image_tag="nemoclaw:v1.0.0")
    mgr = UpgradeOrchestrator()
    mgr.upgrade("nemoclaw:v2.0.0", dry_run=True)
    entry = reg_mod.get_sandbox("box")
    assert entry is not None
    assert entry.image_tag == "nemoclaw:v1.0.0"


# ===========================================================================
# sandbox_names filter
# ===========================================================================

def test_upgrade_explicit_sandbox_names_limits_scope() -> None:
    _register("box-a", image_tag="nemoclaw:v1.0.0")
    _register("box-b", image_tag="nemoclaw:v1.0.0")
    sc = _FakeSandboxClient()
    mgr = UpgradeOrchestrator(sandbox_client=sc)
    result = mgr.upgrade("nemoclaw:v2.0.0", sandbox_names=["box-a"])
    assert "box-a" in result.upgraded
    assert "box-b" not in result.upgraded
    assert sc.deleted == ["box-a"]


def test_upgrade_skipped_result_is_not_in_failed() -> None:
    _register("box", image_tag="nemoclaw:v2.0.0")
    sc = _FakeSandboxClient()
    mgr = UpgradeOrchestrator(sandbox_client=sc)
    result = mgr.upgrade("nemoclaw:v2.0.0")
    assert "box" not in result.failed
    assert result.ok is True
