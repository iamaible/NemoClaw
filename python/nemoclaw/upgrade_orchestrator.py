# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
UpgradeOrchestrator: batch version-check and rebuild across registered sandboxes.

Workflow
--------
1. **Check** — compare each sandbox's ``image_tag`` in the registry against
   the caller-supplied *desired_image_tag*.  Sandboxes that already match are
   skipped.
2. **Rebuild** — for each sandbox that needs an upgrade, delegate to
   :class:`~nemoclaw.rebuild_orchestrator.RebuildOrchestrator` with a new
   :class:`~openshell._proto.openshell_pb2.SandboxSpec` that uses
   *desired_image_tag*.
3. **Update registry** — on success, write *desired_image_tag* back to the
   registry so the next ``check`` sees the new state.

Individual sandbox failures are recorded and do **not** abort the batch.
The overall result is ``ok=True`` only when every sandbox that needed an
upgrade was rebuilt successfully (skipped sandboxes are not counted as
failures).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from nemoclaw import registry as _registry
from nemoclaw.rebuild_orchestrator import RebuildOrchestrator, RebuildResult
from nemoclaw.policy_manager import PolicyManager

if TYPE_CHECKING:
    from openshell.provider import ProviderClient
    from openshell.sandbox import SandboxClient


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UpgradeCheck:
    """Per-sandbox version comparison result."""
    sandbox_name: str
    current_image_tag: str | None
    desired_image_tag: str
    needs_upgrade: bool


@dataclass(frozen=True)
class SandboxUpgradeResult:
    """Outcome of a single sandbox upgrade attempt."""
    sandbox_name: str
    ok: bool
    skipped: bool
    rebuild_result: RebuildResult | None = None
    error: str = ""


@dataclass(frozen=True)
class UpgradeResult:
    """Aggregated result of a full batch upgrade run."""
    ok: bool
    desired_image_tag: str
    checks: list[UpgradeCheck] = field(default_factory=list)
    results: list[SandboxUpgradeResult] = field(default_factory=list)
    upgraded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    dry_run: bool = False


# ---------------------------------------------------------------------------
# UpgradeOrchestratorError
# ---------------------------------------------------------------------------

class UpgradeOrchestratorError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# UpgradeOrchestrator
# ---------------------------------------------------------------------------

class UpgradeOrchestrator:
    """
    Checks sandbox image versions and rebuilds stale sandboxes in batch.

    Parameters
    ----------
    sandbox_client:
        An initialised :class:`openshell.sandbox.SandboxClient`.  Required for
        :meth:`upgrade`; :meth:`check` works without one.
    provider_client:
        Passed through to the internal :class:`~nemoclaw.rebuild_orchestrator.RebuildOrchestrator`
        for provider re-attachment after each rebuild.
    policy_manager:
        Passed through to the internal
        :class:`~nemoclaw.rebuild_orchestrator.RebuildOrchestrator` for preset
        re-application after each rebuild.
    wait_timeout:
        Seconds to wait for each sandbox to become ready after recreation.
    delete_timeout:
        Seconds to wait for each sandbox to disappear after deletion.
    """

    def __init__(
        self,
        sandbox_client: "SandboxClient | None" = None,
        provider_client: "ProviderClient | None" = None,
        policy_manager: PolicyManager | None = None,
        *,
        wait_timeout: float = 300.0,
        delete_timeout: float = 60.0,
    ) -> None:
        self._sandbox = sandbox_client
        self._provider = provider_client
        self._policy = policy_manager
        self._wait_timeout = wait_timeout
        self._delete_timeout = delete_timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(
        self,
        desired_image_tag: str,
        *,
        sandbox_names: list[str] | None = None,
    ) -> list[UpgradeCheck]:
        """
        Compare each sandbox's current image tag against *desired_image_tag*.

        Parameters
        ----------
        desired_image_tag:
            The container image tag that sandboxes should be running.
        sandbox_names:
            Explicit list of sandbox names to check.  When ``None``, all
            sandboxes in the registry are checked.

        Returns
        -------
        list[UpgradeCheck]
            One entry per sandbox, ordered as the registry returns them.
        """
        entries = self._resolve_entries(sandbox_names)
        return [
            UpgradeCheck(
                sandbox_name=e.name,
                current_image_tag=e.image_tag,
                desired_image_tag=desired_image_tag,
                needs_upgrade=e.image_tag != desired_image_tag,
            )
            for e in entries
        ]

    def upgrade(
        self,
        desired_image_tag: str,
        *,
        sandbox_names: list[str] | None = None,
        force: bool = False,
        dry_run: bool = False,
    ) -> UpgradeResult:
        """
        Rebuild every sandbox whose image tag does not match *desired_image_tag*.

        Parameters
        ----------
        desired_image_tag:
            The container image tag to roll out.
        sandbox_names:
            Explicit list of sandbox names to consider.  When ``None``, all
            sandboxes in the registry are considered.
        force:
            When ``True``, rebuild even sandboxes that already report
            *desired_image_tag* in the registry.
        dry_run:
            When ``True``, perform the version check but skip all rebuilds.
            The returned :attr:`UpgradeResult.dry_run` flag is set.

        Returns
        -------
        :class:`UpgradeResult`
            ``ok=True`` when every sandbox that needed upgrading was rebuilt
            successfully.
        """
        if not dry_run and self._sandbox is None:
            raise UpgradeOrchestratorError(
                "upgrade requires a SandboxClient; construct UpgradeOrchestrator with one."
            )

        checks = self.check(desired_image_tag, sandbox_names=sandbox_names)
        results: list[SandboxUpgradeResult] = []
        upgraded: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []

        rebuilder = RebuildOrchestrator(
            self._sandbox,
            self._provider,
            self._policy,
            wait_timeout=self._wait_timeout,
            delete_timeout=self._delete_timeout,
        )

        for chk in checks:
            if not chk.needs_upgrade and not force:
                skipped.append(chk.sandbox_name)
                results.append(SandboxUpgradeResult(
                    sandbox_name=chk.sandbox_name,
                    ok=True,
                    skipped=True,
                ))
                continue

            if dry_run:
                # Report what would be done without executing.
                results.append(SandboxUpgradeResult(
                    sandbox_name=chk.sandbox_name,
                    ok=True,
                    skipped=True,
                ))
                continue

            spec = self._build_spec(chk.sandbox_name, desired_image_tag)
            rebuild_result = rebuilder.rebuild(chk.sandbox_name, spec=spec)

            if rebuild_result.ok:
                _registry.update_sandbox(chk.sandbox_name, image_tag=desired_image_tag)
                upgraded.append(chk.sandbox_name)
                results.append(SandboxUpgradeResult(
                    sandbox_name=chk.sandbox_name,
                    ok=True,
                    skipped=False,
                    rebuild_result=rebuild_result,
                ))
            else:
                failed.append(chk.sandbox_name)
                results.append(SandboxUpgradeResult(
                    sandbox_name=chk.sandbox_name,
                    ok=False,
                    skipped=False,
                    rebuild_result=rebuild_result,
                    error=rebuild_result.error,
                ))

        return UpgradeResult(
            ok=len(failed) == 0,
            desired_image_tag=desired_image_tag,
            checks=checks,
            results=results,
            upgraded=upgraded,
            skipped=skipped,
            failed=failed,
            dry_run=dry_run,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_entries(
        self, sandbox_names: list[str] | None
    ) -> list["_registry.SandboxEntry"]:
        if sandbox_names is None:
            entries, _ = _registry.list_sandboxes()
            return entries

        result = []
        for name in sandbox_names:
            entry = _registry.get_sandbox(name)
            if entry is not None:
                result.append(entry)
        return result

    def _build_spec(self, sandbox_name: str, image_tag: str) -> object:
        """Build a SandboxSpec with *image_tag*, preserving gpu_enabled from registry."""
        from openshell._proto import openshell_pb2  # noqa: PLC0415

        entry = _registry.get_sandbox(sandbox_name)
        gpu = entry.gpu_enabled if entry is not None else False
        return openshell_pb2.SandboxSpec(
            template=openshell_pb2.SandboxTemplate(image=image_tag),
            gpu=gpu,
        )
