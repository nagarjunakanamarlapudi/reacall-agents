import json
from pathlib import Path

import httpx
import pytest

from recallops.config import get_settings
from recallops.data.loaders import load_demo_dataset, load_recall_snapshot
from recallops.services.recall_registry import RecallRegistryService
from recallops.services.traceability import TraceabilityService


def _public_root(tmp_path: Path) -> Path:
    root = tmp_path / "configured-data"
    public = root / "public"
    public.mkdir(parents=True)
    for name in ("H-1230-2026.json", "H-1230-2026.metadata.json"):
        source = Path("data/public") / name
        (public / name).write_bytes(source.read_bytes())
    return root


def test_settings_validate_source_mode_and_resolve_repository_defaults(monkeypatch) -> None:
    monkeypatch.delenv("RECALLOPS_DATA_DIR", raising=False)
    monkeypatch.delenv("RECALLOPS_SOURCE_MODE", raising=False)
    settings = get_settings()
    assert settings.data_dir.is_absolute()
    assert settings.data_dir.name == "data"
    assert settings.source_mode == "snapshot"

    monkeypatch.setenv("RECALLOPS_SOURCE_MODE", "unsupported")
    with pytest.raises(ValueError, match="RECALLOPS_SOURCE_MODE"):
        get_settings()


def test_loaders_and_services_use_configured_data_without_hidden_fallback(
    monkeypatch, tmp_path: Path
) -> None:
    missing = tmp_path / "missing-data"
    monkeypatch.setenv("RECALLOPS_DATA_DIR", str(missing))
    monkeypatch.setenv("RECALLOPS_SOURCE_MODE", "snapshot")

    with pytest.raises(FileNotFoundError):
        load_recall_snapshot()
    with pytest.raises(FileNotFoundError):
        load_demo_dataset()
    with pytest.raises(FileNotFoundError):
        RecallRegistryService().get_recall("H-1230-2026")
    with pytest.raises(FileNotFoundError):
        TraceabilityService()


def test_live_registry_uses_bounded_official_call_through_injected_transport(
    tmp_path: Path,
) -> None:
    root = _public_root(tmp_path)
    payload = json.loads((root / "public/H-1230-2026.json").read_text())["results"][0]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": [payload]})

    registry = RecallRegistryService(
        data_dir=root,
        source_mode="live",
        http_transport=httpx.MockTransport(handler),
        timeout_seconds=1.5,
    )
    record = registry.get_recall("H-1230-2026")

    assert record is not None
    assert record.provenance == "LIVE_OPENFDA"
    assert record.cached is False
    assert requests[0].url.host == "api.fda.gov"
    assert requests[0].url.params["limit"] == "1"
    assert 'recall_number:"H-1230-2026"' in requests[0].url.params["search"]
    assert max(requests[0].extensions["timeout"].values()) == 1.5


def test_live_registry_failure_uses_configured_snapshot_and_labels_it_cached(
    tmp_path: Path,
) -> None:
    root = _public_root(tmp_path)

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    registry = RecallRegistryService(
        data_dir=root,
        source_mode="live",
        http_transport=httpx.MockTransport(unavailable),
        timeout_seconds=0.5,
    )
    record = registry.get_recall("H-1230-2026")

    assert record is not None
    assert record.provenance == "OFFICIAL_OPENFDA_SNAPSHOT"
    assert record.cached is True
    assert "cached fallback" in record.source


def test_live_fallback_does_not_escape_a_missing_configured_path(tmp_path: Path) -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    registry = RecallRegistryService(
        data_dir=tmp_path / "missing",
        source_mode="live",
        http_transport=httpx.MockTransport(unavailable),
    )
    with pytest.raises(FileNotFoundError):
        registry.get_recall("H-1230-2026")


def test_traceability_service_honors_explicit_data_dir_and_source_mode(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        TraceabilityService(data_dir=tmp_path / "missing", source_mode="live")
