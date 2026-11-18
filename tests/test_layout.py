"""The repo's three Python folders stay apart (ingestion split, 17 Nov 2026).

    scripts/   the agent and its API (the API's image: scripts/ + common/)
    ingest/    the loaders, run by hand or by ingest/pipeline.py; never in an image
    common/    what both use: db_config.py, sop_search.py
    frontend/  the page (tested in test_agent_ui.py)

The API must not import a loader (its image has no ingest/), a loader must not import the
agent, and each script must run from anywhere without PYTHONPATH: the modules that import
from common/ put it on sys.path themselves.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FOLDERS = ("scripts", "ingest", "common", "frontend")


def modules(folder: str) -> set[str]:
    return {p.stem for p in (ROOT / folder).glob("*.py")}


def imports(path: Path) -> set[str]:
    """Every module a file imports, at the top or inside a function."""
    found = set()
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Import):
            found |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            found.add(n.module.split(".")[0])
    return found


def imported_from(folder: str, other: str) -> dict[str, set[str]]:
    theirs = modules(other)
    return {p.name: hits for p in sorted((ROOT / folder).glob("*.py"))
            if (hits := imports(p) & theirs)}


def test_the_folders_hold_what_they_should():
    assert {"agent_api", "agent_app", "agent_tools", "agent_graph"} <= modules("scripts")
    assert {"ingest_cmms", "ingest_sops", "pipeline", "relationships", "lineage",
            "agent_user"} <= modules("ingest")
    assert modules("common") >= {"db_config", "sop_search"}


def test_no_module_name_is_in_two_folders():
    """One name, one file: with all four folders on pytest's path, a second db_config.py
    would be imported in place of the first without a word."""
    seen: dict[str, str] = {}
    twice = []
    for folder in FOLDERS:
        for m in modules(folder):
            if m in seen:
                twice.append(f"{m} in {seen[m]}/ and {folder}/")
            seen[m] = folder
    assert not twice


def test_the_api_side_imports_nothing_from_ingest():
    assert imported_from("scripts", "ingest") == {}


def test_ingest_imports_nothing_from_the_api_side():
    assert imported_from("ingest", "scripts") == {}


def test_common_imports_only_itself_and_packages():
    assert imported_from("common", "scripts") == {}
    assert imported_from("common", "ingest") == {}
    assert imported_from("common", "frontend") == {}


def test_the_api_image_has_common_and_not_ingest():
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copies = [ln.split()[1] for ln in docker.splitlines() if ln.startswith("COPY ")]
    assert "scripts/" in copies and "common/" in copies
    assert not any(c.startswith(("ingest", "frontend", ".")) for c in copies)
    for name in (".gcloudignore", ".dockerignore"):
        ignored = (ROOT / name).read_text(encoding="utf-8").split()
        assert "ingest/" in ignored, name


def _clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}


CLIS = ["ingest_logs", "ingest_spares", "ingest_cmms", "ingest_images", "ingest_sops",
        "relationships", "pipeline", "agent_user", "plot_sensor_csv"]


@pytest.mark.parametrize("name", CLIS)
def test_each_loader_runs_from_anywhere_without_pythonpath(name, tmp_path):
    """`python ingest/<name>.py --help` from another folder, with no PYTHONPATH: proves the
    script finds db_config / sop_search in common/ (and its ingest/ neighbours) itself."""
    if not (ROOT / "ingest" / f"{name}.py").is_file() and name == "plot_sensor_csv":
        pytest.skip("no plot_sensor_csv.py in this repo (optional)")
    r = subprocess.run([sys.executable, str(ROOT / "ingest" / f"{name}.py"), "--help"],
                       cwd=tmp_path, env=_clean_env(), capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert "usage" in r.stdout.lower()


def test_the_agent_finds_the_sop_search_without_pythonpath(tmp_path):
    """What the API's image does: scripts/ on the path (uvicorn --app-dir scripts), nothing
    else. agent_tools must reach common/ itself, and get the same search the loader uses."""
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import agent_tools, sop_search, "
            "db_config; print(sop_search.__file__); print(db_config.__file__)")
    r = subprocess.run([sys.executable, "-c", code, str(ROOT / "scripts")], cwd=tmp_path,
                       env=_clean_env(), capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    homes = {Path(line).resolve().parent for line in r.stdout.split()}
    assert homes == {(ROOT / "common").resolve()}