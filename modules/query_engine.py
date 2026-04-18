import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Intent detection vocabulary
# ---------------------------------------------------------------------------

_INTENT_KEYWORDS: Dict[str, List[str]] = {
    "study": [
        "what is", "what are", "tell me about", "describe", "define",
        "who is", "who was", "when did", "where is", "how many", "history of",
        "என்ன", "யார்", "எப்போது", "எங்கே", "எத்தனை", "வரலாறு",
    ],
    "practice": [
        "give me a question", "quiz me", "test me", "practice question",
        "mock question", "sample question", "question about", "ask me",
        "தேர்வு கேள்வி", "கேள்வி கொடு", "கேட்கவும்",
    ],
    "doubt": [
        "why", "how does", "how do", "i don't understand", "confused",
        "difference between", "what's the difference", "versus", " vs ",
        "ஏன்", "எப்படி", "புரியவில்லை", "வித்தியாசம்", "குழப்பம்",
    ],
    "explain": [
        "explain", "step by step", "break down", "in detail", "how to",
        "process of", "mechanism of", "stages of", "steps of",
        "விளக்கு", "படி படியாக", "விரிவாக",
    ],
}

# ---------------------------------------------------------------------------
# Visual reference patterns to replace for accessibility
# ---------------------------------------------------------------------------

_VISUAL_REPLACEMENTS: List[Tuple[str, str]] = [
    (r"\bas you can see\b",                         "as described"),
    (r"\blook at\b",                                 "consider"),
    (r"\bin the figure\b",                           "in the example"),
    (r"\bin the diagram\b",                          "in the description"),
    (r"\bthe table (?:shows|below|above)\b",         "the data"),
    (r"\bthe chart (?:shows|below|above)\b",         "the information"),
    (r"\bsee figure\b",                              "note that"),
    (r"\bfigure\s+\d+\b",                            "the example"),
    (r"\btable\s+\d+\b",                             "the following data"),
    (r"\bas shown (?:in|above|below)\b",             "as described"),
    (r"\bhighlighted in\b",                          "mentioned in"),
    (r"\bin (?:red|blue|green|yellow|bold|italic)\b", ""),
    (r"\bunderlined\b",                              "emphasized"),
    (r"\bthe above (?:figure|diagram|table|chart|image|graph)\b",
     "what was previously described"),
    (r"\bthe (?:following|below) (?:figure|diagram|table|chart|image|graph)\b",
     "the following"),
    (r"\bclick (?:here|on)\b",                       "select"),
    (r"\bscroll (?:down|up|to)\b",                   "move to"),
    (r"\bsee the\b",                                 "note the"),
    (r"\bobserve (?:that|the)\b",                    "note that"),
    (r"\bthe image (?:shows|depicts|illustrates)\b", "the information describes"),
    (r"\bvisually\b",                                "clearly"),
    (r"\bnotice (?:that|how)\b",                     "note that"),
    (r"\bas illustrated\b",                          "as described"),
]

# ---------------------------------------------------------------------------
# Markdown cleanup patterns for audio
# ---------------------------------------------------------------------------

_MARKDOWN_CLEANUPS: List[Tuple[str, str]] = [
    (r"\*\*(.+?)\*\*",    r"\1"),
    (r"\*(.+?)\*",         r"\1"),
    (r"__(.+?)__",         r"\1"),
    (r"_(.+?)_",           r"\1"),
    (r"#{1,6}\s+(.+)",     r"\1"),
    (r"\[(.+?)\]\(.+?\)",  r"\1"),
    (r"`(.+?)`",           r"\1"),
    (r"```[\s\S]+?```",    ""),
    (r">\s+(.+)",          r"\1"),
    (r"\|.+\|\n[-|]+\n",   ""),
    (r"\|",                " "),
]

_BULLET_LINE = re.compile(r"^[\s]*[-•*]\s+(.+)", re.MULTILINE)

# ---------------------------------------------------------------------------
# Difficulty modifiers
# ---------------------------------------------------------------------------

_DIFFICULTY_MODIFIERS: Dict[str, str] = {
    "beginner": (
        "Use very simple language. Avoid all technical jargon. "
        "Use familiar everyday examples from Tamil Nadu village life — farming, temples, "
        "bus journeys, or market visits. Speak as if explaining to someone hearing this "
        "topic for the very first time."
    ),
    "medium": (
        "Use clear language with standard exam terminology. "
        "Give practical examples relevant to Tamil Nadu. "
        "Assume the student has basic familiarity with the subject area."
    ),
    "advanced": (
        "Use precise technical and legal terminology as expected in the exam. "
        "Provide in-depth analysis with multiple perspectives. "
        "Connect this concept to related topics and highlight commonly tested exam angles."
    ),
}

# ---------------------------------------------------------------------------
# Prompts (English and Tamil)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_EN = """\
You are a patient, encouraging teacher helping visually impaired students prepare \
for Tamil Nadu government exams ({exam_type}). The student is studying: {subject}.

STRICT RULES — follow every one:
1. Your response will be read aloud. Write for ears, not eyes.
2. Never use visual phrases: "as you can see", "look at", "in the figure", \
"the table shows", "see diagram", "observe", "notice that", "as illustrated".
3. Replace bullet lists with numbered speech: "First... Second... Third..."
4. Use analogies from Tamil Nadu everyday life (paddy farming, panchayat, temple festival, \
auto-rickshaw, ration shop).
5. Stay under {max_words} words — the student is listening, not reading.
6. End with exactly one sentence starting with "To remember: "
7. Be warm and encouraging. Start with "Good question!" or "This is important for your exam."

Study material retrieved:
{context}

{intent_instruction}

{difficulty_modifier}"""

_SYSTEM_PROMPT_TA = """\
நீங்கள் தமிழ்நாடு அரசு தேர்வுகளுக்கு ({exam_type}) தயாராகும் பார்வையற்ற மாணவர்களுக்கு \
உதவும் பொறுமையான, ஊக்கமளிக்கும் ஆசிரியர். மாணவர் படிக்கும் பாடம்: {subject}.

கட்டாயமாக பின்பற்ற வேண்டிய விதிகள்:
1. பதில் குரலில் படிக்கப்படும். காதுகளுக்காக எழுதுங்கள்.
2. பார்வை சார்ந்த வார்த்தைகள் வேண்டவே வேண்டாம்: "பாருங்கள்", "படத்தில்", \
"அட்டவணையில்", "கவனியுங்கள்".
3. பட்டியல்களை எண்ணிட்ட படிகளாக மாற்றுங்கள்: "முதலாவதாக... இரண்டாவதாக..."
4. தமிழ்நாடு அன்றாட வாழ்க்கையிலிருந்து உதாரணங்கள் பயன்படுத்துங்கள்.
5. {max_words} வார்த்தைகளுக்கு குறைவாக வைத்திருங்கள்.
6. "நினைவில் வைக்க: " என்று தொடங்கும் ஒரு வரியுடன் முடியுங்கள்.
7. "நல்ல கேள்வி!" அல்லது "இது தேர்வில் முக்கியமானது." என்று தொடங்குங்கள்.

படிப்பு பொருள்:
{context}

{intent_instruction}

{difficulty_modifier}"""

_INTENT_INSTRUCTIONS: Dict[str, Dict[str, str]] = {
    "en": {
        "study": (
            "Explain this concept clearly. Give a simple definition first, "
            "then 2 to 3 key points, then one easy-to-remember example."
        ),
        "practice": (
            "Create ONE multiple-choice question with 4 options labelled A, B, C, D. "
            "Read all four options clearly. Then say 'Take a moment to think about it.' "
            "After a pause, give the correct answer letter, then explain why it is correct "
            "in 2 sentences."
        ),
        "doubt": (
            "The student is confused. Start by validating the confusion — many students "
            "find this tricky. Then clarify step by step. If the question asks for a "
            "difference, compare both concepts point by point using 'On one hand... "
            "On the other hand...'"
        ),
        "explain": (
            "Give a numbered step-by-step explanation. Number every step clearly. "
            "After all steps, give one practical analogy. Close with a one-sentence summary."
        ),
        "unknown": (
            "Answer the question helpfully and concisely. Connect it to the exam context "
            "where possible."
        ),
    },
    "ta": {
        "study": (
            "இந்த கருத்தை தெளிவாக விளக்குங்கள். முதலில் எளிய வரையறை, "
            "பின் 2-3 முக்கிய புள்ளிகள், கடைசியில் ஒரு உதாரணம்."
        ),
        "practice": (
            "A, B, C, D என்ற 4 விருப்பங்களுடன் ஒரு பல்லுத்தர கேள்வி உருவாக்குங்கள். "
            "'சிறிது நேரம் சிந்தியுங்கள்' என்று சொல்லுங்கள். "
            "பின் சரியான விடையை ஏன் என்ற விளக்கத்துடன் கொடுங்கள்."
        ),
        "doubt": (
            "மாணவருக்கு குழப்பம் உள்ளது. முதலில் அந்த குழப்பம் இயல்பானது என்று சொல்லுங்கள். "
            "பின் படிப்படியாக தெளிவுபடுத்துங்கள். ஒப்பீடு தேவையெனில் "
            "'ஒருபுறம்... மறுபுறம்...' வடிவத்தில் விளக்குங்கள்."
        ),
        "explain": (
            "எண்ணிட்ட படிகளாக விளக்கம் கொடுங்கள். "
            "அனைத்து படிகளுக்கும் பின் ஒரு நடைமுறை உதாரணம் தாருங்கள். "
            "ஒரு வரி சுருக்கத்துடன் முடியுங்கள்."
        ),
        "unknown": (
            "கேள்விக்கு பயனுள்ள, சுருக்கமான பதில் கொடுங்கள். "
            "தேர்வு சூழலுடன் இணைத்து பதிலளியுங்கள்."
        ),
    },
}

_NO_CONTEXT_NOTE: Dict[str, str] = {
    "en": (
        "Note: No specific study material was found for this topic in the knowledge base. "
        "Answering from general knowledge."
    ),
    "ta": (
        "குறிப்பு: இந்த தலைப்பிற்கு குறிப்பிட்ட படிப்பு பொருள் இல்லை. "
        "பொது அறிவிலிருந்து பதிலளிக்கிறேன்."
    ),
}

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class RetrievedChunk:
    text: str
    source: str
    subject: str
    topic: str
    page: Any
    score: float
    exam_type: str = ""
    language: str = "en"


@dataclass
class QueryResult:
    answer: str
    intent: str
    language: str = "en"
    sources: List[Dict[str, Any]] = field(default_factory=list)
    citation: str = ""
    word_count: int = 0
    context_used: bool = False
    raw_llm_response: str = ""


# ---------------------------------------------------------------------------
# QueryEngine
# ---------------------------------------------------------------------------


class QueryEngine:
    def __init__(self, rag_module=None) -> None:
        self._rag = rag_module
        self._embedding_model = None

    # ------------------------------------------------------------------
    # Lazy accessors
    # ------------------------------------------------------------------

    def _get_rag(self):
        if self._rag is None:
            from modules.rag import RAGModule
            self._rag = RAGModule()
        return self._rag

    def _get_embedding_model(self):
        if self._embedding_model is None:
            from sentence_transformers import SentenceTransformer
            logger.info(f"Loading embedding model: {settings.vector_db.embedding_model}")
            self._embedding_model = SentenceTransformer(
                settings.vector_db.embedding_model
            )
        return self._embedding_model

    def _call_ollama(
        self,
        system_prompt: str,
        user_message: str,
    ) -> str:
        try:
            import ollama
            response = ollama.chat(
                model=settings.llm.model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                options={
                    "temperature": settings.llm.temperature,
                    "num_predict": settings.llm.max_tokens,
                },
            )
            return response["message"]["content"].strip()
        except Exception as e:
            logger.error(f"Ollama call failed ({settings.llm.model_name}): {e}")
            raise

    # ------------------------------------------------------------------
    # Public: intent detection
    # ------------------------------------------------------------------

    def detect_intent(self, text: str) -> str:
        lowered = text.lower()
        scores: Dict[str, int] = {intent: 0 for intent in _INTENT_KEYWORDS}
        for intent, keywords in _INTENT_KEYWORDS.items():
            for kw in keywords:
                if kw in lowered:
                    scores[intent] += 1
        best = max(scores, key=lambda k: scores[k])
        detected = best if scores[best] > 0 else "unknown"
        logger.debug(f"Intent detected: '{detected}' for query: '{text[:60]}'")
        return detected

    # ------------------------------------------------------------------
    # Public: retrieve_context (accepts pre-computed embedding)
    # ------------------------------------------------------------------

    def retrieve_context(
        self,
        query_embedding: List[float],
        filters: Optional[Dict[str, str]] = None,
        top_k: Optional[int] = None,
    ) -> List[RetrievedChunk]:
        k = top_k or settings.vector_db.top_k_results
        collection = self._get_rag()._vector_store._collection

        query_kwargs: Dict[str, Any] = {
            "query_embeddings": [query_embedding],
            "n_results": min(k, max(1, collection.count())),
            "include": ["documents", "metadatas", "distances"],
        }
        if filters:
            query_kwargs["where"] = filters

        try:
            results = collection.query(**query_kwargs)
        except Exception as e:
            logger.error(f"ChromaDB query failed: {e}")
            return []

        chunks: List[RetrievedChunk] = []
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        dists = results.get("distances", [[]])[0]

        for text, meta, dist in zip(docs, metas, dists):
            similarity = round(max(0.0, 1.0 - float(dist)), 4)
            chunks.append(
                RetrievedChunk(
                    text=text,
                    source=meta.get("source", "unknown"),
                    subject=meta.get("subject", ""),
                    topic=meta.get("topic", ""),
                    page=meta.get("page", "N/A"),
                    score=similarity,
                    exam_type=meta.get("exam_type", ""),
                    language=meta.get("language", "en"),
                )
            )

        logger.info(
            f"Retrieved {len(chunks)} chunks "
            f"(top score: {chunks[0].score if chunks else 'N/A'})"
        )
        return chunks

    # ------------------------------------------------------------------
    # Public: generate_response
    # ------------------------------------------------------------------

    def generate_response(
        self,
        query: str,
        context: str,
        language_preference: str = "en",
        intent: str = "study",
        exam_type: Optional[str] = None,
        subject: str = "General",
        difficulty: str = "medium",
    ) -> str:
        exam = exam_type or settings.exam.default_exam
        lang = language_preference if language_preference in ("en", "ta") else "en"
        diff_key = difficulty if difficulty in _DIFFICULTY_MODIFIERS else "medium"
        intent_key = intent if intent in _INTENT_INSTRUCTIONS[lang] else "unknown"

        prompt_template = _SYSTEM_PROMPT_EN if lang == "en" else _SYSTEM_PROMPT_TA

        system_prompt = prompt_template.format(
            exam_type=exam,
            subject=subject,
            max_words=settings.llm.max_response_words,
            context=context or _NO_CONTEXT_NOTE[lang],
            intent_instruction=_INTENT_INSTRUCTIONS[lang][intent_key],
            difficulty_modifier=_DIFFICULTY_MODIFIERS[diff_key],
        )

        try:
            raw = self._call_ollama(system_prompt, query)
            logger.info(f"LLM response: {len(raw.split())} words")
            return raw
        except Exception as e:
            logger.warning(f"LLM unavailable, returning context fallback: {e}")
            if settings.llm.fallback_to_context and context:
                return (
                    "I could not reach the language model right now. "
                    "Here is what I found in your study materials: " + context[:600]
                )
            return (
                "I am unable to generate a response at the moment. "
                "Please ensure Ollama is running with: ollama serve"
            )

    # ------------------------------------------------------------------
    # Public: query_rag (full pipeline)
    # ------------------------------------------------------------------

    def query_rag(
        self,
        user_question: str,
        exam_type: Optional[str] = None,
        subject: Optional[str] = None,
        language: Optional[str] = None,
        difficulty: Optional[str] = None,
        top_k: Optional[int] = None,
    ) -> QueryResult:
        exam = exam_type or settings.exam.default_exam
        subj = subject or "General"
        diff = difficulty or settings.llm.default_difficulty

        from modules.voice import detect_language as _detect_lang
        detected_lang = _detect_lang(user_question)
        lang = language or ("ta" if detected_lang == "tamil" else "en")

        intent = self.detect_intent(user_question)
        logger.info(f"query_rag | intent={intent} lang={lang} exam={exam} subject={subj}")

        model = self._get_embedding_model()
        query_embedding: List[float] = model.encode(
            user_question, normalize_embeddings=True
        ).tolist()

        filters: Dict[str, str] = {"exam_type": exam}
        if subj and subj != "General":
            filters["subject"] = subj

        chunks = self.retrieve_context(query_embedding, filters=filters, top_k=top_k)

        if not chunks and subj != "General":
            logger.info("No chunks with subject filter, retrying without subject filter")
            chunks = self.retrieve_context(
                query_embedding, filters={"exam_type": exam}, top_k=top_k
            )

        context_used = bool(chunks)
        context_text = self._build_context_text(chunks)
        citation = self._build_citation(chunks, lang)
        sources = [
            {
                "source": c.source,
                "subject": c.subject,
                "topic": c.topic,
                "page": c.page,
                "score": c.score,
            }
            for c in chunks
        ]

        raw_answer = self.generate_response(
            query=user_question,
            context=context_text,
            language_preference=lang,
            intent=intent,
            exam_type=exam,
            subject=subj,
            difficulty=diff,
        )

        audio_answer = self.simplify_for_audio(raw_answer)

        if citation:
            audio_answer = audio_answer + "\n\n" + citation

        return QueryResult(
            answer=audio_answer,
            intent=intent,
            language=lang,
            sources=sources,
            citation=citation,
            word_count=len(audio_answer.split()),
            context_used=context_used,
            raw_llm_response=raw_answer,
        )

    # ------------------------------------------------------------------
    # Public: explain_concept
    # ------------------------------------------------------------------

    def explain_concept(
        self,
        topic: str,
        difficulty_level: Optional[str] = None,
        exam_type: Optional[str] = None,
        subject: Optional[str] = None,
        language: str = "en",
    ) -> QueryResult:
        if difficulty_level not in _DIFFICULTY_MODIFIERS:
            difficulty_level = settings.llm.default_difficulty

        explain_query = f"Explain the concept of {topic} in detail, step by step."
        if language == "ta":
            explain_query = f"{topic} என்ற கருத்தை படிப்படியாக விரிவாக விளக்கவும்."

        return self.query_rag(
            user_question=explain_query,
            exam_type=exam_type,
            subject=subject,
            language=language,
            difficulty=difficulty_level,
        )

    # ------------------------------------------------------------------
    # Public: simplify_for_audio
    # ------------------------------------------------------------------

    def simplify_for_audio(self, text: str) -> str:
        if not text:
            return ""

        result = text

        for pattern, replacement in _VISUAL_REPLACEMENTS:
            result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

        for pattern, replacement in _MARKDOWN_CLEANUPS:
            result = re.sub(pattern, replacement, result, flags=re.DOTALL)

        result = self._bullets_to_numbered(result)

        result = result.replace(";", ",")
        result = re.sub(r"\s*—\s*", ", ", result)
        result = re.sub(r"\s*–\s*", ", ", result)
        result = re.sub(r"\.{2,}", ".", result)
        result = re.sub(r"\s{2,}", " ", result)
        result = re.sub(r"\n{3,}", "\n\n", result)

        result = self._enforce_word_limit(result, settings.llm.max_response_words)

        return result.strip()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _bullets_to_numbered(self, text: str) -> str:
        ordinals = [
            "First", "Second", "Third", "Fourth", "Fifth",
            "Sixth", "Seventh", "Eighth", "Ninth", "Tenth",
        ]
        counter = [0]

        def _replace(match: re.Match) -> str:
            idx = counter[0]
            counter[0] += 1
            prefix = ordinals[idx] if idx < len(ordinals) else f"{idx + 1}."
            return f"{prefix}, {match.group(1).strip()}"

        lines = text.split("\n")
        out_lines = []
        in_bullet_block = False
        for line in lines:
            bullet_match = _BULLET_LINE.match(line)
            if bullet_match:
                if not in_bullet_block:
                    counter[0] = 0
                    in_bullet_block = True
                idx = counter[0]
                prefix = ordinals[idx] if idx < len(ordinals) else f"{idx + 1}."
                out_lines.append(f"{prefix}, {bullet_match.group(1).strip()}")
                counter[0] += 1
            else:
                if in_bullet_block:
                    in_bullet_block = False
                    counter[0] = 0
                out_lines.append(line)

        return "\n".join(out_lines)

    def _enforce_word_limit(self, text: str, max_words: int) -> str:
        words = text.split()
        if len(words) <= max_words:
            return text

        truncated = " ".join(words[:max_words])
        last_period = max(
            truncated.rfind(". "),
            truncated.rfind(".\n"),
        )
        if last_period > max_words * 3:
            truncated = truncated[: last_period + 1]
        else:
            last_comma = truncated.rfind(", ")
            if last_comma > max_words * 3:
                truncated = truncated[:last_comma] + "."

        logger.debug(
            f"Response truncated from {len(words)} to {len(truncated.split())} words"
        )
        return truncated

    def _build_context_text(self, chunks: List[RetrievedChunk]) -> str:
        if not chunks:
            return ""
        parts = []
        for i, chunk in enumerate(chunks, start=1):
            header = f"[Excerpt {i} — {chunk.subject} | {chunk.topic} | {chunk.source} p.{chunk.page}]"
            parts.append(f"{header}\n{chunk.text}")
        return "\n\n".join(parts)

    def _build_citation(self, chunks: List[RetrievedChunk], lang: str = "en") -> str:
        if not chunks:
            return ""

        seen: Dict[str, RetrievedChunk] = {}
        for c in chunks:
            key = f"{c.source}:{c.page}"
            if key not in seen:
                seen[key] = c

        unique = list(seen.values())

        if lang == "ta":
            lines = ["ஆதாரங்கள்:"]
            for i, c in enumerate(unique, start=1):
                lines.append(
                    f"  {i}. {c.source} — {c.subject} — {c.topic} — பக்கம் {c.page}"
                )
        else:
            lines = ["Sources:"]
            for i, c in enumerate(unique, start=1):
                lines.append(
                    f"  {i}. {c.source} — {c.subject} — {c.topic} — Page {c.page}"
                )

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Module-level standalone functions
# ---------------------------------------------------------------------------

_default_engine: Optional[QueryEngine] = None


def _get_engine() -> QueryEngine:
    global _default_engine
    if _default_engine is None:
        _default_engine = QueryEngine()
    return _default_engine


def query_rag(
    user_question: str,
    exam_type: Optional[str] = None,
    subject: Optional[str] = None,
    language: Optional[str] = None,
    difficulty: Optional[str] = None,
) -> QueryResult:
    return _get_engine().query_rag(
        user_question,
        exam_type=exam_type,
        subject=subject,
        language=language,
        difficulty=difficulty,
    )


def retrieve_context(
    query_embedding: List[float],
    filters: Optional[Dict[str, str]] = None,
    top_k: Optional[int] = None,
) -> List[RetrievedChunk]:
    return _get_engine().retrieve_context(query_embedding, filters=filters, top_k=top_k)


def generate_response(
    query: str,
    context: str,
    language_preference: str = "en",
    intent: str = "study",
    exam_type: Optional[str] = None,
    subject: str = "General",
    difficulty: str = "medium",
) -> str:
    return _get_engine().generate_response(
        query=query,
        context=context,
        language_preference=language_preference,
        intent=intent,
        exam_type=exam_type,
        subject=subject,
        difficulty=difficulty,
    )


def explain_concept(
    topic: str,
    difficulty_level: Optional[str] = None,
    exam_type: Optional[str] = None,
    subject: Optional[str] = None,
    language: str = "en",
) -> QueryResult:
    return _get_engine().explain_concept(
        topic,
        difficulty_level=difficulty_level,
        exam_type=exam_type,
        subject=subject,
        language=language,
    )


def simplify_for_audio(text: str) -> str:
    return _get_engine().simplify_for_audio(text)
