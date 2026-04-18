import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from config import settings
from modules.question_bank import (
    AnsweredQuestion,
    Question,
    QuestionBank,
    calculate_score,
    load_questions,
    save_practice_history,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Answer parsing — maps spoken input to A/B/C/D
# ---------------------------------------------------------------------------

_OPTION_LETTERS = {"a": "A", "b": "B", "c": "C", "d": "D"}

_LETTER_ALIASES: Dict[str, str] = {
    "alpha": "A", "bravo": "B", "charlie": "C", "delta": "D",
    "first": "A", "second": "B", "third": "C", "fourth": "D",
    "one": "A", "two": "B", "three": "C", "four": "D",
    "1": "A", "2": "B", "3": "C", "4": "D",
    "அ": "A", "ஆ": "B", "இ": "C", "ஈ": "D",
    "option a": "A", "option b": "B", "option c": "C", "option d": "D",
    "answer a": "A", "answer b": "B", "answer c": "C", "answer d": "D",
    "choice a": "A", "choice b": "B", "choice c": "C", "choice d": "D",
    "விடை அ": "A", "விடை ஆ": "B", "விடை இ": "C", "விடை ஈ": "D",
}

_LETTER_RE = re.compile(r"\b([abcd])\b", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Feedback phrases (English + Tamil)
# ---------------------------------------------------------------------------

_FEEDBACK: Dict[str, Dict[str, str]] = {
    "correct": {
        "en": "Excellent! That is correct.",
        "ta": "சரியான விடை! மிகவும் நல்லது.",
    },
    "wrong": {
        "en": "That is not correct.",
        "ta": "அது சரியான விடை இல்லை.",
    },
    "skipped": {
        "en": "Question skipped.",
        "ta": "கேள்வி தவிர்க்கப்பட்டது.",
    },
    "time_up": {
        "en": "Time is up! The practice session has ended.",
        "ta": "நேரம் முடிந்தது! பயிற்சி அமர்வு முடிந்தது.",
    },
    "correct_is": {
        "en": "The correct answer is option",
        "ta": "சரியான விடை",
    },
}

_OPTION_LABELS: Dict[str, Dict[str, str]] = {
    "A": {"en": "Option A", "ta": "விடை அ"},
    "B": {"en": "Option B", "ta": "விடை ஆ"},
    "C": {"en": "Option C", "ta": "விடை இ"},
    "D": {"en": "Option D", "ta": "விடை ஈ"},
}

# ---------------------------------------------------------------------------
# Practice session result
# ---------------------------------------------------------------------------


@dataclass
class SessionResult:
    session_id: str
    mode: str
    exam_type: str
    answered: List[AnsweredQuestion]
    score: Dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    ended_at: str = ""
    timed_out: bool = False

    @property
    def duration_seconds(self) -> float:
        try:
            start = datetime.fromisoformat(self.started_at)
            end = datetime.fromisoformat(self.ended_at)
            return (end - start).total_seconds()
        except Exception:
            return 0.0


# ---------------------------------------------------------------------------
# PracticeSession
# ---------------------------------------------------------------------------


class PracticeSession:
    def __init__(
        self,
        voice_module=None,
        question_bank: Optional[QuestionBank] = None,
        user_id: Optional[str] = None,
        language: str = "en",
    ) -> None:
        self._voice = voice_module
        self._bank = question_bank or QuestionBank()
        self._user_id = user_id or settings.practice.default_user_id
        self._language = language
        self._time_up = threading.Event()
        logger.info(f"PracticeSession init | user={self._user_id} lang={language}")

    # ------------------------------------------------------------------
    # Public: practice modes
    # ------------------------------------------------------------------

    def run_topic_practice(
        self,
        exam_type: str,
        subject: Optional[str] = None,
        topic: Optional[str] = None,
        count: int = 10,
    ) -> SessionResult:
        if subject is None:
            subject = self._select_subject(exam_type)
        if topic is None:
            topic = self._select_topic(exam_type, subject)

        difficulty = self._bank.get_recommended_difficulty(
            self._user_id, subject, topic or "General", exam_type
        )
        self._speak(
            f"Topic practice: {subject}. Topic: {topic or 'all topics'}. "
            f"Difficulty: {difficulty}. Loading {count} questions."
        )

        questions = self._bank.load_questions(
            exam_type=exam_type,
            subject=subject,
            topic=topic,
            difficulty=difficulty,
            limit=count,
        )
        if not questions:
            questions = self._bank.load_questions(
                exam_type=exam_type, subject=subject, limit=count
            )

        return self._run_question_loop(questions, mode="topic", exam_type=exam_type)

    def run_year_practice(
        self, exam_type: str, year: int, count: int = 10
    ) -> SessionResult:
        self._speak(f"Previous year paper: {year}. Exam: {exam_type}.")
        questions = self._bank.load_questions(
            exam_type=exam_type, year=year, limit=count
        )
        if not questions:
            self._speak(f"No questions found for year {year}. Starting random practice instead.")
            return self.run_random_practice(exam_type, count)
        return self._run_question_loop(questions, mode="year", exam_type=exam_type)

    def run_random_practice(
        self, exam_type: str, count: int = 10
    ) -> SessionResult:
        self._speak(f"Random practice for {exam_type}. {count} mixed questions.")
        questions = self._bank.load_questions(exam_type=exam_type, limit=count)
        return self._run_question_loop(questions, mode="random", exam_type=exam_type)

    def generate_mock_test(
        self,
        exam_type: str,
        size: str = "mini",
    ) -> SessionResult:
        cfg = settings.practice.mock_test_configs.get(exam_type, {}).get(
            size, {"questions": 20, "duration_mins": 20}
        )
        num_q = cfg["questions"]
        duration_mins = cfg["duration_mins"]
        duration_secs = duration_mins * 60

        self._speak(
            f"Mock test for {exam_type}. "
            f"{num_q} questions. Time limit: {duration_mins} minutes. "
            "The test will begin now. Good luck!"
        )
        questions = self._bank.load_questions(exam_type=exam_type, limit=num_q)
        return self._run_question_loop(
            questions,
            mode="mock",
            exam_type=exam_type,
            time_limit_secs=duration_secs,
        )

    # ------------------------------------------------------------------
    # Public: standalone required functions
    # ------------------------------------------------------------------

    def read_question_aloud(self, question: Question, number: int = 0, total: int = 0) -> None:
        lang = self._question_lang(question)
        text = question.text_tamil if lang == "ta" and question.text_tamil else question.text
        opt_a = question.option_a_tamil if lang == "ta" and question.option_a_tamil else question.option_a
        opt_b = question.option_b_tamil if lang == "ta" and question.option_b_tamil else question.option_b
        opt_c = question.option_c_tamil if lang == "ta" and question.option_c_tamil else question.option_c
        opt_d = question.option_d_tamil if lang == "ta" and question.option_d_tamil else question.option_d

        header = ""
        if number and total:
            header = f"Question {number} of {total}. "

        self._speak(f"{header}{text}", lang)
        time.sleep(settings.practice.post_question_pause_ms / 1000)

        for label, opt_text in [("A", opt_a), ("B", opt_b), ("C", opt_c), ("D", opt_d)]:
            spoken_label = _OPTION_LABELS[label][lang]
            self._speak(f"{spoken_label}: {opt_text}", lang)
            time.sleep(settings.practice.option_pause_ms / 1000)

        prompt = "Please say your answer: A, B, C, or D." if lang == "en" else \
                 "உங்கள் விடையை சொல்லுங்கள்: அ, ஆ, இ, அல்லது ஈ."
        self._speak(prompt, lang)

    def accept_voice_answer(self, language: str = "en") -> Optional[str]:
        max_retries = settings.practice.answer_max_retries
        for attempt in range(1, max_retries + 1):
            if self._time_up.is_set():
                return None

            raw = self._listen()
            if raw is None:
                if attempt < max_retries:
                    retry_msg = (
                        "I did not hear you. Please say A, B, C, or D."
                        if language == "en"
                        else "கேட்கவில்லை. அ, ஆ, இ அல்லது ஈ சொல்லுங்கள்."
                    )
                    self._speak(retry_msg, language)
                continue

            answer = self._parse_answer(raw)
            if answer:
                return answer

            if attempt < max_retries:
                not_understood = (
                    f"I heard '{raw}' but could not find a valid option. Please say A, B, C, or D."
                    if language == "en"
                    else f"புரியவில்லை. தயவுசெய்து அ, ஆ, இ அல்லது ஈ சொல்லுங்கள்."
                )
                self._speak(not_understood, language)

        return None

    def check_answer(self, user_answer: Optional[str], correct_answer: str) -> bool:
        if user_answer is None:
            return False
        return user_answer.upper() == correct_answer.upper()

    def provide_explanation(
        self,
        question: Question,
        user_answer: Optional[str] = None,
        language: Optional[str] = None,
    ) -> None:
        lang = language or self._question_lang(question)
        correct = question.correct_answer

        if user_answer and not self.check_answer(user_answer, correct):
            wrong_msg = (
                f"{_FEEDBACK['wrong'][lang]} "
                f"{_FEEDBACK['correct_is'][lang]} {_OPTION_LABELS[correct][lang]}."
            )
            self._speak(wrong_msg, lang)
        elif user_answer is None:
            skipped_msg = (
                f"{_FEEDBACK['skipped'][lang]} "
                f"{_FEEDBACK['correct_is'][lang]} {_OPTION_LABELS[correct][lang]}."
            )
            self._speak(skipped_msg, lang)

        explanation = (
            question.explanation_tamil
            if lang == "ta" and question.explanation_tamil
            else question.explanation
        )
        if explanation:
            intro = "Here is why: " if lang == "en" else "விளக்கம்: "
            self._speak(f"{intro}{explanation}", lang)

    def calculate_score(self, answered: List[AnsweredQuestion]) -> Dict[str, Any]:
        return self._bank.calculate_score(answered)

    # ------------------------------------------------------------------
    # Private: core question loop
    # ------------------------------------------------------------------

    def _run_question_loop(
        self,
        questions: List[Question],
        mode: str,
        exam_type: str,
        time_limit_secs: Optional[float] = None,
    ) -> SessionResult:
        if not questions:
            self._speak("No questions found for the selected filters. Please try different settings.")
            return SessionResult(
                session_id=str(uuid.uuid4()),
                mode=mode,
                exam_type=exam_type,
                answered=[],
                score={},
                started_at=datetime.utcnow().isoformat(),
                ended_at=datetime.utcnow().isoformat(),
            )

        session_id = str(uuid.uuid4())
        answered: List[AnsweredQuestion] = []
        started_at = datetime.utcnow().isoformat()
        self._time_up.clear()
        timer_thread = None

        if time_limit_secs:
            timer_thread = threading.Timer(time_limit_secs, self._handle_time_up)
            timer_thread.daemon = True
            timer_thread.start()
            self._start_timer_announcements(time_limit_secs)

        total = len(questions)
        i = 0

        while i < total and not self._time_up.is_set():
            question = questions[i]
            lang = self._question_lang(question)

            self.read_question_aloud(question, number=i + 1, total=total)
            t_start = time.time()
            user_answer = self.accept_voice_answer(language=lang)
            t_end = time.time()

            is_correct = self.check_answer(user_answer, question.correct_answer)
            time_taken = round(t_end - t_start, 2)

            if user_answer and is_correct:
                self._speak(_FEEDBACK["correct"][lang], lang)

            self.provide_explanation(question, user_answer, lang)

            answered.append(
                AnsweredQuestion(
                    question=question,
                    user_answer=user_answer,
                    is_correct=is_correct,
                    time_taken=time_taken,
                    timestamp=datetime.utcnow().isoformat(),
                    mode=mode,
                )
            )

            nav = self._navigation_prompt(lang, i + 1, total)
            if nav == "quit":
                break
            if nav == "repeat":
                continue
            i += 1

        if timer_thread:
            timer_thread.cancel()

        ended_at = datetime.utcnow().isoformat()
        timed_out = self._time_up.is_set()
        if timed_out:
            self._speak(_FEEDBACK["time_up"][self._language])

        score = self._bank.calculate_score(answered)
        save_practice_history(
            user_id=self._user_id,
            questions_attempted=answered,
            session_id=session_id,
            mode=mode,
        )

        self._announce_score(score)

        return SessionResult(
            session_id=session_id,
            mode=mode,
            exam_type=exam_type,
            answered=answered,
            score=score,
            started_at=started_at,
            ended_at=ended_at,
            timed_out=timed_out,
        )

    # ------------------------------------------------------------------
    # Private: navigation after each question
    # ------------------------------------------------------------------

    def _navigation_prompt(self, lang: str, current: int, total: int) -> str:
        remaining = total - current
        if remaining <= 0:
            return "next"

        if lang == "en":
            prompt = (
                f"{remaining} questions remaining. "
                "Say 'next' to continue, 'repeat' to hear the question again, "
                "or 'stop' to end the session."
            )
        else:
            prompt = (
                f"இன்னும் {remaining} கேள்விகள் உள்ளன. "
                "'அடுத்து' சொல்ல தொடரலாம், 'மீண்டும்' சொல்ல கேள்வியை மறுபடியும் கேட்கலாம்."
            )
        self._speak(prompt, lang)

        raw = self._listen()
        if raw is None:
            return "next"

        lowered = raw.lower()
        if any(w in lowered for w in ["stop", "quit", "exit", "end", "நிறுத்து"]):
            confirmed = self._voice.confirm_action("end the practice session") \
                if self._voice else True
            if confirmed:
                return "quit"
        if any(w in lowered for w in ["repeat", "again", "மீண்டும்"]):
            return "repeat"

        return "next"

    # ------------------------------------------------------------------
    # Private: score announcement
    # ------------------------------------------------------------------

    def _announce_score(self, score: Dict[str, Any]) -> None:
        if not score or score.get("total", 0) == 0:
            return

        lang = self._language
        pct = score["percentage"]
        grade = score["grade"]
        correct = score["correct"]
        total = score["total"]

        if lang == "en":
            summary = (
                f"Practice complete! You answered {correct} out of {total} correctly. "
                f"Your score is {pct} percent. Grade: {grade}."
            )
        else:
            summary = (
                f"பயிற்சி முடிந்தது! {total} கேள்விகளில் {correct} சரியாக பதிலளித்தீர்கள். "
                f"மதிப்பெண்: {pct} சதவீதம். தரம்: {grade}."
            )
        self._speak(summary)

        weak = score.get("weak_topics", [])
        if weak and lang == "en":
            topic_list = ", ".join(weak[:3])
            self._speak(
                f"Focus on these weak areas: {topic_list}. "
                "Practice them again for better results."
            )
        elif weak and lang == "ta":
            topic_list = ", ".join(weak[:3])
            self._speak(f"இந்த பலவீனமான தலைப்புகளில் மேலும் பயிற்சி செய்யுங்கள்: {topic_list}.")

    # ------------------------------------------------------------------
    # Private: timer management
    # ------------------------------------------------------------------

    def _handle_time_up(self) -> None:
        logger.info("Mock test timer expired")
        self._time_up.set()

    def _start_timer_announcements(self, total_secs: float) -> None:
        interval = settings.practice.timer_announce_interval_mins * 60
        final = settings.practice.timer_final_warning_mins * 60

        def _announce_thread():
            elapsed = 0
            while not self._time_up.is_set():
                time.sleep(10)
                elapsed += 10
                remaining = total_secs - elapsed
                if remaining <= 0:
                    break
                if abs(remaining - final) < 10:
                    mins = int(remaining / 60)
                    self._speak(
                        f"{mins} minutes remaining. Please pace yourself."
                        if self._language == "en"
                        else f"இன்னும் {mins} நிமிடங்கள் உள்ளன."
                    )
                elif elapsed % interval < 10 and elapsed > 30:
                    mins = int(remaining / 60)
                    self._speak(
                        f"{mins} minutes remaining."
                        if self._language == "en"
                        else f"இன்னும் {mins} நிமிடங்கள்."
                    )

        t = threading.Thread(target=_announce_thread, daemon=True)
        t.start()

    # ------------------------------------------------------------------
    # Private: voice helpers
    # ------------------------------------------------------------------

    def _speak(self, text: str, language: Optional[str] = None) -> None:
        lang = language or self._language
        if self._voice:
            self._voice.speak_text(text, language=lang)
        else:
            print(f"[TTS] {text}")

    def _listen(self) -> Optional[str]:
        if self._voice:
            return self._voice.listen_to_command(language=self._language)
        raw = input("[STT] Your answer: ").strip()
        return raw if raw else None

    # ------------------------------------------------------------------
    # Private: menu helpers (subject/topic selection)
    # ------------------------------------------------------------------

    def _select_subject(self, exam_type: str) -> str:
        subjects = self._bank.get_subjects(exam_type=exam_type)
        if not subjects:
            return "General"
        if len(subjects) == 1:
            return subjects[0]
        if self._voice:
            choice = self._voice.voice_menu(
                subjects,
                header=f"Which subject would you like to practice for {exam_type}?",
            )
            return subjects[choice - 1] if choice else subjects[0]
        print("Available subjects:")
        for i, s in enumerate(subjects, 1):
            print(f"  {i}. {s}")
        try:
            idx = int(input("Enter number: ").strip()) - 1
            return subjects[max(0, min(idx, len(subjects) - 1))]
        except ValueError:
            return subjects[0]

    def _select_topic(self, exam_type: str, subject: str) -> Optional[str]:
        topics = self._bank.get_topics(subject=subject, exam_type=exam_type)
        if not topics or len(topics) <= 1:
            return None
        options = ["All topics"] + topics
        if self._voice:
            choice = self._voice.voice_menu(
                options,
                header=f"Which topic in {subject}?",
            )
            if choice is None or choice == 1:
                return None
            return topics[choice - 2]
        print("Available topics:")
        for i, t in enumerate(options, 1):
            print(f"  {i}. {t}")
        try:
            idx = int(input("Enter number: ").strip()) - 1
            if idx == 0:
                return None
            return topics[max(0, min(idx - 1, len(topics) - 1))]
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Private: parsing + language
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_answer(raw: str) -> Optional[str]:
        lowered = raw.lower().strip()

        for alias, letter in sorted(_LETTER_ALIASES.items(), key=lambda x: -len(x[0])):
            if alias in lowered:
                return letter

        match = _LETTER_RE.search(lowered)
        if match:
            return match.group(1).upper()

        return None

    def _question_lang(self, question: Question) -> str:
        if self._language == "ta" and question.text_tamil:
            return "ta"
        return "en"


# ---------------------------------------------------------------------------
# Module-level standalone functions
# ---------------------------------------------------------------------------

_default_session: Optional[PracticeSession] = None


def _get_session(voice_module=None, language: str = "en") -> PracticeSession:
    global _default_session
    if _default_session is None or _default_session._language != language:
        _default_session = PracticeSession(
            voice_module=voice_module, language=language
        )
    return _default_session


def read_question_aloud(
    question: Question,
    number: int = 0,
    total: int = 0,
    voice_module=None,
    language: str = "en",
) -> None:
    _get_session(voice_module, language).read_question_aloud(question, number, total)


def accept_voice_answer(
    language: str = "en", voice_module=None
) -> Optional[str]:
    return _get_session(voice_module, language).accept_voice_answer(language)


def check_answer(user_answer: Optional[str], correct_answer: str) -> bool:
    return PracticeSession.check_answer(None, user_answer, correct_answer)  # type: ignore[arg-type]


def provide_explanation(
    question: Question,
    user_answer: Optional[str] = None,
    language: str = "en",
    voice_module=None,
) -> None:
    _get_session(voice_module, language).provide_explanation(question, user_answer, language)
