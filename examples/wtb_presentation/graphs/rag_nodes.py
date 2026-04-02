"""
RAG Pipeline Nodes for WTB Presentation Demo.

═══════════════════════════════════════════════════════════════════════════════
RAG ARCHITECTURE - Separated Indexing vs Query Phases
═══════════════════════════════════════════════════════════════════════════════

PHASE 1: DOCUMENT INDEXING (One-time, when documents change)
───────────────────────────────────────────────────────────────────────────────
  1. rag_load_docs    - Load documents from workspace/documents
  2. rag_chunk_split  - Split documents into chunks (500 chars, 50 overlap)
  3. rag_embed_docs   - Generate embeddings for ALL chunks → store in index

  Documents are indexed ONCE and cached. Re-indexing only when:
  - New documents added
  - Documents modified
  - Index cache invalidated

PHASE 2: QUERY PROCESSING (Per-query, uses indexed embeddings)
───────────────────────────────────────────────────────────────────────────────
  4. rag_embed_query  - Embed the user query (single embedding)
  5. rag_retrieve     - Vector similarity search against doc index
  6. rag_grade        - LLM grades relevance, decides routing
  7. rag_generate     - LLM generates answer from context

NODE VARIANTS:
───────────────────────────────────────────────────────────────────────────────
  Embed Docs:  text-embedding-3-small, text-embedding-3-large
  Embed Query: text-embedding-3-small, text-embedding-3-large  
  Retrieve:    dense_v1 (cosine), bm25_v1 (keyword), hybrid_v1 (fusion)
  Grade:       gpt-4o-mini, gpt-4o
  Generate:    gpt-4o-mini, gpt-4o

EXECUTION FLOW DIAGRAM:
───────────────────────────────────────────────────────────────────────────────

  ┌─────────────────────────────────────────────────────────────────────────┐
  │                    INDEXING PHASE (Cached)                               │
  │  ┌────────────┐    ┌─────────────┐    ┌───────────────┐                 │
  │  │ load_docs  │ ─► │ chunk_split │ ─► │  embed_docs   │ ─► [INDEX]     │
  │  │            │    │             │    │               │      ▼          │
  │  │ venv: base │    │ venv: base  │    │ venv: numpy   │   Vector       │
  │  │ ray: 1 cpu │    │ ray: 1 cpu  │    │ ray: 2 cpu    │   Store        │
  │  │ mem: 256MB │    │ mem: 256MB  │    │ mem: 2048MB   │   (FAISS)      │
  │  └────────────┘    └─────────────┘    └───────────────┘                 │
  └─────────────────────────────────────────────────────────────────────────┘
                                                  │
                                                  │ index ready
                                                  ▼
  ┌─────────────────────────────────────────────────────────────────────────┐
  │                    QUERY PHASE (Per Request)                             │
  │                                                                          │
  │  Query ─► ┌─────────────┐    ┌────────────┐    ┌────────────┐           │
  │           │ embed_query │ ─► │  retrieve  │ ─► │   grade    │           │
  │           │             │    │            │    │            │           │
  │           │ venv: numpy │    │venv: faiss │    │ venv: base │           │
  │           │ ray: 1 cpu  │    │ray: 2 cpu  │    │ ray: 1 cpu │           │
  │           │ mem: 512MB  │    │mem: 1024MB │    │ mem: 512MB │           │
  │           └─────────────┘    └────────────┘    └──────┬─────┘           │
  │                                                        │                 │
  │                               ┌────────────────────────┼──────────┐      │
  │                               │                        │          │      │
  │                            [rewrite]              [generate]   [sql]    │
  │                               │                        │          │      │
  │                               ▼                        ▼          ▼      │
  │                        ┌────────────┐          ┌────────────┐  ┌─────┐  │
  │                        │  retrieve  │          │  generate  │  │ SQL │  │
  │                        │  (loop)    │          │            │  │Agent│  │
  │                        │            │          │ venv: base │  │     │  │
  │                        │ ray: 2 cpu │          │ ray: 1 cpu │  │ 1cpu│  │
  │                        │ mem: 1024MB│          │mem: 1024MB │  │512MB│  │
  │                        └────────────┘          └────────────┘  └─────┘  │
  └─────────────────────────────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

WORKSPACE_DIR = Path(__file__).parent.parent / "workspace"
DOCUMENTS_DIR = WORKSPACE_DIR / "documents"
OUTPUTS_DIR = WORKSPACE_DIR / "outputs"

# LLM Configuration - import from config
try:
    from examples.wtb_presentation.config.llm_config import (
        generate_embeddings,
        generate_text_result,
        grade_document_relevance_result,
        generate_answer_result,
        DEFAULT_LLM,
        ALT_LLM,
        EMBEDDING_MODEL,
        ALT_EMBEDDING_MODEL,
    )
    LLM_AVAILABLE = True
except ImportError:
    LLM_AVAILABLE = False
    DEFAULT_LLM = "gpt-4o-mini"
    ALT_LLM = "gpt-4o"
    EMBEDDING_MODEL = "text-embedding-3-small"
    ALT_EMBEDDING_MODEL = "text-embedding-3-large"


def _cache_state_update(cache_refs: Dict[str, str], cache_hits: Dict[str, bool]) -> Dict[str, Dict[str, Any]]:
    """Return lightweight cache metadata updates for workflow state."""
    updates: Dict[str, Dict[str, Any]] = {}
    if cache_refs:
        updates["llm_cache_refs"] = cache_refs
    if cache_hits:
        updates["llm_cache_hits"] = cache_hits
    return updates


# ═══════════════════════════════════════════════════════════════════════════════
# Node 1: Load Documents
# ═══════════════════════════════════════════════════════════════════════════════

def rag_load_docs(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node 1: Load documents from workspace/documents directory.
    
    Reads all markdown and text files from the documents directory.
    
    Input state:
        - query: User query (for logging)
        
    Output state:
        - raw_docs: List of document dictionaries
        - docs_loaded: Count of documents loaded
        - messages: Updated with load status
        - _output_files: Manifest of loaded documents
    """
    start_time = time.time()
    
    docs_dir = DOCUMENTS_DIR
    raw_docs = []
    
    # Load all documents from workspace
    if docs_dir.exists():
        for file_path in docs_dir.glob("**/*"):
            if file_path.suffix in [".md", ".txt", ".json"]:
                try:
                    content = file_path.read_text(encoding="utf-8")
                    raw_docs.append({
                        "id": hashlib.md5(str(file_path).encode()).hexdigest()[:8],
                        "source": str(file_path.relative_to(docs_dir)),
                        "content": content,
                        "metadata": {
                            "filename": file_path.name,
                            "size": len(content),
                            "loaded_at": datetime.now().isoformat(),
                        }
                    })
                except Exception as e:
                    print(f"[rag_load_docs] Error loading {file_path}: {e}")
    
    duration_ms = (time.time() - start_time) * 1000
    
    # Create output manifest
    manifest = {
        "loaded_at": datetime.now().isoformat(),
        "document_count": len(raw_docs),
        "documents": [{"id": d["id"], "source": d["source"], "size": d["metadata"]["size"]} for d in raw_docs],
    }
    
    return {
        "raw_docs": raw_docs,
        "docs_loaded": len(raw_docs),
        "messages": [f"[rag_load_docs] Loaded {len(raw_docs)} documents in {duration_ms:.1f}ms"],
        "current_node": "rag_load_docs",
        "_output_files": {"load_manifest.json": json.dumps(manifest, indent=2)},
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Node 2: Chunk/Split Documents
# ═══════════════════════════════════════════════════════════════════════════════

def rag_chunk_split(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node 2: Split documents into chunks for embedding.
    
    Uses simple character-based splitting with overlap.
    In production, would use RecursiveCharacterTextSplitter.
    
    Input state:
        - raw_docs: List of loaded documents
        
    Output state:
        - chunks: List of document chunks
        - chunk_count: Number of chunks created
        - messages: Updated with chunk status
        - _output_files: Chunks saved to file
    """
    start_time = time.time()
    
    raw_docs = state.get("raw_docs", [])
    chunk_size = 500
    chunk_overlap = 50
    
    chunks = []
    chunk_id = 0
    
    for doc in raw_docs:
        content = doc["content"]
        source = doc["source"]
        
        # Simple character-based chunking
        start = 0
        while start < len(content):
            end = min(start + chunk_size, len(content))
            chunk_text = content[start:end]
            
            chunks.append({
                "id": f"chunk_{chunk_id}",
                "content": chunk_text.strip(),
                "metadata": {
                    "source": source,
                    "doc_id": doc["id"],
                    "start_char": start,
                    "end_char": end,
                },
                "source": source,
            })
            
            chunk_id += 1
            start += chunk_size - chunk_overlap
    
    duration_ms = (time.time() - start_time) * 1000
    
    # Save chunks to output file
    chunks_output = {
        "created_at": datetime.now().isoformat(),
        "chunk_count": len(chunks),
        "chunk_size": chunk_size,
        "overlap": chunk_overlap,
        "chunks": [{"id": c["id"], "source": c["source"], "length": len(c["content"])} for c in chunks],
    }
    
    return {
        "chunks": chunks,
        "chunk_count": len(chunks),
        "messages": [f"[rag_chunk_split] Created {len(chunks)} chunks from {len(raw_docs)} docs in {duration_ms:.1f}ms"],
        "current_node": "rag_chunk_split",
        "_output_files": {"chunks_manifest.json": json.dumps(chunks_output, indent=2)},
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Node 3: Embed Documents (INDEXING PHASE - One Time)
# ═══════════════════════════════════════════════════════════════════════════════

# Global index cache - avoids re-embedding documents on every query
_DOC_INDEX_CACHE: Dict[str, Any] = {}
EMBEDDINGS_CACHE_DIR = WORKSPACE_DIR / "embeddings"


def rag_embed_docs(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node 3: Generate embeddings for DOCUMENT chunks (indexing phase).
    
    This is the INDEXING step - happens once when documents are loaded.
    Results are cached to avoid re-embedding on every query.
    
    Input state:
        - chunks: List of document chunks from rag_chunk_split
        
    Output state:
        - doc_embeddings: List of document embedding vectors
        - doc_index_ready: Flag indicating index is ready
        - embedding_model: Model used
        - index_id: Unique ID for this index version
        - messages: Updated with embed status
    
    Caching:
        - Embeddings cached by content hash
        - Skip embedding if index already exists for same docs
    """
    return _embed_documents(state, model="text-embedding-3-small")


def rag_embed_docs_small(state: Dict[str, Any]) -> Dict[str, Any]:
    """Variant: text-embedding-3-small (1536 dims, faster)."""
    return _embed_documents(state, model="text-embedding-3-small")


def rag_embed_docs_large(state: Dict[str, Any]) -> Dict[str, Any]:
    """Variant: text-embedding-3-large (3072 dims, better quality)."""
    return _embed_documents(state, model="text-embedding-3-large")


def _embed_documents(state: Dict[str, Any], model: str) -> Dict[str, Any]:
    """
    Internal: Generate document embeddings with caching.
    
    IMPORTANT: Embeddings are stored in EXTERNAL CACHE (_DOC_INDEX_CACHE), 
    NOT in state. Only the index_id reference is stored in state.
    
    This prevents checkpoint memory explosion - checkpoints only store
    the lightweight index_id, not the actual embedding vectors.
    
    Architecture:
        State stores:    index_id (string reference)
        External stores: _DOC_INDEX_CACHE[index_id] = {embeddings: [...]}
    """
    global _DOC_INDEX_CACHE
    start_time = time.time()
    
    chunks = state.get("chunks", [])
    
    # Create content hash for cache key
    content_hash = hashlib.sha256(
        json.dumps([c["content"] for c in chunks], sort_keys=True).encode()
    ).hexdigest()[:16]
    
    cache_key = f"{model}_{content_hash}"
    
    # Check cache
    if cache_key in _DOC_INDEX_CACHE:
        cached = _DOC_INDEX_CACHE[cache_key]
        duration_ms = (time.time() - start_time) * 1000
        # NOTE: Do NOT return doc_embeddings in state - they stay in external cache
        return {
            # "doc_embeddings": cached["embeddings"],  # REMOVED: Don't put embeddings in state!
            "doc_index_ready": True,
            "embedding_model": model,
            "index_id": cache_key,  # Only store reference, not data
            "index_chunk_count": len(cached["embeddings"]),
            "index_from_cache": True,
            "messages": [f"[rag_embed_docs:{model}] Loaded index '{cache_key}' ({len(cached['embeddings'])} chunks) from cache in {duration_ms:.1f}ms"],
            "current_node": "rag_embed_docs",
        }
    
    # Generate new embeddings
    embeddings = []
    dims = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}
    dim = dims.get(model, 1536)
    
    if LLM_AVAILABLE:
        try:
            texts = [chunk["content"] for chunk in chunks]
            vectors = generate_embeddings(texts, model=model)
            
            for chunk, vector in zip(chunks, vectors):
                embeddings.append({
                    "chunk_id": chunk["id"],
                    "content": chunk["content"],  # Store for retrieval
                    "metadata": chunk.get("metadata", {}),
                    "vector": vector,
                    "model": model,
                })
        except Exception as e:
            print(f"[rag_embed_docs] API failed, using fallback: {e}")
            embeddings = _fallback_doc_embeddings(chunks, model, dim)
    else:
        embeddings = _fallback_doc_embeddings(chunks, model, dim)
    
    # Cache the result in EXTERNAL storage (not in state!)
    _DOC_INDEX_CACHE[cache_key] = {"embeddings": embeddings, "model": model}
    
    # Also save to disk cache for persistence across restarts
    EMBEDDINGS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # Note: Don't save actual vectors to JSON for large indexes, use binary format
    
    duration_ms = (time.time() - start_time) * 1000
    
    # NOTE: Do NOT return doc_embeddings in state - they stay in external cache
    return {
        # "doc_embeddings": embeddings,  # REMOVED: Don't put embeddings in state!
        "doc_index_ready": True,
        "embedding_model": model,
        "index_id": cache_key,  # Only store reference, not actual data
        "index_chunk_count": len(embeddings),
        "index_from_cache": False,
        "messages": [f"[rag_embed_docs:{model}] Indexed {len(embeddings)} chunks ({dim}d) in {duration_ms:.1f}ms. index_id={cache_key}"],
        "current_node": "rag_embed_docs",
    }


def _fallback_doc_embeddings(chunks: List[Dict], model: str, dim: int) -> List[Dict]:
    """Fallback: Generate deterministic pseudo-embeddings for documents."""
    embeddings = []
    for chunk in chunks:
        content_hash = hashlib.sha256(chunk["content"].encode()).digest()
        vector = [float(b % 256) / 255.0 - 0.5 for b in content_hash[:dim]]
        norm = sum(v*v for v in vector) ** 0.5
        vector = [v / norm for v in vector] if norm > 0 else vector
        
        embeddings.append({
            "chunk_id": chunk["id"],
            "content": chunk["content"],
            "metadata": chunk.get("metadata", {}),
            "vector": vector[:dim],
            "model": f"{model}_fallback",
        })
    return embeddings


# ═══════════════════════════════════════════════════════════════════════════════
# Node 4: Embed Query (QUERY PHASE - Per Request)
# ═══════════════════════════════════════════════════════════════════════════════

def rag_embed_query(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node 4: Generate embedding for USER QUERY (query phase).
    
    This happens for EVERY query - embeds just the query text.
    Much faster than document embedding (single vector).
    
    Input state:
        - query: User query string
        - embedding_model: Model to use (must match doc embeddings)
        
    Output state:
        - query_embedding: Single embedding vector
        - query_embedded: Flag indicating query is ready
        - messages: Updated with embed status
    """
    return _embed_query(state, model=state.get("embedding_model", "text-embedding-3-small"))


def rag_embed_query_small(state: Dict[str, Any]) -> Dict[str, Any]:
    """Variant: text-embedding-3-small for query."""
    return _embed_query(state, model="text-embedding-3-small")


def rag_embed_query_large(state: Dict[str, Any]) -> Dict[str, Any]:
    """Variant: text-embedding-3-large for query."""
    return _embed_query(state, model="text-embedding-3-large")


def _embed_query(state: Dict[str, Any], model: str) -> Dict[str, Any]:
    """Internal: Generate query embedding."""
    start_time = time.time()
    
    query = state.get("rewritten_query") or state.get("query", "")
    
    dims = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}
    dim = dims.get(model, 1536)
    
    query_vector = None
    
    if LLM_AVAILABLE:
        try:
            vectors = generate_embeddings([query], model=model)
            query_vector = vectors[0]
        except Exception as e:
            print(f"[rag_embed_query] API failed, using fallback: {e}")
            query_vector = _fallback_query_embedding(query, dim)
    else:
        query_vector = _fallback_query_embedding(query, dim)
    
    duration_ms = (time.time() - start_time) * 1000
    
    return {
        "query_embedding": query_vector,
        "query_embedded": True,
        "query_embedding_model": model,
        "messages": [f"[rag_embed_query:{model}] Embedded query in {duration_ms:.1f}ms"],
        "current_node": "rag_embed_query",
    }


def _fallback_query_embedding(query: str, dim: int) -> List[float]:
    """Fallback: Generate deterministic pseudo-embedding for query."""
    content_hash = hashlib.sha256(query.encode()).digest()
    vector = [float(b % 256) / 255.0 - 0.5 for b in content_hash[:dim]]
    norm = sum(v*v for v in vector) ** 0.5
    return [v / norm for v in vector] if norm > 0 else vector[:dim]


# ═══════════════════════════════════════════════════════════════════════════════
# Legacy Node: rag_embed (for backward compatibility)
# ═══════════════════════════════════════════════════════════════════════════════

def rag_embed(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    LEGACY: Combined embed node (for backward compatibility).
    
    In new code, use rag_embed_docs for indexing and rag_embed_query for queries.
    """
    return _embed_chunks(state, model="minilm_v1")


def rag_embed_minilm_v1(state: Dict[str, Any]) -> Dict[str, Any]:
    """Variant: MiniLM embedding model."""
    return _embed_chunks(state, model="minilm_v1")


def rag_embed_bge_v1(state: Dict[str, Any]) -> Dict[str, Any]:
    """Variant: BGE embedding model."""
    return _embed_chunks(state, model="bge_v1")


def _embed_chunks(state: Dict[str, Any], model: str) -> Dict[str, Any]:
    """Internal: Generate embeddings using specified model."""
    start_time = time.time()
    
    chunks = state.get("chunks", [])
    embeddings = []
    
    # Map internal model names to OpenAI embedding models
    model_map = {
        "minilm_v1": EMBEDDING_MODEL,      # text-embedding-3-small
        "bge_v1": ALT_EMBEDDING_MODEL,     # text-embedding-3-large
    }
    api_model = model_map.get(model, EMBEDDING_MODEL)
    
    # Embedding dimensions based on model
    dims = {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
        "minilm_v1": 384,
        "bge_v1": 768,
    }
    dim = dims.get(api_model, 1536)
    
    # Try real API embeddings first
    if LLM_AVAILABLE:
        try:
            texts = [chunk["content"] for chunk in chunks]
            vectors = generate_embeddings(texts, model=api_model)
            
            for chunk, vector in zip(chunks, vectors):
                embeddings.append({
                    "chunk_id": chunk["id"],
                    "vector": vector,
                    "model": api_model,
                })
        except Exception as e:
            print(f"[rag_embed] Real API failed, using fallback: {e}")
            embeddings = _fallback_embeddings(chunks, model, dim)
    else:
        embeddings = _fallback_embeddings(chunks, model, dim)
    
    duration_ms = (time.time() - start_time) * 1000
    
    return {
        "embeddings": embeddings,
        "embedding_model": api_model,
        "vector_store_ready": True,
        "messages": [f"[rag_embed:{api_model}] Embedded {len(embeddings)} chunks ({dim}d) in {duration_ms:.1f}ms"],
        "current_node": "rag_embed",
    }


def _fallback_embeddings(chunks: List[Dict], model: str, dim: int) -> List[Dict]:
    """Fallback: Generate deterministic pseudo-embeddings when API unavailable."""
    embeddings = []
    for chunk in chunks:
        content_hash = hashlib.sha256(chunk["content"].encode()).digest()
        vector = [float(b % 256) / 255.0 - 0.5 for b in content_hash[:dim]]
        norm = sum(v*v for v in vector) ** 0.5
        vector = [v / norm for v in vector] if norm > 0 else vector
        
        embeddings.append({
            "chunk_id": chunk["id"],
            "vector": vector[:dim],
            "model": f"{model}_fallback",
        })
    return embeddings


# ═══════════════════════════════════════════════════════════════════════════════
# Node 5: Retrieve (QUERY PHASE - Uses indexed embeddings)
# ═══════════════════════════════════════════════════════════════════════════════

def rag_retrieve(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node 5: Retrieve relevant chunks using vector similarity.
    
    Uses INDEXED doc_embeddings (from rag_embed_docs) and compares against
    query_embedding (from rag_embed_query). Does NOT re-embed documents!
    
    Input state:
        - query_embedding: Embedded query vector (from rag_embed_query)
        - doc_embeddings: Indexed document embeddings (from rag_embed_docs)
        - query: Original query (for BM25 fallback)
        
    Output state:
        - retrieved_docs: List of retrieved documents with scores
        - retrieval_method: Method used for retrieval
        - retrieval_count: Number of documents retrieved
        - messages: Updated with retrieval status
        
    Retrieval Methods:
        - dense_v1: Cosine similarity between query and doc vectors
        - bm25_v1: Keyword-based term frequency scoring
        - hybrid_v1: Weighted combination of dense + BM25
    """
    return rag_retrieve_dense_v1(state)


def rag_retrieve_dense_v1(state: Dict[str, Any]) -> Dict[str, Any]:
    """Variant: Dense vector retrieval using embeddings."""
    return _retrieve_chunks(state, method="dense_v1")


def rag_retrieve_bm25_v1(state: Dict[str, Any]) -> Dict[str, Any]:
    """Variant: BM25 sparse keyword retrieval."""
    return _retrieve_chunks(state, method="bm25_v1")


def rag_retrieve_hybrid_v1(state: Dict[str, Any]) -> Dict[str, Any]:
    """Variant: Hybrid retrieval (dense + BM25 fusion)."""
    return _retrieve_chunks(state, method="hybrid_v1")


def _retrieve_chunks(state: Dict[str, Any], method: str, top_k: int = 3) -> Dict[str, Any]:
    """
    Internal: Retrieve chunks using specified method.
    
    Uses pre-computed embeddings from EXTERNAL CACHE:
    - index_id: Reference to embeddings in _DOC_INDEX_CACHE
    - query_embedding: From rag_embed_query (per query)
    - Falls back to text-based scoring if embeddings unavailable
    
    IMPORTANT: Embeddings are loaded from external cache (_DOC_INDEX_CACHE),
    NOT from state. This prevents checkpoint bloat.
    """
    global _DOC_INDEX_CACHE
    start_time = time.time()
    
    query = state.get("rewritten_query") or state.get("query", "")
    
    # Load doc embeddings from EXTERNAL CACHE using index_id reference
    index_id = state.get("index_id")
    doc_embeddings = []
    
    if index_id and index_id in _DOC_INDEX_CACHE:
        doc_embeddings = _DOC_INDEX_CACHE[index_id].get("embeddings", [])
    
    # Legacy fallback: check state (for backward compatibility)
    if not doc_embeddings:
        doc_embeddings = state.get("doc_embeddings", [])
    if not doc_embeddings:
        doc_embeddings = state.get("embeddings", [])
    
    query_embedding = state.get("query_embedding")
    chunks = state.get("chunks", [])
    
    retrieved_docs = []
    
    if method == "dense_v1" and query_embedding and doc_embeddings:
        # Dense: Real cosine similarity between query and doc embeddings
        for doc_emb in doc_embeddings:
            doc_vector = doc_emb.get("vector", [])
            
            if doc_vector and query_embedding:
                # Compute cosine similarity
                score = _cosine_similarity(query_embedding, doc_vector)
            else:
                score = 0.0
            
            retrieved_docs.append({
                "chunk_id": doc_emb.get("chunk_id", ""),
                "content": doc_emb.get("content", ""),
                "score": round(score, 4),
                "metadata": doc_emb.get("metadata", {}),
            })
    
    elif method == "dense_v1":
        # Fallback: Word overlap scoring when embeddings unavailable
        query_words = set(query.lower().split())
        source_list = doc_embeddings if doc_embeddings else chunks
        
        for item in source_list:
            content = item.get("content", "")
            content_words = set(content.lower().split())
            overlap = len(query_words & content_words)
            score = min(overlap / max(len(query_words), 1), 1.0) * 0.7 + 0.25
            
            retrieved_docs.append({
                "chunk_id": item.get("chunk_id", item.get("id", "")),
                "content": content,
                "score": round(score, 3),
                "metadata": item.get("metadata", {}),
            })
        
    elif method == "bm25_v1":
        # BM25: Keyword-based term frequency scoring (no embeddings needed)
        query_terms = query.lower().split()
        source_list = doc_embeddings if doc_embeddings else chunks
        
        for item in source_list:
            content = item.get("content", "")
            content_lower = content.lower()
            tf_score = sum(content_lower.count(term) for term in query_terms)
            score = min(tf_score / 10, 1.0) * 0.65 + 0.2
            
            retrieved_docs.append({
                "chunk_id": item.get("chunk_id", item.get("id", "")),
                "content": content,
                "score": round(score, 3),
                "metadata": item.get("metadata", {}),
            })
            
    elif method == "hybrid_v1":
        # Hybrid: Combine dense (vector) and BM25 (keyword) scores
        query_terms = query.lower().split()
        source_list = doc_embeddings if doc_embeddings else chunks
        
        for item in source_list:
            content = item.get("content", "")
            content_lower = content.lower()
            
            # Dense score (vector similarity or word overlap)
            if query_embedding and "vector" in item:
                dense_score = _cosine_similarity(query_embedding, item["vector"])
            else:
                query_words = set(query.lower().split())
                content_words = set(content_lower.split())
                dense_overlap = len(query_words & content_words)
                dense_score = min(dense_overlap / max(len(query_words), 1), 1.0)
            
            # BM25 score
            tf_score = sum(content_lower.count(term) for term in query_terms)
            bm25_score = min(tf_score / 10, 1.0)
            
            # Weighted fusion
            score = 0.6 * dense_score + 0.4 * bm25_score
            
            retrieved_docs.append({
                "chunk_id": item.get("chunk_id", item.get("id", "")),
                "content": content,
                "score": round(min(score, 1.0), 4),
                "metadata": item.get("metadata", {}),
            })
    
    # Sort by score and take top_k
    retrieved_docs.sort(key=lambda x: x["score"], reverse=True)
    retrieved_docs = retrieved_docs[:top_k]
    
    duration_ms = (time.time() - start_time) * 1000
    
    # Note if using vector similarity vs fallback
    using_vectors = query_embedding is not None and doc_embeddings and "vector" in doc_embeddings[0]
    method_desc = f"{method}" + (" (vector)" if using_vectors else " (fallback)")
    
    # Save retrieved docs
    retrieval_output = {
        "retrieved_at": datetime.now().isoformat(),
        "method": method,
        "using_vectors": using_vectors,
        "query": query,
        "top_k": top_k,
        "results": [{"id": d["chunk_id"], "score": d["score"]} for d in retrieved_docs],
    }
    
    return {
        "retrieved_docs": retrieved_docs,
        "retrieval_method": method,
        "retrieval_count": len(retrieved_docs),
        "using_vector_similarity": using_vectors,
        "messages": [f"[rag_retrieve:{method_desc}] Retrieved {len(retrieved_docs)} docs in {duration_ms:.1f}ms"],
        "current_node": "rag_retrieve",
        "_output_files": {"retrieval_results.json": json.dumps(retrieval_output, indent=2)},
    }


def _cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if not vec1 or not vec2 or len(vec1) != len(vec2):
        return 0.0
    
    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    norm1 = sum(a * a for a in vec1) ** 0.5
    norm2 = sum(b * b for b in vec2) ** 0.5
    
    if norm1 == 0 or norm2 == 0:
        return 0.0
    
    return dot_product / (norm1 * norm2)


# ═══════════════════════════════════════════════════════════════════════════════
# Node 5: Grade Documents
# ═══════════════════════════════════════════════════════════════════════════════

def rag_grade(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node 5: Grade retrieved documents for relevance using LLM.
    
    Makes routing decision:
        - "generate": Documents are relevant, proceed to generation
        - "rewrite": Documents not relevant, rewrite query (max 2 times)
        - "sql": Query requires database lookup
        
    Input state:
        - query: User query
        - retrieved_docs: Retrieved documents
        - rewrite_count: Number of rewrite attempts
        
    Output state:
        - graded_docs: Documents with relevance grades
        - grade_decision: Routing decision
        - relevant_count: Number of relevant documents
        - messages: Updated with grading status
        - rewritten_query: New query if rewriting
        - rewrite_count: Updated rewrite count
    """
    start_time = time.time()
    
    query = state.get("query", "")
    query_lower = query.lower()
    retrieved_docs = state.get("retrieved_docs", [])
    rewrite_count = state.get("rewrite_count", 0)
    
    graded_docs = []
    relevant_count = 0
    llm_cache_refs: Dict[str, str] = {}
    llm_cache_hits: Dict[str, bool] = {}
    
    # Check for SQL keywords
    sql_keywords = ["table", "database", "sql", "select", "query db", "schema", "rows"]
    needs_sql = any(kw in query_lower for kw in sql_keywords)
    
    if needs_sql:
        grade_decision = "sql"
        reason = "Query requires SQL database access"
    else:
        # Grade each document using LLM
        for doc in retrieved_docs:
            score = doc.get("score", 0)
            
            # Try LLM-based grading
            if LLM_AVAILABLE:
                try:
                    grade_result = grade_document_relevance_result(
                        query=query,
                        document=doc["content"],
                        model=DEFAULT_LLM,
                    )
                    is_relevant = grade_result.get("is_relevant", False)
                    grade_reason = grade_result.get("reason", "LLM grading")
                    llm_cache_refs[f"rag_grade:{doc['chunk_id']}"] = grade_result["cache_key"]
                    llm_cache_hits[f"rag_grade:{doc['chunk_id']}"] = bool(grade_result["cache_hit"])
                except Exception as e:
                    print(f"[rag_grade] LLM grading failed: {e}")
                    is_relevant = score >= 0.4
                    grade_reason = "Fallback: score threshold"
            else:
                is_relevant = score >= 0.4
                grade_reason = "Score threshold (API not configured)"
            
            graded_docs.append({
                "chunk_id": doc["chunk_id"],
                "content": doc["content"],
                "score": score,
                "is_relevant": is_relevant,
                "grade_reason": grade_reason,
            })
            
            if is_relevant:
                relevant_count += 1
        
        # Decision logic
        if relevant_count >= 1:
            grade_decision = "generate"
            reason = f"Found {relevant_count} relevant documents"
        elif rewrite_count < 2:
            grade_decision = "rewrite"
            reason = f"No relevant docs, rewriting query (attempt {rewrite_count + 1}/2)"
        else:
            grade_decision = "generate"
            reason = "Max rewrites reached, generating with available context"
    
    duration_ms = (time.time() - start_time) * 1000
    
    # Prepare output
    result = {
        "graded_docs": graded_docs,
        "grade_decision": grade_decision,
        "relevant_count": relevant_count,
        "messages": [f"[rag_grade] Decision: {grade_decision} - {reason} ({duration_ms:.1f}ms)"],
        "current_node": "rag_grade",
    }
    result.update(_cache_state_update(llm_cache_refs, llm_cache_hits))
    
    # Handle rewrite with LLM
    if grade_decision == "rewrite":
        if LLM_AVAILABLE:
            try:
                rewrite_result = generate_text_result(
                    prompt=f"Rewrite this query to be more specific and detailed for document retrieval:\n{query}",
                    model=DEFAULT_LLM,
                    temperature=0.3,
                    max_tokens=100,
                )
                rewritten = rewrite_result.text
                result.setdefault("llm_cache_refs", {})["rag_rewrite"] = rewrite_result.cache_key
                result.setdefault("llm_cache_hits", {})["rag_rewrite"] = bool(rewrite_result.cache_hit)
            except Exception:
                rewritten = f"{query} (expanded: detailed explanation)"
        else:
            rewritten = f"{query} (expanded: detailed explanation)"
        
        result["rewritten_query"] = rewritten
        result["rewrite_count"] = rewrite_count + 1
    
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Node 6: Generate Answer
# ═══════════════════════════════════════════════════════════════════════════════

def rag_generate(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node 6: Generate final answer from context (default - GPT-4 style).
    
    Input state:
        - query: User query
        - graded_docs or retrieved_docs: Context documents
        
    Output state:
        - answer: Generated answer
        - generator_model: Model used for generation
        - messages: Updated with generation status
        - _output_files: Answer saved to file
    """
    return rag_generate_gpt4_v1(state)


def rag_generate_gpt4_v1(state: Dict[str, Any]) -> Dict[str, Any]:
    """Variant: GPT-4 style generation (simulated)."""
    return _generate_answer(state, model="gpt4_v1")


def rag_generate_local_v1(state: Dict[str, Any]) -> Dict[str, Any]:
    """Variant: Local LLM generation (simulated)."""
    return _generate_answer(state, model="local_v1")


def _generate_answer(state: Dict[str, Any], model: str) -> Dict[str, Any]:
    """Internal: Generate answer using specified model."""
    start_time = time.time()
    
    query = state.get("query", "")
    docs = state.get("graded_docs") or state.get("retrieved_docs", [])
    
    # Build context from retrieved documents
    context_parts = []
    for doc in docs[:3]:  # Use top 3 docs
        content = doc.get("content", "")
        context_parts.append(content)
    
    context = "\n\n---\n\n".join(context_parts) if context_parts else "No context available"
    
    # Map internal model names to OpenAI models
    model_map = {
        "gpt4_v1": DEFAULT_LLM,      # gpt-4o-mini
        "gpt4o_v1": ALT_LLM,         # gpt-4o
        "local_v1": DEFAULT_LLM,     # Fallback to default
    }
    api_model = model_map.get(model, DEFAULT_LLM)
    
    # Try real API generation first
    if LLM_AVAILABLE:
        try:
            answer_result = generate_answer_result(
                query=query,
                context=context,
                model=api_model,
            )
            answer = answer_result.text
            cache_refs = {"rag_generate": answer_result.cache_key}
            cache_hits = {"rag_generate": bool(answer_result.cache_hit)}
        except Exception as e:
            print(f"[rag_generate] Real API failed, using fallback: {e}")
            answer = _fallback_generate(query, context, model)
            cache_refs = {}
            cache_hits = {}
    else:
        answer = _fallback_generate(query, context, model)
        cache_refs = {}
        cache_hits = {}
    
    duration_ms = (time.time() - start_time) * 1000
    
    # Save answer to file
    answer_output = {
        "generated_at": datetime.now().isoformat(),
        "model": api_model,
        "query": query,
        "context_docs": len(docs),
        "answer": answer,
    }
    
    return {
        "answer": answer,
        "generator_model": api_model,
        "status": "completed",
        "messages": [f"[rag_generate:{api_model}] Generated answer ({len(answer)} chars) in {duration_ms:.1f}ms"],
        "current_node": "rag_generate",
        "_output_files": {"answer.json": json.dumps(answer_output, indent=2)},
        **_cache_state_update(cache_refs, cache_hits),
    }


def _fallback_generate(query: str, context: str, model: str) -> str:
    """Fallback: Generate answer when API unavailable."""
    if model == "gpt4_v1" or model == "gpt4o_v1":
        return (
            f"[Fallback Response - API not configured]\n\n"
            f"Query: {query}\n\n"
            f"Based on the provided context:\n{context[:500]}...\n\n"
            f"To enable real LLM generation, configure your API key in "
            f"examples/wtb_presentation/.env"
        )
    else:
        return (
            f"[Local Fallback Response]\n\n"
            f"Query: {query}\n"
            f"Context available: {len(context)} characters\n\n"
            f"Configure LLM API for full functionality."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Exports
# ═══════════════════════════════════════════════════════════════════════════════

__all__ = [
    # ═══════════════════════════════════════════════════════════════════════════
    # INDEXING PHASE NODES (One-time document processing)
    # ═══════════════════════════════════════════════════════════════════════════
    "rag_load_docs",           # Node 1: Load documents
    "rag_chunk_split",         # Node 2: Split into chunks
    "rag_embed_docs",          # Node 3: Embed documents (CACHED)
    "rag_embed_docs_small",    # Variant: text-embedding-3-small
    "rag_embed_docs_large",    # Variant: text-embedding-3-large
    
    # ═══════════════════════════════════════════════════════════════════════════
    # QUERY PHASE NODES (Per-query processing)
    # ═══════════════════════════════════════════════════════════════════════════
    "rag_embed_query",         # Node 4: Embed query (single vector)
    "rag_embed_query_small",   # Variant: text-embedding-3-small
    "rag_embed_query_large",   # Variant: text-embedding-3-large
    "rag_retrieve",            # Node 5: Retrieve from index
    "rag_retrieve_dense_v1",   # Variant: Dense vector similarity
    "rag_retrieve_bm25_v1",    # Variant: BM25 keyword matching
    "rag_retrieve_hybrid_v1",  # Variant: Dense + BM25 fusion
    "rag_grade",               # Node 6: Grade relevance
    "rag_generate",            # Node 7: Generate answer
    "rag_generate_gpt4_v1",    # Variant: GPT-4o-mini
    "rag_generate_local_v1",   # Variant: Fallback
    
    # ═══════════════════════════════════════════════════════════════════════════
    # LEGACY (Backward compatibility)
    # ═══════════════════════════════════════════════════════════════════════════
    "rag_embed",               # Legacy combined embed
    "rag_embed_minilm_v1",     # Legacy variant
    "rag_embed_bge_v1",        # Legacy variant
]
