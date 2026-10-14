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
    monkeypatch.setattr(repomap_server, "HINT_SENT", False)


def search_raw(**kwargs):
    fn = getattr(repomap_server.search_identifiers, "fn", repomap_server.search_identifiers)
    return asyncio.run(fn(**kwargs))


def flat(response):
    """Hits of a grouped response in order, each with its file added."""
    return [{"file": f["file"], **h} for f in response["results"] for h in f["hits"]]


def search(**kwargs):
    """Like search_raw, but `results` is the flat hit list (the grouped shape is tested separately)."""
    response = search_raw(**kwargs)
    return {**response, "results": flat(response)} if "results" in response else response


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
    second = search(query="interleave").get("hint", "")

    assert (tmp_path / ".repomap" / "ignore").exists()
    assert "Created .repomap/ignore" in first
    assert "Created" not in second


def map_raw(**kwargs):
    fn = getattr(repomap_server.repo_map, "fn", repomap_server.repo_map)
    return asyncio.run(fn(**kwargs))


@pytest.mark.parametrize("call", [lambda: search_raw(query="interleave"), lambda: map_raw()])
def test_ignore_hint_comes_once_per_server_process_unless_results_look_generated(tmp_path, call):
    (tmp_path / "a.ts").write_text("export function interleave() {}\n")

    assert ".repomap/ignore" in call()["hint"]
    assert "hint" not in call()
    assert "hint" not in call()


def test_search_repeats_the_hint_when_hits_are_in_generated_looking_files(tmp_path):
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "app.min.js").write_text("function interleave() {}\n")
    (tmp_path / "a.ts").write_text("export function interleave() {}\n")
    (tmp_path / "b.ts").write_text("export function other() {}\n")
    search_raw(query="other")                               # the first call spends the once-per-process hint

    assert "hint" not in search_raw(query="other")
    assert ".repomap/ignore" in search_raw(query="interleave")["hint"]


def test_repo_map_repeats_the_hint_when_the_map_lists_generated_looking_files(tmp_path):
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "lib.bundle.js").write_text("function interleave() {}\n")
    (tmp_path / "a.ts").write_text("export function other() {}\n")
    map_raw(chat_files=["a.ts"], other_files=["a.ts"])
    assert "hint" not in map_raw(chat_files=["a.ts"], other_files=["a.ts"])

    response = map_raw(chat_files=["vendor/lib.bundle.js"], other_files=["a.ts", "vendor/lib.bundle.js"])

    assert ".repomap/ignore" in response["hint"]


@pytest.mark.parametrize("text,expected", [
    ("src/app.ts", False),
    ("static/app.min.js", True),
    ("static/vendor.bundle.js", True),
    ("web/dist/main.js", True),
    ("third_party/x.ts", True),
    ("src/api.generated.ts", True),
    ("proto/user_pb2.py", True),
    ("src/distance/near.ts", False),
    ("src/vendored_names.ts", False),
])
def test_looks_generated_by_path_alone(text, expected):
    assert repomap_server.looks_generated(text) is expected


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


def hits(result):
    return [(r["file"], r["line"]) for r in result["results"]]


def test_search_definition_is_not_repeated_as_reference(tmp_path):
    (tmp_path / "a.ts").write_text("export function interleave() {}\n\n\n\n\n\n\n\n\n\ninterleave();\n")

    results = search(query="interleave", context_lines=0)["results"]

    assert [(r["line"], r["kind"]) for r in results] == [(1, "def"), (11, "ref")]


def test_search_merges_adjacent_hits_into_one_block(tmp_path):
    body = "\n".join(f"  const x{i} = interleave(a);" for i in range(3))
    padding = "\n".join(f"const v{i} = {i};" for i in range(30))
    (tmp_path / "a.ts").write_text(f"function f(a) {{\n{body}\n}}\n{padding}\nfunction g(a) {{ return interleave(a); }}\n")

    results = search(query="interleave", context_lines=1)["results"]

    assert [r["line"] for r in results] == [2, 36]
    assert results[0]["lines"] == [2, 3, 4]
    assert "lines" not in results[1]
    assert all(f"x{i}" in results[0]["context"] for i in range(3))


def test_search_exact_matches_come_before_substring_matches(tmp_path):
    (tmp_path / "a.py").write_text("def get_tags_raw():\n    pass\n\n\ndef get_tags():\n    pass\n")

    results = search(query="get_tags", context_lines=0)["results"]

    assert [r["name"] for r in results] == ["get_tags", "get_tags_raw"]
    assert [r["exact"] for r in results] == [True, False]


def test_search_exact_flag_is_left_out_when_all_hits_are_exact(tmp_path):
    (tmp_path / "a.py").write_text("def get_tags():\n    pass\n")

    assert "exact" not in search(query="get_tags")["results"][0]


def test_search_orders_references_by_file_and_line_with_tests_last(tmp_path):
    (tmp_path / "z.py").write_text("def use():\n    interleave()\n")
    (tmp_path / "a_test.py").write_text("def check():\n    interleave()\n")
    (tmp_path / "b.py").write_text("\n\n\n\n\n\n\n\ninterleave()\n\n\ninterleave()\n")
    (tmp_path / "lib.py").write_text("def interleave():\n    pass\n")

    results = search(query="interleave", context_lines=0)["results"]

    assert hits({"results": results}) == [("lib.py", 1), ("b.py", 9), ("b.py", 12), ("z.py", 2), ("a_test.py", 2)]


def test_search_uses_areas_toml_test_kinds(tmp_path):
    (tmp_path / ".repomap").mkdir()
    (tmp_path / ".repomap" / "areas.toml").write_text('[paths."checks"]\nkind = "test"\n')
    (tmp_path / "checks").mkdir()
    (tmp_path / "checks" / "a.py").write_text("def check():\n    interleave()\n")
    (tmp_path / "z.py").write_text("def use():\n    interleave()\n")

    assert hits(search(query="interleave", context_lines=0)) == [("z.py", 2), ("checks/a.py", 2)]


def test_search_groups_hits_by_file_and_strips_the_path_from_context(tmp_path):
    (tmp_path / "a.ts").write_text("export function interleave() {}\n" + "\n" * 10 + "interleave();\n")
    (tmp_path / "b.ts").write_text("interleave();\n")

    response = search_raw(query="interleave", context_lines=0)

    assert [f["file"] for f in response["results"]] == ["a.ts", "b.ts"]
    assert [h["line"] for h in response["results"][0]["hits"]] == [1, 12]
    assert all("file" not in h for f in response["results"] for h in f["hits"])
    assert all("a.ts" not in h["context"] and "b.ts" not in h["context"] for h in flat(response))
    assert "interleave" in response["results"][1]["hits"][0]["context"]


def test_search_files_are_ordered_by_their_best_hit(tmp_path):
    (tmp_path / "a.ts").write_text("interleave();\n")
    (tmp_path / "b.ts").write_text("export function interleave() {}\n" + "\n" * 10 + "interleave();\n")

    response = search_raw(query="interleave", context_lines=0)

    assert [(f["file"], [h["kind"] for h in f["hits"]]) for f in response["results"]] == [
        ("b.ts", ["def", "ref"]), ("a.ts", ["ref"])]


def test_search_counts_hits_cut_by_max_results(tmp_path):
    (tmp_path / "a.ts").write_text("export function interleave() {}\n" + "".join("\n" * 10 + "interleave();\n" for _ in range(4)))

    cut = search_raw(query="interleave", context_lines=0, max_results=2)
    full = search_raw(query="interleave", context_lines=0)

    assert len(flat(cut)) == 2 and cut["more_hits"] == 3
    assert "more_hits" not in full and len(flat(full)) == 5
