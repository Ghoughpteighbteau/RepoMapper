import pytest

from repomapper import repomap_class
from repomapper.repomap_class import RepoMap
from repomapper.utils import count_tokens, read_text


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    # TAGS_CACHE_DIR is fixed to the process cwd at import time; keep tests out of it.
    monkeypatch.setattr(repomap_class, "TAGS_CACHE_DIR", str(tmp_path / ".cache"))


def make_project(root, files):
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return [str(root / name) for name in files]


def make_repomap(root, map_tokens=1024):
    silent = {"info": lambda *a: None, "warning": lambda *a: None, "error": lambda *a: None}
    return RepoMap(
        map_tokens=map_tokens,
        root=str(root),
        token_counter_func=lambda text: count_tokens(text, "gpt-4"),
        file_reader_func=read_text,
        output_handler_funcs=silent,
    )


PROJECT = {
    "core.ts": "export function helper(x: number): number { return x; }\n",
    "used.ts": "import { helper } from './core';\nexport function caller() { return helper(1); }\n",
    "alone.ts": "export function unrelated() { return 2; }\n",
}


@pytest.mark.parametrize("name,source,expected", [
    ("a.py", "def foo():\n    pass\n\nclass Bar:\n    pass\n", {"foo", "Bar"}),
    ("a.ts", "export function foo() {}\nexport class Bar {}\n", {"foo", "Bar"}),
])
def test_finds_definitions(tmp_path, name, source, expected):
    (tmp_path / name).write_text(source)
    tags = make_repomap(tmp_path).get_tags(str(tmp_path / name), name)

    assert {t.name for t in tags if t.kind == "def"} >= expected


def test_map_includes_definitions(tmp_path):
    files = make_project(tmp_path, PROJECT)
    map_string, report = make_repomap(tmp_path).get_repo_map(chat_files=[], other_files=files)

    assert report.definition_matches > 0
    assert "helper" in map_string


def test_references_are_found(tmp_path):
    files = make_project(tmp_path, PROJECT)
    _, report = make_repomap(tmp_path).get_repo_map(chat_files=[], other_files=files)

    assert report.reference_matches > 0


def test_chat_file_raises_rank_of_files_it_references(tmp_path):
    files = make_project(tmp_path, PROJECT)
    chat = str(tmp_path / "used.ts")
    others = [f for f in files if f != chat]
    ranked, _ = make_repomap(tmp_path).get_ranked_tags(
        chat_fnames=[chat], other_fnames=others, mentioned_fnames=set(), mentioned_idents=set(),
    )
    rank = {tag.rel_fname: r for r, tag in ranked}

    assert rank["core.ts"] > rank["alone.ts"]


def test_backfilled_refs_have_line_numbers(tmp_path):
    (tmp_path / "a.ts").write_text("export function foo() {}\n\nfoo();\n")
    tags = make_repomap(tmp_path).get_tags(str(tmp_path / "a.ts"), "a.ts")

    assert [t.line for t in tags if t.kind == "ref" and t.name == "foo"] == [1, 3]


def test_skips_minified_files(tmp_path):
    body = ";".join(f"function f{i}(a){{return a}}" for i in range(200))
    (tmp_path / "lib-min.js").write_text(body + "\n")

    assert make_repomap(tmp_path).get_tags(str(tmp_path / "lib-min.js"), "lib-min.js") == []


def test_finds_call_refs_in_files_with_type_refs(tmp_path):
    source = "import { interleave } from './u';\nexport function run(x: Thing) {\n  return interleave(x);\n}\n"
    (tmp_path / "a.ts").write_text(source)
    tags = make_repomap(tmp_path).get_tags(str(tmp_path / "a.ts"), "a.ts")
    refs = {(t.name, t.line) for t in tags if t.kind == "ref"}

    assert ("Thing", 2) in refs
    assert ("interleave", 3) in refs


def test_specific_identifiers_outrank_common_ones(tmp_path):
    files = {f"common{i}.ts": "export function create() {}\n" for i in range(6)}
    files["bundle.js"] = "function create() {}\n"
    files["core.ts"] = "export function fetchUserAccount() {}\n"
    files["chat.ts"] = "fetchUserAccount();\ncreate();\n"
    paths = make_project(tmp_path, files)
    chat = str(tmp_path / "chat.ts")
    ranked, _ = make_repomap(tmp_path).get_ranked_tags(
        chat_fnames=[chat], other_fnames=[p for p in paths if p != chat],
    )
    rank = {tag.rel_fname: r for r, tag in ranked}

    assert rank["core.ts"] > rank["bundle.js"]
