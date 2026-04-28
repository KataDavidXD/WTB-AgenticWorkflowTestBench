"""
Per-Node Ray Resource Configurations.

Defines NodeResourceConfig for each node in the workflow,
enabling fine-grained control over Ray distributed execution.

Architecture:
    ┌────────────────────────────────────────────────────────────────┐
    │                   Ray Resource Allocation                       │
    │                                                                 │
    │  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐│
    │  │ rag_embed       │  │ rag_generate    │  │ sql_agent       ││
    │  │ CPU: 2.0        │  │ CPU: 2.0        │  │ CPU: 1.0        ││
    │  │ GPU: 0.5        │  │ GPU: 1.0        │  │ GPU: 0.0        ││
    │  │ Mem: 4GB        │  │ Mem: 8GB        │  │ Mem: 1GB        ││
    │  └─────────────────┘  └─────────────────┘  └─────────────────┘│
    │                                                                 │
    │  Ray schedules nodes based on resource requirements             │
    └────────────────────────────────────────────────────────────────┘

Usage:
    from config.ray_resources import NODE_RESOURCES, get_resources_for_node
    
    # Get resources for a node
    embed_resources = get_resources_for_node("rag_embed")
    
    # Use in WorkflowProject
    project = WorkflowProject(
        execution=ExecutionConfig(
            batch_executor="ray",
            node_resources=NODE_RESOURCES,
        ),
    )
"""

from __future__ import annotations

from typing import Dict, Optional

# Import from WTB SDK
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from wtb.sdk import NodeResourceConfig, RayConfig


# ═══════════════════════════════════════════════════════════════════════════════
# Default Ray Configuration
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_RAY_CONFIG = RayConfig(
    address="auto",  # Auto-detect local cluster
    max_concurrent_workflows=4,
    object_store_memory=None,  # Use Ray default
    max_retries=2,
    retry_delay=1.0,
)

PRODUCTION_RAY_CONFIG = RayConfig(
    address="ray://cluster:10001",  # Remote cluster
    max_concurrent_workflows=20,
    object_store_memory=4 * 1024 * 1024 * 1024,  # 4GB
    max_retries=3,
    retry_delay=2.0,
)

TESTING_RAY_CONFIG = RayConfig(
    address="auto",
    max_concurrent_workflows=2,
    object_store_memory=None,
    max_retries=1,
    retry_delay=0.5,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Per-Node Resource Configurations
# ═══════════════════════════════════════════════════════════════════════════════

# Embedding node - GPU-intensive
RAG_EMBED_RESOURCES = NodeResourceConfig(
    num_cpus=2.0,
    num_gpus=0.5,  # Share GPU across 2 embed tasks
    memory="4GB",
    custom_resources={},
)

# Retrieval node - CPU-bound vector operations
RAG_RETRIEVE_RESOURCES = NodeResourceConfig(
    num_cpus=2.0,
    num_gpus=0.0,
    memory="2GB",
    custom_resources={},
)

# Chunking node - lightweight
RAG_CHUNK_RESOURCES = NodeResourceConfig(
    num_cpus=1.0,
    num_gpus=0.0,
    memory="1GB",
    custom_resources={},
)

# Generation node - GPU for LLM inference
RAG_GENERATE_RESOURCES = NodeResourceConfig(
    num_cpus=2.0,
    num_gpus=1.0,  # Full GPU for generation
    memory="8GB",
    custom_resources={},
)

# Document loading - I/O bound
RAG_LOAD_RESOURCES = NodeResourceConfig(
    num_cpus=1.0,
    num_gpus=0.0,
    memory="512MB",
    custom_resources={},
)

# Grade node - lightweight LLM call
RAG_GRADE_RESOURCES = NodeResourceConfig(
    num_cpus=1.0,
    num_gpus=0.0,
    memory="1GB",
    custom_resources={},
)

# SQL Agent - database operations
SQL_AGENT_RESOURCES = NodeResourceConfig(
    num_cpus=1.0,
    num_gpus=0.0,
    memory="1GB",
    custom_resources={},
)

# Default for unconfigured nodes
DEFAULT_NODE_RESOURCES = NodeResourceConfig(
    num_cpus=1.0,
    num_gpus=0.0,
    memory="1GB",
    custom_resources={},
)


# ═══════════════════════════════════════════════════════════════════════════════
# Node Resources Dictionary
# ═══════════════════════════════════════════════════════════════════════════════

NODE_RESOURCES: Dict[str, NodeResourceConfig] = {
    # RAG Pipeline nodes
    "rag_load_docs": RAG_LOAD_RESOURCES,
    "rag_chunk_split": RAG_CHUNK_RESOURCES,
    "rag_embed": RAG_EMBED_RESOURCES,
    "rag_embed_docs": RAG_EMBED_RESOURCES,
    "rag_embed_query": RAG_EMBED_RESOURCES,
    "rag_retrieve": RAG_RETRIEVE_RESOURCES,
    "rag_grade": RAG_GRADE_RESOURCES,
    "rag_generate": RAG_GENERATE_RESOURCES,
    
    # SQL Agent node
    "sql_agent": SQL_AGENT_RESOURCES,
}


# ═══════════════════════════════════════════════════════════════════════════════
# Variant-Specific Resources
# ═══════════════════════════════════════════════════════════════════════════════

# Some variants may need different resources
VARIANT_RESOURCES: Dict[str, NodeResourceConfig] = {
    # BGE embeddings - larger model
    "rag_embed:bge": NodeResourceConfig(
        num_cpus=2.0,
        num_gpus=1.0,  # Full GPU for larger model
        memory="8GB",
        custom_resources={},
    ),
    
    # Hybrid retrieval - more CPU
    "rag_retrieve:hybrid": NodeResourceConfig(
        num_cpus=4.0,
        num_gpus=0.0,
        memory="4GB",
        custom_resources={},
    ),
    
    # Local LLM generation - significant resources
    "rag_generate:local": NodeResourceConfig(
        num_cpus=4.0,
        num_gpus=1.0,
        memory="16GB",
        custom_resources={},
    ),
}


# ═══════════════════════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════════════════════

def get_resources_for_node(node_id: str) -> NodeResourceConfig:
    """
    Get resource configuration for a node.
    
    Args:
        node_id: The node identifier
        
    Returns:
        NodeResourceConfig for the node, or default if not specified
    """
    return NODE_RESOURCES.get(node_id, DEFAULT_NODE_RESOURCES)


def get_resources_for_variant(node_id: str, variant_name: str) -> NodeResourceConfig:
    """
    Get resource configuration for a node variant.
    
    Falls back to node resources if variant-specific not defined.
    
    Args:
        node_id: The node identifier
        variant_name: The variant name
        
    Returns:
        NodeResourceConfig for the variant, or node default
    """
    variant_key = f"{node_id}:{variant_name}"
    
    # Try variant-specific first
    if variant_key in VARIANT_RESOURCES:
        return VARIANT_RESOURCES[variant_key]
    
    # Fall back to node default
    return NODE_RESOURCES.get(node_id, DEFAULT_NODE_RESOURCES)


def get_total_resources() -> Dict[str, float]:
    """
    Calculate total resources needed for all nodes.
    
    Useful for cluster sizing.
    
    Returns:
        Dictionary with total CPU, GPU, and memory requirements
    """
    total_cpus = sum(r.num_cpus for r in NODE_RESOURCES.values())
    total_gpus = sum(r.num_gpus for r in NODE_RESOURCES.values())
    
    # Parse memory strings and sum
    def parse_memory_gb(mem_str: str) -> float:
        if mem_str.endswith("GB"):
            return float(mem_str[:-2])
        elif mem_str.endswith("MB"):
            return float(mem_str[:-2]) / 1024
        return 1.0
    
    total_memory_gb = sum(parse_memory_gb(r.memory) for r in NODE_RESOURCES.values())
    
    return {
        "total_cpus": total_cpus,
        "total_gpus": total_gpus,
        "total_memory_gb": total_memory_gb,
    }


def create_ray_config_for_env(env: str = "development") -> RayConfig:
    """
    Create RayConfig for a specific environment.
    
    Args:
        env: "development", "testing", or "production"
        
    Returns:
        Appropriate RayConfig instance
    """
    if env == "production":
        return PRODUCTION_RAY_CONFIG
    elif env == "testing":
        return TESTING_RAY_CONFIG
    else:
        return DEFAULT_RAY_CONFIG


# ═══════════════════════════════════════════════════════════════════════════════
# Exports
# ═══════════════════════════════════════════════════════════════════════════════

__all__ = [
    "NODE_RESOURCES",
    "VARIANT_RESOURCES",
    "DEFAULT_NODE_RESOURCES",
    "get_resources_for_node",
    "get_resources_for_variant",
    "get_total_resources",
    # Ray configs
    "DEFAULT_RAY_CONFIG",
    "PRODUCTION_RAY_CONFIG",
    "TESTING_RAY_CONFIG",
    "create_ray_config_for_env",
    # Individual resources
    "RAG_EMBED_RESOURCES",
    "RAG_RETRIEVE_RESOURCES",
    "RAG_CHUNK_RESOURCES",
    "RAG_GENERATE_RESOURCES",
    "RAG_LOAD_RESOURCES",
    "RAG_GRADE_RESOURCES",
    "SQL_AGENT_RESOURCES",
]
