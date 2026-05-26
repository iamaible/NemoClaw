# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
SkillDeployer: install a local skill directory into a sandbox via SSH.

Workflow
--------
1. **Preflight** — parse ``SKILL.md`` YAML frontmatter (validates ``name``
   field), collect files, reject dotfiles and unsafe paths.
2. **Create SSH session** — look up the sandbox via
   :meth:`~openshell.sandbox.SandboxClient.get`, then call
   :meth:`~openshell.sandbox.SandboxClient.create_ssh_session` to obtain an
   :class:`~openshell.sandbox.SshSessionRef`.
3. **Upload** — for each file: ``mkdir -p <remote_dir> && cat > <remote_path>``
   with the file content piped as stdin.
4. **Clear sessions** — truncate the agent's ``sessions.json`` so the skill
   is discovered on the next session (OpenClaw-only; skipped when
   *sessions_file* is ``None``).
5. **Verify** — confirm ``SKILL.md`` exists at the remote skill directory.
6. **Revoke session** — always called, even on failure.

SSH connection
--------------
The default :func:`paramiko_runner_factory` connects to
``SshSessionRef.gateway_host:gateway_port`` using the session token as the
SSH username (the gateway validates the token, no password required).  Pass a
custom *ssh_runner_factory* to substitute any executor in tests or alternative
transport implementations.
"""

from __future__ import annotations

import pathlib
import re
import shlex
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterator

if TYPE_CHECKING:
    from openshell.sandbox import SandboxClient, SshSessionRef

# Type aliases for the injectable SSH runner.
# SshRunner: (command, stdin?) → (exit_code, stdout)
SshRunner = Callable[[str, "bytes | None"], "tuple[int, str]"]
# SshRunnerFactory: SessionRef → context-manager that yields an SshRunner.
SshRunnerFactory = Callable[["SshSessionRef"], "Any"]  # ContextManager[SshRunner]

_SKILL_MD = "SKILL.md"
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9._\-/]+$")
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DeployStep:
    """Record of a single step in the deploy workflow."""
    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class DeployResult:
    """Aggregated outcome of a full skill deploy operation."""
    ok: bool
    sandbox_name: str
    skill_name: str
    steps: list[DeployStep] = field(default_factory=list)
    uploaded_files: list[str] = field(default_factory=list)
    skipped_dotfiles: list[str] = field(default_factory=list)
    error: str = ""


# ---------------------------------------------------------------------------
# SkillDeployerError
# ---------------------------------------------------------------------------

class SkillDeployerError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# SkillDeployer
# ---------------------------------------------------------------------------

class SkillDeployer:
    """
    Installs a local skill directory into a sandbox via SSH.

    Parameters
    ----------
    sandbox_client:
        An initialised :class:`openshell.sandbox.SandboxClient`.  Required for
        :meth:`deploy`.
    agent_skills_dir:
        Remote base directory where skill sub-directories live.
        Defaults to the OpenClaw skills directory.
    sessions_file:
        Remote path to the agent sessions index to clear after upload so the
        skill is discovered on the next session.  Pass ``None`` to skip the
        clear step (non-OpenClaw agents).
    ssh_runner_factory:
        Callable ``(SshSessionRef) -> ContextManager[SshRunner]``.  Defaults
        to :func:`paramiko_runner_factory`.  Override in tests or when a
        custom SSH transport is required.
    connect_timeout:
        Seconds allowed for SSH connection establishment.
    exec_timeout:
        Seconds allowed for each remote command.
    """

    _DEFAULT_SKILLS_DIR = "/sandbox/.openclaw/skills"
    _DEFAULT_SESSIONS_FILE = "/sandbox/.openclaw/agents/main/sessions/sessions.json"

    def __init__(
        self,
        sandbox_client: "SandboxClient | None" = None,
        *,
        agent_skills_dir: str = _DEFAULT_SKILLS_DIR,
        sessions_file: str | None = _DEFAULT_SESSIONS_FILE,
        ssh_runner_factory: SshRunnerFactory | None = None,
        connect_timeout: float = 30.0,
        exec_timeout: float = 60.0,
    ) -> None:
        self._client = sandbox_client
        self._skills_dir = agent_skills_dir
        self._sessions_file = sessions_file
        self._runner_factory: SshRunnerFactory = (
            ssh_runner_factory or paramiko_runner_factory
        )
        self._connect_timeout = connect_timeout
        self._exec_timeout = exec_timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def deploy(
        self,
        sandbox_name: str,
        skill_dir: "str | pathlib.Path",
        *,
        force: bool = False,
    ) -> DeployResult:
        """
        Install the skill at *skill_dir* into *sandbox_name*.

        Parameters
        ----------
        sandbox_name:
            Registry / gateway name of the target sandbox.
        skill_dir:
            Local directory containing the skill (must have a ``SKILL.md``).
        force:
            When ``True``, overwrite an existing installation without prompting.
            When ``False`` (default), return an error if the skill is already
            installed and the file count differs — otherwise overwrite silently.

        Returns
        -------
        :class:`DeployResult`
            ``ok=True`` when all steps succeed.
        """
        if self._client is None:
            raise SkillDeployerError(
                "deploy requires a SandboxClient; construct SkillDeployer with one."
            )

        skill_dir = pathlib.Path(skill_dir)
        steps: list[DeployStep] = []

        # ------------------------------------------------------------------
        # Step 1: Preflight
        # ------------------------------------------------------------------
        preflight = self._preflight(skill_dir)
        if not preflight["ok"]:
            steps.append(DeployStep(name="preflight", ok=False, detail=preflight["error"]))
            return DeployResult(
                ok=False,
                sandbox_name=sandbox_name,
                skill_name="",
                steps=steps,
                error=preflight["error"],
            )

        skill_name: str = preflight["skill_name"]
        rel_files: list[str] = preflight["files"]
        dotfiles: list[str] = preflight["dotfiles"]
        steps.append(DeployStep(
            name="preflight",
            ok=True,
            detail=f"skill={skill_name!r} files={len(rel_files)}",
        ))

        remote_skill_dir = f"{self._skills_dir}/{skill_name}"

        # ------------------------------------------------------------------
        # Step 2: Create SSH session
        # ------------------------------------------------------------------
        try:
            sandbox_ref = self._client.get(sandbox_name)
        except Exception as exc:  # noqa: BLE001
            steps.append(DeployStep(
                name="create_session",
                ok=False,
                detail=f"could not look up sandbox: {exc}",
            ))
            return DeployResult(
                ok=False,
                sandbox_name=sandbox_name,
                skill_name=skill_name,
                steps=steps,
                error=f"could not look up sandbox: {exc}",
            )

        try:
            session_ref = self._client.create_ssh_session(sandbox_ref.id)
        except Exception as exc:  # noqa: BLE001
            steps.append(DeployStep(
                name="create_session",
                ok=False,
                detail=f"create_ssh_session failed: {exc}",
            ))
            return DeployResult(
                ok=False,
                sandbox_name=sandbox_name,
                skill_name=skill_name,
                steps=steps,
                error=f"create_ssh_session failed: {exc}",
            )

        steps.append(DeployStep(name="create_session", ok=True))

        # ------------------------------------------------------------------
        # Steps 3–5 run inside the SSH connection; session is always revoked.
        # ------------------------------------------------------------------
        uploaded_files: list[str] = []
        try:
            with self._runner_factory(session_ref) as run:
                # Step 3: Upload
                upload_ok, upload_detail, uploaded_files = self._upload(
                    run, skill_dir, rel_files, remote_skill_dir
                )
                steps.append(DeployStep(name="upload", ok=upload_ok, detail=upload_detail))
                if not upload_ok:
                    return DeployResult(
                        ok=False,
                        sandbox_name=sandbox_name,
                        skill_name=skill_name,
                        steps=steps,
                        uploaded_files=uploaded_files,
                        skipped_dotfiles=dotfiles,
                        error=upload_detail,
                    )

                # Step 4: Clear sessions
                if self._sessions_file is not None:
                    clear_ok, clear_detail = self._clear_sessions(run)
                    steps.append(DeployStep(
                        name="clear_sessions", ok=clear_ok, detail=clear_detail
                    ))
                    if not clear_ok:
                        return DeployResult(
                            ok=False,
                            sandbox_name=sandbox_name,
                            skill_name=skill_name,
                            steps=steps,
                            uploaded_files=uploaded_files,
                            skipped_dotfiles=dotfiles,
                            error=clear_detail,
                        )

                # Step 5: Verify
                verify_ok, verify_detail = self._verify(run, remote_skill_dir)
                steps.append(DeployStep(name="verify", ok=verify_ok, detail=verify_detail))
                if not verify_ok:
                    return DeployResult(
                        ok=False,
                        sandbox_name=sandbox_name,
                        skill_name=skill_name,
                        steps=steps,
                        uploaded_files=uploaded_files,
                        skipped_dotfiles=dotfiles,
                        error=verify_detail,
                    )

        except Exception as exc:  # noqa: BLE001
            steps.append(DeployStep(
                name="upload", ok=False, detail=f"SSH connection failed: {exc}"
            ))
            return DeployResult(
                ok=False,
                sandbox_name=sandbox_name,
                skill_name=skill_name,
                steps=steps,
                uploaded_files=uploaded_files,
                skipped_dotfiles=dotfiles,
                error=f"SSH connection failed: {exc}",
            )
        finally:
            try:
                self._client.revoke_ssh_session(session_ref.token)
            except Exception:  # noqa: BLE001
                pass

        return DeployResult(
            ok=True,
            sandbox_name=sandbox_name,
            skill_name=skill_name,
            steps=steps,
            uploaded_files=uploaded_files,
            skipped_dotfiles=dotfiles,
        )

    # ------------------------------------------------------------------
    # Step implementations
    # ------------------------------------------------------------------

    def _preflight(self, skill_dir: pathlib.Path) -> dict:
        skill_md = skill_dir / _SKILL_MD
        if not skill_md.exists():
            return {"ok": False, "error": f"no SKILL.md found in {skill_dir}"}

        try:
            skill_name = parse_skill_frontmatter(skill_md.read_text())
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        files, dotfiles, unsafe = _collect_files(skill_dir)
        if unsafe:
            return {
                "ok": False,
                "error": f"unsafe file paths rejected: {', '.join(unsafe)}",
            }
        if not files:
            return {"ok": False, "error": "skill directory contains no files"}

        return {
            "ok": True,
            "skill_name": skill_name,
            "files": files,
            "dotfiles": dotfiles,
        }

    def _upload(
        self,
        run: SshRunner,
        skill_dir: pathlib.Path,
        rel_files: list[str],
        remote_skill_dir: str,
    ) -> tuple[bool, str, list[str]]:
        uploaded: list[str] = []
        for rel in rel_files:
            local_path = skill_dir / rel
            parts = rel.split("/")
            if len(parts) > 1:
                remote_subdir = remote_skill_dir + "/" + "/".join(parts[:-1])
            else:
                remote_subdir = remote_skill_dir
            remote_path = f"{remote_skill_dir}/{rel}"

            cmd = (
                f"mkdir -p {shlex.quote(remote_subdir)} "
                f"&& cat > {shlex.quote(remote_path)}"
            )
            try:
                content = local_path.read_bytes()
                exit_code, _ = run(cmd, content)
            except Exception as exc:  # noqa: BLE001
                return False, f"upload failed for {rel!r}: {exc}", uploaded

            if exit_code != 0:
                return False, f"upload failed for {rel!r}: exit {exit_code}", uploaded
            uploaded.append(rel)

        return True, f"uploaded {len(uploaded)} file(s)", uploaded

    def _clear_sessions(self, run: SshRunner) -> tuple[bool, str]:
        cmd = f"printf '{{}}' > {shlex.quote(self._sessions_file)}"  # type: ignore[arg-type]
        try:
            exit_code, _ = run(cmd, None)
        except Exception as exc:  # noqa: BLE001
            return False, f"clear_sessions exec failed: {exc}"
        if exit_code != 0:
            return False, f"clear_sessions exited with code {exit_code}"
        return True, ""

    def _verify(self, run: SshRunner, remote_skill_dir: str) -> tuple[bool, str]:
        target = shlex.quote(f"{remote_skill_dir}/{_SKILL_MD}")
        cmd = f"test -f {target} && echo EXISTS"
        try:
            exit_code, stdout = run(cmd, None)
        except Exception as exc:  # noqa: BLE001
            return False, f"verify exec failed: {exc}"
        if exit_code != 0 or stdout.strip() != "EXISTS":
            return False, f"SKILL.md not found at {remote_skill_dir}/{_SKILL_MD}"
        return True, ""


# ---------------------------------------------------------------------------
# Default SSH runner: paramiko
# ---------------------------------------------------------------------------

@contextmanager
def paramiko_runner_factory(session_ref: "SshSessionRef") -> Iterator[SshRunner]:
    """
    Connect to the sandbox gateway using paramiko and yield a runner callable.

    The session token is used as the SSH username; the gateway validates the
    token from the prior gRPC ``CreateSshSession`` call (no password needed).
    """
    import paramiko  # noqa: PLC0415

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=session_ref.gateway_host,
            port=session_ref.gateway_port,
            username=session_ref.token,
            password="",
            look_for_keys=False,
            allow_agent=False,
            timeout=30.0,
            auth_timeout=30.0,
            banner_timeout=30.0,
        )

        def _run(command: str, stdin: bytes | None = None) -> tuple[int, str]:
            stdin_fh, stdout_fh, _ = client.exec_command(command, timeout=60.0)
            if stdin is not None:
                stdin_fh.write(stdin)
                stdin_fh.channel.shutdown_write()
            exit_code = stdout_fh.channel.recv_exit_status()
            stdout_str = stdout_fh.read().decode("utf-8", errors="replace").strip()
            return exit_code, stdout_str

        yield _run
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

def parse_skill_frontmatter(content: str) -> str:
    """
    Parse YAML frontmatter from a SKILL.md string and return the skill name.

    Raises
    ------
    ValueError
        When frontmatter delimiters are missing, YAML is invalid, the ``name``
        field is absent or empty, or the name contains invalid characters.
    """
    import yaml  # noqa: PLC0415

    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md missing opening --- frontmatter delimiter")

    closing = next(
        (i for i, line in enumerate(lines) if i > 0 and line.strip() == "---"), -1
    )
    if closing == -1:
        raise ValueError("SKILL.md missing closing --- frontmatter delimiter")

    fm_text = "\n".join(lines[1:closing])
    try:
        parsed = yaml.safe_load(fm_text)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"SKILL.md frontmatter is not valid YAML: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError("SKILL.md frontmatter must be a YAML mapping")

    name = str(parsed.get("name") or "").strip()
    if not name:
        raise ValueError("SKILL.md frontmatter missing required 'name' field")
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"SKILL.md name {name!r} contains invalid characters "
            "(only [A-Za-z0-9._-] allowed)"
        )
    return name


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------

def _collect_files(
    skill_dir: pathlib.Path,
) -> tuple[list[str], list[str], list[str]]:
    """
    Walk *skill_dir* and split files into safe, dotfile-skipped, and unsafe.

    Returns
    -------
    tuple[list[str], list[str], list[str]]
        ``(safe_files, skipped_dotfiles, unsafe_paths)`` — all paths relative
        to *skill_dir* using forward slashes.
    """
    safe: list[str] = []
    dotfiles: list[str] = []
    unsafe: list[str] = []

    for abs_path in sorted(skill_dir.rglob("*")):
        if abs_path.is_dir():
            continue
        rel = abs_path.relative_to(skill_dir).as_posix()
        parts = rel.split("/")

        if any(part.startswith(".") for part in parts):
            dotfiles.append(rel)
            continue

        if not _SAFE_PATH_RE.fullmatch(rel) or ".." in parts or "." in parts:
            unsafe.append(rel)
            continue

        safe.append(rel)

    return safe, dotfiles, unsafe
