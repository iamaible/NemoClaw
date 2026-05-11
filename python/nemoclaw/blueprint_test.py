# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING

import pytest

from nemoclaw.blueprint import Blueprint, BlueprintError, load, load_string

if TYPE_CHECKING:
    pass

_REAL_BLUEPRINT = (
    pathlib.Path(__file__).parents[2]
    / "nemoclaw-blueprint"
    / "blueprint.yaml"
)

_MINIMAL = """\
version: "0.1.0"
components:
  sandbox:
    image: "ghcr.io/example/sandbox:latest"
    name: "test"
"""

_FULL = """\
version: "0.1.0"
min_openshell_version: "0.0.37"
max_openshell_version: "0.0.37"
digest: "sha256:abc"
profiles:
  - default
  - nim-local
description: "test blueprint"
components:
  sandbox:
    image: "ghcr.io/example/sandbox:latest"
    name: "mybox"
    forward_ports:
      - 18789
  inference:
    profiles:
      default:
        provider_type: "nvidia"
        provider_name: "nvidia-inference"
        endpoint: "https://integrate.api.nvidia.com/v1"
        model: "nvidia/nemotron-super"
      nim-local:
        provider_type: "openai"
        provider_name: "nim-local"
        endpoint: "http://nim-service.local:8000/v1"
        model: "nvidia/nemotron-super"
        credential_env: "NIM_API_KEY"
        timeout_secs: 180
  router:
    enabled: true
    port: 4000
    pool_config_path: "router/pool-config.yaml"
  policy:
    base: "sandboxes/openclaw/policy.yaml"
    additions:
      nim_service:
        name: nim_service
        endpoints:
          - host: "nim-service.local"
            port: 8000
            access: full
"""


# --- load_string ---

def test_load_string_minimal() -> None:
    bp = load_string(_MINIMAL)
    assert isinstance(bp, Blueprint)
    assert bp.version == "0.1.0"
    assert bp.components.sandbox.image == "ghcr.io/example/sandbox:latest"


def test_load_string_full() -> None:
    bp = load_string(_FULL)
    assert bp.profiles == ["default", "nim-local"]
    assert bp.components.sandbox.forward_ports == [18789]
    assert bp.components.router.port == 4000
    assert bp.components.policy.base == "sandboxes/openclaw/policy.yaml"


def test_load_string_inference_profiles() -> None:
    bp = load_string(_FULL)
    profiles = bp.components.inference.profiles
    assert "default" in profiles
    assert profiles["default"].provider_type == "nvidia"
    assert profiles["nim-local"].timeout_secs == 180
    assert profiles["nim-local"].credential_env == "NIM_API_KEY"


def test_load_string_policy_additions() -> None:
    bp = load_string(_FULL)
    additions = bp.components.policy.additions
    assert "nim_service" in additions
    ep = additions["nim_service"].endpoints[0]
    assert ep.host == "nim-service.local"
    assert ep.port == 8000
    assert ep.access == "full"


def test_profile_helper_returns_matching_profile() -> None:
    bp = load_string(_FULL)
    p = bp.profile("nim-local")
    assert p.provider_name == "nim-local"


def test_profile_helper_raises_on_missing() -> None:
    bp = load_string(_FULL)
    with pytest.raises(KeyError, match="not found"):
        bp.profile("nonexistent")


def test_empty_components_get_defaults() -> None:
    bp = load_string("version: '0.1.0'\n")
    assert bp.components.sandbox.image == ""
    assert bp.components.router.port == 4000
    assert bp.components.router.enabled is True
    assert bp.components.inference.profiles == {}


def test_load_string_invalid_yaml_raises() -> None:
    with pytest.raises(BlueprintError, match="invalid YAML"):
        load_string("key: [unclosed")


def test_load_string_non_mapping_raises() -> None:
    with pytest.raises(BlueprintError, match="mapping"):
        load_string("- item1\n- item2\n")


# --- load (file) ---

def test_load_missing_file_raises() -> None:
    with pytest.raises(BlueprintError, match="not found"):
        load("/nonexistent/path/blueprint.yaml")


def test_load_file(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "blueprint.yaml"
    f.write_text(_MINIMAL)
    bp = load(f)
    assert bp.components.sandbox.name == "test"


# --- real blueprint ---

def test_load_real_blueprint() -> None:
    if not _REAL_BLUEPRINT.exists():
        pytest.skip("nemoclaw-blueprint/blueprint.yaml not found")
    bp = load(_REAL_BLUEPRINT)
    assert bp.version
    assert bp.digest.startswith("sha256:")
    assert "default" in bp.components.inference.profiles
    assert bp.components.sandbox.image
