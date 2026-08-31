"""Deterministic command-line entry points for validation and demonstration."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from recallops.data.loaders import load_demo_dataset, load_recall_snapshot, validate_manifest
from recallops.paths import DATA_DIR
from recallops.ui.adapter import DurableRuntimeAdapter
from recallops.ui.presenters import APPROVAL_JUSTIFICATION


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="recallops", description="RecallOps academic demo CLI")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("data-validate", help="Validate pinned public and synthetic data")
    demo = subcommands.add_parser("demo", help="Run the credential-free flagship walkthrough")
    demo.add_argument("--recall-number", required=True)
    evaluate = subcommands.add_parser("eval", help="Summarize an evaluation report")
    evaluate.add_argument("--report", type=Path, default=DATA_DIR / "evals" / "report.json")
    subcommands.add_parser("mcp-config", help="Print safe stdio MCP configuration")
    return parser


def _data_validate() -> int:
    recall = load_recall_snapshot()
    dataset = load_demo_dataset()
    errors = validate_manifest(dataset)
    if errors:
        for error in errors:
            print(f"DATA INVALID: {error}", file=sys.stderr)
        return 1
    print(f"DATA VALID: {recall.recall_number} · OFFICIAL — openFDA snapshot")
    print(
        f"{len(dataset['products'])} products · {len(dataset['lots'])} lots · "
        f"{len(dataset['events'])} events · SYNTHETIC — ACADEMIC DEMO"
    )
    print("Northstar Grocers is fictional training data; it is not a party to the public recall.")
    return 0


async def _run_demo(recall_number: str, runtime_dir: Path, transport: str) -> int:
    adapter = DurableRuntimeAdapter(
        checkpoint_path=runtime_dir / "checkpoints.sqlite3",
        operations_path=runtime_dir / "operations.sqlite3",
        transport=transport,
    )
    case = await adapter.open_case(recall_number)
    case["case_id"] = f"CASE-DEMO-{recall_number}"
    case["thread_id"] = f"THREAD-DEMO-{recall_number}"
    print("RecallOps Command Center")
    print(f"Recall number: {recall_number}")
    print("OFFICIAL — openFDA snapshot | Cached/frozen fallback")
    print("SYNTHETIC — ACADEMIC DEMO | Northstar is fictional training data")
    case = await adapter.run_investigation(case)
    print("Agentic RAG: BM25 sparse + LSA dense → RRF → deterministic rerank → critic")
    print("Runtime: Durable LangGraph + SQLite")
    print(f"Transport: {adapter.transport_label}")
    print(
        "Planner: deterministic | Specialists: Regulatory Intake, Product & Lot Matching, Traceability, Containment | Independent verifier"
    )
    print(f"Review required | case={case['case_id']} | version={case['case_version']}")
    print(f"Decision=approve | Actor=Food-safety manager | Justification={APPROVAL_JUSTIFICATION}")
    case = await adapter.resume_review(
        case,
        decision="approve",
        actor="Food-safety manager",
        justification=APPROVAL_JUSTIFICATION,
        edited_action="",
    )
    print("Approve recorded zero writes; execution confirmation is pending")
    print("Simulate approved actions")
    case = await adapter.simulate_approved_actions(case)
    receipt = case["receipts"][-1]
    print(
        f"Simulated action recorded | receipt={receipt['receipt_id']} | "
        f"action={receipt['action_type']} | version={receipt['case_version']}"
    )
    case = await adapter.request_closure(case)
    print(case["closure"]["status"])
    for blocker in case["closure"]["blockers"]:
        print(f"BLOCKER: {blocker}")
    print("Final closure remains a separate human-reviewed, version-bound action.")
    return 0


def _demo(recall_number: str) -> int:
    transport = os.environ.get("RECALLOPS_MCP_TRANSPORT", "direct").strip().casefold()
    configured_runtime_dir = os.environ.get("RECALLOPS_RUNTIME_DIR")
    if configured_runtime_dir:
        return asyncio.run(_run_demo(recall_number, Path(configured_runtime_dir), transport))
    with tempfile.TemporaryDirectory(prefix="recallops-demo-") as temporary:
        return asyncio.run(_run_demo(recall_number, Path(temporary), transport))


def _eval(report_path: Path) -> int:
    if not report_path.exists():
        print(f"Evaluation report not available: {report_path}", file=sys.stderr)
        return 1
    report = json.loads(report_path.read_text())
    scenarios = report.get("scenarios", [])
    if not isinstance(scenarios, list):
        print("Evaluation report has no scenario list.", file=sys.stderr)
        return 1
    critical = [item for item in scenarios if item.get("safety_critical") is True]
    passed = [item for item in critical if item.get("passed") is True]
    print(f"Evaluation scenarios: {len(scenarios)}")
    print(f"Safety-critical: {len(passed)}/{len(critical)} passed")
    return 0 if len(passed) == len(critical) and bool(critical) else 1


def _mcp_config() -> int:
    module_by_name = {
        "recall-registry": "recallops.mcp.recall_registry_server",
        "traceability": "recallops.mcp.traceability_server",
        "operations": "recallops.mcp.operations_server",
    }
    payload: dict[str, Any] = {
        "mcpServers": {
            name: {"command": "uv", "args": ["run", "python", "-m", module]}
            for name, module in module_by_name.items()
        }
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "data-validate":
            return _data_validate()
        if args.command == "demo":
            return _demo(args.recall_number)
        if args.command == "eval":
            return _eval(args.report)
        if args.command == "mcp-config":
            return _mcp_config()
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(str(exc).splitlines()[0][:240], file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
