from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_fastmcp_security_floor_is_version_3_2() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = project["project"]["dependencies"]

    assert "fastmcp>=3.2.0,<4" in requirements


def test_locked_fastmcp_is_patched_and_diskcache_is_absent() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = {package["name"]: package for package in lock["package"]}

    assert tuple(map(int, packages["fastmcp"]["version"].split("."))) >= (3, 2, 0)
    assert "diskcache" not in packages
