# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import pathlib
from typing import TYPE_CHECKING

import pytest

import nemoclaw.registry as reg_mod
from nemoclaw.registry import (
    CustomPolicyEntry,
    RegistryError,
    SandboxEntry,
    SandboxRegistry,
    _entry_from_dict,
    _entry_to_dict,
    _registry_from_dict,
    _registry_to_dict,
)

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the registry to a temp directory for each test."""
    registry_file = tmp_path / ".nemoclaw" / "sandboxes.json"
    monkeypatch.setattr(reg_mod, "REGISTRY_FILE", registry_file)
    monkeypatch.setattr(reg_mod, "_LOCK_DIR", pathlib.Path(str(registry_file) + ".lock"))
    monkeypatch.setattr(reg_mod, "_LOCK_OWNER", pathlib.Path(str(registry_file) + ".lock") / "owner")


def _make_entry(name: str = "my-box", **kwargs: object) -> SandboxEntry:
    return SandboxEntry(name=name, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Serialisation round-trips
# ---------------------------------------------------------------------------

def test_entry_round_trip_minimal() -> None:
    e = _make_entry()
    assert _entry_from_dict(_entry_to_dict(e)).name == "my-box"


def test_entry_round_trip_full() -> None:
    e = SandboxEntry(
        name="box",
        created_at="2026-01-01T00:00:00+00:00",
        model="nvidia/nemotron-super",
        provider="nvidia",
        gpu_enabled=True,
        policies=["slack", "discord"],
        custom_policies=[CustomPolicyEntry(name="cp1", content="yaml: true", applied_at="2026-01-01T00:00:00+00:00")],
        policy_tier="strict",
        disabled_channels=["discord"],
        dashboard_port=9090,
    )
    d = _entry_to_dict(e)
    assert d["model"] == "nvidia/nemotron-super"
    assert d["gpuEnabled"] is True
    assert d["policies"] == ["slack", "discord"]
    assert d["customPolicies"][0]["name"] == "cp1"
    assert d["disabledChannels"] == ["discord"]
    assert d["dashboardPort"] == 9090

    e2 = _entry_from_dict(d)
    assert e2.model == "nvidia/nemotron-super"
    assert e2.custom_policies[0].content == "yaml: true"
    assert e2.disabled_channels == ["discord"]


def test_registry_round_trip() -> None:
    reg = SandboxRegistry(
        sandboxes={"box": _make_entry("box")},
        default_sandbox="box",
    )
    assert _registry_from_dict(_registry_to_dict(reg)).default_sandbox == "box"


def test_entry_to_dict_omits_none_fields() -> None:
    d = _entry_to_dict(_make_entry())
    assert "model" not in d
    assert "nimContainer" not in d
    assert "dashboardPort" not in d


# ---------------------------------------------------------------------------
# load / save
# ---------------------------------------------------------------------------

def test_load_returns_empty_when_missing() -> None:
    result = reg_mod.load()
    assert result.sandboxes == {}
    assert result.default_sandbox is None


def test_load_reads_existing_file(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry_file = reg_mod.REGISTRY_FILE
    registry_file.parent.mkdir(parents=True, exist_ok=True)
    registry_file.write_text(json.dumps({
        "sandboxes": {"box": {"name": "box"}},
        "defaultSandbox": "box",
    }))
    result = reg_mod.load()
    assert "box" in result.sandboxes
    assert result.default_sandbox == "box"


def test_save_writes_json(tmp_path: pathlib.Path) -> None:
    r = SandboxRegistry(sandboxes={"b": _make_entry("b")}, default_sandbox="b")
    reg_mod.save(r)
    data = json.loads(reg_mod.REGISTRY_FILE.read_text())
    assert data["defaultSandbox"] == "b"
    assert "b" in data["sandboxes"]


# ---------------------------------------------------------------------------
# register_sandbox / get_sandbox / list_sandboxes
# ---------------------------------------------------------------------------

def test_register_and_get_sandbox() -> None:
    reg_mod.register_sandbox(_make_entry("alpha"))
    result = reg_mod.get_sandbox("alpha")
    assert result is not None
    assert result.name == "alpha"


def test_register_sets_created_at_when_absent() -> None:
    reg_mod.register_sandbox(_make_entry("alpha"))
    result = reg_mod.get_sandbox("alpha")
    assert result is not None
    assert result.created_at != ""


def test_register_first_sandbox_becomes_default() -> None:
    reg_mod.register_sandbox(_make_entry("alpha"))
    assert reg_mod.get_default() == "alpha"


def test_register_second_sandbox_keeps_first_as_default() -> None:
    reg_mod.register_sandbox(_make_entry("alpha"))
    reg_mod.register_sandbox(_make_entry("beta"))
    assert reg_mod.get_default() == "alpha"


def test_get_sandbox_returns_none_for_missing() -> None:
    assert reg_mod.get_sandbox("nope") is None


def test_list_sandboxes_returns_all() -> None:
    reg_mod.register_sandbox(_make_entry("a"))
    reg_mod.register_sandbox(_make_entry("b"))
    sandboxes, default = reg_mod.list_sandboxes()
    assert {s.name for s in sandboxes} == {"a", "b"}
    assert default == "a"


# ---------------------------------------------------------------------------
# update_sandbox
# ---------------------------------------------------------------------------

def test_update_sandbox_single_field() -> None:
    reg_mod.register_sandbox(_make_entry("box"))
    reg_mod.update_sandbox("box", model="nvidia/nemotron-super")
    assert reg_mod.get_sandbox("box").model == "nvidia/nemotron-super"  # type: ignore[union-attr]


def test_update_sandbox_returns_false_for_missing() -> None:
    assert reg_mod.update_sandbox("ghost", model="x") is False


def test_update_sandbox_rejects_name_change() -> None:
    reg_mod.register_sandbox(_make_entry("box"))
    result = reg_mod.update_sandbox("box", **{"name": "other"})
    assert result is False
    assert reg_mod.get_sandbox("box") is not None


# ---------------------------------------------------------------------------
# remove_sandbox / set_default / clear_all
# ---------------------------------------------------------------------------

def test_remove_sandbox() -> None:
    reg_mod.register_sandbox(_make_entry("box"))
    assert reg_mod.remove_sandbox("box") is True
    assert reg_mod.get_sandbox("box") is None


def test_remove_sandbox_promotes_next_default() -> None:
    reg_mod.register_sandbox(_make_entry("alpha"))
    reg_mod.register_sandbox(_make_entry("beta"))
    reg_mod.remove_sandbox("alpha")
    assert reg_mod.get_default() == "beta"


def test_remove_sandbox_returns_false_for_missing() -> None:
    assert reg_mod.remove_sandbox("ghost") is False


def test_set_default() -> None:
    reg_mod.register_sandbox(_make_entry("alpha"))
    reg_mod.register_sandbox(_make_entry("beta"))
    assert reg_mod.set_default("beta") is True
    assert reg_mod.get_default() == "beta"


def test_set_default_returns_false_for_missing() -> None:
    assert reg_mod.set_default("ghost") is False


def test_clear_all() -> None:
    reg_mod.register_sandbox(_make_entry("box"))
    reg_mod.clear_all()
    sandboxes, _ = reg_mod.list_sandboxes()
    assert sandboxes == []


# ---------------------------------------------------------------------------
# Custom policies
# ---------------------------------------------------------------------------

def test_add_and_get_custom_policy() -> None:
    reg_mod.register_sandbox(_make_entry("box"))
    cp = CustomPolicyEntry(name="slack", content="yaml: true")
    reg_mod.add_custom_policy("box", cp)
    result = reg_mod.get_custom_policies("box")
    assert len(result) == 1
    assert result[0].name == "slack"


def test_add_custom_policy_upserts() -> None:
    reg_mod.register_sandbox(_make_entry("box"))
    reg_mod.add_custom_policy("box", CustomPolicyEntry(name="slack", content="v1"))
    reg_mod.add_custom_policy("box", CustomPolicyEntry(name="slack", content="v2"))
    policies = reg_mod.get_custom_policies("box")
    assert len(policies) == 1
    assert policies[0].content == "v2"


def test_add_custom_policy_returns_false_for_missing_sandbox() -> None:
    assert reg_mod.add_custom_policy("ghost", CustomPolicyEntry(name="x", content="y")) is False


def test_remove_custom_policy() -> None:
    reg_mod.register_sandbox(_make_entry("box"))
    reg_mod.add_custom_policy("box", CustomPolicyEntry(name="slack", content="yaml: true"))
    assert reg_mod.remove_custom_policy("box", "slack") is True
    assert reg_mod.get_custom_policies("box") == []


def test_remove_custom_policy_returns_false_when_not_present() -> None:
    reg_mod.register_sandbox(_make_entry("box"))
    assert reg_mod.remove_custom_policy("box", "slack") is False


# ---------------------------------------------------------------------------
# Disabled channels
# ---------------------------------------------------------------------------

def test_set_channel_disabled_adds() -> None:
    reg_mod.register_sandbox(_make_entry("box"))
    reg_mod.set_channel_disabled("box", "discord", disabled=True)
    assert "discord" in reg_mod.get_disabled_channels("box")


def test_set_channel_disabled_removes() -> None:
    reg_mod.register_sandbox(_make_entry("box"))
    reg_mod.set_channel_disabled("box", "discord", disabled=True)
    reg_mod.set_channel_disabled("box", "discord", disabled=False)
    assert "discord" not in reg_mod.get_disabled_channels("box")


def test_set_channel_disabled_returns_false_for_missing() -> None:
    assert reg_mod.set_channel_disabled("ghost", "discord", disabled=True) is False


def test_get_disabled_channels_empty_by_default() -> None:
    reg_mod.register_sandbox(_make_entry("box"))
    assert reg_mod.get_disabled_channels("box") == []


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

def test_acquire_and_release_lock() -> None:
    reg_mod.acquire_lock()
    reg_mod.release_lock()
    # After release the lock dir must be gone
    assert not reg_mod._LOCK_DIR.exists()


def test_release_lock_idempotent() -> None:
    reg_mod.release_lock()  # no lock held — should not raise
