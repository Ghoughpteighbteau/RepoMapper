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
