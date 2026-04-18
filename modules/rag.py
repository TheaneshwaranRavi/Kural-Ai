import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from tqdm import tqdm

from config import settings
from modules.chunker import Chunk, SemanticChunker, chunk_text
from modules.ingestion import DocumentProcessor, ProcessedDocument, process_document

logger = logging.getLogger(__name__)


class RAGModule:
    def __init__(self):
        self._embeddings = None
        self._vector_store = None
        self._chunker = SemanticChunker(
            embedding_model_name=settings.vector_db.embedding_model
        )
        self._init_vector_store()

    def _get_embeddings(self):
        if self._embeddings is None:
            from langchain_community.embeddings import HuggingFaceEmbeddings
            logger.info(f"Loading embeddings: {settings.vector_db.embedding_model}")
            self._embeddings = HuggingFaceEmbeddings(
                model_name=settings.vector_db.embedding_model,
                model_kwargs={"device": "cpu"},
                encode_kwargs={"normalize_embeddings": True},
            )
        return self._embeddings

    def _init_vector_store(self) -> None:
        from langchain_community.vectorstores import Chroma
        Path(settings.vector_db.db_path).mkdir(parents=True, exist_ok=True)
        self._vector_store = Chroma(
            collection_name=settings.vector_db.collection_name,
            embedding_function=self._get_embeddings(),
            persist_directory=settings.vector_db.db_path,
        )
        logger.info(
            f"Vector store ready. Documents: {self.get_collection_count()}"
        )

    def generate_embeddings(self, chunks: List[Chunk]) -> List[List[float]]:
        if not chunks:
            return []
        logger.info(f"Generating embeddings for {len(chunks)} chunks")
        model = self._chunker._get_model()
        texts = [c.text for c in chunks]
        raw = model.encode(
            texts,
            show_progress_bar=len(texts) > 20,
            batch_size=32,
            normalize_embeddings=True,
        )
        return raw.tolist()

    def store_in_vectordb(
        self,
        chunks: List[Chunk],
        embeddings: List[List[float]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        if not chunks:
            logger.warning("store_in_vectordb called with empty chunks list")
            return []

        if len(chunks) != len(embeddings):
            raise ValueError(
                f"Chunk count ({len(chunks)}) != embedding count ({len(embeddings)})"
            )

        from langchain.schema import Document

        ids: List[str] = []
        documents: List[Document] = []
        embedding_list: List[List[float]] = []

        for chunk, emb in zip(chunks, embeddings):
            doc_id = str(uuid.uuid4())
            ids.append(doc_id)

            combined_meta = {**chunk.metadata}
            if metadata:
                combined_meta.update(metadata)
            combined_meta["token_count"] = chunk.token_count
            combined_meta["chunk_index"] = chunk.chunk_index
            combined_meta = {
                k: (str(v) if not isinstance(v, (str, int, float, bool)) else v)
                for k, v in combined_meta.items()
            }

            documents.append(Document(page_content=chunk.text, metadata=combined_meta))
            embedding_list.append(emb)

        logger.info(f"Storing {len(documents)} chunks in vector DB")
        self._vector_store.add_embeddings(
            texts=[d.page_content for d in documents],
            embeddings=embedding_list,
            metadatas=[d.metadata for d in documents],
            ids=ids,
        )
        logger.info(f"Stored. Total docs: {self.get_collection_count()}")
        return ids

    def ingest_document(
        self,
        file_path: str,
        exam_type: str = "TNPSC",
        subject: str = "General",
        topic: str = "General",
        language: str = "en",
        chunk_size: Optional[int] = None,
        overlap: Optional[int] = None,
    ) -> Dict[str, Any]:
        chunk_size = chunk_size or settings.vector_db.chunk_size
        overlap = overlap or settings.vector_db.chunk_overlap

        doc: ProcessedDocument = process_document(
            file_path=file_path,
            exam_type=exam_type,
            subject=subject,
            topic=topic,
            language=language,
        )

        base_metadata = {
            "source": Path(file_path).name,
            "exam_type": exam_type,
            "subject": subject,
            "topic": topic,
            "language": language,
        }

        all_chunks: List[Chunk] = []
        for page_num, page_text in enumerate(
            tqdm(doc.pages, desc=f"Chunking {Path(file_path).name}", unit="page"),
            start=1,
        ):
            if not page_text.strip():
                continue
            page_meta = {**base_metadata, "page": page_num}
            page_chunks = self._chunker.chunk_text(
                page_text,
                chunk_size=chunk_size,
                overlap=overlap,
                metadata=page_meta,
            )
            all_chunks.extend(page_chunks)

        if not all_chunks:
            logger.warning(f"No chunks produced from {file_path}")
            return {"file": file_path, "chunks": 0, "ids": []}

        embeddings = self.generate_embeddings(all_chunks)
        ids = self.store_in_vectordb(all_chunks, embeddings)

        return {
            "file": file_path,
            "chunks": len(ids),
            "ids": ids,
            "pages": doc.page_count,
            "ocr_used": doc.ocr_used,
            "tables_found": len(doc.tables),
        }

    def update_content(self, new_documents: List[Dict[str, Any]]) -> Dict[str, Any]:
        results = {"processed": 0, "failed": 0, "total_chunks": 0, "errors": []}

        for doc_info in tqdm(new_documents, desc="Updating content", unit="doc"):
            file_path = doc_info.get("file_path", "")
            exam_type = doc_info.get("exam_type", settings.exam.default_exam)
            subject = doc_info.get("subject", "Current Affairs")
            topic = doc_info.get("topic", "General")
            language = doc_info.get("language", "en")

            try:
                result = self.ingest_document(
                    file_path=file_path,
                    exam_type=exam_type,
                    subject=subject,
                    topic=topic,
                    language=language,
                )
                results["processed"] += 1
                results["total_chunks"] += result["chunks"]
                logger.info(
                    f"Updated: {file_path} → {result['chunks']} chunks"
                )
            except Exception as e:
                results["failed"] += 1
                error_msg = f"{file_path}: {e}"
                results["errors"].append(error_msg)
                logger.error(f"Failed to process {file_path}: {e}")

        logger.info(
            f"update_content complete — "
            f"{results['processed']} ok, {results['failed']} failed, "
            f"{results['total_chunks']} total chunks"
        )
        return results

    def query(
        self,
        question: str,
        exam_filter: Optional[str] = None,
        subject_filter: Optional[str] = None,
        language_filter: Optional[str] = None,
        top_k: Optional[int] = None,
    ) -> Dict[str, Any]:
        k = top_k or settings.vector_db.top_k_results

        where: Dict[str, Any] = {}
        if exam_filter:
            where["exam_type"] = exam_filter
        if subject_filter:
            where["subject"] = subject_filter
        if language_filter:
            where["language"] = language_filter

        search_kwargs: Dict[str, Any] = {"k": k}
        if where:
            search_kwargs["filter"] = where

        from langchain.schema import Document
        results: List[Document] = self._vector_store.similarity_search_with_score(
            question, **search_kwargs
        )

        if not results:
            return {"context": "", "sources": [], "found": False}

        sources = []
        context_parts = []
        for doc, score in results:
            context_parts.append(doc.page_content)
            sources.append(
                {
                    "source": doc.metadata.get("source", "unknown"),
                    "page": doc.metadata.get("page", "N/A"),
                    "subject": doc.metadata.get("subject", ""),
                    "topic": doc.metadata.get("topic", ""),
                    "score": round(float(score), 4),
                }
            )

        return {
            "context": "\n\n---\n\n".join(context_parts),
            "sources": sources,
            "found": True,
        }

    def delete_by_source(self, source_filename: str) -> int:
        collection = self._vector_store._collection
        results = collection.get(where={"source": source_filename})
        ids = results.get("ids", [])
        if ids:
            collection.delete(ids=ids)
            logger.info(f"Deleted {len(ids)} chunks for source: {source_filename}")
        return len(ids)

    def get_collection_count(self) -> int:
        try:
            return self._vector_store._collection.count()
        except Exception:
            return 0
