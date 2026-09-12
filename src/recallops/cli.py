"""Deterministic command-line entry points for validation and demonstration."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from recallops.data.loaders import load_demo_dataset, load_recall_snapshot, validate_manifest
from recallops.paths import DATA_DIR, EvaluationArtifactPaths
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
    retrieval = subcommands.add_parser(
        "eval-retrieval", help="Run or verify the deterministic retrieval benchmark"
    )
    retrieval.add_argument(
        "--report", type=Path, default=DATA_DIR / "evals" / "retrieval_report.json"
    )
    retrieval.add_argument("--cases", type=Path)
    retrieval.add_argument("--run", action="store_true", help="Regenerate before summarizing")
    orchestration = subcommands.add_parser(
        "eval-orchestration", help="Run or verify the read-only orchestration benchmark"
    )
    orchestration.add_argument(
        "--report", type=Path, default=DATA_DIR / "evals" / "orchestration_report.json"
    )
    orchestration.add_argument("--cases", type=Path)
    orchestration.add_argument("--run", action="store_true", help="Regenerate before summarizing")
    orchestration.add_argument(
        "--openai", action="store_true", help="Use the shared OpenAI live service (opt-in only)"
    )
    orchestration.add_argument(
        "--live",
        action="store_true",
        help="Load project environment and use its live adapter or shared OpenAI service",
    )
    orchestration.add_argument(
        "--live-adapter",
        metavar="MODULE:ATTRIBUTE",
        help="Explicit LiveRunnerFactory object or zero-argument factory (opt-in only)",
    )
    smoke = subcommands.add_parser(
        "eval-model-smoke", help="Run exactly one OpenAI case into a separate new smoke report"
    )
    smoke.add_argument("--report", type=Path, required=True)
    scorecard = subcommands.add_parser(
        "eval-scorecard", help="Verify and summarize the combined offline scorecard"
    )
    scorecard.add_argument("--scorecard", type=Path, default=DATA_DIR / "evals" / "scorecard.json")
    scorecard.add_argument("--safety-report", type=Path)
    scorecard.add_argument("--retrieval-report", type=Path)
    scorecard.add_argument("--orchestration-report", type=Path)
    subcommands.add_parser("mcp-config", help="Print safe stdio MCP configuration")
    subcommands.add_parser("llm-check", help="Require configured OpenAI mode and credentials")
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
    scenarios = report.get("results", [])
    if not isinstance(scenarios, list):
        print("Evaluation report has no scenario list.", file=sys.stderr)
        return 1
    critical = [item for item in scenarios if item.get("safety_critical") is True]
    passed = [item for item in critical if item.get("passed") is True]
    gate_passed = report.get("gate_passed") is True
    print(f"Evaluation scenarios: {len(scenarios)}")
    print(f"Safety-critical: {len(passed)}/{len(critical)} passed")
    print(f"Evaluation gate: {'PASSED' if gate_passed else 'FAILED'}")
    return 0 if gate_passed and len(passed) == len(critical) and bool(critical) else 1


def _unverified(label: str, error: BaseException) -> int:
    lines = str(error).splitlines()
    detail = lines[0][:200] if lines else "evaluation error"
    print(f"{label}: UNVERIFIED — {detail}")
    return 1


def _case_path(report_path: Path, configured: Path | None, filename: str) -> Path:
    return configured if configured is not None else report_path.with_name(filename)


async def _atomic_benchmark[Report: Any](
    output_path: Path,
    run: Callable[[Path], Awaitable[Report]],
    validate: Callable[[Path], Report],
) -> Report:
    """Build beside the destination, verify exact bytes, then replace atomically."""
    destination = output_path.expanduser().absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        await run(temporary)
        report = validate(temporary)
        if report.gate_passed:
            os.replace(temporary, destination)
            directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return report
    finally:
        temporary.unlink(missing_ok=True)


def _eval_retrieval(report_path: Path, case_path: Path | None, *, run: bool) -> int:
    from recallops.evaluation.retrieval_benchmark import (
        load_retrieval_report,
        run_retrieval_benchmark,
    )

    cases = _case_path(report_path, case_path, "retrieval_cases.json")
    try:
        if run:
            report = asyncio.run(
                _atomic_benchmark(
                    report_path,
                    lambda output: run_retrieval_benchmark(cases, output),
                    lambda output: load_retrieval_report(output, cases),
                )
            )
        else:
            report = load_retrieval_report(report_path, cases)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return _unverified("Retrieval evaluation", exc)
    except Exception:
        return _unverified("Retrieval evaluation", RuntimeError("evaluation runner failed"))
    agentic = next(item for item in report.configurations if item.name == "agentic_rag")
    print(f"Retrieval cases: {len(agentic.results)}")
    print(f"Persisted results: {sum(len(item.results) for item in report.configurations)}")
    print(f"Agentic RAG Recall@5: {agentic.metrics.recall_at_5:.6f}")
    print(f"Agentic RAG nDCG@5: {agentic.metrics.ndcg_at_5:.6f}")
    print(f"Fusion Recall@5 delta: {report.gates.fusion_recall_delta:+.6f}")
    print(f"Rerank nDCG@5 delta: {report.gates.rerank_ndcg_delta:+.6f}")
    print(f"Case corpus SHA-256: {report.retrieval_case_corpus_sha256}")
    print(f"Knowledge corpus SHA-256: {report.knowledge_corpus_sha256}")
    print(f"Report SHA-256: {report.report_sha256}")
    print(f"Retrieval gate: {'PASSED' if report.gate_passed else 'FAILED'}")
    return 0 if report.gate_passed else 1


def _load_live_adapter(specification: str):
    from recallops.evaluation.orchestration_benchmark import LiveRunnerFactory

    if (
        re.fullmatch(
            r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*",
            specification,
            flags=re.ASCII,
        )
        is None
    ):
        raise ValueError("live adapter must use MODULE:ATTRIBUTE syntax")
    module_name, attribute_name = specification.split(":", 1)
    try:
        candidate = getattr(importlib.import_module(module_name), attribute_name)
    except (AttributeError, ImportError) as exc:
        raise ValueError("configured live adapter is unavailable") from exc
    try:
        adapter = (
            candidate()
            if callable(candidate) and type(candidate) is not LiveRunnerFactory
            else candidate
        )
    except Exception as exc:
        raise ValueError("configured live adapter or provider credentials are unavailable") from exc
    if type(adapter) is not LiveRunnerFactory:
        raise ValueError("configured live adapter must provide LiveRunnerFactory")
    return adapter


def _eval_orchestration(
    report_path: Path,
    case_path: Path | None,
    *,
    run: bool,
    live_adapter: str | None,
    openai: bool = False,
    live: bool = False,
) -> int:
    from recallops.evaluation.orchestration_benchmark import (
        load_orchestration_report,
        run_orchestration_benchmark,
    )

    cases = _case_path(report_path, case_path, "orchestration_cases.json")
    if live and (not run or openai or live_adapter is not None):
        return _unverified(
            "Orchestration evaluation",
            ValueError("--live requires --run and no --openai or --live-adapter"),
        )
    if live_adapter is not None and not run:
        return _unverified("Orchestration evaluation", ValueError("--live-adapter requires --run"))
    if openai and (not run or live_adapter is not None):
        return _unverified(
            "Orchestration evaluation", ValueError("--openai requires --run and no --live-adapter")
        )
    try:
        if live:
            from recallops.llm.config import load_project_env

            load_project_env()
            live_adapter = os.environ.get("LIVE_MODEL_ADAPTER") or None
            openai = live_adapter is None
        adapter = _load_live_adapter(live_adapter) if live_adapter is not None else None
        if openai:
            from recallops.evaluation.openai_live_adapter import build_openai_live_factory

            adapter = build_openai_live_factory(_openai_settings())
        if run:
            report = asyncio.run(
                _atomic_benchmark(
                    report_path,
                    lambda output: run_orchestration_benchmark(cases, output, adapter),
                    lambda output: load_orchestration_report(output, cases),
                )
            )
        else:
            report = load_orchestration_report(report_path, cases)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return _unverified("Orchestration evaluation", exc)
    except Exception:
        return _unverified("Orchestration evaluation", RuntimeError("evaluation runner failed"))
    single, specialists = report.profiles
    print(f"Orchestration cases: {len(single.results)}")
    print(f"Persisted offline results: {sum(len(item.results) for item in report.profiles)}")
    print(f"Single-agent task success: {single.metrics.task_success_rate:.6f}")
    print(f"Fixed-specialist task success: {specialists.metrics.task_success_rate:.6f}")
    print(f"Optional live: {report.live_status.status} (excluded from offline gate)")
    if report.live_status.error_code is not None:
        print(f"Optional live error: {report.live_status.error_code}")
    print(f"Case corpus SHA-256: {report.orchestration_case_corpus_sha256}")
    print(f"Evidence boundary SHA-256: {report.evidence_boundary_sha256}")
    print(f"Report SHA-256: {report.report_sha256}")
    print(f"Orchestration gate: {'PASSED' if report.gate_passed else 'FAILED'}")
    return 0 if report.gate_passed else 1


def _eval_scorecard(
    scorecard_path: Path,
    safety_report: Path | None,
    retrieval_report: Path | None,
    orchestration_report: Path | None,
) -> int:
    from recallops.evaluation.scorecard import validate_scorecard

    parent = scorecard_path.parent
    paths = EvaluationArtifactPaths(
        safety_report=safety_report or parent / "report.json",
        retrieval_report=retrieval_report or parent / "retrieval_report.json",
        orchestration_report=orchestration_report or parent / "orchestration_report.json",
    )
    try:
        scorecard = validate_scorecard(scorecard_path, paths)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return _unverified("Combined evaluation", exc)
    for suite in scorecard.suite_summaries:
        verdict = "PASSED" if suite.gate_passed else "FAILED"
        print(f"{suite.name}: {suite.case_count} cases · {suite.result_count} results · {verdict}")
    print(f"Optional live: {scorecard.optional_live_status.status} (excluded from offline gate)")
    for name, digest in sorted(scorecard.artifact_digests.items()):
        print(f"{name.replace('_', ' ').title()} SHA-256: {digest}")
    print(f"Scorecard SHA-256: {scorecard.scorecard_sha256}")
    print(f"Offline evaluation gate: {'PASSED' if scorecard.offline_gate_passed else 'FAILED'}")
    return 0 if scorecard.offline_gate_passed else 1


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


def _openai_settings():
    from recallops.llm.config import get_llm_settings, load_project_env

    load_project_env()
    settings = get_llm_settings()
    if settings.mode != "openai":
        raise ValueError("RECALLOPS_MODEL_MODE must be openai for this command")
    return settings


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "llm-check":
            try:
                _openai_settings()
            except ValueError as error:
                return _unverified("OpenAI configuration", error)
            print("OpenAI configuration is ready")
            return 0
        if args.command == "data-validate":
            return _data_validate()
        if args.command == "demo":
            return _demo(args.recall_number)
        if args.command == "eval":
            return _eval(args.report)
        if args.command == "eval-retrieval":
            return _eval_retrieval(args.report, args.cases, run=args.run)
        if args.command == "eval-orchestration":
            return _eval_orchestration(
                args.report,
                args.cases,
                run=args.run,
                live_adapter=args.live_adapter,
                openai=args.openai,
                live=args.live,
            )
        if args.command == "eval-model-smoke":
            from recallops.evaluation.live_smoke import run_live_smoke

            report = asyncio.run(run_live_smoke(_openai_settings(), args.report))
            print(json.dumps(report, sort_keys=True))
            return 0 if report["passed"] else 1
        if args.command == "eval-scorecard":
            return _eval_scorecard(
                args.scorecard,
                args.safety_report,
                args.retrieval_report,
                args.orchestration_report,
            )
        if args.command == "mcp-config":
            return _mcp_config()
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(str(exc).splitlines()[0][:240], file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
