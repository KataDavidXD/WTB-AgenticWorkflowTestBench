"""
Minimal database configuration for WTB.

Keeps:
- storage mode
- project-root based data path
- SQLite file paths
- PostgreSQL connection info (three separate databases)
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# "sqlite", "postgresql"
StorageMode = 'sqlite'

@dataclass
class DatabaseConfig:
    mode: str = StorageMode

    # Base data directory (only used for SQLite)
    data_dir: Path = Path("data")

    # PostgreSQL common connection parameters
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_user: str = "postgres"
    pg_password: str = "secret"

    # --- Three independent PostgreSQL database names ---
    pg_database_wtb: str = "wtb"
    pg_database_agentgit: str = "agentgit"
    pg_database_filetracker: str = "filetracker"

    # ---------- SQLite path properties ----------
    @property
    def wtb_db_path(self) -> Path:
        return self.data_dir / "wtb.db"

    @property
    def agentgit_db_path(self) -> Path:
        return self.data_dir / "agentgit.db"

    @property
    def filetracker_db_path(self) -> Path:
        return self.data_dir / "filetracker.db"

    # ---------- SQLite URL generation ----------
    def _sqlite_url(self, path: Path) -> str:
        return f"sqlite:///{path}"

    @property
    def sqlite_wtb_url(self) -> str:
        return self._sqlite_url(self.wtb_db_path)

    @property
    def sqlite_agentgit_url(self) -> str:
        return self._sqlite_url(self.agentgit_db_path)

    @property
    def sqlite_filetracker_url(self) -> str:
        return self._sqlite_url(self.filetracker_db_path)

    # ---------- PostgreSQL URL generation (per component) ----------
    def _postgresql_url(self, database: str) -> str:
        """Generate PostgreSQL URL for a specific database."""
        encoded_password = quote_plus(self.pg_password)
        return (
            f"postgresql://{self.pg_user}:{encoded_password}"
            f"@{self.pg_host}:{self.pg_port}/{database}"
        )

    @property
    def pg_wtb_url(self) -> str:
        return self._postgresql_url(self.pg_database_wtb)

    @property
    def pg_agentgit_url(self) -> str:
        return self._postgresql_url(self.pg_database_agentgit)

    @property
    def pg_filetracker_url(self) -> str:
        return self._postgresql_url(self.pg_database_filetracker)

    # ---------- Unified database URLs (switch by mode) ----------
    @property
    def wtb_db_url(self) -> str:
        if self.mode == "postgresql":
            return self.pg_wtb_url
        return self.sqlite_wtb_url

    @property
    def agentgit_db_url(self) -> str:
        if self.mode == "postgresql":
            return self.pg_agentgit_url
        return self.sqlite_agentgit_url

    @property
    def filetracker_db_url(self) -> str:
        if self.mode == "postgresql":
            return self.pg_filetracker_url
        return self.sqlite_filetracker_url


def get_project_root() -> Path:
    """Find project root by pyproject.toml or .git."""
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "pyproject.toml").exists() or (parent / ".git").exists():
            return parent
    return Path.cwd()


def get_database_config() -> DatabaseConfig:
    """Return minimal fixed database config."""
    data_dir = get_project_root() / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return DatabaseConfig(mode=StorageMode, data_dir=data_dir)


def print_database_locations() -> None:
    """Print all database locations (simplified with loops)."""
    config = get_database_config()
    print("\n" + "=" * 60)
    print(f"DATABASE LOCATIONS ({config.mode.upper()})")
    print("=" * 60)

    DB_NAMES = ["wtb", "agentgit", "filetracker"]
    if config.mode == "sqlite":
        print(f"\nData Directory: {config.data_dir}")
        for name in DB_NAMES:
            path = getattr(config, f"{name}_db_path")
            url = getattr(config, f"{name}_db_url")
            print(f"\n{name.capitalize()} (SQLite):")
            print(f"  Path: {path}")
            print(f"  URL:  {url}")
            print(f"  Exists: {path.exists()}")
    else:
        print(f"\nPostgreSQL:")
        print(f"  Host: {config.pg_host}:{config.pg_port}")
        print(f"  User: {config.pg_user}")
        print(f"\nDatabases:")
        for name in DB_NAMES:
            db_name = getattr(config, f"pg_database_{name}")
            print(f"  {name.capitalize():11} {db_name}")
        print(f"\nConnection URLs (password hidden):")
        for name in DB_NAMES:
            db_name = getattr(config, f"pg_database_{name}")
            print(f"  {name.capitalize():11} postgresql://{config.pg_user}:****@{config.pg_host}:{config.pg_port}/{db_name}")

    print("=" * 60 + "\n")


if __name__ == "__main__":
    print_database_locations()