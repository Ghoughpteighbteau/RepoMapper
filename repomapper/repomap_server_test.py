import asyncio
import subprocess

import pytest

from repomapper import repomap_class, repomap_server
from repomapper.config import Config


@pytest.fixture(autouse=True)
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.setattr(repomap_class, "TAGS_CACHE_DIR", str(tmp_path / ".cache"))
    monkeypatch.setattr(repomap_server, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(repomap_server, "CREATED_FILES", [])


def search(**kwargs):
    fn = getattr(repomap_server.search_identifiers, "fn", repomap_server.search_identifiers)
    return asyncio.run(fn(**kwargs))


def test_search_context_lines_are_capped(tmp_path):
    long_line = "const s = '" + "x" * 5000 + "';"
    short_lines = "\n".join(f"const v{i} = {i};" for i in range(50))
    (tmp_path / "a.ts").write_text(f"export function interleave() {{}}\n{long_line}\n{short_lines}\n")

    results = search(query="interleave")["results"]

    assert results and results[0]["line"] == 1
    assert all(len(line) <= Config().search.max_context_line_length for r in results for line in r["context"].splitlines())


def test_search_suggests_ignore_file(tmp_path):
    (tmp_path / "a.ts").write_text("export function interleave() {}\n")

    assert ".repomap/ignore" in search(query="interleave")["hint"]


def test_search_respects_ignore_file(tmp_path):
    (tmp_path / ".repomap").mkdir()
    (tmp_path / ".repomap" / "ignore").write_text("bundle.js\n")
    (tmp_path / "a.ts").write_text("export function interleave() {}\n")
    (tmp_path / "bundle.js").write_text("function interleave() {}\n")

    files = {r["file"] for r in search(query="interleave")["results"]}

    assert files == {"a.ts"}


def test_startup_init_is_reported_in_first_response_only(tmp_path):
    (tmp_path / "a.ts").write_text("export function interleave() {}\n")

    repomap_server.init_project(str(tmp_path))
    first = search(query="interleave")["hint"]
    second = search(query="interleave")["hint"]

    assert (tmp_path / ".repomap" / "ignore").exists()
    assert "Created .repomap/ignore" in first
    assert "Created" not in second


def test_resolve_project_root_prefers_explicit_root(tmp_path):
    assert repomap_server.resolve_project_root(str(tmp_path)) == str(tmp_path.resolve())


def test_resolve_project_root_uses_git_root_of_cwd(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path / "sub")

    assert repomap_server.resolve_project_root() == str(tmp_path.resolve())


def test_search_reports_config_warnings(tmp_path):
    (tmp_path / ".repomap").mkdir()
    (tmp_path / ".repomap" / "config.toml").write_text("[search]\nmax_context_lines = 5\n")
    (tmp_path / "a.ts").write_text("export function interleave() {}\n")

    assert search(query="interleave")["config_warnings"] == [
        ".repomap/config.toml: unknown key 'search.max_context_lines'"
    ]
