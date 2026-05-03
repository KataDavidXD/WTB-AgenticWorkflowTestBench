"""Helpers for actor-local external storage used by Ray execution paths.

The cache and checkpoint databases live outside checkpointed workflow state.
Checkpoint metadata keeps references to those files, while the files themselves
remain on disk under ``data/ray_actors/<actor_id>/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional
import os


def _default_ray_storage_root() -> Path:
    """Return the base directory for Ray actor storage.

    Checks ``WTB_RAY_STORAGE_ROOT`` first so that tests can isolate each run
    in its own temp directory.  Falls back to the repo-local default.
    """
    env = os.getenv("WTB_RAY_STORAGE_ROOT")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[3] / "data" / "ray_actors"


def _expand_path(value: Optional[str]) -> Optional[Path]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return Path(text).expanduser()


@dataclass(frozen=True)
class ActorLocalStoragePaths:
    """Concrete actor-local paths for checkpoint and LLM cache storage."""

    actor_id: str
    storage_root: Path
    checkpoint_db_path: Path
    llm_cache_path: Path
    cache_storage_scope: str = "actor_local"

    @property
    def bundle_path(self) -> Path:
        """Directory containing both actor-local databases."""
        return self.checkpoint_db_path.parent

    def to_env_vars(self) -> dict[str, str]:
        """Return environment variables pointing at this storage bundle."""
        return {
            "WTB_CHECKPOINT_DB_PATH": str(self.checkpoint_db_path),
            "WTB_LLM_CACHE_PATH": str(self.llm_cache_path),
            "WTB_CACHE_STORAGE_SCOPE": self.cache_storage_scope,
            "WTB_CACHE_ACTOR_ID": self.actor_id,
        }


def resolve_actor_local_storage_paths(
    actor_id: str,
    storage_root: Optional[str | Path] = None,
) -> ActorLocalStoragePaths:
    """Return the actor-local storage bundle for a Ray worker."""
    normalized_actor_id = str(actor_id).strip()
    if not normalized_actor_id:
        raise ValueError("actor_id is required for actor-local storage")

    root = Path(storage_root).expanduser() if storage_root else _default_ray_storage_root()
    bundle_path = root / normalized_actor_id
    bundle_path.mkdir(parents=True, exist_ok=True)

    return ActorLocalStoragePaths(
        actor_id=normalized_actor_id,
        storage_root=root,
        checkpoint_db_path=bundle_path / "wtb_checkpoints.db",
        llm_cache_path=bundle_path / "llm_response_cache.db",
    )


def resolve_execution_storage_paths(
    execution_metadata: Optional[Mapping[str, Any]] = None,
    storage_root: Optional[str | Path] = None,
    fallback_actor_id: Optional[str] = None,
) -> ActorLocalStoragePaths:
    """Rehydrate storage paths from execution metadata.

    The metadata usually contains ``checkpoint_db_path`` and ``llm_cache_path``.
    When only one of them is present we derive the sibling path from the same
    bundle directory so rollback/fork can reconnect to both external stores.
    """
    if execution_metadata is None:
        metadata: dict[str, Any] = {}
    elif isinstance(execution_metadata, Mapping):
        metadata = dict(execution_metadata)
    else:
        metadata = {}
    actor_id = str(
        metadata.get("actor_id")
        or fallback_actor_id
        or os.getenv("WTB_CACHE_ACTOR_ID")
        or "standalone"
    ).strip()
    cache_storage_scope = str(metadata.get("cache_storage_scope") or "actor_local")

    checkpoint_db_path = _expand_path(
        metadata.get("checkpoint_db_path") or os.getenv("WTB_CHECKPOINT_DB_PATH")
    )
    llm_cache_path = _expand_path(
        metadata.get("llm_cache_path") or os.getenv("WTB_LLM_CACHE_PATH")
    )

    if checkpoint_db_path or llm_cache_path:
        bundle_path = (checkpoint_db_path or llm_cache_path).parent  # type: ignore[union-attr]
        bundle_path.mkdir(parents=True, exist_ok=True)
        if checkpoint_db_path is None:
            checkpoint_db_path = bundle_path / "wtb_checkpoints.db"
        if llm_cache_path is None:
            llm_cache_path = bundle_path / "llm_response_cache.db"
        return ActorLocalStoragePaths(
            actor_id=actor_id,
            storage_root=bundle_path.parent,
            checkpoint_db_path=checkpoint_db_path,
            llm_cache_path=llm_cache_path,
            cache_storage_scope=cache_storage_scope,
        )

    return resolve_actor_local_storage_paths(actor_id, storage_root=storage_root)
