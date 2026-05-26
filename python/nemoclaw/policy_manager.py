# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
PolicyManager: apply, remove, and list network-policy presets on a sandbox.

Combines :class:`nemoclaw.policy_engine.PolicyEngine` (YAML preset parsing)
with :class:`openshell.policy.PolicyClient` (gRPC merge operations) so callers
don't have to build proto objects themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from nemoclaw import registry as _registry
from nemoclaw.policy_engine import PolicyEngine, PresetInfo, TierInfo

if TYPE_CHECKING:
    from openshell.policy import PolicyClient


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PolicyAddResult:
    ok: bool
    preset: str
    applied_rules: list[str] = field(default_factory=list)
    error: str = ""


@dataclass(frozen=True)
class PolicyRemoveResult:
    ok: bool
    preset: str
    removed_rules: list[str] = field(default_factory=list)
    error: str = ""


@dataclass(frozen=True)
class PolicyApplyTierResult:
    ok: bool
    tier: str
    applied_presets: list[str] = field(default_factory=list)
    failed_presets: list[str] = field(default_factory=list)
    error: str = ""


# ---------------------------------------------------------------------------
# PolicyManagerError
# ---------------------------------------------------------------------------

class PolicyManagerError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# PolicyManager
# ---------------------------------------------------------------------------

class PolicyManager:
    """
    Manages network-policy preset lifecycle for a sandbox.

    Parameters
    ----------
    policy_client:
        An initialised :class:`openshell.policy.PolicyClient`.  When ``None``,
        :meth:`add_preset`, :meth:`remove_preset`, and :meth:`apply_tier` raise
        :exc:`PolicyManagerError`.  Read-only methods always work.
    engine:
        A :class:`~nemoclaw.policy_engine.PolicyEngine` instance.  Defaults to
        the standard engine (reads from the bundled ``nemoclaw-blueprint``).
    """

    def __init__(
        self,
        policy_client: "PolicyClient | None" = None,
        engine: PolicyEngine | None = None,
    ) -> None:
        self._client = policy_client
        self._engine = engine or PolicyEngine()

    # ------------------------------------------------------------------
    # Mutating operations
    # ------------------------------------------------------------------

    def add_preset(self, sandbox_name: str, preset_name: str) -> PolicyAddResult:
        """
        Apply a preset's network-policy rules to a sandbox via gRPC merge.

        Loads the preset from disk, converts each ``network_policies`` entry
        to an ``AddNetworkRule`` merge operation, calls
        ``policy_client.merge()``, then records the preset in the registry.
        """
        if self._client is None:
            raise PolicyManagerError(
                "add_preset requires a PolicyClient; construct PolicyManager with one."
            )

        content = self._engine.load_preset(preset_name)
        if content is None:
            return PolicyAddResult(
                ok=False,
                preset=preset_name,
                error=f"unknown preset {preset_name!r}",
            )

        np = self._engine.extract_network_policies(content)
        if not np:
            return PolicyAddResult(
                ok=False,
                preset=preset_name,
                error=f"preset {preset_name!r} has no network_policies section",
            )

        from openshell.policy import AddNetworkRule  # noqa: PLC0415

        ops = [
            AddNetworkRule(rule_name=k, rule=_np_entry_to_proto(k, v))
            for k, v in np.items()
        ]

        try:
            self._client.merge(sandbox_name, ops)
        except Exception as exc:  # noqa: BLE001
            return PolicyAddResult(ok=False, preset=preset_name, error=str(exc))

        self._registry_add_preset(sandbox_name, preset_name)
        return PolicyAddResult(ok=True, preset=preset_name, applied_rules=list(np.keys()))

    def remove_preset(self, sandbox_name: str, preset_name: str) -> PolicyRemoveResult:
        """
        Remove a preset's network-policy rules from a sandbox via gRPC merge.

        Loads the preset to discover which rule keys to remove, issues
        ``RemoveNetworkRule`` merge operations, then removes the preset from
        the registry.
        """
        if self._client is None:
            raise PolicyManagerError(
                "remove_preset requires a PolicyClient; construct PolicyManager with one."
            )

        content = self._engine.load_preset(preset_name)
        if content is None:
            return PolicyRemoveResult(
                ok=False,
                preset=preset_name,
                error=f"unknown preset {preset_name!r}",
            )

        np = self._engine.extract_network_policies(content)
        if not np:
            return PolicyRemoveResult(
                ok=False,
                preset=preset_name,
                error=f"preset {preset_name!r} has no network_policies section",
            )

        from openshell.policy import RemoveNetworkRule  # noqa: PLC0415

        ops = [RemoveNetworkRule(rule_name=k) for k in np.keys()]

        try:
            self._client.merge(sandbox_name, ops)
        except Exception as exc:  # noqa: BLE001
            return PolicyRemoveResult(ok=False, preset=preset_name, error=str(exc))

        self._registry_remove_preset(sandbox_name, preset_name)
        return PolicyRemoveResult(ok=True, preset=preset_name, removed_rules=list(np.keys()))

    def apply_tier(self, sandbox_name: str, tier_name: str) -> PolicyApplyTierResult:
        """
        Apply every preset in a tier, in order.

        Partial success is reported when some presets are missing or their
        gateway merge fails.
        """
        if self._client is None:
            raise PolicyManagerError(
                "apply_tier requires a PolicyClient; construct PolicyManager with one."
            )

        tier = self._engine.get_tier(tier_name)
        if tier is None:
            return PolicyApplyTierResult(
                ok=False,
                tier=tier_name,
                error=f"unknown tier {tier_name!r}",
            )

        applied: list[str] = []
        failed: list[str] = []
        for preset_ref in tier.presets:
            result = self.add_preset(sandbox_name, preset_ref.name)
            if result.ok:
                applied.append(preset_ref.name)
            else:
                failed.append(preset_ref.name)

        return PolicyApplyTierResult(
            ok=not failed,
            tier=tier_name,
            applied_presets=applied,
            failed_presets=failed,
        )

    # ------------------------------------------------------------------
    # Read-only queries
    # ------------------------------------------------------------------

    def list_applied_presets(self, sandbox_name: str) -> list[str]:
        """Return the preset names stored in the registry for *sandbox_name*."""
        entry = _registry.get_sandbox(sandbox_name)
        return list(entry.policies) if entry else []

    def get_available_presets(self) -> list[PresetInfo]:
        """Return metadata for all presets discoverable by the engine."""
        return self._engine.list_presets()

    def get_available_tiers(self) -> list[TierInfo]:
        """Return all tier definitions from the engine's tiers file."""
        return self._engine.list_tiers()

    # ------------------------------------------------------------------
    # Registry helpers
    # ------------------------------------------------------------------

    def _registry_add_preset(self, sandbox_name: str, preset_name: str) -> None:
        entry = _registry.get_sandbox(sandbox_name)
        if entry is None:
            return
        applied = set(entry.policies)
        applied.add(preset_name)
        _registry.update_sandbox(sandbox_name, policies=sorted(applied))

    def _registry_remove_preset(self, sandbox_name: str, preset_name: str) -> None:
        entry = _registry.get_sandbox(sandbox_name)
        if entry is None:
            return
        applied = [p for p in entry.policies if p != preset_name]
        _registry.update_sandbox(sandbox_name, policies=applied)


# ---------------------------------------------------------------------------
# YAML → proto conversion
# ---------------------------------------------------------------------------

def _np_entry_to_proto(rule_name: str, rule_dict: dict[str, Any]) -> Any:
    """Convert one network_policies YAML entry to a sandbox_pb2.NetworkPolicyRule."""
    from openshell._proto import sandbox_pb2  # noqa: PLC0415

    endpoints = []
    for ep in rule_dict.get("endpoints") or []:
        l7_rules = []
        for r in ep.get("rules") or []:
            allow_data = r.get("allow")
            if isinstance(allow_data, dict):
                allow = sandbox_pb2.L7Allow(
                    method=str(allow_data.get("method") or ""),
                    path=str(allow_data.get("path") or ""),
                )
                l7_rules.append(sandbox_pb2.L7Rule(allow=allow))

        deny_rules = []
        for dr in ep.get("deny_rules") or []:
            deny_rules.append(sandbox_pb2.L7DenyRule(
                method=str(dr.get("method") or ""),
                path=str(dr.get("path") or ""),
            ))

        endpoints.append(sandbox_pb2.NetworkEndpoint(
            host=str(ep.get("host") or ""),
            port=int(ep.get("port") or 0),
            protocol=str(ep.get("protocol") or ""),
            tls=str(ep.get("tls") or ""),
            enforcement=str(ep.get("enforcement") or ""),
            access=str(ep.get("access") or ""),
            rules=l7_rules,
            deny_rules=deny_rules,
            allowed_ips=list(ep.get("allowed_ips") or []),
            ports=[int(p) for p in (ep.get("ports") or [])],
        ))

    binaries = []
    for b in rule_dict.get("binaries") or []:
        path = b if isinstance(b, str) else str(b.get("path") or "")
        binaries.append(sandbox_pb2.NetworkBinary(path=path))

    return sandbox_pb2.NetworkPolicyRule(
        name=str(rule_dict.get("name") or rule_name),
        endpoints=endpoints,
        binaries=binaries,
    )
