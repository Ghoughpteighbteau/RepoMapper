
import os
import subprocess

import pytest

from repomapper import repomap_class
from repomapper.config import IdentifierWeightConfig, RankingConfig
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
    rm.TAGS_CACHE[fname] = {"stamp": rm.get_file_stamp(fname), "extractor": repomap_class.extractor_fingerprint(), "data": []}

    assert rm.get_tags(fname, "a.ts") == []
    assert rm.get_tags(fname, "a.ts", force_refresh=True) == stale
    assert rm.get_tags(fname, "a.ts") == stale


def poison_cache(rm, fname, **overrides):
    entry = {"stamp": rm.get_file_stamp(fname), "extractor": repomap_class.extractor_fingerprint(), "data": []}
    rm.TAGS_CACHE[fname] = {**entry, **overrides}


def test_extractor_change_invalidates_cached_tags(tmp_path):
    (tmp_path / "a.ts").write_text("export function foo() {}\n")
    fname = str(tmp_path / "a.ts")
    rm = make_repomap(tmp_path)
    fresh = rm.get_tags(fname, "a.ts")
    poison_cache(rm, fname, extractor="old-extractor")

    assert rm.get_tags(fname, "a.ts") == fresh


def test_same_mtime_size_change_invalidates_cached_tags(tmp_path):
    path = tmp_path / "a.ts"
    path.write_text("export function foo() {}\n")
    fname = str(path)
    rm = make_repomap(tmp_path)
    poison_cache(rm, fname)
    st = os.stat(fname)
    path.write_text("export function foo() {}\nexport function bar() {}\n")
    os.utime(fname, ns=(st.st_atime_ns, st.st_mtime_ns))  # mtime-preserving edit

    assert {t.name for t in rm.get_tags(fname, "a.ts") if t.kind == "def"} == {"foo", "bar"}


def test_legacy_mtime_entries_are_reparsed(tmp_path):
    (tmp_path / "a.ts").write_text("export function foo() {}\n")
    fname = str(tmp_path / "a.ts")
    rm = make_repomap(tmp_path)
    rm.TAGS_CACHE[fname] = {"mtime": os.path.getmtime(fname), "data": []}

    assert rm.get_tags(fname, "a.ts") != []


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


def test_seeded_map_annotates_files_and_leaves_folder_notes_to_the_trunk(tmp_path):
    files = make_project(tmp_path, {**PROJECT, "old/legacy.ts": "export function oldHelper() { return helper(2); }\n"})
    (tmp_path / ".repomap").mkdir()
    (tmp_path / ".repomap" / "areas.toml").write_text(
        '[paths."old"]\nstatus = "legacy"\nnote = "folder note"\n'
        '[paths."core.ts"]\nstatus = "deprecated"\nreplaced_by = "used.ts"\nnote = "old helpers"\n'
    )
    map_string, _ = make_repomap(tmp_path).get_repo_map(chat_files=[files[3]], other_files=files[:3])

    assert "core.ts: [deprecated, replaced by used.ts] — old helpers\n" in map_string
    assert "old/legacy.ts: [legacy]\n" in map_string
    assert "legacy] — folder note\n" in map_string      # the trunk's directory line carries the folder's note...
    assert "old/legacy.ts: [legacy]\n" in map_string    # ...the file's own line only its status


def test_seeded_map_note_without_status_follows_the_colon(tmp_path):
    files = make_project(tmp_path, PROJECT)
    (tmp_path / ".repomap").mkdir()
    (tmp_path / ".repomap" / "areas.toml").write_text('[paths."used.ts"]\nnote = "calls helper"\n')
    map_string, _ = make_repomap(tmp_path).get_repo_map(chat_files=[files[1]], other_files=files)

    assert "used.ts: calls helper\n" in map_string


def test_recent_commits_raise_unseeded_rank_and_seeded_maps_ignore_them(tmp_path):
    files = make_project(tmp_path, PROJECT)
    plain = make_repomap(tmp_path).build_ranking([], files)
    assert plain.report.git_history.startswith("unavailable")

    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": os.environ["PATH"]}
    for args in (["init", "-q"], ["add", "alone.ts"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, env=env)
    tilted = make_repomap(tmp_path).build_ranking([], files)

    assert tilted.file_ranks["alone.ts"] > plain.file_ranks["alone.ts"]
    assert tilted.report.git_history.startswith("1 commit since")
    assert make_repomap(tmp_path).build_ranking([files[0]], files[1:]).report.git_history is None


def test_report_counts_parseable_files_apart_from_all_considered(tmp_path):
    fnames = make_project(tmp_path, {**PROJECT, "README.md": "# docs\n", "uv.lock": "x\n"})
    _out, report = make_repomap(tmp_path).get_repo_map([], fnames)
    assert report.total_files_considered == len(PROJECT) + 2
    assert report.parseable_files == len(PROJECT)


def test_names_only_link_files_of_the_same_language_family(tmp_path):
    files = make_project(tmp_path, {
        "codec.ts": "export function fromBase64(s: string) { return s; }\n",
        "Mail.java": "class Mail { String body() { return fromBase64(x); } }\n",
        "page.js": "fromBase64('x');\n",
    })
    graph = make_repomap(tmp_path).build_ranking([], files).graph

    assert graph.has_edge("page.js", "codec.ts")
    assert not graph.has_edge("Mail.java", "codec.ts")


def test_language_families_come_from_config(tmp_path):
    from repomapper.config import Config, LanguagesConfig
    files = make_project(tmp_path, {
        "codec.ts": "export function fromBase64(s: string) { return s; }\n",
        "page.js": "fromBase64('x');\n",
    })
    rm = make_repomap(tmp_path)
    rm.config = Config(languages=LanguagesConfig(families=[]))

    assert not rm.build_ranking([], files).graph.has_edge("page.js", "codec.ts")


def test_nested_helpers_are_not_outline_samples(tmp_path):
    files = make_project(tmp_path, {
        "walker.py": "def run_all(n):\n    def walk(x):\n        return x\n    return walk(n)\n\n"
                     "class Tree:\n    def visit(self):\n        return 1\n",
        "user.py": "import os\nfor p in os.walk('.'):\n    run_all(p)\nTree().visit()\n",
    })
    tags = make_repomap(tmp_path).get_tags(files[0], "walker.py")
    out, _ = make_repomap(tmp_path).get_outline(files, 10_000)

    assert {t.name for t in tags if t.kind == "def"} >= {"run_all", "walk", "Tree", "visit"}   # still ranked
    assert {t.name for t in tags if t.kind == "nested"} == {"walk"}
    samples = out.split("walker.py ")[1].split("\n")[0].split(": ")[1].split(", ")
    assert "walk" not in samples
    assert {"run_all", "visit"} <= set(samples)


def test_class_like_definitions_get_a_type_tag(tmp_path):
    files = make_project(tmp_path, {
        "shapes.ts": "export interface Shape { area(): number }\nexport type Id = string;\n"
                     "export enum Color { Red }\nexport class Circle { radius = 1; grow() {} }\n"
                     "export function make() { return 1 }\n",
    })
    tags = make_repomap(tmp_path).get_tags(files[0], "shapes.ts")
    assert {t.name for t in tags if t.kind == "type"} == {"Shape", "Id", "Color", "Circle"}
    assert {"area", "grow", "make"} <= {t.name for t in tags if t.kind == "def"}


def test_outline_samples_prefer_types_over_methods(tmp_path):
    files = make_project(tmp_path, {
        "shapes.ts": "export class Circle { grow() {} shrink() {} }\n",
        "user.ts": "const c = new Circle();\nc.grow();\nc.grow();\nc.grow();\nc.shrink();\nc.shrink();\n",
    })
    out, _ = make_repomap(tmp_path).get_outline(files, 10_000)
    samples = out.split("shapes.ts ")[1].split("\n")[0].split(": ")[1].split(", ")
    assert samples[0] == "Circle"


def test_outline_demotes_test_files_from_areas_globs(tmp_path):
    files = make_project(tmp_path, {
        "core.py": "def helper():\n    return 1\n",
        "core_test.py": "from core import helper\ndef test_helper():\n    assert helper()\n",
    })
    (tmp_path / ".repomap").mkdir()
    (tmp_path / ".repomap" / "areas.toml").write_text('[globs."*_test.*"]\nimportance = "low"\nkind = "test"\n')
    out, _ = make_repomap(tmp_path).get_outline(files, 10_000)

    assert "1 test" in out
    assert "test_helper" not in out and "Dependencies" not in out


def test_low_importance_scales_rank_after_pagerank(tmp_path):
    # hub.ts is referenced from many files, other.ts from one; low importance used to only shrink hub's seed,
    # which its incoming links swamped
    files = {"hub.ts": "export function hubHelper() {}\n", "other.ts": "export function otherHelper() {}\n"}
    files.update({f"page{i}.ts": "hubHelper();\n" for i in range(3)})
    files["main.ts"] = "otherHelper();\n"
    fnames = make_project(tmp_path, files)
    plain = make_repomap(tmp_path).build_ranking([], fnames)
    assert plain.file_ranks["hub.ts"] > plain.file_ranks["other.ts"]

    (tmp_path / ".repomap").mkdir()
    (tmp_path / ".repomap" / "areas.toml").write_text('[paths."hub.ts"]\nimportance = "low"\n')
    tilted = make_repomap(tmp_path).build_ranking([], fnames)

    assert tilted.file_ranks["hub.ts"] < tilted.file_ranks["other.ts"]
    assert tilted.ident_ranks[("hub.ts", "hubHelper")] < tilted.ident_ranks[("other.ts", "otherHelper")]


def test_core_importance_multiplies_the_pagerank_result(tmp_path):
    files = make_project(tmp_path, PROJECT)
    plain = make_repomap(tmp_path).build_ranking([], files).file_ranks
    (tmp_path / ".repomap").mkdir()
    (tmp_path / ".repomap" / "areas.toml").write_text('[paths."core.ts"]\nimportance = "core"\n')
    tilted = make_repomap(tmp_path).build_ranking([], files).file_ranks

    assert tilted["core.ts"] / tilted["alone.ts"] == pytest.approx(4.0 * plain["core.ts"] / plain["alone.ts"])
    assert sum(tilted.values()) == pytest.approx(1.0)


def test_seeded_ranking_ignores_importance(tmp_path):
    files = make_project(tmp_path, PROJECT)
    plain = make_repomap(tmp_path).build_ranking([files[1]], files).file_ranks
    (tmp_path / ".repomap").mkdir()
    (tmp_path / ".repomap" / "areas.toml").write_text('[paths."core.ts"]\nimportance = "low"\n')
    tilted = make_repomap(tmp_path).build_ranking([files[1]], files).file_ranks

    assert tilted == pytest.approx(plain)


def test_default_specific_identifier_weight_is_5():
    assert IdentifierWeightConfig().specific == 5.0


def test_outline_shows_directories_of_unparseable_files(tmp_path):
    files = make_project(tmp_path, {
        "core.py": "def helper():\n    return 1\n",
        "flyway/V1__a.sql": "create table a (id int);\n",
        "flyway/V2__b.sql": "create table b (id int);\n",
        "flyway/V3__c.sql": "create table c (id int);\n",
        ".github/workflows/a.yml": "a: 1\n", ".github/workflows/b.yml": "a: 1\n", ".github/workflows/c.yml": "a: 1\n",
    })
    (tmp_path / ".repomap").mkdir()
    (tmp_path / ".repomap" / "areas.toml").write_text('[paths."flyway"]\nnote = "Flyway migrations"\n')
    out, _ = make_repomap(tmp_path).get_outline(files, 10_000)

    assert "flyway/  3 files (sql 3) — Flyway migrations" in out
    assert "V1__a.sql" not in out and "github" not in out


def test_imports_are_tagged_for_js_and_ts_only(tmp_path):
    make_project(tmp_path, {"a.ts": "import { x } from './b';\nconst y = require('./c');\n", "a.py": "import b\n"})
    rm = make_repomap(tmp_path)

    assert [(t.name, t.line) for t in rm.get_tags(str(tmp_path / "a.ts"), "a.ts") if t.kind == "import"] == [
        ("./b", 1), ("./c", 2)]
    assert not [t for t in rm.get_tags(str(tmp_path / "a.py"), "a.py") if t.kind == "import"]


def test_relative_import_links_files_with_no_shared_names(tmp_path):
    files = make_project(tmp_path, {
        "app/main.ts": "import './setup';\nimport { onlyHere } from '../lib/util';\nexport const m = 1;\n",
        "lib/util.ts": "export const zz = 1;\n",
        "app/setup.ts": "export const qq = 2;\n",
    })
    graph = make_repomap(tmp_path).build_ranking([], files).graph

    assert graph.has_edge("app/main.ts", "lib/util.ts")
    assert graph.has_edge("app/main.ts", "app/setup.ts")
    assert not graph.has_edge("lib/util.ts", "app/main.ts")
    data = graph.get_edge_data("app/main.ts", "lib/util.ts")
    assert [d["weight"] for d in data.values()] == [RankingConfig().import_weight]


def test_workspace_package_import_follows_a_barrel_to_its_files(tmp_path):
    files = make_project(tmp_path, {
        "lib/types/package.json": '{"name": "@acme/types", "main": "./dist/index.js"}',
        "lib/types/src/index.ts": "export * from './user';\nexport * from './task';\n",
        "lib/types/src/user.ts": "export interface User { a: string }\n",
        "lib/types/src/task.ts": "export interface Task { b: string }\n",
        "ui/app.ts": "import { User } from '@acme/types';\nexport const q = 1;\n",
    })
    rm = make_repomap(tmp_path)
    mapped = [f for f in files if not f.endswith("index.ts")]   # barrels are auto-ignored, so they are not in the map
    graph = rm.build_ranking([], mapped).graph

    assert graph.has_edge("ui/app.ts", "lib/types/src/user.ts")
    assert graph.has_edge("ui/app.ts", "lib/types/src/task.ts")
    assert not graph.has_edge("ui/app.ts", "lib/types/src/index.ts")


def test_import_weight_is_configurable_and_zero_disables_it(tmp_path):
    from repomapper.config import Config
    files = make_project(tmp_path, {"a.ts": "import './b';\nexport const q = 1;\n", "b.ts": "export const w = 1;\n"})
    rm = make_repomap(tmp_path)
    rm.config = Config(ranking=RankingConfig(import_weight=7.0))
    assert [d["weight"] for d in rm.build_ranking([], files).graph.get_edge_data("a.ts", "b.ts").values()] == [7.0]
    rm.config = Config(ranking=RankingConfig(import_weight=0.0))
    assert not rm.build_ranking([], files).graph.has_edge("a.ts", "b.ts")


def test_imports_raise_the_rank_of_what_a_chat_file_imports(tmp_path):
    files = make_project(tmp_path, {"a.ts": "import './b';\nexport const q = 1;\n", "b.ts": "export const w = 1;\n",
                                    "c.ts": "export const e = 1;\n"})
    ranking = make_repomap(tmp_path).build_ranking([files[0]], files[1:])

    assert ranking.file_ranks["b.ts"] > ranking.file_ranks["c.ts"]
    assert not ranking.ident_ranks.get(("b.ts", None))   # import edges carry no identifier rank


def test_outline_shows_import_direction_and_drops_shared_name_edges_between_js_files(tmp_path):
    files = make_project(tmp_path, {
        "ui/src/page.ts": "import { Model } from '../../lib/src/model';\nexport function renderPage() { return Model; }\n",
        "lib/src/model.ts": "export class Model {}\nexport function renderPage2() { return renderPage(); }\n",
        "api/src/route.ts": "export function renderPage() {}\nexport function handle() {}\n",
        "ui/src/other.ts": "export function other() {}\n",
        "lib/src/other.ts": "export function other2() {}\n",
        "api/src/other.ts": "export function other3() { return handle(); }\n",
    })
    out, _ = make_repomap(tmp_path).get_outline(files, 10_000)
    deps = out.split("Dependencies")[1]

    assert "ui/src/page.ts -> lib/src/model.ts" in deps
    assert "<->" not in deps and "api/src" not in deps


def test_outline_lists_docs_matching_doc_globs(tmp_path):
    files = make_project(tmp_path, {**PROJECT, "README.md": "# x\n", "mail.README.md": "# m\n", "NOTES.txt": "n\n"})
    out, _ = make_repomap(tmp_path).get_outline(files, 10_000)
    assert "[README.md, mail.README.md]" in out

    repomap = make_repomap(tmp_path)
    repomap.config.outline.doc_globs = ["NOTES.*"]
    out, _ = repomap.get_outline(files, 10_000)
    assert "[NOTES.txt]" in out and "README" not in out


STEPS_PROJECT = {
    "lib/steps/src/task.ts": "export class Task { run() { return 1; } }\nexport function makeTask() { return new Task(); }\n",
    "lib/steps/src/queue.ts": "export class Queue { push() { return 1; } }\n",
    "red/src/node.ts": "import { Task } from '../../lib/steps/src/task';\nexport function useTask() { return new Task(); }\n",
    "api/src/route.ts": "import { Task } from '../../lib/steps/src/task';\nexport function route() { return new Task(); }\n",
    "docs/guide/a.ts": "export function guideA() { return 1; }\n",
    "docs/guide/b.ts": "export function guideB() { return 2; }\n",
}


def test_seeded_map_is_a_trunk_with_signatures_inside_it(tmp_path):
    files = make_project(tmp_path, STEPS_PROJECT)
    map_string, _ = make_repomap(tmp_path, 4096).get_repo_map(chat_files=[files[0]], other_files=files[1:])

    assert "Zoomed map" in map_string and "Rank value" in map_string
    lines = map_string.splitlines()
    assert any(line.strip() == "lib/steps/src/task.ts:" for line in lines)
    assert "docs/guide/a.ts:" not in map_string                                           # no signatures outside the focus
    assert "red/src/  1 file" in map_string                                               # one-line siblings
    deps = map_string.split("Dependencies (strongest")[1]
    assert "red" in deps and "api" in deps and "lib/steps" in deps                        # used by red and api
    assert "red/src/node.ts -> lib/steps/src/" in deps and "api/src/route.ts -> lib/steps/src/" in deps   # import direction


def test_directory_in_mentioned_files_opens_to_signatures(tmp_path):
    files = make_project(tmp_path, STEPS_PROJECT)
    map_string, _ = make_repomap(tmp_path, 4096).get_repo_map(chat_files=[], other_files=files,
                                                              mentioned_fnames={"lib/steps/src"})

    assert "lib/steps/src/task.ts:" in map_string and "lib/steps/src/queue.ts:" in map_string
    assert "class Queue" in map_string
    assert "docs/guide/a.ts" not in map_string


def test_directory_in_mentioned_files_may_be_absolute_or_have_a_trailing_slash(tmp_path):
    files = make_project(tmp_path, STEPS_PROJECT)
    for mention in (str(tmp_path / "lib/steps/src"), "lib/steps/src/"):
        map_string, _ = make_repomap(tmp_path, 4096).get_repo_map(chat_files=[], other_files=files,
                                                                  mentioned_fnames={mention})
        assert "lib/steps/src/queue.ts:" in map_string


@pytest.mark.parametrize("tokens", [200, 400, 800, 2048])
def test_seeded_map_stays_within_the_budget(tmp_path, tokens):
    files = make_project(tmp_path, STEPS_PROJECT)
    map_string, _ = make_repomap(tmp_path, tokens).get_repo_map(chat_files=[files[0]], other_files=files[1:])

    assert count_tokens(map_string, "gpt-4") <= tokens


def test_seeded_trunk_share_is_a_config_key(tmp_path):
    from repomapper.config import Config, OutlineConfig
    files = make_project(tmp_path, STEPS_PROJECT)
    rm = make_repomap(tmp_path, 4096)
    rm.config = Config(outline=OutlineConfig(seeded_trunk_share=0.9))
    wide, _ = rm.get_repo_map(chat_files=[files[0]], other_files=files[1:])
    narrow, _ = make_repomap(tmp_path, 4096).get_repo_map(chat_files=[files[0]], other_files=files[1:])

    assert wide != narrow or count_tokens(wide, "gpt-4") < 4096


def test_mentioned_identifier_opens_the_directory_that_defines_it(tmp_path):
    files = make_project(tmp_path, STEPS_PROJECT)
    map_string, _ = make_repomap(tmp_path, 4096).get_repo_map(chat_files=[], other_files=files,
                                                              mentioned_idents={"makeTask"})

    assert "lib/steps/src/task.ts:" in map_string and "lib/steps/src/queue.ts:" in map_string
