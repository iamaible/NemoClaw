# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
ChannelManager: add, remove, start, and stop messaging channels on a sandbox.

Each channel is backed by one or more OpenShell gateway providers that inject
the channel's bot token into the sandbox environment.  The manager handles both
the gateway provider lifecycle (via :class:`openshell.provider.ProviderClient`)
and the registry bookkeeping (via :mod:`nemoclaw.registry`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from nemoclaw import registry as _registry

if TYPE_CHECKING:
    from openshell.provider import ProviderClient


# ---------------------------------------------------------------------------
# Channel definitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChannelDef:
    """Describes a known messaging channel and its credential structure."""
    name: str
    description: str
    primary_env_key: str
    secondary_env_key: str = ""


KNOWN_CHANNELS: dict[str, ChannelDef] = {
    "telegram": ChannelDef(
        name="telegram",
        description="Telegram bot messaging",
        primary_env_key="TELEGRAM_BOT_TOKEN",
    ),
    "discord": ChannelDef(
        name="discord",
        description="Discord bot messaging",
        primary_env_key="DISCORD_BOT_TOKEN",
    ),
    "slack": ChannelDef(
        name="slack",
        description="Slack bot messaging",
        primary_env_key="SLACK_BOT_TOKEN",
        secondary_env_key="SLACK_APP_TOKEN",
    ),
}


def get_channel_def(name: str) -> ChannelDef | None:
    return KNOWN_CHANNELS.get(name.strip().lower())


def known_channel_names() -> list[str]:
    return list(KNOWN_CHANNELS)


def channel_token_keys(channel: ChannelDef) -> list[str]:
    """Return the ordered list of env-key names for a channel's tokens."""
    if channel.secondary_env_key:
        return [channel.primary_env_key, channel.secondary_env_key]
    return [channel.primary_env_key]


# ---------------------------------------------------------------------------
# Bridge provider naming (mirrors TypeScript bridgeProviderName)
# ---------------------------------------------------------------------------

def bridge_provider_name(sandbox_name: str, channel_name: str, env_key: str) -> str:
    if channel_name == "slack" and env_key == "SLACK_APP_TOKEN":
        return f"{sandbox_name}-slack-app"
    return f"{sandbox_name}-{channel_name}-bridge"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChannelAddResult:
    ok: bool
    channel: str
    registered_providers: list[str] = field(default_factory=list)
    error: str = ""


@dataclass(frozen=True)
class ChannelRemoveResult:
    ok: bool
    channel: str
    deleted_providers: list[str] = field(default_factory=list)
    error: str = ""


@dataclass(frozen=True)
class ChannelStatus:
    name: str
    description: str
    added: bool
    enabled: bool


# ---------------------------------------------------------------------------
# ChannelManagerError
# ---------------------------------------------------------------------------

class ChannelManagerError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# ChannelManager
# ---------------------------------------------------------------------------

class ChannelManager:
    """
    Manages messaging channel lifecycle for a sandbox.

    Parameters
    ----------
    provider_client:
        An initialised :class:`openshell.provider.ProviderClient`.  When
        ``None``, :meth:`add_channel` and :meth:`remove_channel` raise
        :exc:`ChannelManagerError`.  :meth:`start_channel` and
        :meth:`stop_channel` are registry-only and always work.
    """

    def __init__(self, provider_client: "ProviderClient | None" = None) -> None:
        self._provider = provider_client

    # ------------------------------------------------------------------
    # Channel discovery
    # ------------------------------------------------------------------

    def list_channels(self, sandbox_name: str) -> list[ChannelStatus]:
        """Return status of every known channel for *sandbox_name*."""
        entry = _registry.get_sandbox(sandbox_name)
        added = set(entry.messaging_channels if entry else [])
        disabled = set(entry.disabled_channels if entry else [])
        return [
            ChannelStatus(
                name=name,
                description=ch.description,
                added=name in added,
                enabled=name in added and name not in disabled,
            )
            for name, ch in KNOWN_CHANNELS.items()
        ]

    # ------------------------------------------------------------------
    # Add / remove (gateway + registry)
    # ------------------------------------------------------------------

    def add_channel(
        self,
        sandbox_name: str,
        channel_name: str,
        tokens: dict[str, str],
    ) -> ChannelAddResult:
        """
        Register a channel's bridge provider(s) in the gateway and record the
        channel in the registry.

        Parameters
        ----------
        sandbox_name:
            Registry name of the target sandbox.
        channel_name:
            Short channel name (``"telegram"``, ``"discord"``, ``"slack"``).
        tokens:
            Mapping of ``{env_key: token_value}`` for each token the channel
            needs.  At minimum the primary key must be present.

        Returns
        -------
        :class:`ChannelAddResult`
            ``ok=True`` if all providers were registered successfully.
        """
        if self._provider is None:
            raise ChannelManagerError(
                "add_channel requires a ProviderClient; construct ChannelManager with one."
            )
        channel = get_channel_def(channel_name)
        if channel is None:
            return ChannelAddResult(
                ok=False,
                channel=channel_name,
                error=f"unknown channel {channel_name!r}; valid: {known_channel_names()}",
            )

        registered: list[str] = []
        for env_key in channel_token_keys(channel):
            token = tokens.get(env_key)
            if not token:
                continue
            provider_name = bridge_provider_name(sandbox_name, channel_name, env_key)
            try:
                self._upsert_provider(provider_name, env_key, token)
                registered.append(provider_name)
            except Exception as exc:  # noqa: BLE001
                return ChannelAddResult(
                    ok=False,
                    channel=channel_name,
                    registered_providers=registered,
                    error=f"failed to register provider {provider_name!r}: {exc}",
                )

        if not registered:
            return ChannelAddResult(
                ok=False,
                channel=channel_name,
                error=f"no tokens provided for channel {channel_name!r}",
            )

        self._registry_add_channel(sandbox_name, channel_name)
        return ChannelAddResult(ok=True, channel=channel_name, registered_providers=registered)

    def remove_channel(
        self,
        sandbox_name: str,
        channel_name: str,
    ) -> ChannelRemoveResult:
        """
        Delete bridge provider(s) from the gateway and remove the channel from
        the registry.  A "not found" response from the gateway is treated as
        success (idempotent).
        """
        if self._provider is None:
            raise ChannelManagerError(
                "remove_channel requires a ProviderClient; construct ChannelManager with one."
            )
        channel = get_channel_def(channel_name)
        if channel is None:
            return ChannelRemoveResult(
                ok=False,
                channel=channel_name,
                error=f"unknown channel {channel_name!r}",
            )

        deleted: list[str] = []
        failed: list[str] = []
        for env_key in channel_token_keys(channel):
            provider_name = bridge_provider_name(sandbox_name, channel_name, env_key)
            try:
                self._provider.delete(provider_name)
                deleted.append(provider_name)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).lower()
                if "not found" in msg or "notfound" in msg:
                    # Treat as success — previous run may have cleaned up
                    deleted.append(provider_name)
                else:
                    failed.append(f"{provider_name}: {exc}")

        if failed:
            return ChannelRemoveResult(
                ok=False,
                channel=channel_name,
                deleted_providers=deleted,
                error="; ".join(failed),
            )

        self._registry_remove_channel(sandbox_name, channel_name, channel)
        return ChannelRemoveResult(ok=True, channel=channel_name, deleted_providers=deleted)

    # ------------------------------------------------------------------
    # Start / stop (registry-only)
    # ------------------------------------------------------------------

    def start_channel(self, sandbox_name: str, channel_name: str) -> bool:
        """
        Mark *channel_name* as enabled in the registry.

        Returns ``False`` if the sandbox does not exist in the registry.
        """
        normalized = channel_name.strip().lower()
        return _registry.set_channel_disabled(sandbox_name, normalized, False)

    def stop_channel(self, sandbox_name: str, channel_name: str) -> bool:
        """
        Mark *channel_name* as disabled in the registry.

        Returns ``False`` if the sandbox does not exist in the registry.
        """
        normalized = channel_name.strip().lower()
        return _registry.set_channel_disabled(sandbox_name, normalized, True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _upsert_provider(self, name: str, env_key: str, token: str) -> None:
        """Create or update a bridge provider in the gateway."""
        assert self._provider is not None
        try:
            self._provider.get(name)
            # Provider exists — update credentials
            self._provider.update(name, credentials={env_key: token})
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if "not found" in msg or "notfound" in msg:
                self._provider.create(name, "generic", credentials={env_key: token})
            else:
                raise

    def _registry_add_channel(self, sandbox_name: str, channel_name: str) -> None:
        entry = _registry.get_sandbox(sandbox_name)
        if entry is None:
            return
        enabled = set(entry.messaging_channels)
        enabled.add(channel_name)
        disabled = [c for c in entry.disabled_channels if c != channel_name]
        _registry.update_sandbox(sandbox_name, messaging_channels=sorted(enabled), disabled_channels=disabled)

    def _registry_remove_channel(
        self,
        sandbox_name: str,
        channel_name: str,
        channel: ChannelDef,
    ) -> None:
        entry = _registry.get_sandbox(sandbox_name)
        if entry is None:
            return
        enabled = [c for c in entry.messaging_channels if c != channel_name]
        hashes = dict(entry.provider_credential_hashes)
        for env_key in channel_token_keys(channel):
            hashes.pop(env_key, None)
        _registry.update_sandbox(
            sandbox_name,
            messaging_channels=enabled,
            provider_credential_hashes=hashes,
        )
