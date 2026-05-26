# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pathlib
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import pytest

from nemoclaw.skill_deployer import (
    DeployResult,
    SkillDeployer,
    SkillDeployerError,
    _collect_files,
    parse_skill_frontmatter,
)


# ---------------------------------------------------------------------------
# Fake SSH infra
# ---------------------------------------------------------------------------

@dataclass
class _FakeSandboxRef:
    id: str
    name: str
    phase: int = 2


@dataclass
class _FakeSshSessionRef:
    sandbox_id: str
    token: str
    gateway_host: str = "gw.example.com"
    gateway_port: int = 8443
    gateway_scheme: str = "ssh"
    connect_path: str = ""
    host_key_fingerprint: str = "SHA256:abc"
    expires_at_ms: int = 9_999_999


class _FakeSandboxClient:
    def __init__(
        self,
        *,
        fail_get: bool = False,
        fail_create_session: bool = False,
        fail_revoke: bool = False,
    ) -> None:
        self._fail_get = fail_get
        self._fail_create_session = fail_create_session
        self._fail_revoke = fail_revoke
        self.revoked_tokens: list[str] = []

    def get(self, sandbox_name: str) -> _FakeSandboxRef:
        if self._fail_get:
            raise RuntimeError(f"sandbox {sandbox_name!r} not found")
        return _FakeSandboxRef(id="sb-001", name=sandbox_name)

    def create_ssh_session(self, sandbox_id: str) -> _FakeSshSessionRef:
        if self._fail_create_session:
            raise RuntimeError("create_ssh_session failed")
        return _FakeSshSessionRef(sandbox_id=sandbox_id, token="tok-test")

    def revoke_ssh_session(self, token: str) -> bool:
        if self._fail_revoke:
            raise RuntimeError("revoke failed")
        self.revoked_tokens.append(token)
        return True


# ---------------------------------------------------------------------------
# Fake SSH runner factory
# ---------------------------------------------------------------------------

class _FakeRunner:
    """Records all (command, stdin) calls and returns preset responses."""

    def __init__(self, responses: list[tuple[int, str]] | None = None) -> None:
        # Each entry is consumed once; last entry is reused if list is exhausted.
        self._responses = list(responses or [])
        self.calls: list[tuple[str, bytes | None]] = []

    def __call__(self, command: str, stdin: bytes | None = None) -> tuple[int, str]:
        self.calls.append((command, stdin))
        if self._responses:
            r = self._responses.pop(0)
        else:
            r = (0, "EXISTS")
        return r


def _make_factory(runner: _FakeRunner):
    @contextmanager
    def _factory(session_ref) -> Iterator[_FakeRunner]:  # noqa: ANN001
        yield runner
    return _factory


# ---------------------------------------------------------------------------
# Skill directory helpers
# ---------------------------------------------------------------------------

_VALID_FRONTMATTER = "---\nname: my-skill\n---\n"


def _write_skill(
    base: pathlib.Path,
    *,
    frontmatter: str = _VALID_FRONTMATTER,
    extra_files: dict[str, str] | None = None,
) -> pathlib.Path:
    skill_dir = base / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(frontmatter + "\nSkill body.")
    for name, content in (extra_files or {}).items():
        p = skill_dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return skill_dir


def _default_runner(
    *,
    upload_ok: bool = True,
    clear_ok: bool = True,
    verify_ok: bool = True,
) -> _FakeRunner:
    """Build a runner whose responses match the default 3-step sequence."""
    def _rc(ok: bool, out: str = "") -> tuple[int, str]:
        return (0 if ok else 1, out)

    return _FakeRunner([
        _rc(upload_ok),   # SKILL.md upload
        _rc(clear_ok),    # clear sessions
        _rc(verify_ok, "EXISTS" if verify_ok else ""),  # verify
    ])


def _make_deployer(
    sc: _FakeSandboxClient,
    runner: _FakeRunner,
    *,
    sessions_file: str | None = "/sandbox/.openclaw/agents/main/sessions/sessions.json",
) -> SkillDeployer:
    return SkillDeployer(
        sc,
        ssh_runner_factory=_make_factory(runner),
        sessions_file=sessions_file,
    )


# ===========================================================================
# parse_skill_frontmatter
# ===========================================================================

def test_parse_valid_frontmatter() -> None:
    assert parse_skill_frontmatter("---\nname: my-skill\n---\n") == "my-skill"


def test_parse_missing_opening_delimiter() -> None:
    with pytest.raises(ValueError, match="opening ---"):
        parse_skill_frontmatter("name: my-skill\n---\n")


def test_parse_missing_closing_delimiter() -> None:
    with pytest.raises(ValueError, match="closing ---"):
        parse_skill_frontmatter("---\nname: my-skill\n")


def test_parse_invalid_yaml() -> None:
    with pytest.raises(ValueError, match="not valid YAML"):
        parse_skill_frontmatter("---\n: : :\n---\n")


def test_parse_missing_name_field() -> None:
    with pytest.raises(ValueError, match="missing required 'name'"):
        parse_skill_frontmatter("---\ndescription: hello\n---\n")


def test_parse_empty_name() -> None:
    with pytest.raises(ValueError, match="missing required 'name'"):
        parse_skill_frontmatter("---\nname: \n---\n")


def test_parse_invalid_name_chars() -> None:
    with pytest.raises(ValueError, match="invalid characters"):
        parse_skill_frontmatter("---\nname: my skill!\n---\n")


def test_parse_name_with_dots_and_dashes() -> None:
    assert parse_skill_frontmatter("---\nname: my.skill-v2\n---\n") == "my.skill-v2"


def test_parse_extra_frontmatter_fields_ignored() -> None:
    fm = "---\nname: alpha\ndescription: A skill\nversion: 1\n---\n"
    assert parse_skill_frontmatter(fm) == "alpha"


def test_parse_non_mapping_frontmatter() -> None:
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        parse_skill_frontmatter("---\n- item1\n- item2\n---\n")


# ===========================================================================
# _collect_files
# ===========================================================================

def test_collect_files_basic(tmp_path: pathlib.Path) -> None:
    (tmp_path / "SKILL.md").write_text("x")
    (tmp_path / "handler.py").write_text("x")
    safe, dots, unsafe = _collect_files(tmp_path)
    assert "SKILL.md" in safe
    assert "handler.py" in safe
    assert dots == []
    assert unsafe == []


def test_collect_files_skips_dotfiles(tmp_path: pathlib.Path) -> None:
    (tmp_path / ".hidden").write_text("x")
    (tmp_path / "visible.py").write_text("x")
    safe, dots, unsafe = _collect_files(tmp_path)
    assert ".hidden" in dots
    assert ".hidden" not in safe


def test_collect_files_skips_dotfile_in_subdir(tmp_path: pathlib.Path) -> None:
    sub = tmp_path / ".git"
    sub.mkdir()
    (sub / "config").write_text("x")
    (tmp_path / "real.py").write_text("x")
    safe, dots, _ = _collect_files(tmp_path)
    assert "real.py" in safe
    assert all(".git" in d for d in dots)


def test_collect_files_nested_structure(tmp_path: pathlib.Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "helper.py").write_text("x")
    safe, _, _ = _collect_files(tmp_path)
    assert "sub/helper.py" in safe


def test_collect_files_rejects_unsafe_chars(tmp_path: pathlib.Path) -> None:
    (tmp_path / "SKILL.md").write_text("x")
    # Directly create via pathlib (bypasses shell)
    bad = tmp_path / "bad file.py"
    bad.write_text("x")
    _, _, unsafe = _collect_files(tmp_path)
    assert any("bad" in u for u in unsafe)


# ===========================================================================
# deploy() — requires client
# ===========================================================================

def test_deploy_requires_client() -> None:
    mgr = SkillDeployer()
    with pytest.raises(SkillDeployerError, match="SandboxClient"):
        mgr.deploy("box", "/tmp/skill")


# ===========================================================================
# Preflight errors
# ===========================================================================

def test_deploy_missing_skill_md(tmp_path: pathlib.Path) -> None:
    sc = _FakeSandboxClient()
    runner = _FakeRunner()
    mgr = _make_deployer(sc, runner)
    result = mgr.deploy("box", tmp_path)
    assert result.ok is False
    assert "SKILL.md" in result.error


def test_deploy_invalid_frontmatter(tmp_path: pathlib.Path) -> None:
    (tmp_path / "SKILL.md").write_text("---\ndescription: no name\n---\n")
    sc = _FakeSandboxClient()
    runner = _FakeRunner()
    mgr = _make_deployer(sc, runner)
    result = mgr.deploy("box", tmp_path)
    assert result.ok is False
    assert "preflight" in [s.name for s in result.steps]


def test_deploy_empty_skill_dir(tmp_path: pathlib.Path) -> None:
    # SKILL.md but hidden everything else — only SKILL.md itself is safe
    # Actually, let's test a dir where only dotfiles exist
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(_VALID_FRONTMATTER)
    (skill_dir / ".hidden").write_text("x")
    sc = _FakeSandboxClient()
    runner = _default_runner()
    mgr = _make_deployer(sc, runner)
    # SKILL.md is in safe files, so deploy should proceed
    result = mgr.deploy("box", skill_dir)
    assert result.ok is True


def test_deploy_unsafe_paths_abort_preflight(tmp_path: pathlib.Path) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(_VALID_FRONTMATTER)
    bad = skill_dir / "bad file.txt"
    bad.write_text("x")
    sc = _FakeSandboxClient()
    runner = _FakeRunner()
    mgr = _make_deployer(sc, runner)
    result = mgr.deploy("box", skill_dir)
    assert result.ok is False
    assert "unsafe" in result.error


# ===========================================================================
# Session errors
# ===========================================================================

def test_deploy_sandbox_not_found_returns_error(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path)
    sc = _FakeSandboxClient(fail_get=True)
    runner = _FakeRunner()
    mgr = _make_deployer(sc, runner)
    result = mgr.deploy("ghost", skill_dir)
    assert result.ok is False
    assert "could not look up" in result.error


def test_deploy_create_session_failure_returns_error(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path)
    sc = _FakeSandboxClient(fail_create_session=True)
    runner = _FakeRunner()
    mgr = _make_deployer(sc, runner)
    result = mgr.deploy("box", skill_dir)
    assert result.ok is False
    assert "create_ssh_session" in result.error


# ===========================================================================
# Happy path
# ===========================================================================

def test_deploy_ok(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path)
    sc = _FakeSandboxClient()
    runner = _default_runner()
    mgr = _make_deployer(sc, runner)
    result = mgr.deploy("box", skill_dir)
    assert result.ok is True


def test_deploy_skill_name_in_result(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path)
    sc = _FakeSandboxClient()
    runner = _default_runner()
    mgr = _make_deployer(sc, runner)
    result = mgr.deploy("box", skill_dir)
    assert result.skill_name == "my-skill"


def test_deploy_sandbox_name_in_result(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path)
    sc = _FakeSandboxClient()
    runner = _default_runner()
    mgr = _make_deployer(sc, runner)
    result = mgr.deploy("box", skill_dir)
    assert result.sandbox_name == "box"


def test_deploy_steps_recorded(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path)
    sc = _FakeSandboxClient()
    runner = _default_runner()
    mgr = _make_deployer(sc, runner)
    result = mgr.deploy("box", skill_dir)
    step_names = [s.name for s in result.steps]
    assert "preflight" in step_names
    assert "create_session" in step_names
    assert "upload" in step_names
    assert "clear_sessions" in step_names
    assert "verify" in step_names


def test_deploy_all_steps_ok(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path)
    sc = _FakeSandboxClient()
    runner = _default_runner()
    mgr = _make_deployer(sc, runner)
    result = mgr.deploy("box", skill_dir)
    assert all(s.ok for s in result.steps)


def test_deploy_uploaded_files_in_result(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path, extra_files={"handler.py": "x"})
    sc = _FakeSandboxClient()
    # 2 uploads + 1 clear_sessions + 1 verify (needs "EXISTS")
    runner = _FakeRunner([(0, ""), (0, ""), (0, ""), (0, "EXISTS")])
    mgr = _make_deployer(sc, runner)
    result = mgr.deploy("box", skill_dir)
    assert result.ok is True
    assert "SKILL.md" in result.uploaded_files
    assert "handler.py" in result.uploaded_files


def test_deploy_revokes_session_on_success(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path)
    sc = _FakeSandboxClient()
    runner = _default_runner()
    mgr = _make_deployer(sc, runner)
    mgr.deploy("box", skill_dir)
    assert "tok-test" in sc.revoked_tokens


# ===========================================================================
# Upload command shape
# ===========================================================================

def test_deploy_upload_uses_mkdir_and_cat(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path)
    sc = _FakeSandboxClient()
    runner = _default_runner()
    mgr = _make_deployer(sc, runner)
    mgr.deploy("box", skill_dir)
    upload_call = runner.calls[0]
    assert "mkdir -p" in upload_call[0]
    assert "cat >" in upload_call[0]


def test_deploy_upload_sends_file_content_as_stdin(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path)
    sc = _FakeSandboxClient()
    runner = _default_runner()
    mgr = _make_deployer(sc, runner)
    mgr.deploy("box", skill_dir)
    _, stdin = runner.calls[0]
    assert stdin is not None
    assert len(stdin) > 0


def test_deploy_upload_path_includes_skill_name(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path)
    sc = _FakeSandboxClient()
    runner = _default_runner()
    mgr = _make_deployer(sc, runner)
    mgr.deploy("box", skill_dir)
    cmd = runner.calls[0][0]
    assert "my-skill" in cmd


def test_deploy_upload_nested_file_creates_subdir(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path, extra_files={"lib/helper.py": "x"})
    sc = _FakeSandboxClient()
    runner = _FakeRunner([(0, "")] * 10)
    mgr = _make_deployer(sc, runner)
    mgr.deploy("box", skill_dir)
    # Find the command that uploads lib/helper.py
    lib_cmd = next((cmd for cmd, _ in runner.calls if "helper.py" in cmd), None)
    assert lib_cmd is not None
    assert "lib" in lib_cmd


# ===========================================================================
# Clear sessions
# ===========================================================================

def test_deploy_clear_sessions_uses_printf(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path)
    sc = _FakeSandboxClient()
    runner = _default_runner()
    mgr = _make_deployer(sc, runner)
    mgr.deploy("box", skill_dir)
    clear_cmd = next((cmd for cmd, _ in runner.calls if "printf" in cmd), None)
    assert clear_cmd is not None
    assert "sessions.json" in clear_cmd


def test_deploy_skips_clear_sessions_when_no_file(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path)
    sc = _FakeSandboxClient()
    # only 2 responses needed (upload + verify); no clear_sessions call
    runner = _FakeRunner([(0, ""), (0, "EXISTS")])
    mgr = _make_deployer(sc, runner, sessions_file=None)
    result = mgr.deploy("box", skill_dir)
    assert result.ok is True
    step_names = [s.name for s in result.steps]
    assert "clear_sessions" not in step_names


def test_deploy_custom_sessions_file(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path)
    sc = _FakeSandboxClient()
    runner = _default_runner()
    mgr = _make_deployer(sc, runner, sessions_file="/custom/path/sessions.json")
    mgr.deploy("box", skill_dir)
    clear_cmd = next((cmd for cmd, _ in runner.calls if "printf" in cmd), None)
    assert clear_cmd is not None
    assert "/custom/path/sessions.json" in clear_cmd


# ===========================================================================
# Verify step
# ===========================================================================

def test_deploy_verify_checks_skill_md(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path)
    sc = _FakeSandboxClient()
    runner = _default_runner()
    mgr = _make_deployer(sc, runner)
    mgr.deploy("box", skill_dir)
    verify_cmd = next((cmd for cmd, _ in runner.calls if "test -f" in cmd), None)
    assert verify_cmd is not None
    assert "SKILL.md" in verify_cmd


# ===========================================================================
# Failure paths
# ===========================================================================

def test_deploy_upload_failure_returns_error(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path)
    sc = _FakeSandboxClient()
    runner = _FakeRunner([(1, "")])  # upload fails
    mgr = _make_deployer(sc, runner)
    result = mgr.deploy("box", skill_dir)
    assert result.ok is False
    upload_step = next(s for s in result.steps if s.name == "upload")
    assert upload_step.ok is False


def test_deploy_upload_failure_still_revokes_session(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path)
    sc = _FakeSandboxClient()
    runner = _FakeRunner([(1, "")])
    mgr = _make_deployer(sc, runner)
    mgr.deploy("box", skill_dir)
    assert "tok-test" in sc.revoked_tokens


def test_deploy_clear_sessions_failure_returns_error(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path)
    sc = _FakeSandboxClient()
    runner = _FakeRunner([(0, ""), (1, "")])  # upload ok, clear fails
    mgr = _make_deployer(sc, runner)
    result = mgr.deploy("box", skill_dir)
    assert result.ok is False
    clear_step = next((s for s in result.steps if s.name == "clear_sessions"), None)
    assert clear_step is not None
    assert clear_step.ok is False


def test_deploy_verify_failure_returns_error(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path)
    sc = _FakeSandboxClient()
    runner = _FakeRunner([(0, ""), (0, ""), (1, "")])  # upload+clear ok, verify fails
    mgr = _make_deployer(sc, runner)
    result = mgr.deploy("box", skill_dir)
    assert result.ok is False
    verify_step = next(s for s in result.steps if s.name == "verify")
    assert verify_step.ok is False


def test_deploy_verify_wrong_stdout_fails(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path)
    sc = _FakeSandboxClient()
    runner = _FakeRunner([(0, ""), (0, ""), (0, "NOTEXISTS")])
    mgr = _make_deployer(sc, runner)
    result = mgr.deploy("box", skill_dir)
    assert result.ok is False


def test_deploy_revokes_session_on_failure(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path)
    sc = _FakeSandboxClient()
    runner = _FakeRunner([(1, "")])  # upload fails
    mgr = _make_deployer(sc, runner)
    mgr.deploy("box", skill_dir)
    assert "tok-test" in sc.revoked_tokens


def test_deploy_no_session_revoke_when_session_not_created(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path)
    sc = _FakeSandboxClient(fail_get=True)
    runner = _FakeRunner()
    mgr = _make_deployer(sc, runner)
    mgr.deploy("ghost", skill_dir)
    # No session was created, so nothing should be revoked
    assert sc.revoked_tokens == []


# ===========================================================================
# Dotfiles reported in result
# ===========================================================================

def test_deploy_dotfiles_in_result(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path)
    (skill_dir / ".hidden").write_text("x")
    sc = _FakeSandboxClient()
    runner = _default_runner()
    mgr = _make_deployer(sc, runner)
    result = mgr.deploy("box", skill_dir)
    assert result.ok is True
    assert ".hidden" in result.skipped_dotfiles


# ===========================================================================
# Custom agent_skills_dir
# ===========================================================================

def test_deploy_custom_skills_dir(tmp_path: pathlib.Path) -> None:
    skill_dir = _write_skill(tmp_path)
    sc = _FakeSandboxClient()
    # sessions_file=None means no clear_sessions call: 1 upload + 1 verify
    runner = _FakeRunner([(0, ""), (0, "EXISTS")])
    mgr = SkillDeployer(
        sc,
        agent_skills_dir="/sandbox/.hermes/skills",
        sessions_file=None,
        ssh_runner_factory=_make_factory(runner),
    )
    result = mgr.deploy("box", skill_dir)
    assert result.ok is True
    upload_cmd = runner.calls[0][0]
    assert ".hermes" in upload_cmd
