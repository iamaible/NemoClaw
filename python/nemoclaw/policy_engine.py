# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Pure-Python policy engine: preset discovery, tier resolution, and YAML
merge/remove logic.  No gRPC or proto imports — callers (e.g. PolicyManager)
handle proto conversion.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field
from typing import Any

import yaml


class PolicyEngineError(ValueError):
    pass


@dataclass(frozen=True)
class PresetInfo:
    file: str
    name: str
    description: str


@dataclass(frozen=True)
class TierPresetRef:
    name: str
    access: str = ""


@dataclass(frozen=True)
class TierInfo:
    name: str
    label: str
    description: str
    presets: tuple[TierPresetRef, ...] = field(default_factory=tuple)


# Public sentinel — returned when the policy YAML is empty / unparseable.
EMPTY_POLICY = "version: 1\n\nnetwork_policies:\n"


class PolicyEngine:
    """
    Discovers and resolves NemoClaw preset and tier definitions from disk.

    Parameters
    ----------
    presets_dir:
        Directory containing ``*.yaml`` preset files.
        Defaults to ``nemoclaw-blueprint/policies/presets/`` relative to this
        package's grandparent (i.e. the repo root).
    tiers_file:
        Path to ``tiers.yaml``.
        Defaults to ``nemoclaw-blueprint/policies/tiers.yaml`` relative to the
        repo root.
    """

    def __init__(
        self,
        presets_dir: pathlib.Path | str | None = None,
        tiers_file: pathlib.Path | str | None = None,
    ) -> None:
        _root = pathlib.Path(__file__).parents[2]
        self._presets_dir = (
            pathlib.Path(presets_dir)
            if presets_dir is not None
            else _root / "nemoclaw-blueprint" / "policies" / "presets"
        )
        self._tiers_file = (
            pathlib.Path(tiers_file)
            if tiers_file is not None
            else _root / "nemoclaw-blueprint" / "policies" / "tiers.yaml"
        )

    # ------------------------------------------------------------------
    # Preset discovery
    # ------------------------------------------------------------------

    def list_presets(self) -> list[PresetInfo]:
        """Return metadata for every ``*.yaml`` preset in the presets directory."""
        if not self._presets_dir.is_dir():
            return []
        results: list[PresetInfo] = []
        for f in sorted(self._presets_dir.glob("*.yaml")):
            try:
                content = f.read_text()
                doc = yaml.safe_load(content) or {}
                preset_meta = doc.get("preset") or {}
                name = preset_meta.get("name") or f.stem
                description = preset_meta.get("description") or ""
            except Exception:
                name = f.stem
                description = ""
            results.append(PresetInfo(file=f.name, name=name, description=description))
        return results

    def load_preset(self, name: str) -> str | None:
        """
        Read preset YAML content by short name.  Guards against path traversal.
        Returns ``None`` if the preset does not exist.
        """
        candidate = (self._presets_dir / f"{name}.yaml").resolve()
        if not str(candidate).startswith(str(self._presets_dir.resolve())):
            return None
        if not candidate.exists():
            return None
        return candidate.read_text()

    def get_preset_endpoints(self, content: str) -> list[str]:
        """Extract bare hostnames declared in a preset YAML (for user display)."""
        hosts: list[str] = []
        for match in re.finditer(r"host:\s*([^\s,}]+)", content):
            hosts.append(match.group(1).strip("'\""))
        return hosts

    # ------------------------------------------------------------------
    # Tier resolution
    # ------------------------------------------------------------------

    def _load_tiers_raw(self) -> list[dict[str, Any]]:
        if not self._tiers_file.exists():
            return []
        try:
            doc = yaml.safe_load(self._tiers_file.read_text()) or {}
            return list(doc.get("tiers") or [])
        except yaml.YAMLError as exc:
            raise PolicyEngineError(f"invalid YAML in tiers file: {exc}") from exc

    def list_tiers(self) -> list[TierInfo]:
        """Return all tier definitions from ``tiers.yaml``."""
        results: list[TierInfo] = []
        for t in self._load_tiers_raw():
            presets = tuple(
                TierPresetRef(name=p["name"], access=p.get("access") or "")
                for p in (t.get("presets") or [])
            )
            results.append(
                TierInfo(
                    name=t.get("name") or "",
                    label=t.get("label") or "",
                    description=t.get("description") or "",
                    presets=presets,
                )
            )
        return results

    def get_tier(self, name: str) -> TierInfo | None:
        """Return a single tier by name, or ``None`` if not found."""
        for tier in self.list_tiers():
            if tier.name == name:
                return tier
        return None

    def get_tier_preset_names(self, tier_name: str) -> list[str]:
        """Return the ordered list of preset names for a given tier."""
        tier = self.get_tier(tier_name)
        if tier is None:
            return []
        return [p.name for p in tier.presets]

    # ------------------------------------------------------------------
    # YAML merge / remove
    # ------------------------------------------------------------------

    @staticmethod
    def extract_network_policies(content: str | None) -> dict[str, Any] | None:
        """
        Parse the ``network_policies`` section from a preset YAML string.

        Returns a ``dict`` keyed by rule name, or ``None`` if the section is
        absent or unparseable.
        """
        if not content:
            return None
        try:
            doc = yaml.safe_load(content) or {}
        except yaml.YAMLError:
            return None
        np = doc.get("network_policies")
        if not isinstance(np, dict) or not np:
            return None
        return np

    @staticmethod
    def _parse_current_policy(raw: str | None) -> str:
        """
        Normalise raw ``openshell policy get --full`` output: strip the
        Version/Hash metadata header (everything before the first ``---`` line)
        and reject obviously invalid content.
        """
        if not raw:
            return ""
        sep = raw.find("---")
        candidate = (raw[sep + 3:] if sep != -1 else raw).strip()
        if not candidate:
            return ""
        if re.match(r"^(error|failed|invalid|warning|status)\b", candidate, re.IGNORECASE):
            return ""
        if not re.search(r"^[a-z_][a-z0-9_]*\s*:", candidate, re.MULTILINE):
            return ""
        try:
            parsed = yaml.safe_load(candidate)
        except yaml.YAMLError:
            return ""
        if not isinstance(parsed, dict):
            return ""
        return candidate

    @staticmethod
    def _text_merge(current_policy: str, preset_entries_yaml: str) -> str:
        """Fallback text-based merge when structured YAML parsing fails."""
        if not current_policy:
            return "version: 1\n\nnetwork_policies:\n" + preset_entries_yaml
        if re.search(r"^network_policies\s*:", current_policy, re.MULTILINE):
            lines = current_policy.split("\n")
            result: list[str] = []
            in_np = False
            inserted = False
            for line in lines:
                if re.match(r"^network_policies\s*:", line):
                    in_np = True
                    result.append(line)
                    continue
                if in_np and re.match(r"^\S.*:", line) and not inserted:
                    result.append(preset_entries_yaml)
                    inserted = True
                    in_np = False
                result.append(line)
            if in_np and not inserted:
                result.append(preset_entries_yaml)
            merged = "\n".join(result)
        else:
            merged = current_policy.rstrip() + "\n\nnetwork_policies:\n" + preset_entries_yaml
        if not merged.lstrip().startswith("version:"):
            merged = "version: 1\n\n" + merged
        return merged

    def merge_preset_into_policy(
        self,
        current_policy_raw: str,
        preset_name: str,
    ) -> str:
        """
        Load ``preset_name`` from disk and merge its ``network_policies``
        entries into ``current_policy_raw``.  Returns the merged YAML string.
        Raises :exc:`PolicyEngineError` if the preset cannot be loaded or has
        no ``network_policies`` section.
        """
        content = self.load_preset(preset_name)
        if content is None:
            raise PolicyEngineError(f"preset not found: {preset_name!r}")
        np = self.extract_network_policies(content)
        if np is None:
            raise PolicyEngineError(
                f"preset {preset_name!r} has no network_policies section"
            )
        return self._merge_network_policies(current_policy_raw, np)

    def remove_preset_from_policy(
        self,
        current_policy_raw: str,
        preset_name: str,
        *,
        custom_content: str | None = None,
    ) -> str:
        """
        Remove a preset's ``network_policies`` keys from ``current_policy_raw``.
        Looks up the preset in the built-in presets directory; if not found and
        ``custom_content`` is provided, parses it from there.

        Returns the updated YAML string.
        Raises :exc:`PolicyEngineError` if no content can be found for the preset.
        """
        content = self.load_preset(preset_name)
        if content is None:
            if custom_content is not None:
                content = custom_content
            else:
                raise PolicyEngineError(f"preset not found: {preset_name!r}")
        np = self.extract_network_policies(content)
        if np is None:
            raise PolicyEngineError(
                f"preset {preset_name!r} has no network_policies section"
            )
        return self._remove_network_policies(current_policy_raw, set(np.keys()))

    def merge_preset_names_into_policy(
        self,
        current_policy_raw: str,
        preset_names: list[str],
    ) -> tuple[str, list[str], list[str]]:
        """
        Apply multiple presets in order.

        Returns ``(merged_yaml, applied_names, missing_names)``.
        """
        merged = current_policy_raw
        applied: list[str] = []
        missing: list[str] = []
        seen: set[str] = set()
        for name in preset_names:
            if name in seen:
                continue
            seen.add(name)
            try:
                merged = self.merge_preset_into_policy(merged, name)
                applied.append(name)
            except PolicyEngineError:
                missing.append(name)
        return merged, applied, missing

    # ------------------------------------------------------------------
    # Internal YAML helpers
    # ------------------------------------------------------------------

    def _merge_network_policies(
        self,
        current_policy_raw: str,
        preset_np: dict[str, Any],
    ) -> str:
        current = self._parse_current_policy(current_policy_raw)
        if not current:
            return yaml.dump({"version": 1, "network_policies": preset_np}, default_flow_style=False)

        try:
            doc = yaml.safe_load(current)
        except yaml.YAMLError:
            entries_yaml = yaml.dump({"network_policies": preset_np})[len("network_policies:\n"):]
            return self._text_merge(current, entries_yaml)

        if not isinstance(doc, dict):
            entries_yaml = yaml.dump({"network_policies": preset_np})[len("network_policies:\n"):]
            return self._text_merge(current, entries_yaml)

        existing_np = doc.get("network_policies") or {}
        if isinstance(existing_np, dict):
            doc["network_policies"] = {**existing_np, **preset_np}
        else:
            doc["network_policies"] = preset_np
        doc.setdefault("version", 1)
        return yaml.dump(doc, default_flow_style=False)

    def _remove_network_policies(
        self,
        current_policy_raw: str,
        keys_to_remove: set[str],
    ) -> str:
        current = self._parse_current_policy(current_policy_raw)
        if not current:
            return EMPTY_POLICY

        try:
            doc = yaml.safe_load(current)
        except yaml.YAMLError:
            return current

        if not isinstance(doc, dict):
            return current

        existing_np = doc.get("network_policies")
        if not isinstance(existing_np, dict):
            return current

        for key in keys_to_remove:
            existing_np.pop(key, None)
        doc["network_policies"] = existing_np
        return yaml.dump(doc, default_flow_style=False)

    # ------------------------------------------------------------------
    # Applied-preset introspection
    # ------------------------------------------------------------------

    def get_applied_preset_names(self, current_policy_raw: str) -> list[str]:
        """
        Return which built-in presets are fully represented in the given policy
        YAML (all ``network_policies`` keys from a preset must be present).

        Returns ``[]`` when the policy is unreachable / unparseable, matching
        the TypeScript behaviour that returns ``null`` for gateway-error vs
        ``[]`` for empty policy — callers may distinguish by checking whether
        ``current_policy_raw`` is empty.
        """
        current = self._parse_current_policy(current_policy_raw)
        if not current:
            return []

        try:
            doc = yaml.safe_load(current)
        except yaml.YAMLError:
            return []

        if not isinstance(doc, dict):
            return []

        gateway_np = doc.get("network_policies")
        if not isinstance(gateway_np, dict):
            return []

        gateway_keys = set(gateway_np.keys())
        matched: list[str] = []
        for preset in self.list_presets():
            content = self.load_preset(preset.name)
            if content is None:
                continue
            np = self.extract_network_policies(content)
            if not np:
                continue
            if np.keys() and all(k in gateway_keys for k in np):
                matched.append(preset.name)
        return matched
