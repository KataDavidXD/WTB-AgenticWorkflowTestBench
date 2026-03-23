"""
Reusable LangChain/OpenAI helper for demos and workflow nodes.

This module keeps the implementation intentionally small and additive:
- cached raw OpenAI clients
- cached LangChain chat and embedding wrappers
- persistent SQLite caching for repeated text generations

All heavy imports are lazy so the module can be imported in environments where
the optional LLM dependencies are not installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Iterable, List, Optional, Tuple
import hashlib
import json
import os
import sqlite3
import time


def _normalize_base_url(base_url: Optional[str]) -> Optional[str]:
    """Normalize provider URLs so equivalent values share caches."""
    if base_url is None:
        return None
    normalized = base_url.strip()
    if not normalized:
        return None
    return normalized.rstrip("/")


def _as_bool(value: str, default: bool) -> bool:
    """Parse a forgiving boolean environment variable value."""
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass(frozen=True)
class LangChainOpenAIConfig:
    """
    Configuration for the reusable LangChain/OpenAI helper.

    The config is frozen so it can safely be used as a cache key for the
    memoized service factory.
    """

    api_key: str = field(default="", repr=False)
    base_url: Optional[str] = "https://api.openai.com/v1"
    default_text_model: str = "gpt-4o-mini"
    default_embedding_model: str = "text-embedding-3-small"
    response_cache_path: Optional[str] = None
    response_cache_enabled: bool = True
    debug: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _normalize_base_url(self.base_url))
        if self.response_cache_path:
            path = str(Path(self.response_cache_path).expanduser())
        else:
            path = None
        object.__setattr__(self, "response_cache_path", path)

    @classmethod
    def from_env(
        cls,
        response_cache_path: Optional[str] = None,
        response_cache_enabled: Optional[bool] = None,
    ) -> "LangChainOpenAIConfig":
        """Build config from common OpenAI-style environment variables."""
        api_key = os.getenv("OPENAI_API_KEY", os.getenv("LLM_API_KEY", ""))
        base_url = os.getenv("OPENAI_BASE_URL", os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"))
        text_model = os.getenv("WTB_LLM_TEXT_MODEL", os.getenv("DEFAULT_LLM", "gpt-4o-mini"))
        embedding_model = os.getenv(
            "WTB_LLM_EMBEDDING_MODEL",
            os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
        )

        cache_enabled = response_cache_enabled
        if cache_enabled is None:
            cache_enabled = _as_bool(
                os.getenv("WTB_LLM_RESPONSE_CACHE_ENABLED", "true"),
                default=True,
            )

        cache_path = response_cache_path or os.getenv("WTB_LLM_CACHE_PATH")

        return cls(
            api_key=api_key,
            base_url=base_url,
            default_text_model=text_model,
            default_embedding_model=embedding_model,
            response_cache_path=cache_path or None,
            response_cache_enabled=cache_enabled,
            debug=_as_bool(os.getenv("WTB_LLM_DEBUG", "false"), default=False),
        )

    @property
    def response_cache_active(self) -> bool:
        """True when persistent response caching is fully configured."""
        return self.response_cache_enabled and bool(self.response_cache_path)

    def cache_identity(self) -> Tuple[Any, ...]:
        """Stable identity used by the memoized service factory."""
        return (
            self.api_key,
            self.base_url,
            self.default_text_model,
            self.default_embedding_model,
            self.response_cache_path,
            self.response_cache_enabled,
            self.debug,
        )


@dataclass(frozen=True)
class TextGenerationResult:
    """Detailed result for text generation calls."""

    text: str
    model: str
    cache_key: str
    cache_hit: bool
    duration_ms: float


class _SQLiteTextResponseCache:
    """Small SQLite-backed cache for repeated text generations."""

    def __init__(self, path: str):
        self._path = Path(path)
        self._lock = RLock()
        self._ensure_schema()

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def _ensure_schema(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS text_generation_cache (
                    cache_key TEXT PRIMARY KEY,
                    request_json TEXT NOT NULL,
                    response_text TEXT NOT NULL,
                    model TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def get(self, cache_key: str) -> Optional[str]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT response_text FROM text_generation_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        return str(row[0])

    def set(self, cache_key: str, request_json: str, response_text: str, model: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO text_generation_cache (
                    cache_key,
                    request_json,
                    response_text,
                    model,
                    created_at
                ) VALUES (?, ?, ?, ?, datetime('now'))
                """,
                (cache_key, request_json, response_text, model),
            )
            conn.commit()

    def count(self) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM text_generation_cache").fetchone()
        return int(row[0]) if row is not None else 0

    def clear(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM text_generation_cache")
            conn.commit()


class LangChainOpenAIService:
    """
    Reusable OpenAI/LangChain helper with wrapper reuse and text caching.
    """

    def __init__(self, config: LangChainOpenAIConfig):
        self.config = config
        self._openai_client: Any = None
        self._chat_models: Dict[Tuple[str, float, Optional[int]], Any] = {}
        self._embedding_models: Dict[str, Any] = {}
        self._client_lock = RLock()
        self._cache = (
            _SQLiteTextResponseCache(config.response_cache_path)
            if config.response_cache_active
            else None
        )
        self._stats = {
            "hits": 0,
            "misses": 0,
            "writes": 0,
        }

    def _load_openai_client_class(self) -> Any:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ImportError(
                "openai package not installed. Install with: pip install openai"
            ) from exc
        return OpenAI

    def _load_chat_model_class(self) -> Any:
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:
            raise ImportError(
                "langchain-openai package not installed. Install with: pip install langchain-openai"
            ) from exc
        return ChatOpenAI

    def _load_embedding_model_class(self) -> Any:
        try:
            from langchain_openai import OpenAIEmbeddings
        except ImportError as exc:
            raise ImportError(
                "langchain-openai package not installed. Install with: pip install langchain-openai"
            ) from exc
        return OpenAIEmbeddings

    def get_openai_client(self) -> Any:
        """Return a cached raw OpenAI client."""
        with self._client_lock:
            if self._openai_client is None:
                client_class = self._load_openai_client_class()
                kwargs: Dict[str, Any] = {"api_key": self.config.api_key}
                if self.config.base_url:
                    kwargs["base_url"] = self.config.base_url
                self._openai_client = client_class(**kwargs)
            return self._openai_client

    def get_chat_model(
        self,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ) -> Any:
        """Return a cached ChatOpenAI wrapper for a specific parameter set."""
        resolved_model = model or self.config.default_text_model
        cache_key = (resolved_model, float(temperature), max_tokens)
        with self._client_lock:
            if cache_key not in self._chat_models:
                chat_class = self._load_chat_model_class()
                kwargs: Dict[str, Any] = {
                    "model": resolved_model,
                    "temperature": float(temperature),
                    "api_key": self.config.api_key,
                }
                if self.config.base_url:
                    kwargs["base_url"] = self.config.base_url
                if max_tokens is not None:
                    kwargs["max_tokens"] = int(max_tokens)
                self._chat_models[cache_key] = chat_class(**kwargs)
            return self._chat_models[cache_key]

    def get_embedding_model(self, model: Optional[str] = None) -> Any:
        """Return a cached OpenAIEmbeddings wrapper for a model."""
        resolved_model = model or self.config.default_embedding_model
        with self._client_lock:
            if resolved_model not in self._embedding_models:
                embedding_class = self._load_embedding_model_class()
                kwargs: Dict[str, Any] = {
                    "model": resolved_model,
                    "api_key": self.config.api_key,
                }
                if self.config.base_url:
                    kwargs["base_url"] = self.config.base_url
                self._embedding_models[resolved_model] = embedding_class(**kwargs)
            return self._embedding_models[resolved_model]

    def generate_text(
        self,
        prompt: str,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Generate text and return only the text payload."""
        return self.generate_text_result(
            prompt=prompt,
            model=model,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        ).text

    def generate_text_result(
        self,
        prompt: str,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ) -> TextGenerationResult:
        """
        Generate text with detailed cache metadata.

        The response cache is intentionally limited to text generation calls.
        """
        resolved_model = model or self.config.default_text_model
        request_payload = {
            "base_url": self.config.base_url,
            "model": resolved_model,
            "system_prompt": system_prompt or "",
            "prompt": prompt,
            "temperature": float(temperature),
            "max_tokens": max_tokens,
        }
        request_json = json.dumps(request_payload, sort_keys=True, separators=(",", ":"))
        cache_key = hashlib.sha256(request_json.encode("utf-8")).hexdigest()

        if self._cache is not None:
            cached_text = self._cache.get(cache_key)
            if cached_text is not None:
                self._stats["hits"] += 1
                return TextGenerationResult(
                    text=cached_text,
                    model=resolved_model,
                    cache_key=cache_key,
                    cache_hit=True,
                    duration_ms=0.0,
                )

        self._stats["misses"] += 1
        chat_model = self.get_chat_model(
            model=resolved_model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        messages: List[Any] = []
        if system_prompt:
            messages.append(("system", system_prompt))
        messages.append(("human", prompt))

        started_at = time.perf_counter()
        response = chat_model.invoke(messages)
        duration_ms = (time.perf_counter() - started_at) * 1000.0
        text = self._extract_text(response)

        if self._cache is not None:
            self._cache.set(cache_key, request_json, text, resolved_model)
            self._stats["writes"] += 1

        return TextGenerationResult(
            text=text,
            model=resolved_model,
            cache_key=cache_key,
            cache_hit=False,
            duration_ms=duration_ms,
        )

    def generate_embeddings(
        self,
        texts: Iterable[str],
        model: Optional[str] = None,
    ) -> List[List[float]]:
        """Generate embeddings using a cached OpenAIEmbeddings wrapper."""
        text_list = list(texts)
        embedding_model = self.get_embedding_model(model=model)
        return list(embedding_model.embed_documents(text_list))

    def get_cache_stats(self) -> Dict[str, Any]:
        """Return persistent cache information plus in-process counters."""
        entries = self._cache.count() if self._cache is not None else 0
        return {
            "enabled": self._cache is not None,
            "path": str(self._cache.path) if self._cache is not None else None,
            "entries": entries,
            "hits": self._stats["hits"],
            "misses": self._stats["misses"],
            "writes": self._stats["writes"],
        }

    def clear_response_cache(self) -> None:
        """Clear the persistent text cache and reset in-process counters."""
        if self._cache is not None:
            self._cache.clear()
        self._stats = {
            "hits": 0,
            "misses": 0,
            "writes": 0,
        }

    @staticmethod
    def _extract_text(response: Any) -> str:
        """Extract plain text from a LangChain response object."""
        if isinstance(response, str):
            return response

        content = getattr(response, "content", response)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        parts.append(str(text))
            return "".join(parts)
        return str(content)


_SERVICE_CACHE: Dict[Tuple[Any, ...], LangChainOpenAIService] = {}
_SERVICE_CACHE_LOCK = RLock()


def get_service(config: LangChainOpenAIConfig) -> LangChainOpenAIService:
    """Memoized factory for reusable LangChain/OpenAI services."""
    cache_key = config.cache_identity()
    with _SERVICE_CACHE_LOCK:
        if cache_key not in _SERVICE_CACHE:
            _SERVICE_CACHE[cache_key] = LangChainOpenAIService(config)
        return _SERVICE_CACHE[cache_key]


def reset_service_cache() -> None:
    """Clear the memoized service cache. Useful for tests."""
    with _SERVICE_CACHE_LOCK:
        _SERVICE_CACHE.clear()
