from pydantic_settings import BaseSettings
from pydantic import Field
from typing import List
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class LanguageSettings(BaseSettings):
    default_language: str = "en"
    supported_languages: List[str] = ["en", "ta"]
    tamil_tts_voice: str = "tamil"
    english_tts_voice: str = "english"


class ExamSettings(BaseSettings):
    supported_exams: List[str] = ["TNPSC", "TRB", "Banking"]
    default_exam: str = "TNPSC"
    exam_categories: dict = {
        "TNPSC": ["Group 1", "Group 2", "Group 4", "VAO"],
        "TRB": ["PG Assistant", "BT Assistant", "Polytechnic"],
        "Banking": ["IBPS PO", "IBPS Clerk", "SBI PO", "SBI Clerk"],
    }


class VoiceSettings(BaseSettings):
    tts_rate: int = 150
    tts_volume: float = 1.0
    stt_language: str = "en-IN"
    stt_tamil_language: str = "ta-IN"
    energy_threshold: int = 300
    pause_threshold: float = 0.8
    timeout: int = 10
    phrase_time_limit: int = 15

    speed_slow: int = 100
    speed_medium: int = 150
    speed_fast: int = 200
    default_speed: str = "medium"

    menu_max_retries: int = 3
    confirm_max_retries: int = 3
    noise_calibration_duration: float = 0.8

    vosk_model_en: str = "models/vosk-model-small-en-in-0.4"
    vosk_model_ta: str = "models/vosk-model-small-ta-0.4"

    gtts_tld: str = "co.in"
    inter_sentence_pause_ms: int = 400


class VectorDBSettings(BaseSettings):
    db_path: str = str(BASE_DIR / "vector_db")
    collection_name: str = "exam_content"
    embedding_model: str = "all-MiniLM-L6-v2"
    chunk_size: int = 500
    chunk_overlap: int = 50
    top_k_results: int = 5


class LLMSettings(BaseSettings):
    provider: str = "ollama"
    model_name: str = "llama3"
    base_url: str = "http://localhost:11434"
    temperature: float = 0.7
    max_tokens: int = 512
    max_response_words: int = 350
    ollama_timeout: int = 120
    fallback_to_context: bool = True
    default_difficulty: str = "medium"
    supported_difficulties: List[str] = ["beginner", "medium", "advanced"]


class NewsSettings(BaseSettings):
    db_path: str = str(BASE_DIR / "data" / "news.db")
    cache_dir: str = str(BASE_DIR / "data" / "news_cache")
    relevance_threshold: int = 6
    max_age_days: int = 7
    fetch_timeout_secs: int = 15
    daily_brief_max_items: int = 8
    quiz_default_count: int = 5
    rag_batch_size: int = 50
    scheduler_timezone: str = "Asia/Kolkata"
    daily_update_hour: int = 6
    weekly_compile_day: str = "sun"
    weekly_compile_hour: int = 18
    enabled_sources: List[str] = [
        "PIB India",
        "PIB Tamil Nadu",
        "The Hindu National",
        "The Hindu Tamil Nadu",
        "Indian Express India",
        "DD News India",
        "PIB Economy",
    ]


class UserSettings(BaseSettings):
    db_path: str = str(BASE_DIR / "data" / "users.db")
    default_user_id: str = "default_user"
    default_daily_study_hours: float = 2.0
    weak_accuracy_threshold: float = 0.60
    readiness_mock_weight: float = 0.35
    readiness_accuracy_weight: float = 0.40
    readiness_streak_weight: float = 0.15
    readiness_hours_weight: float = 0.10
    streak_target_days: int = 30
    study_hours_target: float = 40.0


class PracticeSettings(BaseSettings):
    db_path: str = str(BASE_DIR / "data" / "questions.db")
    default_user_id: str = "default_user"

    adaptive_min_attempts: int = 5
    adaptive_promote_threshold: float = 0.70
    adaptive_demote_threshold: float = 0.40
    weak_topic_threshold: float = 0.50

    answer_max_retries: int = 3
    option_pause_ms: int = 500
    post_question_pause_ms: int = 800

    mock_test_configs: dict = {
        "TNPSC": {
            "full": {"questions": 200, "duration_mins": 180},
            "mini": {"questions": 30,  "duration_mins": 30},
        },
        "TRB": {
            "full": {"questions": 150, "duration_mins": 180},
            "mini": {"questions": 25,  "duration_mins": 25},
        },
        "Banking": {
            "full": {"questions": 100, "duration_mins": 60},
            "mini": {"questions": 20,  "duration_mins": 20},
        },
    }
    timer_announce_interval_mins: int = 10
    timer_final_warning_mins: int = 5


class SyncSettings(BaseSettings):
    db_path: str = str(BASE_DIR / "data" / "sync.db")
    backup_dir: str = str(BASE_DIR / "data" / "backups")
    cache_dir: str = str(BASE_DIR / "data" / "sync_cache")
    storage_limit_bytes: int = 5 * 1024 * 1024 * 1024  # 5 GB
    connectivity_timeout_secs: float = 3.0
    max_backups_per_user: int = 3
    compress_older_than_days: int = 30
    prune_unused_days: int = 90
    content_manifest_url: str = ""  # optional remote manifest endpoint
    background_sync_minutes: int = 240  # 4 hours


class AdminSettings(BaseSettings):
    db_path: str = str(BASE_DIR / "data" / "admin.db")
    upload_dir: str = str(BASE_DIR / "data" / "uploads")
    username: str = "admin"
    password: str = "admin123"
    secret_key: str = "change-me-in-production"
    web_host: str = "127.0.0.1"
    web_port: int = 5055
    preview_chunk_limit: int = 10
    max_upload_mb: int = 50


class SyllabusSettings(BaseSettings):
    dir: str = str(BASE_DIR / "data" / "syllabi")
    db_path: str = str(BASE_DIR / "data" / "syllabus.db")
    min_mapping_confidence: float = 0.15
    priority_topics_limit: int = 10


class AppSettings(BaseSettings):
    language: LanguageSettings = Field(default_factory=LanguageSettings)
    exam: ExamSettings = Field(default_factory=ExamSettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    vector_db: VectorDBSettings = Field(default_factory=VectorDBSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    news: NewsSettings = Field(default_factory=NewsSettings)
    user: UserSettings = Field(default_factory=UserSettings)
    practice: PracticeSettings = Field(default_factory=PracticeSettings)
    syllabus: SyllabusSettings = Field(default_factory=SyllabusSettings)
    sync: SyncSettings = Field(default_factory=SyncSettings)
    admin: AdminSettings = Field(default_factory=AdminSettings)

    data_dir: str = str(BASE_DIR / "data")
    models_dir: str = str(BASE_DIR / "models")
    log_level: str = "INFO"

    class Config:
        env_file = str(BASE_DIR / ".env")
        env_nested_delimiter = "__"


settings = AppSettings()
