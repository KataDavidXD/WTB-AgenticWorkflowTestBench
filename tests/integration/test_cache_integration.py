import os
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("openai")
pytest.importorskip("langchain_openai")

try:
    import ray

    RAY_AVAILABLE = True
except ImportError:
    RAY_AVAILABLE = False

from wtb.application.services.external_storage import resolve_actor_local_storage_paths
from wtb.infrastructure.llm.openai_langchain import (
    LangChainOpenAIConfig,
    get_service,
)

LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
LLM_BASE_URL = os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
LLM_TEXT_MODEL = os.getenv("WTB_LLM_TEXT_MODEL") or os.getenv("DEFAULT_LLM") or "gpt-4o-mini"

pytestmark = pytest.mark.skipif(
    not RAY_AVAILABLE or not LLM_API_KEY,
    reason="Ray or live LLM credentials not available",
)


@ray.remote
class CacheProbeActor:
    def __init__(
        self,
        actor_id: str,
        cache_root: str,
        api_key: str,
        base_url: str,
        model: str,
    ):
        self.paths = resolve_actor_local_storage_paths(actor_id, storage_root=cache_root)
        config = LangChainOpenAIConfig(
            api_key=api_key,
            base_url=base_url,
            default_text_model=model,
            default_embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
            response_cache_path=str(self.paths.llm_cache_path),
            response_cache_enabled=True,
            debug=False,
        )
        self.service = get_service(config)

    def generate_twice(self, prompt: str):
        first = self.service.generate_text_result(
            prompt=prompt,
            temperature=0.0,
            max_tokens=32,
        )
        second = self.service.generate_text_result(
            prompt=prompt,
            temperature=0.0,
            max_tokens=32,
        )
        stats = self.service.get_cache_stats()
        return {
            "first_hit": first.cache_hit,
            "second_hit": second.cache_hit,
            "cache_path": str(self.paths.llm_cache_path),
            "entries": stats["entries"],
        }


def test_real_ray_actors_get_isolated_cache_dbs(tmp_path):
    if not ray.is_initialized():
        ray.init(num_cpus=2, ignore_reinit_error=True)

    cache_root = tmp_path / "ray_actors"
    prompt = "Reply with the single word cache-test."

    actor_a = CacheProbeActor.remote(
        "actor_a",
        str(cache_root),
        LLM_API_KEY,
        LLM_BASE_URL,
        LLM_TEXT_MODEL,
    )
    actor_b = CacheProbeActor.remote(
        "actor_b",
        str(cache_root),
        LLM_API_KEY,
        LLM_BASE_URL,
        LLM_TEXT_MODEL,
    )

    result_a = ray.get(actor_a.generate_twice.remote(prompt))
    result_b = ray.get(actor_b.generate_twice.remote(prompt))

    assert result_a["first_hit"] is False
    assert result_a["second_hit"] is True
    assert result_b["first_hit"] is False
    assert result_b["second_hit"] is True
    assert result_a["entries"] == 1
    assert result_b["entries"] == 1
    assert result_a["cache_path"] != result_b["cache_path"]

    for cache_path in (result_a["cache_path"], result_b["cache_path"]):
        db_path = Path(cache_path)
        assert db_path.exists()
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM text_generation_cache"
            ).fetchone()
            assert row is not None
            assert row[0] == 1
