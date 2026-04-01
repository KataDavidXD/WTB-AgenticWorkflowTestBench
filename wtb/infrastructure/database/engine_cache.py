"""
Shared SQLAlchemy engines keyed by (db_url, echo).

Avoids creating a new Engine per UnitOfWork instance for the same database URL.
In-memory SQLite is excluded from caching because each caller expects its own DB.
"""

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


@lru_cache(maxsize=32)
def _get_cached_engine(db_url: str, echo: bool) -> Engine:
    """Internal cached engine factory for file-backed databases."""
    return create_engine(db_url, echo=echo)


def get_engine(db_url: str, echo: bool = False) -> Engine:
    """Return an Engine for the given URL. In-memory SQLite bypasses cache."""
    if ":memory:" in db_url:
        return create_engine(db_url, echo=echo)
    return _get_cached_engine(db_url, echo)
