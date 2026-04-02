"""
Per-Node Virtual Environment Specifications.

Defines EnvSpec configurations for each node in the workflow,
enabling isolated Python environments with specific dependencies.

Architecture:
    ┌────────────────────────────────────────────────────────────────┐
    │                   Per-Node Venv Isolation                       │
    │                                                                 │
    │  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐│
    │  │ rag_embed venv  │  │rag_retrieve venv│  │sql_agent venv   ││
    │  │                 │  │                 │  │                 ││
    │  │ sentence-       │  │ faiss-cpu       │  │ langchain-      ││
    │  │ transformers    │  │ numpy           │  │ community       ││
    │  │ torch           │  │                 │  │ sqlalchemy      ││
    │  └─────────────────┘  └─────────────────┘  └─────────────────┘│
    │                                                                 │
    │  UV Venv Manager creates/manages these environments            │
    └────────────────────────────────────────────────────────────────┘

Usage:
    from config.venv_specs import NODE_ENVIRONMENTS, get_env_for_node
    
    # Get env spec for a specific node
    embed_env = get_env_for_node("rag_embed")
    
    # Use in WorkflowProject
    project = WorkflowProject(
        environment=EnvironmentConfig(
            granularity="node",
            node_environments=NODE_ENVIRONMENTS,
        ),
    )
"""

from __future__ import annotations

from typing import Dict, Optional

# Import from WTB SDK
import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from wtb.sdk import EnvSpec


# ═══════════════════════════════════════════════════════════════════════════════
# Node Environment Specifications
# ═══════════════════════════════════════════════════════════════════════════════

LLM_ENV_VARS = {
    "OPENAI_API_KEY": "${OPENAI_API_KEY}",
    "LLM_API_KEY": "${LLM_API_KEY}",
    "OPENAI_BASE_URL": "${OPENAI_BASE_URL}",
    "LLM_BASE_URL": "${LLM_BASE_URL}",
    "WTB_LLM_CACHE_PATH": "${WTB_LLM_CACHE_PATH}",
    "WTB_LLM_RESPONSE_CACHE_ENABLED": "${WTB_LLM_RESPONSE_CACHE_ENABLED}",
}

# Embedding node - requires ML libraries
RAG_EMBED_ENV = EnvSpec(
    python_version="3.12",
    dependencies=[
        "sentence-transformers>=2.2.0",
        "torch>=2.0.0",
        "numpy>=1.24.0",
        "transformers>=4.30.0",
        "openai>=1.0.0",
        "langchain-openai>=0.0.5",
    ],
    env_vars={
        "TOKENIZERS_PARALLELISM": "false",
        **LLM_ENV_VARS,
    },
)

# Retrieval node - requires vector store
RAG_RETRIEVE_ENV = EnvSpec(
    python_version="3.12",
    dependencies=[
        "faiss-cpu>=1.7.0",
        "numpy>=1.24.0",
        "scipy>=1.10.0",
    ],
    env_vars={},
)

# Chunking node - text processing
RAG_CHUNK_ENV = EnvSpec(
    python_version="3.12",
    dependencies=[
        "tiktoken>=0.5.0",
        "langchain-text-splitters>=0.0.1",
    ],
    env_vars={},
)

# Generation node - LLM inference
RAG_GENERATE_ENV = EnvSpec(
    python_version="3.12",
    dependencies=[
        "langchain-core>=0.1.0",
        "langchain-openai>=0.0.5",
        "openai>=1.0.0",
    ],
    env_vars=LLM_ENV_VARS,
)

# SQL Agent node - database tools
SQL_AGENT_ENV = EnvSpec(
    python_version="3.12",
    dependencies=[
        "langchain-community>=0.0.20",
        "sqlalchemy>=2.0.0",
        "langchain-openai>=0.0.5",
        "openai>=1.0.0",
        # sqlite3 is a stdlib module -- no pip install needed
    ],
    env_vars=LLM_ENV_VARS,
)

# Document loading node - minimal deps
RAG_LOAD_ENV = EnvSpec(
    python_version="3.12",
    dependencies=[
        "pyyaml>=6.0",
        "chardet>=5.0.0",
    ],
    env_vars={},
)

# Grade node - LLM for relevance grading
RAG_GRADE_ENV = EnvSpec(
    python_version="3.12",
    dependencies=[
        "langchain-core>=0.1.0",
        "langchain-openai>=0.0.5",
        "openai>=1.0.0",
    ],
    env_vars=LLM_ENV_VARS,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Node Environments Dictionary
# ═══════════════════════════════════════════════════════════════════════════════

NODE_ENVIRONMENTS: Dict[str, EnvSpec] = {
    # RAG Pipeline nodes
    "rag_load_docs": RAG_LOAD_ENV,
    "rag_chunk_split": RAG_CHUNK_ENV,
    "rag_embed": RAG_EMBED_ENV,
    "rag_embed_docs": RAG_EMBED_ENV,
    "rag_embed_query": RAG_EMBED_ENV,
    "rag_retrieve": RAG_RETRIEVE_ENV,
    "rag_grade": RAG_GRADE_ENV,
    "rag_generate": RAG_GENERATE_ENV,
    
    # SQL Agent node
    "sql_agent": SQL_AGENT_ENV,
}


# ═══════════════════════════════════════════════════════════════════════════════
# Variant-Specific Environments
# ═══════════════════════════════════════════════════════════════════════════════

# Variant environments use "node_id:variant_name" as key
VARIANT_ENVIRONMENTS: Dict[str, EnvSpec] = {
    # BGE embeddings need different model
    "rag_embed:bge": EnvSpec(
        python_version="3.12",
        dependencies=[
            "sentence-transformers>=2.2.0",
            "torch>=2.0.0",
            "FlagEmbedding>=1.0.0",
        ],
        env_vars={},
    ),
    
    # Local LLM generation
    "rag_generate:local": EnvSpec(
        python_version="3.12",
        dependencies=[
            "llama-cpp-python>=0.2.0",
            "transformers>=4.30.0",
        ],
        env_vars={},
    ),
    
    # Hybrid retrieval needs both sparse and dense
    "rag_retrieve:hybrid": EnvSpec(
        python_version="3.12",
        dependencies=[
            "faiss-cpu>=1.7.0",
            "rank-bm25>=0.2.0",
            "numpy>=1.24.0",
        ],
        env_vars={},
    ),
}


# ═══════════════════════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════════════════════

def get_env_for_node(node_id: str) -> Optional[EnvSpec]:
    """
    Get environment specification for a node.
    
    Args:
        node_id: The node identifier
        
    Returns:
        EnvSpec for the node, or None if not specified
    """
    return NODE_ENVIRONMENTS.get(node_id)


def get_env_for_variant(node_id: str, variant_name: str) -> Optional[EnvSpec]:
    """
    Get environment specification for a node variant.
    
    Falls back to node environment if variant-specific env not defined.
    
    Args:
        node_id: The node identifier
        variant_name: The variant name
        
    Returns:
        EnvSpec for the variant, or node default, or None
    """
    variant_key = f"{node_id}:{variant_name}"
    
    # Try variant-specific first
    if variant_key in VARIANT_ENVIRONMENTS:
        return VARIANT_ENVIRONMENTS[variant_key]
    
    # Fall back to node default
    return NODE_ENVIRONMENTS.get(node_id)


def get_all_dependencies() -> list:
    """
    Get all unique dependencies across all node environments.
    
    Useful for creating a consolidated requirements.txt.
    
    Returns:
        List of unique dependency strings
    """
    all_deps = set()
    
    for env in NODE_ENVIRONMENTS.values():
        all_deps.update(env.dependencies)
    
    for env in VARIANT_ENVIRONMENTS.values():
        all_deps.update(env.dependencies)
    
    return sorted(all_deps)


# ═══════════════════════════════════════════════════════════════════════════════
# Exports
# ═══════════════════════════════════════════════════════════════════════════════

__all__ = [
    "NODE_ENVIRONMENTS",
    "VARIANT_ENVIRONMENTS",
    "get_env_for_node",
    "get_env_for_variant",
    "get_all_dependencies",
    # Individual env specs
    "RAG_EMBED_ENV",
    "RAG_RETRIEVE_ENV",
    "RAG_CHUNK_ENV",
    "RAG_GENERATE_ENV",
    "RAG_LOAD_ENV",
    "RAG_GRADE_ENV",
    "SQL_AGENT_ENV",
]
