"""Durable session lifecycle storage.

The registry is deliberately independent of any TUI.  A session has a stable UUID,
a hot directory while active, and an immutable per-session tar.zst archive later.
Legacy ``temp/model_responses`` files remain readable by the existing UI.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1
SHORT_AGE_SECONDS = 7 * 24 * 60 * 60
LONG_AGE_SECONDS = 180 * 24 * 60 * 60


def _now() -> int:
    return int(time.time())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class SessionStore:
    """SQLite-backed session registry and safe hot-to-archive transition."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or Path(__file__).resolve().parents[1]).resolve()
        self.hot_root = self.root / "temp" / "sessions" / "hot"
        self.runtime_root = self.root / "temp" / "runtime" / "restore"
        self.index_root = self.root / "memory" / "L4_raw_sessions" / "index"
        self.archive_root = self.root / "memory" / "L4_raw_sessions" / "archive"
        self.db_path = self.index_root / "sessions.sqlite3"
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        self.index_root.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                legacy_log_basename TEXT,
                created_at INTEGER NOT NULL,
                last_activity_at INTEGER NOT NULL,
                class TEXT NOT NULL CHECK(class IN ('short','long','legacy-unclassified')),
                title TEXT NOT NULL DEFAULT '',
                promotion_reason TEXT NOT NULL DEFAULT '',
                promoted_at INTEGER,
                workspace_history TEXT NOT NULL DEFAULT '[]',
                hot_path TEXT,
                archive_path TEXT,
                archive_sha256 TEXT,
                archive_created_at INTEGER,
                summary TEXT NOT NULL DEFAULT '',
                turn_count INTEGER NOT NULL DEFAULT 0,
                byte_count INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'hot' CHECK(state IN ('hot','archived')),
                schema_version INTEGER NOT NULL DEFAULT 1
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS sessions_activity ON sessions(state, class, last_activity_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS sessions_title ON sessions(title)")

    @staticmethod
    def _validate_id(session_id: str) -> str:
        try:
            return str(uuid.UUID(str(session_id)))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("session_id must be a UUID") from exc

    def hot_dir(self, session_id: str) -> Path:
        return self.hot_root / self._validate_id(session_id)

    def transcript_path(self, session_id: str) -> Path:
        return self.hot_dir(session_id) / "transcript.txt"

    def create_session(self, *, legacy_log_basename: str = "", title: str = "") -> dict:
        session_id, stamp = str(uuid.uuid4()), _now()
        hot = self.hot_dir(session_id)
        hot.mkdir(parents=True, exist_ok=False)
        data = {"session_id": session_id, "created_at": stamp, "schema_version": SCHEMA_VERSION,
                "class": "short", "title": title, "workspace_history": []}
        (hot / "transcript.txt").touch()
        self._atomic_json(hot / "session.json", data)
        with self._connect() as conn:
            conn.execute("""INSERT INTO sessions(session_id, legacy_log_basename, created_at, last_activity_at,
                         class, title, workspace_history, hot_path, schema_version)
                         VALUES(?,?,?,?,?,?,?,?,?)""",
                         (session_id, legacy_log_basename, stamp, stamp, "short", title, "[]",
                          str(hot.relative_to(self.root)), SCHEMA_VERSION))
        return self.get(session_id)

    def get(self, session_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (self._validate_id(session_id),)).fetchone()
        return self._row(row)

    def list(self, *, include_archived: bool = True, search: str = "") -> list[dict]:
        sql, values = "SELECT * FROM sessions", []
        clauses = []
        if not include_archived:
            clauses.append("state='hot'")
        if search:
            clauses.append("(title LIKE ? OR summary LIKE ? OR workspace_history LIKE ?)")
            values += [f"%{search}%"] * 3
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY last_activity_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, values).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        out = dict(row)
        out["workspace_history"] = json.loads(out["workspace_history"] or "[]")
        return out

    def _atomic_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError): os.unlink(tmp)
            raise

    def _refresh_sidecar(self, session_id: str) -> None:
        row = self.get(session_id)
        if not row or row["state"] != "hot":
            return
        self._atomic_json(self.hot_dir(session_id) / "session.json", {
            key: row[key] for key in ("session_id", "created_at", "last_activity_at", "class", "title",
            "promotion_reason", "promoted_at", "workspace_history", "summary", "turn_count", "byte_count",
            "schema_version")})

    def record_activity(self, session_id: str, *, summary: str | None = None) -> None:
        session_id = self._validate_id(session_id)
        transcript = self.transcript_path(session_id)
        turns = 0
        if transcript.exists():
            try:
                text = transcript.read_text(encoding="utf-8", errors="replace")
                turns = text.count("=== Response ===")
                byte_count = transcript.stat().st_size
            except OSError:
                byte_count = 0
        else:
            byte_count = 0
        with self._connect() as conn:
            if summary is None:
                conn.execute("UPDATE sessions SET last_activity_at=?, turn_count=?, byte_count=? WHERE session_id=? AND state='hot'",
                             (_now(), turns, byte_count, session_id))
            else:
                conn.execute("UPDATE sessions SET last_activity_at=?, turn_count=?, byte_count=?, summary=? WHERE session_id=? AND state='hot'",
                             (_now(), turns, byte_count, summary, session_id))
        self._refresh_sidecar(session_id)

    def promote(self, session_id: str, reason: str, *, title: str | None = None, workspace: str | None = None) -> dict:
        session_id = self._validate_id(session_id)
        row = self.get(session_id)
        if not row:
            raise KeyError(session_id)
        history = row["workspace_history"]
        if workspace and workspace not in history:
            history.append(workspace)
        stamp = _now()
        with self._connect() as conn:
            conn.execute("""UPDATE sessions SET class='long', promotion_reason=?, promoted_at=?,
                         title=CASE WHEN ? IS NULL THEN title ELSE ? END, workspace_history=?, last_activity_at=?
                         WHERE session_id=? AND state='hot'""",
                         (reason, stamp, title, title, json.dumps(history, ensure_ascii=False), stamp, session_id))
        self._refresh_sidecar(session_id)
        return self.get(session_id)

    def set_summary(self, session_id: str, summary: str) -> None:
        self.record_activity(session_id, summary=summary[:4000])

    @contextlib.contextmanager
    def lock(self, session_id: str, blocking: bool = True) -> Iterator[None]:
        """Process lock. Archive refuses a session held by a live agent."""
        import fcntl
        path = self.hot_dir(session_id) / ".active.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+") as fh:
            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(fh.fileno(), flags)
            except BlockingIOError as exc:
                raise RuntimeError("session is active/locked") from exc
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def eligible(self, now: int | None = None) -> list[dict]:
        now = _now() if now is None else now
        with self._connect() as conn:
            rows = conn.execute("""SELECT * FROM sessions WHERE state='hot' AND
                ((class='short' AND last_activity_at<=?) OR (class='long' AND last_activity_at<=?))
                ORDER BY last_activity_at""", (now - SHORT_AGE_SECONDS, now - LONG_AGE_SECONDS)).fetchall()
        return [self._row(r) for r in rows]

    def archive(self, session_id: str, *, remove_hot: bool = True) -> dict:
        """Create, fully verify, atomically publish and index one immutable archive."""
        session_id = self._validate_id(session_id)
        row = self.get(session_id)
        if not row:
            raise KeyError(session_id)
        if row["state"] == "archived":
            return row
        hot = self.hot_dir(session_id)
        if not hot.is_dir():
            raise FileNotFoundError(hot)
        with self.lock(session_id, blocking=False):
            self.record_activity(session_id)
            row = self.get(session_id)
            stamp = datetime.now(timezone.utc)
            target_dir = self.archive_root / stamp.strftime("%Y") / stamp.strftime("%m")
            target_dir.mkdir(parents=True, exist_ok=True)
            final = target_dir / f"{session_id}.tar.zst"
            if final.exists():
                raise FileExistsError(f"refusing to overwrite immutable archive: {final}")
            staging_parent = self.runtime_root.parent
            staging_parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=f"archive-{session_id}-", dir=staging_parent) as tmp_name:
                stage = Path(tmp_name) / session_id
                shutil.copytree(hot, stage, ignore=shutil.ignore_patterns(".active.lock"))
                (stage / "summary.txt").write_text(row["summary"] or "", encoding="utf-8")
                members = []
                for path in sorted(p for p in stage.rglob("*") if p.is_file() and p.name != "manifest.json"):
                    rel = path.relative_to(stage).as_posix()
                    members.append({"path": rel, "bytes": path.stat().st_size, "sha256": _sha256(path)})
                self._atomic_json(stage / "manifest.json", {"schema_version": SCHEMA_VERSION,
                    "session_id": session_id, "created_at": _now(), "members": members})
                partial = target_dir / f"{session_id}.tar.zst.partial"
                if partial.exists():
                    raise FileExistsError(f"stale partial archive requires manual inspection: {partial}")
                try:
                    subprocess.run(["tar", "--zstd", "-cf", str(partial), "-C", str(stage.parent), session_id], check=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    subprocess.run(["zstd", "-t", str(partial)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    listed = subprocess.run(["tar", "--zstd", "-tf", str(partial)], check=True, text=True,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.splitlines()
                    required = {f"{session_id}/transcript.txt", f"{session_id}/session.json", f"{session_id}/summary.txt", f"{session_id}/manifest.json"}
                    if not required.issubset(set(listed)):
                        raise ValueError("archive misses required session members")
                    self._verify_archive(partial, session_id)
                    os.replace(partial, final)
                except BaseException:
                    # Preserve hot data and the partial as forensic evidence; never conceal a failed archive.
                    raise
            # Do the final archive read-back before committing its state.  If it
            # fails the registry remains hot and its original data is untouched.
            self._verify_archive(final, session_id)
            digest = _sha256(final)
            relative = str(final.relative_to(self.root))
            with self._connect() as conn:
                conn.execute("""UPDATE sessions SET state='archived', archive_path=?, archive_sha256=?,
                             archive_created_at=?, hot_path=NULL WHERE session_id=? AND state='hot'""",
                             (relative, digest, _now(), session_id))
            if remove_hot:
                shutil.rmtree(hot)
        return self.get(session_id)

    def _verify_archive(self, archive: Path, session_id: str) -> None:
        with tempfile.TemporaryDirectory(prefix=f"verify-{session_id}-", dir=self.runtime_root.parent) as out:
            subprocess.run(["tar", "--zstd", "-xf", str(archive), "-C", out], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            base = Path(out) / session_id
            manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
            if manifest.get("session_id") != session_id:
                raise ValueError("archive manifest session id mismatch")
            for entry in manifest.get("members", []):
                member = base / entry["path"]
                if not member.is_file() or member.stat().st_size != entry["bytes"] or _sha256(member) != entry["sha256"]:
                    raise ValueError(f"archive member verification failed: {entry['path']}")

    def restore(self, session_id: str) -> Path:
        """Extract and verify exactly one archived session into runtime/restore."""
        row = self.get(session_id)
        if not row or row["state"] != "archived" or not row["archive_path"]:
            raise KeyError(session_id)
        archive = self.root / row["archive_path"]
        if not archive.is_file() or _sha256(archive) != row["archive_sha256"]:
            raise ValueError("archive checksum mismatch")
        dest = self.runtime_root / self._validate_id(session_id)
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        try:
            subprocess.run(["tar", "--zstd", "-xf", str(archive), "-C", str(dest)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            extracted = dest / session_id
            # Reuse the same manifest checker on a temporary tar-free extraction.
            manifest = json.loads((extracted / "manifest.json").read_text(encoding="utf-8"))
            for entry in manifest["members"]:
                p = extracted / entry["path"]
                if not p.is_file() or p.stat().st_size != entry["bytes"] or _sha256(p) != entry["sha256"]:
                    raise ValueError(f"restored member verification failed: {entry['path']}")
            return extracted
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            raise


def default_store() -> SessionStore:
    return SessionStore(Path(__file__).resolve().parents[1])
