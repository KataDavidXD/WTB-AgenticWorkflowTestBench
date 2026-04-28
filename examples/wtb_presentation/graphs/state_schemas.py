"""
State Schemas for WTB Presentation Demo.

Defines TypedDict state schemas for:
- RAGState: State for RAG pipeline nodes
- SQLState: State for SQL agent node
- UnifiedState: Combined state for unified workflow

These schemas are used by LangGraph StateGraph for type-safe state management.

Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │                     UnifiedState                            │
    │  ┌─────────────────────┐  ┌──────────────────────────────┐ │
    │  │      RAGState       │  │         SQLState             │ │
    │  │ - query             │  │ - sql_query                  │ │
    │  │ - raw_docs          │  │ - sql_result                 │ │
    │  │ - chunks            │  │ - tables_schema              │ │
    │  │ - embeddings        │  │                              │ │
    │  │ - retrieved_docs    │  │                              │ │
    │  │ - graded_docs       │  │                              │ │
    │  │ - grade_decision    │  │                              │ │
    │  │ - answer            │  │                              │ │
    │  └─────────────────────┘  └──────────────────────────────┘ │
    │  Common: messages, status, current_node, metadata          │
    └─────────────────────────────────────────────────────────────┘
"""

from typing import Annotated, Any, Dict, List, Optional, TypedDict
from operator import add


def merge_dicts(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    """
    LangGraph reducer to merge shallow dictionaries.
    
    Used for output file manifests and lightweight cache reference tracking.
    """
    if left is None:
        left = {}
    if right is None:
        right = {}
    return {**left, **right}


class DocumentChunk(TypedDict, total=False):
    """A document chunk with content and metadata."""
    id: str
    content: str
    metadata: Dict[str, Any]
    source: str


class EmbeddingVector(TypedDict, total=False):
    """An embedding vector for a chunk."""
    chunk_id: str
    vector: List[float]
    model: str


class RetrievedDoc(TypedDict, total=False):
    """A retrieved document with relevance score."""
    chunk_id: str
    content: str
    score: float
    metadata: Dict[str, Any]


class GradedDoc(TypedDict, total=False):
    """A graded document with relevance assessment."""
    chunk_id: str
    content: str
    score: float
    is_relevant: bool
    grade_reason: str


class RAGState(TypedDict, total=False):
    """
    State schema for RAG (Retrieval-Augmented Generation) pipeline.
    
    Flow:
        query -> load_docs -> chunks -> embed -> retrieve -> grade -> generate
    
    Each node reads from and writes to specific keys in this state.
    
    IMPORTANT - Memory Optimization:
    ================================
    Embeddings are stored in EXTERNAL cache (_DOC_INDEX_CACHE), NOT in state.
    Only the index_id reference is stored. This prevents checkpoint bloat.
    
    State stores:   index_id (string ~20 bytes)
    External cache: _DOC_INDEX_CACHE[index_id] = {embeddings: [...]} (MBs)
    """
    # Input
    query: str
    
    # Node 1: Load Documents
    raw_docs: List[Dict[str, Any]]
    docs_loaded: int
    
    # Node 2: Chunk/Split
    chunks: List[DocumentChunk]
    chunk_count: int
    
    # Node 3: Embed Documents (indexing phase)
    # NOTE: Actual embeddings stored in external cache, NOT in state!
    # embeddings: List[EmbeddingVector]  # REMOVED - causes checkpoint bloat!
    index_id: str  # Reference to embeddings in external cache
    index_chunk_count: int  # Number of chunks in index
    doc_index_ready: bool  # Flag indicating index is ready
    embedding_model: str
    
    # Node 4: Embed Query (per request)
    query_embedding: List[float]  # Single vector (~6KB), acceptable
    query_embedded: bool
    
    # Node 5: Retrieve
    retrieved_docs: List[RetrievedDoc]
    retrieval_method: str
    retrieval_count: int
    
    # Node 6: Grade
    graded_docs: List[GradedDoc]
    grade_decision: str  # "generate" | "rewrite" | "sql"
    relevant_count: int
    
    # Node 7: Generate
    answer: str
    generator_model: str
    llm_cache_refs: Annotated[Dict[str, str], merge_dicts]
    llm_cache_hits: Annotated[Dict[str, bool], merge_dicts]
    
    # Rewrite loop
    rewrite_count: int
    rewritten_query: str
    
    # Tracking
    messages: Annotated[List[str], add]
    current_node: str
    execution_time_ms: float


class SQLState(TypedDict, total=False):
    """
    State schema for SQL Agent node.
    
    Wraps the complete SQL agent flow in a single node:
        query -> list_tables -> get_schema -> generate_sql -> execute
    """
    # Input
    sql_query: str
    
    # Processing
    tables_schema: Dict[str, List[str]]
    generated_sql: str
    sql_valid: bool
    
    # Output
    sql_result: List[Dict[str, Any]]
    sql_error: Optional[str]
    llm_cache_refs: Annotated[Dict[str, str], merge_dicts]
    llm_cache_hits: Annotated[Dict[str, bool], merge_dicts]
    
    # Tracking
    tables_accessed: List[str]
    execution_time_ms: float


class UnifiedState(TypedDict, total=False):
    """
    Unified state schema combining RAG and SQL workflows.
    
    This is the main state type used by the unified workflow graph.
    
    Flow Decision (at rag_grade):
        - "generate": Continue to rag_generate -> END
        - "rewrite": Loop back to rag_retrieve
        - "sql": Route to sql_agent -> END
    
    IMPORTANT - Memory Optimization:
    ================================
    Embeddings are stored in EXTERNAL cache (_DOC_INDEX_CACHE), NOT in state.
    Only the index_id reference is stored. This prevents checkpoint bloat.
    
    Checkpoints will be lightweight (~KBs) instead of heavy (~MBs).
    """
    # === Common Fields ===
    query: str
    messages: Annotated[List[str], add]
    current_node: str
    status: str  # "running" | "completed" | "failed" | "paused"
    
    # Metadata for tracking
    metadata: Dict[str, Any]
    execution_id: str
    branch_name: str
    
    # === RAG Pipeline Fields ===
    # Node 1: Load Documents
    raw_docs: List[Dict[str, Any]]
    docs_loaded: int
    
    # Node 2: Chunk/Split
    chunks: List[DocumentChunk]
    chunk_count: int
    
    # Node 3: Embed Documents (indexing phase)
    # NOTE: Actual embeddings in external cache, NOT in state!
    # embeddings: List[EmbeddingVector]  # REMOVED - causes checkpoint bloat!
    index_id: str  # Reference to embeddings in external _DOC_INDEX_CACHE
    index_chunk_count: int  # Number of chunks in index
    doc_index_ready: bool  # Flag indicating index is ready
    embedding_model: str
    
    # Node 4: Embed Query (per request)
    query_embedding: List[float]  # Single vector (~6KB), acceptable
    query_embedded: bool
    
    # Node 5: Retrieve
    retrieved_docs: List[RetrievedDoc]
    retrieval_method: str
    retrieval_count: int
    
    # Node 6: Grade
    graded_docs: List[GradedDoc]
    grade_decision: str  # "generate" | "rewrite" | "sql"
    relevant_count: int
    
    # Node 7: Generate
    answer: str
    generator_model: str
    llm_cache_refs: Annotated[Dict[str, str], merge_dicts]
    llm_cache_hits: Annotated[Dict[str, bool], merge_dicts]
    
    # Rewrite loop
    rewrite_count: int
    rewritten_query: str
    
    # === SQL Agent Fields ===
    sql_query: str
    tables_schema: Dict[str, List[str]]
    generated_sql: str
    sql_valid: bool
    sql_result: List[Dict[str, Any]]
    sql_error: Optional[str]
    tables_accessed: List[str]
    
    # === Output Files (for FileTracker) ===
    # Uses merge_dicts reducer to accumulate files from all nodes
    _output_files: Annotated[Dict[str, str], merge_dicts]


def create_initial_state(query: str) -> Dict[str, Any]:
    """
    Create initial state for workflow execution.
    
    Args:
        query: The user query to process
        
    Returns:
        Initial state dictionary ready for workflow execution
    """
    return {
        "query": query,
        "messages": [],
        "current_node": "__start__",
        "status": "running",
        "metadata": {},
        "rewrite_count": 0,
        "grade_decision": "",
        "llm_cache_refs": {},
        "llm_cache_hits": {},
    }


def create_batch_test_states(queries: List[str]) -> List[Dict[str, Any]]:
    """
    Create batch of initial states for multiple queries.
    
    Args:
        queries: List of user queries
        
    Returns:
        List of initial state dictionaries
    """
    return [create_initial_state(q) for q in queries]
