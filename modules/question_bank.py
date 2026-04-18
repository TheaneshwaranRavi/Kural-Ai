import json
import logging
import random
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Question:
    question_id: str
    text: str
    option_a: str
    option_b: str
    option_c: str
    option_d: str
    correct_answer: str
    explanation: str
    subject: str
    exam_type: str
    difficulty: str = "medium"
    topic: str = "General"
    year: Optional[int] = None
    language: str = "en"
    text_tamil: str = ""
    option_a_tamil: str = ""
    option_b_tamil: str = ""
    option_c_tamil: str = ""
    option_d_tamil: str = ""
    explanation_tamil: str = ""


@dataclass
class AnsweredQuestion:
    question: Question
    user_answer: Optional[str]
    is_correct: bool
    time_taken: float
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    mode: str = "random"


@dataclass
class UserPerformance:
    user_id: str
    subject: str
    topic: str
    exam_type: str
    difficulty: str
    total_attempted: int
    correct_count: int
    last_updated: str

    @property
    def accuracy(self) -> float:
        if self.total_attempted == 0:
            return 0.0
        return self.correct_count / self.total_attempted


# ---------------------------------------------------------------------------
# Database schema SQL
# ---------------------------------------------------------------------------

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS questions (
    question_id   TEXT PRIMARY KEY,
    text          TEXT NOT NULL,
    option_a      TEXT NOT NULL,
    option_b      TEXT NOT NULL,
    option_c      TEXT NOT NULL,
    option_d      TEXT NOT NULL,
    correct_answer TEXT NOT NULL,
    explanation   TEXT DEFAULT '',
    subject       TEXT NOT NULL,
    exam_type     TEXT NOT NULL,
    difficulty    TEXT DEFAULT 'medium',
    topic         TEXT DEFAULT 'General',
    year          INTEGER,
    language      TEXT DEFAULT 'en',
    text_tamil        TEXT DEFAULT '',
    option_a_tamil    TEXT DEFAULT '',
    option_b_tamil    TEXT DEFAULT '',
    option_c_tamil    TEXT DEFAULT '',
    option_d_tamil    TEXT DEFAULT '',
    explanation_tamil TEXT DEFAULT '',
    created_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_q_exam_subject
    ON questions(exam_type, subject, difficulty);
CREATE INDEX IF NOT EXISTS idx_q_topic
    ON questions(topic);
CREATE INDEX IF NOT EXISTS idx_q_year
    ON questions(year);

CREATE TABLE IF NOT EXISTS practice_history (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id              TEXT NOT NULL,
    session_id           TEXT NOT NULL,
    question_id          TEXT NOT NULL,
    user_answer          TEXT,
    is_correct           INTEGER,
    time_taken_seconds   REAL,
    timestamp            TEXT,
    exam_type            TEXT,
    subject              TEXT,
    topic                TEXT,
    difficulty           TEXT,
    mode                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_ph_user_session
    ON practice_history(user_id, session_id);
CREATE INDEX IF NOT EXISTS idx_ph_user_topic
    ON practice_history(user_id, exam_type, subject, topic);

CREATE TABLE IF NOT EXISTS user_performance (
    user_id         TEXT NOT NULL,
    subject         TEXT NOT NULL,
    topic           TEXT NOT NULL,
    exam_type       TEXT NOT NULL,
    difficulty      TEXT NOT NULL,
    total_attempted INTEGER DEFAULT 0,
    correct_count   INTEGER DEFAULT 0,
    last_updated    TEXT,
    PRIMARY KEY (user_id, subject, topic, exam_type, difficulty)
);
"""

# ---------------------------------------------------------------------------
# Sample questions (pre-seeded so the system works out of the box)
# ---------------------------------------------------------------------------

_SAMPLE_QUESTIONS: List[Dict[str, Any]] = [
    {
        "text": "Who was the first Chief Minister of Tamil Nadu (Madras State)?",
        "option_a": "C. Rajagopalachari",
        "option_b": "K. Kamaraj",
        "option_c": "M. Bhaktavatsalam",
        "option_d": "C.N. Annadurai",
        "correct_answer": "A",
        "explanation": (
            "C. Rajagopalachari, popularly known as Rajaji, served as the first "
            "Chief Minister of Madras State from 1952 to 1954. He was a close "
            "associate of Mahatma Gandhi and a key figure in the Indian independence movement."
        ),
        "subject": "History",
        "exam_type": "TNPSC",
        "difficulty": "medium",
        "topic": "Tamil Nadu Political History",
    },
    {
        "text": "In which year was the Panchayati Raj Act enacted in Tamil Nadu?",
        "option_a": "1994",
        "option_b": "1996",
        "option_c": "1998",
        "option_d": "2000",
        "correct_answer": "A",
        "explanation": (
            "The Tamil Nadu Panchayats Act was enacted in 1994, in accordance with "
            "the 73rd Constitutional Amendment. This gave constitutional status to "
            "panchayats and established the three-tier system of local governance."
        ),
        "subject": "Polity",
        "exam_type": "TNPSC",
        "difficulty": "medium",
        "topic": "Local Self-Government",
    },
    {
        "text": "Which river is known as the 'Ganges of the South' in Tamil Nadu?",
        "option_a": "Vaigai",
        "option_b": "Tamiraparani",
        "option_c": "Kaveri",
        "option_d": "Palar",
        "correct_answer": "C",
        "explanation": (
            "The Kaveri River is often called the 'Ganges of the South' due to its "
            "religious and cultural significance in Tamil Nadu and Karnataka. "
            "It originates in the Brahmagiri Hills of Coorg and flows through Tamil Nadu "
            "before joining the Bay of Bengal."
        ),
        "subject": "Geography",
        "exam_type": "TNPSC",
        "difficulty": "beginner",
        "topic": "Tamil Nadu Geography",
    },
    {
        "text": "What is the capital of Tamil Nadu?",
        "option_a": "Madurai",
        "option_b": "Coimbatore",
        "option_c": "Chennai",
        "option_d": "Tiruchirappalli",
        "correct_answer": "C",
        "explanation": (
            "Chennai, formerly known as Madras, is the capital of Tamil Nadu. "
            "It is also the largest city in Tamil Nadu and the fourth largest city in India. "
            "Chennai is located on the Coromandel Coast of the Bay of Bengal."
        ),
        "subject": "Geography",
        "exam_type": "TNPSC",
        "difficulty": "beginner",
        "topic": "Tamil Nadu Geography",
    },
    {
        "text": "Which article of the Indian Constitution deals with the Right to Education?",
        "option_a": "Article 19",
        "option_b": "Article 21A",
        "option_c": "Article 24",
        "option_d": "Article 32",
        "correct_answer": "B",
        "explanation": (
            "Article 21A, inserted by the 86th Constitutional Amendment Act of 2002, "
            "makes free and compulsory education a fundamental right for all children "
            "between 6 and 14 years of age. Option A deals with freedom of speech, "
            "Article 24 prohibits child labour, and Article 32 is the right to constitutional remedies."
        ),
        "subject": "Polity",
        "exam_type": "TNPSC",
        "difficulty": "medium",
        "topic": "Fundamental Rights",
    },
    {
        "text": "The 'Green Revolution' in India is primarily associated with which crop?",
        "option_a": "Cotton",
        "option_b": "Wheat",
        "option_c": "Rice",
        "option_d": "Sugarcane",
        "correct_answer": "B",
        "explanation": (
            "The Green Revolution in India during the 1960s and 1970s is primarily "
            "associated with wheat. Dr. M.S. Swaminathan introduced high-yielding varieties "
            "of wheat, dramatically increasing production especially in Punjab, Haryana, "
            "and western Uttar Pradesh."
        ),
        "subject": "Economy",
        "exam_type": "TNPSC",
        "difficulty": "medium",
        "topic": "Indian Agriculture",
    },
    {
        "text": "Who founded the Dravidar Kazhagam (DK) party?",
        "option_a": "E.V. Ramasamy Periyar",
        "option_b": "C.N. Annadurai",
        "option_c": "M. Karunanidhi",
        "option_d": "K. Kamaraj",
        "correct_answer": "A",
        "explanation": (
            "E.V. Ramasamy Periyar, known as 'Periyar', founded the Dravidar Kazhagam "
            "in 1944. He was a prominent social reformer who fought against caste discrimination "
            "and Brahminical dominance. C.N. Annadurai later broke away to form the DMK in 1949."
        ),
        "subject": "History",
        "exam_type": "TNPSC",
        "difficulty": "medium",
        "topic": "Tamil Nadu Political History",
    },
    {
        "text": "Which is the largest district by area in Tamil Nadu?",
        "option_a": "Coimbatore",
        "option_b": "Villupuram",
        "option_c": "Dindigul",
        "option_d": "Vellore",
        "correct_answer": "A",
        "explanation": (
            "Coimbatore is the largest district in Tamil Nadu by area, covering approximately "
            "7,469 square kilometres. It is also an important industrial city known as the "
            "'Manchester of South India' due to its textile industry."
        ),
        "subject": "Geography",
        "exam_type": "TNPSC",
        "difficulty": "advanced",
        "topic": "Tamil Nadu Geography",
    },
    {
        "text": "What does 'GDP' stand for?",
        "option_a": "Gross Domestic Product",
        "option_b": "Gross Development Plan",
        "option_c": "General Domestic Production",
        "option_d": "Government Development Programme",
        "correct_answer": "A",
        "explanation": (
            "GDP stands for Gross Domestic Product. It is the total monetary value of all "
            "goods and services produced within a country's borders in a specific time period. "
            "It is the most widely used measure of an economy's size and health."
        ),
        "subject": "Economy",
        "exam_type": "TNPSC",
        "difficulty": "beginner",
        "topic": "Basic Economics",
    },
    {
        "text": "Which is the hardest natural substance known?",
        "option_a": "Quartz",
        "option_b": "Iron",
        "option_c": "Diamond",
        "option_d": "Graphite",
        "correct_answer": "C",
        "explanation": (
            "Diamond is the hardest natural substance known, scoring 10 on the Mohs hardness scale. "
            "Both diamond and graphite are allotropes of carbon, but diamond's crystal structure "
            "makes it extremely hard while graphite is soft and slippery."
        ),
        "subject": "Science",
        "exam_type": "TNPSC",
        "difficulty": "beginner",
        "topic": "General Science",
    },
    {
        "text": "The Reserve Bank of India was established in which year?",
        "option_a": "1935",
        "option_b": "1947",
        "option_c": "1949",
        "option_d": "1952",
        "correct_answer": "A",
        "explanation": (
            "The Reserve Bank of India was established on April 1, 1935, under the "
            "Reserve Bank of India Act, 1934. It was nationalized in 1949. "
            "The RBI acts as the central bank and regulates the monetary policy of India."
        ),
        "subject": "Economy",
        "exam_type": "Banking",
        "difficulty": "medium",
        "topic": "Banking History",
    },
    {
        "text": "Which article of the Indian Constitution abolishes untouchability?",
        "option_a": "Article 14",
        "option_b": "Article 15",
        "option_c": "Article 17",
        "option_d": "Article 18",
        "correct_answer": "C",
        "explanation": (
            "Article 17 of the Indian Constitution abolishes untouchability and forbids "
            "its practice in any form. Article 14 deals with equality before law, "
            "Article 15 prohibits discrimination, and Article 18 abolishes titles."
        ),
        "subject": "Polity",
        "exam_type": "TNPSC",
        "difficulty": "advanced",
        "topic": "Fundamental Rights",
    },
    {
        "text": "What is the minimum age requirement to become a member of the Rajya Sabha?",
        "option_a": "21 years",
        "option_b": "25 years",
        "option_c": "30 years",
        "option_d": "35 years",
        "correct_answer": "C",
        "explanation": (
            "The minimum age to become a member of the Rajya Sabha (Upper House) is 30 years. "
            "For the Lok Sabha (Lower House), the minimum age is 25 years. "
            "For the President of India, the minimum age is 35 years."
        ),
        "subject": "Polity",
        "exam_type": "TNPSC",
        "difficulty": "medium",
        "topic": "Parliament",
    },
    {
        "text": "Which gas is primarily responsible for the greenhouse effect?",
        "option_a": "Oxygen",
        "option_b": "Nitrogen",
        "option_c": "Carbon Dioxide",
        "option_d": "Hydrogen",
        "correct_answer": "C",
        "explanation": (
            "Carbon dioxide (CO2) is the primary greenhouse gas responsible for global warming. "
            "It traps heat in the atmosphere. Other greenhouse gases include methane, "
            "nitrous oxide, and water vapour. Burning fossil fuels is the main source of CO2."
        ),
        "subject": "Science",
        "exam_type": "TNPSC",
        "difficulty": "beginner",
        "topic": "Environment",
    },
    {
        "text": "In banking, what does 'NPA' stand for?",
        "option_a": "New Private Account",
        "option_b": "Non-Performing Asset",
        "option_c": "National Payment Authority",
        "option_d": "Net Profit Adjustment",
        "correct_answer": "B",
        "explanation": (
            "NPA stands for Non-Performing Asset. A loan or advance is classified as NPA "
            "when interest or principal payment remains overdue for more than 90 days. "
            "High NPA levels indicate stress in a bank's loan portfolio."
        ),
        "subject": "Economy",
        "exam_type": "Banking",
        "difficulty": "medium",
        "topic": "Banking Terminology",
    },
]


# ---------------------------------------------------------------------------
# QuestionBank
# ---------------------------------------------------------------------------


class QuestionBank:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or settings.practice.db_path
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_DDL)
        if self._count_questions() == 0:
            logger.info("Empty question bank — seeding sample questions")
            self.bulk_insert_questions(_SAMPLE_QUESTIONS)

    def _count_questions(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM questions").fetchone()
            return row[0]

    # ------------------------------------------------------------------
    # Question CRUD
    # ------------------------------------------------------------------

    def add_question(self, q: Dict[str, Any]) -> str:
        qid = q.get("question_id") or str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO questions
                  (question_id, text, option_a, option_b, option_c, option_d,
                   correct_answer, explanation, subject, exam_type, difficulty,
                   topic, year, language,
                   text_tamil, option_a_tamil, option_b_tamil,
                   option_c_tamil, option_d_tamil, explanation_tamil, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    qid,
                    q["text"],
                    q["option_a"],
                    q["option_b"],
                    q["option_c"],
                    q["option_d"],
                    q["correct_answer"].upper(),
                    q.get("explanation", ""),
                    q["subject"],
                    q["exam_type"],
                    q.get("difficulty", "medium"),
                    q.get("topic", "General"),
                    q.get("year"),
                    q.get("language", "en"),
                    q.get("text_tamil", ""),
                    q.get("option_a_tamil", ""),
                    q.get("option_b_tamil", ""),
                    q.get("option_c_tamil", ""),
                    q.get("option_d_tamil", ""),
                    q.get("explanation_tamil", ""),
                    now,
                ),
            )
        return qid

    def bulk_insert_questions(self, questions: List[Dict[str, Any]]) -> int:
        inserted = 0
        for q in questions:
            try:
                self.add_question(q)
                inserted += 1
            except Exception as e:
                logger.warning(f"Skipped question: {e}")
        logger.info(f"Bulk insert: {inserted}/{len(questions)} questions")
        return inserted

    def import_from_json(self, file_path: str) -> int:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"JSON file not found: {file_path}")
        with open(path, encoding="utf-8") as f:
            questions = json.load(f)
        return self.bulk_insert_questions(questions)

    def get_question(self, question_id: str) -> Optional[Question]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM questions WHERE question_id = ?", (question_id,)
            ).fetchone()
        return self._row_to_question(row) if row else None

    def load_questions(
        self,
        subject: Optional[str] = None,
        exam_type: Optional[str] = None,
        difficulty: Optional[str] = None,
        topic: Optional[str] = None,
        year: Optional[int] = None,
        language: Optional[str] = None,
        limit: Optional[int] = None,
        shuffle: bool = True,
    ) -> List[Question]:
        clauses: List[str] = []
        params: List[Any] = []

        if exam_type:
            clauses.append("exam_type = ?")
            params.append(exam_type)
        if subject:
            clauses.append("subject = ?")
            params.append(subject)
        if difficulty:
            clauses.append("difficulty = ?")
            params.append(difficulty)
        if topic:
            clauses.append("topic = ?")
            params.append(topic)
        if year:
            clauses.append("year = ?")
            params.append(year)
        if language:
            clauses.append("language = ?")
            params.append(language)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM questions {where}"
        if limit and not shuffle:
            sql += f" LIMIT {int(limit)}"

        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()

        questions = [self._row_to_question(r) for r in rows]
        if shuffle:
            random.shuffle(questions)
        if limit:
            questions = questions[:limit]
        return questions

    def get_subjects(self, exam_type: Optional[str] = None) -> List[str]:
        sql = "SELECT DISTINCT subject FROM questions"
        params = []
        if exam_type:
            sql += " WHERE exam_type = ?"
            params.append(exam_type)
        sql += " ORDER BY subject"
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [r[0] for r in rows]

    def get_topics(
        self, subject: Optional[str] = None, exam_type: Optional[str] = None
    ) -> List[str]:
        clauses, params = [], []
        if subject:
            clauses.append("subject = ?")
            params.append(subject)
        if exam_type:
            clauses.append("exam_type = ?")
            params.append(exam_type)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT topic FROM questions {where} ORDER BY topic", params
            ).fetchall()
        return [r[0] for r in rows]

    def get_years(self, exam_type: Optional[str] = None) -> List[int]:
        sql = "SELECT DISTINCT year FROM questions WHERE year IS NOT NULL"
        params = []
        if exam_type:
            sql += " AND exam_type = ?"
            params.append(exam_type)
        sql += " ORDER BY year DESC"
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # Practice history
    # ------------------------------------------------------------------

    def save_practice_history(
        self,
        user_id: str,
        session_id: str,
        answered: List[AnsweredQuestion],
        mode: str = "random",
    ) -> None:
        records = []
        for aq in answered:
            records.append(
                (
                    user_id,
                    session_id,
                    aq.question.question_id,
                    aq.user_answer,
                    1 if aq.is_correct else 0,
                    aq.time_taken,
                    aq.timestamp,
                    aq.question.exam_type,
                    aq.question.subject,
                    aq.question.topic,
                    aq.question.difficulty,
                    mode,
                )
            )
        with self._conn() as conn:
            conn.executemany(
                """INSERT INTO practice_history
                   (user_id, session_id, question_id, user_answer, is_correct,
                    time_taken_seconds, timestamp, exam_type, subject, topic, difficulty, mode)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                records,
            )
        self._update_performance(user_id, answered)
        logger.info(f"Saved {len(records)} history records for session {session_id}")

    def _update_performance(
        self, user_id: str, answered: List[AnsweredQuestion]
    ) -> None:
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            for aq in answered:
                q = aq.question
                conn.execute(
                    """INSERT INTO user_performance
                       (user_id, subject, topic, exam_type, difficulty,
                        total_attempted, correct_count, last_updated)
                       VALUES (?,?,?,?,?,1,?,?)
                       ON CONFLICT(user_id, subject, topic, exam_type, difficulty)
                       DO UPDATE SET
                         total_attempted = total_attempted + 1,
                         correct_count   = correct_count + excluded.correct_count,
                         last_updated    = excluded.last_updated""",
                    (
                        user_id,
                        q.subject,
                        q.topic,
                        q.exam_type,
                        q.difficulty,
                        1 if aq.is_correct else 0,
                        now,
                    ),
                )

    # ------------------------------------------------------------------
    # Performance analytics
    # ------------------------------------------------------------------

    def get_user_performance(
        self,
        user_id: str,
        exam_type: Optional[str] = None,
    ) -> List[UserPerformance]:
        sql = "SELECT * FROM user_performance WHERE user_id = ?"
        params: List[Any] = [user_id]
        if exam_type:
            sql += " AND exam_type = ?"
            params.append(exam_type)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            UserPerformance(
                user_id=r["user_id"],
                subject=r["subject"],
                topic=r["topic"],
                exam_type=r["exam_type"],
                difficulty=r["difficulty"],
                total_attempted=r["total_attempted"],
                correct_count=r["correct_count"],
                last_updated=r["last_updated"],
            )
            for r in rows
        ]

    def get_recommended_difficulty(
        self, user_id: str, subject: str, topic: str, exam_type: str
    ) -> str:
        cfg = settings.practice
        difficulties = ["beginner", "medium", "advanced"]
        best = "medium"
        for diff in difficulties:
            with self._conn() as conn:
                row = conn.execute(
                    """SELECT total_attempted, correct_count
                       FROM user_performance
                       WHERE user_id=? AND subject=? AND topic=? AND exam_type=? AND difficulty=?""",
                    (user_id, subject, topic, exam_type, diff),
                ).fetchone()
            if not row or row["total_attempted"] < cfg.adaptive_min_attempts:
                best = diff
                break
            acc = row["correct_count"] / row["total_attempted"]
            if acc >= cfg.adaptive_promote_threshold:
                idx = difficulties.index(diff)
                best = difficulties[min(idx + 1, len(difficulties) - 1)]
            elif acc <= cfg.adaptive_demote_threshold:
                idx = difficulties.index(diff)
                best = difficulties[max(idx - 1, 0)]
            else:
                best = diff
                break
        logger.debug(f"Recommended difficulty for {subject}/{topic}: {best}")
        return best

    def identify_weak_topics(
        self, user_id: str, exam_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        threshold = settings.practice.weak_topic_threshold
        perfs = self.get_user_performance(user_id, exam_type=exam_type)
        weak = []
        for p in perfs:
            if (
                p.total_attempted >= settings.practice.adaptive_min_attempts
                and p.accuracy < threshold
            ):
                weak.append(
                    {
                        "subject": p.subject,
                        "topic": p.topic,
                        "difficulty": p.difficulty,
                        "accuracy": round(p.accuracy * 100, 1),
                        "attempted": p.total_attempted,
                    }
                )
        weak.sort(key=lambda x: x["accuracy"])
        return weak

    def calculate_score(
        self, answered: List[AnsweredQuestion]
    ) -> Dict[str, Any]:
        if not answered:
            return {
                "total": 0, "correct": 0, "wrong": 0, "skipped": 0,
                "percentage": 0.0, "topic_breakdown": {}, "weak_topics": [],
                "strong_topics": [], "grade": "N/A", "time_taken": 0.0,
            }

        total = len(answered)
        correct = sum(1 for a in answered if a.is_correct)
        skipped = sum(1 for a in answered if a.user_answer is None)
        wrong = total - correct - skipped
        percentage = round((correct / total) * 100, 1) if total else 0.0
        time_taken = sum(a.time_taken for a in answered)

        topic_breakdown: Dict[str, Dict[str, Any]] = {}
        for aq in answered:
            key = f"{aq.question.subject} — {aq.question.topic}"
            if key not in topic_breakdown:
                topic_breakdown[key] = {"attempted": 0, "correct": 0, "percentage": 0.0}
            topic_breakdown[key]["attempted"] += 1
            if aq.is_correct:
                topic_breakdown[key]["correct"] += 1
        for key, val in topic_breakdown.items():
            val["percentage"] = round(
                (val["correct"] / val["attempted"]) * 100, 1
            )

        threshold = settings.practice.weak_topic_threshold * 100
        weak_topics = [k for k, v in topic_breakdown.items() if v["percentage"] < threshold]
        strong_topics = [k for k, v in topic_breakdown.items() if v["percentage"] >= 80.0]

        grade = self._letter_grade(percentage)

        return {
            "total": total,
            "correct": correct,
            "wrong": wrong,
            "skipped": skipped,
            "percentage": percentage,
            "topic_breakdown": topic_breakdown,
            "weak_topics": weak_topics,
            "strong_topics": strong_topics,
            "grade": grade,
            "time_taken": round(time_taken, 1),
        }

    @staticmethod
    def _letter_grade(pct: float) -> str:
        if pct >= 90:
            return "A+"
        if pct >= 80:
            return "A"
        if pct >= 70:
            return "B"
        if pct >= 60:
            return "C"
        if pct >= 50:
            return "D"
        return "F"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_question(row: sqlite3.Row) -> Question:
        return Question(
            question_id=row["question_id"],
            text=row["text"],
            option_a=row["option_a"],
            option_b=row["option_b"],
            option_c=row["option_c"],
            option_d=row["option_d"],
            correct_answer=row["correct_answer"],
            explanation=row["explanation"],
            subject=row["subject"],
            exam_type=row["exam_type"],
            difficulty=row["difficulty"],
            topic=row["topic"],
            year=row["year"],
            language=row["language"],
            text_tamil=row["text_tamil"],
            option_a_tamil=row["option_a_tamil"],
            option_b_tamil=row["option_b_tamil"],
            option_c_tamil=row["option_c_tamil"],
            option_d_tamil=row["option_d_tamil"],
            explanation_tamil=row["explanation_tamil"],
        )


# ---------------------------------------------------------------------------
# Module-level standalone functions
# ---------------------------------------------------------------------------

_default_bank: Optional[QuestionBank] = None


def _get_bank() -> QuestionBank:
    global _default_bank
    if _default_bank is None:
        _default_bank = QuestionBank()
    return _default_bank


def load_questions(
    subject: Optional[str] = None,
    exam_type: Optional[str] = None,
    difficulty: Optional[str] = None,
    topic: Optional[str] = None,
    year: Optional[int] = None,
    limit: Optional[int] = None,
    shuffle: bool = True,
) -> List[Question]:
    return _get_bank().load_questions(
        subject=subject,
        exam_type=exam_type,
        difficulty=difficulty,
        topic=topic,
        year=year,
        limit=limit,
        shuffle=shuffle,
    )


def save_practice_history(
    user_id: str,
    questions_attempted: List[AnsweredQuestion],
    session_id: Optional[str] = None,
    mode: str = "random",
) -> None:
    sid = session_id or str(uuid.uuid4())
    _get_bank().save_practice_history(user_id, sid, questions_attempted, mode)


def calculate_score(answered_questions: List[AnsweredQuestion]) -> Dict[str, Any]:
    return _get_bank().calculate_score(answered_questions)
