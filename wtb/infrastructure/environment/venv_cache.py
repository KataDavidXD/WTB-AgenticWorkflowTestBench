"""
VenvCacheManager - LRU Cache for Virtual Environments.

Provides efficient venv reuse across variant executions by caching
environments based on specification hash. Implements LRU eviction
to manage disk space.

Design Principles:
- SOLID: Single responsibility (venv caching)
- ACID: Atomic cache operations
- DIP: Depends on hash function abstraction

Architecture:
- VenvCacheManager: Central cache management
- VenvCacheEntry: Cache entry metadata
- LRU eviction based on last access time

Thread Safety:
- Thread-safe via RLock for all operations
- File-level locking for concurrent access

Related Documents:
- docs/issues/WORKSPACE_ISOLATION_DESIGN.md (Section 12.3)
"""

import hashlib
import json
import logging
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Data Classes
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class VenvSpec:
    """
    Specification for a virtual environment.
    """
    python_version: str
    packages: list[str]
    requirements_content: str | None = None
    lock_file_content: str | None = None
    extra_env_vars: dict[str, str] = field(default_factory=dict)
    
    def compute_hash(self) -> str:
        """
        Compute SHA-256 hash of the venv specification.
        
        Returns:
            16-character hex hash
        """
        spec_data = {
            "python_version": self.python_version,
            "packages": sorted(self.packages),
            "requirements": self.requirements_content,
            "lock": self.lock_file_content,
        }
        spec_json = json.dumps(spec_data, sort_keys=True)
        return hashlib.sha256(spec_json.encode()).hexdigest()[:16]
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "python_version": self.python_version,
            "packages": self.packages,
            "requirements_content": self.requirements_content,
            "lock_file_content": self.lock_file_content,
            "extra_env_vars": self.extra_env_vars,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VenvSpec":
        """Create from dictionary."""
        return cls(
            python_version=data.get("python_version", "3.12"),
            packages=data.get("packages", []),
            requirements_content=data.get("requirements_content"),
            lock_file_content=data.get("lock_file_content"),
            extra_env_vars=data.get("extra_env_vars", {}),
        )


@dataclass
class VenvCacheEntry:
    """
    Metadata for a cached venv entry.
    """
    spec_hash: str
    venv_path: Path
    spec: VenvSpec
    created_at: datetime
    last_accessed_at: datetime
    access_count: int = 0
    size_bytes: int = 0
    
    def update_access(self) -> None:
        """Update access time and count."""
        self.last_accessed_at = datetime.now()
        self.access_count += 1
    
    @property
    def size_mb(self) -> float:
        """Size in megabytes."""
        return self.size_bytes / (1024 * 1024)
    
    @property
    def age_hours(self) -> float:
        """Age in hours since creation."""
        return (datetime.now() - self.created_at).total_seconds() / 3600
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "spec_hash": self.spec_hash,
            "venv_path": str(self.venv_path),
            "spec": self.spec.to_dict(),
            "created_at": self.created_at.isoformat(),
            "last_accessed_at": self.last_accessed_at.isoformat(),
            "access_count": self.access_count,
            "size_bytes": self.size_bytes,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VenvCacheEntry":
        """Create from dictionary."""
        return cls(
            spec_hash=data["spec_hash"],
            venv_path=Path(data["venv_path"]),
            spec=VenvSpec.from_dict(data["spec"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            last_accessed_at=datetime.fromisoformat(data["last_accessed_at"]),
            access_count=data.get("access_count", 0),
            size_bytes=data.get("size_bytes", 0),
        )


@dataclass
class VenvCacheConfig:
    """
    Configuration for VenvCacheManager.
    """
    cache_dir: Path | None = None
    max_size_gb: float = 10.0
    max_age_days: int = 30
    max_entries: int = 100
    cleanup_threshold: float = 0.9  # Cleanup when 90% full
    
    def get_cache_dir(self) -> Path:
        """Get cache directory, defaulting to system temp."""
        if self.cache_dir:
            return self.cache_dir
        
        import tempfile
        return Path(tempfile.gettempdir()) / "wtb_venv_cache"
    
    @property
    def max_size_bytes(self) -> int:
        """Maximum size in bytes."""
        return int(self.max_size_gb * 1024 * 1024 * 1024)


@dataclass
class CacheStats:
    """
    Statistics for the venv cache.
    """
    total_entries: int = 0
    total_size_bytes: int = 0
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    
    @property
    def total_size_mb(self) -> float:
        """Total size in megabytes."""
        return self.total_size_bytes / (1024 * 1024)
    
    @property
    def total_size_gb(self) -> float:
        """Total size in gigabytes."""
        return self.total_size_bytes / (1024 * 1024 * 1024)
    
    @property
    def hit_rate(self) -> float:
        """Cache hit rate."""
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# VenvCacheManager
# ═══════════════════════════════════════════════════════════════════════════════


class VenvCacheManager:
    """
    LRU cache for virtual environments.
    
    Caches venv directories by specification hash for efficient reuse
    across variant executions. Implements LRU eviction to manage disk space.
    
    Cache Structure:
        {cache_dir}/
        ├── .cache_index.json        # Index of cached entries
        ├── {spec_hash_1}/           # Cached venv directory
        │   ├── .venv_meta.json      # Entry metadata
        │   └── .venv/               # Actual venv
        └── {spec_hash_2}/
            └── ...
    
    Thread Safety:
    - All public methods are thread-safe via RLock
    - File-level locking for index updates
    
    Usage:
        cache = VenvCacheManager(VenvCacheConfig(max_size_gb=10.0))
        
        # Check cache
        entry = cache.get(spec_hash)
        if entry:
            # Use cached venv
            copy_venv(entry.venv_path, target_workspace)
        else:
            # Create venv and add to cache
            create_venv(target_workspace, spec)
            cache.put(spec)
    """
    
    def __init__(self, config: VenvCacheConfig | None = None):
        """
        Initialize VenvCacheManager.
        
        Args:
            config: Cache configuration
        """
        self._config = config or VenvCacheConfig()
        self._lock = threading.RLock()
        
        # In-memory index: spec_hash -> VenvCacheEntry
        self._entries: dict[str, VenvCacheEntry] = {}
        
        # Statistics
        self._stats = CacheStats()
        
        # Initialize cache directory and load index
        self._cache_dir = self._config.get_cache_dir()
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        
        self._index_file = self._cache_dir / ".cache_index.json"
        self._load_index()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Index Management
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _load_index(self) -> None:
        """Load cache index from disk."""
        if not self._index_file.exists():
            return
        
        try:
            data = json.loads(self._index_file.read_text(encoding="utf-8"))
            
            for entry_data in data.get("entries", []):
                try:
                    entry = VenvCacheEntry.from_dict(entry_data)
                    # Verify venv still exists
                    if entry.venv_path.exists():
                        self._entries[entry.spec_hash] = entry
                except Exception as e:
                    logger.warning(f"Failed to load cache entry: {e}")
            
            # Load stats
            stats_data = data.get("stats", {})
            self._stats.hits = stats_data.get("hits", 0)
            self._stats.misses = stats_data.get("misses", 0)
            self._stats.evictions = stats_data.get("evictions", 0)
            
            logger.info(f"Loaded venv cache index: {len(self._entries)} entries")
            
        except Exception as e:
            logger.warning(f"Failed to load cache index: {e}")
    
    def _save_index(self) -> None:
        """Save cache index to disk."""
        try:
            data = {
                "entries": [e.to_dict() for e in self._entries.values()],
                "stats": {
                    "hits": self._stats.hits,
                    "misses": self._stats.misses,
                    "evictions": self._stats.evictions,
                },
                "updated_at": datetime.now().isoformat(),
            }
            
            self._index_file.write_text(
                json.dumps(data, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"Failed to save cache index: {e}")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Cache Operations
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get(self, spec_hash: str) -> VenvCacheEntry | None:
        """
        Get cached venv by specification hash.
        
        Args:
            spec_hash: Specification hash
            
        Returns:
            VenvCacheEntry if found, None otherwise
        """
        with self._lock:
            entry = self._entries.get(spec_hash)
            
            if entry:
                # Verify venv still exists
                if entry.venv_path.exists():
                    entry.update_access()
                    self._stats.hits += 1
                    self._save_index()
                    return entry
                else:
                    # Entry exists but venv deleted
                    self._entries.pop(spec_hash, None)
                    self._save_index()
            
            self._stats.misses += 1
            return None
    
    def get_by_spec(self, spec: VenvSpec) -> VenvCacheEntry | None:
        """
        Get cached venv by specification.
        
        Args:
            spec: VenvSpec to look up
            
        Returns:
            VenvCacheEntry if found, None otherwise
        """
        return self.get(spec.compute_hash())
    
    def put(
        self,
        spec: VenvSpec,
        venv_source_path: Path,
        copy_to_cache: bool = True,
    ) -> VenvCacheEntry:
        """
        Add venv to cache.
        
        Args:
            spec: Venv specification
            venv_source_path: Path to venv to cache
            copy_to_cache: Whether to copy (True) or move (False)
            
        Returns:
            VenvCacheEntry for the cached venv
        """
        spec_hash = spec.compute_hash()
        
        with self._lock:
            # Check if we need to evict before adding
            self._maybe_evict()
            
            # Create cache entry directory
            entry_dir = self._cache_dir / spec_hash
            entry_dir.mkdir(parents=True, exist_ok=True)
            
            # Copy or move venv to cache
            cache_venv_path = entry_dir / ".venv"
            
            if cache_venv_path.exists():
                shutil.rmtree(cache_venv_path)
            
            if copy_to_cache:
                shutil.copytree(venv_source_path, cache_venv_path)
            else:
                shutil.move(str(venv_source_path), str(cache_venv_path))
            
            # Calculate size
            size_bytes = self._calculate_directory_size(entry_dir)
            
            # Create entry
            now = datetime.now()
            entry = VenvCacheEntry(
                spec_hash=spec_hash,
                venv_path=entry_dir,
                spec=spec,
                created_at=now,
                last_accessed_at=now,
                access_count=1,
                size_bytes=size_bytes,
            )
            
            # Save entry metadata
            meta_file = entry_dir / ".venv_meta.json"
            meta_file.write_text(
                json.dumps(entry.to_dict(), indent=2),
                encoding="utf-8"
            )
            
            # Add to index
            self._entries[spec_hash] = entry
            self._update_stats()
            self._save_index()
            
            logger.info(
                f"Added venv to cache: {spec_hash}, "
                f"size={entry.size_mb:.1f}MB"
            )
            
            return entry
    
    def copy_to_workspace(
        self,
        spec_hash: str,
        target_path: Path,
    ) -> bool:
        """
        Copy cached venv to workspace.
        
        Args:
            spec_hash: Specification hash
            target_path: Target path for venv
            
        Returns:
            True if copied successfully, False if not found
        """
        with self._lock:
            entry = self._entries.get(spec_hash)
            if not entry or not entry.venv_path.exists():
                return False
            
            source_venv = entry.venv_path / ".venv"
            if not source_venv.exists():
                return False
            
            # Copy venv to target
            if target_path.exists():
                shutil.rmtree(target_path)
            
            shutil.copytree(source_venv, target_path)
            
            # Update access
            entry.update_access()
            self._save_index()
            
            logger.debug(f"Copied cached venv {spec_hash} to {target_path}")
            return True
    
    def invalidate(self, spec_hash: str) -> bool:
        """
        Invalidate and remove a cached entry.
        
        Args:
            spec_hash: Specification hash to invalidate
            
        Returns:
            True if invalidated, False if not found
        """
        with self._lock:
            entry = self._entries.pop(spec_hash, None)
            if not entry:
                return False
            
            # Remove from disk
            if entry.venv_path.exists():
                try:
                    shutil.rmtree(entry.venv_path)
                except Exception as e:
                    logger.warning(f"Failed to remove cached venv: {e}")
            
            self._update_stats()
            self._save_index()
            
            logger.info(f"Invalidated cached venv: {spec_hash}")
            return True
    
    # ═══════════════════════════════════════════════════════════════════════════
    # LRU Eviction
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _maybe_evict(self) -> int:
        """
        Evict entries if cache is over threshold.
        
        Returns:
            Number of entries evicted
        """
        current_size = self._calculate_total_size()
        threshold = int(self._config.max_size_bytes * self._config.cleanup_threshold)
        
        if current_size < threshold and len(self._entries) < self._config.max_entries:
            return 0
        
        return self._evict_lru()
    
    def _evict_lru(self, target_free_bytes: int | None = None) -> int:
        """
        Evict least recently used entries.
        
        Args:
            target_free_bytes: Target bytes to free (or use config default)
            
        Returns:
            Number of entries evicted
        """
        if target_free_bytes is None:
            # Free up to 20% of max size
            target_free_bytes = int(self._config.max_size_bytes * 0.2)
        
        # Sort entries by last_accessed_at (oldest first)
        sorted_entries = sorted(
            self._entries.values(),
            key=lambda e: e.last_accessed_at,
        )
        
        evicted = 0
        freed_bytes = 0
        
        for entry in sorted_entries:
            if freed_bytes >= target_free_bytes:
                break
            
            # Don't evict if accessed in last hour
            if (datetime.now() - entry.last_accessed_at).total_seconds() < 3600:
                continue
            
            # Evict entry
            if self.invalidate(entry.spec_hash):
                evicted += 1
                freed_bytes += entry.size_bytes
                self._stats.evictions += 1
        
        if evicted > 0:
            logger.info(
                f"Evicted {evicted} entries, freed {freed_bytes / (1024*1024):.1f}MB"
            )
        
        return evicted
    
    def evict_by_age(self) -> int:
        """
        Evict entries older than max_age_days.
        
        Returns:
            Number of entries evicted
        """
        max_age = timedelta(days=self._config.max_age_days)
        cutoff = datetime.now() - max_age
        
        evicted = 0
        
        with self._lock:
            for spec_hash in list(self._entries.keys()):
                entry = self._entries.get(spec_hash)
                if entry and entry.created_at < cutoff:
                    if self.invalidate(spec_hash):
                        evicted += 1
        
        return evicted
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Utilities
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _calculate_directory_size(self, path: Path) -> int:
        """Calculate total size of directory."""
        total = 0
        try:
            for item in path.rglob("*"):
                if item.is_file():
                    total += item.stat().st_size
        except OSError:
            pass
        return total
    
    def _calculate_total_size(self) -> int:
        """Calculate total cache size."""
        return sum(e.size_bytes for e in self._entries.values())
    
    def _update_stats(self) -> None:
        """Update cache statistics."""
        self._stats.total_entries = len(self._entries)
        self._stats.total_size_bytes = self._calculate_total_size()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Query
    # ═══════════════════════════════════════════════════════════════════════════
    
    def list_entries(self) -> list[VenvCacheEntry]:
        """List all cache entries."""
        with self._lock:
            return list(self._entries.values())
    
    def get_stats(self) -> CacheStats:
        """Get cache statistics."""
        with self._lock:
            self._update_stats()
            return CacheStats(
                total_entries=self._stats.total_entries,
                total_size_bytes=self._stats.total_size_bytes,
                hits=self._stats.hits,
                misses=self._stats.misses,
                evictions=self._stats.evictions,
            )
    
    def clear(self) -> int:
        """
        Clear all cache entries.
        
        Returns:
            Number of entries cleared
        """
        with self._lock:
            count = len(self._entries)
            
            for spec_hash in list(self._entries.keys()):
                self.invalidate(spec_hash)
            
            self._entries.clear()
            self._save_index()
            
            return count
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Context Manager
    # ═══════════════════════════════════════════════════════════════════════════
    
    def __enter__(self) -> "VenvCacheManager":
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Context manager exit - save index."""
        self._save_index()
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Factory Functions
# ═══════════════════════════════════════════════════════════════════════════════


def create_venv_cache(
    cache_dir: Path | None = None,
    max_size_gb: float = 10.0,
    max_age_days: int = 30,
) -> VenvCacheManager:
    """
    Factory function to create VenvCacheManager.
    
    Args:
        cache_dir: Cache directory path
        max_size_gb: Maximum cache size in GB
        max_age_days: Maximum entry age in days
        
    Returns:
        Configured VenvCacheManager
    """
    config = VenvCacheConfig(
        cache_dir=cache_dir,
        max_size_gb=max_size_gb,
        max_age_days=max_age_days,
    )
    return VenvCacheManager(config)
