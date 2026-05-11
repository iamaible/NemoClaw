# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pathlib
from typing import Any

import pytest

import nemoclaw.registry as reg_mod
from nemoclaw.channels import (
    KNOWN_CHANNELS,
    ChannelAddResult,
    ChannelDef,
    ChannelManager,
    ChannelManagerError,
    ChannelRemoveResult,
    ChannelStatus,
    bridge_provider_name,
    channel_token_keys,
    get_channel_def,
    known_channel_names,
)
from nemoclaw.registry import SandboxEntry


# ---------------------------------------------------------------------------
# Registry isolation fixture
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
# Fake ProviderClient
# ---------------------------------------------------------------------------

class _FakeProvider:
    def __init__(self, *, fail_create: bool = False, fail_delete: bool = False) -> None:
        self._fail_create = fail_create
        self._fail_delete = fail_delete
        self.created: list[tuple[str, str, dict[str, str]]] = []
        self.updated: list[tuple[str, dict[str, str]]] = []
        self.deleted: list[str] = []
        self._existing: set[str] = set()

    def get(self, name: str) -> Any:
        if name not in self._existing:
            raise RuntimeError(f"not found: {name}")

        class _Ref:
            pass
        return _Ref()

    def create(self, name: str, type_: str, *, credentials: dict[str, str] | None = None, **_: Any) -> Any:
        if self._fail_create:
            raise RuntimeError("create failed")
        self.created.append((name, type_, credentials or {}))
        self._existing.add(name)

        class _Ref:
            pass
        return _Ref()

    def update(self, name: str, *, credentials: dict[str, str] | None = None, **_: Any) -> Any:
        self.updated.append((name, credentials or {}))

        class _Ref:
            pass
        return _Ref()

    def delete(self, name: str) -> bool:
        if self._fail_delete and name in self._existing:
            raise RuntimeError("delete failed")
        self.deleted.append(name)
        self._existing.discard(name)
        return True


# ---------------------------------------------------------------------------
# Channel definitions
# ---------------------------------------------------------------------------

def test_known_channels_has_expected() -> None:
    assert "telegram" in KNOWN_CHANNELS
    assert "discord" in KNOWN_CHANNELS
    assert "slack" in KNOWN_CHANNELS


def test_get_channel_def_case_insensitive() -> None:
    assert get_channel_def("Telegram") is not None
    assert get_channel_def("DISCORD") is not None


def test_get_channel_def_returns_none_for_unknown() -> None:
    assert get_channel_def("whatsapp") is None


def test_known_channel_names() -> None:
    names = known_channel_names()
    assert set(names) >= {"telegram", "discord", "slack"}


def test_channel_token_keys_single() -> None:
    ch = get_channel_def("telegram")
    assert ch is not None
    assert channel_token_keys(ch) == ["TELEGRAM_BOT_TOKEN"]


def test_channel_token_keys_slack_has_two() -> None:
    ch = get_channel_def("slack")
    assert ch is not None
    keys = channel_token_keys(ch)
    assert "SLACK_BOT_TOKEN" in keys
    assert "SLACK_APP_TOKEN" in keys


# ---------------------------------------------------------------------------
# bridge_provider_name
# ---------------------------------------------------------------------------

def test_bridge_provider_name_telegram() -> None:
    assert bridge_provider_name("my-box", "telegram", "TELEGRAM_BOT_TOKEN") == "my-box-telegram-bridge"


def test_bridge_provider_name_slack_app_token() -> None:
    assert bridge_provider_name("my-box", "slack", "SLACK_APP_TOKEN") == "my-box-slack-app"


def test_bridge_provider_name_slack_bot_token() -> None:
    assert bridge_provider_name("my-box", "slack", "SLACK_BOT_TOKEN") == "my-box-slack-bridge"


# ---------------------------------------------------------------------------
# list_channels
# ---------------------------------------------------------------------------

def test_list_channels_all_not_added_initially() -> None:
    _register("box")
    mgr = ChannelManager()
    statuses = mgr.list_channels("box")
    assert all(not s.added for s in statuses)
    assert all(not s.enabled for s in statuses)


def test_list_channels_after_add() -> None:
    _register("box", messaging_channels=["telegram"])
    mgr = ChannelManager()
    statuses = {s.name: s for s in mgr.list_channels("box")}
    assert statuses["telegram"].added is True
    assert statuses["telegram"].enabled is True


def test_list_channels_disabled_channel() -> None:
    _register("box", messaging_channels=["telegram"], disabled_channels=["telegram"])
    mgr = ChannelManager()
    statuses = {s.name: s for s in mgr.list_channels("box")}
    assert statuses["telegram"].added is True
    assert statuses["telegram"].enabled is False


def test_list_channels_missing_sandbox() -> None:
    mgr = ChannelManager()
    statuses = mgr.list_channels("ghost")
    assert all(not s.added for s in statuses)


# ---------------------------------------------------------------------------
# add_channel
# ---------------------------------------------------------------------------

def test_add_channel_requires_provider_client() -> None:
    mgr = ChannelManager()
    with pytest.raises(ChannelManagerError, match="ProviderClient"):
        mgr.add_channel("box", "telegram", {"TELEGRAM_BOT_TOKEN": "abc"})


def test_add_channel_unknown_channel() -> None:
    fp = _FakeProvider()
    mgr = ChannelManager(fp)
    result = mgr.add_channel("box", "whatsapp", {})
    assert result.ok is False
    assert "unknown" in result.error


def test_add_channel_no_tokens() -> None:
    _register("box")
    fp = _FakeProvider()
    mgr = ChannelManager(fp)
    result = mgr.add_channel("box", "telegram", {})
    assert result.ok is False
    assert "no tokens" in result.error


def test_add_channel_telegram_creates_provider() -> None:
    _register("box")
    fp = _FakeProvider()
    mgr = ChannelManager(fp)
    result = mgr.add_channel("box", "telegram", {"TELEGRAM_BOT_TOKEN": "tok123"})
    assert result.ok is True
    assert len(fp.created) == 1
    name, type_, creds = fp.created[0]
    assert name == "box-telegram-bridge"
    assert type_ == "generic"
    assert creds["TELEGRAM_BOT_TOKEN"] == "tok123"


def test_add_channel_updates_registry() -> None:
    _register("box")
    fp = _FakeProvider()
    mgr = ChannelManager(fp)
    mgr.add_channel("box", "telegram", {"TELEGRAM_BOT_TOKEN": "tok"})
    entry = reg_mod.get_sandbox("box")
    assert entry is not None
    assert "telegram" in entry.messaging_channels


def test_add_channel_removes_from_disabled() -> None:
    _register("box", disabled_channels=["telegram"])
    fp = _FakeProvider()
    mgr = ChannelManager(fp)
    mgr.add_channel("box", "telegram", {"TELEGRAM_BOT_TOKEN": "tok"})
    entry = reg_mod.get_sandbox("box")
    assert entry is not None
    assert "telegram" not in entry.disabled_channels


def test_add_channel_slack_creates_two_providers() -> None:
    _register("box")
    fp = _FakeProvider()
    mgr = ChannelManager(fp)
    result = mgr.add_channel("box", "slack", {
        "SLACK_BOT_TOKEN": "xoxb-123",
        "SLACK_APP_TOKEN": "xapp-456",
    })
    assert result.ok is True
    names = [c[0] for c in fp.created]
    assert "box-slack-bridge" in names
    assert "box-slack-app" in names


def test_add_channel_upserts_existing_provider() -> None:
    _register("box")
    fp = _FakeProvider()
    fp._existing.add("box-telegram-bridge")
    mgr = ChannelManager(fp)
    result = mgr.add_channel("box", "telegram", {"TELEGRAM_BOT_TOKEN": "new_tok"})
    assert result.ok is True
    assert len(fp.created) == 0
    assert len(fp.updated) == 1
    assert fp.updated[0][1]["TELEGRAM_BOT_TOKEN"] == "new_tok"


def test_add_channel_gateway_failure() -> None:
    _register("box")
    fp = _FakeProvider(fail_create=True)
    mgr = ChannelManager(fp)
    result = mgr.add_channel("box", "telegram", {"TELEGRAM_BOT_TOKEN": "tok"})
    assert result.ok is False
    assert "failed to register" in result.error


# ---------------------------------------------------------------------------
# remove_channel
# ---------------------------------------------------------------------------

def test_remove_channel_requires_provider_client() -> None:
    mgr = ChannelManager()
    with pytest.raises(ChannelManagerError, match="ProviderClient"):
        mgr.remove_channel("box", "telegram")


def test_remove_channel_unknown_channel() -> None:
    fp = _FakeProvider()
    mgr = ChannelManager(fp)
    result = mgr.remove_channel("box", "whatsapp")
    assert result.ok is False


def test_remove_channel_deletes_provider() -> None:
    _register("box", messaging_channels=["telegram"])
    fp = _FakeProvider()
    mgr = ChannelManager(fp)
    result = mgr.remove_channel("box", "telegram")
    assert result.ok is True
    assert "box-telegram-bridge" in fp.deleted


def test_remove_channel_updates_registry() -> None:
    _register("box", messaging_channels=["telegram"])
    fp = _FakeProvider()
    mgr = ChannelManager(fp)
    mgr.remove_channel("box", "telegram")
    entry = reg_mod.get_sandbox("box")
    assert entry is not None
    assert "telegram" not in entry.messaging_channels


def test_remove_channel_not_found_is_success() -> None:
    _register("box")
    fp = _FakeProvider()
    mgr = ChannelManager(fp)
    result = mgr.remove_channel("box", "telegram")
    assert result.ok is True


def test_remove_channel_gateway_failure_does_not_update_registry() -> None:
    _register("box", messaging_channels=["discord"])
    fp = _FakeProvider(fail_delete=True)
    fp._existing.add("box-discord-bridge")
    mgr = ChannelManager(fp)
    result = mgr.remove_channel("box", "discord")
    assert result.ok is False
    entry = reg_mod.get_sandbox("box")
    assert entry is not None
    assert "discord" in entry.messaging_channels  # not updated


# ---------------------------------------------------------------------------
# start / stop channel
# ---------------------------------------------------------------------------

def test_stop_channel_marks_disabled() -> None:
    _register("box")
    mgr = ChannelManager()
    assert mgr.stop_channel("box", "telegram") is True
    assert "telegram" in reg_mod.get_disabled_channels("box")


def test_start_channel_clears_disabled() -> None:
    _register("box")
    mgr = ChannelManager()
    mgr.stop_channel("box", "telegram")
    assert mgr.start_channel("box", "telegram") is True
    assert "telegram" not in reg_mod.get_disabled_channels("box")


def test_start_stop_returns_false_for_missing_sandbox() -> None:
    mgr = ChannelManager()
    assert mgr.stop_channel("ghost", "telegram") is False
    assert mgr.start_channel("ghost", "telegram") is False


def test_stop_channel_idempotent() -> None:
    _register("box")
    mgr = ChannelManager()
    mgr.stop_channel("box", "telegram")
    mgr.stop_channel("box", "telegram")
    assert reg_mod.get_disabled_channels("box").count("telegram") == 1
