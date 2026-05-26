# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pathlib
from typing import Any

import pytest

import nemoclaw.registry as reg_mod
from nemoclaw.policy_engine import PolicyEngine
from nemoclaw.policy_manager import (
    PolicyAddResult,
    PolicyApplyTierResult,
    PolicyManager,
    PolicyManagerError,
    PolicyRemoveResult,
    _np_entry_to_proto,
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
# Fake PolicyClient
# ---------------------------------------------------------------------------

class _FakePolicyClient:
    def __init__(self, *, fail_merge: bool = False) -> None:
        self._fail_merge = fail_merge
        self.merge_calls: list[tuple[str, list[Any]]] = []

    def merge(self, sandbox_name: str, ops: Any) -> None:
        if self._fail_merge:
            raise RuntimeError("merge failed")
        self.merge_calls.append((sandbox_name, list(ops)))


# ---------------------------------------------------------------------------
# Preset YAML fixture
# ---------------------------------------------------------------------------

_SIMPLE_PRESET_YAML = """\
preset:
  name: mybot
  description: Test bot preset

network_policies:
  mybot_api:
    name: mybot_api
    endpoints:
      - host: api.mybot.io
        port: 443
        protocol: rest
        enforcement: enforce
        rules:
          - allow: { method: GET, path: "/**" }
          - allow: { method: POST, path: "/**" }
    binaries:
      - { path: /usr/local/bin/node }
"""

_TWO_RULE_PRESET_YAML = """\
preset:
  name: twobots

network_policies:
  rule_a:
    name: rule_a
    endpoints:
      - host: a.example.com
        port: 443
        access: full
  rule_b:
    name: rule_b
    endpoints:
      - host: b.example.com
        port: 80
"""

_TIER_YAML = """\
tiers:
  - name: standard
    label: Standard
    description: A standard tier
    presets:
      - name: mybot
      - name: twobots
"""


@pytest.fixture()
def preset_engine(tmp_path: pathlib.Path) -> PolicyEngine:
    presets_dir = tmp_path / "presets"
    presets_dir.mkdir()
    (presets_dir / "mybot.yaml").write_text(_SIMPLE_PRESET_YAML)
    (presets_dir / "twobots.yaml").write_text(_TWO_RULE_PRESET_YAML)

    tiers_file = tmp_path / "tiers.yaml"
    tiers_file.write_text(_TIER_YAML)

    return PolicyEngine(presets_dir=presets_dir, tiers_file=tiers_file)


# ---------------------------------------------------------------------------
# Error: missing PolicyClient
# ---------------------------------------------------------------------------

def test_add_preset_requires_policy_client(preset_engine: PolicyEngine) -> None:
    mgr = PolicyManager(engine=preset_engine)
    with pytest.raises(PolicyManagerError, match="PolicyClient"):
        mgr.add_preset("box", "mybot")


def test_remove_preset_requires_policy_client(preset_engine: PolicyEngine) -> None:
    mgr = PolicyManager(engine=preset_engine)
    with pytest.raises(PolicyManagerError, match="PolicyClient"):
        mgr.remove_preset("box", "mybot")


def test_apply_tier_requires_policy_client(preset_engine: PolicyEngine) -> None:
    mgr = PolicyManager(engine=preset_engine)
    with pytest.raises(PolicyManagerError, match="PolicyClient"):
        mgr.apply_tier("box", "standard")


# ---------------------------------------------------------------------------
# add_preset
# ---------------------------------------------------------------------------

def test_add_preset_unknown_returns_error(preset_engine: PolicyEngine) -> None:
    fp = _FakePolicyClient()
    mgr = PolicyManager(fp, engine=preset_engine)
    result = mgr.add_preset("box", "nonexistent")
    assert result.ok is False
    assert "unknown" in result.error


def test_add_preset_calls_merge(preset_engine: PolicyEngine) -> None:
    _register("box")
    fp = _FakePolicyClient()
    mgr = PolicyManager(fp, engine=preset_engine)
    result = mgr.add_preset("box", "mybot")
    assert result.ok is True
    assert len(fp.merge_calls) == 1
    sandbox_name, ops = fp.merge_calls[0]
    assert sandbox_name == "box"
    assert len(ops) == 1


def test_add_preset_rule_name(preset_engine: PolicyEngine) -> None:
    _register("box")
    fp = _FakePolicyClient()
    mgr = PolicyManager(fp, engine=preset_engine)
    mgr.add_preset("box", "mybot")
    _, ops = fp.merge_calls[0]
    assert ops[0].rule_name == "mybot_api"


def test_add_preset_rule_proto_has_endpoints(preset_engine: PolicyEngine) -> None:
    _register("box")
    fp = _FakePolicyClient()
    mgr = PolicyManager(fp, engine=preset_engine)
    mgr.add_preset("box", "mybot")
    _, ops = fp.merge_calls[0]
    rule = ops[0].rule
    assert len(rule.endpoints) == 1
    assert rule.endpoints[0].host == "api.mybot.io"
    assert rule.endpoints[0].port == 443


def test_add_preset_rule_proto_has_binaries(preset_engine: PolicyEngine) -> None:
    _register("box")
    fp = _FakePolicyClient()
    mgr = PolicyManager(fp, engine=preset_engine)
    mgr.add_preset("box", "mybot")
    _, ops = fp.merge_calls[0]
    rule = ops[0].rule
    assert len(rule.binaries) == 1
    assert rule.binaries[0].path == "/usr/local/bin/node"


def test_add_preset_updates_registry(preset_engine: PolicyEngine) -> None:
    _register("box")
    fp = _FakePolicyClient()
    mgr = PolicyManager(fp, engine=preset_engine)
    mgr.add_preset("box", "mybot")
    entry = reg_mod.get_sandbox("box")
    assert entry is not None
    assert "mybot" in entry.policies


def test_add_preset_idempotent_in_registry(preset_engine: PolicyEngine) -> None:
    _register("box", policies=["mybot"])
    fp = _FakePolicyClient()
    mgr = PolicyManager(fp, engine=preset_engine)
    mgr.add_preset("box", "mybot")
    entry = reg_mod.get_sandbox("box")
    assert entry is not None
    assert entry.policies.count("mybot") == 1


def test_add_preset_gateway_failure_does_not_update_registry(preset_engine: PolicyEngine) -> None:
    _register("box")
    fp = _FakePolicyClient(fail_merge=True)
    mgr = PolicyManager(fp, engine=preset_engine)
    result = mgr.add_preset("box", "mybot")
    assert result.ok is False
    assert "merge failed" in result.error
    entry = reg_mod.get_sandbox("box")
    assert entry is not None
    assert "mybot" not in entry.policies


def test_add_preset_multi_rule_preset(preset_engine: PolicyEngine) -> None:
    _register("box")
    fp = _FakePolicyClient()
    mgr = PolicyManager(fp, engine=preset_engine)
    result = mgr.add_preset("box", "twobots")
    assert result.ok is True
    _, ops = fp.merge_calls[0]
    rule_names = {op.rule_name for op in ops}
    assert rule_names == {"rule_a", "rule_b"}


def test_add_preset_returns_applied_rule_names(preset_engine: PolicyEngine) -> None:
    _register("box")
    fp = _FakePolicyClient()
    mgr = PolicyManager(fp, engine=preset_engine)
    result = mgr.add_preset("box", "twobots")
    assert set(result.applied_rules) == {"rule_a", "rule_b"}


# ---------------------------------------------------------------------------
# remove_preset
# ---------------------------------------------------------------------------

def test_remove_preset_unknown_returns_error(preset_engine: PolicyEngine) -> None:
    fp = _FakePolicyClient()
    mgr = PolicyManager(fp, engine=preset_engine)
    result = mgr.remove_preset("box", "nonexistent")
    assert result.ok is False
    assert "unknown" in result.error


def test_remove_preset_calls_merge_with_remove_ops(preset_engine: PolicyEngine) -> None:
    _register("box", policies=["mybot"])
    fp = _FakePolicyClient()
    mgr = PolicyManager(fp, engine=preset_engine)
    result = mgr.remove_preset("box", "mybot")
    assert result.ok is True
    assert len(fp.merge_calls) == 1
    _, ops = fp.merge_calls[0]
    assert len(ops) == 1
    assert ops[0].rule_name == "mybot_api"


def test_remove_preset_op_has_no_rule_field(preset_engine: PolicyEngine) -> None:
    _register("box", policies=["mybot"])
    fp = _FakePolicyClient()
    mgr = PolicyManager(fp, engine=preset_engine)
    mgr.remove_preset("box", "mybot")
    _, ops = fp.merge_calls[0]
    assert not hasattr(ops[0], "rule")


def test_remove_preset_updates_registry(preset_engine: PolicyEngine) -> None:
    _register("box", policies=["mybot"])
    fp = _FakePolicyClient()
    mgr = PolicyManager(fp, engine=preset_engine)
    mgr.remove_preset("box", "mybot")
    entry = reg_mod.get_sandbox("box")
    assert entry is not None
    assert "mybot" not in entry.policies


def test_remove_preset_not_in_registry_still_ok(preset_engine: PolicyEngine) -> None:
    _register("box")  # mybot not in policies
    fp = _FakePolicyClient()
    mgr = PolicyManager(fp, engine=preset_engine)
    result = mgr.remove_preset("box", "mybot")
    assert result.ok is True


def test_remove_preset_gateway_failure_does_not_update_registry(preset_engine: PolicyEngine) -> None:
    _register("box", policies=["mybot"])
    fp = _FakePolicyClient(fail_merge=True)
    mgr = PolicyManager(fp, engine=preset_engine)
    result = mgr.remove_preset("box", "mybot")
    assert result.ok is False
    entry = reg_mod.get_sandbox("box")
    assert entry is not None
    assert "mybot" in entry.policies


def test_remove_preset_returns_removed_rule_names(preset_engine: PolicyEngine) -> None:
    _register("box", policies=["twobots"])
    fp = _FakePolicyClient()
    mgr = PolicyManager(fp, engine=preset_engine)
    result = mgr.remove_preset("box", "twobots")
    assert set(result.removed_rules) == {"rule_a", "rule_b"}


# ---------------------------------------------------------------------------
# apply_tier
# ---------------------------------------------------------------------------

def test_apply_tier_unknown_returns_error(preset_engine: PolicyEngine) -> None:
    fp = _FakePolicyClient()
    mgr = PolicyManager(fp, engine=preset_engine)
    result = mgr.apply_tier("box", "enterprise")
    assert result.ok is False
    assert "unknown" in result.error


def test_apply_tier_applies_all_presets(preset_engine: PolicyEngine) -> None:
    _register("box")
    fp = _FakePolicyClient()
    mgr = PolicyManager(fp, engine=preset_engine)
    result = mgr.apply_tier("box", "standard")
    assert result.ok is True
    assert set(result.applied_presets) == {"mybot", "twobots"}
    assert result.failed_presets == []


def test_apply_tier_calls_merge_per_preset(preset_engine: PolicyEngine) -> None:
    _register("box")
    fp = _FakePolicyClient()
    mgr = PolicyManager(fp, engine=preset_engine)
    mgr.apply_tier("box", "standard")
    assert len(fp.merge_calls) == 2


def test_apply_tier_partial_failure(tmp_path: pathlib.Path) -> None:
    _register("box")
    presets_dir = tmp_path / "presets2"
    presets_dir.mkdir()
    (presets_dir / "goodone.yaml").write_text(
        "preset:\n  name: goodone\nnetwork_policies:\n  r:\n    endpoints: []\n"
    )
    tiers_file = tmp_path / "tiers2.yaml"
    tiers_file.write_text(
        "tiers:\n  - name: mixed\n    label: Mixed\n    description: x\n"
        "    presets:\n      - name: goodone\n      - name: missingone\n"
    )
    engine = PolicyEngine(presets_dir=presets_dir, tiers_file=tiers_file)
    fp = _FakePolicyClient()
    mgr = PolicyManager(fp, engine=engine)
    result = mgr.apply_tier("box", "mixed")
    assert result.ok is False
    assert "goodone" in result.applied_presets
    assert "missingone" in result.failed_presets


def test_apply_tier_updates_registry_for_applied(preset_engine: PolicyEngine) -> None:
    _register("box")
    fp = _FakePolicyClient()
    mgr = PolicyManager(fp, engine=preset_engine)
    mgr.apply_tier("box", "standard")
    entry = reg_mod.get_sandbox("box")
    assert entry is not None
    assert "mybot" in entry.policies
    assert "twobots" in entry.policies


# ---------------------------------------------------------------------------
# list_applied_presets
# ---------------------------------------------------------------------------

def test_list_applied_presets_from_registry(preset_engine: PolicyEngine) -> None:
    _register("box", policies=["mybot", "twobots"])
    mgr = PolicyManager(engine=preset_engine)
    presets = mgr.list_applied_presets("box")
    assert set(presets) == {"mybot", "twobots"}


def test_list_applied_presets_empty_for_missing_sandbox(preset_engine: PolicyEngine) -> None:
    mgr = PolicyManager(engine=preset_engine)
    assert mgr.list_applied_presets("ghost") == []


def test_list_applied_presets_no_client_needed(preset_engine: PolicyEngine) -> None:
    _register("box", policies=["mybot"])
    mgr = PolicyManager(engine=preset_engine)  # no client
    assert mgr.list_applied_presets("box") == ["mybot"]


# ---------------------------------------------------------------------------
# get_available_presets / get_available_tiers
# ---------------------------------------------------------------------------

def test_get_available_presets(preset_engine: PolicyEngine) -> None:
    mgr = PolicyManager(engine=preset_engine)
    presets = mgr.get_available_presets()
    names = [p.name for p in presets]
    assert "mybot" in names
    assert "twobots" in names


def test_get_available_tiers(preset_engine: PolicyEngine) -> None:
    mgr = PolicyManager(engine=preset_engine)
    tiers = mgr.get_available_tiers()
    assert any(t.name == "standard" for t in tiers)


# ---------------------------------------------------------------------------
# _np_entry_to_proto
# ---------------------------------------------------------------------------

def test_np_entry_to_proto_basic() -> None:
    rule_dict = {
        "name": "test",
        "endpoints": [{"host": "x.com", "port": 443, "protocol": "rest"}],
        "binaries": [{"path": "/bin/node"}],
    }
    proto = _np_entry_to_proto("test", rule_dict)
    assert proto.name == "test"
    assert len(proto.endpoints) == 1
    assert proto.endpoints[0].host == "x.com"
    assert proto.endpoints[0].port == 443
    assert proto.endpoints[0].protocol == "rest"
    assert len(proto.binaries) == 1
    assert proto.binaries[0].path == "/bin/node"


def test_np_entry_to_proto_l7_rules() -> None:
    rule_dict = {
        "endpoints": [{
            "host": "api.x.com",
            "port": 443,
            "rules": [
                {"allow": {"method": "GET", "path": "/**"}},
                {"allow": {"method": "POST", "path": "/upload"}},
            ],
        }],
    }
    proto = _np_entry_to_proto("x", rule_dict)
    ep = proto.endpoints[0]
    assert len(ep.rules) == 2
    assert ep.rules[0].allow.method == "GET"
    assert ep.rules[1].allow.path == "/upload"


def test_np_entry_to_proto_access_tls() -> None:
    rule_dict = {
        "endpoints": [{"host": "wss.slack.com", "port": 443, "access": "full", "tls": "skip"}],
    }
    proto = _np_entry_to_proto("slack_wss", rule_dict)
    ep = proto.endpoints[0]
    assert ep.access == "full"
    assert ep.tls == "skip"


def test_np_entry_to_proto_binary_string_form() -> None:
    rule_dict = {
        "endpoints": [],
        "binaries": ["/usr/bin/python"],
    }
    proto = _np_entry_to_proto("py", rule_dict)
    assert proto.binaries[0].path == "/usr/bin/python"


def test_np_entry_to_proto_rule_name_fallback() -> None:
    rule_dict = {"endpoints": []}
    proto = _np_entry_to_proto("fallback_key", rule_dict)
    assert proto.name == "fallback_key"
