# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import pathlib
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator

_HOME = pathlib.Path(os.environ.get("HOME", "/tmp"))
REGISTRY_FILE = _HOME / ".nemoclaw" / "sandboxes.json"
_LOCK_DIR = pathlib.Path(str(REGISTRY_FILE) + ".lock")
_LOCK_OWNER = _LOCK_DIR / "owner"
_LOCK_STALE_SECS = 10.0
_LOCK_RETRY_SECS = 0.1
_LOCK_MAX_RETRIES = 120


class RegistryError(OSError):
    pass


@dataclass
class CustomPolicyEntry:
    name: str
    content: str
    source_path: str = ""
    applied_at: str = ""


@dataclass
class SandboxEntry:
    name: str
    created_at: str = ""
    model: str | None = None
    nim_container: str | None = None
    provider: str | None = None
    gpu_enabled: bool = False
    host_gpu_detected: bool = False
    sandbox_gpu_enabled: bool = False
    sandbox_gpu_mode: str | None = None
    sandbox_gpu_device: str | None = None
    openshell_driver: str | None = None
    openshell_version: str | None = None
    policies: list[str] = field(default_factory=list)
    custom_policies: list[CustomPolicyEntry] = field(default_factory=list)
    policy_tier: str | None = None
    agent: str | None = None
    agent_version: str | None = None
    image_tag: str | None = None
    provider_credential_hashes: dict[str, str] = field(default_factory=dict)
    messaging_channels: list[str] = field(default_factory=list)
    messaging_channel_config: dict[str, str] = field(default_factory=dict)
    disabled_channels: list[str] = field(default_factory=list)
    dashboard_port: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SandboxRegistry:
    sandboxes: dict[str, SandboxEntry] = field(default_factory=dict)
    default_sandbox: str | None = None


# --- JSON serialisation helpers ---

def _entry_to_dict(e: SandboxEntry) -> dict[str, Any]:
    """Convert a SandboxEntry to the camelCase JSON shape used on disk."""
    d: dict[str, Any] = {"name": e.name}
    if e.created_at:
        d["createdAt"] = e.created_at
    if e.model is not None:
        d["model"] = e.model
    if e.nim_container is not None:
        d["nimContainer"] = e.nim_container
    if e.provider is not None:
        d["provider"] = e.provider
    d["gpuEnabled"] = e.gpu_enabled
    d["hostGpuDetected"] = e.host_gpu_detected
    d["sandboxGpuEnabled"] = e.sandbox_gpu_enabled
    if e.sandbox_gpu_mode is not None:
        d["sandboxGpuMode"] = e.sandbox_gpu_mode
    if e.sandbox_gpu_device is not None:
        d["sandboxGpuDevice"] = e.sandbox_gpu_device
    if e.openshell_driver is not None:
        d["openshellDriver"] = e.openshell_driver
    if e.openshell_version is not None:
        d["openshellVersion"] = e.openshell_version
    d["policies"] = e.policies
    if e.custom_policies:
        d["customPolicies"] = [
            {k: v for k, v in {
                "name": cp.name,
                "content": cp.content,
                "sourcePath": cp.source_path or None,
                "appliedAt": cp.applied_at or None,
            }.items() if v is not None}
            for cp in e.custom_policies
        ]
    if e.policy_tier is not None:
        d["policyTier"] = e.policy_tier
    if e.agent is not None:
        d["agent"] = e.agent
    if e.agent_version is not None:
        d["agentVersion"] = e.agent_version
    if e.image_tag is not None:
        d["imageTag"] = e.image_tag
    if e.provider_credential_hashes:
        d["providerCredentialHashes"] = e.provider_credential_hashes
    d["messagingChannels"] = e.messaging_channels
    if e.messaging_channel_config:
        d["messagingChannelConfig"] = e.messaging_channel_config
    if e.disabled_channels:
        d["disabledChannels"] = sorted(e.disabled_channels)
    if e.dashboard_port is not None:
        d["dashboardPort"] = e.dashboard_port
    if e.metadata:
        d["metadata"] = e.metadata
    return d


def _entry_from_dict(d: dict[str, Any]) -> SandboxEntry:
    """Parse the camelCase JSON shape from disk into a SandboxEntry."""
    custom_policies = [
        CustomPolicyEntry(
            name=cp["name"],
            content=cp.get("content", ""),
            source_path=cp.get("sourcePath") or "",
            applied_at=cp.get("appliedAt") or "",
        )
        for cp in (d.get("customPolicies") or [])
    ]
    return SandboxEntry(
        name=d["name"],
        created_at=d.get("createdAt") or "",
        model=d.get("model"),
        nim_container=d.get("nimContainer"),
        provider=d.get("provider"),
        gpu_enabled=bool(d.get("gpuEnabled", False)),
        host_gpu_detected=bool(d.get("hostGpuDetected", False)),
        sandbox_gpu_enabled=bool(d.get("sandboxGpuEnabled", False)),
        sandbox_gpu_mode=d.get("sandboxGpuMode"),
        sandbox_gpu_device=d.get("sandboxGpuDevice"),
        openshell_driver=d.get("openshellDriver"),
        openshell_version=d.get("openshellVersion"),
        policies=list(d.get("policies") or []),
        custom_policies=custom_policies,
        policy_tier=d.get("policyTier"),
        agent=d.get("agent"),
        agent_version=d.get("agentVersion"),
        image_tag=d.get("imageTag"),
        provider_credential_hashes=dict(d.get("providerCredentialHashes") or {}),
        messaging_channels=list(d.get("messagingChannels") or []),
        messaging_channel_config=dict(d.get("messagingChannelConfig") or {}),
        disabled_channels=list(d.get("disabledChannels") or []),
        dashboard_port=d.get("dashboardPort"),
        metadata=dict(d.get("metadata") or {}),
    )


def _registry_to_dict(reg: SandboxRegistry) -> dict[str, Any]:
    return {
        "sandboxes": {k: _entry_to_dict(v) for k, v in reg.sandboxes.items()},
        "defaultSandbox": reg.default_sandbox,
    }


def _registry_from_dict(d: dict[str, Any]) -> SandboxRegistry:
    sandboxes = {k: _entry_from_dict(v) for k, v in (d.get("sandboxes") or {}).items()}
    return SandboxRegistry(sandboxes=sandboxes, default_sandbox=d.get("defaultSandbox"))


# --- File I/O ---

def _ensure_dir(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_raw() -> SandboxRegistry:
    if not REGISTRY_FILE.exists():
        return SandboxRegistry()
    try:
        data = json.loads(REGISTRY_FILE.read_text())
        if isinstance(data, dict):
            return _registry_from_dict(data)
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    return SandboxRegistry()


def _write_raw(reg: SandboxRegistry) -> None:
    _ensure_dir(REGISTRY_FILE.parent)
    tmp = REGISTRY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_registry_to_dict(reg), indent=2))
    tmp.rename(REGISTRY_FILE)


# --- Advisory locking (POSIX mkdir) ---

def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists but we lack permission to signal it


def acquire_lock() -> None:
    _ensure_dir(REGISTRY_FILE.parent)
    for _ in range(_LOCK_MAX_RETRIES):
        try:
            _LOCK_DIR.mkdir()
            owner_tmp = _LOCK_DIR / f"owner.tmp.{os.getpid()}"
            try:
                owner_tmp.write_text(str(os.getpid()))
                owner_tmp.rename(_LOCK_OWNER)
            except Exception:
                for path in (owner_tmp, _LOCK_OWNER):
                    try:
                        path.unlink()
                    except OSError:
                        pass
                try:
                    _LOCK_DIR.rmdir()
                except OSError:
                    pass
                raise
            return
        except FileExistsError:
            owner_checked = False
            try:
                raw = _LOCK_OWNER.read_text().strip()
                owner_pid = int(raw)
                if owner_pid > 0:
                    owner_checked = True
                    if not _is_pid_alive(owner_pid):
                        recheck = _LOCK_OWNER.read_text().strip()
                        if recheck == raw:
                            try:
                                import shutil
                                shutil.rmtree(_LOCK_DIR, ignore_errors=True)
                            except OSError:
                                pass
                            continue
            except (OSError, ValueError):
                pass
            if not owner_checked:
                try:
                    mtime = _LOCK_DIR.stat().st_mtime
                    if time.time() - mtime > _LOCK_STALE_SECS:
                        try:
                            import shutil
                            shutil.rmtree(_LOCK_DIR, ignore_errors=True)
                        except OSError:
                            pass
                        continue
                except OSError:
                    continue
            time.sleep(_LOCK_RETRY_SECS)
    raise RegistryError(
        f"Failed to acquire registry lock after {_LOCK_MAX_RETRIES} retries: {_LOCK_DIR}"
    )


def release_lock() -> None:
    try:
        _LOCK_OWNER.unlink()
    except FileNotFoundError:
        pass
    try:
        import shutil
        shutil.rmtree(_LOCK_DIR, ignore_errors=True)
    except OSError:
        pass


@contextmanager
def _locked() -> Iterator[None]:
    acquire_lock()
    try:
        yield
    finally:
        release_lock()


# --- Public API ---

def load() -> SandboxRegistry:
    """Return the current registry without acquiring the lock."""
    return _read_raw()


def save(reg: SandboxRegistry) -> None:
    """Persist the registry without acquiring the lock (caller must hold it)."""
    _write_raw(reg)


def get_sandbox(name: str) -> SandboxEntry | None:
    return load().sandboxes.get(name)


def get_default() -> str | None:
    reg = load()
    if reg.default_sandbox and reg.default_sandbox in reg.sandboxes:
        return reg.default_sandbox
    names = list(reg.sandboxes)
    return names[0] if names else None


def register_sandbox(entry: SandboxEntry) -> None:
    with _locked():
        reg = load()
        if not entry.created_at:
            import datetime
            entry = SandboxEntry(**{**asdict(entry), "created_at": datetime.datetime.now(datetime.UTC).isoformat()})
        reg.sandboxes[entry.name] = entry
        if not reg.default_sandbox:
            reg.default_sandbox = entry.name
        save(reg)


def update_sandbox(sandbox_name: str, **updates: Any) -> bool:
    with _locked():
        reg = load()
        if sandbox_name not in reg.sandboxes:
            return False
        entry = reg.sandboxes[sandbox_name]
        if "name" in updates and updates["name"] != sandbox_name:
            return False
        current = asdict(entry)
        current.update(updates)
        # Reconstruct nested dataclasses that asdict flattens
        current["custom_policies"] = [
            CustomPolicyEntry(**cp) if isinstance(cp, dict) else cp
            for cp in current.get("custom_policies", [])
        ]
        reg.sandboxes[sandbox_name] = SandboxEntry(**current)
        save(reg)
        return True


def remove_sandbox(name: str) -> bool:
    with _locked():
        reg = load()
        if name not in reg.sandboxes:
            return False
        del reg.sandboxes[name]
        if reg.default_sandbox == name:
            remaining = list(reg.sandboxes)
            reg.default_sandbox = remaining[0] if remaining else None
        save(reg)
        return True


def list_sandboxes() -> tuple[list[SandboxEntry], str | None]:
    """Return (sandboxes, default_sandbox_name)."""
    reg = load()
    return list(reg.sandboxes.values()), reg.default_sandbox


def set_default(name: str) -> bool:
    with _locked():
        reg = load()
        if name not in reg.sandboxes:
            return False
        reg.default_sandbox = name
        save(reg)
        return True


def clear_all() -> None:
    with _locked():
        save(SandboxRegistry())


def get_custom_policies(name: str) -> list[CustomPolicyEntry]:
    return load().sandboxes.get(name, SandboxEntry(name="")).custom_policies


def add_custom_policy(name: str, entry: CustomPolicyEntry) -> bool:
    with _locked():
        reg = load()
        sandbox = reg.sandboxes.get(name)
        if sandbox is None:
            return False
        import datetime
        applied_at = entry.applied_at or datetime.datetime.now(datetime.UTC).isoformat()
        new_entry = CustomPolicyEntry(
            name=entry.name,
            content=entry.content,
            source_path=entry.source_path,
            applied_at=applied_at,
        )
        updated = [cp for cp in sandbox.custom_policies if cp.name != entry.name]
        updated.append(new_entry)
        sandbox.custom_policies = updated
        save(reg)
        return True


def remove_custom_policy(sandbox_name: str, preset_name: str) -> bool:
    with _locked():
        reg = load()
        sandbox = reg.sandboxes.get(sandbox_name)
        if sandbox is None:
            return False
        before = len(sandbox.custom_policies)
        sandbox.custom_policies = [cp for cp in sandbox.custom_policies if cp.name != preset_name]
        if len(sandbox.custom_policies) == before:
            return False
        save(reg)
        return True


def get_disabled_channels(name: str) -> list[str]:
    return list(load().sandboxes.get(name, SandboxEntry(name="")).disabled_channels)


def set_channel_disabled(name: str, channel: str, disabled: bool) -> bool:
    with _locked():
        reg = load()
        entry = reg.sandboxes.get(name)
        if entry is None:
            return False
        current = set(entry.disabled_channels)
        if disabled:
            current.add(channel)
        else:
            current.discard(channel)
        entry.disabled_channels = sorted(current)
        save(reg)
        return True
