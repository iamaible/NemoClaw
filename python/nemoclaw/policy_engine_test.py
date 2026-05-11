# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pathlib
import textwrap

import pytest
import yaml

from nemoclaw.policy_engine import (
    EMPTY_POLICY,
    PolicyEngine,
    PolicyEngineError,
    PresetInfo,
    TierInfo,
    TierPresetRef,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SLACK_PRESET = textwrap.dedent("""\
    preset:
      name: slack
      description: "Slack API access"

    network_policies:
      slack:
        name: slack
        endpoints:
          - host: slack.com
            port: 443
            protocol: rest
            enforcement: enforce
            rules:
              - allow: { method: GET, path: "/**" }
              - allow: { method: POST, path: "/**" }
        binaries:
          - { path: /usr/local/bin/node }
""")

_DISCORD_PRESET = textwrap.dedent("""\
    preset:
      name: discord
      description: "Discord API access"

    network_policies:
      discord:
        name: discord
        endpoints:
          - host: discord.com
            port: 443
            access: full
""")

_TIERS_YAML = textwrap.dedent("""\
    tiers:
      - name: restricted
        label: Restricted
        description: No third-party access.
        presets: []

      - name: balanced
        label: Balanced
        description: Dev tooling and web search.
        presets:
          - { name: slack, access: read-write }
          - { name: discord, access: read-write }
""")

_EXISTING_POLICY = textwrap.dedent("""\
    version: 1
    network_policies:
      github:
        name: github
        endpoints:
          - host: github.com
            port: 443
""")


@pytest.fixture()
def preset_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / "presets"
    d.mkdir()
    (d / "slack.yaml").write_text(_SLACK_PRESET)
    (d / "discord.yaml").write_text(_DISCORD_PRESET)
    return d


@pytest.fixture()
def tiers_file(tmp_path: pathlib.Path) -> pathlib.Path:
    f = tmp_path / "tiers.yaml"
    f.write_text(_TIERS_YAML)
    return f


@pytest.fixture()
def engine(preset_dir: pathlib.Path, tiers_file: pathlib.Path) -> PolicyEngine:
    return PolicyEngine(presets_dir=preset_dir, tiers_file=tiers_file)


# ---------------------------------------------------------------------------
# Preset discovery
# ---------------------------------------------------------------------------

def test_list_presets_returns_all(engine: PolicyEngine) -> None:
    presets = engine.list_presets()
    names = {p.name for p in presets}
    assert names == {"slack", "discord"}


def test_list_presets_populates_description(engine: PolicyEngine) -> None:
    presets = {p.name: p for p in engine.list_presets()}
    assert presets["slack"].description == "Slack API access"


def test_list_presets_empty_dir(tmp_path: pathlib.Path) -> None:
    eng = PolicyEngine(presets_dir=tmp_path / "empty", tiers_file=tmp_path / "none.yaml")
    assert eng.list_presets() == []


def test_load_preset_returns_content(engine: PolicyEngine) -> None:
    content = engine.load_preset("slack")
    assert content is not None
    assert "slack.com" in content


def test_load_preset_returns_none_for_missing(engine: PolicyEngine) -> None:
    assert engine.load_preset("nope") is None


def test_load_preset_blocks_path_traversal(engine: PolicyEngine) -> None:
    assert engine.load_preset("../secret") is None


def test_get_preset_endpoints(engine: PolicyEngine) -> None:
    content = engine.load_preset("slack")
    assert content is not None
    hosts = engine.get_preset_endpoints(content)
    assert "slack.com" in hosts


# ---------------------------------------------------------------------------
# Tier resolution
# ---------------------------------------------------------------------------

def test_list_tiers(engine: PolicyEngine) -> None:
    tiers = engine.list_tiers()
    names = [t.name for t in tiers]
    assert "restricted" in names
    assert "balanced" in names


def test_get_tier_restricted_has_no_presets(engine: PolicyEngine) -> None:
    tier = engine.get_tier("restricted")
    assert tier is not None
    assert tier.presets == ()


def test_get_tier_balanced_has_presets(engine: PolicyEngine) -> None:
    tier = engine.get_tier("balanced")
    assert tier is not None
    assert any(p.name == "slack" for p in tier.presets)


def test_get_tier_returns_none_for_missing(engine: PolicyEngine) -> None:
    assert engine.get_tier("nonexistent") is None


def test_get_tier_preset_names(engine: PolicyEngine) -> None:
    names = engine.get_tier_preset_names("balanced")
    assert "slack" in names
    assert "discord" in names


def test_get_tier_preset_names_empty_for_unknown(engine: PolicyEngine) -> None:
    assert engine.get_tier_preset_names("unknown") == []


def test_tiers_file_missing_returns_empty(tmp_path: pathlib.Path, preset_dir: pathlib.Path) -> None:
    eng = PolicyEngine(presets_dir=preset_dir, tiers_file=tmp_path / "missing.yaml")
    assert eng.list_tiers() == []


# ---------------------------------------------------------------------------
# extract_network_policies
# ---------------------------------------------------------------------------

def test_extract_network_policies_returns_dict(engine: PolicyEngine) -> None:
    content = engine.load_preset("slack")
    assert content is not None
    np = PolicyEngine.extract_network_policies(content)
    assert np is not None
    assert "slack" in np


def test_extract_network_policies_none_for_empty() -> None:
    assert PolicyEngine.extract_network_policies("") is None


def test_extract_network_policies_none_for_missing_section() -> None:
    content = "preset:\n  name: foo\n"
    assert PolicyEngine.extract_network_policies(content) is None


# ---------------------------------------------------------------------------
# merge_preset_into_policy
# ---------------------------------------------------------------------------

def test_merge_adds_preset_to_empty_policy(engine: PolicyEngine) -> None:
    result = engine.merge_preset_into_policy("", "slack")
    doc = yaml.safe_load(result)
    assert "slack" in doc["network_policies"]


def test_merge_adds_preset_to_existing_policy(engine: PolicyEngine) -> None:
    result = engine.merge_preset_into_policy(_EXISTING_POLICY, "slack")
    doc = yaml.safe_load(result)
    assert "github" in doc["network_policies"]
    assert "slack" in doc["network_policies"]


def test_merge_raises_for_unknown_preset(engine: PolicyEngine) -> None:
    with pytest.raises(PolicyEngineError, match="not found"):
        engine.merge_preset_into_policy("", "nope")


def test_merge_idempotent_on_same_preset(engine: PolicyEngine) -> None:
    once = engine.merge_preset_into_policy("", "slack")
    twice = engine.merge_preset_into_policy(once, "slack")
    doc_once = yaml.safe_load(once)
    doc_twice = yaml.safe_load(twice)
    assert doc_once["network_policies"] == doc_twice["network_policies"]


# ---------------------------------------------------------------------------
# remove_preset_from_policy
# ---------------------------------------------------------------------------

def test_remove_preset_removes_keys(engine: PolicyEngine) -> None:
    merged = engine.merge_preset_into_policy(_EXISTING_POLICY, "slack")
    result = engine.remove_preset_from_policy(merged, "slack")
    doc = yaml.safe_load(result)
    assert "slack" not in doc["network_policies"]
    assert "github" in doc["network_policies"]


def test_remove_preset_from_empty_raises(engine: PolicyEngine) -> None:
    with pytest.raises(PolicyEngineError, match="nope"):
        engine.remove_preset_from_policy("", "nope")


def test_remove_preset_uses_custom_content(engine: PolicyEngine) -> None:
    merged = engine.merge_preset_into_policy(_EXISTING_POLICY, "slack")
    result = engine.remove_preset_from_policy(merged, "slack", custom_content=_SLACK_PRESET)
    doc = yaml.safe_load(result)
    assert "slack" not in doc["network_policies"]


# ---------------------------------------------------------------------------
# merge_preset_names_into_policy
# ---------------------------------------------------------------------------

def test_merge_multiple_presets(engine: PolicyEngine) -> None:
    result, applied, missing = engine.merge_preset_names_into_policy("", ["slack", "discord"])
    doc = yaml.safe_load(result)
    assert "slack" in doc["network_policies"]
    assert "discord" in doc["network_policies"]
    assert set(applied) == {"slack", "discord"}
    assert missing == []


def test_merge_skips_missing_presets(engine: PolicyEngine) -> None:
    result, applied, missing = engine.merge_preset_names_into_policy("", ["slack", "nope"])
    assert "slack" in applied
    assert "nope" in missing


def test_merge_deduplicates_names(engine: PolicyEngine) -> None:
    result, applied, missing = engine.merge_preset_names_into_policy("", ["slack", "slack"])
    assert applied.count("slack") == 1


# ---------------------------------------------------------------------------
# get_applied_preset_names
# ---------------------------------------------------------------------------

def test_get_applied_preset_names_after_merge(engine: PolicyEngine) -> None:
    merged, _, _ = engine.merge_preset_names_into_policy("", ["slack", "discord"])
    applied = engine.get_applied_preset_names(merged)
    assert "slack" in applied
    assert "discord" in applied


def test_get_applied_preset_names_empty_for_bad_yaml(engine: PolicyEngine) -> None:
    assert engine.get_applied_preset_names("key: [unclosed") == []


def test_get_applied_preset_names_empty_for_empty_policy(engine: PolicyEngine) -> None:
    assert engine.get_applied_preset_names("") == []


# ---------------------------------------------------------------------------
# Real preset files (integration — skipped if repo not available)
# ---------------------------------------------------------------------------

@pytest.fixture()
def real_engine() -> PolicyEngine:
    return PolicyEngine()


def test_real_presets_list(real_engine: PolicyEngine) -> None:
    if not real_engine._presets_dir.is_dir():
        pytest.skip("nemoclaw-blueprint/policies/presets not found")
    presets = real_engine.list_presets()
    assert len(presets) > 0
    names = {p.name for p in presets}
    assert "slack" in names
    assert "discord" in names


def test_real_tiers_list(real_engine: PolicyEngine) -> None:
    if not real_engine._tiers_file.exists():
        pytest.skip("nemoclaw-blueprint/policies/tiers.yaml not found")
    tiers = real_engine.list_tiers()
    tier_names = {t.name for t in tiers}
    assert "restricted" in tier_names
    assert "balanced" in tier_names
