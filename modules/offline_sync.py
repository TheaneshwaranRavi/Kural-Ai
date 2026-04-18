from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import socket
import sqlite3
import tarfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connectivity probes
# ---------------------------------------------------------------------------

_PROBE_HOSTS: List[Tuple[str, int]] = [
    ("1.1.1.1", 53),       # Cloudflare DNS
    ("8.8.8.8", 53),       # Google DNS
    ("pib.gov.in", 443),   # PIB (exam-relevant)
]


def check_internet_connection(timeout: float = 3.0) -> bool:
    """Return True if any probe host is reachable within timeout."""
    for host, port in _PROBE_HOSTS:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def ollama_available(base_url: Optional[str] = None) -> bool:
    """Check if local Ollama server is reachable (offline LLM)."""
    base_url = base_url or settings.llm.base_url
    try:
        host = urlparse(base_url).hostname or "localhost"
        port = urlparse(base_url).port or 11434
        with socket.create_connection((host, port), timeout=2.0):
            return True
    except OSError:
        return False


def vosk_available() -> bool:
    """Offline STT is ready if the vosk English model directory exists."""
    p = Path(settings.voice.vosk_model_en)
    if not p.is_absolute():
        p = Path(settings.data_dir).parent / p
    return p.exists() and any(p.iterdir()) if p.exists() else False


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class SyncResult:
    success: bool
    operation: str
    items_synced: int = 0
    bytes_transferred: int = 0
    errors: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class DownloadJob:
    job_id: str
    url: str
    dest_path: str
    total_bytes: int = 0
    downloaded_bytes: int = 0
    status: str = "pending"  # pending / active / completed / failed / paused
    version: str = ""
    exam_type: str = ""
    etag: str = ""
    last_modified: str = ""
    started_at: str = ""
    completed_at: str = ""


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sync_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    operation      TEXT NOT NULL,
    success        INTEGER NOT NULL,
    items_synced   INTEGER NOT NULL DEFAULT 0,
    bytes          INTEGER NOT NULL DEFAULT 0,
    errors         TEXT,
    duration_secs  REAL NOT NULL DEFAULT 0,
    timestamp      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS download_jobs (
    job_id           TEXT PRIMARY KEY,
    url              TEXT NOT NULL,
    dest_path        TEXT NOT NULL,
    total_bytes      INTEGER NOT NULL DEFAULT 0,
    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'pending',
    version          TEXT DEFAULT '',
    exam_type        TEXT DEFAULT '',
    etag             TEXT DEFAULT '',
    last_modified    TEXT DEFAULT '',
    started_at       TEXT DEFAULT '',
    completed_at     TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS content_versions (
    exam_type      TEXT NOT NULL,
    content_key    TEXT NOT NULL,
    version        TEXT NOT NULL,
    checksum       TEXT NOT NULL,
    installed_at   TEXT NOT NULL,
    file_path      TEXT,
    PRIMARY KEY (exam_type, content_key)
);

CREATE TABLE IF NOT EXISTS backup_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        TEXT NOT NULL,
    backup_path    TEXT NOT NULL,
    size_bytes     INTEGER NOT NULL DEFAULT 0,
    checksum       TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    restored_at    TEXT
);
"""


# ---------------------------------------------------------------------------
# OfflineSyncManager
# ---------------------------------------------------------------------------


class OfflineSyncManager:
    def __init__(
        self,
        db_path: Optional[str] = None,
        backup_dir: Optional[str] = None,
        cache_dir: Optional[str] = None,
        storage_limit_bytes: Optional[int] = None,
        current_affairs_manager=None,
        rag_module=None,
        user_manager=None,
    ):
        sync_cfg = getattr(settings, "sync", None)
        base_data = Path(settings.data_dir)

        if db_path:
            self.db_path = Path(db_path)
        elif sync_cfg is not None:
            self.db_path = Path(sync_cfg.db_path)
        else:
            self.db_path = base_data / "sync.db"

        if backup_dir:
            self.backup_dir = Path(backup_dir)
        elif sync_cfg is not None:
            self.backup_dir = Path(sync_cfg.backup_dir)
        else:
            self.backup_dir = base_data / "backups"

        if cache_dir:
            self.cache_dir = Path(cache_dir)
        elif sync_cfg is not None:
            self.cache_dir = Path(sync_cfg.cache_dir)
        else:
            self.cache_dir = base_data / "sync_cache"

        if storage_limit_bytes is not None:
            self.storage_limit_bytes = storage_limit_bytes
        elif sync_cfg is not None:
            self.storage_limit_bytes = sync_cfg.storage_limit_bytes
        else:
            self.storage_limit_bytes = 5 * 1024 * 1024 * 1024  # 5 GB

        for d in (self.db_path.parent, self.backup_dir, self.cache_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.current_affairs = current_affairs_manager
        self.rag = rag_module
        self.user_manager = user_manager

        self._init_db()

    # -------- DB helpers -------------------------------------------------

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as c:
            c.executescript(_DDL)

    def _log_sync(self, result: SyncResult) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO sync_log
                (operation, success, items_synced, bytes, errors,
                 duration_secs, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (result.operation, int(result.success), result.items_synced,
                 result.bytes_transferred, json.dumps(result.errors[:20]),
                 result.duration_seconds, result.timestamp),
            )

    def last_sync(self, operation: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute(
                """
                SELECT timestamp FROM sync_log
                WHERE operation=? AND success=1
                ORDER BY id DESC LIMIT 1
                """,
                (operation,),
            ).fetchone()
        return row["timestamp"] if row else None

    # -------- Connectivity ----------------------------------------------

    def is_online(self, timeout: float = 3.0) -> bool:
        return check_internet_connection(timeout=timeout)

    def connectivity_report(self) -> Dict[str, bool]:
        return {
            "internet": check_internet_connection(),
            "ollama": ollama_available(),
            "vosk_offline_stt": vosk_available(),
        }

    # -------- Current affairs sync --------------------------------------

    def sync_current_affairs(
        self,
        last_sync_date: Optional[str] = None,
    ) -> SyncResult:
        start = time.time()
        op = "current_affairs"
        if not self.is_online():
            logger.info("Offline — skipping current affairs sync")
            r = SyncResult(success=False, operation=op,
                           errors=["offline"],
                           duration_seconds=time.time() - start)
            self._log_sync(r)
            return r

        if self.current_affairs is None:
            r = SyncResult(success=False, operation=op,
                           errors=["current_affairs_manager not wired"],
                           duration_seconds=time.time() - start)
            self._log_sync(r)
            return r

        errors: List[str] = []
        new_count = 0
        try:
            stats = self.current_affairs.run_daily_update()
            new_count = int(stats.get("new", 0))
            if new_count > 0 and self.rag is not None:
                try:
                    self.current_affairs.add_to_rag_database(rag_module=self.rag)
                except Exception as e:
                    errors.append(f"rag_ingest: {e}")
        except Exception as e:
            errors.append(str(e))

        r = SyncResult(
            success=not errors,
            operation=op,
            items_synced=new_count,
            duration_seconds=time.time() - start,
            errors=errors,
        )
        self._log_sync(r)
        return r

    # -------- Content downloads (differential, resumable) --------------

    def _job_id(self, url: str, exam_type: str) -> str:
        return hashlib.sha1(f"{exam_type}|{url}".encode()).hexdigest()[:16]

    def _save_job(self, job: DownloadJob) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO download_jobs
                (job_id, url, dest_path, total_bytes, downloaded_bytes, status,
                 version, exam_type, etag, last_modified, started_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job.job_id, job.url, job.dest_path, job.total_bytes,
                 job.downloaded_bytes, job.status, job.version, job.exam_type,
                 job.etag, job.last_modified, job.started_at, job.completed_at),
            )

    def _load_job(self, job_id: str) -> Optional[DownloadJob]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM download_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if not row:
            return None
        return DownloadJob(**dict(row))

    def download_content_updates(
        self,
        exam_type: str,
        manifest_url: Optional[str] = None,
        manifest: Optional[Dict[str, Any]] = None,
    ) -> SyncResult:
        """
        Downloads only content items whose version differs from the local
        content_versions table. Supports resume via HTTP Range requests.

        Manifest format (JSON):
          {"exam": "TNPSC", "items": [
             {"key":"history_vol1", "url":"...", "version":"2024.1",
              "checksum":"sha256:...", "size": 1234567}, ...]}
        """
        start = time.time()
        op = f"content_update:{exam_type}"

        if not self.is_online():
            r = SyncResult(success=False, operation=op,
                           errors=["offline"],
                           duration_seconds=time.time() - start)
            self._log_sync(r)
            return r

        # Acquire manifest
        if manifest is None:
            if not manifest_url:
                r = SyncResult(success=False, operation=op,
                               errors=["no manifest"],
                               duration_seconds=time.time() - start)
                self._log_sync(r)
                return r
            try:
                import requests
                resp = requests.get(manifest_url, timeout=15)
                resp.raise_for_status()
                manifest = resp.json()
            except Exception as e:
                r = SyncResult(success=False, operation=op,
                               errors=[f"manifest fetch: {e}"],
                               duration_seconds=time.time() - start)
                self._log_sync(r)
                return r

        items = manifest.get("items", []) if isinstance(manifest, dict) else []
        if not items:
            r = SyncResult(success=True, operation=op,
                           items_synced=0,
                           duration_seconds=time.time() - start)
            self._log_sync(r)
            return r

        installed = self._installed_versions(exam_type)
        to_download = [
            it for it in items
            if installed.get(it.get("key")) != it.get("version")
        ]
        logger.info(f"{exam_type}: {len(to_download)} content items to update")

        errors: List[str] = []
        total_bytes = 0
        synced = 0
        for item in to_download:
            try:
                bytes_dl = self._download_with_resume(
                    url=item["url"],
                    exam_type=exam_type,
                    key=item["key"],
                    version=item.get("version", ""),
                    expected_checksum=item.get("checksum", ""),
                )
                total_bytes += bytes_dl
                synced += 1
            except Exception as e:
                errors.append(f"{item.get('key')}: {e}")

        r = SyncResult(
            success=not errors,
            operation=op,
            items_synced=synced,
            bytes_transferred=total_bytes,
            errors=errors,
            duration_seconds=time.time() - start,
        )
        self._log_sync(r)
        return r

    def _installed_versions(self, exam_type: str) -> Dict[str, str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT content_key, version FROM content_versions WHERE exam_type=?",
                (exam_type,),
            ).fetchall()
        return {r["content_key"]: r["version"] for r in rows}

    def _download_with_resume(
        self,
        url: str,
        exam_type: str,
        key: str,
        version: str = "",
        expected_checksum: str = "",
        chunk_size: int = 64 * 1024,
    ) -> int:
        import requests

        job_id = self._job_id(url, exam_type)
        dest_dir = self.cache_dir / exam_type
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / f"{key}-{version or 'latest'}"
        partial_path = dest_path.with_suffix(dest_path.suffix + ".part")

        existing = self._load_job(job_id)
        downloaded = partial_path.stat().st_size if partial_path.exists() else 0

        headers: Dict[str, str] = {}
        if downloaded > 0:
            headers["Range"] = f"bytes={downloaded}-"
        if existing and existing.etag:
            headers["If-Match"] = existing.etag

        job = DownloadJob(
            job_id=job_id, url=url, dest_path=str(dest_path),
            downloaded_bytes=downloaded, status="active",
            version=version, exam_type=exam_type,
            started_at=datetime.utcnow().isoformat(),
        )
        self._save_job(job)

        try:
            with requests.get(url, headers=headers, stream=True, timeout=30) as resp:
                resp.raise_for_status()
                job.etag = resp.headers.get("ETag", "")
                job.last_modified = resp.headers.get("Last-Modified", "")
                content_length = int(resp.headers.get("Content-Length", 0))
                job.total_bytes = content_length + downloaded
                self._save_job(job)

                mode = "ab" if downloaded > 0 else "wb"
                with open(partial_path, mode) as f:
                    for chunk in resp.iter_content(chunk_size=chunk_size):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        job.downloaded_bytes = downloaded

            if expected_checksum:
                actual = self._sha256(partial_path)
                expected = expected_checksum.split(":", 1)[-1]
                if expected and actual != expected:
                    partial_path.unlink(missing_ok=True)
                    raise ValueError(f"checksum mismatch for {key}")

            partial_path.replace(dest_path)
            job.status = "completed"
            job.completed_at = datetime.utcnow().isoformat()
            self._save_job(job)

            checksum = expected_checksum or f"sha256:{self._sha256(dest_path)}"
            self._record_installed(exam_type, key, version, checksum, str(dest_path))
            return downloaded
        except Exception:
            job.status = "paused" if downloaded > 0 else "failed"
            self._save_job(job)
            raise

    def _record_installed(
        self, exam_type: str, key: str, version: str,
        checksum: str, file_path: str,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO content_versions
                (exam_type, content_key, version, checksum, installed_at, file_path)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (exam_type, key, version, checksum,
                 datetime.utcnow().isoformat(), file_path),
            )

    def _sha256(self, path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for blk in iter(lambda: f.read(64 * 1024), b""):
                h.update(blk)
        return h.hexdigest()

    def resume_pending_downloads(self) -> SyncResult:
        start = time.time()
        op = "resume_downloads"
        if not self.is_online():
            r = SyncResult(success=False, operation=op, errors=["offline"],
                           duration_seconds=time.time() - start)
            self._log_sync(r)
            return r

        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM download_jobs WHERE status IN ('paused','active')"
            ).fetchall()
        jobs = [DownloadJob(**dict(r)) for r in rows]
        errors: List[str] = []
        resumed = 0
        total_bytes = 0
        for j in jobs:
            try:
                b = self._download_with_resume(
                    url=j.url, exam_type=j.exam_type,
                    key=Path(j.dest_path).stem.split("-")[0],
                    version=j.version,
                )
                total_bytes += b
                resumed += 1
            except Exception as e:
                errors.append(f"{j.job_id}: {e}")

        r = SyncResult(
            success=not errors, operation=op,
            items_synced=resumed, bytes_transferred=total_bytes,
            errors=errors, duration_seconds=time.time() - start,
        )
        self._log_sync(r)
        return r

    # -------- Backup / Restore ------------------------------------------

    def backup_user_progress(
        self,
        user_id: str,
        include_practice_db: bool = True,
    ) -> SyncResult:
        start = time.time()
        op = f"backup:{user_id}"
        errors: List[str] = []
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        backup_path = self.backup_dir / f"{user_id}_{timestamp}.tar.gz"

        targets: List[Path] = []
        candidates = [
            getattr(settings.user, "db_path", None),
            getattr(settings.practice, "db_path", None) if include_practice_db else None,
            getattr(getattr(settings, "syllabus", None), "db_path", None),
        ]
        for p in candidates:
            if p and Path(p).exists():
                targets.append(Path(p))

        if not targets:
            r = SyncResult(success=False, operation=op,
                           errors=["no databases to back up"],
                           duration_seconds=time.time() - start)
            self._log_sync(r)
            return r

        meta = {
            "user_id": user_id,
            "created_at": datetime.utcnow().isoformat(),
            "sources": [str(t) for t in targets],
        }

        try:
            with tarfile.open(backup_path, "w:gz") as tar:
                for t in targets:
                    tar.add(str(t), arcname=t.name)
                meta_bytes = json.dumps(meta, indent=2).encode()
                info = tarfile.TarInfo(name="manifest.json")
                info.size = len(meta_bytes)
                import io
                tar.addfile(info, io.BytesIO(meta_bytes))

            size = backup_path.stat().st_size
            checksum = self._sha256(backup_path)
            with self._conn() as c:
                c.execute(
                    """
                    INSERT INTO backup_log
                    (user_id, backup_path, size_bytes, checksum, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (user_id, str(backup_path), size, checksum,
                     datetime.utcnow().isoformat()),
                )
        except Exception as e:
            errors.append(str(e))

        r = SyncResult(
            success=not errors, operation=op,
            items_synced=len(targets),
            bytes_transferred=backup_path.stat().st_size if backup_path.exists() else 0,
            errors=errors, duration_seconds=time.time() - start,
        )
        self._log_sync(r)
        return r

    def restore_from_backup(
        self,
        user_id: str,
        backup_path: Optional[str] = None,
    ) -> SyncResult:
        start = time.time()
        op = f"restore:{user_id}"
        errors: List[str] = []

        if backup_path:
            bp = Path(backup_path)
        else:
            candidates = sorted(
                self.backup_dir.glob(f"{user_id}_*.tar.gz"), reverse=True
            )
            if not candidates:
                r = SyncResult(success=False, operation=op,
                               errors=[f"no backup found for {user_id}"],
                               duration_seconds=time.time() - start)
                self._log_sync(r)
                return r
            bp = candidates[0]

        if not bp.exists():
            r = SyncResult(success=False, operation=op,
                           errors=[f"backup not found: {bp}"],
                           duration_seconds=time.time() - start)
            self._log_sync(r)
            return r

        restored = 0
        try:
            extract_to = self.cache_dir / f"restore_{user_id}"
            extract_to.mkdir(parents=True, exist_ok=True)
            with tarfile.open(bp, "r:gz") as tar:
                tar.extractall(extract_to)

            mapping = {
                "users.db": getattr(settings.user, "db_path", None),
                "questions.db": getattr(settings.practice, "db_path", None),
                "syllabus.db": getattr(getattr(settings, "syllabus", None), "db_path", None),
            }
            for name, dest in mapping.items():
                src = extract_to / name
                if src.exists() and dest:
                    Path(dest).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                    restored += 1

            with self._conn() as c:
                c.execute(
                    "UPDATE backup_log SET restored_at=? WHERE backup_path=?",
                    (datetime.utcnow().isoformat(), str(bp)),
                )
        except Exception as e:
            errors.append(str(e))

        r = SyncResult(
            success=not errors, operation=op,
            items_synced=restored,
            duration_seconds=time.time() - start,
            errors=errors,
        )
        self._log_sync(r)
        return r

    # -------- Storage optimisation --------------------------------------

    def _dir_size(self, path: Path) -> int:
        total = 0
        if not path.exists():
            return 0
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
        return total

    def get_storage_usage(self) -> Dict[str, Any]:
        base = Path(settings.data_dir)
        vdb = Path(settings.vector_db.db_path)
        models = Path(settings.models_dir)
        usage = {
            "data_dir_bytes": self._dir_size(base),
            "vector_db_bytes": self._dir_size(vdb),
            "models_bytes": self._dir_size(models),
            "backups_bytes": self._dir_size(self.backup_dir),
            "cache_bytes": self._dir_size(self.cache_dir),
        }
        usage["total_bytes"] = sum(usage.values())
        usage["limit_bytes"] = self.storage_limit_bytes
        usage["pct_of_limit"] = round(
            (usage["total_bytes"] / self.storage_limit_bytes) * 100.0, 1
        ) if self.storage_limit_bytes > 0 else 0.0
        return usage

    def optimize_storage(
        self,
        max_backups_per_user: int = 3,
        compress_older_than_days: int = 30,
        prune_least_used: bool = False,
        user_confirmed_prune: bool = False,
    ) -> SyncResult:
        start = time.time()
        op = "optimize_storage"
        errors: List[str] = []
        freed = 0
        actions = 0

        # 1. Rotate backups
        try:
            by_user: Dict[str, List[Path]] = {}
            for p in self.backup_dir.glob("*.tar.gz"):
                uid = p.stem.split("_")[0]
                by_user.setdefault(uid, []).append(p)
            for uid, paths in by_user.items():
                paths.sort(reverse=True)
                for old in paths[max_backups_per_user:]:
                    freed += old.stat().st_size
                    old.unlink()
                    actions += 1
        except Exception as e:
            errors.append(f"backup rotation: {e}")

        # 2. Compress old cache files (>N days) that aren't .gz already
        try:
            cutoff = time.time() - compress_older_than_days * 86400
            for p in self.cache_dir.rglob("*"):
                if not p.is_file():
                    continue
                if p.suffix in (".gz", ".bz2", ".xz", ".zip", ".tar"):
                    continue
                try:
                    if p.stat().st_mtime < cutoff and p.stat().st_size > 64 * 1024:
                        import gzip
                        gz = p.with_suffix(p.suffix + ".gz")
                        with open(p, "rb") as fin, gzip.open(gz, "wb") as fout:
                            shutil.copyfileobj(fin, fout)
                        saved = p.stat().st_size - gz.stat().st_size
                        if saved > 0:
                            freed += saved
                        p.unlink()
                        actions += 1
                except Exception as e:
                    errors.append(f"compress {p.name}: {e}")
        except Exception as e:
            errors.append(f"compression: {e}")

        # 3. Prune least-used content (only if user confirmed)
        if prune_least_used and user_confirmed_prune and self.user_manager is not None:
            try:
                freed += self._prune_least_used_content()
                actions += 1
            except Exception as e:
                errors.append(f"prune: {e}")

        # 4. Remove failed download jobs older than 7 days
        try:
            with self._conn() as c:
                cutoff_iso = (datetime.utcnow() - timedelta(days=7)).isoformat()
                c.execute(
                    "DELETE FROM download_jobs WHERE status='failed' AND started_at < ?",
                    (cutoff_iso,),
                )
        except Exception as e:
            errors.append(f"job cleanup: {e}")

        r = SyncResult(
            success=not errors, operation=op,
            items_synced=actions, bytes_transferred=freed,
            errors=errors, duration_seconds=time.time() - start,
        )
        self._log_sync(r)
        return r

    def _prune_least_used_content(self) -> int:
        """Delete content_version files for topics unused in last 90 days."""
        if self.user_manager is None:
            return 0
        cutoff = (datetime.utcnow() - timedelta(days=90)).isoformat()
        freed = 0
        with self._conn() as c:
            rows = c.execute(
                "SELECT exam_type, content_key, file_path, installed_at FROM content_versions"
            ).fetchall()
        for r in rows:
            if r["installed_at"] > cutoff:
                continue
            fp = r["file_path"]
            if fp and Path(fp).exists():
                try:
                    freed += Path(fp).stat().st_size
                    Path(fp).unlink()
                    with self._conn() as c:
                        c.execute(
                            "DELETE FROM content_versions WHERE exam_type=? AND content_key=?",
                            (r["exam_type"], r["content_key"]),
                        )
                except OSError:
                    pass
        return freed

    # -------- Graceful degradation wrapper ------------------------------

    def run_online_features_if_possible(
        self,
        online_fn: Callable[[], Any],
        offline_fallback: Optional[Callable[[], Any]] = None,
        feature_name: str = "feature",
    ) -> Any:
        if self.is_online():
            try:
                return online_fn()
            except Exception as e:
                logger.warning(f"{feature_name} online call failed: {e}")
        logger.info(f"{feature_name}: using offline fallback")
        return offline_fallback() if offline_fallback else None


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------

_default_manager: Optional[OfflineSyncManager] = None


def _get_default() -> OfflineSyncManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = OfflineSyncManager()
    return _default_manager


def sync_current_affairs(last_sync_date: Optional[str] = None) -> SyncResult:
    return _get_default().sync_current_affairs(last_sync_date=last_sync_date)


def download_content_updates(
    exam_type: str,
    manifest_url: Optional[str] = None,
    manifest: Optional[Dict[str, Any]] = None,
) -> SyncResult:
    return _get_default().download_content_updates(
        exam_type=exam_type, manifest_url=manifest_url, manifest=manifest
    )


def backup_user_progress(user_id: str) -> SyncResult:
    return _get_default().backup_user_progress(user_id)


def restore_from_backup(
    user_id: str, backup_path: Optional[str] = None
) -> SyncResult:
    return _get_default().restore_from_backup(user_id, backup_path=backup_path)


def optimize_storage(
    max_backups_per_user: int = 3,
    compress_older_than_days: int = 30,
    user_confirmed_prune: bool = False,
) -> SyncResult:
    return _get_default().optimize_storage(
        max_backups_per_user=max_backups_per_user,
        compress_older_than_days=compress_older_than_days,
        user_confirmed_prune=user_confirmed_prune,
    )
