import json
import logging
from pathlib import Path
from datetime import datetime
from typing import List, Optional
from config import settings

logger = logging.getLogger(__name__)

SESSION_LOG_FILE = Path(settings.data_dir) / "sessions.json"


class DatabaseModule:
    def __init__(self):
        SESSION_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._sessions: List[dict] = self._load_sessions()

    def _load_sessions(self) -> List[dict]:
        if SESSION_LOG_FILE.exists():
            try:
                with open(SESSION_LOG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return []
        return []

    def _save_sessions(self) -> None:
        with open(SESSION_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(self._sessions, f, ensure_ascii=False, indent=2)

    def log_query(
        self,
        question: str,
        answer: str,
        language: str,
        exam_type: Optional[str] = None,
    ) -> None:
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "language": language,
            "exam_type": exam_type,
            "question": question,
            "answer": answer,
        }
        self._sessions.append(entry)
        self._save_sessions()
        logger.debug(f"Logged query: {question[:50]}...")

    def get_recent_sessions(self, limit: int = 10) -> List[dict]:
        return self._sessions[-limit:]

    def clear_sessions(self) -> None:
        self._sessions = []
        self._save_sessions()
        logger.info("Session history cleared")
