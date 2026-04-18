import json
import logging
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# RSS feed catalogue
# ---------------------------------------------------------------------------

_DEFAULT_FEEDS: List[Dict[str, str]] = [
    {
        "name": "PIB India",
        "url": "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3",
        "category_hint": "Polity",
    },
    {
        "name": "PIB Tamil Nadu",
        "url": "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=33",
        "category_hint": "Tamil Nadu",
    },
    {
        "name": "The Hindu National",
        "url": "https://www.thehindu.com/news/national/feeder/default.rss",
        "category_hint": "General",
    },
    {
        "name": "The Hindu Tamil Nadu",
        "url": "https://www.thehindu.com/news/national/tamil-nadu/feeder/default.rss",
        "category_hint": "Tamil Nadu",
    },
    {
        "name": "Indian Express India",
        "url": "https://indianexpress.com/section/india/feed/",
        "category_hint": "General",
    },
    {
        "name": "DD News India",
        "url": "https://ddnews.gov.in/en/rss.xml",
        "category_hint": "General",
    },
    {
        "name": "PIB Economy",
        "url": "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=5",
        "category_hint": "Economy",
    },
]

# ---------------------------------------------------------------------------
# Keyword tables for filtering + categorisation
# ---------------------------------------------------------------------------

_RELEVANCE_KEYWORDS: List[str] = [
    "scheme", "yojana", "mission", "policy", "act", "bill", "amendment",
    "appointed", "elected", "award", "prize", "ranked", "launched",
    "government", "ministry", "cabinet", "parliament", "supreme court",
    "rbi", "sebi", "niti aayog", "census", "gdp", "budget", "inflation",
    "isro", "drdo", "iit", "aiims", "defence", "satellite",
    "tamil nadu", "chennai", "tnpsc", "trb", "cm stalin", "kaveri",
    "unicef", "unesco", "who", "united nations", "g20", "brics",
    "climate", "tiger reserve", "national park", "biodiversity",
    "padma", "bharat ratna", "nobel", "oscar", "pulitzer",
    "bank", "loan", "insurance", "repo rate", "monetary policy",
    "disaster", "cyclone", "earthquake", "flood relief",
    "railway", "airport", "highway", "infrastructure",
]

_CATEGORY_RULES: Dict[str, List[str]] = {
    "Economy": [
        "gdp", "budget", "rbi", "reserve bank", "inflation", "fiscal deficit",
        "tax", "gst", "revenue", "finance ministry", "sebi", "repo rate",
        "monetary policy", "export", "import", "trade", "investment",
        "bank", "loan", "insurance", "stock market", "sensex", "nifty",
        "niti aayog", "economic survey", "industrial production",
    ],
    "Polity": [
        "parliament", "lok sabha", "rajya sabha", "constitution", "article",
        "amendment", "bill passed", "act", "ministry", "cabinet",
        "governor", "chief minister", "election commission", "president",
        "prime minister", "supreme court", "high court", "judiciary",
        "panchayat", "municipality", "local body", "voter",
    ],
    "Science & Technology": [
        "isro", "nasa", "drdo", "satellite", "launch", "space mission",
        "research", "discovery", "invention", "technology", "artificial intelligence",
        "iit", "innovation", "nuclear", "defence", "missile", "rocket",
        "patent", "vaccine", "drug", "medicine", "health scheme",
    ],
    "Tamil Nadu": [
        "tamil nadu", "chennai", "madurai", "coimbatore", "tirunelveli",
        "trichy", "salem", "erode", "vellore", "tnpsc", "dmk", "aiadmk",
        "cm stalin", "kaveri", "palar", "vaigai", "tamil", "tamilnadu",
        "tn government", "state government", "anna university",
    ],
    "Awards & Appointments": [
        "award", "prize", "honour", "conferred", "padma", "bharat ratna",
        "nobel prize", "oscar", "felicitated", "appointed", "elected",
        "named as", "takes charge", "new chief", "new director",
        "assumes office", "sworn in", "new governor", "new minister",
    ],
    "Environment": [
        "climate change", "global warming", "carbon", "renewable energy",
        "solar power", "wind energy", "pollution", "environment",
        "tiger reserve", "national park", "wildlife", "biodiversity",
        "forest", "mangrove", "coral reef", "green hydrogen",
    ],
    "International": [
        "united nations", "g20", "brics", "asean", "saarc",
        "bilateral", "treaty", "summit", "foreign minister",
        "diplomatic", "visa", "passport", "border", "ceasefire",
        "imf", "world bank", "wto", "cop",
    ],
    "Banking & Finance": [
        "rbi", "bank", "nbfc", "loan waiver", "credit card",
        "upi", "digital payment", "fintech", "ipo", "mutual fund",
        "insurance", "pension", "epfo", "ppf", "nps",
        "repo rate", "crr", "slr", "monetary policy committee",
    ],
}

_EXAM_TAG_MAP: Dict[str, List[str]] = {
    "Economy":              ["TNPSC", "Banking"],
    "Polity":               ["TNPSC", "TRB"],
    "Science & Technology": ["TNPSC", "TRB"],
    "Tamil Nadu":           ["TNPSC"],
    "Awards & Appointments":["TNPSC", "Banking"],
    "Environment":          ["TNPSC"],
    "International":        ["TNPSC", "Banking"],
    "Banking & Finance":    ["Banking", "TNPSC"],
    "General":              ["TNPSC"],
}

# ---------------------------------------------------------------------------
# Quiz question templates
# ---------------------------------------------------------------------------

_QUIZ_TEMPLATES: List[Dict[str, str]] = [
    {
        "pattern": r"(?P<person>[A-Z][a-z]+ [A-Z][a-z]+) (?:has been |was )?appointed (?:as |the )?(?P<role>[^.]+)",
        "question": "Who was recently appointed as {role}?",
        "answer_field": "person",
    },
    {
        "pattern": r"(?:India|India's|Indian) (?P<achievement>[^.]{10,60}) (?:launched|inaugurated|commissioned|unveiled)",
        "question": "What was recently launched / inaugurated by India?",
        "answer_field": "achievement",
    },
    {
        "pattern": r"(?P<scheme>[A-Z][A-Za-z ]{3,40}(?:Yojana|Mission|Scheme|Programme|Portal|App))",
        "question": "Which government scheme / programme was recently in the news?",
        "answer_field": "scheme",
    },
    {
        "pattern": r"(?P<person>[A-Z][a-z]+ [A-Z][a-z]+) (?:won|received|conferred) (?:the )?(?P<award>[A-Z][A-Za-z ]{3,40}(?:Award|Prize|Honour|Medal))",
        "question": "Who received the {award}?",
        "answer_field": "person",
    },
    {
        "pattern": r"Tamil Nadu (?P<event>[^.]{10,80})",
        "question": "What recent event related to Tamil Nadu was reported?",
        "answer_field": "event",
    },
]

# Distractor pool – plausible but wrong values
_DISTRACTOR_NAMES = [
    "Rajnath Singh", "S. Jaishankar", "Nirmala Sitharaman",
    "Piyush Goyal", "Dharmendra Pradhan", "Smriti Irani",
    "Ashwini Vaishnaw", "Kiren Rijiju", "Amit Shah",
    "Arvind Kejriwal", "Yogi Adityanath", "Mamata Banerjee",
]
_DISTRACTOR_SCHEMES = [
    "PM Kisan Samman Nidhi", "Ayushman Bharat", "Jal Jeevan Mission",
    "PM Awas Yojana", "Swachh Bharat Mission", "Digital India",
    "Skill India Mission", "Startup India", "Make in India",
    "National Education Policy", "PM Mudra Yojana", "PMGSY",
]
_DISTRACTOR_NUMBERS = ["₹5,000 crore", "₹10,000 crore", "₹25,000 crore", "₹50,000 crore"]

# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS news_items (
    news_id         TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    url             TEXT DEFAULT '',
    published_date  TEXT NOT NULL,
    source_name     TEXT NOT NULL,
    content         TEXT NOT NULL DEFAULT '',
    summary         TEXT NOT NULL DEFAULT '',
    category        TEXT NOT NULL DEFAULT 'General',
    exam_tags       TEXT NOT NULL DEFAULT '[]',
    relevance_score INTEGER NOT NULL DEFAULT 0,
    fetched_at      TEXT NOT NULL,
    ingested_to_rag INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_ni_date_cat
    ON news_items(published_date, category);
CREATE INDEX IF NOT EXISTS idx_ni_rag
    ON news_items(ingested_to_rag);

CREATE TABLE IF NOT EXISTS news_quiz (
    quiz_id         TEXT PRIMARY KEY,
    news_id         TEXT NOT NULL,
    question        TEXT NOT NULL,
    option_a        TEXT NOT NULL,
    option_b        TEXT NOT NULL,
    option_c        TEXT NOT NULL,
    option_d        TEXT NOT NULL,
    correct_answer  TEXT NOT NULL,
    explanation     TEXT NOT NULL DEFAULT '',
    category        TEXT DEFAULT 'General',
    created_at      TEXT NOT NULL,
    FOREIGN KEY (news_id) REFERENCES news_items(news_id)
);

CREATE INDEX IF NOT EXISTS idx_nq_date
    ON news_quiz(created_at);
"""

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class NewsItem:
    news_id: str
    title: str
    url: str
    published_date: str
    source_name: str
    content: str
    summary: str = ""
    category: str = "General"
    exam_tags: List[str] = field(default_factory=list)
    relevance_score: int = 0
    fetched_at: str = ""
    ingested_to_rag: bool = False


@dataclass
class QuizQuestion:
    quiz_id: str
    news_id: str
    question: str
    option_a: str
    option_b: str
    option_c: str
    option_d: str
    correct_answer: str
    explanation: str
    category: str
    created_at: str


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------


class CurrentAffairsManager:
    def __init__(
        self,
        db_path: Optional[str] = None,
        rag_module=None,
    ) -> None:
        self._db_path = Path(db_path or settings.news.db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._rag = rag_module
        self._scheduler = None
        self._init_db()
        logger.info(f"CurrentAffairsManager ready | db={self._db_path}")

    # ------------------------------------------------------------------
    # DB connection
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

    # ------------------------------------------------------------------
    # 1. Fetch news feeds
    # ------------------------------------------------------------------

    def fetch_news_feeds(
        self,
        sources: Optional[List[Dict[str, str]]] = None,
        max_age_days: int = 7,
    ) -> List[NewsItem]:
        try:
            import feedparser
        except ImportError:
            logger.error("feedparser not installed. Run: pip install feedparser")
            return []

        try:
            import requests as req
        except ImportError:
            req = None

        feeds = sources or _DEFAULT_FEEDS
        cutoff = datetime.utcnow() - timedelta(days=max_age_days)
        all_items: List[NewsItem] = []

        for feed_info in feeds:
            url = feed_info["url"]
            name = feed_info.get("name", url)
            category_hint = feed_info.get("category_hint", "General")
            try:
                logger.info(f"Fetching feed: {name}")
                parsed = feedparser.parse(url)
                if parsed.bozo and not parsed.entries:
                    logger.warning(f"Feed parse issue for {name}: {parsed.bozo_exception}")
                    continue

                for entry in parsed.entries:
                    item = self._parse_feed_entry(entry, name, category_hint, cutoff)
                    if item:
                        all_items.append(item)

                time.sleep(0.5)

            except Exception as e:
                logger.warning(f"Failed to fetch {name}: {e}")

        logger.info(f"Fetched {len(all_items)} raw news items from {len(feeds)} feeds")
        return all_items

    def _parse_feed_entry(
        self,
        entry: Any,
        source_name: str,
        category_hint: str,
        cutoff: datetime,
    ) -> Optional[NewsItem]:
        title = getattr(entry, "title", "").strip()
        if not title:
            return None

        url = getattr(entry, "link", "")
        content = ""
        if hasattr(entry, "summary"):
            content = re.sub(r"<[^>]+>", " ", entry.summary).strip()
        elif hasattr(entry, "description"):
            content = re.sub(r"<[^>]+>", " ", entry.description).strip()

        content = re.sub(r"\s{2,}", " ", content).strip()
        if not content:
            content = title

        pub_date = datetime.utcnow()
        for attr in ("published_parsed", "updated_parsed", "created_parsed"):
            parsed_time = getattr(entry, attr, None)
            if parsed_time:
                try:
                    pub_date = datetime(*parsed_time[:6])
                    break
                except Exception:
                    pass

        if pub_date < cutoff:
            return None

        return NewsItem(
            news_id=str(uuid.uuid4()),
            title=title,
            url=url,
            published_date=pub_date.date().isoformat(),
            source_name=source_name,
            content=content,
            fetched_at=datetime.utcnow().isoformat(),
            category=category_hint,
        )

    # ------------------------------------------------------------------
    # 2. Filter exam-relevant news
    # ------------------------------------------------------------------

    def filter_exam_relevant(
        self,
        news_items: List[NewsItem],
        min_score: int = None,
    ) -> List[NewsItem]:
        threshold = min_score if min_score is not None else settings.news.relevance_threshold
        filtered: List[NewsItem] = []
        for item in news_items:
            score = self._relevance_score(item)
            item.relevance_score = score
            if score >= threshold:
                filtered.append(item)

        filtered.sort(key=lambda x: x.relevance_score, reverse=True)
        logger.info(f"Filtered {len(filtered)}/{len(news_items)} items as exam-relevant")
        return filtered

    def _relevance_score(self, item: NewsItem) -> int:
        combined = (item.title + " " + item.content).lower()
        score = 0
        for kw in _RELEVANCE_KEYWORDS:
            if kw in combined:
                score += 2
        if "tamil nadu" in combined or "tnpsc" in combined:
            score += 5
        if any(w in combined for w in ("scheme", "yojana", "mission", "policy", "act", "bill")):
            score += 3
        if any(w in combined for w in ("appointed", "award", "prize", "launched", "inaugurated")):
            score += 3
        if any(w in combined for w in ("rbi", "isro", "drdo", "niti aayog")):
            score += 4
        return score

    # ------------------------------------------------------------------
    # 3. Summarize news item
    # ------------------------------------------------------------------

    def summarize_news(self, item: NewsItem) -> str:
        if item.summary:
            return item.summary

        ollama_summary = self._ollama_summarize(item)
        if ollama_summary:
            return ollama_summary

        return self._extractive_summarize(item)

    def _ollama_summarize(self, item: NewsItem) -> str:
        try:
            import ollama
            prompt = (
                f"Summarise the following news in exactly 2-3 sentences. "
                f"Focus only on facts relevant for government exam preparation "
                f"(TNPSC, banking, TRB). Use simple clear English. "
                f"Do not start with 'The article says' or similar.\n\n"
                f"Title: {item.title}\n"
                f"Content: {item.content[:800]}"
            )
            resp = ollama.chat(
                model=settings.llm.model_name,
                messages=[{"role": "user", "content": prompt}],
                options={
                    "temperature": 0.3,
                    "num_predict": 120,
                },
            )
            text = resp["message"]["content"].strip()
            if len(text.split()) >= 20:
                return text
        except Exception as e:
            logger.debug(f"Ollama summarise failed: {e}")
        return ""

    def _extractive_summarize(self, item: NewsItem) -> str:
        text = item.title + ". " + item.content
        text = re.sub(r"\s{2,}", " ", text).strip()

        sentences = re.split(r"(?<=[.!?])\s+", text)
        sentences = [s.strip() for s in sentences if len(s.split()) >= 5]

        if not sentences:
            return text[:300]

        scored = []
        kw_set = set(_RELEVANCE_KEYWORDS)
        for s in sentences:
            s_lower = s.lower()
            sc = sum(1 for kw in kw_set if kw in s_lower)
            scored.append((sc, s))

        scored.sort(reverse=True)
        top = [s for _, s in scored[:3]]

        seen: List[str] = []
        for s in top:
            if not any(s[:40] in prev for prev in seen):
                seen.append(s)

        return " ".join(seen[:3])

    # ------------------------------------------------------------------
    # 4. Categorise news item
    # ------------------------------------------------------------------

    def categorize_news(self, item: NewsItem) -> Tuple[str, List[str]]:
        combined = (item.title + " " + item.content).lower()
        scores: Dict[str, int] = {}

        for category, keywords in _CATEGORY_RULES.items():
            score = sum(1 for kw in keywords if kw in combined)
            if score > 0:
                scores[category] = score

        if not scores:
            category = item.category if item.category != "General" else "General"
            return category, _EXAM_TAG_MAP.get(category, ["TNPSC"])

        best_category = max(scores, key=scores.__getitem__)
        exam_tags = _EXAM_TAG_MAP.get(best_category, ["TNPSC"])
        return best_category, exam_tags

    # ------------------------------------------------------------------
    # 5. Process and store news (fetch → filter → summarise → categorise → save)
    # ------------------------------------------------------------------

    def run_daily_update(
        self,
        sources: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        logger.info("Starting daily current affairs update...")
        start = datetime.utcnow()

        raw = self.fetch_news_feeds(sources=sources)
        relevant = self.filter_exam_relevant(raw)

        new_count = 0
        for item in relevant:
            if self._is_duplicate(item):
                continue
            item.summary = self.summarize_news(item)
            item.category, item.exam_tags = self.categorize_news(item)
            self._save_news_item(item)
            new_count += 1

        elapsed = (datetime.utcnow() - start).total_seconds()
        logger.info(f"Daily update: {new_count} new items in {elapsed:.1f}s")

        return {
            "fetched": len(raw),
            "relevant": len(relevant),
            "new": new_count,
            "elapsed_seconds": round(elapsed, 1),
        }

    def _is_duplicate(self, item: NewsItem) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT news_id FROM news_items WHERE title=? AND published_date=?",
                (item.title, item.published_date),
            ).fetchone()
        return row is not None

    def _save_news_item(self, item: NewsItem) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO news_items
                   (news_id, title, url, published_date, source_name,
                    content, summary, category, exam_tags,
                    relevance_score, fetched_at, ingested_to_rag)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item.news_id, item.title, item.url, item.published_date,
                    item.source_name, item.content, item.summary,
                    item.category, json.dumps(item.exam_tags),
                    item.relevance_score, item.fetched_at, 0,
                ),
            )

    # ------------------------------------------------------------------
    # 6. Daily brief — audio script generation
    # ------------------------------------------------------------------

    def generate_daily_brief(
        self,
        brief_date: Optional[str] = None,
        max_items: int = 8,
        language: str = "en",
    ) -> str:
        target_date = brief_date or date.today().isoformat()

        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM news_items
                   WHERE published_date=?
                   ORDER BY relevance_score DESC
                   LIMIT ?""",
                (target_date, max_items),
            ).fetchall()

        if not rows:
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            with self._conn() as conn:
                rows = conn.execute(
                    """SELECT * FROM news_items
                       WHERE published_date >= ?
                       ORDER BY relevance_score DESC
                       LIMIT ?""",
                    (yesterday, max_items),
                ).fetchall()

        if not rows:
            return (
                f"Good morning. No current affairs updates are available for {target_date}. "
                "Please run a news update first by saying 'update news'."
            )

        items = [self._row_to_news_item(r) for r in rows]

        lines = [
            f"Good morning. Here is your current affairs brief for {target_date}.",
            f"We have {len(items)} important news items for your exam preparation.",
            "",
        ]

        category_groups: Dict[str, List[NewsItem]] = {}
        for item in items:
            category_groups.setdefault(item.category, []).append(item)

        item_num = 1
        for category, cat_items in category_groups.items():
            lines.append(f"{category}.")
            for item in cat_items:
                summary = item.summary or self._extractive_summarize(item)
                tags = ", ".join(item.exam_tags) if item.exam_tags else "TNPSC"
                lines.append(
                    f"Item {item_num}: {item.title}. "
                    f"{summary} "
                    f"This is relevant for: {tags}."
                )
                item_num += 1
            lines.append("")

        lines.append(
            "That concludes today's current affairs brief. "
            "Say 'current affairs quiz' to test yourself on today's news, "
            "or 'news about [topic]' for topic-specific updates."
        )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 7. Weekly compilation
    # ------------------------------------------------------------------

    def generate_weekly_compilation(self, language: str = "en") -> str:
        week_start = (date.today() - timedelta(days=7)).isoformat()

        with self._conn() as conn:
            rows = conn.execute(
                """SELECT category,
                          COUNT(*) AS cnt,
                          GROUP_CONCAT(title, ' || ') AS titles
                   FROM news_items
                   WHERE published_date >= ?
                   GROUP BY category
                   ORDER BY cnt DESC""",
                (week_start,),
            ).fetchall()

        if not rows:
            return "No news items found for the past week. Please run a news update first."

        total = sum(r["cnt"] for r in rows)
        lines = [
            f"Weekly current affairs compilation for the past 7 days.",
            f"A total of {total} exam-relevant news items were recorded.",
            "",
        ]

        for row in rows:
            lines.append(
                f"{row['category']}: {row['cnt']} item{'s' if row['cnt'] != 1 else ''}."
            )
            titles = [t.strip() for t in (row["titles"] or "").split("||")[:3]]
            for t in titles:
                if t:
                    lines.append(f"  — {t}.")

        lines.append("")
        lines.append(
            "To study any category in depth, say 'news about [topic]'. "
            "To take a quiz on this week's affairs, say 'current affairs quiz'."
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 8. Topic-wise filtering
    # ------------------------------------------------------------------

    def get_topic_news(
        self,
        topic: str,
        days: int = 7,
        limit: int = 5,
    ) -> Tuple[List[NewsItem], str]:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        topic_lower = topic.lower()

        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM news_items
                   WHERE published_date >= ?
                   ORDER BY relevance_score DESC""",
                (cutoff,),
            ).fetchall()

        matched: List[NewsItem] = []
        for r in rows:
            ni = self._row_to_news_item(r)
            haystack = (ni.title + " " + ni.content + " " + ni.category).lower()
            if topic_lower in haystack or any(w in haystack for w in topic_lower.split()):
                matched.append(ni)
            if len(matched) >= limit:
                break

        if not matched:
            voice_text = (
                f"I found no recent news about '{topic}' in the past {days} days. "
                "Try saying 'update news' to fetch the latest updates, "
                "or ask about a broader topic like 'economy' or 'Tamil Nadu'."
            )
            return [], voice_text

        lines = [
            f"Here are {len(matched)} recent news items about {topic} "
            f"from the past {days} days."
        ]
        for i, item in enumerate(matched, 1):
            summary = item.summary or self._extractive_summarize(item)
            lines.append(f"Item {i}: {item.title}. {summary}")

        lines.append(f"Source{'s' if len(matched) > 1 else ''}: " + ", ".join(
            {item.source_name for item in matched}
        ) + ".")
        return matched, "\n".join(lines)

    # ------------------------------------------------------------------
    # 9. Add processed news to RAG vector database
    # ------------------------------------------------------------------

    def add_to_rag_database(
        self,
        news_items: Optional[List[NewsItem]] = None,
        rag_module=None,
    ) -> Dict[str, Any]:
        rag = rag_module or self._rag
        if rag is None:
            logger.warning("No RAG module provided; skipping vector DB ingestion")
            return {"ingested": 0, "skipped": 0, "error": "No RAG module"}

        if news_items is None:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM news_items WHERE ingested_to_rag=0 ORDER BY published_date DESC LIMIT 100"
                ).fetchall()
            news_items = [self._row_to_news_item(r) for r in rows]

        if not news_items:
            return {"ingested": 0, "skipped": 0}

        from modules.chunker import Chunk

        ingested = 0
        skipped = 0

        for item in news_items:
            try:
                text = f"{item.title}\n\n{item.summary or item.content}"
                chunk = Chunk(
                    text=text,
                    metadata={
                        "source": item.source_name,
                        "exam_type": ",".join(item.exam_tags) if item.exam_tags else "TNPSC",
                        "subject": "Current Affairs",
                        "topic": item.category,
                        "language": "en",
                        "published_date": item.published_date,
                        "news_id": item.news_id,
                        "url": item.url,
                    },
                    chunk_index=0,
                    token_count=len(text.split()),
                )

                embeddings = rag.generate_embeddings([chunk])
                rag.store_in_vectordb(
                    [chunk],
                    embeddings,
                    metadata={"subject": "Current Affairs", "topic": item.category},
                )

                with self._conn() as conn:
                    conn.execute(
                        "UPDATE news_items SET ingested_to_rag=1 WHERE news_id=?",
                        (item.news_id,),
                    )
                ingested += 1

            except Exception as e:
                logger.error(f"RAG ingestion failed for {item.news_id}: {e}")
                skipped += 1

        logger.info(f"RAG ingestion: {ingested} ingested, {skipped} skipped")
        return {"ingested": ingested, "skipped": skipped}

    # ------------------------------------------------------------------
    # 10. Quiz generation from current affairs
    # ------------------------------------------------------------------

    def create_current_affairs_quiz(
        self,
        time_period: str = "week",
        count: int = 5,
        category: Optional[str] = None,
    ) -> Tuple[List[QuizQuestion], str]:
        days = {"day": 1, "week": 7, "month": 30}.get(time_period, 7)
        cutoff = (date.today() - timedelta(days=days)).isoformat()

        query = "SELECT * FROM news_items WHERE published_date >= ? AND relevance_score >= ?"
        params: List[Any] = [cutoff, settings.news.relevance_threshold]
        if category:
            query += " AND category=?"
            params.append(category)
        query += " ORDER BY relevance_score DESC"

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()

        items = [self._row_to_news_item(r) for r in rows]

        existing_ids: set = set()
        with self._conn() as conn:
            ex_rows = conn.execute(
                "SELECT news_id FROM news_quiz WHERE created_at >= ?", (cutoff,)
            ).fetchall()
            existing_ids = {r["news_id"] for r in ex_rows}

        new_items = [i for i in items if i.news_id not in existing_ids]

        questions: List[QuizQuestion] = []
        for item in new_items:
            q = self._generate_mcq(item)
            if q:
                self._save_quiz_question(q)
                questions.append(q)
            if len(questions) >= count:
                break

        if not questions:
            with self._conn() as conn:
                qrows = conn.execute(
                    """SELECT nq.*, ni.category
                       FROM news_quiz nq
                       JOIN news_items ni ON nq.news_id = ni.news_id
                       WHERE nq.created_at >= ?
                       ORDER BY nq.created_at DESC
                       LIMIT ?""",
                    (cutoff, count),
                ).fetchall()
            questions = [self._row_to_quiz(r) for r in qrows]

        if not questions:
            return [], (
                "No quiz questions are available for current affairs. "
                "Please update the news first by saying 'update news'."
            )

        voice_script = self._quiz_to_voice(questions)
        return questions, voice_script

    def _generate_mcq(self, item: NewsItem) -> Optional[QuizQuestion]:
        text = item.title + ". " + (item.summary or item.content)
        correct_answer = ""
        question_text = ""
        distractors: List[str] = []

        for tmpl in _QUIZ_TEMPLATES:
            m = re.search(tmpl["pattern"], text)
            if m:
                answer_field = tmpl["answer_field"]
                try:
                    correct_answer = m.group(answer_field).strip()
                    question_text = tmpl["question"].format(**{
                        k: m.group(k) for k in m.groupdict()
                    })
                    break
                except Exception:
                    continue

        if not correct_answer or len(correct_answer.split()) < 1:
            question_text = (
                f"Based on recent news: '{item.title[:80]}'. "
                "Which of the following statements is correct?"
            )
            correct_answer = (item.summary or item.content)[:120].strip()
            if not correct_answer:
                return None
            distractors = _DISTRACTOR_SCHEMES[:3]
        else:
            if any(c.isupper() for c in correct_answer[:2]):
                distractors = [d for d in _DISTRACTOR_NAMES if d != correct_answer][:3]
            else:
                distractors = [d for d in _DISTRACTOR_SCHEMES if d != correct_answer][:3]

        while len(distractors) < 3:
            distractors.append(_DISTRACTOR_NUMBERS[len(distractors) % len(_DISTRACTOR_NUMBERS)])

        import random
        options = [correct_answer] + distractors[:3]
        random.shuffle(options)
        correct_letter = chr(ord("A") + options.index(correct_answer))

        return QuizQuestion(
            quiz_id=str(uuid.uuid4()),
            news_id=item.news_id,
            question=question_text,
            option_a=options[0],
            option_b=options[1],
            option_c=options[2],
            option_d=options[3],
            correct_answer=correct_letter,
            explanation=(
                f"According to {item.source_name} on {item.published_date}: "
                f"{item.summary or item.title}."
            ),
            category=item.category,
            created_at=datetime.utcnow().isoformat(),
        )

    def _save_quiz_question(self, q: QuizQuestion) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO news_quiz
                   (quiz_id, news_id, question, option_a, option_b, option_c, option_d,
                    correct_answer, explanation, category, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    q.quiz_id, q.news_id, q.question,
                    q.option_a, q.option_b, q.option_c, q.option_d,
                    q.correct_answer, q.explanation, q.category, q.created_at,
                ),
            )

    def _quiz_to_voice(self, questions: List[QuizQuestion]) -> str:
        lines = [
            f"Current affairs quiz. {len(questions)} question{'s' if len(questions) != 1 else ''}.",
            "Listen carefully and say the option letter when ready.",
            "",
        ]
        for i, q in enumerate(questions, 1):
            lines.append(f"Question {i}: {q.question}")
            lines.append(f"Option A: {q.option_a}.")
            lines.append(f"Option B: {q.option_b}.")
            lines.append(f"Option C: {q.option_c}.")
            lines.append(f"Option D: {q.option_d}.")
            lines.append(f"[The correct answer is option {q.correct_answer}. {q.explanation}]")
            lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 11. Scheduling (APScheduler)
    # ------------------------------------------------------------------

    def start_scheduler(self) -> bool:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
        except ImportError:
            logger.warning("apscheduler not installed. Scheduling unavailable. Run: pip install apscheduler")
            return False

        if self._scheduler and self._scheduler.running:
            logger.info("Scheduler already running")
            return True

        self._scheduler = BackgroundScheduler(timezone="Asia/Kolkata")

        self._scheduler.add_job(
            func=self._scheduled_daily_update,
            trigger=CronTrigger(hour=6, minute=0),
            id="daily_news_update",
            name="Daily current affairs update",
            replace_existing=True,
            misfire_grace_time=3600,
        )

        self._scheduler.add_job(
            func=self._scheduled_weekly_compilation,
            trigger=CronTrigger(day_of_week="sun", hour=18, minute=0),
            id="weekly_news_compilation",
            name="Weekly current affairs compilation",
            replace_existing=True,
            misfire_grace_time=3600,
        )

        self._scheduler.start()
        logger.info(
            "Scheduler started: daily update at 06:00 IST, "
            "weekly compilation on Sundays at 18:00 IST"
        )
        return True

    def stop_scheduler(self) -> None:
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

    def _scheduled_daily_update(self) -> None:
        logger.info("Scheduled daily news update triggered")
        try:
            result = self.run_daily_update()
            if self._rag and result["new"] > 0:
                self.add_to_rag_database(rag_module=self._rag)
            logger.info(f"Scheduled update done: {result}")
        except Exception as e:
            logger.error(f"Scheduled daily update failed: {e}")

    def _scheduled_weekly_compilation(self) -> None:
        logger.info("Scheduled weekly compilation triggered")
        try:
            script = self.generate_weekly_compilation()
            summary_path = Path(settings.news.cache_dir) / f"weekly_{date.today().isoformat()}.txt"
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(script, encoding="utf-8")
            logger.info(f"Weekly compilation saved: {summary_path}")
        except Exception as e:
            logger.error(f"Scheduled weekly compilation failed: {e}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_news_item(row: sqlite3.Row) -> NewsItem:
        return NewsItem(
            news_id=row["news_id"],
            title=row["title"],
            url=row["url"] or "",
            published_date=row["published_date"],
            source_name=row["source_name"],
            content=row["content"] or "",
            summary=row["summary"] or "",
            category=row["category"] or "General",
            exam_tags=json.loads(row["exam_tags"] or "[]"),
            relevance_score=row["relevance_score"] or 0,
            fetched_at=row["fetched_at"] or "",
            ingested_to_rag=bool(row["ingested_to_rag"]),
        )

    @staticmethod
    def _row_to_quiz(row: sqlite3.Row) -> QuizQuestion:
        return QuizQuestion(
            quiz_id=row["quiz_id"],
            news_id=row["news_id"],
            question=row["question"],
            option_a=row["option_a"],
            option_b=row["option_b"],
            option_c=row["option_c"],
            option_d=row["option_d"],
            correct_answer=row["correct_answer"],
            explanation=row["explanation"] or "",
            category=row["category"] or "General",
            created_at=row["created_at"],
        )

    def get_stats(self) -> Dict[str, Any]:
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM news_items").fetchone()[0]
            not_ingested = conn.execute(
                "SELECT COUNT(*) FROM news_items WHERE ingested_to_rag=0"
            ).fetchone()[0]
            quiz_count = conn.execute("SELECT COUNT(*) FROM news_quiz").fetchone()[0]
            today_count = conn.execute(
                "SELECT COUNT(*) FROM news_items WHERE published_date=?",
                (date.today().isoformat(),),
            ).fetchone()[0]
        return {
            "total_news_items": total,
            "pending_rag_ingestion": not_ingested,
            "quiz_questions": quiz_count,
            "items_today": today_count,
        }


# ---------------------------------------------------------------------------
# Module-level standalone functions
# ---------------------------------------------------------------------------

_default_manager: Optional[CurrentAffairsManager] = None


def _get_manager(rag_module=None) -> CurrentAffairsManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = CurrentAffairsManager(rag_module=rag_module)
    elif rag_module and _default_manager._rag is None:
        _default_manager._rag = rag_module
    return _default_manager


def fetch_news_feeds(
    sources: Optional[List[Dict[str, str]]] = None,
) -> List[NewsItem]:
    return _get_manager().fetch_news_feeds(sources=sources)


def filter_exam_relevant(
    news_items: List[NewsItem],
    min_score: int = None,
) -> List[NewsItem]:
    return _get_manager().filter_exam_relevant(news_items, min_score)


def summarize_news(news_item: NewsItem) -> str:
    return _get_manager().summarize_news(news_item)


def categorize_news(news_item: NewsItem) -> Tuple[str, List[str]]:
    return _get_manager().categorize_news(news_item)


def generate_daily_brief(
    brief_date: Optional[str] = None,
    language: str = "en",
) -> str:
    return _get_manager().generate_daily_brief(brief_date=brief_date, language=language)


def add_to_rag_database(
    news_items: Optional[List[NewsItem]] = None,
    rag_module=None,
) -> Dict[str, Any]:
    return _get_manager(rag_module).add_to_rag_database(news_items, rag_module)


def create_current_affairs_quiz(
    time_period: str = "week",
    count: int = 5,
    category: Optional[str] = None,
) -> Tuple[List[QuizQuestion], str]:
    return _get_manager().create_current_affairs_quiz(time_period, count, category)
