from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import ClassVar


class ProjectInfo:
    """Helper to access main project configuration and dependencies."""

    _instance: ClassVar[ProjectInfo | None] = None
    _dependencies: dict[str, str] = {}

    def __init__(self, project_root: Path | None = None) -> None:
        if project_root:
            self.project_root = project_root
        else:
            # Assume src/services/project_info.py -> src/services -> src -> root
            self.project_root = Path(__file__).resolve().parent.parent.parent
        self._load_dependencies()

    @classmethod
    def get_instance(cls) -> ProjectInfo:
        if cls._instance is None:
            cls._instance = ProjectInfo()
        return cls._instance

    def _load_dependencies(self) -> None:
        """Load dependencies from pyproject.toml."""
        pyproject_path = self.project_root / "pyproject.toml"
        if not pyproject_path.exists():
            return

        try:
            with open(pyproject_path, "rb") as f:
                data = tomllib.load(f)
            
            deps = data.get("project", {}).get("dependencies", [])
            self._dependencies = self._parse_dependencies(deps)
        except Exception:
            # Fallback or log error if needed
            self._dependencies = {}

    def _parse_dependencies(self, deps: list[str]) -> dict[str, str]:
        """
        Parse a list of dependency strings into a map of {package_name: full_specifier}.
        Example: ["fastapi>=0.100"] -> {"fastapi": "fastapi>=0.100"}
        """
        result = {}
        # Regex to capture the package name (start of string, valid chars)
        # Valid python package name: [A-Z0-9]|[A-Z0-9][A-Z0-9._-]*[A-Z0-9]
        # We will stop at the first character that is not a package name char.
        # Common specifiers start with =, <, >, ~, !, ; (environment marker)
        name_pattern = re.compile(r"^([a-zA-Z0-9][a-zA-Z0-9._-]*)")
        
        for dep in deps:
            match = name_pattern.match(dep)
            if match:
                name = match.group(1).lower()
                result[name] = dep
        return result

    def get_dependency_mapping(self, requested_packages: list[str]) -> list[str]:
        """
        Map requested packages to their versioned counterparts from the main project.
        If a package is requested without a version specifier, and it exists in the
        main project dependencies, replace it with the main project's specifier.
        """
        mapped = []
        # Regex to check if the user provided a specifier.
        # If the string contains only the package name (allowed chars), we assume no specifier.
        # If it has =, <, >, etc., we assume the user specified a version.
        # Note: This is a heuristic.
        specifier_chars = set("=<>!~;")
        
        for pkg in requested_packages:
            # Check if user provided a specifier
            has_specifier = any(c in pkg for c in specifier_chars)
            
            if not has_specifier:
                # Normalize name to look up
                pkg_name = pkg.lower()
                if pkg_name in self._dependencies:
                    # Use the project's constraint
                    mapped.append(self._dependencies[pkg_name])
                else:
                    # No mapping found, use as is
                    mapped.append(pkg)
            else:
                # User provided specific version, respect it
                mapped.append(pkg)
                
        return mapped
