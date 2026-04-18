"""Admin interface for content and question management."""

import csv
import hashlib
import json
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import settings
from modules.chunker import Chunk, SemanticChunker
from modules.ingestion import DocumentProcessor
from modules.question_bank import QuestionBank
from modules.rag import RAGModule
from modules.syllabus_manager import SyllabusManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL for admin meta tables
# ---------------------------------------------------------------------------

_DDL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS admin_uploads (
    upload_id     TEXT PRIMARY KEY,
    filename      TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    exam_type     TEXT NOT NULL,
    subject       TEXT NOT NULL,
    topic         TEXT NOT NULL,
    language      TEXT DEFAULT 'en',
    page_count    INTEGER DEFAULT 0,
    chunk_count   INTEGER DEFAULT 0,
    ocr_used      INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'pending',
    uploaded_at   TEXT NOT NULL,
    committed_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_au_status ON admin_uploads(status);
CREATE INDEX IF NOT EXISTS idx_au_sha    ON admin_uploads(sha256);

CREATE TABLE IF NOT EXISTS question_flags (
    question_id   TEXT PRIMARY KEY,
    reason        TEXT DEFAULT '',
    flagged_by    TEXT DEFAULT '',
    flagged_at    TEXT NOT NULL,
    resolved      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS content_access_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    topic         TEXT NOT NULL,
    subject       TEXT NOT NULL,
    exam_type     TEXT NOT NULL,
    access_type   TEXT NOT NULL,
    user_id       TEXT DEFAULT '',
    accessed_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cal_topic ON content_access_log(topic, subject);
"""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PreviewChunk:
    index: int
    text: str
    token_count: int
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class UploadPreview:
    upload_id: str
    filename: str
    exam_type: str
    subject: str
    topic: str
    language: str
    page_count: int
    ocr_used: bool
    tables_found: int
    chunk_count: int
    duplicate_of: Optional[str]
    ocr_confidence: str
    tamil_valid: bool
    chunks: List[PreviewChunk] = field(default_factory=list)


@dataclass
class BulkResult:
    inserted: int
    skipped: int
    errors: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# AdminManager
# ---------------------------------------------------------------------------


class AdminManager:
    def __init__(
        self,
        rag_module: Optional[RAGModule] = None,
        question_bank: Optional[QuestionBank] = None,
        syllabus_manager: Optional[SyllabusManager] = None,
        db_path: Optional[str] = None,
        upload_dir: Optional[str] = None,
    ):
        admin_cfg = getattr(settings, "admin", None)
        base_data = Path(settings.data_dir)

        if db_path:
            self.db_path = Path(db_path)
        elif admin_cfg is not None:
            self.db_path = Path(admin_cfg.db_path)
        else:
            self.db_path = base_data / "admin.db"

        if upload_dir:
            self.upload_dir = Path(upload_dir)
        elif admin_cfg is not None:
            self.upload_dir = Path(admin_cfg.upload_dir)
        else:
            self.upload_dir = base_data / "uploads"

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)

        self.rag = rag_module or RAGModule()
        self.question_bank = question_bank or QuestionBank()
        self.syllabus = syllabus_manager or SyllabusManager()
        self._doc_processor = DocumentProcessor()
        self._chunker = SemanticChunker(
            embedding_model_name=settings.vector_db.embedding_model
        )
        self._pending: Dict[str, Dict[str, Any]] = {}
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

    # -------- File hashing / dedup --------------------------------------

    @staticmethod
    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def _find_duplicate(self, sha: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute(
                "SELECT upload_id FROM admin_uploads WHERE sha256=? AND status='committed'",
                (sha,),
            ).fetchone()
        return row["upload_id"] if row else None

    # -------- Quality control helpers -----------------------------------

    @staticmethod
    def _is_tamil(text: str) -> bool:
        return any("\u0B80" <= ch <= "\u0BFF" for ch in text)

    @staticmethod
    def _validate_tamil_text(text: str) -> bool:
        """Cheap heuristic: ensure Tamil chars have complete syllables
        (no stray combining marks without base consonants)."""
        if not AdminManager._is_tamil(text):
            return True
        combining_only = 0
        total = 0
        for ch in text:
            if "\u0B80" <= ch <= "\u0BFF":
                total += 1
                # Combining marks without standalone vowels
                if ch in "\u0BBE\u0BBF\u0BC0\u0BC1\u0BC2\u0BC6\u0BC7\u0BC8":
                    combining_only += 1
        return total == 0 or (combining_only / max(total, 1)) < 0.5

    @staticmethod
    def _ocr_confidence_label(doc) -> str:
        if not getattr(doc, "ocr_used", False):
            return "native"
        chars = getattr(doc, "char_count", 0)
        pages = max(getattr(doc, "page_count", 1), 1)
        density = chars / pages
        if density > 1500:
            return "high"
        if density > 500:
            return "medium"
        return "low"

    # -------- Document upload + preview ---------------------------------

    def upload_document(
        self,
        file_path: str,
        metadata: Dict[str, Any],
    ) -> UploadPreview:
        """Stage an uploaded document and produce a preview without committing to RAG."""
        return self.process_and_preview(file_path, metadata)

    def process_and_preview(
        self,
        file_path: str,
        metadata: Dict[str, Any],
    ) -> UploadPreview:
        src = Path(file_path)
        if not src.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        sha = self._sha256(src)
        duplicate_of = self._find_duplicate(sha)

        # Copy to managed upload dir
        upload_id = str(uuid.uuid4())
        staged = self.upload_dir / f"{upload_id}-{src.name}"
        if not staged.exists():
            staged.write_bytes(src.read_bytes())

        exam_type = metadata.get("exam_type", settings.exam.default_exam)
        subject = metadata.get("subject", "General")
        topic = metadata.get("topic", "General")
        language = metadata.get("language", "en")

        # Auto-categorise if subject/topic left blank via syllabus mapper
        doc = self._doc_processor.process_document(
            file_path=str(staged),
            exam_type=exam_type,
            subject=subject,
            topic=topic,
            language=language,
        )

        ocr_label = self._ocr_confidence_label(doc)
        tamil_ok = self._validate_tamil_text(doc.raw_text) if language == "ta" else True

        # Chunk per page (same strategy as RAGModule.ingest_document)
        base_meta = {
            "source": src.name,
            "exam_type": exam_type,
            "subject": subject,
            "topic": topic,
            "language": language,
            "upload_id": upload_id,
        }
        all_chunks: List[Chunk] = []
        for page_num, page_text in enumerate(doc.pages, start=1):
            if not page_text.strip():
                continue
            page_meta = {**base_meta, "page": page_num}
            page_chunks = self._chunker.chunk_text(
                page_text,
                chunk_size=settings.vector_db.chunk_size,
                overlap=settings.vector_db.chunk_overlap,
                metadata=page_meta,
            )
            all_chunks.extend(page_chunks)

        # Auto-tag via syllabus mapper when subject/topic are generic
        if subject in ("", "General") and all_chunks:
            mapped = self.syllabus.content_mapper(
                all_chunks[:20], exam=exam_type,
                min_confidence=settings.syllabus.min_mapping_confidence,
                persist=False,
            )
            if mapped:
                top = mapped[0]
                subject = top.subject_code or subject
                topic = top.topic_code or topic
                for ch in all_chunks:
                    ch.metadata["subject"] = subject
                    ch.metadata["topic"] = topic

        preview_chunks = [
            PreviewChunk(
                index=c.chunk_index,
                text=c.text,
                token_count=c.token_count,
                metadata=dict(c.metadata),
            )
            for c in all_chunks[:10]
        ]

        now = datetime.utcnow().isoformat()
        with self._conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO admin_uploads
                (upload_id, filename, sha256, exam_type, subject, topic, language,
                 page_count, chunk_count, ocr_used, status, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (upload_id, src.name, sha, exam_type, subject, topic, language,
                 doc.page_count, len(all_chunks), int(doc.ocr_used), now),
            )

        self._pending[upload_id] = {
            "chunks": all_chunks,
            "metadata": base_meta,
            "file_path": str(staged),
        }

        return UploadPreview(
            upload_id=upload_id,
            filename=src.name,
            exam_type=exam_type,
            subject=subject,
            topic=topic,
            language=language,
            page_count=doc.page_count,
            ocr_used=doc.ocr_used,
            tables_found=len(doc.tables),
            chunk_count=len(all_chunks),
            duplicate_of=duplicate_of,
            ocr_confidence=ocr_label,
            tamil_valid=tamil_ok,
            chunks=preview_chunks,
        )

    def commit_upload(self, upload_id: str) -> Dict[str, Any]:
        """Commit a previously previewed upload into the RAG vector DB."""
        staged = self._pending.get(upload_id)
        if not staged:
            raise KeyError(f"No pending upload with id {upload_id}")
        chunks: List[Chunk] = staged["chunks"]
        if not chunks:
            raise ValueError("No chunks to commit")

        embeddings = self.rag.generate_embeddings(chunks)
        ids = self.rag.store_in_vectordb(chunks, embeddings)

        # Persist content map via syllabus manager
        try:
            self.syllabus.content_mapper(
                chunks, exam=staged["metadata"]["exam_type"], persist=True
            )
        except Exception as e:
            logger.warning(f"syllabus mapping on commit failed: {e}")

        now = datetime.utcnow().isoformat()
        with self._conn() as c:
            c.execute(
                "UPDATE admin_uploads SET status='committed', committed_at=? WHERE upload_id=?",
                (now, upload_id),
            )
        self._pending.pop(upload_id, None)
        return {"upload_id": upload_id, "chunks_committed": len(ids), "ids": ids}

    def discard_upload(self, upload_id: str) -> bool:
        self._pending.pop(upload_id, None)
        with self._conn() as c:
            cur = c.execute(
                "UPDATE admin_uploads SET status='discarded' WHERE upload_id=?",
                (upload_id,),
            )
        return cur.rowcount > 0

    def list_uploads(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM admin_uploads"
        params: List[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY uploaded_at DESC LIMIT 200"
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # -------- Content management (edit/delete chunks) -------------------

    def manage_content(
        self,
        action: str,
        content_id: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """CRUD for previously committed content.

        Actions: delete_source, delete_chunks, update_metadata, get_source_stats
        """
        action = action.lower()

        if action == "delete_source":
            source = kwargs.get("source") or content_id
            if not source:
                raise ValueError("source filename required")
            removed = self.rag.delete_by_source(source)
            return {"action": action, "source": source, "removed": removed}

        if action == "delete_chunks":
            ids = kwargs.get("ids") or ([content_id] if content_id else [])
            if not ids:
                raise ValueError("chunk ids required")
            try:
                self.rag._vector_store._collection.delete(ids=ids)
                return {"action": action, "removed": len(ids)}
            except Exception as e:
                raise RuntimeError(f"delete failed: {e}") from e

        if action == "update_metadata":
            if not content_id:
                raise ValueError("chunk id required")
            new_meta = kwargs.get("metadata", {})
            coll = self.rag._vector_store._collection
            existing = coll.get(ids=[content_id])
            if not existing.get("ids"):
                raise KeyError(f"chunk {content_id} not found")
            merged = dict(existing["metadatas"][0]) if existing.get("metadatas") else {}
            merged.update(new_meta)
            coll.update(ids=[content_id], metadatas=[merged])
            return {"action": action, "id": content_id, "metadata": merged}

        if action == "get_source_stats":
            source = kwargs.get("source") or content_id
            coll = self.rag._vector_store._collection
            res = coll.get(where={"source": source}) if source else coll.get()
            return {
                "action": action,
                "source": source,
                "chunk_count": len(res.get("ids", [])),
            }

        raise ValueError(f"Unknown action: {action}")

    # -------- Question bank management ---------------------------------

    def add_question(self, q: Dict[str, Any]) -> str:
        return self.question_bank.add_question(q)

    def update_question(self, question_id: str, updates: Dict[str, Any]) -> bool:
        existing = self.question_bank.get_question(question_id)
        if not existing:
            raise KeyError(f"question {question_id} not found")
        merged = {
            "question_id": question_id,
            "text": updates.get("text", existing.text),
            "option_a": updates.get("option_a", existing.option_a),
            "option_b": updates.get("option_b", existing.option_b),
            "option_c": updates.get("option_c", existing.option_c),
            "option_d": updates.get("option_d", existing.option_d),
            "correct_answer": updates.get("correct_answer", existing.correct_answer),
            "explanation": updates.get("explanation", existing.explanation),
            "subject": updates.get("subject", existing.subject),
            "exam_type": updates.get("exam_type", existing.exam_type),
            "difficulty": updates.get("difficulty", existing.difficulty),
            "topic": updates.get("topic", existing.topic),
            "year": updates.get("year", existing.year),
            "language": updates.get("language", existing.language),
            "text_tamil": updates.get("text_tamil", existing.text_tamil),
            "option_a_tamil": updates.get("option_a_tamil", existing.option_a_tamil),
            "option_b_tamil": updates.get("option_b_tamil", existing.option_b_tamil),
            "option_c_tamil": updates.get("option_c_tamil", existing.option_c_tamil),
            "option_d_tamil": updates.get("option_d_tamil", existing.option_d_tamil),
            "explanation_tamil": updates.get("explanation_tamil", existing.explanation_tamil),
        }
        self.question_bank.add_question(merged)
        return True

    def delete_question(self, question_id: str) -> bool:
        with sqlite3.connect(str(self.question_bank._db_path)) as conn:
            cur = conn.execute(
                "DELETE FROM questions WHERE question_id=?", (question_id,)
            )
            conn.commit()
            return cur.rowcount > 0

    def flag_question(
        self, question_id: str, reason: str = "", flagged_by: str = "admin"
    ) -> None:
        now = datetime.utcnow().isoformat()
        with self._conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO question_flags
                (question_id, reason, flagged_by, flagged_at, resolved)
                VALUES (?, ?, ?, ?, 0)
                """,
                (question_id, reason, flagged_by, now),
            )

    def list_flagged_questions(self) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM question_flags WHERE resolved=0 ORDER BY flagged_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def resolve_flag(self, question_id: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE question_flags SET resolved=1 WHERE question_id=?",
                (question_id,),
            )
        return cur.rowcount > 0

    # -------- Bulk question upload -------------------------------------

    _REQUIRED_Q_COLS = {
        "text", "option_a", "option_b", "option_c", "option_d",
        "correct_answer", "subject", "exam_type",
    }

    def bulk_upload_questions(self, file_path: str) -> BulkResult:
        """Import questions from CSV / XLSX / JSON."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        suffix = path.suffix.lower()
        if suffix == ".json":
            records = json.loads(path.read_text(encoding="utf-8"))
        elif suffix == ".csv":
            records = self._read_csv(path)
        elif suffix in (".xlsx", ".xls"):
            records = self._read_excel(path)
        else:
            raise ValueError(f"Unsupported format: {suffix}")

        return self._ingest_question_records(records)

    @staticmethod
    def _read_csv(path: Path) -> List[Dict[str, Any]]:
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            return [dict(r) for r in reader]

    @staticmethod
    def _read_excel(path: Path) -> List[Dict[str, Any]]:
        try:
            import pandas as pd
        except ImportError as e:
            raise ImportError(
                "pandas + openpyxl required for Excel upload"
            ) from e
        df = pd.read_excel(path)
        df = df.where(df.notnull(), None)
        return df.to_dict(orient="records")

    def _ingest_question_records(
        self, records: List[Dict[str, Any]]
    ) -> BulkResult:
        inserted = 0
        skipped = 0
        errors: List[str] = []

        for idx, rec in enumerate(records, start=1):
            rec = {k.strip(): (v.strip() if isinstance(v, str) else v)
                   for k, v in rec.items() if k}
            missing = self._REQUIRED_Q_COLS - set(rec.keys())
            if missing:
                skipped += 1
                errors.append(f"row {idx}: missing {missing}")
                continue
            try:
                self.question_bank.add_question(rec)
                inserted += 1
            except Exception as e:
                skipped += 1
                errors.append(f"row {idx}: {e}")

        logger.info(
            f"Bulk question upload: {inserted} inserted, {skipped} skipped"
        )
        return BulkResult(inserted=inserted, skipped=skipped, errors=errors)

    # -------- Duplicate detection for questions ------------------------

    def find_duplicate_questions(self) -> List[Tuple[str, str]]:
        with sqlite3.connect(str(self.question_bank._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT a.question_id AS id_a, b.question_id AS id_b
                FROM questions a JOIN questions b
                  ON a.rowid < b.rowid
                 AND lower(a.text) = lower(b.text)
                """
            ).fetchall()
        return [(r["id_a"], r["id_b"]) for r in rows]

    # -------- Analytics / reports --------------------------------------

    def log_content_access(
        self, topic: str, subject: str, exam_type: str,
        access_type: str = "view", user_id: str = "",
    ) -> None:
        now = datetime.utcnow().isoformat()
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO content_access_log
                (topic, subject, exam_type, access_type, user_id, accessed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (topic, subject, exam_type, access_type, user_id, now),
            )

    def generate_content_report(self) -> Dict[str, Any]:
        """Return usage statistics and content-gap info."""
        report: Dict[str, Any] = {
            "generated_at": datetime.utcnow().isoformat(),
        }

        with self._conn() as c:
            total_uploads = c.execute(
                "SELECT COUNT(*) FROM admin_uploads WHERE status='committed'"
            ).fetchone()[0]
            pending = c.execute(
                "SELECT COUNT(*) FROM admin_uploads WHERE status='pending'"
            ).fetchone()[0]
            flagged = c.execute(
                "SELECT COUNT(*) FROM question_flags WHERE resolved=0"
            ).fetchone()[0]
            most_accessed = c.execute(
                """SELECT topic, subject, COUNT(*) AS hits
                   FROM content_access_log
                   GROUP BY topic, subject
                   ORDER BY hits DESC LIMIT 10"""
            ).fetchall()

        report["total_committed_uploads"] = total_uploads
        report["pending_uploads"] = pending
        report["flagged_questions"] = flagged
        report["most_accessed_topics"] = [dict(r) for r in most_accessed]

        # Question bank stats
        with sqlite3.connect(str(self.question_bank._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            q_total = conn.execute("SELECT COUNT(*) AS n FROM questions").fetchone()["n"]
            per_exam = conn.execute(
                """SELECT exam_type, COUNT(*) AS n FROM questions
                   GROUP BY exam_type ORDER BY n DESC"""
            ).fetchall()
            per_difficulty = conn.execute(
                """SELECT difficulty, COUNT(*) AS n FROM questions
                   GROUP BY difficulty"""
            ).fetchall()
            perf_hot = conn.execute(
                """SELECT subject, topic, SUM(total_attempted) AS att,
                          SUM(correct_count) AS correct
                   FROM user_performance
                   GROUP BY subject, topic
                   HAVING att > 0
                   ORDER BY att DESC LIMIT 10"""
            ).fetchall()

        report["question_total"] = q_total
        report["questions_per_exam"] = [dict(r) for r in per_exam]
        report["questions_per_difficulty"] = [dict(r) for r in per_difficulty]
        report["hot_question_topics"] = [
            {
                "subject": r["subject"],
                "topic": r["topic"],
                "attempted": r["att"],
                "accuracy": round(
                    (r["correct"] or 0) / r["att"] * 100, 1
                ) if r["att"] else 0.0,
            }
            for r in perf_hot
        ]

        # Content gaps — syllabus topics with zero mapped chunks
        gaps: List[Dict[str, Any]] = []
        try:
            for exam in self.syllabus.list_exams():
                syl = self.syllabus.load_syllabus(exam)
                if not syl:
                    continue
                with sqlite3.connect(str(self.syllabus.db_path)) as scn:
                    scn.row_factory = sqlite3.Row
                    for subj in syl.subjects:
                        for topic in subj.topics:
                            row = scn.execute(
                                """SELECT COUNT(*) AS n FROM content_map
                                   WHERE exam_code=? AND topic_code=?""",
                                (syl.exam_code, topic.topic_code),
                            ).fetchone()
                            if row and row["n"] == 0:
                                gaps.append({
                                    "exam": syl.exam_code,
                                    "subject": subj.subject_name,
                                    "topic": topic.topic_name,
                                })
        except Exception as e:
            logger.debug(f"gap scan skipped: {e}")
        report["content_gaps"] = gaps[:50]

        return report


# ---------------------------------------------------------------------------
# Module-level helper functions
# ---------------------------------------------------------------------------

_default_admin: Optional[AdminManager] = None


def _get_admin() -> AdminManager:
    global _default_admin
    if _default_admin is None:
        _default_admin = AdminManager()
    return _default_admin


def upload_document(file_path: str, metadata: Dict[str, Any]) -> UploadPreview:
    return _get_admin().upload_document(file_path, metadata)


def process_and_preview(file_path: str, metadata: Dict[str, Any]) -> UploadPreview:
    return _get_admin().process_and_preview(file_path, metadata)


def bulk_upload_questions(file_path: str) -> BulkResult:
    return _get_admin().bulk_upload_questions(file_path)


def manage_content(action: str, content_id: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    return _get_admin().manage_content(action, content_id, **kwargs)


def generate_content_report() -> Dict[str, Any]:
    return _get_admin().generate_content_report()
