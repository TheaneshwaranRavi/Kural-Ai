import json
import logging
import math
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Spaced-repetition interval table (days after each revision)
# ---------------------------------------------------------------------------
_SR_INTERVALS = [1, 3, 7, 14, 30, 60, 120]

# Weak-area accuracy threshold
_WEAK_THRESHOLD = 0.60

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    user_id              TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    target_exam          TEXT NOT NULL,
    subjects             TEXT NOT NULL,
    registration_date    TEXT NOT NULL,
    language_preference  TEXT DEFAULT 'en',
    voice_speed          TEXT DEFAULT 'medium',
    accessibility        TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS study_sessions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             TEXT NOT NULL,
    subject             TEXT NOT NULL DEFAULT 'General',
    topic               TEXT NOT NULL DEFAULT 'General',
    exam_type           TEXT NOT NULL DEFAULT '',
    duration_seconds    REAL NOT NULL DEFAULT 0,
    questions_attempted INTEGER NOT NULL DEFAULT 0,
    questions_correct   INTEGER NOT NULL DEFAULT 0,
    session_date        TEXT NOT NULL,
    session_start       TEXT NOT NULL,
    mode                TEXT DEFAULT 'study',
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_ss_user_date
    ON study_sessions(user_id, session_date);
CREATE INDEX IF NOT EXISTS idx_ss_user_topic
    ON study_sessions(user_id, subject, topic);

CREATE TABLE IF NOT EXISTS topic_progress (
    user_id          TEXT NOT NULL,
    subject          TEXT NOT NULL,
    topic            TEXT NOT NULL,
    exam_type        TEXT NOT NULL DEFAULT '',
    completed        INTEGER NOT NULL DEFAULT 0,
    last_studied     TEXT,
    revision_due     TEXT,
    revision_count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, subject, topic, exam_type)
);

CREATE TABLE IF NOT EXISTS mock_scores (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             TEXT NOT NULL,
    exam_type           TEXT NOT NULL,
    score               REAL NOT NULL,
    questions_attempted INTEGER NOT NULL DEFAULT 0,
    correct_count       INTEGER NOT NULL DEFAULT 0,
    duration_seconds    REAL NOT NULL DEFAULT 0,
    test_date           TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_ms_user_exam
    ON mock_scores(user_id, exam_type, test_date);
"""

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class UserProfile:
    user_id: str
    name: str
    target_exam: str
    subjects: List[str]
    registration_date: str
    language_preference: str = "en"
    voice_speed: str = "medium"
    accessibility: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StudySession:
    user_id: str
    subject: str
    topic: str
    exam_type: str
    duration_seconds: float
    questions_attempted: int
    questions_correct: int
    session_date: str
    session_start: str
    mode: str = "study"


@dataclass
class TopicProgress:
    user_id: str
    subject: str
    topic: str
    exam_type: str
    completed: bool
    last_studied: Optional[str]
    revision_due: Optional[str]
    revision_count: int


@dataclass
class MockScore:
    user_id: str
    exam_type: str
    score: float
    questions_attempted: int
    correct_count: int
    duration_seconds: float
    test_date: str


@dataclass
class StudyPlanEntry:
    date_str: str
    subject: str
    topic: str
    suggested_duration_mins: int
    reason: str


# ---------------------------------------------------------------------------
# UserManager
# ---------------------------------------------------------------------------


class UserManager:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = Path(
            db_path or settings.user.db_path
        )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        logger.info(f"UserManager ready | db={self._db_path}")

    # ------------------------------------------------------------------
    # Connection helper
    # ------------------------------------------------------------------

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self._db_path))
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
        logger.debug("UserManager schema initialised")

    # ------------------------------------------------------------------
    # User profile CRUD
    # ------------------------------------------------------------------

    def create_user_profile(
        self,
        name: str,
        exam: str,
        subjects: List[str],
        language_preference: str = "en",
        voice_speed: str = "medium",
        accessibility: Optional[Dict[str, Any]] = None,
    ) -> UserProfile:
        user_id = str(uuid.uuid4())
        reg_date = datetime.utcnow().isoformat()
        acc = json.dumps(accessibility or {})
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO users
                   (user_id, name, target_exam, subjects,
                    registration_date, language_preference, voice_speed, accessibility)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    user_id, name, exam,
                    json.dumps(subjects), reg_date,
                    language_preference, voice_speed, acc,
                ),
            )
        profile = UserProfile(
            user_id=user_id,
            name=name,
            target_exam=exam,
            subjects=subjects,
            registration_date=reg_date,
            language_preference=language_preference,
            voice_speed=voice_speed,
            accessibility=accessibility or {},
        )
        logger.info(f"Created user profile: {name} ({user_id})")
        return profile

    def get_user_profile(self, user_id: str) -> Optional[UserProfile]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
        if not row:
            return None
        return UserProfile(
            user_id=row["user_id"],
            name=row["name"],
            target_exam=row["target_exam"],
            subjects=json.loads(row["subjects"]),
            registration_date=row["registration_date"],
            language_preference=row["language_preference"],
            voice_speed=row["voice_speed"],
            accessibility=json.loads(row["accessibility"] or "{}"),
        )

    def update_user_preferences(
        self,
        user_id: str,
        language_preference: Optional[str] = None,
        voice_speed: Optional[str] = None,
        accessibility: Optional[Dict[str, Any]] = None,
    ) -> None:
        updates: List[Tuple[str, Any]] = []
        if language_preference is not None:
            updates.append(("language_preference", language_preference))
        if voice_speed is not None:
            updates.append(("voice_speed", voice_speed))
        if accessibility is not None:
            updates.append(("accessibility", json.dumps(accessibility)))
        if not updates:
            return
        set_clause = ", ".join(f"{col}=?" for col, _ in updates)
        values = [v for _, v in updates] + [user_id]
        with self._conn() as conn:
            conn.execute(
                f"UPDATE users SET {set_clause} WHERE user_id=?", values
            )

    def list_users(self) -> List[UserProfile]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY registration_date").fetchall()
        profiles = []
        for row in rows:
            profiles.append(UserProfile(
                user_id=row["user_id"],
                name=row["name"],
                target_exam=row["target_exam"],
                subjects=json.loads(row["subjects"]),
                registration_date=row["registration_date"],
                language_preference=row["language_preference"],
                voice_speed=row["voice_speed"],
                accessibility=json.loads(row["accessibility"] or "{}"),
            ))
        return profiles

    # ------------------------------------------------------------------
    # Study session logging
    # ------------------------------------------------------------------

    def update_study_session(
        self,
        user_id: str,
        topic: str,
        duration: float,
        questions_attempted: int = 0,
        questions_correct: int = 0,
        subject: str = "General",
        exam_type: str = "",
        mode: str = "study",
    ) -> None:
        now = datetime.utcnow()
        session_date = now.date().isoformat()
        session_start = now.isoformat()

        if not exam_type:
            profile = self.get_user_profile(user_id)
            exam_type = profile.target_exam if profile else ""

        with self._conn() as conn:
            conn.execute(
                """INSERT INTO study_sessions
                   (user_id, subject, topic, exam_type, duration_seconds,
                    questions_attempted, questions_correct, session_date, session_start, mode)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    user_id, subject, topic, exam_type,
                    duration, questions_attempted, questions_correct,
                    session_date, session_start, mode,
                ),
            )

        self._update_topic_progress(user_id, subject, topic, exam_type, questions_attempted, questions_correct)
        logger.debug(f"Study session logged | user={user_id} topic={topic} duration={duration:.0f}s")

    def log_mock_score(
        self,
        user_id: str,
        exam_type: str,
        score: float,
        questions_attempted: int,
        correct_count: int,
        duration_seconds: float = 0,
    ) -> None:
        test_date = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO mock_scores
                   (user_id, exam_type, score, questions_attempted,
                    correct_count, duration_seconds, test_date)
                   VALUES (?,?,?,?,?,?,?)""",
                (user_id, exam_type, score, questions_attempted,
                 correct_count, duration_seconds, test_date),
            )
        logger.debug(f"Mock score logged | user={user_id} score={score:.1f}%")

    # ------------------------------------------------------------------
    # Topic progress + spaced repetition
    # ------------------------------------------------------------------

    def _update_topic_progress(
        self,
        user_id: str,
        subject: str,
        topic: str,
        exam_type: str,
        questions_attempted: int,
        questions_correct: int,
    ) -> None:
        now_iso = datetime.utcnow().isoformat()

        with self._conn() as conn:
            row = conn.execute(
                """SELECT revision_count FROM topic_progress
                   WHERE user_id=? AND subject=? AND topic=? AND exam_type=?""",
                (user_id, subject, topic, exam_type),
            ).fetchone()

        if row is None:
            revision_count = 0
        else:
            revision_count = (row["revision_count"] or 0) + 1

        interval_days = _SR_INTERVALS[min(revision_count, len(_SR_INTERVALS) - 1)]
        revision_due = (date.today() + timedelta(days=interval_days)).isoformat()
        completed = 1 if questions_attempted > 0 else 0

        with self._conn() as conn:
            conn.execute(
                """INSERT INTO topic_progress
                   (user_id, subject, topic, exam_type, completed,
                    last_studied, revision_due, revision_count)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(user_id, subject, topic, exam_type)
                   DO UPDATE SET
                       completed    = MAX(completed, excluded.completed),
                       last_studied = excluded.last_studied,
                       revision_due = excluded.revision_due,
                       revision_count = excluded.revision_count""",
                (user_id, subject, topic, exam_type, completed,
                 now_iso, revision_due, revision_count),
            )

    def get_topics_due_for_revision(
        self, user_id: str, days_ahead: int = 3
    ) -> List[TopicProgress]:
        cutoff = (date.today() + timedelta(days=days_ahead)).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM topic_progress
                   WHERE user_id=? AND completed=1 AND revision_due <= ?
                   ORDER BY revision_due""",
                (user_id, cutoff),
            ).fetchall()
        return [self._row_to_topic_progress(r) for r in rows]

    @staticmethod
    def _row_to_topic_progress(row: sqlite3.Row) -> TopicProgress:
        return TopicProgress(
            user_id=row["user_id"],
            subject=row["subject"],
            topic=row["topic"],
            exam_type=row["exam_type"],
            completed=bool(row["completed"]),
            last_studied=row["last_studied"],
            revision_due=row["revision_due"],
            revision_count=row["revision_count"],
        )

    # ------------------------------------------------------------------
    # Analytics helpers
    # ------------------------------------------------------------------

    def _daily_study_seconds(self, user_id: str, days: int = 30) -> Dict[str, float]:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT session_date, SUM(duration_seconds) AS total
                   FROM study_sessions
                   WHERE user_id=? AND session_date >= ?
                   GROUP BY session_date
                   ORDER BY session_date""",
                (user_id, cutoff),
            ).fetchall()
        return {r["session_date"]: r["total"] for r in rows}

    def _calculate_streak(self, daily_map: Dict[str, float]) -> int:
        streak = 0
        check_day = date.today()
        while True:
            if daily_map.get(check_day.isoformat(), 0) > 0:
                streak += 1
                check_day -= timedelta(days=1)
            else:
                break
        return streak

    def _subject_accuracy(
        self, user_id: str, exam_type: Optional[str] = None
    ) -> Dict[str, Dict[str, Any]]:
        query = """
            SELECT subject, topic,
                   SUM(questions_attempted) AS attempted,
                   SUM(questions_correct)   AS correct
            FROM study_sessions
            WHERE user_id=?
        """
        params: List[Any] = [user_id]
        if exam_type:
            query += " AND exam_type=?"
            params.append(exam_type)
        query += " GROUP BY subject, topic HAVING attempted > 0"

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()

        result: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            subj = r["subject"]
            if subj not in result:
                result[subj] = {"attempted": 0, "correct": 0, "topics": {}}
            result[subj]["attempted"] += r["attempted"]
            result[subj]["correct"] += r["correct"]
            acc = r["correct"] / r["attempted"] if r["attempted"] else 0.0
            result[subj]["topics"][r["topic"]] = {
                "attempted": r["attempted"],
                "correct": r["correct"],
                "accuracy": round(acc * 100, 1),
            }
        for subj, data in result.items():
            if data["attempted"]:
                data["accuracy"] = round(data["correct"] / data["attempted"] * 100, 1)
            else:
                data["accuracy"] = 0.0
        return result

    def _mock_score_trend(
        self, user_id: str, exam_type: Optional[str] = None, limit: int = 10
    ) -> List[MockScore]:
        query = "SELECT * FROM mock_scores WHERE user_id=?"
        params: List[Any] = [user_id]
        if exam_type:
            query += " AND exam_type=?"
            params.append(exam_type)
        query += " ORDER BY test_date DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            MockScore(
                user_id=r["user_id"],
                exam_type=r["exam_type"],
                score=r["score"],
                questions_attempted=r["questions_attempted"],
                correct_count=r["correct_count"],
                duration_seconds=r["duration_seconds"],
                test_date=r["test_date"],
            )
            for r in rows
        ]

    def _trend_label(self, scores: List[float]) -> str:
        if len(scores) < 2:
            return "stable"
        recent = scores[:3]
        older = scores[3:6]
        if not older:
            older = scores[-1:]
        avg_recent = sum(recent) / len(recent)
        avg_older = sum(older) / len(older)
        delta = avg_recent - avg_older
        if delta > 5:
            return "improving"
        if delta < -5:
            return "declining"
        return "stable"

    # ------------------------------------------------------------------
    # Public analytics
    # ------------------------------------------------------------------

    def get_progress_summary(self, user_id: str) -> str:
        profile = self.get_user_profile(user_id)
        if not profile:
            return "User profile not found."

        daily = self._daily_study_seconds(user_id, days=30)
        streak = self._calculate_streak(daily)
        this_week_secs = sum(
            v for k, v in daily.items()
            if k >= (date.today() - timedelta(days=7)).isoformat()
        )
        this_week_mins = round(this_week_secs / 60)

        today_mins = round(daily.get(date.today().isoformat(), 0) / 60)

        subj_acc = self._subject_accuracy(user_id, profile.target_exam)

        mock_scores = self._mock_score_trend(user_id, profile.target_exam)
        latest_mock = f"{mock_scores[0].score:.0f} percent" if mock_scores else "no mock tests taken yet"
        trend = self._trend_label([m.score for m in mock_scores]) if len(mock_scores) >= 2 else "not enough data"

        due_revisions = self.get_topics_due_for_revision(user_id, days_ahead=3)
        rev_text = (
            f"You have {len(due_revisions)} topic{'s' if len(due_revisions) != 1 else ''} due for revision soon."
            if due_revisions else
            "No topics due for revision in the next 3 days."
        )

        readiness = self.calculate_exam_readiness(user_id)

        lines = [
            f"Hello {profile.name}. Here is your progress summary for {profile.target_exam}.",
            f"Your current study streak is {streak} day{'s' if streak != 1 else ''}.",
            f"You studied {today_mins} minute{'s' if today_mins != 1 else ''} today "
            f"and {this_week_mins} minutes this week.",
        ]

        if subj_acc:
            subject_parts = []
            for subj, data in subj_acc.items():
                subject_parts.append(
                    f"{subj}: {data['accuracy']:.0f} percent accuracy "
                    f"over {data['attempted']} questions"
                )
            lines.append("Subject-wise accuracy: " + "; ".join(subject_parts) + ".")

        lines.append(f"Your latest mock test score was {latest_mock}. Trend: {trend}.")
        lines.append(rev_text)
        lines.append(f"Overall exam readiness: {readiness:.0f} percent.")

        return " ".join(lines)

    def identify_weak_areas(
        self, user_id: str, exam_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        profile = self.get_user_profile(user_id)
        if not profile:
            return []
        et = exam_type or profile.target_exam

        subj_acc = self._subject_accuracy(user_id, et)
        weak: List[Dict[str, Any]] = []
        for subject, data in subj_acc.items():
            for topic, tdata in data["topics"].items():
                if tdata["attempted"] >= 3 and tdata["accuracy"] < (_WEAK_THRESHOLD * 100):
                    weak.append({
                        "subject": subject,
                        "topic": topic,
                        "accuracy": tdata["accuracy"],
                        "attempted": tdata["attempted"],
                    })
        weak.sort(key=lambda x: x["accuracy"])
        return weak

    def generate_study_plan(
        self,
        user_id: str,
        exam_date: str,
        daily_study_hours: float = 2.0,
    ) -> Tuple[List[StudyPlanEntry], str]:
        profile = self.get_user_profile(user_id)
        if not profile:
            return [], "User profile not found."

        try:
            exam_dt = date.fromisoformat(exam_date)
        except ValueError:
            return [], "Invalid exam date format. Please use YYYY-MM-DD."

        days_left = (exam_dt - date.today()).days
        if days_left <= 0:
            return [], "The exam date has already passed."

        weak_areas = self.identify_weak_areas(user_id)
        due_revisions = self.get_topics_due_for_revision(user_id, days_ahead=days_left)
        subj_acc = self._subject_accuracy(user_id, profile.target_exam)

        mins_per_day = int(daily_study_hours * 60)

        plan: List[StudyPlanEntry] = []
        plan_date = date.today()

        rev_queue = list(due_revisions)
        weak_queue = list(weak_areas)
        new_topics = [
            s for s in profile.subjects
            if s not in {sa for sa in subj_acc}
        ]

        for day_offset in range(min(days_left, 30)):
            plan_date = date.today() + timedelta(days=day_offset)
            remaining_mins = mins_per_day

            if rev_queue:
                tp = rev_queue.pop(0)
                duration = min(30, remaining_mins)
                plan.append(StudyPlanEntry(
                    date_str=plan_date.isoformat(),
                    subject=tp.subject,
                    topic=tp.topic,
                    suggested_duration_mins=duration,
                    reason="Spaced repetition revision due",
                ))
                remaining_mins -= duration

            if weak_queue and remaining_mins >= 20:
                w = weak_queue[day_offset % len(weak_queue)] if weak_queue else None
                if w:
                    duration = min(45, remaining_mins)
                    plan.append(StudyPlanEntry(
                        date_str=plan_date.isoformat(),
                        subject=w["subject"],
                        topic=w["topic"],
                        suggested_duration_mins=duration,
                        reason=f"Weak area: {w['accuracy']:.0f}% accuracy, needs improvement",
                    ))
                    remaining_mins -= duration

            if new_topics and remaining_mins >= 20:
                nt_idx = day_offset % len(new_topics)
                plan.append(StudyPlanEntry(
                    date_str=plan_date.isoformat(),
                    subject=new_topics[nt_idx],
                    topic="New topic introduction",
                    suggested_duration_mins=min(remaining_mins, 30),
                    reason="New subject area not yet studied",
                ))

        voice_summary = self._plan_to_voice(plan, days_left, profile.name, exam_date)
        return plan, voice_summary

    def _plan_to_voice(
        self,
        plan: List[StudyPlanEntry],
        days_left: int,
        name: str,
        exam_date: str,
    ) -> str:
        if not plan:
            return f"{name}, there are no specific recommendations for your study plan right now."

        lines = [
            f"{name}, your exam is on {exam_date}, which is {days_left} day{'s' if days_left != 1 else ''} away.",
            f"Here is your personalised study plan for the next {min(len(plan), 7)} days.",
        ]

        shown: Dict[str, bool] = {}
        for entry in plan[:14]:
            key = entry.date_str
            if key not in shown:
                shown[key] = True
                day_entries = [e for e in plan if e.date_str == key]
                day_label = entry.date_str
                subjects_today = "; ".join(
                    f"{e.subject} — {e.topic} for {e.suggested_duration_mins} minutes"
                    for e in day_entries
                )
                lines.append(f"{day_label}: {subjects_today}.")

        rev_count = sum(1 for e in plan if "revision" in e.reason.lower())
        weak_count = sum(1 for e in plan if "weak" in e.reason.lower())
        lines.append(
            f"Your plan includes {rev_count} revision session{'s' if rev_count != 1 else ''} "
            f"and {weak_count} weak-area improvement session{'s' if weak_count != 1 else ''}."
        )
        lines.append("Good luck with your preparation!")
        return " ".join(lines)

    def calculate_exam_readiness(
        self, user_id: str, exam_type: Optional[str] = None
    ) -> float:
        profile = self.get_user_profile(user_id)
        if not profile:
            return 0.0
        et = exam_type or profile.target_exam

        subj_acc = self._subject_accuracy(user_id, et)
        mock_scores = self._mock_score_trend(user_id, et)
        daily = self._daily_study_seconds(user_id, days=30)
        streak = self._calculate_streak(daily)
        total_study_hours = sum(daily.values()) / 3600

        score_components: List[float] = []

        if subj_acc:
            overall_attempted = sum(d["attempted"] for d in subj_acc.values())
            overall_correct = sum(d["correct"] for d in subj_acc.values())
            if overall_attempted:
                acc_score = (overall_correct / overall_attempted) * 100
                score_components.append(acc_score * 0.40)

        if mock_scores:
            avg_mock = sum(m.score for m in mock_scores[:5]) / len(mock_scores[:5])
            score_components.append(avg_mock * 0.35)

        streak_bonus = min(streak / 30.0, 1.0) * 15
        score_components.append(streak_bonus)

        hours_bonus = min(total_study_hours / 40.0, 1.0) * 10
        score_components.append(hours_bonus)

        readiness = sum(score_components)
        return round(min(readiness, 100.0), 1)

    def get_weak_areas_voice(self, user_id: str) -> str:
        profile = self.get_user_profile(user_id)
        if not profile:
            return "User profile not found."

        weak = self.identify_weak_areas(user_id)
        if not weak:
            return (
                f"Great news, {profile.name}! "
                "You have no weak topics that need urgent attention right now. "
                "Keep practising to maintain your performance."
            )

        lines = [
            f"{profile.name}, here are your weak topics that need more practice."
        ]
        for i, w in enumerate(weak[:5], 1):
            lines.append(
                f"Number {i}: {w['subject']}, topic {w['topic']}, "
                f"accuracy {w['accuracy']:.0f} percent from {w['attempted']} questions."
            )
        if len(weak) > 5:
            lines.append(f"And {len(weak) - 5} more topics also need attention.")
        lines.append("Focus on these areas to improve your exam score.")
        return " ".join(lines)

    def get_weekly_study_voice(self, user_id: str) -> str:
        profile = self.get_user_profile(user_id)
        if not profile:
            return "User profile not found."

        daily = self._daily_study_seconds(user_id, days=30)
        this_week_secs = sum(
            v for k, v in daily.items()
            if k >= (date.today() - timedelta(days=7)).isoformat()
        )
        last_week_secs = sum(
            v for k, v in daily.items()
            if (date.today() - timedelta(days=14)).isoformat() <= k
            < (date.today() - timedelta(days=7)).isoformat()
        )

        this_week_mins = round(this_week_secs / 60)
        last_week_mins = round(last_week_secs / 60)
        streak = self._calculate_streak(daily)

        comparison = ""
        if last_week_mins > 0:
            delta = this_week_mins - last_week_mins
            if delta > 0:
                comparison = f"That is {delta} minutes more than last week — great progress!"
            elif delta < 0:
                comparison = f"That is {abs(delta)} minutes less than last week. Try to study a bit more."
            else:
                comparison = "That matches last week's study time. Consistency is key!"

        days_studied = sum(1 for v in (
            daily.get((date.today() - timedelta(days=i)).isoformat(), 0) for i in range(7)
        ) if v > 0)

        return (
            f"{profile.name}, this week you studied for {this_week_mins} minutes "
            f"across {days_studied} day{'s' if days_studied != 1 else ''}. "
            f"{comparison} "
            f"Your current study streak is {streak} day{'s' if streak != 1 else ''}."
        )

    def get_exam_readiness_voice(self, user_id: str) -> str:
        profile = self.get_user_profile(user_id)
        if not profile:
            return "User profile not found."

        readiness = self.calculate_exam_readiness(user_id)
        mock_scores = self._mock_score_trend(user_id, profile.target_exam)
        trend = self._trend_label([m.score for m in mock_scores]) if len(mock_scores) >= 2 else "not enough data"
        weak = self.identify_weak_areas(user_id)

        if readiness >= 80:
            verdict = "You are well-prepared for the exam. Keep revising to stay sharp."
        elif readiness >= 60:
            verdict = "You are moderately prepared. Focus on your weak areas to close the gap."
        elif readiness >= 40:
            verdict = "You need more practice. Increase your daily study time and focus on weak topics."
        else:
            verdict = "Your preparation needs significant improvement. Start with the basics and practise daily."

        lines = [
            f"{profile.name}, your estimated exam readiness for {profile.target_exam} is {readiness:.0f} percent.",
            verdict,
        ]

        if mock_scores:
            lines.append(
                f"Your mock test trend is {trend}, "
                f"with a latest score of {mock_scores[0].score:.0f} percent."
            )
        else:
            lines.append("You have not taken any mock tests yet. Taking a mock test will give a better readiness estimate.")

        if weak:
            top_weak = weak[0]
            lines.append(
                f"Your most critical weak area is {top_weak['subject']}, topic {top_weak['topic']}, "
                f"at {top_weak['accuracy']:.0f} percent accuracy."
            )

        return " ".join(lines)


# ---------------------------------------------------------------------------
# Module-level standalone functions (mirror UserManager methods)
# ---------------------------------------------------------------------------

_default_manager: Optional[UserManager] = None


def _get_manager() -> UserManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = UserManager()
    return _default_manager


def create_user_profile(
    name: str,
    exam: str,
    subjects: List[str],
    language_preference: str = "en",
    voice_speed: str = "medium",
) -> UserProfile:
    return _get_manager().create_user_profile(
        name, exam, subjects, language_preference, voice_speed
    )


def update_study_session(
    user_id: str,
    topic: str,
    duration: float,
    questions_attempted: int = 0,
    questions_correct: int = 0,
    subject: str = "General",
    exam_type: str = "",
) -> None:
    _get_manager().update_study_session(
        user_id, topic, duration, questions_attempted, questions_correct, subject, exam_type
    )


def get_progress_summary(user_id: str) -> str:
    return _get_manager().get_progress_summary(user_id)


def identify_weak_areas(
    user_id: str, exam_type: Optional[str] = None
) -> List[Dict[str, Any]]:
    return _get_manager().identify_weak_areas(user_id, exam_type)


def generate_study_plan(
    user_id: str,
    exam_date: str,
    daily_study_hours: float = 2.0,
) -> Tuple[List[StudyPlanEntry], str]:
    return _get_manager().generate_study_plan(user_id, exam_date, daily_study_hours)


def calculate_exam_readiness(
    user_id: str, exam_type: Optional[str] = None
) -> float:
    return _get_manager().calculate_exam_readiness(user_id, exam_type)
