"""Fail-closed release sequencing for the source-only DSH contract."""

from __future__ import annotations

import ast
import os
import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version


ROOT = Path(__file__).resolve().parents[2]


def _metadata() -> dict[str, object]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))


def test_public_version_constant_matches_distribution_metadata() -> None:
    metadata_version = _metadata()["project"]["version"]
    module = ast.parse((ROOT / "wtb/__init__.py").read_text("utf-8"))
    version_assignments = [
        node.value.value
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        )
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]

    assert version_assignments == [metadata_version]


def test_development_version_cannot_match_a_stable_release_tag() -> None:
    metadata = _metadata()
    version = Version(metadata["project"]["version"])
    release = metadata["tool"]["wtb"]["release"]

    assert version == Version("0.4.0.dev0")
    assert version.is_devrelease
    assert release["phase"] == "development"
    assert re.fullmatch(r"v\d+\.\d+\.\d+", f"v{version}") is None


def test_source_only_contract_has_no_claimed_sdk_distribution() -> None:
    metadata = _metadata()
    extras = metadata["project"]["optional-dependencies"]
    release = metadata["tool"]["wtb"]["release"]

    assert release["deepseek-harness-contract"] == "durable-attempt-v2"
    assert release["deepseek-harness-distribution"] == "source-only"
    assert "deepseek-harness" not in extras
    assert {
        Requirement(requirement).name
        for requirement in extras["deepseek-harness-source"]
    } == {"pydantic"}


def test_publish_gate_rejects_source_only_development_state() -> None:
    if os.environ.get("WTB_REQUIRE_STABLE_RELEASE") != "1":
        return

    metadata = _metadata()
    version = Version(metadata["project"]["version"])
    extras = metadata["project"]["optional-dependencies"]
    release = metadata["tool"]["wtb"]["release"]
    sdk_requirements = [
        Requirement(requirement)
        for requirement in extras.get("deepseek-harness", ())
    ]

    assert not version.is_prerelease, "stable publication requires a stable version"
    assert release["phase"] == "release"
    assert release["deepseek-harness-distribution"] == "published"
    assert any(
        requirement.name == "deepseek-harness-sdk"
        and str(requirement.specifier).startswith("==")
        and "," not in str(requirement.specifier)
        and "*" not in str(requirement.specifier)
        for requirement in sdk_requirements
    ), "stable publication requires an exact compatible SDK dependency"


def test_ci_and_publish_workflows_enforce_the_same_release_state() -> None:
    test_workflow = (ROOT / ".github/workflows/test.yml").read_text("utf-8")
    publish_workflow = (ROOT / ".github/workflows/publish.yml").read_text("utf-8")

    assert "deepseek-source-contract:" in test_workflow
    assert '".[deepseek-harness-source]"' in test_workflow
    assert "deepseek-harness-sdk" not in test_workflow
    assert "Run release sequencing gate" in publish_workflow
    assert "tests/release/test_release_sequencing.py" in publish_workflow
    assert "WTB_REQUIRE_STABLE_RELEASE: \"1\"" in publish_workflow
    assert publish_workflow.index("Run release sequencing gate") < publish_workflow.index(
        "Build distributions"
    )
