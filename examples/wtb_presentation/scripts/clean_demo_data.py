#!/usr/bin/env python
"""
clean_demo_data.py - Clean demo data for fresh runs.

Removes all generated demo data:
- SQLite database files (*.db)
- Output files in workspace/outputs
- Embedding cache files
- Checkpoint files

Usage:
    cd <project-root>
    python examples/wtb_presentation/scripts/clean_demo_data.py
    
    # Clean specific directories only:
    python examples/wtb_presentation/scripts/clean_demo_data.py --only=db
    python examples/wtb_presentation/scripts/clean_demo_data.py --only=outputs
    
    # Dry run (show what would be deleted):
    python examples/wtb_presentation/scripts/clean_demo_data.py --dry-run
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import List, Tuple

# ═══════════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════════

# Base paths
PRESENTATION_DIR = Path(__file__).parent.parent
DATA_DIR = PRESENTATION_DIR / "data"
WORKSPACE_DIR = PRESENTATION_DIR / "workspace"
OUTPUTS_DIR = WORKSPACE_DIR / "outputs"
EMBEDDINGS_DIR = WORKSPACE_DIR / "embeddings"

# File patterns to clean
DB_PATTERNS = ["*.db", "*.db-journal", "*.db-wal", "*.db-shm"]
OUTPUT_PATTERNS = ["*.json", "*.txt", "*.csv", "*.md"]
EMBEDDING_PATTERNS = ["*.npy", "*.pkl", "*.faiss"]


# ═══════════════════════════════════════════════════════════════════════════════
# Cleaning Functions
# ═══════════════════════════════════════════════════════════════════════════════


def get_files_to_clean(directory: Path, patterns: List[str]) -> List[Path]:
    """Get list of files matching patterns in directory."""
    files = []
    if directory.exists():
        for pattern in patterns:
            files.extend(directory.glob(pattern))
    return files


def clean_directory(directory: Path, patterns: List[str], dry_run: bool = False) -> Tuple[int, int]:
    """
    Clean files matching patterns in directory.
    
    Args:
        directory: Directory to clean
        patterns: File patterns to match
        dry_run: If True, only print what would be deleted
        
    Returns:
        Tuple of (files_deleted, bytes_freed)
    """
    files = get_files_to_clean(directory, patterns)
    
    files_deleted = 0
    bytes_freed = 0
    
    for file_path in files:
        try:
            file_size = file_path.stat().st_size
            
            if dry_run:
                print(f"  [DRY RUN] Would delete: {file_path} ({file_size:,} bytes)")
            else:
                file_path.unlink()
                print(f"  Deleted: {file_path.name} ({file_size:,} bytes)")
            
            files_deleted += 1
            bytes_freed += file_size
            
        except Exception as e:
            print(f"  [ERROR] Failed to delete {file_path}: {e}")
    
    return files_deleted, bytes_freed


def clean_subdirectories(directory: Path, dry_run: bool = False) -> Tuple[int, int]:
    """
    Clean all subdirectories (but not the directory itself).
    
    Args:
        directory: Directory whose subdirectories to clean
        dry_run: If True, only print what would be deleted
        
    Returns:
        Tuple of (dirs_deleted, bytes_freed)
    """
    dirs_deleted = 0
    bytes_freed = 0
    
    if not directory.exists():
        return dirs_deleted, bytes_freed
    
    for subdir in directory.iterdir():
        if subdir.is_dir():
            try:
                # Calculate size
                total_size = sum(f.stat().st_size for f in subdir.rglob("*") if f.is_file())
                
                if dry_run:
                    print(f"  [DRY RUN] Would delete directory: {subdir} ({total_size:,} bytes)")
                else:
                    shutil.rmtree(subdir)
                    print(f"  Deleted directory: {subdir.name} ({total_size:,} bytes)")
                
                dirs_deleted += 1
                bytes_freed += total_size
                
            except Exception as e:
                print(f"  [ERROR] Failed to delete {subdir}: {e}")
    
    return dirs_deleted, bytes_freed


def clean_databases(dry_run: bool = False) -> Tuple[int, int]:
    """Clean database files."""
    print("\n[1] Cleaning database files...")
    
    total_files = 0
    total_bytes = 0
    
    # Clean DATA_DIR
    if DATA_DIR.exists():
        files, bytes_freed = clean_directory(DATA_DIR, DB_PATTERNS, dry_run)
        total_files += files
        total_bytes += bytes_freed
    
    # Also check for filetracker subdirectory
    filetracker_dir = DATA_DIR / ".filetrack"
    if filetracker_dir.exists():
        files, bytes_freed = clean_directory(filetracker_dir, DB_PATTERNS, dry_run)
        total_files += files
        total_bytes += bytes_freed
    
    print(f"  Total: {total_files} database files ({total_bytes:,} bytes)")
    return total_files, total_bytes


def clean_outputs(dry_run: bool = False) -> Tuple[int, int]:
    """Clean output files."""
    print("\n[2] Cleaning output files...")
    
    total_files = 0
    total_bytes = 0
    
    if OUTPUTS_DIR.exists():
        # Clean files in outputs
        files, bytes_freed = clean_directory(OUTPUTS_DIR, OUTPUT_PATTERNS, dry_run)
        total_files += files
        total_bytes += bytes_freed
        
        # Clean subdirectories in outputs
        dirs, dir_bytes = clean_subdirectories(OUTPUTS_DIR, dry_run)
        total_files += dirs
        total_bytes += dir_bytes
    
    print(f"  Total: {total_files} items ({total_bytes:,} bytes)")
    return total_files, total_bytes


def clean_embeddings(dry_run: bool = False) -> Tuple[int, int]:
    """Clean embedding cache files."""
    print("\n[3] Cleaning embedding cache...")
    
    total_files = 0
    total_bytes = 0
    
    if EMBEDDINGS_DIR.exists():
        files, bytes_freed = clean_directory(EMBEDDINGS_DIR, EMBEDDING_PATTERNS, dry_run)
        total_files += files
        total_bytes += bytes_freed
    
    print(f"  Total: {total_files} embedding files ({total_bytes:,} bytes)")
    return total_files, total_bytes


def clean_all(dry_run: bool = False) -> None:
    """Clean all demo data."""
    print("=" * 60)
    print("  WTB Demo Data Cleaner")
    print("=" * 60)
    
    if dry_run:
        print("\n[DRY RUN MODE - No files will be deleted]")
    
    total_items = 0
    total_bytes = 0
    
    # Clean databases
    items, bytes_freed = clean_databases(dry_run)
    total_items += items
    total_bytes += bytes_freed
    
    # Clean outputs
    items, bytes_freed = clean_outputs(dry_run)
    total_items += items
    total_bytes += bytes_freed
    
    # Clean embeddings
    items, bytes_freed = clean_embeddings(dry_run)
    total_items += items
    total_bytes += bytes_freed
    
    # Summary
    print("\n" + "=" * 60)
    print(f"  Summary: {total_items} items {'would be ' if dry_run else ''}cleaned")
    print(f"  Space {'would be ' if dry_run else ''}freed: {total_bytes:,} bytes ({total_bytes / 1024 / 1024:.2f} MB)")
    print("=" * 60)


# ═══════════════════════════════════════════════════════════════════════════════
# Main Entry Point
# ═══════════════════════════════════════════════════════════════════════════════


def main():
    """Run the cleaner."""
    parser = argparse.ArgumentParser(description="Clean WTB demo data")
    parser.add_argument(
        "--only",
        type=str,
        choices=["db", "outputs", "embeddings"],
        help="Clean only specific type of data",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without actually deleting",
    )
    
    args = parser.parse_args()
    
    if args.only == "db":
        clean_databases(args.dry_run)
    elif args.only == "outputs":
        clean_outputs(args.dry_run)
    elif args.only == "embeddings":
        clean_embeddings(args.dry_run)
    else:
        clean_all(args.dry_run)
    
    if not args.dry_run:
        print("\n[SUCCESS] Cleanup complete!")
    else:
        print("\n[DRY RUN] No files were deleted. Run without --dry-run to clean.")


if __name__ == "__main__":
    main()
