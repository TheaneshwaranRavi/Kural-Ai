import logging
import uuid

from config import settings
from modules import (
    VoiceModule, RAGModule, DatabaseModule, QueryEngine,
    QuestionBank, PracticeSession,
    UserManager,
    CurrentAffairsManager,
    SyllabusManager,
    OfflineSyncManager,
    UXTestSuite,
)

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

HELP_TEXT = {
    "en": (
        "Welcome to the Tamil Nadu Exam Learning Agent. "
        "Say 'help' for commands, 'quit' to exit, "
        "or ask any exam question."
    ),
    "ta": (
        "தமிழ்நாடு தேர்வு கற்றல் முகவருக்கு வரவேற்கிறோம். "
        "'உதவி' என்று சொல்லுங்கள் அல்லது தேர்வு கேள்வி கேளுங்கள்."
    ),
}

COMMANDS = {
    "help":                 "List available commands",
    "language tamil":       "Switch to Tamil",
    "language english":     "Switch to English",
    "exam tnpsc":           "Set exam type to TNPSC",
    "exam trb":             "Set exam type to TRB",
    "exam banking":         "Set exam type to Banking",
    "speed slow":           "Set speech to slow (100 wpm)",
    "speed medium":         "Set speech to medium (150 wpm)",
    "speed fast":           "Set speech to fast (200 wpm)",
    "difficulty beginner":  "Switch to beginner explanations",
    "difficulty medium":    "Switch to medium explanations",
    "difficulty advanced":  "Switch to advanced explanations",
    "explain <topic>":      "Detailed step-by-step explanation of a topic",
    "practice topic":       "Start topic-wise question practice",
    "practice random":      "Start random mixed practice",
    "practice year <year>": "Practice previous year paper",
    "mock test":            "Start a timed mock test (mini size)",
    "mock test full":       "Start a full-length timed mock test",
    "my progress":          "Voice summary of your study progress",
    "weak topics":          "List your weak areas needing practice",
    "study this week":      "How much you studied this week",
    "am i ready":           "Estimated exam readiness percentage",
    "study plan <date>":    "Generate personalised study plan (date: YYYY-MM-DD)",
    "register":             "Create a new user profile",
    "daily brief":          "Today's current affairs audio summary",
    "update news":          "Fetch latest news and update current affairs",
    "weekly news":          "Compilation of this week's current affairs",
    "news about <topic>":   "Topic-specific news (e.g. 'news about economy')",
    "current affairs quiz": "MCQ quiz from recent current affairs",
    "news status":          "Show current affairs database statistics",
    "syllabus":             "Voice-guided syllabus navigator for current exam",
    "topics in <subject>":  "List topics for a syllabus subject",
    "priority topics":      "High-weightage, frequently-asked topics",
    "go to <topic>":        "Navigate to a specific syllabus topic (e.g. 'go to tamil nadu history')",
    "coverage":             "Percentage of syllabus you've covered",
    "start studying <subject>": "Begin a study session for a subject (e.g. 'start studying polity')",
    "check connection":     "Show internet / Ollama / offline model status",
    "sync":                 "Sync current affairs and pending downloads (online only)",
    "backup":               "Back up your progress to a local archive",
    "restore":              "Restore progress from the latest backup",
    "storage":              "Show storage usage and optimisation options",
    "optimize storage":     "Clean caches, rotate backups, compress old files",
    "repeat":               "Repeat last response",
    "history":              "Show recent session history",
    "feedback":             "Rate or give feedback about this session",
    "report problem":       "Report a problem or issue you encountered",
    "ux report":            "Voice summary of UX pain points and accuracy stats",
    "accessibility check":  "Run the accessibility compliance checklist",
    "quit":                 "Exit the agent",
}


class LearningAgent:
    def __init__(self):
        logger.info("Initialising Learning Agent...")
        self.voice = VoiceModule()
        self.rag = RAGModule()
        self.query_engine = QueryEngine(rag_module=self.rag)
        self.db = DatabaseModule()
        self.question_bank = QuestionBank()
        self.user_manager = UserManager()
        self.current_affairs = CurrentAffairsManager(rag_module=self.rag)
        self.syllabus = SyllabusManager()
        self.offline_sync = OfflineSyncManager(
            current_affairs_manager=self.current_affairs,
            rag_module=self.rag,
            user_manager=self.user_manager,
        )
        self.ux = UXTestSuite(voice_module=self.voice)
        self.is_online = self.offline_sync.is_online()
        if not self.is_online:
            logger.warning(
                "No internet detected — running in offline mode. "
                "Current affairs updates will be skipped."
            )
        self.current_user_id = settings.user.default_user_id
        self.current_language = settings.language.default_language
        self.current_exam = settings.exam.default_exam
        self.current_subject = "General"
        self.current_difficulty = settings.llm.default_difficulty
        self._session_id: str = str(uuid.uuid4())
        self._questions_answered: int = 0

    def _respond(self, text: str, language: str = None) -> None:
        print(f"\nAgent: {text}\n")
        self.voice.speak_text(text, language=language or self.current_language)

    def _handle_command(self, text: str):
        lowered = text.lower().strip()
        canonical = self.voice.match_command(lowered)

        if canonical == "stop" or lowered in ("quit", "exit"):
            if self.voice.confirm_action("exit the agent"):
                self._respond("Goodbye! Keep studying hard.")
                self._collect_session_feedback("satisfaction", context="session_end", silent_on_skip=True)
                self.current_affairs.stop_scheduler()
                return False
            return True

        if canonical == "help" or lowered == "help":
            cmd_list = "\n".join(f"  {k}: {v}" for k, v in COMMANDS.items())
            self._respond(f"Available commands:\n{cmd_list}")
            return True

        if canonical == "repeat":
            self.voice.repeat_last()
            return True

        if canonical in ("slow", "medium", "fast"):
            self.voice.set_speed(canonical)
            self._respond(f"Speed set to {canonical}.")
            return True

        if lowered.startswith("speed "):
            speed = lowered.split(" ", 1)[1].strip()
            if speed in ("slow", "medium", "fast"):
                self.voice.set_speed(speed)
                self._respond(f"Speed set to {speed}.")
            return True

        if lowered.startswith("difficulty "):
            diff = lowered.split(" ", 1)[1].strip()
            if diff in settings.llm.supported_difficulties:
                self.current_difficulty = diff
                self._respond(f"Difficulty set to {diff}.")
            return True

        if lowered == "history":
            sessions = self.db.get_recent_sessions(5)
            if not sessions:
                self._respond("No history found.")
            else:
                for s in sessions:
                    print(f"  [{s['timestamp']}] Q: {s['question'][:60]}")
            return True

        if lowered.startswith("language "):
            lang = lowered.split(" ", 1)[1].strip()
            lang_map = {"tamil": "ta", "english": "en"}
            if lang in lang_map:
                self.current_language = lang_map[lang]
                self.voice.set_language(self.current_language)
                self._respond(f"Language set to {lang.capitalize()}.")
            return True

        if lowered.startswith("exam "):
            exam = lowered.split(" ", 1)[1].strip().upper()
            if exam in [e.upper() for e in settings.exam.supported_exams]:
                self.current_exam = exam
                self._respond(f"Exam type set to {exam}.")
            return True

        if lowered.startswith("explain "):
            topic = text.split(" ", 1)[1].strip()
            self._explain_topic(topic)
            return True

        if lowered == "practice topic":
            self._start_practice(mode="topic")
            return True

        if lowered == "practice random":
            self._start_practice(mode="random")
            return True

        if lowered.startswith("practice year "):
            parts = lowered.split()
            year = int(parts[-1]) if parts[-1].isdigit() else None
            self._start_practice(mode="year", year=year)
            return True

        if lowered in ("mock test", "mock"):
            self._start_practice(mode="mock", size="mini")
            return True

        if lowered == "mock test full":
            self._start_practice(mode="mock", size="full")
            return True

        if lowered in ("my progress", "tell me my progress", "progress"):
            report = self.user_manager.get_progress_summary(self.current_user_id)
            self._respond(report)
            return True

        if lowered in ("weak topics", "what are my weak topics", "weak areas"):
            report = self.user_manager.get_weak_areas_voice(self.current_user_id)
            self._respond(report)
            return True

        if lowered in ("study this week", "how much have i studied this week", "weekly study"):
            report = self.user_manager.get_weekly_study_voice(self.current_user_id)
            self._respond(report)
            return True

        if lowered in ("am i ready", "am i ready for the exam", "exam readiness", "readiness"):
            report = self.user_manager.get_exam_readiness_voice(self.current_user_id)
            self._respond(report)
            return True

        if lowered.startswith("study plan "):
            exam_date = lowered.split("study plan ", 1)[1].strip()
            self._generate_study_plan(exam_date)
            return True

        if lowered == "register":
            self._register_user()
            return True

        if lowered in ("daily brief", "current affairs", "today's news", "news brief"):
            self._deliver_daily_brief()
            return True

        if lowered in ("update news", "fetch news", "refresh news", "update current affairs"):
            self._update_news()
            return True

        if lowered in ("weekly news", "weekly current affairs", "this week's news"):
            self._deliver_weekly_news()
            return True

        if lowered.startswith("news about "):
            topic = text.split("news about ", 1)[1].strip()
            self._deliver_topic_news(topic)
            return True

        if lowered in ("current affairs quiz", "news quiz", "ca quiz"):
            self._run_current_affairs_quiz()
            return True

        if lowered in ("check connection", "connection status", "am i online"):
            self._report_connection_status()
            return True

        if lowered in ("sync", "sync now", "update all"):
            self._run_sync()
            return True

        if lowered in ("backup", "backup progress", "save progress"):
            self._backup_progress()
            return True

        if lowered in ("restore", "restore backup", "restore progress"):
            self._restore_progress()
            return True

        if lowered in ("storage", "storage usage", "disk usage"):
            self._report_storage_usage()
            return True

        if lowered in ("optimize storage", "optimise storage", "clean storage"):
            self._optimize_storage()
            return True

        if lowered in ("syllabus", "show syllabus", "what topics are covered"):
            self._respond(
                self.syllabus.syllabus_navigator(
                    self._syllabus_exam_code(), language=self.current_language
                )
            )
            return True

        if lowered.startswith("topics in "):
            subj = text.split("topics in ", 1)[1].strip()
            self._respond(
                self.syllabus.topics_voice_report(
                    self._syllabus_exam_code(), subj
                )
            )
            return True

        if lowered in ("priority topics", "important topics", "high priority topics"):
            self._respond(
                self.syllabus.priority_topics_voice(
                    self._syllabus_exam_code(),
                    limit=settings.syllabus.priority_topics_limit,
                )
            )
            return True

        if lowered.startswith("go to ") or lowered.startswith("take me to "):
            query = text.split(" to ", 1)[1].strip() if " to " in text else ""
            self._navigate_to_topic(query)
            return True

        if lowered in ("coverage", "syllabus coverage", "how much syllabus"):
            self._respond(
                self.syllabus.coverage_voice_report(
                    self.current_user_id, self._syllabus_exam_code()
                )
            )
            return True

        if lowered.startswith("start studying "):
            subj = text.split("start studying ", 1)[1].strip()
            self._start_studying_subject(subj)
            return True

        if lowered in ("news status", "current affairs status"):
            stats = self.current_affairs.get_stats()
            report = (
                f"Current affairs database: {stats['total_news_items']} total items, "
                f"{stats['items_today']} from today, "
                f"{stats['pending_rag_ingestion']} pending RAG ingestion, "
                f"{stats['quiz_questions']} quiz questions generated."
            )
            self._respond(report)
            return True

        if lowered in ("feedback", "rate session", "give feedback"):
            self._collect_session_feedback("satisfaction")
            return True

        if lowered in ("report problem", "report an issue", "there is a problem"):
            self._collect_session_feedback("problem")
            return True

        if lowered in ("ux report", "ux analysis", "pain points"):
            self._deliver_ux_report()
            return True

        if lowered in ("accessibility check", "a11y check", "accessibility report"):
            self._deliver_accessibility_check()
            return True

        return None

    def _answer_question(self, question: str) -> None:
        self.voice.play_earcon("loading")
        try:
            result = self.query_engine.query_rag(
                user_question=question,
                exam_type=self.current_exam,
                subject=self.current_subject,
                language=self.current_language,
                difficulty=self.current_difficulty,
            )
            self.voice.play_earcon("success")
            self._respond(result.answer, language=result.language)
            self.db.log_query(
                question=question,
                answer=result.answer,
                language=result.language,
                exam_type=self.current_exam,
            )
            if result.sources:
                logger.info(
                    f"Sources: {[s['source'] for s in result.sources[:3]]}"
                )
            self._questions_answered += 1
            if self._questions_answered % 5 == 0:
                self._collect_session_feedback("clarity", silent_on_skip=True)
        except Exception as e:
            logger.error(f"Query engine error: {e}")
            self.voice.play_earcon("error")
            self._respond("Something went wrong. Please try again.")

    def _explain_topic(self, topic: str) -> None:
        self.voice.play_earcon("loading")
        try:
            result = self.query_engine.explain_concept(
                topic=topic,
                difficulty_level=self.current_difficulty,
                exam_type=self.current_exam,
                subject=self.current_subject,
                language=self.current_language,
            )
            self.voice.play_earcon("success")
            self._respond(result.answer, language=result.language)
            self.db.log_query(
                question=f"explain: {topic}",
                answer=result.answer,
                language=result.language,
                exam_type=self.current_exam,
            )
        except Exception as e:
            logger.error(f"Explain topic error: {e}")
            self.voice.play_earcon("error")
            self._respond("Could not generate explanation. Please try again.")

    def _generate_study_plan(self, exam_date: str) -> None:
        try:
            _, voice_summary = self.user_manager.generate_study_plan(
                user_id=self.current_user_id,
                exam_date=exam_date,
                daily_study_hours=settings.user.default_daily_study_hours,
            )
            self._respond(voice_summary)
        except Exception as e:
            logger.error(f"Study plan error: {e}")
            self._respond("Could not generate a study plan. Please check the date format and try again.")

    def _register_user(self) -> None:
        self._respond("Let's create your profile. Please say or type your name.")
        print("Your name: ", end="", flush=True)
        name = self.voice.listen_to_command() or input().strip()
        if not name:
            self._respond("No name provided. Registration cancelled.")
            return

        exam_opts = settings.exam.supported_exams
        selection = self.voice.voice_menu(
            options=exam_opts,
            header="Which exam are you preparing for?",
        )
        if selection is None:
            self._respond("No exam selected. Registration cancelled.")
            return
        exam = exam_opts[selection - 1]

        self._respond(
            f"Creating profile for {name}, exam {exam}. "
            "You can update your subjects and preferences later."
        )
        try:
            profile = self.user_manager.create_user_profile(
                name=name,
                exam=exam,
                subjects=list(settings.exam.exam_categories.get(exam, ["General"])),
                language_preference=self.current_language,
                voice_speed=self.voice._speed if hasattr(self.voice, "_speed") else "medium",
            )
            self.current_user_id = profile.user_id
            self._respond(
                f"Profile created successfully! Welcome, {name}. "
                f"Your user ID is {profile.user_id[:8]}. "
                "You can now track your progress as you study."
            )
        except Exception as e:
            logger.error(f"Registration error: {e}")
            self._respond("Registration failed. Please try again.")

    def _start_practice(self, mode: str = "random", year: int = None, size: str = "mini") -> None:
        session = PracticeSession(
            voice_module=self.voice,
            question_bank=self.question_bank,
            user_id=settings.practice.default_user_id,
            language=self.current_language,
        )
        try:
            if mode == "topic":
                result = session.run_topic_practice(exam_type=self.current_exam)
            elif mode == "year":
                result = session.run_year_practice(
                    exam_type=self.current_exam, year=year or 2023
                )
            elif mode == "mock":
                result = session.generate_mock_test(exam_type=self.current_exam, size=size)
            else:
                result = session.run_random_practice(exam_type=self.current_exam)

            if result and result.score:
                pct = result.score.get("percentage", 0)
                grade = result.score.get("grade", "")
                correct = result.score.get("correct", 0)
                total = result.score.get("total", len(result.answered))
                duration = result.duration_seconds
                summary = (
                    f"Practice complete. Score: {pct:.0f} percent. Grade: {grade}. "
                    f"Questions attempted: {total}."
                )
                self._respond(summary)
                self.db.log_query(
                    question=f"practice_{mode}",
                    answer=summary,
                    language=self.current_language,
                    exam_type=self.current_exam,
                )
                try:
                    self.user_manager.update_study_session(
                        user_id=self.current_user_id,
                        topic=f"practice_{mode}",
                        duration=duration,
                        questions_attempted=total,
                        questions_correct=correct,
                        subject=self.current_subject,
                        exam_type=self.current_exam,
                        mode=mode,
                    )
                    if mode == "mock":
                        self.user_manager.log_mock_score(
                            user_id=self.current_user_id,
                            exam_type=self.current_exam,
                            score=pct,
                            questions_attempted=total,
                            correct_count=correct,
                            duration_seconds=duration,
                        )
                except Exception as ue:
                    logger.warning(f"UserManager session update failed: {ue}")

                self._collect_session_feedback("satisfaction", context=f"practice_{mode}", silent_on_skip=True)
        except Exception as e:
            logger.error(f"Practice session error: {e}")
            self.voice.play_earcon("error")
            self._respond("Practice session ended unexpectedly. Please try again.")

    def _collect_session_feedback(
        self,
        kind: str = "satisfaction",
        context: str = "",
        silent_on_skip: bool = False,
    ) -> None:
        try:
            if kind == "clarity":
                prompt = (
                    "Quick check: was the last explanation clear? "
                    "Say yes or no, or press Enter to skip."
                )
            elif kind == "problem":
                prompt = (
                    "Please describe the problem you encountered. "
                    "Say or type your feedback, then press Enter."
                )
            else:
                prompt = (
                    "How would you rate this session on a scale of one to five? "
                    "Say a number, or press Enter to skip."
                )

            if not silent_on_skip:
                self._respond(prompt)
            else:
                print(f"\n[Feedback] {prompt}")

            print("Your feedback (or Enter to skip): ", end="", flush=True)
            raw = self.voice.listen_to_command()
            if raw is None:
                try:
                    raw = input().strip()
                except EOFError:
                    raw = ""

            if not raw:
                return

            rating = None
            yes_no = None
            comment = raw

            if kind == "clarity":
                cmd = self.voice.match_command(raw)
                yes_no = (cmd == "yes") if cmd in ("yes", "no") else None
            elif kind == "satisfaction":
                _num_map = {
                    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5,
                }
                for tok in raw.lower().split():
                    if tok in _num_map:
                        rating = _num_map[tok]
                        break

            self.ux.collect_user_feedback(
                session_id=self._session_id,
                kind=kind,
                rating=rating,
                yes_no=yes_no,
                comment=comment,
                context=context,
                user_id=self.current_user_id,
                language=self.current_language,
                prompt=False,
            )
            if not silent_on_skip:
                self._respond("Thank you for your feedback!")
        except Exception as e:
            logger.warning(f"Feedback collection failed: {e}")

    def _deliver_ux_report(self) -> None:
        try:
            report = self.ux.analyze_pain_points()
            parts = []

            cmd_ok = round(
                sum(c["accuracy_pct"] for c in report["command_accuracy"]) /
                max(len(report["command_accuracy"]), 1), 1
            )
            parts.append(f"Overall voice command recognition accuracy: {cmd_ok} percent.")

            if report["worst_commands"]:
                worst = report["worst_commands"][0]
                parts.append(
                    f"Lowest-scoring command: '{worst['command']}' "
                    f"at {worst['accuracy_pct']} percent."
                )

            if report["scenario_stats"]:
                slowest = report["scenario_stats"][0]
                parts.append(
                    f"Slowest user journey: '{slowest['scenario']}' "
                    f"averaging {slowest['avg_secs']:.1f} seconds."
                )

            unclears = report.get("unclear_explanations", 0)
            if unclears:
                parts.append(f"{unclears} explanations were marked as unclear by users.")

            sat = report.get("satisfaction", {})
            if sat.get("avg_rating"):
                parts.append(
                    f"Average satisfaction rating: {sat['avg_rating']:.1f} out of 5."
                )

            suggestions = report.get("suggestions", [])
            if suggestions:
                parts.append(f"Top suggestion: {suggestions[0]}")

            self._respond(" ".join(parts) if parts else "No UX data collected yet.")
        except Exception as e:
            logger.error(f"UX report error: {e}")
            self._respond("Could not generate UX report. Please try again.")

    def _deliver_accessibility_check(self) -> None:
        try:
            report = self.ux.accessibility_checklist()
            pass_rate = report["pass_rate_pct"]
            total = report["total"]
            passed = sum(1 for c in report["checks"] if c["pass"])
            failed_items = [c for c in report["checks"] if not c["pass"]]

            summary = (
                f"Accessibility check: {passed} of {total} items passed, "
                f"{pass_rate:.0f} percent compliance."
            )
            if failed_items:
                names = ", ".join(c["item"] for c in failed_items[:3])
                summary += f" Items needing attention: {names}."
            else:
                summary += " All checks passed."

            self._respond(summary)
        except Exception as e:
            logger.error(f"Accessibility check error: {e}")
            self._respond("Could not run accessibility check. Please try again.")

    def _deliver_daily_brief(self) -> None:
        self.voice.play_earcon("loading")
        try:
            brief = self.current_affairs.generate_daily_brief(
                language=self.current_language
            )
            self._respond(brief)
        except Exception as e:
            logger.error(f"Daily brief error: {e}")
            self.voice.play_earcon("error")
            self._respond("Could not generate daily brief. Please try again.")

    def _update_news(self) -> None:
        if not self.offline_sync.is_online():
            self._respond(
                "You are currently offline. Showing cached current affairs. "
                "Connect to the internet to fetch fresh news."
            )
            return
        self._respond(
            "Fetching latest current affairs. This may take a minute. Please wait."
        )
        self.voice.play_earcon("loading")
        try:
            result = self.current_affairs.run_daily_update()
            if result["new"] > 0:
                ingest = self.current_affairs.add_to_rag_database(rag_module=self.rag)
                self._respond(
                    f"News update complete. Fetched {result['fetched']} items, "
                    f"{result['relevant']} relevant, {result['new']} new. "
                    f"Added {ingest.get('ingested', 0)} items to the knowledge base."
                )
            else:
                self._respond(
                    f"News update complete. {result['fetched']} items checked, "
                    "but no new items found since the last update."
                )
        except Exception as e:
            logger.error(f"News update error: {e}")
            self.voice.play_earcon("error")
            self._respond(
                "Could not fetch news updates. "
                "Please check your internet connection and try again."
            )

    def _deliver_weekly_news(self) -> None:
        self.voice.play_earcon("loading")
        try:
            compilation = self.current_affairs.generate_weekly_compilation(
                language=self.current_language
            )
            self._respond(compilation)
        except Exception as e:
            logger.error(f"Weekly news error: {e}")
            self.voice.play_earcon("error")
            self._respond("Could not generate weekly news compilation. Please try again.")

    def _deliver_topic_news(self, topic: str) -> None:
        self.voice.play_earcon("loading")
        try:
            _, voice_text = self.current_affairs.get_topic_news(
                topic=topic,
                days=settings.news.max_age_days,
            )
            self._respond(voice_text)
        except Exception as e:
            logger.error(f"Topic news error: {e}")
            self.voice.play_earcon("error")
            self._respond(f"Could not retrieve news about {topic}. Please try again.")

    def _run_current_affairs_quiz(self) -> None:
        self.voice.play_earcon("loading")
        try:
            questions, voice_script = self.current_affairs.create_current_affairs_quiz(
                time_period="week",
                count=settings.news.quiz_default_count,
            )
            if not questions:
                self._respond(voice_script)
                return

            self._respond(
                f"Starting current affairs quiz with {len(questions)} questions. "
                "I will read each question and the options. Say the option letter to answer."
            )

            score = 0
            for i, q in enumerate(questions, 1):
                question_text = (
                    f"Question {i} of {len(questions)}: {q.question} "
                    f"Option A: {q.option_a}. "
                    f"Option B: {q.option_b}. "
                    f"Option C: {q.option_c}. "
                    f"Option D: {q.option_d}."
                )
                self._respond(question_text)

                print("Your answer (A/B/C/D): ", end="", flush=True)
                raw = self.voice.listen_to_command() or input().strip()
                answer = raw.strip().upper()[:1] if raw else ""

                if answer == q.correct_answer:
                    score += 1
                    self._respond(f"Correct! {q.explanation}")
                else:
                    self._respond(
                        f"The correct answer is option {q.correct_answer}. "
                        f"{q.explanation}"
                    )

            pct = round(score / len(questions) * 100)
            self._respond(
                f"Quiz complete. You scored {score} out of {len(questions)}, "
                f"which is {pct} percent."
            )
        except Exception as e:
            logger.error(f"Current affairs quiz error: {e}")
            self.voice.play_earcon("error")
            self._respond("Could not run current affairs quiz. Please try again.")

    def _report_connection_status(self) -> None:
        rpt = self.offline_sync.connectivity_report()
        parts = []
        parts.append("You are online." if rpt["internet"] else "You are offline. Core study features still work.")
        parts.append("Local Ollama is ready." if rpt["ollama"] else "Ollama is not reachable; answers will use cached context.")
        parts.append("Offline speech recognition is available." if rpt["vosk_offline_stt"] else "Offline speech models not installed; online STT will be used when online.")
        self._respond(" ".join(parts))

    def _run_sync(self) -> None:
        if not self.offline_sync.is_online():
            self._respond(
                "Sync requires an internet connection. "
                "You are currently offline. Please try again when connected."
            )
            return
        self.voice.play_earcon("loading")
        self._respond("Starting sync. This may take a few minutes.")
        ca = self.offline_sync.sync_current_affairs()
        resumed = self.offline_sync.resume_pending_downloads()
        parts = []
        if ca.success:
            parts.append(f"Current affairs: {ca.items_synced} new items.")
        else:
            parts.append("Current affairs sync failed.")
        if resumed.items_synced:
            mb = resumed.bytes_transferred / (1024 * 1024)
            parts.append(f"Resumed {resumed.items_synced} downloads, {mb:.1f} megabytes.")
        self._respond(" ".join(parts) or "Sync complete. No new updates.")

    def _backup_progress(self) -> None:
        if not self.voice.confirm_action("back up your progress to a local archive"):
            self._respond("Backup cancelled.")
            return
        result = self.offline_sync.backup_user_progress(self.current_user_id)
        if result.success:
            mb = result.bytes_transferred / (1024 * 1024)
            self._respond(
                f"Backup created successfully. Archive size: {mb:.1f} megabytes. "
                f"Saved {result.items_synced} database files."
            )
        else:
            self._respond(
                "Backup failed. " + (result.errors[0] if result.errors else "Please try again.")
            )

    def _restore_progress(self) -> None:
        if not self.voice.confirm_action(
            "restore progress from the latest backup — this will overwrite current data"
        ):
            self._respond("Restore cancelled.")
            return
        result = self.offline_sync.restore_from_backup(self.current_user_id)
        if result.success:
            self._respond(
                f"Restore complete. {result.items_synced} database files restored. "
                "Please restart the agent to load the restored data."
            )
        else:
            self._respond(
                "Restore failed. " + (result.errors[0] if result.errors else "")
            )

    def _report_storage_usage(self) -> None:
        u = self.offline_sync.get_storage_usage()
        def mb(b: int) -> str:
            return f"{b / (1024 * 1024):.0f} megabytes"
        self._respond(
            f"Storage usage: {mb(u['total_bytes'])} of "
            f"{u['limit_bytes'] / (1024**3):.0f} gigabytes limit "
            f"({u['pct_of_limit']:.0f} percent used). "
            f"Vector database: {mb(u['vector_db_bytes'])}. "
            f"Models: {mb(u['models_bytes'])}. "
            f"Backups: {mb(u['backups_bytes'])}. "
            f"Cache: {mb(u['cache_bytes'])}."
        )

    def _optimize_storage(self) -> None:
        prune = self.voice.confirm_action(
            "also prune rarely-used content older than 90 days"
        )
        result = self.offline_sync.optimize_storage(
            max_backups_per_user=settings.sync.max_backups_per_user,
            compress_older_than_days=settings.sync.compress_older_than_days,
            prune_least_used=prune,
            user_confirmed_prune=prune,
        )
        mb = result.bytes_transferred / (1024 * 1024)
        if result.success:
            self._respond(
                f"Storage optimisation complete. Freed {mb:.1f} megabytes "
                f"across {result.items_synced} actions."
            )
        else:
            self._respond(
                f"Optimisation finished with some errors. Freed {mb:.1f} megabytes. "
                "Check logs for details."
            )

    def _syllabus_exam_code(self) -> str:
        exam = (self.current_exam or "").upper()
        if "GROUP 1" in exam or "GROUP1" in exam:
            return "TNPSC_GROUP1"
        if "GROUP 2" in exam or "GROUP2" in exam:
            return "TNPSC_GROUP2"
        if "GROUP 4" in exam or "GROUP4" in exam:
            return "TNPSC_GROUP4"
        if exam.startswith("TNPSC"):
            return "TNPSC_GROUP4"
        if exam.startswith("TRB"):
            return "TRB"
        if exam.startswith("BANK"):
            return "BANKING"
        return exam or "TNPSC_GROUP4"

    def _navigate_to_topic(self, query: str) -> None:
        if not query:
            self._respond("Please say the topic name you want to navigate to.")
            return
        found = self.syllabus.find_topic(self._syllabus_exam_code(), query)
        if not found:
            self._respond(
                f"Could not find '{query}' in the {self.current_exam} syllabus. "
                "Try saying 'syllabus' to hear available topics."
            )
            return
        subject, topic = found
        self.current_subject = subject.subject_name
        sub_hours = int(topic.estimated_hours) or "a few"
        self._respond(
            f"Navigating to {topic.topic_name} under {subject.subject_name}. "
            f"Priority {topic.priority}. Estimated {sub_hours} study hours. "
            f"It has {len(topic.subtopics)} subtopics. "
            "Ask any question about this topic, or say 'explain' for an overview."
        )
        self.syllabus.record_study(
            user_id=self.current_user_id,
            exam_type=self._syllabus_exam_code(),
            subject_code=subject.subject_code,
            topic_code=topic.topic_code,
            hours=0.0,
            questions_done=0,
        )

    def _start_studying_subject(self, subject_query: str) -> None:
        if not subject_query:
            self._respond("Please name the subject you want to study.")
            return
        exam_code = self._syllabus_exam_code()
        subjects = self.syllabus.get_subjects(exam_code)
        if not subjects:
            self._respond(f"Syllabus for {exam_code} not available.")
            return
        q = subject_query.lower()
        match = None
        for s in subjects:
            if q in s.subject_name.lower() or (s.subject_name_tamil and q in s.subject_name_tamil):
                match = s
                break
        if not match:
            for s in subjects:
                for tok in q.split():
                    if tok in s.subject_name.lower():
                        match = s
                        break
                if match:
                    break
        if not match:
            self._respond(
                f"Could not find subject '{subject_query}' in {exam_code}. "
                "Say 'syllabus' to list subjects."
            )
            return
        self.current_subject = match.subject_name
        top_topics = match.topics[:5]
        lines = [
            f"Starting study session for {match.subject_name}. "
            f"This subject has {len(match.topics)} topics. "
            f"Top topics to cover:"
        ]
        for i, t in enumerate(top_topics, 1):
            lines.append(f"{i}. {t.topic_name}.")
        lines.append("Say 'go to <topic>' to focus on one, or ask any question.")
        self._respond(" ".join(lines))

    def run(self) -> None:
        self.voice.play_earcon("start")
        if self.offline_sync.is_online():
            self.current_affairs.start_scheduler()
        else:
            logger.info("Offline — news scheduler disabled")
        self._respond(HELP_TEXT.get(self.current_language, HELP_TEXT["en"]))

        while True:
            try:
                print("You (speak or press Enter to type): ", end="", flush=True)
                user_input = self.voice.listen_to_command()

                if user_input is None:
                    user_input = input().strip()

                if not user_input:
                    continue

                print(f"You: {user_input}")
                keep_running = self._handle_command(user_input)

                if keep_running is False:
                    break

                if keep_running is None:
                    self._answer_question(user_input)

            except KeyboardInterrupt:
                self._respond("Session interrupted. Goodbye!")
                self.current_affairs.stop_scheduler()
                self._collect_session_feedback("satisfaction", context="session_end", silent_on_skip=True)
                break
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                self.voice.play_earcon("error")
                self._respond("An error occurred. Please try again.")


if __name__ == "__main__":
    agent = LearningAgent()
    agent.run()
