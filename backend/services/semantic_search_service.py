"""
Semantic Search Service — Hybrid (Dense + Sparse BM25)
Vector-based meeting search using Qdrant.

Dense embeddings run on the shared Infinity server (Qwen3-Embedding-0.6B,
1024-dim),
consistent with the rest of the UC suite — NOT a local fastembed model. Sparse
BM25 vectors stay local (fastembed, cheap). Queries use Reciprocal Rank Fusion
(RRF) to combine semantic similarity with keyword relevance, and the RAG path
(api/ai_chat.py) reranks the fused candidates with Infinity's reranker before
building the LLM context.
"""

import logging
import os
from typing import List, Dict, Optional
from datetime import datetime, timezone
import json
import re

logger = logging.getLogger(__name__)

# Qdrant connection. Production deploys set QDRANT_URL=http://host:port; older
# code paths set QDRANT_HOST + QDRANT_PORT separately. Honor both, prefer
# QDRANT_URL since it's what the platform-wide compose env uses.
def _resolve_qdrant():
    url = os.getenv("QDRANT_URL")
    if url:
        from urllib.parse import urlparse
        u = urlparse(url)
        return (u.hostname or "localhost"), (u.port or 6333)
    return os.getenv("QDRANT_HOST", "localhost"), int(os.getenv("QDRANT_PORT", "6333"))

QDRANT_HOST, QDRANT_PORT = _resolve_qdrant()
COLLECTION_NAME = "meeting_transcripts"

# Embedding models.
# Dense embeddings run on the shared Infinity server (suite-consistent across
# the UC apps) — NOT a local fastembed model. The vector dimension is PROBED
# from Infinity at collection-create time so a model swap can't desync the
# Qdrant collection; DENSE_DIM is only the fallback if the probe fails.
DENSE_MODEL = os.getenv("EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-0.6B")  # Infinity, 1024-dim
DENSE_DIM = int(os.getenv("EMBEDDING_DIM", "1024"))        # probe fallback only
SPARSE_MODEL = "Qdrant/bm25"             # BM25 sparse stays local (fastembed, cheap)
INDEX_SCHEMA_VERSION = 2

# Infinity embedding endpoint (OpenAI-compatible POST /embeddings). Prefer the
# embedding-specific endpoint, fall back to the generic one, then the in-cluster
# proxy default.
INFINITY_EMBED_ENDPOINT = (
    os.getenv("INFINITY_EMBEDDING_ENDPOINT")
    or os.getenv("INFINITY_ENDPOINT")
    or "http://unicorn-infinity-proxy:8086/v1"
)
INFINITY_API_KEY = os.getenv("INFINITY_API_KEY", "")

# Chunk settings for long transcripts
CHUNK_SIZE = 400  # words per chunk
CHUNK_OVERLAP = 50  # overlapping words between chunks

# Speaker label pattern for speaker-aware chunking. Index transcripts are
# built internally as "Name: text" lines, so accept ANY short prefix before
# ": " — real display names include hyphens, apostrophes, periods, digits
# and non-ASCII letters ("Mary-Jane:", "O'Brien:", "Dr. Smith:", "José:",
# "Speaker 1:", "SPEAKER_00:"). The 60-char cap keeps a colon deep inside a
# prose line from being misread as a speaker label.
SPEAKER_PATTERN = re.compile(r'^(.{1,60}?):\s', re.MULTILINE)


def _match_speaker_label(line: str) -> Optional[str]:
    """Speaker name if the line starts a "Name: text" turn, else None.

    Tolerates the legacy bracketed form ("[Speaker 1]: ...") by stripping
    surrounding brackets from the captured name.
    """
    m = SPEAKER_PATTERN.match(line)
    if not m:
        return None
    name = m.group(1).strip().strip("[]").strip()
    return name or None


class SemanticSearchService:
    """Manages hybrid vector indexing and search for meeting transcripts.

    Uses dense (semantic) + sparse (BM25 keyword) vectors with Reciprocal
    Rank Fusion for better recall than either method alone.
    """

    def __init__(self):
        self._client = None
        self._dense_embedder = None
        self._sparse_embedder = None
        self._dense_dim = None  # probed from Infinity on first collection setup
        self._hybrid_enabled = False  # Set True after successful collection setup
        self._initialized = False

    def _get_client(self):
        """Lazy-init Qdrant client. check_compatibility is a 1.13+ kwarg;
        the running image has 1.12.x so we pass it conditionally."""
        if self._client is None:
            from qdrant_client import QdrantClient
            try:
                self._client = QdrantClient(
                    host=QDRANT_HOST,
                    port=QDRANT_PORT,
                    check_compatibility=False,
                )
            except TypeError:
                self._client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
        return self._client

    def _get_dense_embedder(self):
        """Lazy-init the dense embedder — the shared Infinity server (suite-
        consistent), not a local model. Returns an InfinityProvider exposing
        ``embed_sync(texts) -> list[list[float]]``."""
        if self._dense_embedder is None:
            from services.providers.impl_embeddings import InfinityProvider
            self._dense_embedder = InfinityProvider(
                api_key=INFINITY_API_KEY,
                endpoint=INFINITY_EMBED_ENDPOINT,
                model=DENSE_MODEL,
            )
            logger.info(
                "Dense embeddings via Infinity (endpoint=%s model=%s)",
                INFINITY_EMBED_ENDPOINT, DENSE_MODEL,
            )
        return self._dense_embedder

    def _probe_dense_dim(self) -> int:
        """Live dense-embedding dimension, probed once from Infinity so the
        Qdrant collection can never desync from the configured model. Falls
        back to DENSE_DIM if the probe fails."""
        if self._dense_dim is None:
            try:
                vec = self._embed(["dimension probe"])
                self._dense_dim = len(vec[0]) if vec and vec[0] else DENSE_DIM
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Dense-dim probe failed (%s); falling back to %s", exc, DENSE_DIM
                )
                self._dense_dim = DENSE_DIM
        return self._dense_dim

    def _get_sparse_embedder(self):
        """Lazy-init sparse BM25 embedding model (Qdrant/bm25)."""
        if self._sparse_embedder is None:
            try:
                from fastembed import SparseTextEmbedding
                self._sparse_embedder = SparseTextEmbedding(SPARSE_MODEL)
                logger.info(f"Loaded sparse embedding model: {SPARSE_MODEL}")
            except Exception as e:
                logger.warning(f"Failed to load sparse embedder: {e}")
                self._sparse_embedder = None
        return self._sparse_embedder

    # Backward-compatible aliases
    def _get_embedder(self):
        return self._get_dense_embedder()

    def _org_filter(self, organization_id: Optional[int]):
        if organization_id is None:
            return None

        from qdrant_client.models import FieldCondition, Filter, MatchValue

        return Filter(
            must=[
                FieldCondition(
                    key="organization_id",
                    match=MatchValue(value=organization_id),
                )
            ]
        )

    @staticmethod
    def _normalize_query_text(text: str) -> str:
        """Normalize free-form text for lightweight lexical matching."""
        cleaned = re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
        return " ".join(cleaned.split())

    @classmethod
    def _title_boost(cls, query: str, title: str) -> float:
        """Give title/name matches an explicit boost when ranking search hits.

        Qdrant already scores semantic similarity, but exact or near-exact
        title matches should win deterministically for user-facing queries like
        "Transcription System Review and GUI Refinement".
        """
        nq = cls._normalize_query_text(query)
        nt = cls._normalize_query_text(title)
        if not nq or not nt:
            return 0.0
        if nq == nt:
            return 2.5
        if nq in nt or nt in nq:
            return 1.5

        query_terms = set(nq.split())
        title_terms = set(nt.split())
        if not query_terms or not title_terms:
            return 0.0

        overlap = len(query_terms & title_terms) / len(query_terms)
        if overlap >= 0.75:
            return 1.0
        if overlap >= 0.5:
            return 0.5
        return 0.0

    @staticmethod
    def _summary_text(summary_value: Optional[str | dict]) -> str:
        """Coerce legacy / structured summary shapes into searchable text."""
        if not summary_value:
            return ""
        if isinstance(summary_value, str):
            return summary_value
        if not isinstance(summary_value, dict):
            return str(summary_value)

        parts: list[str] = []
        executive = summary_value.get("executive") or summary_value.get("executive_summary")
        if executive:
            parts.append(str(executive))
        bullets = summary_value.get("bullets") or summary_value.get("key_points") or []
        if isinstance(bullets, list) and bullets:
            parts.extend(str(b) for b in bullets if b)
        decisions = summary_value.get("decisions") or []
        if isinstance(decisions, list) and decisions:
            parts.extend(str(d) for d in decisions if d)
        action_items = summary_value.get("action_items") or summary_value.get("actions") or []
        if isinstance(action_items, list) and action_items:
            parts.extend(str(a) for a in action_items if a)
        if not parts:
            return json.dumps(summary_value, default=str)
        return "\n".join(parts)

    @staticmethod
    def _search_text(title: str, body: str) -> str:
        """Prefix searchable bodies with the title so title queries land."""
        title = (title or "").strip()
        body = (body or "").strip()
        if title and body:
            return f"{title}\n\n{body}"
        return title or body

    def _embed(self, texts: List[str]) -> List[List[float]]:
        """Generate dense embeddings via the Infinity server."""
        embedder = self._get_dense_embedder()
        vecs = embedder.embed_sync(list(texts))
        if not vecs or len(vecs) != len(texts):
            raise RuntimeError(
                f"Infinity dense embedding returned {len(vecs) if vecs else 0}/"
                f"{len(texts)} vectors (endpoint {INFINITY_EMBED_ENDPOINT} unreachable?)"
            )
        return vecs

    def _sparse_embed(self, texts: List[str]) -> list:
        """Generate sparse BM25 embeddings for a list of texts.

        Returns list of sparse embedding objects (with .indices and .values),
        or None if sparse embedder is unavailable.
        """
        embedder = self._get_sparse_embedder()
        if embedder is None:
            return None
        try:
            return list(embedder.embed(texts))
        except Exception as e:
            logger.warning(f"Sparse embedding failed: {e}")
            return None

    def ensure_collection(self):
        """Create or recreate the Qdrant collection with hybrid vector support.

        The collection uses named vectors:
        - "dense": COSINE, dimension probed from the configured embedding
          provider at runtime (Qwen/Qwen3-Embedding-0.6B = 1024)
        - "sparse": BM25 sparse vectors (keyword matching via fastembed)

        If the collection exists but lacks sparse vector config, it is
        deleted and recreated. All data is reindexed afterward.
        """
        client = self._get_client()
        collections = [c.name for c in client.get_collections().collections]

        needs_creation = False
        want_dim = self._probe_dense_dim()

        if COLLECTION_NAME in collections:
            # Check the existing collection has hybrid config AT THE RIGHT
            # DIMENSION. The old code only checked hybrid-vs-not and would skip
            # a recreate on a dimension change (fastembed 384 -> Infinity
            # bge-m3 1024), leaving Qdrant rejecting every upsert.
            info = client.get_collection(COLLECTION_NAME)
            vectors_config = info.config.params.vectors

            has_named_dense = (
                isinstance(vectors_config, dict) and "dense" in vectors_config
            )
            has_sparse = bool(
                info.config.params.sparse_vectors
                and "sparse" in info.config.params.sparse_vectors
            )
            dense_size = (
                getattr(vectors_config.get("dense"), "size", None)
                if isinstance(vectors_config, dict)
                else None
            )

            if has_named_dense and has_sparse and dense_size == want_dim:
                # Already hybrid at the right dimension — nothing to do
                self._hybrid_enabled = True
                self._initialized = True
                logger.info("Qdrant collection already hybrid at %s-dim", want_dim)
                return

            # Wrong config OR wrong dimension — delete + recreate. The caller
            # MUST reindex afterward (old vectors are gone / incompatible).
            logger.info(
                "Recreating Qdrant collection (named_dense=%s sparse=%s "
                "dense_size=%s want=%s)",
                has_named_dense, has_sparse, dense_size, want_dim,
            )
            client.delete_collection(COLLECTION_NAME)
            needs_creation = True
        else:
            needs_creation = True

        if needs_creation:
            from qdrant_client.models import (
                VectorParams, Distance,
                SparseVectorParams, SparseIndexParams,
            )
            client.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config={
                    "dense": VectorParams(
                        size=want_dim,
                        distance=Distance.COSINE,
                    ),
                },
                sparse_vectors_config={
                    "sparse": SparseVectorParams(
                        index=SparseIndexParams(),
                    ),
                },
            )
            self._hybrid_enabled = True
            logger.info(
                "Created Qdrant collection '%s' (dense %s-dim COSINE + sparse BM25)",
                COLLECTION_NAME, want_dim,
            )

        self._initialized = True

    def _chunk_text(self, text: str) -> List[Dict]:
        """
        Split text into overlapping chunks. Returns list of dicts with
        'text' and 'speakers' keys.

        Speaker-aware: if the text has speaker labels (e.g. "Speaker 1: ..."
        or "[Speaker 1] ..."), splits on speaker turn boundaries first, then
        merges turns into ~CHUNK_SIZE-word chunks with overlap.

        Falls back to plain word-count splitting for unlabelled text.
        """
        # Detect speaker-labelled transcript
        has_speakers = bool(SPEAKER_PATTERN.search(text))

        if has_speakers:
            return self._chunk_speaker_text(text)
        else:
            return self._chunk_plain_text(text)

    def _chunk_plain_text(self, text: str) -> List[Dict]:
        """Plain word-count chunking (no speaker labels)."""
        words = text.split()
        if len(words) <= CHUNK_SIZE:
            return [{"text": text, "speakers": []}]
        chunks = []
        start = 0
        while start < len(words):
            end = start + CHUNK_SIZE
            chunk = " ".join(words[start:end])
            chunks.append({"text": chunk, "speakers": []})
            start = end - CHUNK_OVERLAP
        return chunks

    def _chunk_speaker_text(self, text: str) -> List[Dict]:
        """
        Speaker-aware chunking. Splits on speaker turn boundaries,
        then groups turns into ~CHUNK_SIZE-word chunks with CHUNK_OVERLAP
        word overlap. Keeps speaker labels in the chunk text.
        """
        # Split into speaker turns. Each turn starts with a speaker label.
        lines = text.split("\n")
        turns = []  # list of (speaker_name, turn_text)
        current_speaker = ""
        current_lines = []

        for line in lines:
            # Check for speaker label at start of line ("Name: text")
            speaker = _match_speaker_label(line)

            if speaker:
                # Save previous turn
                if current_lines:
                    turns.append((current_speaker, "\n".join(current_lines)))
                current_speaker = speaker
                current_lines = [line]  # keep original line with label
            else:
                # Continuation of current speaker's turn
                if line.strip():
                    current_lines.append(line)

        # Don't forget the last turn
        if current_lines:
            turns.append((current_speaker, "\n".join(current_lines)))

        if not turns:
            return self._chunk_plain_text(text)

        # Now merge turns into chunks of ~CHUNK_SIZE words
        chunks = []
        current_chunk_turns = []
        current_word_count = 0
        current_speakers = set()

        for speaker, turn_text in turns:
            turn_words = len(turn_text.split())

            # If adding this turn exceeds the limit and we already have content,
            # finalize the current chunk
            if current_word_count + turn_words > CHUNK_SIZE and current_chunk_turns:
                chunk_text = "\n".join(current_chunk_turns)
                chunks.append({
                    "text": chunk_text,
                    "speakers": sorted(current_speakers),
                })

                # Overlap: keep the last few turns that fit within CHUNK_OVERLAP words
                overlap_turns = []
                overlap_words = 0
                for ot_text in reversed(current_chunk_turns):
                    ot_words = len(ot_text.split())
                    if overlap_words + ot_words > CHUNK_OVERLAP:
                        break
                    overlap_turns.insert(0, ot_text)
                    overlap_words += ot_words

                current_chunk_turns = overlap_turns
                current_word_count = overlap_words
                # Re-extract speakers from overlap turns (each turn keeps its
                # label line, so the first line carries the name)
                current_speakers = set()
                for ot in overlap_turns:
                    ot_speaker = _match_speaker_label(ot)
                    if ot_speaker:
                        current_speakers.add(ot_speaker)

            current_chunk_turns.append(turn_text)
            current_word_count += turn_words
            if speaker:
                current_speakers.add(speaker)

        # Finalize the last chunk
        if current_chunk_turns:
            chunk_text = "\n".join(current_chunk_turns)
            chunks.append({
                "text": chunk_text,
                "speakers": sorted(current_speakers),
            })

        return chunks if chunks else [{"text": text, "speakers": []}]

    def index_session(
        self,
        session_id: str,
        title: str,
        transcript: Optional[str],
        summary: Optional[str],
        created_at: Optional[str] = None,
        organization_id: Optional[int] = None,
        source_kind: str = "meeting",
    ):
        """
        Index a meeting session into Qdrant with hybrid (dense + sparse) vectors.
        Chunks long transcripts and indexes each chunk as a separate point.
        Also indexes the summary and title as their own points.

        `source_kind` labels what kind of source this is ("meeting",
        "lecture", "document") for the Ask AI context header and citation
        UI (Phase 3D/3E). Defaults to "meeting" so every pre-existing call
        site (run_upload_pipeline, omi_webhooks finalize, etc.) keeps its
        exact current behavior with zero changes required — this is an
        additive parameter, not a breaking one.
        """
        self.ensure_collection()
        client = self._get_client()

        # First, delete any existing points for this session (re-index)
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=Filter(
                must=[FieldCondition(
                    key="session_id",
                    match=MatchValue(value=session_id),
                )]
            ),
        )

        texts_to_embed = []
        payloads = []
        indexed_at = datetime.now(timezone.utc).isoformat()

        def _payload(values: Dict) -> Dict:
            """Attach immutable vector provenance to every Qdrant point.

            A collection can retain points through a model change when the
            dimensions happen to match.  Recording the actual model beside
            each point makes that state observable and lets reindex jobs prove
            that a workspace is homogeneous before search relies on it.
            """
            return {
                **values,
                "source_kind": source_kind,
                "dense_embedding_model": DENSE_MODEL,
                "sparse_embedding_model": SPARSE_MODEL,
                "index_schema_version": INDEX_SCHEMA_VERSION,
                "indexed_at": indexed_at,
            }

        # Index transcript chunks
        if transcript and len(transcript.strip()) > 10:
            chunks = self._chunk_text(transcript)
            for i, chunk_data in enumerate(chunks):
                chunk_text = chunk_data["text"]
                chunk_speakers = chunk_data.get("speakers", [])
                texts_to_embed.append(self._search_text(title, chunk_text))
                payloads.append(_payload({
                    "session_id": session_id,
                    "title": title or "",
                    "content_type": "transcript",
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                    "text": chunk_text,  # Store full chunk text for RAG retrieval
                    "speakers": chunk_speakers,
                    "created_at": created_at or "",
                    "organization_id": organization_id,
                }))

        # Index summary as a single point
        if summary and len(summary.strip()) > 10:
            texts_to_embed.append(self._search_text(title, summary))
            payloads.append(_payload({
                "session_id": session_id,
                "title": title or "",
                "content_type": "summary",
                "chunk_index": 0,
                "total_chunks": 1,
                "text": summary,  # Store full summary text for RAG retrieval
                "speakers": [],
                "created_at": created_at or "",
                "organization_id": organization_id,
            }))

        # Index title
        if title and len(title.strip()) > 3:
            texts_to_embed.append(self._search_text(title, title))
            payloads.append(_payload({
                "session_id": session_id,
                "title": title,
                "content_type": "title",
                "chunk_index": 0,
                "total_chunks": 1,
                "text": title,
                "speakers": [],
                "created_at": created_at or "",
                "organization_id": organization_id,
            }))

        if not texts_to_embed:
            logger.debug(f"No content to index for session {session_id}")
            return 0

        # Generate dense embeddings
        dense_embeddings = self._embed(texts_to_embed)

        # Generate sparse (BM25) embeddings
        sparse_embeddings = self._sparse_embed(texts_to_embed)

        # Build points with named vectors
        from qdrant_client.models import PointStruct, SparseVector
        import uuid

        points = []
        for i, (dense_emb, payload) in enumerate(zip(dense_embeddings, payloads)):
            # Keep the measured dimension with the exact point, rather than
            # trusting a collection-level fallback setting.
            payload["dense_embedding_dimension"] = len(dense_emb)
            vector_dict = {
                "dense": dense_emb,
            }

            # Add sparse vector if available
            if sparse_embeddings is not None and i < len(sparse_embeddings):
                sparse = sparse_embeddings[i]
                vector_dict["sparse"] = SparseVector(
                    indices=sparse.indices.tolist(),
                    values=sparse.values.tolist(),
                )

            points.append(PointStruct(
                id=str(uuid.uuid4()),
                vector=vector_dict,
                payload=payload,
            ))

        # Upsert to Qdrant
        client.upsert(collection_name=COLLECTION_NAME, points=points)
        has_sparse = sparse_embeddings is not None
        logger.info(
            f"Indexed session {session_id}: {len(points)} points "
            f"({len([p for p in payloads if p['content_type'] == 'transcript'])} transcript, "
            f"{len([p for p in payloads if p['content_type'] == 'summary'])} summary, "
            f"{len([p for p in payloads if p['content_type'] == 'title'])} title) "
            f"[hybrid={'yes' if has_sparse else 'no'}]"
        )
        return len(points)

    def _hybrid_query(
        self,
        query: str,
        limit: int,
        organization_id: Optional[int] = None,
    ):
        """Execute a hybrid (dense + sparse) query using Qdrant prefetch + RRF fusion.

        Falls back to dense-only if sparse embedder is unavailable or the
        collection doesn't have sparse vectors configured.

        Returns (points_list, search_mode_str).
        """
        client = self._get_client()

        # Dense embedding (always available)
        query_dense = self._embed([query])[0]
        org_filter = self._org_filter(organization_id)

        # Try sparse embedding
        sparse_result = self._sparse_embed([query])
        use_hybrid = (
            self._hybrid_enabled
            and sparse_result is not None
            and len(sparse_result) > 0
        )

        if use_hybrid:
            try:
                from qdrant_client.models import (
                    Prefetch, FusionQuery, Fusion, SparseVector,
                )

                sparse = sparse_result[0]
                query_sparse = SparseVector(
                    indices=sparse.indices.tolist(),
                    values=sparse.values.tolist(),
                )

                query_result = client.query_points(
                    collection_name=COLLECTION_NAME,
                    prefetch=[
                        Prefetch(
                            query=query_dense,
                            using="dense",
                            limit=limit * 3,
                            filter=org_filter,
                        ),
                        Prefetch(
                            query=query_sparse,
                            using="sparse",
                            limit=limit * 3,
                            filter=org_filter,
                        ),
                    ],
                    query=FusionQuery(fusion=Fusion.RRF),
                    limit=limit,
                )
                return query_result.points, "hybrid"

            except Exception as e:
                logger.warning(
                    f"Hybrid query failed, falling back to dense-only: {e}"
                )

        # Fallback: dense-only search
        query_result = client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_dense,
            using="dense",
            limit=limit,
            query_filter=org_filter,
        )
        return query_result.points, "dense"

    def search(
        self,
        query: str,
        limit: int = 10,
        organization_id: Optional[int] = None,
    ) -> List[Dict]:
        """
        Hybrid semantic search across all indexed meetings.
        Uses dense + sparse BM25 with RRF fusion for best recall.
        Returns matching sessions with relevance scores and snippets.
        """
        self.ensure_collection()
        client = self._get_client()

        # Check if collection has points
        info = client.get_collection(COLLECTION_NAME)
        if info.points_count == 0:
            return []

        # Hybrid query: dense + sparse with RRF fusion
        results, search_mode = self._hybrid_query(
            query,
            limit=limit * 3,
            organization_id=organization_id,
        )

        # Deduplicate by session_id, keeping the best score
        seen_sessions = {}
        for hit in results:
            sid = hit.payload.get("session_id")
            title = hit.payload.get("title", "")
            boosted_score = round(
                float(hit.score) + self._title_boost(query, title),
                4,
            )
            if sid not in seen_sessions or boosted_score > seen_sessions[sid]["score"]:
                seen_sessions[sid] = {
                    "session_id": sid,
                    "title": title,
                    "score": boosted_score,
                    "match_type": hit.payload.get("content_type", ""),
                    "snippet": hit.payload.get("text", ""),
                    "created_at": hit.payload.get("created_at", ""),
                }

        # Sort by score descending, limit
        results_list = sorted(
            seen_sessions.values(),
            key=lambda x: x["score"],
            reverse=True,
        )[:limit]

        return results_list

    def search_chunks(
        self,
        query: str,
        limit: int = 10,
        organization_id: Optional[int] = None,
    ) -> List[Dict]:
        """
        Hybrid semantic search returning individual chunks (not deduplicated by session).
        Used by RAG pipeline to retrieve relevant context across all meetings.
        Returns chunks with full text, session metadata, and relevance scores.
        """
        self.ensure_collection()
        client = self._get_client()

        info = client.get_collection(COLLECTION_NAME)
        if info.points_count == 0:
            return []

        # Hybrid query: dense + sparse with RRF fusion
        results, search_mode = self._hybrid_query(
            query,
            limit=limit,
            organization_id=organization_id,
        )

        chunks = []
        for hit in results:
            title = hit.payload.get("title", "")
            chunks.append({
                "session_id": hit.payload.get("session_id", ""),
                "title": title,
                "content_type": hit.payload.get("content_type", ""),
                "chunk_index": hit.payload.get("chunk_index", 0),
                "text": hit.payload.get("text", ""),
                "speakers": hit.payload.get("speakers", []),
                "score": round(float(hit.score) + self._title_boost(query, title), 4),
                "created_at": hit.payload.get("created_at", ""),
                "source_kind": hit.payload.get("source_kind", "meeting"),
            })

        return chunks

    def delete_session(self, session_id: str):
        """Remove all indexed points for a session."""
        self.ensure_collection()
        client = self._get_client()
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=Filter(
                must=[FieldCondition(
                    key="session_id",
                    match=MatchValue(value=session_id),
                )]
            ),
        )
        logger.info(f"Deleted vectors for session {session_id}")

    def get_stats(self, organization_id: Optional[int] = None) -> Dict:
        """Get collection statistics including hybrid search status."""
        try:
            client = self._get_client()
            collections = [c.name for c in client.get_collections().collections]
            if COLLECTION_NAME not in collections:
                return {
                    "initialized": False,
                    "collection": COLLECTION_NAME,
                    "points_count": 0,
                }
            info = client.get_collection(COLLECTION_NAME)

            # Detect vector configuration
            vectors_config = info.config.params.vectors
            has_named_dense = (
                isinstance(vectors_config, dict) and "dense" in vectors_config
            )
            has_sparse = bool(
                info.config.params.sparse_vectors
                and "sparse" in info.config.params.sparse_vectors
            )
            search_mode = "hybrid" if (has_named_dense and has_sparse) else "dense-only"

            return {
                "initialized": True,
                "collection": COLLECTION_NAME,
                "points_count": info.points_count,
                "indexed_vectors_count": info.indexed_vectors_count,
                "status": str(info.status),
                "search_mode": search_mode,
                "dense_model": DENSE_MODEL,
                "sparse_model": SPARSE_MODEL if has_sparse else None,
                "organization_id": organization_id,
            }
        except Exception as e:
            return {
                "initialized": False,
                "error": str(e),
            }

    def reindex_all(self, db_session, organization_id: Optional[int] = None) -> Dict:
        """Reindex all completed sessions from the database.

        Recreates the collection with hybrid vectors if needed, then
        indexes all sessions with both dense and sparse embeddings.
        """
        from database.models import RecordingSession
        sessions = db_session.query(RecordingSession).filter(
            RecordingSession.status.in_(["completed", "failed"])
        ).all()
        if organization_id is not None:
            sessions = [s for s in sessions if s.organization_id == organization_id]

        total_points = 0
        indexed = 0
        skipped = 0

        for session in sessions:
            # Gather text content
            transcript = session.transcript_simple or ""
            if not transcript and session.transcript:
                # Try to extract text from JSON transcript
                import json
                try:
                    t = json.loads(session.transcript) if isinstance(session.transcript, str) else session.transcript
                    if isinstance(t, dict) and "text" in t:
                        transcript = t["text"]
                    elif isinstance(t, dict) and "segments" in t:
                        transcript = " ".join(seg.get("text", "") for seg in t["segments"])
                except Exception:
                    transcript = str(session.transcript) if session.transcript else ""

            summary = session.summary or ""
            if not summary and isinstance(session.final_summary, dict):
                summary = self._summary_text(session.final_summary)
            elif isinstance(summary, dict):
                summary = self._summary_text(summary)
            elif not summary and session.final_summary:
                summary = self._summary_text(session.final_summary)
            title = session.title or session.name or ""
            created = session.created_at.isoformat() if session.created_at else ""

            if not transcript and not summary and not title:
                skipped += 1
                continue

            points = self.index_session(
                session_id=session.session_id or str(session.id),
                title=title,
                transcript=transcript,
                summary=summary,
                created_at=created,
                organization_id=session.organization_id,
            )
            total_points += points
            indexed += 1

        return {
            "sessions_indexed": indexed,
            "sessions_skipped": skipped,
            "total_points": total_points,
            "search_mode": "hybrid" if self._hybrid_enabled else "dense-only",
            "organization_id": organization_id,
        }


# Global singleton
semantic_search = SemanticSearchService()
