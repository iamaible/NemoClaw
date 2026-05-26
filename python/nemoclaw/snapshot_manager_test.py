# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import Any, Iterator

import pytest

from nemoclaw.snapshot_manager import (
    SnapshotManager,
    SnapshotManagerError,
    SnapshotResult,
    RestoreResult,
    SnapshotInfo,
)


# ---------------------------------------------------------------------------
# Fake openshell.sandbox types
# ---------------------------------------------------------------------------

@dataclass
class _FakeExecChunk:
    stream: str
    data: bytes


@dataclass
class _FakeExecResult:
    exit_code: int


@dataclass
class _FakeSandboxRef:
    id: str
    name: str


class _FakeSandboxClient:
    """Simulates SandboxClient.get() and exec_stream()."""

    def __init__(
        self,
        *,
        sandbox_id: str = "sb-001",
        sandbox_name: str = "box",
        fail_get: bool = False,
        # backup exec: list of (stream, data) + final exit_code
        stdout_bytes: bytes = b"",
        exec_exit_code: int = 0,
        # restore exec exit code
        restore_exit_code: int = 0,
        fail_exec: bool = False,
    ) -> None:
        self._sandbox_id = sandbox_id
        self._sandbox_name = sandbox_name
        self._fail_get = fail_get
        self._stdout_bytes = stdout_bytes
        self._exec_exit_code = exec_exit_code
        self._restore_exit_code = restore_exit_code
        self._fail_exec = fail_exec

        # recorded calls
        self.exec_commands: list[list[str]] = []
        self.exec_stdins: list[bytes | None] = []

    def get(self, sandbox_name: str) -> _FakeSandboxRef:
        if self._fail_get:
            raise RuntimeError(f"sandbox {sandbox_name!r} not found")
        return _FakeSandboxRef(id=self._sandbox_id, name=self._sandbox_name)

    def exec_stream(
        self,
        sandbox_id: str,
        command: list[str],
        *,
        stdin: bytes | None = None,
        timeout_seconds: int = 300,
    ) -> Iterator[Any]:
        self.exec_commands.append(command)
        self.exec_stdins.append(stdin)

        if self._fail_exec:
            raise RuntimeError("exec_stream connection failed")

        # Yield stdout chunks for backup, nothing for restore.
        if stdin is None and self._stdout_bytes:
            # Split into two chunks to verify concatenation.
            mid = len(self._stdout_bytes) // 2
            yield _FakeExecChunk(stream="stdout", data=self._stdout_bytes[:mid])
            yield _FakeExecChunk(stream="stdout", data=self._stdout_bytes[mid:])
            yield _FakeExecChunk(stream="stderr", data=b"some noise")

        yield _FakeExecResult(exit_code=self._restore_exit_code if stdin is not None else self._exec_exit_code)


# ---------------------------------------------------------------------------
# Fixtures: patch openshell.sandbox names so isinstance() checks pass
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_exec_types(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ExecChunk / ExecResult resolve to our fake dataclasses."""
    import openshell.sandbox as _os_sb  # noqa: PLC0415
    monkeypatch.setattr(_os_sb, "ExecChunk", _FakeExecChunk)
    monkeypatch.setattr(_os_sb, "ExecResult", _FakeExecResult)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_manager(
    client: _FakeSandboxClient | None = None,
    *,
    snapshots_dir: pathlib.Path,
) -> SnapshotManager:
    return SnapshotManager(client, snapshots_dir=snapshots_dir)


_FAKE_TAR = b"\x1f\x8b\x08fake-tar-archive-bytes" + b"\x00" * 20


# ===========================================================================
# Error: no client
# ===========================================================================

def test_backup_requires_client(tmp_path: pathlib.Path) -> None:
    mgr = _make_manager(snapshots_dir=tmp_path)
    with pytest.raises(SnapshotManagerError, match="SandboxClient"):
        mgr.backup("box", ["/workspace"])


def test_restore_requires_client(tmp_path: pathlib.Path) -> None:
    mgr = _make_manager(snapshots_dir=tmp_path)
    with pytest.raises(SnapshotManagerError, match="SandboxClient"):
        mgr.restore("box", "2026-01-01T00:00:00Z")


# ===========================================================================
# Backup — happy path
# ===========================================================================

def test_backup_returns_ok(tmp_path: pathlib.Path) -> None:
    sc = _FakeSandboxClient(stdout_bytes=_FAKE_TAR)
    mgr = _make_manager(sc, snapshots_dir=tmp_path)
    result = mgr.backup("box", ["/workspace"])
    assert result.ok is True
    assert result.sandbox_name == "box"


def test_backup_writes_archive(tmp_path: pathlib.Path) -> None:
    sc = _FakeSandboxClient(stdout_bytes=_FAKE_TAR)
    mgr = _make_manager(sc, snapshots_dir=tmp_path)
    result = mgr.backup("box", ["/workspace"])
    assert result.archive_path is not None
    assert result.archive_path.exists()
    assert result.archive_path.read_bytes() == _FAKE_TAR


def test_backup_writes_manifest(tmp_path: pathlib.Path) -> None:
    sc = _FakeSandboxClient(stdout_bytes=_FAKE_TAR)
    mgr = _make_manager(sc, snapshots_dir=tmp_path)
    result = mgr.backup("box", ["/workspace", "/data"], label="pre-upgrade")
    manifest_path = result.archive_path.parent / "manifest.json"  # type: ignore[union-attr]
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["sandbox_name"] == "box"
    assert manifest["label"] == "pre-upgrade"
    assert "/workspace" in manifest["paths"]
    assert "/data" in manifest["paths"]


def test_backup_size_bytes(tmp_path: pathlib.Path) -> None:
    sc = _FakeSandboxClient(stdout_bytes=_FAKE_TAR)
    mgr = _make_manager(sc, snapshots_dir=tmp_path)
    result = mgr.backup("box", ["/workspace"])
    assert result.size_bytes == len(_FAKE_TAR)


def test_backup_timestamp_in_result(tmp_path: pathlib.Path) -> None:
    sc = _FakeSandboxClient(stdout_bytes=_FAKE_TAR)
    mgr = _make_manager(sc, snapshots_dir=tmp_path)
    result = mgr.backup("box", ["/workspace"])
    assert result.timestamp != ""
    # ISO-8601 UTC format YYYY-MM-DDTHH:MM:SSZ
    assert result.timestamp.endswith("Z")
    assert "T" in result.timestamp


def test_backup_tar_command_uses_paths(tmp_path: pathlib.Path) -> None:
    sc = _FakeSandboxClient(stdout_bytes=_FAKE_TAR)
    mgr = _make_manager(sc, snapshots_dir=tmp_path)
    mgr.backup("box", ["/workspace", "/home/user"])
    cmd = sc.exec_commands[0]
    assert "tar" in cmd
    assert "/workspace" in cmd
    assert "/home/user" in cmd


def test_backup_storage_layout(tmp_path: pathlib.Path) -> None:
    sc = _FakeSandboxClient(stdout_bytes=_FAKE_TAR)
    mgr = _make_manager(sc, snapshots_dir=tmp_path)
    result = mgr.backup("box", ["/workspace"])
    # Layout: <snapshots_dir>/box/<timestamp>/archive.tar.gz
    archive = result.archive_path
    assert archive is not None
    assert archive.name == "archive.tar.gz"
    assert archive.parent.parent.name == "box"
    assert archive.parent.parent.parent == tmp_path


# ===========================================================================
# Backup — error cases
# ===========================================================================

def test_backup_no_paths_returns_error(tmp_path: pathlib.Path) -> None:
    sc = _FakeSandboxClient(stdout_bytes=_FAKE_TAR)
    mgr = _make_manager(sc, snapshots_dir=tmp_path)
    result = mgr.backup("box", [])
    assert result.ok is False
    assert "no paths" in result.error


def test_backup_sandbox_not_found_returns_error(tmp_path: pathlib.Path) -> None:
    sc = _FakeSandboxClient(fail_get=True)
    mgr = _make_manager(sc, snapshots_dir=tmp_path)
    result = mgr.backup("ghost", ["/workspace"])
    assert result.ok is False
    assert "could not look up" in result.error


def test_backup_exec_failure_returns_error(tmp_path: pathlib.Path) -> None:
    sc = _FakeSandboxClient(fail_exec=True)
    mgr = _make_manager(sc, snapshots_dir=tmp_path)
    result = mgr.backup("box", ["/workspace"])
    assert result.ok is False
    assert "exec failed" in result.error


def test_backup_tar_nonzero_exit_returns_error(tmp_path: pathlib.Path) -> None:
    sc = _FakeSandboxClient(stdout_bytes=_FAKE_TAR, exec_exit_code=1)
    mgr = _make_manager(sc, snapshots_dir=tmp_path)
    result = mgr.backup("box", ["/workspace"])
    assert result.ok is False
    assert "code 1" in result.error


def test_backup_empty_tar_output_returns_error(tmp_path: pathlib.Path) -> None:
    sc = _FakeSandboxClient(stdout_bytes=b"", exec_exit_code=0)
    mgr = _make_manager(sc, snapshots_dir=tmp_path)
    result = mgr.backup("box", ["/workspace"])
    assert result.ok is False
    assert "no output" in result.error


def test_backup_exec_failure_cleans_up_dir(tmp_path: pathlib.Path) -> None:
    sc = _FakeSandboxClient(fail_exec=True)
    mgr = _make_manager(sc, snapshots_dir=tmp_path)
    mgr.backup("box", ["/workspace"])
    sandbox_dir = tmp_path / "box"
    # Any created timestamp dir should have been cleaned up.
    if sandbox_dir.exists():
        ts_dirs = list(sandbox_dir.iterdir())
        assert ts_dirs == [], "snapshot dir should be cleaned up on exec failure"


def test_backup_tar_error_cleans_up_dir(tmp_path: pathlib.Path) -> None:
    sc = _FakeSandboxClient(stdout_bytes=_FAKE_TAR, exec_exit_code=2)
    mgr = _make_manager(sc, snapshots_dir=tmp_path)
    mgr.backup("box", ["/workspace"])
    sandbox_dir = tmp_path / "box"
    if sandbox_dir.exists():
        ts_dirs = list(sandbox_dir.iterdir())
        assert ts_dirs == []


# ===========================================================================
# Restore — happy path
# ===========================================================================

def _write_snapshot(
    snapshots_dir: pathlib.Path,
    sandbox_name: str,
    timestamp: str,
    *,
    label: str = "",
    paths: list[str] | None = None,
    archive_bytes: bytes = _FAKE_TAR,
) -> pathlib.Path:
    snap_dir = snapshots_dir / sandbox_name / timestamp
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "archive.tar.gz").write_bytes(archive_bytes)
    manifest = {
        "sandbox_name": sandbox_name,
        "timestamp": timestamp,
        "label": label,
        "paths": paths or ["/workspace"],
        "size_bytes": len(archive_bytes),
    }
    (snap_dir / "manifest.json").write_text(json.dumps(manifest))
    return snap_dir


def test_restore_returns_ok(tmp_path: pathlib.Path) -> None:
    _write_snapshot(tmp_path, "box", "2026-01-01T00:00:00Z")
    sc = _FakeSandboxClient()
    mgr = _make_manager(sc, snapshots_dir=tmp_path)
    result = mgr.restore("box", "2026-01-01T00:00:00Z")
    assert result.ok is True
    assert result.sandbox_name == "box"
    assert result.restored_from == "2026-01-01T00:00:00Z"


def test_restore_sends_archive_as_stdin(tmp_path: pathlib.Path) -> None:
    _write_snapshot(tmp_path, "box", "2026-01-01T00:00:00Z", archive_bytes=_FAKE_TAR)
    sc = _FakeSandboxClient()
    mgr = _make_manager(sc, snapshots_dir=tmp_path)
    mgr.restore("box", "2026-01-01T00:00:00Z")
    assert len(sc.exec_stdins) == 1
    assert sc.exec_stdins[0] == _FAKE_TAR


def test_restore_tar_command_includes_dest(tmp_path: pathlib.Path) -> None:
    _write_snapshot(tmp_path, "box", "2026-01-01T00:00:00Z")
    sc = _FakeSandboxClient()
    mgr = _make_manager(sc, snapshots_dir=tmp_path)
    mgr.restore("box", "2026-01-01T00:00:00Z", dest="/home/user")
    cmd = sc.exec_commands[0]
    assert "/home/user" in cmd


def test_restore_default_dest_is_root(tmp_path: pathlib.Path) -> None:
    _write_snapshot(tmp_path, "box", "2026-01-01T00:00:00Z")
    sc = _FakeSandboxClient()
    mgr = _make_manager(sc, snapshots_dir=tmp_path)
    mgr.restore("box", "2026-01-01T00:00:00Z")
    cmd = sc.exec_commands[0]
    assert "/" in cmd


# ===========================================================================
# Restore — error cases
# ===========================================================================

def test_restore_missing_archive_returns_error(tmp_path: pathlib.Path) -> None:
    sc = _FakeSandboxClient()
    mgr = _make_manager(sc, snapshots_dir=tmp_path)
    result = mgr.restore("box", "2026-01-01T00:00:00Z")
    assert result.ok is False
    assert "not found" in result.error


def test_restore_sandbox_not_found_returns_error(tmp_path: pathlib.Path) -> None:
    _write_snapshot(tmp_path, "ghost", "2026-01-01T00:00:00Z")
    sc = _FakeSandboxClient(fail_get=True)
    mgr = _make_manager(sc, snapshots_dir=tmp_path)
    result = mgr.restore("ghost", "2026-01-01T00:00:00Z")
    assert result.ok is False
    assert "could not look up" in result.error


def test_restore_exec_failure_returns_error(tmp_path: pathlib.Path) -> None:
    _write_snapshot(tmp_path, "box", "2026-01-01T00:00:00Z")
    sc = _FakeSandboxClient(fail_exec=True)
    mgr = _make_manager(sc, snapshots_dir=tmp_path)
    result = mgr.restore("box", "2026-01-01T00:00:00Z")
    assert result.ok is False
    assert "exec failed" in result.error


def test_restore_tar_nonzero_exit_returns_error(tmp_path: pathlib.Path) -> None:
    _write_snapshot(tmp_path, "box", "2026-01-01T00:00:00Z")
    sc = _FakeSandboxClient(restore_exit_code=1)
    mgr = _make_manager(sc, snapshots_dir=tmp_path)
    result = mgr.restore("box", "2026-01-01T00:00:00Z")
    assert result.ok is False
    assert "code 1" in result.error


# ===========================================================================
# list_snapshots
# ===========================================================================

def test_list_snapshots_empty_when_no_dir(tmp_path: pathlib.Path) -> None:
    mgr = _make_manager(snapshots_dir=tmp_path)
    assert mgr.list_snapshots("box") == []


def test_list_snapshots_single(tmp_path: pathlib.Path) -> None:
    _write_snapshot(tmp_path, "box", "2026-01-01T00:00:00Z")
    mgr = _make_manager(snapshots_dir=tmp_path)
    snaps = mgr.list_snapshots("box")
    assert len(snaps) == 1
    assert snaps[0].timestamp == "2026-01-01T00:00:00Z"


def test_list_snapshots_newest_first(tmp_path: pathlib.Path) -> None:
    _write_snapshot(tmp_path, "box", "2026-01-01T00:00:00Z")
    _write_snapshot(tmp_path, "box", "2026-02-01T00:00:00Z")
    _write_snapshot(tmp_path, "box", "2026-03-01T00:00:00Z")
    mgr = _make_manager(snapshots_dir=tmp_path)
    snaps = mgr.list_snapshots("box")
    assert snaps[0].timestamp == "2026-03-01T00:00:00Z"
    assert snaps[-1].timestamp == "2026-01-01T00:00:00Z"


def test_list_snapshots_version_numbers(tmp_path: pathlib.Path) -> None:
    _write_snapshot(tmp_path, "box", "2026-01-01T00:00:00Z")
    _write_snapshot(tmp_path, "box", "2026-02-01T00:00:00Z")
    _write_snapshot(tmp_path, "box", "2026-03-01T00:00:00Z")
    mgr = _make_manager(snapshots_dir=tmp_path)
    snaps = mgr.list_snapshots("box")
    # v1 = oldest, vN = newest; list is newest-first
    versions = [s.version for s in snaps]
    assert versions == [3, 2, 1]


def test_list_snapshots_skips_incomplete_dirs(tmp_path: pathlib.Path) -> None:
    # Only manifest, no archive
    incomplete = tmp_path / "box" / "2026-01-01T00:00:00Z"
    incomplete.mkdir(parents=True)
    (incomplete / "manifest.json").write_text(json.dumps({"sandbox_name": "box", "timestamp": "t", "label": "", "paths": [], "size_bytes": 0}))

    _write_snapshot(tmp_path, "box", "2026-02-01T00:00:00Z")

    mgr = _make_manager(snapshots_dir=tmp_path)
    snaps = mgr.list_snapshots("box")
    assert len(snaps) == 1
    assert snaps[0].timestamp == "2026-02-01T00:00:00Z"


def test_list_snapshots_label_preserved(tmp_path: pathlib.Path) -> None:
    _write_snapshot(tmp_path, "box", "2026-01-01T00:00:00Z", label="before-upgrade")
    mgr = _make_manager(snapshots_dir=tmp_path)
    snaps = mgr.list_snapshots("box")
    assert snaps[0].label == "before-upgrade"


def test_list_snapshots_size_bytes_preserved(tmp_path: pathlib.Path) -> None:
    data = b"x" * 512
    _write_snapshot(tmp_path, "box", "2026-01-01T00:00:00Z", archive_bytes=data)
    mgr = _make_manager(snapshots_dir=tmp_path)
    snaps = mgr.list_snapshots("box")
    assert snaps[0].size_bytes == 512


def test_list_snapshots_sandbox_isolation(tmp_path: pathlib.Path) -> None:
    _write_snapshot(tmp_path, "box", "2026-01-01T00:00:00Z")
    _write_snapshot(tmp_path, "other", "2026-02-01T00:00:00Z")
    mgr = _make_manager(snapshots_dir=tmp_path)
    assert len(mgr.list_snapshots("box")) == 1
    assert len(mgr.list_snapshots("other")) == 1


# ===========================================================================
# find_snapshot
# ===========================================================================

def test_find_snapshot_by_version(tmp_path: pathlib.Path) -> None:
    _write_snapshot(tmp_path, "box", "2026-01-01T00:00:00Z")
    _write_snapshot(tmp_path, "box", "2026-02-01T00:00:00Z")
    mgr = _make_manager(snapshots_dir=tmp_path)
    snap = mgr.find_snapshot("box", "v1")
    assert snap is not None
    assert snap.timestamp == "2026-01-01T00:00:00Z"


def test_find_snapshot_v2(tmp_path: pathlib.Path) -> None:
    _write_snapshot(tmp_path, "box", "2026-01-01T00:00:00Z")
    _write_snapshot(tmp_path, "box", "2026-02-01T00:00:00Z")
    mgr = _make_manager(snapshots_dir=tmp_path)
    snap = mgr.find_snapshot("box", "v2")
    assert snap is not None
    assert snap.timestamp == "2026-02-01T00:00:00Z"


def test_find_snapshot_by_label(tmp_path: pathlib.Path) -> None:
    _write_snapshot(tmp_path, "box", "2026-01-01T00:00:00Z", label="before-upgrade")
    mgr = _make_manager(snapshots_dir=tmp_path)
    snap = mgr.find_snapshot("box", "before-upgrade")
    assert snap is not None
    assert snap.label == "before-upgrade"


def test_find_snapshot_by_timestamp(tmp_path: pathlib.Path) -> None:
    _write_snapshot(tmp_path, "box", "2026-01-01T00:00:00Z")
    mgr = _make_manager(snapshots_dir=tmp_path)
    snap = mgr.find_snapshot("box", "2026-01-01T00:00:00Z")
    assert snap is not None
    assert snap.timestamp == "2026-01-01T00:00:00Z"


def test_find_snapshot_version_not_found(tmp_path: pathlib.Path) -> None:
    _write_snapshot(tmp_path, "box", "2026-01-01T00:00:00Z")
    mgr = _make_manager(snapshots_dir=tmp_path)
    assert mgr.find_snapshot("box", "v99") is None


def test_find_snapshot_label_not_found(tmp_path: pathlib.Path) -> None:
    _write_snapshot(tmp_path, "box", "2026-01-01T00:00:00Z", label="real-label")
    mgr = _make_manager(snapshots_dir=tmp_path)
    assert mgr.find_snapshot("box", "not-a-label") is None


def test_find_snapshot_no_snapshots(tmp_path: pathlib.Path) -> None:
    mgr = _make_manager(snapshots_dir=tmp_path)
    assert mgr.find_snapshot("box", "v1") is None


def test_find_snapshot_version_selector_case_insensitive(tmp_path: pathlib.Path) -> None:
    _write_snapshot(tmp_path, "box", "2026-01-01T00:00:00Z")
    mgr = _make_manager(snapshots_dir=tmp_path)
    snap = mgr.find_snapshot("box", "V1")
    assert snap is not None


# ===========================================================================
# delete_snapshot
# ===========================================================================

def test_delete_snapshot_removes_dir(tmp_path: pathlib.Path) -> None:
    _write_snapshot(tmp_path, "box", "2026-01-01T00:00:00Z")
    mgr = _make_manager(snapshots_dir=tmp_path)
    deleted = mgr.delete_snapshot("box", "2026-01-01T00:00:00Z")
    assert deleted is True
    assert not (tmp_path / "box" / "2026-01-01T00:00:00Z").exists()


def test_delete_snapshot_not_found_returns_false(tmp_path: pathlib.Path) -> None:
    mgr = _make_manager(snapshots_dir=tmp_path)
    assert mgr.delete_snapshot("box", "2026-01-01T00:00:00Z") is False


def test_delete_snapshot_leaves_other_snapshots(tmp_path: pathlib.Path) -> None:
    _write_snapshot(tmp_path, "box", "2026-01-01T00:00:00Z")
    _write_snapshot(tmp_path, "box", "2026-02-01T00:00:00Z")
    mgr = _make_manager(snapshots_dir=tmp_path)
    mgr.delete_snapshot("box", "2026-01-01T00:00:00Z")
    snaps = mgr.list_snapshots("box")
    assert len(snaps) == 1
    assert snaps[0].timestamp == "2026-02-01T00:00:00Z"


# ===========================================================================
# snapshots_dir override
# ===========================================================================

def test_custom_snapshots_dir(tmp_path: pathlib.Path) -> None:
    custom = tmp_path / "my-snaps"
    sc = _FakeSandboxClient(stdout_bytes=_FAKE_TAR)
    mgr = SnapshotManager(sc, snapshots_dir=custom)
    result = mgr.backup("box", ["/workspace"])
    assert result.ok is True
    assert result.archive_path is not None
    assert str(result.archive_path).startswith(str(custom))


def test_custom_snapshots_dir_as_string(tmp_path: pathlib.Path) -> None:
    custom = str(tmp_path / "str-snaps")
    sc = _FakeSandboxClient(stdout_bytes=_FAKE_TAR)
    mgr = SnapshotManager(sc, snapshots_dir=custom)
    result = mgr.backup("box", ["/workspace"])
    assert result.ok is True


# ===========================================================================
# SnapshotInfo fields
# ===========================================================================

def test_snapshot_info_archive_path_exists(tmp_path: pathlib.Path) -> None:
    _write_snapshot(tmp_path, "box", "2026-01-01T00:00:00Z")
    mgr = _make_manager(snapshots_dir=tmp_path)
    snap = mgr.list_snapshots("box")[0]
    assert snap.archive_path.exists()


def test_snapshot_info_manifest_path_exists(tmp_path: pathlib.Path) -> None:
    _write_snapshot(tmp_path, "box", "2026-01-01T00:00:00Z")
    mgr = _make_manager(snapshots_dir=tmp_path)
    snap = mgr.list_snapshots("box")[0]
    assert snap.manifest_path.exists()


def test_snapshot_info_paths_list(tmp_path: pathlib.Path) -> None:
    _write_snapshot(tmp_path, "box", "2026-01-01T00:00:00Z", paths=["/a", "/b"])
    mgr = _make_manager(snapshots_dir=tmp_path)
    snap = mgr.list_snapshots("box")[0]
    assert snap.paths == ["/a", "/b"]
