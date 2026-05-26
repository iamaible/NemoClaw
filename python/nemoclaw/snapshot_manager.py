# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
SnapshotManager: backup and restore sandbox workspace state via gRPC exec.

Uses :meth:`~openshell.sandbox.SandboxClient.exec_stream` to pipe ``tar``
output directly over gRPC, avoiding the need for a separate SSH connection
or the CLI ``openshell sandbox ssh-config`` command.

Storage layout::

    ~/.nemoclaw/snapshots/<sandbox_name>/<timestamp>/
        manifest.json
        archive.tar.gz

Timestamps are ISO-8601 UTC strings (``YYYY-MM-DDTHH:MM:SSZ``).
"""

from __future__ import annotations

import datetime
import json
import pathlib
import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openshell.sandbox import SandboxClient

_DEFAULT_SNAPSHOTS_DIR = pathlib.Path.home() / ".nemoclaw" / "snapshots"


# ---------------------------------------------------------------------------
# Result and info types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SnapshotInfo:
    """Metadata for a single snapshot."""
    sandbox_name: str
    timestamp: str
    label: str
    paths: list[str]
    size_bytes: int
    version: int
    archive_path: pathlib.Path
    manifest_path: pathlib.Path


@dataclass(frozen=True)
class SnapshotResult:
    """Result of a backup operation."""
    ok: bool
    sandbox_name: str
    timestamp: str = ""
    archive_path: pathlib.Path | None = None
    size_bytes: int = 0
    error: str = ""


@dataclass(frozen=True)
class RestoreResult:
    """Result of a restore operation."""
    ok: bool
    sandbox_name: str
    restored_from: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# SnapshotManagerError
# ---------------------------------------------------------------------------

class SnapshotManagerError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# SnapshotManager
# ---------------------------------------------------------------------------

class SnapshotManager:
    """
    Creates, lists, and restores sandbox snapshots using gRPC exec.

    Parameters
    ----------
    sandbox_client:
        An initialised :class:`openshell.sandbox.SandboxClient`.  Required for
        :meth:`backup` and :meth:`restore`; read-only methods always work.
    snapshots_dir:
        Local directory where snapshots are stored.  Defaults to
        ``~/.nemoclaw/snapshots``.
    exec_timeout:
        Seconds allowed for the tar command inside the sandbox.
    """

    def __init__(
        self,
        sandbox_client: "SandboxClient | None" = None,
        *,
        snapshots_dir: pathlib.Path | str | None = None,
        exec_timeout: float = 300.0,
    ) -> None:
        self._client = sandbox_client
        self._snapshots_dir = pathlib.Path(snapshots_dir) if snapshots_dir else _DEFAULT_SNAPSHOTS_DIR
        self._exec_timeout = exec_timeout

    # ------------------------------------------------------------------
    # Backup
    # ------------------------------------------------------------------

    def backup(
        self,
        sandbox_name: str,
        paths: list[str],
        *,
        label: str = "",
    ) -> SnapshotResult:
        """
        Tar *paths* from the sandbox and store the archive locally.

        Parameters
        ----------
        sandbox_name:
            Registry name of the sandbox to back up.
        paths:
            Absolute paths inside the sandbox to include in the archive.
        label:
            Optional human-readable tag (e.g. ``"before-upgrade"``).

        Returns
        -------
        :class:`SnapshotResult`
            ``ok=True`` on success.
        """
        if self._client is None:
            raise SnapshotManagerError(
                "backup requires a SandboxClient; construct SnapshotManager with one."
            )
        if not paths:
            return SnapshotResult(
                ok=False,
                sandbox_name=sandbox_name,
                error="no paths specified",
            )

        try:
            sandbox_ref = self._client.get(sandbox_name)
        except Exception as exc:  # noqa: BLE001
            return SnapshotResult(
                ok=False,
                sandbox_name=sandbox_name,
                error=f"could not look up sandbox: {exc}",
            )

        timestamp = _utc_now()
        snapshot_dir = self._snapshots_dir / sandbox_name / timestamp
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        archive_path = snapshot_dir / "archive.tar.gz"
        manifest_path = snapshot_dir / "manifest.json"

        # Run tar inside sandbox, stream raw bytes over gRPC exec.
        command = ["tar", "czf", "-", "--", *paths]
        try:
            raw_bytes, exit_code = self._exec_raw_stdout(sandbox_ref.id, command)
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(snapshot_dir, ignore_errors=True)
            return SnapshotResult(
                ok=False,
                sandbox_name=sandbox_name,
                error=f"exec failed: {exc}",
            )

        if exit_code != 0:
            shutil.rmtree(snapshot_dir, ignore_errors=True)
            return SnapshotResult(
                ok=False,
                sandbox_name=sandbox_name,
                error=f"tar exited with code {exit_code}",
            )

        if not raw_bytes:
            shutil.rmtree(snapshot_dir, ignore_errors=True)
            return SnapshotResult(
                ok=False,
                sandbox_name=sandbox_name,
                error="tar produced no output",
            )

        archive_path.write_bytes(raw_bytes)
        size = len(raw_bytes)

        manifest: dict[str, Any] = {
            "sandbox_name": sandbox_name,
            "timestamp": timestamp,
            "label": label,
            "paths": paths,
            "size_bytes": size,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2))

        return SnapshotResult(
            ok=True,
            sandbox_name=sandbox_name,
            timestamp=timestamp,
            archive_path=archive_path,
            size_bytes=size,
        )

    # ------------------------------------------------------------------
    # Restore
    # ------------------------------------------------------------------

    def restore(
        self,
        sandbox_name: str,
        timestamp: str,
        *,
        dest: str = "/",
    ) -> RestoreResult:
        """
        Restore a previously taken snapshot into the sandbox.

        Parameters
        ----------
        sandbox_name:
            Target sandbox name.
        timestamp:
            Timestamp string identifying the snapshot (from :attr:`SnapshotInfo.timestamp`
            or a selector resolved via :meth:`find_snapshot`).
        dest:
            Extraction root inside the sandbox (default ``"/"``).
        """
        if self._client is None:
            raise SnapshotManagerError(
                "restore requires a SandboxClient; construct SnapshotManager with one."
            )

        snapshot_dir = self._snapshots_dir / sandbox_name / timestamp
        archive_path = snapshot_dir / "archive.tar.gz"
        if not archive_path.exists():
            return RestoreResult(
                ok=False,
                sandbox_name=sandbox_name,
                error=f"snapshot archive not found: {archive_path}",
            )

        try:
            sandbox_ref = self._client.get(sandbox_name)
        except Exception as exc:  # noqa: BLE001
            return RestoreResult(
                ok=False,
                sandbox_name=sandbox_name,
                error=f"could not look up sandbox: {exc}",
            )

        tar_bytes = archive_path.read_bytes()
        command = ["tar", "xzf", "-", "--no-same-owner", "-C", dest]
        try:
            exit_code = self._exec_with_stdin(sandbox_ref.id, command, stdin=tar_bytes)
        except Exception as exc:  # noqa: BLE001
            return RestoreResult(
                ok=False,
                sandbox_name=sandbox_name,
                error=f"exec failed: {exc}",
            )

        if exit_code != 0:
            return RestoreResult(
                ok=False,
                sandbox_name=sandbox_name,
                error=f"tar extract exited with code {exit_code}",
            )

        return RestoreResult(
            ok=True,
            sandbox_name=sandbox_name,
            restored_from=timestamp,
        )

    # ------------------------------------------------------------------
    # Listing and lookup
    # ------------------------------------------------------------------

    def list_snapshots(self, sandbox_name: str) -> list[SnapshotInfo]:
        """
        Return all snapshots for *sandbox_name*, newest-first.

        Version numbers are assigned by position (``v1`` = oldest).
        """
        sandbox_dir = self._snapshots_dir / sandbox_name
        if not sandbox_dir.is_dir():
            return []

        entries: list[SnapshotInfo] = []
        for ts_dir in sorted(sandbox_dir.iterdir()):
            if not ts_dir.is_dir():
                continue
            manifest_path = ts_dir / "manifest.json"
            archive_path = ts_dir / "archive.tar.gz"
            if not manifest_path.exists() or not archive_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text())
            except Exception:  # noqa: BLE001
                continue
            entries.append(SnapshotInfo(
                sandbox_name=sandbox_name,
                timestamp=manifest.get("timestamp", ts_dir.name),
                label=manifest.get("label", ""),
                paths=list(manifest.get("paths", [])),
                size_bytes=int(manifest.get("size_bytes", 0)),
                version=0,  # filled in below
                archive_path=archive_path,
                manifest_path=manifest_path,
            ))

        # Assign version numbers (v1 = oldest).
        entries.sort(key=lambda e: e.timestamp)
        versioned = [
            SnapshotInfo(
                sandbox_name=e.sandbox_name,
                timestamp=e.timestamp,
                label=e.label,
                paths=e.paths,
                size_bytes=e.size_bytes,
                version=i + 1,
                archive_path=e.archive_path,
                manifest_path=e.manifest_path,
            )
            for i, e in enumerate(entries)
        ]
        # Return newest-first.
        return list(reversed(versioned))

    def find_snapshot(self, sandbox_name: str, selector: str) -> SnapshotInfo | None:
        """
        Resolve a selector to a snapshot.

        Selector precedence:

        1. ``v<N>`` — version number (``v1`` = oldest).
        2. Exact label match.
        3. Exact timestamp match.
        """
        snapshots = self.list_snapshots(sandbox_name)
        if not snapshots:
            return None

        # v<N> version selector
        if selector.lower().startswith("v") and selector[1:].isdigit():
            wanted = int(selector[1:])
            for s in snapshots:
                if s.version == wanted:
                    return s
            return None

        # Exact label
        for s in snapshots:
            if s.label == selector:
                return s

        # Exact timestamp
        for s in snapshots:
            if s.timestamp == selector:
                return s

        return None

    def delete_snapshot(self, sandbox_name: str, timestamp: str) -> bool:
        """
        Delete a snapshot directory from local storage.

        Returns ``True`` if the directory existed and was removed, ``False``
        if it was not found.
        """
        snapshot_dir = self._snapshots_dir / sandbox_name / timestamp
        if not snapshot_dir.is_dir():
            return False
        shutil.rmtree(snapshot_dir)
        return True

    # ------------------------------------------------------------------
    # Internal exec helpers
    # ------------------------------------------------------------------

    def _exec_raw_stdout(self, sandbox_id: str, command: list[str]) -> tuple[bytes, int]:
        """
        Run *command* in the sandbox, return ``(raw_stdout_bytes, exit_code)``.

        Streams raw bytes before decoding so binary data (tar archives) is not
        corrupted.
        """
        from openshell.sandbox import ExecChunk, ExecResult  # noqa: PLC0415

        chunks: list[bytes] = []
        exit_code = 1
        for item in self._client.exec_stream(  # type: ignore[union-attr]
            sandbox_id,
            command,
            timeout_seconds=int(self._exec_timeout),
        ):
            if isinstance(item, ExecChunk) and item.stream == "stdout":
                chunks.append(item.data)
            elif isinstance(item, ExecResult):
                exit_code = item.exit_code
        return b"".join(chunks), exit_code

    def _exec_with_stdin(self, sandbox_id: str, command: list[str], *, stdin: bytes) -> int:
        """Run *command* with *stdin* bytes, return exit code."""
        from openshell.sandbox import ExecResult  # noqa: PLC0415

        exit_code = 1
        for item in self._client.exec_stream(  # type: ignore[union-attr]
            sandbox_id,
            command,
            stdin=stdin,
            timeout_seconds=int(self._exec_timeout),
        ):
            if isinstance(item, ExecResult):
                exit_code = item.exit_code
        return exit_code


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
