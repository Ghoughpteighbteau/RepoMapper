
import pytest

from repomapper import repomap_class
from repomapper.config import IdentifierWeightConfig
from repomapper.repomap_class import RepoMap, identifier_weight
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


def test_force_refresh_reparses_cached_tags(tmp_path):
    (tmp_path / "a.ts").write_text("export function foo() {}\n")
    fname = str(tmp_path / "a.ts")
    rm = make_repomap(tmp_path)
    stale = rm.get_tags(fname, "a.ts")
    rm.TAGS_CACHE[fname] = {"mtime": rm.get_mtime(fname), "data": []}

    assert rm.get_tags(fname, "a.ts") == []
    assert rm.get_tags(fname, "a.ts", force_refresh=True) == stale
    assert rm.get_tags(fname, "a.ts") == stale


def test_tags_cache_goes_in_repomap_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(repomap_class, "TAGS_CACHE_DIR", repomap_class.TAGS_CACHE_DIR_DEFAULT)

    assert make_repomap(tmp_path).tags_cache_dir() == tmp_path.resolve() / ".repomap" / "cache" / "tags.v1"


def test_mentioned_file_raises_rank_of_files_it_references(tmp_path):
    files = make_project(tmp_path, PROJECT)
    ranked, _ = make_repomap(tmp_path).get_ranked_tags(
        chat_fnames=[], other_fnames=files, mentioned_fnames={"used.ts"},
    )
    rank = {tag.rel_fname: r for r, tag in ranked}

    assert rank["core.ts"] > rank["alone.ts"]


def test_parse_errors_are_reported_and_not_cached(tmp_path, monkeypatch):
    files = make_project(tmp_path, PROJECT)
    rm = make_repomap(tmp_path)
    import tree_sitter
    monkeypatch.setattr(tree_sitter, "Query", lambda *a: 1 / 0)

    _, report = rm.get_ranked_tags(chat_fnames=[], other_fnames=files)

    assert set(report.parse_errors) == set(files)
    assert "division by zero" in report.parse_errors[files[0]]
    assert rm.TAGS_CACHE.get(files[0]) is None


def test_unseeded_map_ranks_referenced_files_higher(tmp_path):
    files = make_project(tmp_path, PROJECT)
    ranked, _ = make_repomap(tmp_path).get_ranked_tags(chat_fnames=[], other_fnames=files)
    rank = {tag.rel_fname: r for r, tag in ranked}

    assert rank["core.ts"] > rank["alone.ts"]


def test_unreferenced_defs_in_popular_file_rank_below_used_defs_elsewhere(tmp_path):
    helpers = "".join(f"export function unusedHelper{i}() {{}}\n" for i in range(20))
    files = {
        "utils.ts": "export function popularHelper() {}\n" + helpers,
        "service.ts": "export function runService() {}\n",
    }
    files.update({f"page{i}.ts": "popularHelper();\n" for i in range(5)})
    files["main.ts"] = "runService();\n"
    ranked, _ = make_repomap(tmp_path).get_ranked_tags(
        chat_fnames=[], other_fnames=make_project(tmp_path, files),
    )
    rank = {tag.name: r for r, tag in ranked}

    assert rank["popularHelper"] > rank["runService"] > rank["unusedHelper0"]


def test_identifier_weight_uses_configured_multipliers():
    weights = IdentifierWeightConfig(specific=3.0, common=0.5)
    assert identifier_weight("get_user_name", 6, set(), weights) == 1.5


def test_core_importance_raises_unseeded_rank(tmp_path):
    files = make_project(tmp_path, PROJECT)
    plain = make_repomap(tmp_path).build_ranking([], files).file_ranks
    (tmp_path / ".repomap").mkdir()
    (tmp_path / ".repomap" / "areas.toml").write_text('[paths."alone.ts"]\nimportance = "core"\n')
    tilted = make_repomap(tmp_path).build_ranking([], files).file_ranks

    assert tilted["alone.ts"] > plain["alone.ts"]


def test_seeded_map_starts_with_notes_for_shown_areas(tmp_path):
    files = make_project(tmp_path, {**PROJECT, "old/legacy.ts": "export function oldHelper() { return helper(2); }\n"})
    (tmp_path / ".repomap").mkdir()
    (tmp_path / ".repomap" / "areas.toml").write_text(
        '[paths."old"]\nstatus = "deprecated"\nreplaced_by = "core.ts"\n[paths."alone.ts"]\nnote = "unused"\n'
    )
    map_string, _ = make_repomap(tmp_path).get_repo_map(chat_files=[files[3]], other_files=files[:3])

    header = map_string.split("\n\n")[0].splitlines()
    assert header[0] == "Area notes from .repomap/areas.toml:"
    assert "  old/ [deprecated, replaced by core.ts]" in header
    assert "  alone.ts — unused" in header
    assert "Don't copy patterns from deprecated areas" in map_string
