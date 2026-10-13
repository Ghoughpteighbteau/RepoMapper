import asyncio

import pytest

from repomapper import repomap_class, repomap_server


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(repomap_class, "TAGS_CACHE_DIR", str(tmp_path / ".cache"))


def search(**kwargs):
    fn = getattr(repomap_server.search_identifiers, "fn", repomap_server.search_identifiers)
    return asyncio.run(fn(**kwargs))


def test_search_context_lines_are_capped(tmp_path):
    long_line = "const s = '" + "x" * 5000 + "';"
    short_lines = "\n".join(f"const v{i} = {i};" for i in range(50))
    (tmp_path / "a.ts").write_text(f"export function interleave() {{}}\n{long_line}\n{short_lines}\n")

    results = search(project_root=str(tmp_path), query="interleave")["results"]

    assert results and results[0]["line"] == 1
    assert all(len(line) <= repomap_server.MAX_CONTEXT_LINE_LENGTH for r in results for line in r["context"].splitlines())
