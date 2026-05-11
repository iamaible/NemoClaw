# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pathlib
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator


class InferenceProfile(BaseModel):
    provider_type: str = ""
    provider_name: str = ""
    endpoint: str = ""
    model: str = ""
    credential_env: str = ""
    credential_default: str = ""
    timeout_secs: int | None = None
    dynamic_endpoint: bool = False


class InferenceConfig(BaseModel):
    profiles: dict[str, InferenceProfile] = Field(default_factory=dict)


class SandboxSpec(BaseModel):
    image: str = ""
    name: str = ""
    forward_ports: list[int] = Field(default_factory=list)


class RouterConfig(BaseModel):
    enabled: bool = True
    port: int = 4000
    pool_config_path: str = ""


class PolicyEndpoint(BaseModel):
    host: str
    port: int
    protocol: str = ""
    enforcement: str = ""
    tls: str = ""
    access: str = ""
    allowed_ips: list[str] = Field(default_factory=list)
    rules: list[dict[str, Any]] = Field(default_factory=list)
    deny_rules: list[dict[str, Any]] = Field(default_factory=list)
    ports: list[int] = Field(default_factory=list)


class PolicyBinary(BaseModel):
    path: str


class PolicyAddition(BaseModel):
    name: str
    endpoints: list[PolicyEndpoint] = Field(default_factory=list)
    binaries: list[PolicyBinary] = Field(default_factory=list)


class PolicyConfig(BaseModel):
    base: str = ""
    additions: dict[str, PolicyAddition] = Field(default_factory=dict)


class Components(BaseModel):
    sandbox: SandboxSpec = Field(default_factory=SandboxSpec)
    inference: InferenceConfig = Field(default_factory=InferenceConfig)
    router: RouterConfig = Field(default_factory=RouterConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)


class Blueprint(BaseModel):
    version: str = ""
    min_openshell_version: str = ""
    max_openshell_version: str = ""
    min_openclaw_version: str = ""
    digest: str = ""
    profiles: list[str] = Field(default_factory=list)
    description: str = ""
    components: Components = Field(default_factory=Components)

    @model_validator(mode="before")
    @classmethod
    def _coerce_components(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # Ensure nested dicts are present for optional top-level keys.
        data.setdefault("components", {})
        comps = data["components"]
        comps.setdefault("sandbox", {})
        comps.setdefault("inference", {})
        comps.setdefault("router", {})
        comps.setdefault("policy", {})
        return data

    def profile(self, name: str) -> InferenceProfile:
        try:
            return self.components.inference.profiles[name]
        except KeyError:
            available = list(self.components.inference.profiles)
            raise KeyError(f"inference profile {name!r} not found; available: {available}") from None


class BlueprintError(ValueError):
    pass


def load(path: str | pathlib.Path) -> Blueprint:
    """Parse and validate blueprint.yaml from *path*."""
    p = pathlib.Path(path)
    if not p.exists():
        raise BlueprintError(f"blueprint file not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as exc:
        raise BlueprintError(f"invalid YAML in {p}: {exc}") from exc
    if not isinstance(raw, dict):
        raise BlueprintError(f"blueprint must be a YAML mapping, got {type(raw).__name__}")
    try:
        return Blueprint.model_validate(raw)
    except Exception as exc:
        raise BlueprintError(f"blueprint validation failed: {exc}") from exc


def load_string(content: str) -> Blueprint:
    """Parse and validate blueprint YAML from a string (useful for K8s ConfigMap mounts)."""
    try:
        raw = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise BlueprintError(f"invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise BlueprintError(f"blueprint must be a YAML mapping, got {type(raw).__name__}")
    try:
        return Blueprint.model_validate(raw)
    except Exception as exc:
        raise BlueprintError(f"blueprint validation failed: {exc}") from exc
