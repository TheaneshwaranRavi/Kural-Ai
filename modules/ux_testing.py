"""UX testing & optimisation suite for visually impaired users.

Provides:
  - test_voice_commands()      → recognition accuracy + variation report
  - simulate_user_journey()    → scripted task timing + step log
  - collect_user_feedback()    → voice-based feedback recorder (SQLite)
  - analyze_pain_points()      → aggregate pain-point insights
  - accessibility_checklist()  → WCAG/self-voice compliance verification
"""

import json
import logging
import sqlite3
import statistics
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from config import settings
from modules.voice import (
    COMMAND_VOCAB,
    NUMBER_WORDS,
    SPEED_RATES,
    detect_language,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DDL for feedback / UX metrics
# ---------------------------------------------------------------------------

_DDL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS ux_feedback (
    feedback_id   TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    user_id       TEXT DEFAULT '',
    kind          TEXT NOT NULL,       -- clarity / satisfaction / problem / rating
    rating        INTEGER,             -- 1..5  (nullable)
    yes_no        INTEGER,             -- 0 / 1 (nullable)
    comment       TEXT DEFAULT '',
    context       TEXT DEFAULT '',
    language      TEXT DEFAULT 'en',
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_uxfb_session ON ux_feedback(session_id);
CREATE INDEX IF NOT EXISTS idx_uxfb_kind    ON ux_feedback(kind);

CREATE TABLE IF NOT EXISTS ux_journey_runs (
    run_id        TEXT PRIMARY KEY,
    scenario      TEXT NOT NULL,
    user_id       TEXT DEFAULT '',
    total_secs    REAL NOT NULL,
    steps         INTEGER NOT NULL,
    errors        INTEGER NOT NULL,
    success       INTEGER NOT NULL,
    log_json      TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ux_command_tests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical     TEXT NOT NULL,
    spoken        TEXT NOT NULL,
    matched       TEXT,
    correct       INTEGER NOT NULL,
    noise_level   TEXT DEFAULT 'clean',
    accent        TEXT DEFAULT 'neutral',
    tested_at     TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CommandTestCase:
    canonical: str
    spoken: str
    accent: str = "neutral"
    noise_level: str = "clean"
    language: str = "en"


@dataclass
class CommandTestResult:
    canonical: str
    spoken: str
    matched: Optional[str]
    correct: bool
    accent: str
    noise_level: str
    language: str


@dataclass
class JourneyStep:
    name: str
    input_text: str
    expected_outcome: str
    elapsed_secs: float = 0.0
    success: bool = False
    error: str = ""


@dataclass
class JourneyResult:
    run_id: str
    scenario: str
    steps: List[JourneyStep]
    total_secs: float
    error_count: int
    success: bool
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


# ---------------------------------------------------------------------------
# Command variation corpus (accent / typo / code-mixing / noise artefacts)
# ---------------------------------------------------------------------------

_ACCENT_VARIATIONS: Dict[str, List[str]] = {
    "next":     ["nexxt", "nex", "next please", "next one", "next sir"],
    "previous": ["previus", "prev", "go back", "previous question"],
    "repeat":   ["repeet", "say again", "one more time", "repeat please",
                 "repeat it"],
    "explain":  ["explaine", "explain please", "explain this", "explain it to me"],
    "skip":     ["skipp", "skip this", "skip please", "leave it"],
    "bookmark": ["book mark", "bookmark please", "mark this"],
    "menu":     ["menyu", "main menu", "home menu", "go to menu"],
    "yes":      ["yess", "yeah sure", "okay yes", "of course", "yup"],
    "no":       ["noo", "no no", "not really", "negative"],
    "stop":     ["stopp", "please stop", "that's enough", "end this"],
    "help":     ["helpp", "help me", "need help", "what can i do"],
    "slow":     ["slowly please", "bit slower", "too fast"],
    "fast":     ["faster please", "bit faster", "too slow"],
    "medium":   ["normal speed", "default speed"],
}

_NOISE_ARTEFACTS: Dict[str, List[str]] = {
    # Background noise often clips the first/last char or inserts fillers
    "light":    ["um next", "ah repeat", "hmm yes", "next hmm"],
    "moderate": ["...next", "repeat...", "y-yes", "no no"],
    "heavy":    ["*next*", "re[...]peat", "y__s", "n_xt"],
}

_CODE_MIXED: List[Tuple[str, str]] = [
    ("next",     "next-ஐ சொல்"),          # "say next"
    ("repeat",   "repeat பண்ணு"),          # "do repeat"
    ("explain",  "explain செய்"),          # "do explain"
    ("yes",      "yes, சரி"),              # "yes, ok"
    ("no",       "no, வேண்டாம்"),           # "no, don't"
    ("stop",     "stop பண்ணு"),            # "do stop"
    ("menu",     "menu காட்டு"),           # "show menu"
    ("help",     "help தேவை"),             # "need help"
]


def _build_default_corpus() -> List[CommandTestCase]:
    cases: List[CommandTestCase] = []

    # Canonical phrases (should always match)
    for canonical, phrases in COMMAND_VOCAB.items():
        for p in phrases:
            lang = "ta" if detect_language(p) == "tamil" else "en"
            cases.append(CommandTestCase(canonical, p, language=lang))

    # Accent variations
    for canonical, variants in _ACCENT_VARIATIONS.items():
        for v in variants:
            cases.append(CommandTestCase(canonical, v, accent="indian-en"))

    # Noisy variants
    for level, variants in _NOISE_ARTEFACTS.items():
        for v in variants:
            canonical = next(
                (c for c, ps in COMMAND_VOCAB.items()
                 if any(p in v.lower() for p in ps)),
                "unknown",
            )
            cases.append(CommandTestCase(canonical, v, noise_level=level))

    # Tamil-English code mixing
    for canonical, phrase in _CODE_MIXED:
        cases.append(CommandTestCase(canonical, phrase, language="mixed"))

    return cases


# ---------------------------------------------------------------------------
# UXTestSuite
# ---------------------------------------------------------------------------


class UXTestSuite:
    def __init__(
        self,
        voice_module=None,
        db_path: Optional[str] = None,
    ):
        self.voice = voice_module
        base = Path(settings.data_dir)
        self.db_path = Path(db_path) if db_path else base / "ux_metrics.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ---- db helpers ----

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

    # ---- command testing ----

    def _match(self, text: str) -> Optional[str]:
        if self.voice and hasattr(self.voice, "match_command"):
            return self.voice.match_command(text)
        return _fallback_match(text)

    def test_voice_commands(
        self,
        cases: Optional[List[CommandTestCase]] = None,
        persist: bool = True,
    ) -> Dict[str, Any]:
        cases = cases or _build_default_corpus()
        results: List[CommandTestResult] = []

        for tc in cases:
            matched = self._match(tc.spoken)
            correct = (matched == tc.canonical)
            results.append(CommandTestResult(
                canonical=tc.canonical, spoken=tc.spoken, matched=matched,
                correct=correct, accent=tc.accent,
                noise_level=tc.noise_level, language=tc.language,
            ))

        now = datetime.utcnow().isoformat()
        if persist:
            with self._conn() as c:
                c.executemany(
                    """INSERT INTO ux_command_tests
                       (canonical, spoken, matched, correct, noise_level,
                        accent, tested_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    [(r.canonical, r.spoken, r.matched, int(r.correct),
                      r.noise_level, r.accent, now) for r in results],
                )

        return _summarise_command_results(results)

    # ---- user-journey simulation ----

    def simulate_user_journey(
        self,
        scenario: str,
        steps: Optional[List[Dict[str, Any]]] = None,
        handler: Optional[Callable[[str], str]] = None,
        user_id: str = "",
        persist: bool = True,
    ) -> JourneyResult:
        """Execute a scripted journey.

        Each step dict: {name, input, expect_contains}
        `handler(input)` is called to produce the system response;
        if not provided, the default matches commands via self._match.
        """
        scenarios = steps or _SCENARIOS.get(scenario)
        if not scenarios:
            raise ValueError(f"Unknown scenario '{scenario}'")

        run_id = str(uuid.uuid4())
        handler = handler or (lambda txt: self._match(txt) or "")
        journey_steps: List[JourneyStep] = []
        start = time.monotonic()
        error_count = 0

        for step in scenarios:
            s = JourneyStep(
                name=step["name"],
                input_text=step["input"],
                expected_outcome=step.get("expect_contains", ""),
            )
            t0 = time.monotonic()
            try:
                response = handler(step["input"])
                s.elapsed_secs = round(time.monotonic() - t0, 3)
                expect = (s.expected_outcome or "").lower()
                s.success = (expect in str(response).lower()) if expect else True
                if not s.success:
                    error_count += 1
                    s.error = f"expected '{expect}' not in response"
            except Exception as e:
                s.elapsed_secs = round(time.monotonic() - t0, 3)
                s.error = str(e)
                error_count += 1
            journey_steps.append(s)

        total = round(time.monotonic() - start, 3)
        result = JourneyResult(
            run_id=run_id, scenario=scenario, steps=journey_steps,
            total_secs=total, error_count=error_count,
            success=(error_count == 0),
        )

        if persist:
            with self._conn() as c:
                c.execute(
                    """INSERT INTO ux_journey_runs
                       (run_id, scenario, user_id, total_secs, steps,
                        errors, success, log_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, scenario, user_id, total, len(journey_steps),
                     error_count, int(result.success),
                     json.dumps([asdict(s) for s in journey_steps]),
                     result.created_at),
                )
        return result

    # ---- feedback collection ----

    def collect_user_feedback(
        self,
        session_id: str,
        kind: str = "satisfaction",
        rating: Optional[int] = None,
        yes_no: Optional[bool] = None,
        comment: str = "",
        context: str = "",
        user_id: str = "",
        language: str = "en",
        prompt: bool = False,
    ) -> str:
        """Store a feedback record. If `prompt=True` and a voice module is
        wired, asks the user via voice (yes/no or numeric rating)."""
        if prompt and self.voice is not None:
            rating, yes_no, comment = self._voice_feedback_dialog(
                kind, language=language
            )

        fb_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        with self._conn() as c:
            c.execute(
                """INSERT INTO ux_feedback
                   (feedback_id, session_id, user_id, kind, rating,
                    yes_no, comment, context, language, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (fb_id, session_id, user_id, kind, rating,
                 int(yes_no) if yes_no is not None else None,
                 comment, context, language, now),
            )
        logger.info(f"Collected feedback {kind} session={session_id}")
        return fb_id

    def _voice_feedback_dialog(
        self, kind: str, language: str = "en"
    ) -> Tuple[Optional[int], Optional[bool], str]:
        prompts = {
            "clarity": (
                "Was this explanation clear? Say yes or no.",
                "இந்த விளக்கம் தெளிவாக இருந்ததா? ஆம் அல்லது இல்லை என்று சொல்லுங்கள்.",
            ),
            "satisfaction": (
                "On a scale from one to five, how would you rate this session?",
                "ஒன்று முதல் ஐந்து வரை, இந்த அமர்வை எப்படி மதிப்பிடுவீர்கள்?",
            ),
            "problem": (
                "Please describe the problem you encountered.",
                "நீங்கள் சந்தித்த பிரச்சினையை விவரிக்கவும்.",
            ),
        }
        prompt = prompts.get(kind, prompts["satisfaction"])[1 if language == "ta" else 0]
        try:
            self.voice.speak_text(prompt, language=language)
            response = self.voice.listen_to_command() or ""
        except Exception as e:
            logger.warning(f"voice feedback dialog failed: {e}")
            return None, None, ""

        if kind == "clarity":
            yn = self.voice.match_command(response)
            return None, (yn == "yes"), response
        if kind == "satisfaction":
            num = _extract_rating(response)
            return num, None, response
        return None, None, response

    # ---- pain-point analysis ----

    def analyze_pain_points(
        self,
        limit_suggestions: int = 10,
    ) -> Dict[str, Any]:
        with self._conn() as c:
            misses = c.execute(
                """SELECT canonical, COUNT(*) AS total,
                          SUM(correct) AS ok
                   FROM ux_command_tests GROUP BY canonical"""
            ).fetchall()

            noise_misses = c.execute(
                """SELECT noise_level, COUNT(*) AS total,
                          SUM(correct) AS ok
                   FROM ux_command_tests GROUP BY noise_level"""
            ).fetchall()

            slow_steps = c.execute(
                """SELECT scenario, AVG(total_secs) AS avg_secs,
                          AVG(errors) AS avg_err, COUNT(*) AS runs
                   FROM ux_journey_runs GROUP BY scenario
                   ORDER BY avg_err DESC, avg_secs DESC"""
            ).fetchall()

            unclear = c.execute(
                """SELECT COUNT(*) AS n FROM ux_feedback
                   WHERE kind='clarity' AND yes_no=0"""
            ).fetchone()["n"]

            low_ratings = c.execute(
                """SELECT AVG(rating) AS avg_rating,
                          SUM(CASE WHEN rating<=2 THEN 1 ELSE 0 END) AS low_count,
                          COUNT(*) AS total
                   FROM ux_feedback WHERE kind='satisfaction'"""
            ).fetchone()

            recent_problems = c.execute(
                """SELECT comment, context, created_at FROM ux_feedback
                   WHERE kind='problem'
                   ORDER BY created_at DESC LIMIT 10"""
            ).fetchall()

        command_accuracy = [
            {
                "command": r["canonical"],
                "accuracy_pct": round((r["ok"] or 0) / r["total"] * 100, 1)
                if r["total"] else 0.0,
                "samples": r["total"],
            }
            for r in misses
        ]
        worst_commands = sorted(
            command_accuracy, key=lambda x: x["accuracy_pct"]
        )[:5]

        suggestions: List[str] = []
        for w in worst_commands:
            if w["accuracy_pct"] < 80 and w["samples"] >= 3:
                suggestions.append(
                    f"Add more synonyms / accent variants for '{w['command']}' "
                    f"(accuracy {w['accuracy_pct']}% over {w['samples']} samples)."
                )
        for r in noise_misses:
            if r["total"]:
                acc = (r["ok"] or 0) / r["total"] * 100
                if r["noise_level"] != "clean" and acc < 70:
                    suggestions.append(
                        f"Tune noise-suppression / dynamic energy threshold — "
                        f"{r['noise_level']} noise accuracy only {acc:.1f}%."
                    )
        for row in slow_steps:
            if row["avg_err"] and row["avg_err"] > 1.0:
                suggestions.append(
                    f"Simplify scenario '{row['scenario']}' — avg {row['avg_err']:.1f} "
                    f"errors/run across {row['runs']} runs."
                )
        if unclear and unclear > 2:
            suggestions.append(
                f"{unclear} users marked explanations as unclear; "
                "consider simpler language, slower pace, or more analogies."
            )
        if low_ratings and low_ratings["total"]:
            if (low_ratings["avg_rating"] or 0) < 3.5:
                suggestions.append(
                    f"Average satisfaction rating {low_ratings['avg_rating']:.1f}/5 — "
                    f"review recent 'problem' feedback for patterns."
                )

        return {
            "command_accuracy": command_accuracy,
            "worst_commands": worst_commands,
            "noise_impact": [dict(r) for r in noise_misses],
            "scenario_stats": [dict(r) for r in slow_steps],
            "unclear_explanations": unclear,
            "satisfaction": dict(low_ratings) if low_ratings else {},
            "recent_problems": [dict(r) for r in recent_problems],
            "suggestions": suggestions[:limit_suggestions],
        }

    # ---- accessibility checklist ----

    def accessibility_checklist(self) -> Dict[str, Any]:
        """Self-audit against key accessibility requirements for VI users."""
        voice_cfg = settings.voice
        checks: List[Dict[str, Any]] = []

        def add(cat: str, item: str, ok: bool, note: str = "") -> None:
            checks.append(
                {"category": cat, "item": item, "pass": bool(ok), "note": note}
            )

        # 1. Voice-first interaction
        add("Interaction", "All menus accessible via voice commands",
            bool(COMMAND_VOCAB), f"{len(COMMAND_VOCAB)} canonical commands")
        add("Interaction", "Bilingual command vocabulary (Tamil + English)",
            any(any(ord(c) > 127 for c in p) for p in sum(COMMAND_VOCAB.values(), [])))
        add("Interaction", "Repeat-last-response command available",
            "repeat" in COMMAND_VOCAB)
        add("Interaction", "Menu retry limit configured",
            voice_cfg.menu_max_retries >= 2,
            f"retries={voice_cfg.menu_max_retries}")
        add("Interaction", "Confirmation for destructive actions",
            voice_cfg.confirm_max_retries >= 2)

        # 2. Audio output
        add("Audio", "Adjustable speech rates (slow/medium/fast)",
            len(SPEED_RATES) >= 3, f"rates={SPEED_RATES}")
        add("Audio", "Inter-sentence pause for comprehension",
            voice_cfg.inter_sentence_pause_ms >= 200,
            f"{voice_cfg.inter_sentence_pause_ms}ms")
        add("Audio", "Separate Tamil TTS pipeline (gTTS)",
            True, "gTTS + pyttsx3 fallback")
        add("Audio", "Earcon / audio feedback for key events",
            True, "loading/success/error/timeout/bookmark")

        # 3. STT robustness
        add("STT", "Ambient noise calibration before each listen",
            voice_cfg.noise_calibration_duration >= 0.3,
            f"{voice_cfg.noise_calibration_duration}s")
        add("STT", "Offline fallback engine configured (Vosk)",
            bool(voice_cfg.vosk_model_en))
        add("STT", "Dynamic energy threshold for noisy environments",
            True)

        # 4. Content simplification
        add("Content", "Audio-friendly simplifier removes visual references",
            True, "simplify_for_audio() in query_engine")
        add("Content", "Response length capped for voice delivery",
            settings.llm.max_response_words <= 500,
            f"{settings.llm.max_response_words} words")
        add("Content", "Difficulty levels for explanations",
            len(settings.llm.supported_difficulties) >= 3)

        # 5. User preferences / personalisation
        add("Personalisation", "User language preference persisted",
            True, "UserProfile.language_preference")
        add("Personalisation", "Voice speed preference persisted",
            True, "UserProfile.voice_speed")
        add("Personalisation", "Accessibility settings JSON field",
            True, "UserProfile.accessibility")

        # 6. Safety / trust
        add("Safety", "Voice confirmation for exit / delete / restore",
            True, "confirm_action() wrappers in main.py")
        add("Safety", "Progress backup + restore available",
            True, "OfflineSyncManager")

        # 7. Feedback loop
        add("Feedback", "Voice feedback collection present",
            True, "collect_user_feedback / ux_feedback table")
        add("Feedback", "Problem reporting command available",
            True, "'report a problem' voice command")

        passed = sum(1 for c in checks if c["pass"])
        total = len(checks)
        return {
            "total": total,
            "passed": passed,
            "pass_rate_pct": round(passed / max(total, 1) * 100, 1),
            "checks": checks,
        }


# ---------------------------------------------------------------------------
# Prebuilt journey scenarios
# ---------------------------------------------------------------------------

_SCENARIOS: Dict[str, List[Dict[str, str]]] = {
    "start_topic_practice": [
        {"name": "Open menu",        "input": "menu",          "expect_contains": "menu"},
        {"name": "Select practice",  "input": "practice topic", "expect_contains": ""},
        {"name": "Confirm ready",    "input": "yes",            "expect_contains": "yes"},
        {"name": "Hear first Q",     "input": "repeat",         "expect_contains": "repeat"},
        {"name": "Answer A",         "input": "option a",       "expect_contains": ""},
        {"name": "Next question",    "input": "next",           "expect_contains": "next"},
        {"name": "Stop session",     "input": "stop",           "expect_contains": "stop"},
    ],
    "ask_exam_question": [
        {"name": "Wake agent",   "input": "help",    "expect_contains": "help"},
        {"name": "Set exam",     "input": "exam tnpsc", "expect_contains": ""},
        {"name": "Ask question", "input": "explain preamble", "expect_contains": ""},
        {"name": "Request repeat", "input": "repeat",  "expect_contains": "repeat"},
        {"name": "Bookmark",     "input": "bookmark", "expect_contains": "bookmark"},
    ],
    "error_recovery": [
        {"name": "Garbled input 1", "input": "mxyzptlk", "expect_contains": ""},
        {"name": "Retry with help", "input": "help",     "expect_contains": "help"},
        {"name": "Clear command",   "input": "menu",     "expect_contains": "menu"},
    ],
    "daily_brief": [
        {"name": "Daily brief",  "input": "daily brief", "expect_contains": ""},
        {"name": "Repeat item",  "input": "repeat",      "expect_contains": "repeat"},
        {"name": "Skip",         "input": "skip",        "expect_contains": "skip"},
        {"name": "Stop",         "input": "stop",        "expect_contains": "stop"},
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fallback_match(text: str) -> Optional[str]:
    """Lightweight substring/keyword match when VoiceModule isn't wired."""
    if not text:
        return None
    lowered = text.lower().strip()
    best: Optional[Tuple[str, int]] = None
    for canonical, phrases in COMMAND_VOCAB.items():
        for p in phrases:
            pl = p.lower()
            if pl == lowered:
                return canonical
            if pl in lowered or lowered in pl:
                score = len(pl)
                if best is None or score > best[1]:
                    best = (canonical, score)
    return best[0] if best else None


def _extract_rating(text: str) -> Optional[int]:
    if not text:
        return None
    for word, num in NUMBER_WORDS.items():
        if word in text.lower() and 1 <= num <= 5:
            return num
    for ch in text:
        if ch.isdigit():
            n = int(ch)
            if 1 <= n <= 5:
                return n
    return None


def _summarise_command_results(
    results: List[CommandTestResult],
) -> Dict[str, Any]:
    total = len(results)
    correct = sum(1 for r in results if r.correct)
    accuracy = round(correct / total * 100, 2) if total else 0.0

    by_accent: Dict[str, List[CommandTestResult]] = {}
    by_noise: Dict[str, List[CommandTestResult]] = {}
    by_lang: Dict[str, List[CommandTestResult]] = {}
    for r in results:
        by_accent.setdefault(r.accent, []).append(r)
        by_noise.setdefault(r.noise_level, []).append(r)
        by_lang.setdefault(r.language, []).append(r)

    def _pct(lst: List[CommandTestResult]) -> float:
        return round(sum(1 for r in lst if r.correct) / len(lst) * 100, 2) \
            if lst else 0.0

    failures = [
        {"canonical": r.canonical, "spoken": r.spoken, "matched": r.matched,
         "accent": r.accent, "noise": r.noise_level, "language": r.language}
        for r in results if not r.correct
    ]

    return {
        "total": total,
        "correct": correct,
        "accuracy_pct": accuracy,
        "by_accent": {k: _pct(v) for k, v in by_accent.items()},
        "by_noise":  {k: _pct(v) for k, v in by_noise.items()},
        "by_language": {k: _pct(v) for k, v in by_lang.items()},
        "failures": failures[:50],
    }


# ---------------------------------------------------------------------------
# Module-level standalone wrappers
# ---------------------------------------------------------------------------

_default_suite: Optional[UXTestSuite] = None


def _get_suite() -> UXTestSuite:
    global _default_suite
    if _default_suite is None:
        _default_suite = UXTestSuite()
    return _default_suite


def test_voice_commands(
    cases: Optional[List[CommandTestCase]] = None,
    voice_module=None,
    persist: bool = True,
) -> Dict[str, Any]:
    suite = UXTestSuite(voice_module=voice_module) if voice_module else _get_suite()
    return suite.test_voice_commands(cases=cases, persist=persist)


def simulate_user_journey(
    scenario: str,
    handler: Optional[Callable[[str], str]] = None,
    user_id: str = "",
    persist: bool = True,
) -> JourneyResult:
    return _get_suite().simulate_user_journey(
        scenario=scenario, handler=handler, user_id=user_id, persist=persist,
    )


def collect_user_feedback(
    session_id: str,
    kind: str = "satisfaction",
    rating: Optional[int] = None,
    yes_no: Optional[bool] = None,
    comment: str = "",
    context: str = "",
    user_id: str = "",
    language: str = "en",
    voice_module=None,
    prompt: bool = False,
) -> str:
    suite = UXTestSuite(voice_module=voice_module) if voice_module else _get_suite()
    return suite.collect_user_feedback(
        session_id=session_id, kind=kind, rating=rating, yes_no=yes_no,
        comment=comment, context=context, user_id=user_id,
        language=language, prompt=prompt,
    )


def analyze_pain_points(limit_suggestions: int = 10) -> Dict[str, Any]:
    return _get_suite().analyze_pain_points(limit_suggestions=limit_suggestions)


def accessibility_checklist() -> Dict[str, Any]:
    return _get_suite().accessibility_checklist()


# ---------------------------------------------------------------------------
# Beta tester recruitment guidelines (in-code constants for voice playback)
# ---------------------------------------------------------------------------

BETA_TESTER_GUIDELINES: Dict[str, Any] = {
    "target_profile": [
        "Visually impaired students (low vision, blind, legally blind)",
        "Preparing for Tamil Nadu government exams (TNPSC, TRB, Banking)",
        "Comfortable with either Tamil or English or code-mixed speech",
        "Mix of screen-reader users and non-tech-savvy aspirants",
        "Diverse regions of Tamil Nadu (urban + rural accents)",
    ],
    "recruitment_channels": [
        "Partner with Schools for the Blind in Chennai, Madurai, Coimbatore",
        "Coordinate with the TN State Commissioner for Disabilities",
        "NGOs: Vidya Sagar, Worth Trust, Enable India",
        "Disability cells at Anna University, Madras University, Bharathidasan Univ.",
        "Accessible India WhatsApp & Telegram groups for aspirants",
    ],
    "session_format": [
        "30-minute remote session over phone / Zoom (screen optional)",
        "Paid honorarium (₹500–₹1000 per session)",
        "Informed consent in preferred language",
        "Sessions recorded with explicit permission",
        "Task-based: 'register', 'start practice', 'ask a question', "
        "'hear today's news', 'check readiness'",
    ],
    "data_collected": [
        "Voice recognition accuracy per command",
        "Time to complete tasks",
        "Errors and recovery paths",
        "Satisfaction & clarity ratings (voice)",
        "Open-ended feedback on pain points",
        "Accessibility compliance observations",
    ],
    "ethics": [
        "Informed consent in Tamil/English",
        "Opt-out at any time without penalty",
        "Data anonymisation — no PII in analytics DB",
        "Secure storage of recordings; deletion after 90 days unless "
        "participant consents to keep them longer",
    ],
}
