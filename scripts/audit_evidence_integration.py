"""Source inventory; physical function spans are not engineering hours."""

import argparse
import ast
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "c22e85564511b5ea37c11a7e1306a901f480e4be"


def inventory(path, selected=None):
    source = path.read_text(encoding="utf-8")
    functions = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            selected is None or node.name in selected
        ):
            functions.append(
                {
                    "name": node.name,
                    "start": node.lineno,
                    "end": node.end_lineno,
                    "physical_lines": node.end_lineno - node.lineno + 1,
                }
            )
    return {
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "total_physical_lines": len(source.splitlines()),
        "functions": functions,
    }


def run(legacy):
    unchanged = {}
    for relative in [
        "wtb/domain/publication_authority.py",
        "wtb/domain/interfaces/workflow_runtime.py",
        "wtb/application/services/execution_controller.py",
        "wtb/infrastructure/runtimes/deepseek_harness.py",
        "wtb/infrastructure/adapters/langgraph_workflow_runtime.py",
    ]:
        original = subprocess.check_output(
            ["git", "show", BASE + ":" + relative], cwd=ROOT
        ).replace(b"\r\n", b"\n")
        current = (ROOT / relative).read_bytes().replace(b"\r\n", b"\n")
        unchanged[relative] = original == current
        assert unchanged[relative]
    product = [
        "wtb/domain/execution_evidence.py",
        "wtb/domain/interfaces/publication_sink.py",
        "wtb/application/services/evidence_collector.py",
        "wtb/infrastructure/evidence/sqlite_store.py",
        "wtb/infrastructure/evidence/sql_store.py",
        "wtb/infrastructure/evidence/postgres_store.py",
        "wtb/infrastructure/evidence/publication_sink.py",
        "wtb/infrastructure/evidence/external_delivery.py",
    ]
    result = {
        "baseline": BASE,
        "unchanged_existing_interfaces_and_implementations": unchanged,
        "new_trusted_product_code": {p: inventory(ROOT / p) for p in product},
        "runtime_specific_receipt_mapping": inventory(
            ROOT / product[2],
            {"deepseek_native_reference", "langgraph_native_reference"},
        ),
        "integration_fixture": inventory(
            ROOT / "tests/integration/test_evidence_collectors_real.py"
        ),
        "interpretation": "Shared validation/storage/projection/sinks are paid once by all comparison arms. Native ID extraction alone is not total integration cost; graph/model setup and material mapping also count.",
    }
    if legacy:
        result["historical_application_adapter"] = inventory(
            legacy, {"collect", "wtb_snapshot"}
        )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(
        json.dumps(run(args.legacy), indent=2) + "\n", encoding="utf-8"
    )
