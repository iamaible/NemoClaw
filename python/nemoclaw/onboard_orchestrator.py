# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
OnboardOrchestrator: first-time sandbox creation and registration.

Programmatic equivalent of ``nemoclaw onboard`` for use by control planes and
automation that create sandboxes on demand (no interactive wizard).

Workflow
--------
1. **Preflight** — verify the sandbox does not already exist in the registry.
2. **Create sandbox** — :meth:`SandboxClient.create` with the caller-supplied spec.
3. **Wait ready** — :meth:`SandboxClient.wait_ready`.
4. **Attach providers** — :meth:`ProviderClient.attach` for each named provider.
5. **Apply policy presets** — :meth:`PolicyManager.add_preset` for each preset.
6. **Register** — write a :class:`~nemoclaw.registry.SandboxEntry` to the local
   registry so all other NemoClaw SDK components can find the sandbox.

Unlike :class:`~nemoclaw.rebuild_orchestrator.RebuildOrchestrator`, this
orchestrator has no delete step and does not require an existing registry entry.
It is the natural pair: onboard creates, rebuild updates.
"""

from __future__ import annotations

import datetime
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
class OnboardStep:
    """Record of a single step in the onboard workflow."""

    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class OnboardResult:
    """Aggregated result of a full onboard operation."""

    ok: bool
    sandbox_name: str
    steps: list[OnboardStep] = field(default_factory=list)
    attached_providers: list[str] = field(default_factory=list)
    applied_presets: list[str] = field(default_factory=list)
    error: str = ""


# ---------------------------------------------------------------------------
# OnboardOrchestratorError
# ---------------------------------------------------------------------------


class OnboardOrchestratorError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# OnboardOrchestrator
# ---------------------------------------------------------------------------


class OnboardOrchestrator:
    """
    Orchestrates first-time sandbox creation and registration.

    Parameters
    ----------
    sandbox_client:
        An initialised :class:`openshell.sandbox.SandboxClient`.  Required.
    provider_client:
        An initialised :class:`openshell.provider.ProviderClient`.  When
        ``None``, provider attachment is skipped.
    policy_manager:
        A :class:`~nemoclaw.policy_manager.PolicyManager` (with a live
        ``PolicyClient``) used to apply presets after the sandbox is ready.
        When ``None``, presets are not applied.
    wait_timeout:
        Seconds to wait for the sandbox to become ready.
    """

    def __init__(
        self,
        sandbox_client: "SandboxClient | None" = None,
        provider_client: "ProviderClient | None" = None,
        policy_manager: PolicyManager | None = None,
        *,
        wait_timeout: float = 300.0,
    ) -> None:
        self._sandbox = sandbox_client
        self._provider = provider_client
        self._policy = policy_manager
        self._wait_timeout = wait_timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def onboard(
        self,
        sandbox_name: str,
        *,
        spec: Any,
        providers: list[str] | None = None,
        presets: list[str] | None = None,
        registry_entry: "_registry.SandboxEntry | None" = None,
    ) -> OnboardResult:
        """
        Create and register a new sandbox.

        Parameters
        ----------
        sandbox_name:
            Name for the new sandbox.  Must not already exist in the registry.
        spec:
            ``openshell_pb2.SandboxSpec`` describing the image, env, and GPU
            settings.  Callers build this before calling onboard.
        providers:
            Provider names to attach after the sandbox is ready.  Providers
            must already exist in OpenShell (created via ``ProviderClient``).
        presets:
            Policy preset names to apply after provider attachment.  Each
            preset must be resolvable by the ``PolicyEngine``.
        registry_entry:
            A fully-populated :class:`~nemoclaw.registry.SandboxEntry` to
            write on success.  When ``None``, a minimal entry is written using
            ``sandbox_name``, ``presets``, and image from ``spec``.  Pass a
            custom entry to include caller-specific metadata (e.g.
            ``folder_id``, ``model_id``) in the ``metadata`` field.

        Returns
        -------
        :class:`OnboardResult`
            ``ok=True`` when the sandbox was created, all providers attached,
            all presets applied, and the registry entry written.
        """
        if self._sandbox is None:
            raise OnboardOrchestratorError(
                "onboard requires a SandboxClient; construct OnboardOrchestrator with one."
            )

        providers = list(providers or [])
        presets = list(presets or [])
        steps: list[OnboardStep] = []

        # ------------------------------------------------------------------
        # Step 1: Preflight — sandbox must NOT already exist
        # ------------------------------------------------------------------
        existing = _registry.get_sandbox(sandbox_name)
        if existing is not None:
            steps.append(OnboardStep(
                name="preflight",
                ok=False,
                detail=f"sandbox {sandbox_name!r} already exists in registry",
            ))
            return OnboardResult(
                ok=False,
                sandbox_name=sandbox_name,
                steps=steps,
                error=f"sandbox {sandbox_name!r} already exists in registry",
            )
        steps.append(OnboardStep(name="preflight", ok=True))

        # ------------------------------------------------------------------
        # Step 2: Create sandbox
        # ------------------------------------------------------------------
        try:
            sandbox_ref = self._sandbox.create(spec=spec, name=sandbox_name)
            # The gateway assigns its own name regardless of the hint. Use the
            # name it actually assigned for all subsequent gateway API calls.
            effective_name = sandbox_ref.name or sandbox_name
            steps.append(OnboardStep(name="create", ok=True))
        except Exception as exc:  # noqa: BLE001
            steps.append(OnboardStep(name="create", ok=False, detail=str(exc)))
            return OnboardResult(
                ok=False,
                sandbox_name=sandbox_name,
                steps=steps,
                error=f"create failed: {exc}",
            )

        # ------------------------------------------------------------------
        # Step 3: Wait ready
        # ------------------------------------------------------------------
        try:
            self._sandbox.wait_ready(effective_name, timeout_seconds=self._wait_timeout)
            steps.append(OnboardStep(name="wait_ready", ok=True))
        except Exception as exc:  # noqa: BLE001
            steps.append(OnboardStep(name="wait_ready", ok=False, detail=str(exc)))
            return OnboardResult(
                ok=False,
                sandbox_name=effective_name,
                steps=steps,
                error=f"sandbox did not become ready: {exc}",
            )

        # ------------------------------------------------------------------
        # Step 4: Attach providers
        # ------------------------------------------------------------------
        attached: list[str] = []
        attach_failures: list[str] = []
        if self._provider is not None and providers:
            for pname in providers:
                try:
                    self._provider.attach(effective_name, pname)
                    attached.append(pname)
                except Exception as exc:  # noqa: BLE001
                    attach_failures.append(f"{pname}: {exc}")

            if attach_failures:
                steps.append(OnboardStep(
                    name="attach_providers",
                    ok=False,
                    detail=f"failed: {'; '.join(attach_failures)}; ok: {', '.join(attached)}",
                ))
            else:
                steps.append(OnboardStep(
                    name="attach_providers",
                    ok=True,
                    detail=f"attached {len(attached)} provider(s)",
                ))

        # ------------------------------------------------------------------
        # Step 5: Apply policy presets
        # ------------------------------------------------------------------
        applied: list[str] = []
        preset_failures: list[str] = []
        if self._policy is not None and presets:
            for preset_name in presets:
                try:
                    result: PolicyAddResult = self._policy.add_preset(effective_name, preset_name)
                    if result.ok:
                        applied.append(preset_name)
                    else:
                        preset_failures.append(f"{preset_name}: {result.error}")
                except Exception as exc:  # noqa: BLE001
                    preset_failures.append(f"{preset_name}: {exc}")

            if preset_failures:
                steps.append(OnboardStep(
                    name="apply_presets",
                    ok=False,
                    detail=f"failed: {'; '.join(preset_failures)}; ok: {', '.join(applied)}",
                ))
            else:
                steps.append(OnboardStep(
                    name="apply_presets",
                    ok=True,
                    detail=f"applied {len(applied)} preset(s)",
                ))

        # ------------------------------------------------------------------
        # Step 6: Register in local registry
        # ------------------------------------------------------------------
        # If the caller supplied a registry_entry, update its name to the
        # gateway-assigned name so the registry key matches what the gateway knows.
        if registry_entry is not None and registry_entry.name != effective_name:
            import dataclasses
            entry = dataclasses.replace(registry_entry, name=effective_name)
        else:
            entry = registry_entry or _registry.SandboxEntry(
                name=effective_name,
                created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                image_tag=_image_from_spec(spec),
                policies=list(applied),
            )
        try:
            _registry.register_sandbox(entry)
            steps.append(OnboardStep(name="register", ok=True))
        except Exception as exc:  # noqa: BLE001
            steps.append(OnboardStep(name="register", ok=False, detail=str(exc)))
            # Non-fatal for the sandbox itself — it is running — but signal failure.
            failed_steps = [s for s in steps if not s.ok]
            return OnboardResult(
                ok=False,
                sandbox_name=effective_name,
                steps=steps,
                attached_providers=attached,
                applied_presets=applied,
                error=failed_steps[0].detail if failed_steps else str(exc),
            )

        # ------------------------------------------------------------------
        # Overall result
        # ------------------------------------------------------------------
        failed_steps = [s for s in steps if not s.ok]
        ok = len(failed_steps) == 0
        return OnboardResult(
            ok=ok,
            sandbox_name=effective_name,
            steps=steps,
            attached_providers=attached,
            applied_presets=applied,
            error=failed_steps[0].detail if not ok and failed_steps else "",
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _image_from_spec(spec: Any) -> str | None:
    """Best-effort image tag extraction from a SandboxSpec proto."""
    try:
        return spec.template.image or None
    except AttributeError:
        return None
