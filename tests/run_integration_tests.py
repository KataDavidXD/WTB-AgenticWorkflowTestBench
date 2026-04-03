"""
Integration Test Runner with Real Services and Local Database.

This script sets up real services and runs integration tests:
1. Creates local SQLite databases (wtb.db, checkpoints.db)
2. Runs database migrations
3. Starts OutboxProcessor in background
4. Runs all integration tests
5. Cleans up resources

Usage:
    python tests/run_integration_tests.py
    python tests/run_integration_tests.py --verbose
    python tests/run_integration_tests.py --keep-db  # Don't cleanup after

Reference: CONSOLIDATED_ISSUES.md - Integration Test Requirements
"""

import argparse
import sys
import os
import shutil
import tempfile
import time
import subprocess
from pathlib import Path
from typing import Optional
from contextlib import contextmanager

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class IntegrationTestEnvironment:
    """
    Manages the integration test environment with real services.
    """
    
    def __init__(
        self,
        data_dir: Optional[Path] = None,
        keep_db: bool = False,
        verbose: bool = False,
    ):
        self.data_dir = data_dir or Path(tempfile.mkdtemp(prefix="wtb_test_"))
        self.keep_db = keep_db
        self.verbose = verbose
        self.outbox_processor = None
        
        # Database paths
        self.wtb_db_path = self.data_dir / "wtb.db"
        self.checkpoint_db_path = self.data_dir / "checkpoints.db"
        self.wtb_db_url = f"sqlite:///{self.wtb_db_path}"
        self.checkpoint_db_url = f"sqlite:///{self.checkpoint_db_path}"
        
    def setup(self):
        """Set up the test environment."""
        print(f"Setting up test environment in: {self.data_dir}")
        
        # Create directories
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize databases
        self._init_databases()
        
        # Run migrations
        self._run_migrations()
        
        # Start background services
        self._start_services()
        
        print("Test environment ready.")
        
    def _init_databases(self):
        """Initialize SQLite databases."""
        print("Initializing databases...")
        
        from wtb.infrastructure.database.unit_of_work import SQLAlchemyUnitOfWork
        
        # Initialize WTB database
        with SQLAlchemyUnitOfWork(self.wtb_db_url) as uow:
            uow.commit()
        
        print(f"  - WTB database: {self.wtb_db_path}")
        print(f"  - Checkpoint database: {self.checkpoint_db_path}")
        
    def _run_migrations(self):
        """Run database migrations."""
        print("Running migrations...")
        
        migrations_dir = PROJECT_ROOT / "wtb" / "infrastructure" / "database" / "migrations"
        
        if not migrations_dir.exists():
            print("  - No migrations directory found, skipping.")
            return
        
        # For SQLite, migrations are typically applied on first connection
        # The ORM models auto-create tables
        print("  - Tables created via ORM auto-migration")
        
    def _start_services(self):
        """Start background services."""
        print("Starting background services...")
        
        from wtb.infrastructure.outbox import OutboxProcessor
        
        # Create and start outbox processor
        self.outbox_processor = OutboxProcessor(
            wtb_db_url=self.wtb_db_url,
            poll_interval_seconds=1.0,
            batch_size=50,
            strict_verification=False,
        )
        self.outbox_processor.start()
        
        print("  - OutboxProcessor started")
        
    def teardown(self):
        """Tear down the test environment."""
        print("\nTearing down test environment...")
        
        # Stop services
        if self.outbox_processor and self.outbox_processor.is_running():
            self.outbox_processor.stop(timeout=5.0)
            print("  - OutboxProcessor stopped")
        
        # Cleanup database files
        if not self.keep_db:
            if self.data_dir.exists():
                shutil.rmtree(self.data_dir)
                print(f"  - Cleaned up: {self.data_dir}")
        else:
            print(f"  - Keeping database at: {self.data_dir}")
        
        print("Teardown complete.")
        
    def get_env_vars(self) -> dict:
        """Get environment variables for tests."""
        return {
            "WTB_DB_URL": self.wtb_db_url,
            "WTB_CHECKPOINT_DB_URL": self.checkpoint_db_url,
            "WTB_DATA_DIR": str(self.data_dir),
            "WTB_TEST_MODE": "integration",
        }
        
    def run_tests(self, test_paths: Optional[list] = None, pytest_args: Optional[list] = None) -> int:
        """
        Run pytest with the configured environment.
        
        Returns:
            Exit code from pytest
        """
        # Default test paths
        if test_paths is None:
            test_paths = [
                "tests/test_outbox_transaction_consistency/",
                "tests/test_architecture/",
            ]
        
        # Build pytest command
        cmd = ["python", "-m", "pytest"]
        cmd.extend(test_paths)
        
        if self.verbose:
            cmd.append("-v")
        
        if pytest_args:
            cmd.extend(pytest_args)
        
        # Set environment variables
        env = os.environ.copy()
        env.update(self.get_env_vars())
        
        print(f"\nRunning: {' '.join(cmd)}")
        print("-" * 60)
        
        # Run tests
        result = subprocess.run(cmd, env=env, cwd=str(PROJECT_ROOT))
        
        return result.returncode


@contextmanager
def integration_test_environment(**kwargs):
    """Context manager for integration test environment."""
    env = IntegrationTestEnvironment(**kwargs)
    try:
        env.setup()
        yield env
    finally:
        env.teardown()


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Run WTB integration tests with real services"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose output"
    )
    parser.add_argument(
        "--keep-db",
        action="store_true",
        help="Keep database files after tests"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Custom data directory for databases"
    )
    parser.add_argument(
        "--test-path",
        action="append",
        help="Specific test paths to run (can specify multiple)"
    )
    parser.add_argument(
        "pytest_args",
        nargs="*",
        help="Additional pytest arguments"
    )
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("WTB Integration Test Runner")
    print("=" * 60)
    
    with integration_test_environment(
        data_dir=args.data_dir,
        keep_db=args.keep_db,
        verbose=args.verbose,
    ) as env:
        exit_code = env.run_tests(
            test_paths=args.test_path,
            pytest_args=args.pytest_args,
        )
    
    print("=" * 60)
    print(f"Tests completed with exit code: {exit_code}")
    print("=" * 60)
    
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
