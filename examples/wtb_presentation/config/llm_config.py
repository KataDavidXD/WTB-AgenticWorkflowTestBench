"""
LLM configuration for the WTB presentation workflow.

This module keeps the original demo-facing API stable while routing the real
work through the shared LangChain/OpenAI helper in WTB infrastructure.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from wtb.infrastructure.llm import (
    LangChainOpenAIConfig,
    TextGenerationResult,
    get_service as get_langchain_openai_service,
)

# Load environment variables
try:
    from dotenv import load_dotenv

    ENV_PATH = Path(__file__).parent.parent / ".env"
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)

    ENV_LOCAL_PATH = Path(__file__).parent.parent / "env.local"
    if ENV_LOCAL_PATH.exists():
        load_dotenv(ENV_LOCAL_PATH)
except ImportError:
    pass


def _as_bool(value: str, default: bool) -> bool:
    """Parse a forgiving boolean environment variable value."""
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


PRESENTATION_DIR = Path(__file__).parent.parent
DATA_DIR = PRESENTATION_DIR / "data"
DEFAULT_RESPONSE_CACHE_PATH = DATA_DIR / "llm_response_cache.db"

# LLM configuration
LLM_BASE_URL = os.getenv("LLM_BASE_URL", os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"))
LLM_API_KEY = os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", ""))
DEFAULT_LLM = os.getenv("DEFAULT_LLM", "gpt-4o-mini")
ALT_LLM = os.getenv("ALT_LLM", "gpt-4o")

# Embedding configuration
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
ALT_EMBEDDING_MODEL = os.getenv("ALT_EMBEDDING_MODEL", "text-embedding-3-large")

# Cache and debug configuration
RESPONSE_CACHE_ENABLED = _as_bool(
    os.getenv("WTB_LLM_RESPONSE_CACHE_ENABLED", "true"),
    default=True,
)
RESPONSE_CACHE_PATH = os.getenv(
    "WTB_LLM_CACHE_PATH",
    str(DEFAULT_RESPONSE_CACHE_PATH),
)
DEBUG = _as_bool(os.getenv("DEBUG", os.getenv("WTB_LLM_DEBUG", "false")), default=False)


def _require_api_key() -> None:
    """Raise a clear error when no API key is configured."""
    if not LLM_API_KEY:
        raise ValueError(
            "LLM_API_KEY or OPENAI_API_KEY not configured. "
            "Create a .env file in examples/wtb_presentation/ with your API key."
        )


def _get_service_config() -> LangChainOpenAIConfig:
    """Build the presentation workflow's shared LangChain/OpenAI config."""
    _require_api_key()
    return LangChainOpenAIConfig(
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
        default_text_model=DEFAULT_LLM,
        default_embedding_model=EMBEDDING_MODEL,
        response_cache_path=RESPONSE_CACHE_PATH,
        response_cache_enabled=RESPONSE_CACHE_ENABLED,
        debug=DEBUG,
    )


def _get_service():
    """Return the memoized shared service for this presentation config."""
    return get_langchain_openai_service(_get_service_config())


def _parse_grade_response(response_text: str) -> Dict[str, Any]:
    """Parse JSON-like grading output with a resilient fallback."""
    try:
        start = response_text.find("{")
        end = response_text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(response_text[start:end])
    except json.JSONDecodeError:
        pass

    is_relevant = "true" in response_text.lower() or "relevant" in response_text.lower()
    return {"is_relevant": is_relevant, "reason": response_text[:100]}


def get_llm_client():
    """
    Get the cached raw OpenAI client used by the presentation workflow.
    """
    return _get_service().get_openai_client()


def get_embedding_client():
    """
    Get the cached raw OpenAI client used for embeddings.
    """
    return get_llm_client()


def generate_text_result(
    prompt: str,
    model: str = None,
    system_prompt: str = None,
    temperature: float = 0.7,
    max_tokens: int = 1024,
) -> TextGenerationResult:
    """
    Generate text and return cache metadata for node-level callers.
    """
    if DEBUG:
        resolved_model = model or DEFAULT_LLM
        print(f"[LLM] Generating with {resolved_model}: {prompt[:50]}...")

    return _get_service().generate_text_result(
        prompt=prompt,
        model=model or DEFAULT_LLM,
        system_prompt=system_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def generate_text(
    prompt: str,
    model: str = None,
    system_prompt: str = None,
    temperature: float = 0.7,
    max_tokens: int = 1024,
) -> str:
    """
    Generate text using the configured LLM.
    """
    return generate_text_result(
        prompt=prompt,
        model=model,
        system_prompt=system_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
    ).text


def generate_embeddings(
    texts: List[str],
    model: str = None,
) -> List[List[float]]:
    """
    Generate embeddings for a list of texts.
    """
    if DEBUG:
        resolved_model = model or EMBEDDING_MODEL
        print(f"[Embedding] Generating {len(texts)} embeddings with {resolved_model}")

    return _get_service().generate_embeddings(
        texts=texts,
        model=model or EMBEDDING_MODEL,
    )


def grade_document_relevance_result(
    query: str,
    document: str,
    model: str = None,
) -> Dict[str, Any]:
    """
    Grade a document and include cache metadata for workflow nodes.
    """
    resolved_model = model or DEFAULT_LLM
    system_prompt = """You are a relevance grading assistant.
    Given a query and a document, determine if the document is relevant to answering the query.
    Respond with JSON: {"is_relevant": true/false, "reason": "brief explanation"}
    Be strict - only mark as relevant if the document directly helps answer the query."""

    prompt = f"""Query: {query}

Document:
{document[:1000]}

Is this document relevant to the query? Respond with JSON only."""

    generation = generate_text_result(
        prompt=prompt,
        model=resolved_model,
        system_prompt=system_prompt,
        temperature=0.1,
        max_tokens=100,
    )
    parsed = _parse_grade_response(generation.text)

    return {
        "is_relevant": bool(parsed.get("is_relevant", False)),
        "reason": parsed.get("reason", generation.text[:100]),
        "raw_response": generation.text,
        "cache_key": generation.cache_key,
        "cache_hit": generation.cache_hit,
        "duration_ms": generation.duration_ms,
        "model": generation.model,
    }


def grade_document_relevance(
    query: str,
    document: str,
    model: str = None,
) -> Dict[str, Any]:
    """
    Grade a document's relevance to a query using the configured LLM.
    """
    result = grade_document_relevance_result(
        query=query,
        document=document,
        model=model,
    )
    return {
        "is_relevant": result["is_relevant"],
        "reason": result["reason"],
    }


def generate_answer_result(
    query: str,
    context: str,
    model: str = None,
) -> TextGenerationResult:
    """
    Generate an answer and include cache metadata for workflow nodes.
    """
    resolved_model = model or DEFAULT_LLM
    system_prompt = """You are a helpful assistant that answers questions based on provided context.
    Use the context to provide accurate, detailed answers.
    If the context doesn't contain relevant information, say so.
    Always cite specific facts from the context when possible."""

    prompt = f"""Context:
{context}

Question: {query}

Please provide a detailed answer based on the context above."""

    return generate_text_result(
        prompt=prompt,
        model=resolved_model,
        system_prompt=system_prompt,
        temperature=0.3,
        max_tokens=1024,
    )


def generate_answer(
    query: str,
    context: str,
    model: str = None,
) -> str:
    """
    Generate an answer using RAG context.
    """
    return generate_answer_result(
        query=query,
        context=context,
        model=model,
    ).text


@dataclass
class ModelVariant:
    """Configuration for a model variant."""

    name: str
    model_id: str
    description: str
    temperature: float = 0.7
    max_tokens: int = 1024


GENERATION_VARIANTS = {
    "gpt4o_mini": ModelVariant(
        name="gpt4o_mini",
        model_id=DEFAULT_LLM,
        description="GPT-4o-mini - Fast and cost-effective",
        temperature=0.7,
    ),
    "gpt4o": ModelVariant(
        name="gpt4o",
        model_id=ALT_LLM,
        description="GPT-4o - Higher quality, more expensive",
        temperature=0.5,
    ),
}

EMBEDDING_VARIANTS = {
    "small": ModelVariant(
        name="small",
        model_id=EMBEDDING_MODEL,
        description="text-embedding-3-small - Fast, good quality",
    ),
    "large": ModelVariant(
        name="large",
        model_id=ALT_EMBEDDING_MODEL,
        description="text-embedding-3-large - Best quality",
    ),
}


def get_model_variant(variant_type: str, variant_name: str) -> ModelVariant:
    """Get a model variant by type and name."""
    variants = GENERATION_VARIANTS if variant_type == "generation" else EMBEDDING_VARIANTS
    return variants.get(variant_name, list(variants.values())[0])


def check_api_connection() -> bool:
    """
    Check if the configured API connection is working.

    This intentionally bypasses the response cache.
    """
    try:
        client = get_llm_client()
        client.chat.completions.create(
            model=DEFAULT_LLM,
            messages=[{"role": "user", "content": "test"}],
            max_tokens=5,
        )
        return True
    except Exception as exc:
        print(f"[LLM] Connection check failed: {exc}")
        return False


def get_config_summary() -> Dict[str, Any]:
    """Get a summary of current LLM and cache configuration."""
    return {
        "base_url": LLM_BASE_URL,
        "api_key_configured": bool(LLM_API_KEY),
        "default_llm": DEFAULT_LLM,
        "alt_llm": ALT_LLM,
        "embedding_model": EMBEDDING_MODEL,
        "alt_embedding_model": ALT_EMBEDDING_MODEL,
        "response_cache_enabled": RESPONSE_CACHE_ENABLED,
        "response_cache_path": RESPONSE_CACHE_PATH if RESPONSE_CACHE_ENABLED else None,
        "response_cache_active": RESPONSE_CACHE_ENABLED and bool(RESPONSE_CACHE_PATH),
        "debug": DEBUG,
    }


__all__ = [
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "DEFAULT_LLM",
    "ALT_LLM",
    "EMBEDDING_MODEL",
    "ALT_EMBEDDING_MODEL",
    "get_llm_client",
    "get_embedding_client",
    "generate_text",
    "generate_text_result",
    "generate_embeddings",
    "grade_document_relevance",
    "grade_document_relevance_result",
    "generate_answer",
    "generate_answer_result",
    "ModelVariant",
    "GENERATION_VARIANTS",
    "EMBEDDING_VARIANTS",
    "get_model_variant",
    "check_api_connection",
    "get_config_summary",
]
