from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exam -> syllabus JSON file map
# ---------------------------------------------------------------------------

_EXAM_FILE_MAP: Dict[str, str] = {
    "TNPSC_GROUP1": "tnpsc_group1.json",
    "TNPSC_GROUP2": "tnpsc_group2.json",
    "TNPSC_GROUP2A": "tnpsc_group2.json",
    "TNPSC_GROUP4": "tnpsc_group4.json",
    "TNPSC": "tnpsc_group4.json",
    "TRB": "trb.json",
    "BANKING": "banking.json",
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Subtopic:
    code: str
    name: str
    hours: float = 1.0
    priority: str = "medium"
    frequently_asked: bool = False


@dataclass
class Topic:
    topic_code: str
    topic_name: str
    topic_name_tamil: str = ""
    estimated_hours: float = 0.0
    priority: str = "medium"
    subtopics: List[Subtopic] = field(default_factory=list)


@dataclass
class Subject:
    subject_code: str
    subject_name: str
    subject_name_tamil: str = ""
    stage: str = "Single"
    weightage: float = 0.0
    topics: List[Topic] = field(default_factory=list)


@dataclass
class Syllabus:
    exam_code: str
    exam_name: str
    name_tamil: str = ""
    stages: List[str] = field(default_factory=list)
    official_syllabus_pdf: str = ""
    last_updated: str = ""
    subjects: List[Subject] = field(default_factory=list)


@dataclass
class MappedContent:
    chunk_id: str
    exam_code: str
    subject_code: str
    topic_code: str
    subtopic_code: str
    confidence: float
    source: str
    matched_keywords: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS syllabus_coverage (
    user_id        TEXT NOT NULL,
    exam_code      TEXT NOT NULL,
    subject_code   TEXT NOT NULL,
    topic_code     TEXT NOT NULL,
    subtopic_code  TEXT NOT NULL DEFAULT '',
    hours_studied  REAL NOT NULL DEFAULT 0,
    questions_done INTEGER NOT NULL DEFAULT 0,
    last_touched   TEXT,
    PRIMARY KEY (user_id, exam_code, subject_code, topic_code, subtopic_code)
);

CREATE TABLE IF NOT EXISTS content_map (
    chunk_id       TEXT NOT NULL,
    exam_code      TEXT NOT NULL,
    subject_code   TEXT NOT NULL,
    topic_code     TEXT NOT NULL,
    subtopic_code  TEXT NOT NULL DEFAULT '',
    confidence     REAL NOT NULL DEFAULT 0.0,
    source         TEXT,
    matched_keywords TEXT,
    mapped_at      TEXT,
    PRIMARY KEY (chunk_id, exam_code, subject_code, topic_code, subtopic_code)
);

CREATE INDEX IF NOT EXISTS idx_cm_topic
    ON content_map(exam_code, topic_code);

CREATE TABLE IF NOT EXISTS question_topic_tags (
    question_id    TEXT NOT NULL,
    exam_code      TEXT NOT NULL,
    topic_code     TEXT NOT NULL,
    subtopic_code  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (question_id, exam_code, topic_code, subtopic_code)
);
"""


# ---------------------------------------------------------------------------
# Keyword tokeniser for content matching
# ---------------------------------------------------------------------------

_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "at", "and", "or", "to", "for",
    "is", "are", "was", "were", "be", "by", "with", "as", "from", "this",
    "that", "it", "its", "into",
}


def _tokenize(text: str) -> List[str]:
    if not text:
        return []
    tokens = re.findall(r"[A-Za-z\u0B80-\u0BFF]{3,}", text.lower())
    return [t for t in tokens if t not in _STOPWORDS]


# ---------------------------------------------------------------------------
# SyllabusManager
# ---------------------------------------------------------------------------


class SyllabusManager:
    def __init__(
        self,
        syllabus_dir: Optional[str] = None,
        db_path: Optional[str] = None,
    ):
        syl_cfg = getattr(settings, "syllabus", None)
        if syllabus_dir:
            self.syllabus_dir = Path(syllabus_dir)
        elif syl_cfg is not None:
            self.syllabus_dir = Path(syl_cfg.dir)
        else:
            self.syllabus_dir = Path(settings.data_dir) / "syllabi"
        if db_path:
            self.db_path = Path(db_path)
        elif syl_cfg is not None:
            self.db_path = Path(syl_cfg.db_path)
        else:
            self.db_path = Path(settings.data_dir) / "syllabus.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.syllabus_dir.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, Syllabus] = {}
        self._init_db()

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

    # -------- Loading & navigation ---------------------------------------

    def _normalise_exam_code(self, exam: str) -> str:
        if not exam:
            return ""
        key = exam.strip().upper().replace(" ", "_").replace("-", "_")
        if key in _EXAM_FILE_MAP:
            return key
        for known in _EXAM_FILE_MAP:
            if known in key or key in known:
                return known
        return key

    def _syllabus_file(self, exam: str) -> Optional[Path]:
        code = self._normalise_exam_code(exam)
        fname = _EXAM_FILE_MAP.get(code)
        if not fname:
            return None
        p = self.syllabus_dir / fname
        return p if p.exists() else None

    def load_syllabus(self, exam_type: str) -> Optional[Syllabus]:
        code = self._normalise_exam_code(exam_type)
        if code in self._cache:
            return self._cache[code]
        f = self._syllabus_file(exam_type)
        if not f:
            logger.warning(f"No syllabus JSON found for exam '{exam_type}'")
            return None
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Failed to parse {f}: {e}")
            return None

        subjects: List[Subject] = []
        for s in raw.get("subjects", []):
            topics: List[Topic] = []
            for t in s.get("topics", []):
                subtopics = [
                    Subtopic(
                        code=st.get("code", ""),
                        name=st.get("name", ""),
                        hours=float(st.get("hours", 1)),
                        priority=st.get("priority", "medium"),
                        frequently_asked=bool(st.get("frequently_asked", False)),
                    )
                    for st in t.get("subtopics", [])
                ]
                topics.append(Topic(
                    topic_code=t.get("topic_code", ""),
                    topic_name=t.get("topic_name", ""),
                    topic_name_tamil=t.get("topic_name_tamil", ""),
                    estimated_hours=float(t.get("estimated_hours", 0)),
                    priority=t.get("priority", "medium"),
                    subtopics=subtopics,
                ))
            subjects.append(Subject(
                subject_code=s.get("subject_code", ""),
                subject_name=s.get("subject_name", ""),
                subject_name_tamil=s.get("subject_name_tamil", ""),
                stage=s.get("stage", "Single"),
                weightage=float(s.get("weightage", 0)),
                topics=topics,
            ))

        syllabus = Syllabus(
            exam_code=raw.get("exam_code", code),
            exam_name=raw.get("exam_name", code),
            name_tamil=raw.get("name_tamil", ""),
            stages=list(raw.get("stages", [])),
            official_syllabus_pdf=raw.get("official_syllabus_pdf", ""),
            last_updated=raw.get("last_updated", ""),
            subjects=subjects,
        )
        self._cache[code] = syllabus
        return syllabus

    def list_exams(self) -> List[str]:
        return sorted(set(_EXAM_FILE_MAP.keys()))

    def get_subjects(self, exam_type: str) -> List[Subject]:
        syl = self.load_syllabus(exam_type)
        return list(syl.subjects) if syl else []

    def get_topics(self, exam_type: str, subject_code: str) -> List[Topic]:
        syl = self.load_syllabus(exam_type)
        if not syl:
            return []
        sc = subject_code.upper()
        for s in syl.subjects:
            if s.subject_code.upper() == sc or s.subject_name.upper() == sc:
                return list(s.topics)
        return []

    def find_topic(
        self, exam_type: str, query: str
    ) -> Optional[Tuple[Subject, Topic]]:
        syl = self.load_syllabus(exam_type)
        if not syl or not query:
            return None
        q = query.lower()
        best: Optional[Tuple[Subject, Topic, int]] = None
        for s in syl.subjects:
            for t in s.topics:
                score = 0
                name = t.topic_name.lower()
                if q == name:
                    score = 1000
                elif q in name or name in q:
                    score = 100
                else:
                    for tok in _tokenize(q):
                        if tok in name:
                            score += 10
                if t.topic_name_tamil and q in t.topic_name_tamil:
                    score += 500
                if score > 0 and (best is None or score > best[2]):
                    best = (s, t, score)
        return (best[0], best[1]) if best else None

    # -------- Voice navigation -------------------------------------------

    def syllabus_navigator(
        self, exam_type: str, language: str = "en"
    ) -> str:
        syl = self.load_syllabus(exam_type)
        if not syl:
            return f"Syllabus for {exam_type} is not available yet."

        lines: List[str] = []
        if language == "ta" and syl.name_tamil:
            lines.append(f"{syl.name_tamil} தேர்வு பாடத்திட்டம்:")
        else:
            lines.append(f"Syllabus for {syl.exam_name}.")
        lines.append(f"Stages: {', '.join(syl.stages) or 'Single stage'}.")
        lines.append(f"Total subjects: {len(syl.subjects)}.")
        for i, s in enumerate(syl.subjects, 1):
            topic_count = len(s.topics)
            lines.append(
                f"{i}. {s.subject_name} — {topic_count} topics, "
                f"stage {s.stage}, weightage {int(s.weightage) or 'NA'}."
            )
        lines.append(
            "Say 'topics in <subject>' to explore a subject, "
            "or 'priority topics' for the most important areas."
        )
        return " ".join(lines)

    def topics_voice_report(
        self, exam_type: str, subject_code: str
    ) -> str:
        topics = self.get_topics(exam_type, subject_code)
        if not topics:
            return f"No topics found for subject {subject_code} in {exam_type}."
        lines = [f"{subject_code} contains {len(topics)} topics."]
        for i, t in enumerate(topics[:10], 1):
            prio = t.priority.capitalize()
            hours = int(t.estimated_hours) or "a few"
            lines.append(
                f"{i}. {t.topic_name}. Priority {prio}. "
                f"Estimated {hours} study hours."
            )
        return " ".join(lines)

    # -------- Priority topics --------------------------------------------

    def priority_topics(
        self, exam_type: str, limit: int = 10
    ) -> List[Dict[str, Any]]:
        syl = self.load_syllabus(exam_type)
        if not syl:
            return []
        priority_score = {"high": 3, "medium": 2, "low": 1}
        out: List[Dict[str, Any]] = []
        for subj in syl.subjects:
            for t in subj.topics:
                freq = sum(1 for st in t.subtopics if st.frequently_asked)
                score = (
                    priority_score.get(t.priority, 1) * 10
                    + freq * 3
                    + (subj.weightage / 50.0 if subj.weightage else 0)
                )
                out.append({
                    "exam_code": syl.exam_code,
                    "subject_code": subj.subject_code,
                    "subject_name": subj.subject_name,
                    "topic_code": t.topic_code,
                    "topic_name": t.topic_name,
                    "priority": t.priority,
                    "frequently_asked_count": freq,
                    "estimated_hours": t.estimated_hours,
                    "score": round(score, 2),
                })
        out.sort(key=lambda x: x["score"], reverse=True)
        return out[:limit]

    def priority_topics_voice(self, exam_type: str, limit: int = 5) -> str:
        items = self.priority_topics(exam_type, limit=limit)
        if not items:
            return f"Could not find priority topics for {exam_type}."
        lines = [f"Top {len(items)} high-priority topics for {exam_type}:"]
        for i, it in enumerate(items, 1):
            lines.append(
                f"{i}. {it['topic_name']} in {it['subject_name']}. "
                f"Priority {it['priority']}. "
                f"Approximately {int(it['estimated_hours']) or 'a few'} study hours."
            )
        lines.append("Focus on these topics first for the best score impact.")
        return " ".join(lines)

    # -------- Content mapping --------------------------------------------

    def _build_topic_index(
        self, exam_type: str
    ) -> List[Tuple[Subject, Topic, Optional[Subtopic], List[str]]]:
        syl = self.load_syllabus(exam_type)
        if not syl:
            return []
        index = []
        for s in syl.subjects:
            for t in s.topics:
                keywords = _tokenize(t.topic_name) + _tokenize(t.topic_name_tamil)
                index.append((s, t, None, keywords))
                for st in t.subtopics:
                    sub_kw = _tokenize(st.name) + keywords
                    index.append((s, t, st, sub_kw))
        return index

    def content_mapper(
        self,
        chunks: List[Any],
        exam: str,
        subject: Optional[str] = None,
        min_confidence: float = 0.15,
        persist: bool = True,
    ) -> List[MappedContent]:
        """
        Tag RAG chunks to syllabus topics/subtopics.
        Accepts Chunk objects (with .text, .chunk_id, .metadata) or dicts.
        Returns list of MappedContent entries.
        """
        index = self._build_topic_index(exam)
        if not index:
            return []

        results: List[MappedContent] = []
        now = datetime.utcnow().isoformat()

        for ch in chunks:
            text = getattr(ch, "text", None) or (ch.get("text") if isinstance(ch, dict) else "")
            chunk_id = (
                getattr(ch, "chunk_id", None)
                or (ch.get("chunk_id") if isinstance(ch, dict) else None)
                or (ch.get("id") if isinstance(ch, dict) else None)
                or str(uuid.uuid4())
            )
            meta = getattr(ch, "metadata", None) or (ch.get("metadata") if isinstance(ch, dict) else {}) or {}
            source = meta.get("source", "")
            tokens = set(_tokenize(text))
            if not tokens:
                continue

            best: Optional[Tuple[float, Subject, Topic, Optional[Subtopic], List[str]]] = None
            for s, t, st, kw in index:
                if subject and s.subject_code.upper() != subject.upper() and s.subject_name.upper() != subject.upper():
                    continue
                if not kw:
                    continue
                matched = [k for k in kw if k in tokens]
                if not matched:
                    continue
                score = len(matched) / max(len(set(kw)), 1)
                if best is None or score > best[0]:
                    best = (score, s, t, st, matched)

            if best and best[0] >= min_confidence:
                score, s, t, st, matched = best
                mc = MappedContent(
                    chunk_id=chunk_id,
                    exam_code=self._normalise_exam_code(exam),
                    subject_code=s.subject_code,
                    topic_code=t.topic_code,
                    subtopic_code=st.code if st else "",
                    confidence=round(score, 3),
                    source=source,
                    matched_keywords=matched[:8],
                )
                results.append(mc)

        if persist and results:
            self._save_mappings(results, now)
        return results

    def _save_mappings(self, results: List[MappedContent], ts: str) -> None:
        rows = [
            (
                r.chunk_id, r.exam_code, r.subject_code, r.topic_code,
                r.subtopic_code, r.confidence, r.source,
                ",".join(r.matched_keywords), ts,
            )
            for r in results
        ]
        with self._conn() as c:
            c.executemany(
                """
                INSERT OR REPLACE INTO content_map
                (chunk_id, exam_code, subject_code, topic_code, subtopic_code,
                 confidence, source, matched_keywords, mapped_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    # -------- Question tagging -------------------------------------------

    def tag_question_to_topic(
        self,
        question_id: str,
        exam_type: str,
        topic_code: str,
        subtopic_code: str = "",
    ) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO question_topic_tags
                (question_id, exam_code, topic_code, subtopic_code)
                VALUES (?, ?, ?, ?)
                """,
                (question_id, self._normalise_exam_code(exam_type),
                 topic_code, subtopic_code),
            )

    # -------- Coverage tracking ------------------------------------------

    def record_study(
        self,
        user_id: str,
        exam_type: str,
        subject_code: str,
        topic_code: str,
        subtopic_code: str = "",
        hours: float = 0.0,
        questions_done: int = 0,
    ) -> None:
        now = datetime.utcnow().isoformat()
        exam_code = self._normalise_exam_code(exam_type)
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO syllabus_coverage
                (user_id, exam_code, subject_code, topic_code, subtopic_code,
                 hours_studied, questions_done, last_touched)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, exam_code, subject_code, topic_code, subtopic_code)
                DO UPDATE SET
                    hours_studied = hours_studied + excluded.hours_studied,
                    questions_done = questions_done + excluded.questions_done,
                    last_touched = excluded.last_touched
                """,
                (user_id, exam_code, subject_code, topic_code, subtopic_code,
                 float(hours), int(questions_done), now),
            )

    def coverage_tracker(
        self,
        user_id: str,
        exam: str,
    ) -> Dict[str, Any]:
        syl = self.load_syllabus(exam)
        if not syl:
            return {"exam_code": exam, "coverage_pct": 0.0, "subjects": []}

        exam_code = self._normalise_exam_code(exam)
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT subject_code, topic_code, subtopic_code,
                       hours_studied, questions_done, last_touched
                FROM syllabus_coverage
                WHERE user_id=? AND exam_code=?
                """,
                (user_id, exam_code),
            ).fetchall()

        study_map: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for r in rows:
            study_map[(r["subject_code"], r["topic_code"], r["subtopic_code"] or "")] = {
                "hours_studied": r["hours_studied"],
                "questions_done": r["questions_done"],
                "last_touched": r["last_touched"],
            }

        subjects_out: List[Dict[str, Any]] = []
        total_planned = 0.0
        total_done = 0.0

        for subj in syl.subjects:
            subj_planned = 0.0
            subj_done = 0.0
            topics_out: List[Dict[str, Any]] = []
            for t in subj.topics:
                planned = float(t.estimated_hours) or sum(st.hours for st in t.subtopics) or 1.0
                studied_info = study_map.get((subj.subject_code, t.topic_code, ""), {})
                topic_hours = float(studied_info.get("hours_studied", 0.0))
                for st in t.subtopics:
                    sinfo = study_map.get((subj.subject_code, t.topic_code, st.code), {})
                    topic_hours += float(sinfo.get("hours_studied", 0.0))
                pct = min(100.0, (topic_hours / planned) * 100.0) if planned > 0 else 0.0
                topics_out.append({
                    "topic_code": t.topic_code,
                    "topic_name": t.topic_name,
                    "planned_hours": planned,
                    "hours_studied": round(topic_hours, 2),
                    "coverage_pct": round(pct, 1),
                    "priority": t.priority,
                })
                subj_planned += planned
                subj_done += min(topic_hours, planned)

            subj_pct = (subj_done / subj_planned * 100.0) if subj_planned > 0 else 0.0
            subjects_out.append({
                "subject_code": subj.subject_code,
                "subject_name": subj.subject_name,
                "planned_hours": round(subj_planned, 2),
                "hours_studied": round(subj_done, 2),
                "coverage_pct": round(subj_pct, 1),
                "topics": topics_out,
            })
            total_planned += subj_planned
            total_done += subj_done

        total_pct = (total_done / total_planned * 100.0) if total_planned > 0 else 0.0
        return {
            "exam_code": exam_code,
            "exam_name": syl.exam_name,
            "total_planned_hours": round(total_planned, 2),
            "total_hours_studied": round(total_done, 2),
            "coverage_pct": round(total_pct, 1),
            "subjects": subjects_out,
        }

    def coverage_voice_report(self, user_id: str, exam: str) -> str:
        data = self.coverage_tracker(user_id, exam)
        if not data.get("subjects"):
            return f"No coverage data yet for {exam}. Start studying to see progress."
        lines = [
            f"Syllabus coverage for {data['exam_name']}: "
            f"{data['coverage_pct']:.0f} percent complete. "
            f"You have studied {data['total_hours_studied']:.1f} out of "
            f"{data['total_planned_hours']:.0f} planned hours."
        ]
        top_subjects = sorted(
            data["subjects"], key=lambda s: s["coverage_pct"], reverse=True
        )[:3]
        low_subjects = sorted(
            [s for s in data["subjects"] if s["coverage_pct"] < 40],
            key=lambda s: s["coverage_pct"],
        )[:3]
        if top_subjects:
            lines.append("Strongest areas: " + ", ".join(
                f"{s['subject_name']} at {s['coverage_pct']:.0f} percent"
                for s in top_subjects
            ) + ".")
        if low_subjects:
            lines.append("Needs attention: " + ", ".join(
                f"{s['subject_name']} at {s['coverage_pct']:.0f} percent"
                for s in low_subjects
            ) + ".")
        return " ".join(lines)

    # -------- PDF metadata extraction ------------------------------------

    def extract_syllabus_pdf_metadata(self, pdf_path: str) -> Dict[str, Any]:
        p = Path(pdf_path)
        if not p.exists():
            return {"error": f"file not found: {pdf_path}"}
        meta: Dict[str, Any] = {
            "path": str(p),
            "file_size_bytes": p.stat().st_size,
            "filename": p.name,
        }
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(str(p))
            meta["page_count"] = doc.page_count
            meta["metadata"] = dict(doc.metadata or {})
            toc = doc.get_toc() or []
            meta["toc"] = [
                {"level": lvl, "title": title, "page": pg}
                for lvl, title, pg in toc
            ]
            text_parts: List[str] = []
            for i in range(min(3, doc.page_count)):
                text_parts.append(doc.load_page(i).get_text())
            sample_text = "\n".join(text_parts)
            meta["sample_text"] = sample_text[:2000]
            meta["detected_sections"] = self._detect_syllabus_sections(
                sample_text + "\n" + "\n".join(
                    doc.load_page(i).get_text()
                    for i in range(doc.page_count)
                )
            )
            doc.close()
        except ImportError:
            meta["error"] = "PyMuPDF (fitz) not installed"
        except Exception as e:
            meta["error"] = str(e)
        return meta

    def _detect_syllabus_sections(self, text: str) -> List[str]:
        patterns = [
            r"(?i)^\s*unit\s+[ivx\d]+[:.\-]\s*(.+)$",
            r"(?i)^\s*chapter\s+\d+[:.\-]\s*(.+)$",
            r"(?i)^\s*paper\s+[ivx\d]+[:.\-]\s*(.+)$",
            r"(?i)^\s*\d+\.\s+([A-Z][^\n]{5,80})$",
        ]
        seen: List[str] = []
        for line in text.splitlines():
            for pat in patterns:
                m = re.match(pat, line.strip())
                if m:
                    val = m.group(1).strip()
                    if val and val not in seen:
                        seen.append(val)
                    break
        return seen[:60]


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------

_default_manager: Optional[SyllabusManager] = None


def _get_default() -> SyllabusManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = SyllabusManager()
    return _default_manager


def content_mapper(
    uploaded_doc: List[Any],
    exam: str,
    subject: Optional[str] = None,
) -> List[MappedContent]:
    return _get_default().content_mapper(uploaded_doc, exam=exam, subject=subject)


def syllabus_navigator(exam_type: str, language: str = "en") -> str:
    return _get_default().syllabus_navigator(exam_type, language=language)


def coverage_tracker(user_id: str, exam: str) -> Dict[str, Any]:
    return _get_default().coverage_tracker(user_id, exam)


def priority_topics(exam_type: str, limit: int = 10) -> List[Dict[str, Any]]:
    return _get_default().priority_topics(exam_type, limit=limit)


def extract_syllabus_pdf_metadata(pdf_path: str) -> Dict[str, Any]:
    return _get_default().extract_syllabus_pdf_metadata(pdf_path)
