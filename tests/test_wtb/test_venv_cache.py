"""
Tests for VenvCacheManager.

Tests cover:
- Cache entry creation and retrieval
- LRU eviction
- Age-based eviction
- Spec hash computation
- Cache statistics
"""

import pytest
import tempfile
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from wtb.infrastructure.environment.venv_cache import (
    VenvCacheManager,
    VenvCacheConfig,
    VenvCacheEntry,
    VenvSpec,
    CacheStats,
    create_venv_cache,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def temp_cache_dir():
    """Create temporary cache directory."""
    temp_dir = Path(tempfile.mkdtemp(prefix="wtb_test_venv_cache_"))
    yield temp_dir
    # Cleanup
    if temp_dir.exists():
        shutil.rmtree(temp_dir)


@pytest.fixture
def cache(temp_cache_dir):
    """Create VenvCacheManager with temp directory."""
    config = VenvCacheConfig(
        cache_dir=temp_cache_dir,
        max_size_gb=1.0,
        max_age_days=7,
        max_entries=10,
    )
    return VenvCacheManager(config)


@pytest.fixture
def sample_venv(temp_cache_dir):
    """Create a sample venv structure for testing."""
    venv_dir = temp_cache_dir / "sample_venv" / ".venv"
    venv_dir.mkdir(parents=True)
    
    # Create some dummy files
    (venv_dir / "pyvenv.cfg").write_text("python = /usr/bin/python3.12\n")
    (venv_dir / "lib").mkdir()
    (venv_dir / "lib" / "dummy.py").write_text("# dummy\n")
    
    return venv_dir.parent


# ═══════════════════════════════════════════════════════════════════════════════
# VenvSpec Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestVenvSpec:
    """Tests for VenvSpec."""
    
    def test_compute_hash_deterministic(self):
        """Test hash computation is deterministic."""
        spec = VenvSpec(
            python_version="3.12",
            packages=["numpy", "pandas"],
        )
        
        hash1 = spec.compute_hash()
        hash2 = spec.compute_hash()
        
        assert hash1 == hash2
        assert len(hash1) == 16  # 16 hex characters
    
    def test_compute_hash_different_packages(self):
        """Test different packages produce different hashes."""
        spec1 = VenvSpec(python_version="3.12", packages=["numpy"])
        spec2 = VenvSpec(python_version="3.12", packages=["pandas"])
        
        assert spec1.compute_hash() != spec2.compute_hash()
    
    def test_compute_hash_order_independent(self):
        """Test package order doesn't affect hash."""
        spec1 = VenvSpec(python_version="3.12", packages=["numpy", "pandas"])
        spec2 = VenvSpec(python_version="3.12", packages=["pandas", "numpy"])
        
        assert spec1.compute_hash() == spec2.compute_hash()
    
    def test_to_dict_from_dict_roundtrip(self):
        """Test serialization roundtrip."""
        spec = VenvSpec(
            python_version="3.12",
            packages=["numpy", "pandas"],
            requirements_content="numpy==1.24.0\n",
        )
        
        data = spec.to_dict()
        restored = VenvSpec.from_dict(data)
        
        assert restored.python_version == spec.python_version
        assert restored.packages == spec.packages
        assert restored.compute_hash() == spec.compute_hash()


# ═══════════════════════════════════════════════════════════════════════════════
# VenvCacheEntry Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestVenvCacheEntry:
    """Tests for VenvCacheEntry."""
    
    def test_update_access(self):
        """Test access tracking."""
        spec = VenvSpec(python_version="3.12", packages=[])
        entry = VenvCacheEntry(
            spec_hash="abc123",
            venv_path=Path("/tmp/test"),
            spec=spec,
            created_at=datetime.now(),
            last_accessed_at=datetime.now() - timedelta(hours=1),
            access_count=0,
        )
        
        old_access_time = entry.last_accessed_at
        entry.update_access()
        
        assert entry.access_count == 1
        assert entry.last_accessed_at > old_access_time
    
    def test_size_conversion(self):
        """Test size unit conversions."""
        spec = VenvSpec(python_version="3.12", packages=[])
        entry = VenvCacheEntry(
            spec_hash="abc123",
            venv_path=Path("/tmp/test"),
            spec=spec,
            created_at=datetime.now(),
            last_accessed_at=datetime.now(),
            size_bytes=1024 * 1024 * 100,  # 100MB
        )
        
        assert entry.size_mb == 100.0
    
    def test_age_calculation(self):
        """Test age calculation."""
        spec = VenvSpec(python_version="3.12", packages=[])
        entry = VenvCacheEntry(
            spec_hash="abc123",
            venv_path=Path("/tmp/test"),
            spec=spec,
            created_at=datetime.now() - timedelta(hours=48),
            last_accessed_at=datetime.now(),
        )
        
        assert entry.age_hours >= 47  # Allow some tolerance


# ═══════════════════════════════════════════════════════════════════════════════
# VenvCacheManager Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestVenvCacheManager:
    """Tests for VenvCacheManager."""
    
    def test_cache_miss(self, cache):
        """Test cache miss returns None."""
        result = cache.get("nonexistent_hash")
        assert result is None
    
    def test_put_and_get(self, cache, sample_venv):
        """Test adding and retrieving from cache."""
        spec = VenvSpec(python_version="3.12", packages=["test"])
        spec_hash = spec.compute_hash()
        
        entry = cache.put(spec, sample_venv / ".venv")
        
        assert entry.spec_hash == spec_hash
        assert entry.venv_path.exists()
        
        # Retrieve
        retrieved = cache.get(spec_hash)
        assert retrieved is not None
        assert retrieved.spec_hash == spec_hash
    
    def test_get_by_spec(self, cache, sample_venv):
        """Test getting by spec object."""
        spec = VenvSpec(python_version="3.12", packages=["numpy"])
        
        cache.put(spec, sample_venv / ".venv")
        
        retrieved = cache.get_by_spec(spec)
        assert retrieved is not None
    
    def test_copy_to_workspace(self, cache, sample_venv, temp_cache_dir):
        """Test copying cached venv to workspace."""
        spec = VenvSpec(python_version="3.12", packages=["test"])
        spec_hash = spec.compute_hash()
        
        cache.put(spec, sample_venv / ".venv")
        
        target_path = temp_cache_dir / "workspace" / ".venv"
        result = cache.copy_to_workspace(spec_hash, target_path)
        
        assert result is True
        assert target_path.exists()
        assert (target_path / "pyvenv.cfg").exists()
    
    def test_invalidate(self, cache, sample_venv):
        """Test invalidating cache entry."""
        spec = VenvSpec(python_version="3.12", packages=["test"])
        spec_hash = spec.compute_hash()
        
        cache.put(spec, sample_venv / ".venv")
        
        result = cache.invalidate(spec_hash)
        
        assert result is True
        assert cache.get(spec_hash) is None
    
    def test_cache_stats(self, cache, sample_venv):
        """Test cache statistics."""
        spec = VenvSpec(python_version="3.12", packages=["test"])
        
        # Initial stats
        stats = cache.get_stats()
        assert stats.total_entries == 0
        
        # Add entry
        cache.put(spec, sample_venv / ".venv")
        
        # Get (hit)
        cache.get(spec.compute_hash())
        
        # Get (miss)
        cache.get("nonexistent")
        
        stats = cache.get_stats()
        assert stats.total_entries == 1
        assert stats.hits >= 1
        assert stats.misses >= 1
    
    def test_clear(self, cache, sample_venv):
        """Test clearing all cache entries."""
        spec1 = VenvSpec(python_version="3.12", packages=["pkg1"])
        spec2 = VenvSpec(python_version="3.12", packages=["pkg2"])
        
        cache.put(spec1, sample_venv / ".venv", copy_to_cache=True)
        
        # Create another sample venv
        venv2 = sample_venv.parent / "sample_venv2" / ".venv"
        venv2.mkdir(parents=True, exist_ok=True)
        (venv2 / "pyvenv.cfg").write_text("python = /usr/bin/python3.12\n")
        
        cache.put(spec2, venv2, copy_to_cache=True)
        
        assert cache.get_stats().total_entries >= 1
        
        count = cache.clear()
        
        assert count >= 1
        assert cache.get_stats().total_entries == 0


# ═══════════════════════════════════════════════════════════════════════════════
# LRU Eviction Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestLRUEviction:
    """Tests for LRU eviction."""
    
    def test_evict_by_age(self, temp_cache_dir):
        """Test age-based eviction."""
        config = VenvCacheConfig(
            cache_dir=temp_cache_dir,
            max_age_days=1,  # 1 day max
        )
        cache = VenvCacheManager(config)
        
        spec = VenvSpec(python_version="3.12", packages=["old"])
        
        # Create a sample venv
        venv_dir = temp_cache_dir / "old_venv" / ".venv"
        venv_dir.mkdir(parents=True)
        (venv_dir / "pyvenv.cfg").write_text("test\n")
        
        entry = cache.put(spec, venv_dir)
        
        # Manually age the entry
        cache._entries[entry.spec_hash].created_at = datetime.now() - timedelta(days=5)
        
        evicted = cache.evict_by_age()
        
        assert evicted >= 1
        assert cache.get(spec.compute_hash()) is None


# ═══════════════════════════════════════════════════════════════════════════════
# CacheStats Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCacheStats:
    """Tests for CacheStats."""
    
    def test_size_conversions(self):
        """Test size unit conversions."""
        stats = CacheStats(
            total_size_bytes=1024 * 1024 * 1024,  # 1GB
        )
        
        assert stats.total_size_mb == 1024.0
        assert stats.total_size_gb == 1.0
    
    def test_hit_rate_calculation(self):
        """Test hit rate calculation."""
        stats = CacheStats(hits=80, misses=20)
        
        assert stats.hit_rate == 0.8
    
    def test_hit_rate_zero_total(self):
        """Test hit rate with zero total."""
        stats = CacheStats(hits=0, misses=0)
        
        assert stats.hit_rate == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Factory Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFactory:
    """Tests for factory functions."""
    
    def test_create_venv_cache(self, temp_cache_dir):
        """Test factory function."""
        cache = create_venv_cache(
            cache_dir=temp_cache_dir,
            max_size_gb=5.0,
            max_age_days=14,
        )
        
        assert isinstance(cache, VenvCacheManager)
        assert cache._config.max_size_gb == 5.0
        assert cache._config.max_age_days == 14


# ═══════════════════════════════════════════════════════════════════════════════
# Context Manager Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestContextManager:
    """Tests for context manager usage."""
    
    def test_context_manager(self, temp_cache_dir):
        """Test using cache as context manager."""
        config = VenvCacheConfig(cache_dir=temp_cache_dir)
        
        with VenvCacheManager(config) as cache:
            spec = VenvSpec(python_version="3.12", packages=["test"])
            
            # Create a sample venv
            venv_dir = temp_cache_dir / "ctx_venv" / ".venv"
            venv_dir.mkdir(parents=True)
            (venv_dir / "pyvenv.cfg").write_text("test\n")
            
            cache.put(spec, venv_dir)
        
        # Index should be saved after exit
        index_file = temp_cache_dir / ".cache_index.json"
        assert index_file.exists()
