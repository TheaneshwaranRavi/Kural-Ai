### [x] Step 1: Set up virtual environment configuration
- Create setup.sh for venv creation and activation
- Add .env.example template

### [x] Step 2: Create requirements.txt
- chromadb, sentence-transformers, langchain, langchain-community
- ollama, pyttsx3, SpeechRecognition, pyaudio
- python-dotenv, pydantic, pydantic-settings, PyMuPDF

### [x] Step 3: Create folder structure
- /data (uploaded documents)
- /vector_db (ChromaDB storage)
- /models (local LLM models)
- /modules (voice, rag, database components)
- /config (settings files)

### [x] Step 4: Create configuration file (config/settings.py)
- LanguageSettings: Tamil/English preferences
- ExamSettings: TNPSC, TRB, Banking types and categories
- VoiceSettings: TTS rate/volume, STT language codes
- VectorDBSettings: db path, collection, embedding model, chunking
- LLMSettings: Ollama provider, model, URL, temperature
- AppSettings: nested config with .env support

### [x] Step 5: Create module stubs
- modules/voice.py: VoiceModule (TTS via pyttsx3, STT via SpeechRecognition)
- modules/rag.py: RAGModule (document ingestion, similarity search via ChromaDB)
- modules/database.py: DatabaseModule (session logging to JSON)
- modules/__init__.py

### [x] Step 6: Create main entry point (main.py)
- LearningAgent class orchestrating voice, RAG, and database modules
- Voice-first interaction loop with keyboard fallback
- Command handling: language switch, exam selection, history, quit
- Tamil/English welcome messages

### [x] Step 7: Build document ingestion and RAG pipeline
- modules/ingestion.py: DocumentProcessor for PDF/TXT/DOCX
  - OCR fallback via Tesseract (eng+tam) for scanned pages
  - Table extraction from PDF (PyMuPDF) and DOCX (python-docx) → markdown tables
  - Math symbol expansion to readable English descriptions
  - Multi-encoding TXT support, NFC unicode normalisation
- modules/chunker.py: SemanticChunker
  - Sentence embedding with sentence-transformers
  - Cosine-distance breakpoint detection at configurable percentile
  - Hard size-limit enforcement with overlap carry-forward
  - chunk_text() standalone function
- modules/rag.py: RAGModule (full rewrite)
  - generate_embeddings(chunks) → List[List[float]]
  - store_in_vectordb(chunks, embeddings, metadata) → List[str] IDs
  - ingest_document(file_path, exam_type, subject, topic, language)
  - update_content(new_documents) for current affairs batch updates
  - query() returns dict with context, sources, similarity scores, found flag
  - delete_by_source() for content management
- requirements.txt updated: pytesseract, Pillow, python-docx, pdf2image, numpy, tqdm
- main.py updated to handle new dict return from query()

### [x] Step 8: Voice interaction module for visually impaired students
- modules/voice.py: Full rewrite of VoiceModule
  - STT: Google Speech Recognition (primary) → Vosk offline fallback
    - _GoogleSTT, _VoskSTT engine classes with lazy model loading
    - dynamic_energy_threshold for background noise adaptation
    - calibrates ambient noise before each listen()
  - TTS: pyttsx3 (English) + gTTS (Tamil) with automatic fallback
    - _PyttxsEngine: sentence-aware pausing at punctuation boundaries
    - _GTTSEngine: gTTS → temp mp3 → pygame/afplay/mpg123 playback
    - _play_mp3(): cross-platform (pygame → afplay → mpg123/ffplay/cvlc)
    - Mixed content: char-level Tamil/English segmentation, routes each to correct engine
  - detect_language(text): Unicode ratio → langdetect fallback → 'tamil'/'english'/'mixed'
  - speak_text(text, language, speed): unified TTS entry point
  - listen_to_command(): STT with Google→Vosk fallback
  - match_command(text): maps transcribed text to canonical COMMAND_VOCAB keys
  - voice_menu(options, header): numbered menu with up to 3 retries
  - confirm_action(action): yes/no confirmation loop with retries
  - repeat_last(): repeats last spoken text
  - set_speed(slow/medium/fast): adjustable 100/150/200 wpm
  - play_earcon(event): spoken audio feedback (start/success/error/timeout/bookmark/loading)
  - COMMAND_VOCAB: 13 canonical commands with Tamil + English phrases
  - NUMBER_WORDS: English + Tamil number words for menu selection
  - Module-level standalone functions: listen_to_command, speak_text, voice_menu, confirm_action
- config/settings.py: Added speed_slow/medium/fast, menu/confirm retries, vosk model paths, gTTS tld, inter_sentence_pause_ms
- requirements.txt: added gTTS, langdetect, pygame (vosk optional/commented)
- main.py: uses speak_text, listen_to_command, match_command, set_speed, play_earcon, confirm_action

### [x] Step 9: RAG-powered query engine with local LLM (Ollama)
- modules/query_engine.py: QueryEngine class (full implementation)
  - detect_intent(text): keyword scoring across study/practice/doubt/explain/unknown
    - bilingual keyword vocabulary (English + Tamil)
  - retrieve_context(query_embedding, filters, top_k): raw ChromaDB vector search
    - accepts pre-computed embedding (no double-encode)
    - metadata filter support (exam_type, subject)
    - returns List[RetrievedChunk] with similarity scores
  - generate_response(query, context, language_preference, intent, exam_type, subject, difficulty)
    - builds system prompt from _SYSTEM_PROMPT_EN / _SYSTEM_PROMPT_TA templates
    - intent-specific instruction injection (_INTENT_INSTRUCTIONS)
    - difficulty modifier injection (beginner/medium/advanced)
    - calls Ollama via ollama.chat() with temperature + max_tokens
    - graceful fallback: returns raw context excerpt if Ollama unavailable
  - query_rag(user_question, exam_type, subject, language, difficulty, top_k) → QueryResult
    - full pipeline: detect language → detect intent → embed query → retrieve chunks
    - retry without subject filter if no results found
    - builds context text with labelled excerpts + citation string
    - calls generate_response, then simplify_for_audio
    - returns QueryResult(answer, intent, language, sources, citation, word_count)
  - explain_concept(topic, difficulty_level, exam_type, subject, language) → QueryResult
    - forces "explain" intent with step-by-step prompt
    - respects difficulty modifier
  - simplify_for_audio(text) → str
    - 20 visual-reference regex replacements (as you can see, look at, figure N, etc.)
    - markdown cleanup (bold, italic, headers, links, code, tables)
    - bullet-to-numbered-speech conversion (First, Second, Third...)
    - punctuation normalization (semicolons → commas, em-dashes → commas)
    - word count enforcement with sentence-boundary truncation
  - Module-level standalone functions: query_rag, retrieve_context, generate_response,
    explain_concept, simplify_for_audio
  - System prompts: bilingual (EN/TA) with 7 strict accessibility rules
  - Intent instructions: 5 intents × 2 languages = 10 specialized prompt fragments
  - Difficulty modifiers: beginner/medium/advanced with Tamil Nadu life analogies
  - Citation: spoken-friendly source attribution with file, subject, topic, page
- config/settings.py: Added max_response_words=350, ollama_timeout=120,
  fallback_to_context, default_difficulty, supported_difficulties
- modules/__init__.py: exports QueryEngine, QueryResult, RetrievedChunk + standalone functions
- main.py: QueryEngine wired with shared RAGModule; added difficulty command,
  explain <topic> command, _explain_topic() method; _answer_question uses query_rag()

### [x] Step 10: Interactive question practice system with voice-based MCQ answering
- config/settings.py: Added PracticeSettings with SQLite DB path, adaptive thresholds
  (promote at 70%, demote at 40%), weak topic threshold, mock test configs per exam type
  (TNPSC/TRB/Banking with full/mini sizes), timer announcement intervals
- modules/question_bank.py: QuestionBank class with full SQLite backend
  - Schema: 3 tables — questions (with Tamil parallel fields), practice_history, user_performance
  - CRUD: add_question, bulk_insert_questions, import_from_json, load_questions
    (filterable by subject/exam_type/difficulty/topic/year/language, with shuffle)
  - Analytics: get_recommended_difficulty() (adaptive), identify_weak_topics(),
    calculate_score() (returns percentage, topic breakdown, grade, weak/strong topics)
  - 15 pre-seeded TNPSC sample questions across History, Polity, Geography, Economy, Science, Banking
  - Standalone functions: load_questions, save_practice_history, calculate_score
- modules/practice_session.py: PracticeSession class
  - 4 practice modes: run_topic_practice, run_year_practice, run_random_practice, generate_mock_test
  - Voice flow: read_question_aloud (reads question + all 4 options with pauses),
    accept_voice_answer (3 retries, parses A/B/C/D including Tamil characters),
    check_answer, provide_explanation
  - Answer parsing: regex + alias map for "option A", "first", "one", "அ" etc.
  - Navigation: after each question — next/repeat/stop with voice confirmation
  - Mock test timer: threading.Timer with interval announcements
  - Score announcement: percentage, grade, weak topics spoken aloud
  - Adaptive difficulty: get_recommended_difficulty called before loading questions
  - Standalone functions: read_question_aloud, accept_voice_answer, check_answer, provide_explanation
- modules/__init__.py: exports QuestionBank, PracticeSession, Question, AnsweredQuestion,
  UserPerformance, SessionResult, and all standalone functions
- main.py: QuestionBank and PracticeSession wired in LearningAgent
  - practice topic / practice random / practice year <year> commands
  - mock test / mock test full commands
  - _start_practice() method with score summary and session logging

### [x] Step 11: User management system with progress tracking and personalised learning paths
- modules/user_manager.py: UserManager class with full SQLite backend
  - Schema: 4 tables — users, study_sessions, topic_progress, mock_scores
  - UserProfile dataclass: user_id, name, target_exam, subjects, registration_date,
    language_preference, voice_speed, accessibility_settings
  - create_user_profile(name, exam, subjects, language, voice_speed) → UserProfile
  - update_study_session(user_id, topic, duration, questions_attempted, questions_correct,
    subject, exam_type, mode) — logs session + triggers spaced repetition update
  - log_mock_score(user_id, exam_type, score, ...) — stores timed test result
  - update_user_preferences(user_id, language, voice_speed, accessibility)
  - Spaced repetition: _update_topic_progress() uses _SR_INTERVALS [1,3,7,14,30,60,120 days]
    with ON CONFLICT upsert; get_topics_due_for_revision(user_id, days_ahead)
  - Analytics:
    - _daily_study_seconds(user_id, days) → {date: seconds} map
    - _calculate_streak(daily_map) → consecutive days studied
    - _subject_accuracy(user_id, exam_type) → {subject: {accuracy, topics: {...}}}
    - _mock_score_trend(user_id, exam_type) → List[MockScore]
    - _trend_label(scores) → 'improving'/'stable'/'declining' (5-pt delta threshold)
  - get_progress_summary(user_id) → voice-friendly full report string
    (streak, today/weekly minutes, subject accuracy, mock trend, revision due, readiness %)
  - identify_weak_areas(user_id) → topics with <60% accuracy, sorted ascending
  - generate_study_plan(user_id, exam_date, daily_hours) → (List[StudyPlanEntry], voice_str)
    - 30-day rolling plan; prioritises due revisions, then weak areas, then new topics
    - _plan_to_voice() renders plan as spoken schedule (day-by-day for 14 days)
  - calculate_exam_readiness(user_id) → float 0–100
    (40% subject accuracy + 35% mock avg + 15% streak bonus + 10% total hours)
  - get_weak_areas_voice(user_id) → spoken weak-topic report (top 5)
  - get_weekly_study_voice(user_id) → this-week vs last-week comparison spoken
  - get_exam_readiness_voice(user_id) → readiness %, verdict, trend, top weak area
  - Module-level standalone functions: create_user_profile, update_study_session,
    get_progress_summary, identify_weak_areas, generate_study_plan, calculate_exam_readiness
- config/settings.py: Added UserSettings with db_path, weak_accuracy_threshold,
  readiness weight factors, streak/hours targets; wired into AppSettings as `user`
- modules/__init__.py: exports UserManager, UserProfile, StudySession, TopicProgress,
  MockScore, StudyPlanEntry, and all standalone functions
- main.py: UserManager wired in LearningAgent
  - current_user_id field initialised from settings.user.default_user_id
  - Voice dashboard commands: "my progress", "weak topics", "study this week",
    "am i ready", "study plan <date>", "register"
  - _generate_study_plan(exam_date) — calls generate_study_plan, speaks voice summary
  - _register_user() — voice/keyboard name capture, voice_menu exam selection,
    creates profile, sets self.current_user_id
  - _start_practice() extended: after session, calls update_study_session + log_mock_score
    so all practice data is automatically tracked in UserManager

### [x] Step 12: Current affairs updater with scheduled news fetching and audio delivery
- modules/current_affairs.py: CurrentAffairsManager class with full SQLite backend
  - Schema: 2 tables — news_items (with relevance_score, ingested_to_rag flag), news_quiz
  - RSS feed catalogue: 7 feeds — PIB India, PIB Tamil Nadu, PIB Economy,
    The Hindu (national + TN), Indian Express, DD News India
  - fetch_news_feeds(sources) — feedparser-based multi-feed fetcher with
    HTML tag stripping, date parsing, 0.5s courtesy delay between requests,
    cutoff filter for max_age_days, graceful per-feed error handling
  - filter_exam_relevant(news_items) — keyword scoring across 30+ terms;
    +5 bonus for Tamil Nadu / TNPSC; +3 for schemes, bills, appointments;
    +4 for RBI/ISRO/DRDO/NITI Aayog; configurable threshold (default 6)
  - summarize_news(news_item) — Ollama prompt (0.3 temp, 120 tokens) with
    extractive fallback: sentence scoring by keyword density, top-3 sentences
  - categorize_news(news_item) → (category, exam_tags) — keyword scoring across
    8 categories: Economy, Polity, Science & Technology, Tamil Nadu,
    Awards & Appointments, Environment, International, Banking & Finance
  - run_daily_update() — full pipeline: fetch → filter → deduplicate → summarise →
    categorise → save; returns stats dict {fetched, relevant, new, elapsed_seconds}
  - generate_daily_brief(date, max_items, language) → voice audio script
    (category-grouped, numbered items, exam tag annotation, 5-minute length)
  - generate_weekly_compilation() → category-wise summary with top 3 headlines each
  - get_topic_news(topic, days, limit) → (items, voice_text) with keyword search
  - add_to_rag_database(news_items, rag_module) — creates Chunk objects directly,
    calls generate_embeddings + store_in_vectordb, marks ingested_to_rag=1
  - create_current_affairs_quiz(time_period, count, category) → (questions, voice_script)
    - 5 regex template patterns for MCQ extraction
      (appointments, scheme launches, awards, achievements, Tamil Nadu events)
    - 3 distractor pools: names, scheme names, monetary values
    - Shuffles options, assigns correct_answer letter, saves to news_quiz table
    - Falls back to already-generated questions if no new items available
  - start_scheduler() — APScheduler BackgroundScheduler (Asia/Kolkata timezone)
    - Daily 06:00: run_daily_update() + add_to_rag_database()
    - Sunday 18:00: generate_weekly_compilation() → saved to data/news_cache/
  - stop_scheduler() — clean shutdown on agent exit
  - get_stats() → {total_news_items, pending_rag_ingestion, quiz_questions, items_today}
  - Module-level standalone functions: fetch_news_feeds, filter_exam_relevant,
    summarize_news, categorize_news, generate_daily_brief,
    add_to_rag_database, create_current_affairs_quiz
- config/settings.py: Added NewsSettings with db_path, cache_dir, relevance_threshold,
  max_age_days, fetch_timeout, daily_brief_max_items, quiz_default_count,
  APScheduler schedule config (timezone, daily hour, weekly day/hour),
  enabled_sources list; wired into AppSettings as `news`
- requirements.txt: added feedparser>=6.0.10, requests>=2.31.0, APScheduler>=3.10.4
- modules/__init__.py: exports CurrentAffairsManager, NewsItem, QuizQuestion,
  and all 7 standalone functions
- main.py: CurrentAffairsManager wired in LearningAgent with shared RAGModule
  - Voice commands: "daily brief", "update news", "weekly news",
    "news about <topic>", "current affairs quiz", "news status"
  - _deliver_daily_brief(), _update_news(), _deliver_weekly_news(),
    _deliver_topic_news(topic), _run_current_affairs_quiz() methods
  - _run_current_affairs_quiz(): interactive per-question voice loop with scoring
  - Scheduler started in run(), stopped on KeyboardInterrupt

### [x] Step 13: Structured syllabus content modules with exam-specific mapping
- data/syllabi/: 5 JSON syllabus files
  - tnpsc_group1.json (Prelims + Mains, 7 GS topics + 2 Mains subjects)
  - tnpsc_group2.json (Group 2/2A single-stage GS breakdown)
  - tnpsc_group4.json (General Tamil + GS + Aptitude)
  - trb.json (Physics, Chemistry, Pedagogy for PG Assistant)
  - banking.json (Reasoning, Quant, English, GA, Computer)
  - Schema: exam_code, exam_name, name_tamil, stages, subjects[topics[subtopics]]
    with estimated_hours, priority, frequently_asked, weightage, official PDF URL
- modules/syllabus_manager.py: SyllabusManager class
  - Dataclasses: Subtopic, Topic, Subject, Syllabus, MappedContent
  - SQLite schema: syllabus_coverage, content_map, question_topic_tags tables
  - _EXAM_FILE_MAP with normalisation (TNPSC, GROUP 1/2/2A/4, TRB, BANKING)
  - load_syllabus(exam_type) with in-memory cache
  - list_exams(), get_subjects(), get_topics(), find_topic() (fuzzy scoring)
  - syllabus_navigator(exam_type, language) → voice-guided overview
  - topics_voice_report(exam, subject_code) → top 10 topics with priority/hours
  - priority_topics(exam_type, limit) → scored high-weightage frequently-asked topics
  - priority_topics_voice(exam_type) → spoken summary
  - content_mapper(chunks, exam, subject, min_confidence, persist)
    - Token-level keyword match between chunk text and topic/subtopic names
    - Confidence scoring with best-match selection, persists to content_map table
    - Accepts Chunk dataclass or dict; graceful fallback
  - tag_question_to_topic(question_id, exam, topic, subtopic) → question_topic_tags
  - record_study(user_id, exam, subject, topic, subtopic, hours, questions_done)
    with ON CONFLICT upsert on syllabus_coverage
  - coverage_tracker(user_id, exam) → structured dict with per-subject and
    per-topic coverage_pct, planned vs studied hours, total syllabus %
  - coverage_voice_report(user_id, exam) → spoken progress report
  - extract_syllabus_pdf_metadata(pdf_path) using PyMuPDF fitz
    (page count, TOC, sample text, auto-detected unit/chapter/paper sections)
  - Module-level standalone functions: content_mapper, syllabus_navigator,
    coverage_tracker, priority_topics, extract_syllabus_pdf_metadata
- config/settings.py: Added SyllabusSettings (dir, db_path, min_mapping_confidence,
  priority_topics_limit); wired into AppSettings as `syllabus`
- modules/__init__.py: exports SyllabusManager, Syllabus, Subject, Topic, Subtopic,
  MappedContent, and all 5 standalone functions
- main.py: SyllabusManager wired in LearningAgent
  - Voice commands: "syllabus", "topics in <subject>", "priority topics",
    "go to <topic>", "coverage", "start studying <subject>"
  - _syllabus_exam_code() maps current_exam (TNPSC/TRB/Banking + Group tier)
    to syllabus exam codes
  - _navigate_to_topic(query) uses find_topic fuzzy matching, announces
    subject/priority/hours/subtopic count, records study event
  - _start_studying_subject(query) matches subject by name (English + Tamil),
    sets current_subject, speaks top 5 topics as navigable list

### [x] Step 14: Offline-first architecture with optional online sync
- modules/offline_sync.py: OfflineSyncManager class
  - Connectivity probes: check_internet_connection() tries Cloudflare/Google DNS
    and PIB over socket; ollama_available() probes localhost:11434;
    vosk_available() checks offline STT model dir
  - Dataclasses: SyncResult, DownloadJob
  - SQLite schema: sync_log, download_jobs, content_versions, backup_log
  - sync_current_affairs(last_sync_date) — gated by is_online(); delegates to
    CurrentAffairsManager.run_daily_update + add_to_rag_database; logs to sync_log
  - download_content_updates(exam_type, manifest_url|manifest) — differential
    sync: compares manifest versions against content_versions table, only downloads
    changed items; _download_with_resume() uses HTTP Range + If-Match headers,
    .part staging file, SHA-256 checksum verification, pauses on error
  - resume_pending_downloads() — picks up any paused/active jobs on reconnect
  - backup_user_progress(user_id) — tar.gz archive of users.db + questions.db +
    syllabus.db with manifest.json; SHA-256 checksum; entry in backup_log
  - restore_from_backup(user_id, backup_path) — extracts latest backup, restores
    DBs to configured paths, marks backup_log restored_at
  - Storage optimisation:
    - get_storage_usage() — data/vector_db/models/backups/cache directory sizes
      with pct_of_limit against 5 GB default
    - optimize_storage(max_backups, compress_older_than_days, prune) —
      rotates old backups, gzip-compresses cache files > N days old,
      optionally prunes content_versions unused > 90 days (requires user consent),
      cleans failed download_jobs older than 7 days
  - run_online_features_if_possible(online_fn, offline_fallback) —
    generic graceful degradation wrapper
  - Module-level standalone functions: check_internet_connection,
    sync_current_affairs, download_content_updates, backup_user_progress,
    restore_from_backup, optimize_storage
- config/settings.py: Added SyncSettings (db_path, backup_dir, cache_dir,
  storage_limit_bytes=5 GB, connectivity_timeout, max_backups_per_user,
  compress_older_than_days, prune_unused_days, content_manifest_url,
  background_sync_minutes); wired into AppSettings as `sync`
- modules/__init__.py: exports OfflineSyncManager, SyncResult, DownloadJob,
  check_internet_connection, ollama_available, vosk_available, and 5
  standalone sync functions
- main.py: OfflineSyncManager wired with shared CurrentAffairsManager/RAGModule/
  UserManager
  - self.is_online probed at startup with offline-mode log warning
  - Voice commands: "check connection", "sync", "backup", "restore",
    "storage", "optimize storage"
  - _report_connection_status() — spoken internet/Ollama/Vosk status
  - _run_sync() — guarded sync_current_affairs + resume_pending_downloads
  - _backup_progress() / _restore_progress() — voice-confirmed with
    destructive-action protection on restore
  - _report_storage_usage() / _optimize_storage() — voice-friendly MB/GB
    breakdown and spoken cleanup summary
  - _update_news() and current_affairs scheduler guarded by is_online();
    offline users get cached content with a clear spoken explanation

### [x] Step 15: Admin interface for content & question bank management
- modules/admin.py: AdminManager class
  - SQLite schema: admin_uploads (hash-indexed dedup tracking), question_flags,
    content_access_log
  - Dataclasses: PreviewChunk, UploadPreview, BulkResult
  - Document upload pipeline:
    - upload_document / process_and_preview: stage to data/uploads/,
      SHA-256 dedup, DocumentProcessor + SemanticChunker (same pipeline as RAG),
      auto-tag subject/topic via SyllabusManager.content_mapper if left "General",
      persist upload meta in 'pending' status, hold chunks in-memory until commit
    - commit_upload: generate embeddings + store_in_vectordb, persist
      content_map via syllabus_manager, mark upload committed
    - discard_upload: mark discarded, drop staged chunks
    - list_uploads(status) for dashboard
  - Content quality control:
    - _ocr_confidence_label (native/low/medium/high via char density per page)
    - _validate_tamil_text (heuristic combining-mark ratio check)
    - _find_duplicate (SHA-256 lookup against committed uploads)
    - find_duplicate_questions (case-insensitive text match on questions table)
  - Content CRUD (manage_content):
    - delete_source → RAGModule.delete_by_source (removes all chunks for a file)
    - delete_chunks → chroma collection.delete(ids=...)
    - update_metadata → merges new metadata into existing chunk's metadata
    - get_source_stats → count chunks per source
  - Question bank admin:
    - add_question, update_question (partial merge), delete_question (hard delete)
    - flag_question / list_flagged_questions / resolve_flag (review workflow)
    - bulk_upload_questions: CSV (csv.DictReader), XLSX (pandas + openpyxl), JSON
      with column validation against _REQUIRED_Q_COLS
  - Analytics (generate_content_report):
    - upload / question counts (per exam, per difficulty)
    - most-accessed topics from content_access_log
    - hot question topics with accuracy from user_performance
    - content gaps: syllabus topics with zero mapped chunks (cross-DB join)
    - log_content_access for view tracking
  - Module-level functions: upload_document, process_and_preview,
    bulk_upload_questions, manage_content, generate_content_report
- modules/admin_web.py: Flask web dashboard
  - Basic session-based auth with login_required decorator
  - Routes: /login, /logout, / (dashboard), /upload (+ commit/discard),
    /content (delete-source, update-metadata), /questions (list/filter/new/edit),
    /questions/<qid>/delete, /questions/<qid>/flag, /questions/bulk,
    /flags, /flags/<qid>/resolve, /report, /api/report.json
  - Inline HTML templates with shared CSS + nav; flash messages with categories
  - Preview page shows OCR confidence, duplicate warning, Tamil validation,
    first 10 chunks with token counts, commit/discard buttons
  - File upload size capped via MAX_CONTENT_LENGTH = max_upload_mb
  - main() CLI entry: python -m modules.admin_web [--host --port --debug]
- modules/admin_cli.py: Parallel CLI admin tool (for headless / accessibility)
  - Subcommands: upload, commit, discard, list-uploads, delete-source,
    bulk-questions, flag, resolve-flag, list-flags, duplicates, report
  - Interactive password prompt (reuses settings.admin credentials)
  - --no-auth flag for dev / scripted usage
- config/settings.py: Added AdminSettings
  (db_path, upload_dir, username, password, secret_key, web_host, web_port,
   preview_chunk_limit, max_upload_mb); wired into AppSettings as `admin`
- modules/__init__.py: exports AdminManager, UploadPreview, PreviewChunk,
  BulkResult + 5 module-level functions
- requirements.txt: added Flask>=3.0.0, Werkzeug>=3.0.0, pandas>=2.0.0,
  openpyxl>=3.1.0

### [x] Step 16: Visually impaired UX testing suite & accessibility optimisation
- modules/ux_testing.py: UXTestSuite class
  - SQLite schema: ux_feedback, ux_journey_runs, ux_command_tests
  - Dataclasses: CommandTestCase, CommandTestResult, JourneyStep, JourneyResult
  - Command test corpus:
    - Canonical phrases from COMMAND_VOCAB (self-match baseline)
    - _ACCENT_VARIATIONS (13 commands × 4–5 Indian-English accent/typo variants)
    - _NOISE_ARTEFACTS (light/moderate/heavy fillers, clipped tokens)
    - _CODE_MIXED (8 Tamil-English code-mixed phrases)
  - test_voice_commands(cases, persist) → accuracy report
    - Overall accuracy %, by_accent / by_noise / by_language breakdowns
    - Failure list (first 50) with canonical vs matched debugging info
    - Persisted per-sample to ux_command_tests for longitudinal tracking
  - simulate_user_journey(scenario, handler, persist) → JourneyResult
    - 4 prebuilt scenarios: start_topic_practice, ask_exam_question,
      error_recovery, daily_brief
    - Custom handler callback (defaults to command-matcher)
    - Per-step timing, error detection via expected_contains substring match
    - Run persisted to ux_journey_runs with full step log JSON
  - collect_user_feedback(session_id, kind, rating|yes_no|comment, prompt)
    - 4 kinds: clarity (yes/no), satisfaction (1-5), problem, rating
    - prompt=True triggers voice dialog in Tamil or English via wired VoiceModule
    - _extract_rating parses number words (EN + TA) and digits from speech
  - analyze_pain_points(limit_suggestions)
    - Command accuracy per canonical + worst-5 list
    - Noise-level accuracy impact breakdown
    - Scenario avg seconds + error rate
    - Unclear-explanation count from clarity feedback
    - Satisfaction avg + low-rating count
    - 10 recent problem reports
    - Auto-generated improvement suggestions based on thresholds
      (accent synonyms < 80%, noise < 70%, errors > 1/run, avg rating < 3.5)
  - accessibility_checklist() → 21 checks across 7 categories:
    - Interaction (voice-first menus, bilingual vocab, repeat, retries, confirms)
    - Audio (speed presets, inter-sentence pause, Tamil TTS, earcons)
    - STT (noise calibration, Vosk fallback, dynamic energy)
    - Content (simplifier, response length cap, difficulty levels)
    - Personalisation (language/speed/accessibility preferences)
    - Safety (destructive-action confirmation, backup/restore)
    - Feedback (voice feedback, problem reporting)
    - Returns pass_rate_pct + per-check detail
  - BETA_TESTER_GUIDELINES constant with 5 sections:
    target_profile, recruitment_channels, session_format, data_collected, ethics
  - _fallback_match() substring matcher used when VoiceModule isn't wired
  - Module-level standalone functions: test_voice_commands, simulate_user_journey,
    collect_user_feedback, analyze_pain_points, accessibility_checklist
- tests/__init__.py + tests/test_ux.py: pytest suite
  - Canonical phrase baseline test (≥95% self-match accuracy)
  - Full default corpus smoke test
  - Accent variant ≥70% accuracy regression guard
  - Journey simulation with prebuilt + custom handler + unknown-scenario raise
  - Feedback persistence across clarity / satisfaction / problem kinds
  - analyze_pain_points returns full structured report
  - Accessibility checklist structure + core VI-essential items passing
  - Beta guidelines completeness check
- modules/__init__.py: exports UXTestSuite, dataclasses, 5 standalone functions,
  and BETA_TESTER_GUIDELINES constant
- requirements.txt: added pytest>=7.4.0

### [x] Step 17: Wire UX testing hooks into main.py
- Imported UXTestSuite + uuid into main.py
- Instantiated self.ux = UXTestSuite(voice_module=self.voice) in LearningAgent.__init__
- Added self._session_id (uuid4) and self._questions_answered counter
- New voice commands:
  - "feedback" / "rate session" → _collect_session_feedback("satisfaction")
  - "report problem" → _collect_session_feedback("problem")
  - "ux report" → _deliver_ux_report() — spoken pain-point summary
  - "accessibility check" → _deliver_accessibility_check() — spoken a11y compliance %
- Automatic feedback hooks:
  - _answer_question(): clarity prompt every 5th question (silent_on_skip=True)
  - _start_practice(): satisfaction prompt after every practice session (silent_on_skip=True)
  - "stop"/"quit" command: satisfaction prompt before exiting (silent_on_skip=True)
  - KeyboardInterrupt handler: satisfaction prompt on Ctrl-C exit
- _collect_session_feedback(): voice-first with keyboard fallback, parses yes/no for
  clarity, numeric words ("one"–"five") for rating, free text for problem reports
- _deliver_ux_report(): command accuracy %, worst command, slowest journey, unclear
  explanations, avg satisfaction rating, top suggestion — all spoken aloud
- _deliver_accessibility_check(): pass/fail count, compliance %, failing item names
- COMMANDS dict updated with 4 new entries

### [x] Step 18: Missing function audit and deployment preparation
- Audited all cross-module function calls in main.py against module implementations
  - get_question(), find_duplicate_questions(), connectivity_report(), play_earcon(),
    match_command(), stop_scheduler(), run_daily_update(), get_stats(),
    topics_voice_report(), priority_topics_voice(), coverage_voice_report(),
    log_mock_score(), log_query(), run_topic_practice(), run_year_practice(),
    generate_mock_test(), run_random_practice() — all verified present
- Fixed requirements.txt: deduplicated chromadb entry, pinned chromadb>=0.4.22
- Fixed config/settings.py: added env_prefix="KURAL_" to AppSettings.Config
  to prevent OS environment variable collision (USER, LANGUAGE, etc.)
- Updated .env.example: all env vars now use KURAL_ prefix
- Python syntax check: all 17 module files + config + main.py + tests pass py_compile
- Config import verification: AppSettings instantiates cleanly with all 11 sub-models
- All cross-module imports verified structurally sound
