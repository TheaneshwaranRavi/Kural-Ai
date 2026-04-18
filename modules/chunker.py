import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

logger = logging.getLogger(__name__)

SENTENCE_ENDINGS = re.compile(r"(?<=[.!?।])\s+")
PARAGRAPH_BREAK = re.compile(r"\n{2,}")


@dataclass
class Chunk:
    text: str
    metadata: dict = field(default_factory=dict)
    chunk_index: int = 0
    token_count: int = 0


class SemanticChunker:
    def __init__(
        self,
        embedding_model_name: str = "all-MiniLM-L6-v2",
        breakpoint_percentile: float = 85.0,
    ):
        self._model_name = embedding_model_name
        self._breakpoint_percentile = breakpoint_percentile
        self._model = None

    def _get_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            logger.info(f"Loading embedding model: {self._model_name}")
            self._model = SentenceTransformer(self._model_name)
        return self._model

    def _split_sentences(self, text: str) -> List[str]:
        paragraphs = PARAGRAPH_BREAK.split(text)
        sentences: List[str] = []
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            parts = SENTENCE_ENDINGS.split(para)
            for part in parts:
                part = part.strip()
                if part:
                    sentences.append(part)
        return sentences

    def _estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def _cosine_distances(self, embeddings: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1e-10, norms)
        normalized = embeddings / norms
        similarities = np.sum(normalized[:-1] * normalized[1:], axis=1)
        return 1.0 - similarities

    def _find_breakpoints(self, distances: np.ndarray) -> List[int]:
        if len(distances) == 0:
            return []
        threshold = np.percentile(distances, self._breakpoint_percentile)
        return [i for i, d in enumerate(distances) if d >= threshold]

    def chunk_text(
        self,
        text: str,
        chunk_size: int = 750,
        overlap: int = 75,
        metadata: Optional[dict] = None,
    ) -> List[Chunk]:
        metadata = metadata or {}
        sentences = self._split_sentences(text)

        if not sentences:
            return []

        if len(sentences) <= 3:
            return [
                Chunk(
                    text=text.strip(),
                    metadata=metadata,
                    chunk_index=0,
                    token_count=self._estimate_tokens(text),
                )
            ]

        logger.debug(f"Embedding {len(sentences)} sentences for semantic chunking")
        model = self._get_model()
        embeddings = model.encode(sentences, show_progress_bar=False, batch_size=64)
        embeddings = np.array(embeddings)

        distances = self._cosine_distances(embeddings)
        breakpoints = set(self._find_breakpoints(distances))

        raw_groups: List[List[str]] = []
        current_group: List[str] = []
        for i, sentence in enumerate(sentences):
            current_group.append(sentence)
            if i in breakpoints and len(current_group) > 0:
                raw_groups.append(current_group)
                current_group = []
        if current_group:
            raw_groups.append(current_group)

        chunks = self._enforce_size_limits(raw_groups, chunk_size, overlap, metadata)
        return chunks

    def _enforce_size_limits(
        self,
        groups: List[List[str]],
        chunk_size: int,
        overlap: int,
        metadata: dict,
    ) -> List[Chunk]:
        chunks: List[Chunk] = []
        chunk_index = 0
        overlap_buffer: List[str] = []

        for group in groups:
            group_text = " ".join(group).strip()
            group_tokens = self._estimate_tokens(group_text)

            if group_tokens > chunk_size:
                sub_chunks = self._hard_split(group, chunk_size, overlap, metadata)
                for sc in sub_chunks:
                    sc.chunk_index = chunk_index
                    chunks.append(sc)
                    chunk_index += 1
                overlap_buffer = group[-max(1, len(group) // 4):]
                continue

            prefix_text = " ".join(overlap_buffer).strip()
            combined = (prefix_text + " " + group_text).strip() if prefix_text else group_text
            token_count = self._estimate_tokens(combined)

            chunk = Chunk(
                text=combined,
                metadata={**metadata, "chunk_index": chunk_index},
                chunk_index=chunk_index,
                token_count=token_count,
            )
            chunks.append(chunk)
            chunk_index += 1

            overlap_token_budget = overlap
            overlap_buffer = []
            running = 0
            for sentence in reversed(group):
                t = self._estimate_tokens(sentence)
                if running + t > overlap_token_budget:
                    break
                overlap_buffer.insert(0, sentence)
                running += t

        return chunks

    def _hard_split(
        self,
        sentences: List[str],
        chunk_size: int,
        overlap: int,
        metadata: dict,
    ) -> List[Chunk]:
        chunks: List[Chunk] = []
        current_sentences: List[str] = []
        current_tokens = 0

        for sentence in sentences:
            t = self._estimate_tokens(sentence)
            if current_tokens + t > chunk_size and current_sentences:
                text = " ".join(current_sentences).strip()
                chunks.append(
                    Chunk(
                        text=text,
                        metadata=dict(metadata),
                        chunk_index=0,
                        token_count=self._estimate_tokens(text),
                    )
                )
                overlap_sentences: List[str] = []
                running = 0
                for s in reversed(current_sentences):
                    st = self._estimate_tokens(s)
                    if running + st > overlap:
                        break
                    overlap_sentences.insert(0, s)
                    running += st
                current_sentences = overlap_sentences + [sentence]
                current_tokens = sum(self._estimate_tokens(s) for s in current_sentences)
            else:
                current_sentences.append(sentence)
                current_tokens += t

        if current_sentences:
            text = " ".join(current_sentences).strip()
            chunks.append(
                Chunk(
                    text=text,
                    metadata=dict(metadata),
                    chunk_index=0,
                    token_count=self._estimate_tokens(text),
                )
            )
        return chunks


def chunk_text(
    text: str,
    chunk_size: int = 750,
    overlap: int = 75,
    metadata: Optional[dict] = None,
    embedding_model: str = "all-MiniLM-L6-v2",
) -> List[Chunk]:
    chunker = SemanticChunker(embedding_model_name=embedding_model)
    return chunker.chunk_text(text, chunk_size=chunk_size, overlap=overlap, metadata=metadata)
