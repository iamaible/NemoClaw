# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
RebuildOrchestrator: delete → recreate → re-attach providers → re-apply presets.

Coordinates the gRPC operations needed to rebuild a sandbox while preserving
its provider attachments and policy preset configuration.  File-system backup
and restore (workspace state) are out of scope; callers handle that separately.

Workflow
--------
1. **Preflight** — verify sandbox exists in registry and clients are ready.
2. **Snapshot providers** — list currently attached providers before destruction.
3. **Delete sandbox** — :meth:`SandboxClient.delete`.
4. **Create sandbox** — :meth:`SandboxClient.create` with the original (or
   caller-supplied) :class:`~openshell._proto.openshell_pb2.SandboxSpec`.
5. **Wait ready** — :meth:`SandboxClient.wait_ready`.
6. **Re-attach providers** — :meth:`ProviderClient.attach` for each provider
   that was attached before the delete.
7. **Re-apply presets** — :meth:`PolicyManager.add_preset` for each preset
   recorded in the registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from nemoclaw import registry as _registry
from nemoclaw.policy_manager import PolicyAddResult, PolicyManager

if TYPE_CHECKING:
    from openshell.provider import ProviderClient
    from openshell.sandbox import SandboxClient


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RebuildStep:
    """Record of a single step in the rebuild workflow."""
    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class RebuildResult:
    """Aggregated result of a full rebuild operation."""
    ok: bool
    sandbox_name: str
    steps: list[RebuildStep] = field(default_factory=list)
    reattached_providers: list[str] = field(default_factory=list)
    restored_presets: list[str] = field(default_factory=list)
    error: str = ""


# ---------------------------------------------------------------------------
# RebuildOrchestratorError
# ---------------------------------------------------------------------------

class RebuildOrchestratorError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# RebuildOrchestrator
# ---------------------------------------------------------------------------

class RebuildOrchestrator:
    """
    Orchestrates the gRPC-level sandbox rebuild workflow.

    Parameters
    ----------
    sandbox_client:
        An initialised :class:`openshell.sandbox.SandboxClient`.  Required to
        delete, create, and wait for the sandbox.  Raises
        :exc:`RebuildOrchestratorError` when ``None``.
    provider_client:
        An initialised :class:`openshell.provider.ProviderClient`.  When
        ``None``, provider snapshot and re-attachment are skipped (the sandbox
        will have no providers attached after rebuild).
    policy_manager:
        A :class:`~nemoclaw.policy_manager.PolicyManager` (with a live
        ``PolicyClient``) used to re-apply presets.  When ``None``, presets
        are not re-applied.
    wait_timeout:
        Seconds to wait for the sandbox to become ready after recreation.
    delete_timeout:
        Seconds to wait for the old sandbox to disappear after deletion.
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

    def rebuild(
        self,
        sandbox_name: str,
        *,
        spec: Any = None,
    ) -> RebuildResult:
        """
        Rebuild *sandbox_name*.

        Parameters
        ----------
        sandbox_name:
            Registry name of the sandbox to rebuild.
        spec:
            Optional ``openshell_pb2.SandboxSpec``.  When ``None``, a minimal
            spec is derived from the registry entry (``image_tag`` +
            ``gpu_enabled``).

        Returns
        -------
        :class:`RebuildResult`
            ``ok=True`` when the sandbox was successfully recreated and all
            providers + presets were restored.  Individual step failures
            (e.g. partial provider reattachment) cause ``ok=False`` even when
            the sandbox itself is running.
        """
        if self._sandbox is None:
            raise RebuildOrchestratorError(
                "rebuild requires a SandboxClient; construct RebuildOrchestrator with one."
            )

        steps: list[RebuildStep] = []

        # ------------------------------------------------------------------
        # Step 1: Preflight
        # ------------------------------------------------------------------
        entry = _registry.get_sandbox(sandbox_name)
        if entry is None:
            steps.append(RebuildStep(
                name="preflight",
                ok=False,
                detail=f"sandbox {sandbox_name!r} not found in registry",
            ))
            return RebuildResult(
                ok=False,
                sandbox_name=sandbox_name,
                steps=steps,
                error=f"sandbox {sandbox_name!r} not found in registry",
            )
        steps.append(RebuildStep(name="preflight", ok=True))

        # ------------------------------------------------------------------
        # Step 2: Snapshot provider list (before destruction)
        # ------------------------------------------------------------------
        provider_names: list[str] = []
        if self._provider is not None:
            try:
                live = self._provider.list_sandbox_providers(sandbox_name)
                provider_names = [p.name for p in live]
                steps.append(RebuildStep(
                    name="snapshot_providers",
                    ok=True,
                    detail=f"{len(provider_names)} provider(s): {', '.join(provider_names) or '(none)'}",
                ))
            except Exception as exc:  # noqa: BLE001
                steps.append(RebuildStep(
                    name="snapshot_providers",
                    ok=False,
                    detail=f"could not list providers: {exc}",
                ))
                # Non-fatal — proceed without provider re-attachment.

        # ------------------------------------------------------------------
        # Step 3: Snapshot policy presets (from registry)
        # ------------------------------------------------------------------
        presets: list[str] = list(entry.policies)

        # ------------------------------------------------------------------
        # Step 4: Build spec
        # ------------------------------------------------------------------
        if spec is None:
            spec = _build_spec_from_entry(entry)

        # ------------------------------------------------------------------
        # Step 5: Delete sandbox
        # ------------------------------------------------------------------
        try:
            self._sandbox.delete(sandbox_name)
            steps.append(RebuildStep(name="delete", ok=True))
        except Exception as exc:  # noqa: BLE001
            steps.append(RebuildStep(name="delete", ok=False, detail=str(exc)))
            return RebuildResult(
                ok=False,
                sandbox_name=sandbox_name,
                steps=steps,
                error=f"delete failed: {exc}",
            )

        try:
            self._sandbox.wait_deleted(sandbox_name, timeout_seconds=self._delete_timeout)
        except Exception:  # noqa: BLE001
            pass  # Best-effort — continue to create.

        # ------------------------------------------------------------------
        # Step 6: Create sandbox  [POINT OF NO RETURN]
        # ------------------------------------------------------------------
        try:
            self._sandbox.create(spec=spec)
            steps.append(RebuildStep(name="create", ok=True))
        except Exception as exc:  # noqa: BLE001
            steps.append(RebuildStep(name="create", ok=False, detail=str(exc)))
            return RebuildResult(
                ok=False,
                sandbox_name=sandbox_name,
                steps=steps,
                error=f"create failed after delete: {exc}",
            )

        # ------------------------------------------------------------------
        # Step 7: Wait for sandbox to become ready
        # ------------------------------------------------------------------
        try:
            self._sandbox.wait_ready(sandbox_name, timeout_seconds=self._wait_timeout)
            steps.append(RebuildStep(name="wait_ready", ok=True))
        except Exception as exc:  # noqa: BLE001
            steps.append(RebuildStep(name="wait_ready", ok=False, detail=str(exc)))
            # Don't abort — sandbox may still be usable.

        # ------------------------------------------------------------------
        # Step 8: Re-attach providers
        # ------------------------------------------------------------------
        reattached: list[str] = []
        attach_failures: list[str] = []
        if self._provider is not None and provider_names:
            for pname in provider_names:
                try:
                    self._provider.attach(sandbox_name, pname)
                    reattached.append(pname)
                except Exception as exc:  # noqa: BLE001
                    attach_failures.append(f"{pname}: {exc}")

            if attach_failures:
                steps.append(RebuildStep(
                    name="attach_providers",
                    ok=False,
                    detail=f"failed: {'; '.join(attach_failures)}; ok: {', '.join(reattached)}",
                ))
            else:
                steps.append(RebuildStep(
                    name="attach_providers",
                    ok=True,
                    detail=f"reattached {len(reattached)} provider(s)",
                ))

        # ------------------------------------------------------------------
        # Step 9: Re-apply policy presets
        # ------------------------------------------------------------------
        restored: list[str] = []
        preset_failures: list[str] = []
        if self._policy is not None and presets:
            for preset_name in presets:
                try:
                    result: PolicyAddResult = self._policy.add_preset(sandbox_name, preset_name)
                    if result.ok:
                        restored.append(preset_name)
                    else:
                        preset_failures.append(f"{preset_name}: {result.error}")
                except Exception as exc:  # noqa: BLE001
                    preset_failures.append(f"{preset_name}: {exc}")

            if preset_failures:
                steps.append(RebuildStep(
                    name="apply_presets",
                    ok=False,
                    detail=f"failed: {'; '.join(preset_failures)}; ok: {', '.join(restored)}",
                ))
            else:
                steps.append(RebuildStep(
                    name="apply_presets",
                    ok=True,
                    detail=f"restored {len(restored)} preset(s)",
                ))

        # ------------------------------------------------------------------
        # Overall result
        # ------------------------------------------------------------------
        failed_steps = [s for s in steps if not s.ok]
        ok = len(failed_steps) == 0

        return RebuildResult(
            ok=ok,
            sandbox_name=sandbox_name,
            steps=steps,
            reattached_providers=reattached,
            restored_presets=restored,
            error=failed_steps[0].detail if not ok and failed_steps else "",
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_spec_from_entry(entry: "_registry.SandboxEntry") -> Any:
    """Construct a minimal SandboxSpec from a registry entry."""
    from openshell._proto import openshell_pb2  # noqa: PLC0415

    return openshell_pb2.SandboxSpec(
        template=openshell_pb2.SandboxTemplate(image=entry.image_tag or ""),
        gpu=entry.gpu_enabled,
    )
