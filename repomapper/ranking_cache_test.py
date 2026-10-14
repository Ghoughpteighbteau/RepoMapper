import asyncio
import os
import subprocess
import time

import pytest

from repomapper import ranking_cache, repomap_class, repomap_server
from repomapper.config import Config, MapConfig, RankingConfig
from repomapper.repomap_class import RepoMap
from repomapper.utils import count_tokens, read_text

real_time = time.time
SILENT = {"info": lambda *a: None, "warning": lambda *a: None, "error": lambda *a: None}

PROJECT = {
    "core.ts": "export function helper(x: number): number { return x; }\n",
    "used.ts": "import { helper } from './core';\nexport function caller() { return helper(1); }\n",
    "alone.ts": "export function unrelated() { return 2; }\n",
    "pkg/tool.py": "def make_tool():\n    return 1\n\n\ndef use_tool():\n    return make_tool()\n",
}
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": os.environ["PATH"]}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(repomap_class, "TAGS_CACHE_DIR", str(tmp_path / ".cache"))
    ranking_cache.memo_clear()
    yield
    ranking_cache.memo_clear()


@pytest.fixture
def computes(monkeypatch):
    """Counts the builds that missed every cache."""
    calls = []
    original = RepoMap.compute_ranking

    def counting(self, *args, **kwargs):
        calls.append(1)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(RepoMap, "compute_ranking", counting)
    return calls


def write(root, files):
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return [str(root / name) for name in files]


def make_repomap(root, config=None, areas=None):
    return RepoMap(root=str(root), token_counter_func=lambda text: count_tokens(text, "gpt-4"),
                   file_reader_func=read_text, output_handler_funcs=SILENT, config=config, areas=areas)


def cache_file(root):
    return root / ".repomap" / "cache" / "ranking.v1"


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, env=GIT_ENV)


def from_disk():
    """Forget the in-process memo, so the next build can only hit the file."""
    ranking_cache.memo_clear()


def same_ranking(a, b):
    assert a.file_ranks == b.file_ranks
    assert a.ident_ranks == b.ident_ranks
    assert a.definitions == b.definitions and a.nested == b.nested and a.types == b.types
    assert a.included == b.included
    assert a.report == b.report


def test_second_build_loads_the_stored_ranking_and_matches_a_fresh_one(tmp_path, computes):
    files = write(tmp_path, PROJECT)
    fresh = make_repomap(tmp_path).build_ranking([], files)
    assert len(computes) == 1 and cache_file(tmp_path).is_file()

    from_disk()
    loaded = make_repomap(tmp_path).build_ranking([], files)

    assert len(computes) == 1
    same_ranking(fresh, loaded)


def test_outline_from_a_stored_ranking_is_identical_to_a_fresh_one(tmp_path, computes):
    files = write(tmp_path, {**PROJECT, "app/a.ts": "import '../core';\nexport const a = helper(1);\n"})
    fresh_text, fresh_report = make_repomap(tmp_path, Config(map=MapConfig(ranking_cache=False))).get_outline(files, 10_000)
    assert not cache_file(tmp_path).exists()

    first_text, _ = make_repomap(tmp_path).get_outline(files, 10_000)
    from_disk()
    disk_text, disk_report = make_repomap(tmp_path).get_outline(files, 10_000)
    memo_text, _ = make_repomap(tmp_path).get_outline(files, 10_000)

    assert len(computes) == 2   # the uncached run and the first cached one
    assert fresh_text == first_text == disk_text == memo_text
    assert disk_report == fresh_report


def test_stored_ranking_is_reused_by_a_new_instance_through_the_memo(tmp_path, computes):
    files = write(tmp_path, PROJECT)
    make_repomap(tmp_path).build_ranking([], files)
    cache_file(tmp_path).unlink()

    make_repomap(tmp_path).build_ranking([], files)

    assert len(computes) == 1


def test_a_changed_file_invalidates(tmp_path, computes):
    files = write(tmp_path, PROJECT)
    make_repomap(tmp_path).build_ranking([], files)
    from_disk()
    (tmp_path / "alone.ts").write_text("export function unrelated() { return 3; }\nexport function more() {}\n")

    ranking = make_repomap(tmp_path).build_ranking([], files)

    assert len(computes) == 2
    assert "more" in ranking.definitions["alone.ts"]


def test_a_changed_file_invalidates_the_memo_too(tmp_path, computes):
    files = write(tmp_path, PROJECT)
    make_repomap(tmp_path).build_ranking([], files)
    make_repomap(tmp_path).build_ranking([], files)
    assert len(computes) == 1
    (tmp_path / "alone.ts").write_text("export function unrelated() { return 3; }\nexport function more() {}\n")

    assert "more" in make_repomap(tmp_path).build_ranking([], files).definitions["alone.ts"]
    assert len(computes) == 2


def test_a_new_or_removed_file_invalidates(tmp_path, computes):
    files = write(tmp_path, PROJECT)
    make_repomap(tmp_path).build_ranking([], files)
    from_disk()
    added = write(tmp_path, {"fresh.ts": "export function brandNew() { return helper(2); }\n"})

    ranking = make_repomap(tmp_path).build_ranking([], files + added)
    assert len(computes) == 2 and "fresh.ts" in ranking.definitions

    from_disk()
    ranking = make_repomap(tmp_path).build_ranking([], files)
    assert len(computes) == 3 and "fresh.ts" not in ranking.definitions


def test_changed_ranking_settings_invalidate(tmp_path, computes):
    files = write(tmp_path, PROJECT)
    make_repomap(tmp_path).build_ranking([], files)
    from_disk()

    make_repomap(tmp_path, Config(ranking=RankingConfig(import_weight=3.0))).build_ranking([], files)
    assert len(computes) == 2
    make_repomap(tmp_path, Config(ranking=RankingConfig(import_weight=3.0))).build_ranking([], files)
    assert len(computes) == 2


def test_changed_shared_name_edge_weight_invalidates(tmp_path, computes):
    from repomapper.config import OutlineConfig
    files = write(tmp_path, PROJECT)
    make_repomap(tmp_path).build_ranking([], files)
    from_disk()

    make_repomap(tmp_path, Config(outline=OutlineConfig(shared_name_edge_weight=1.0))).build_ranking([], files)

    assert len(computes) == 2


def test_config_toml_edit_invalidates(tmp_path, computes):
    files = write(tmp_path, PROJECT)
    make_repomap(tmp_path).build_ranking([], files)
    from_disk()
    write(tmp_path, {".repomap/config.toml": "[ranking]\npagerank_alpha = 0.5\n"})

    make_repomap(tmp_path).build_ranking([], files)

    assert len(computes) == 2


def test_areas_edit_invalidates_and_changes_the_ranks(tmp_path, computes):
    files = write(tmp_path, PROJECT)
    plain = make_repomap(tmp_path).build_ranking([], files)
    from_disk()
    write(tmp_path, {".repomap/areas.toml": '[paths."alone.ts"]\nimportance = "core"\n'})

    tilted = make_repomap(tmp_path).build_ranking([], files)

    assert len(computes) == 2
    assert tilted.file_ranks["alone.ts"] > plain.file_ranks["alone.ts"]
    from_disk()
    same_ranking(tilted, make_repomap(tmp_path).build_ranking([], files))
    assert len(computes) == 2


def test_new_head_invalidates(tmp_path, computes):
    files = write(tmp_path, PROJECT)
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "alone.ts")
    git(tmp_path, "commit", "-q", "-m", "one")
    before = make_repomap(tmp_path).build_ranking([], files)
    from_disk()
    assert make_repomap(tmp_path).build_ranking([], files).report.git_history == before.report.git_history
    assert len(computes) == 1

    git(tmp_path, "add", "core.ts")
    git(tmp_path, "commit", "-q", "-m", "two")
    after = make_repomap(tmp_path).build_ranking([], files)

    assert len(computes) == 2
    assert after.report.git_history.startswith("2 commits since")


def test_a_new_day_invalidates(tmp_path, computes, monkeypatch):
    files = write(tmp_path, PROJECT)
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "alone.ts")
    git(tmp_path, "commit", "-q", "-m", "one")
    make_repomap(tmp_path).build_ranking([], files)
    from_disk()
    make_repomap(tmp_path).build_ranking([], files)
    assert len(computes) == 1

    monkeypatch.setattr(time, "time", lambda: real_time() + 2 * 86400)
    from_disk()
    make_repomap(tmp_path).build_ranking([], files)

    assert len(computes) == 2



BARREL_PROJECT = {
    "lib/types/package.json": '{"name": "@acme/types", "main": "./dist/index.js"}',
    "lib/types/src/user.ts": "export interface User { a: string }\n",
    "lib/types/src/task.ts": "export interface Task { b: string }\n",
    "ui/app.ts": "import { User } from '@acme/types';\nexport const q = 1;\n",
}


def test_an_unlisted_barrel_that_import_resolution_reads_invalidates(tmp_path, computes):
    files = write(tmp_path, BARREL_PROJECT)
    barrel = tmp_path / "lib/types/src/index.ts"
    barrel.write_text("export * from './user';\n")   # not in the map, as auto-ignored barrels are
    first = make_repomap(tmp_path).build_ranking([], files)
    assert first.graph.has_edge("ui/app.ts", "lib/types/src/user.ts")
    assert not first.graph.has_edge("ui/app.ts", "lib/types/src/task.ts")
    from_disk()
    assert len(ranking_cache.load(str(cache_file(tmp_path)), repomap_key(tmp_path, files))[1]) > 0

    barrel.write_text("export * from './user';\nexport * from './task';\n")
    second = make_repomap(tmp_path).build_ranking([], files)

    assert len(computes) == 2
    assert second.pair_edges is not None
    from_disk()
    assert make_repomap(tmp_path).build_ranking([], files).pair_edges.imports.src.size == 2
    assert len(computes) == 2


def test_an_unlisted_barrel_appearing_where_resolution_found_nothing_invalidates(tmp_path, computes):
    files = write(tmp_path, BARREL_PROJECT)
    make_repomap(tmp_path).build_ranking([], files)   # no src/index.ts yet: the probe found nothing
    from_disk()
    (tmp_path / "lib/types/src/index.ts").write_text("export * from './user';\n")

    make_repomap(tmp_path).build_ranking([], files)

    assert len(computes) == 2
    from_disk()
    imports = make_repomap(tmp_path).build_ranking([], files).pair_edges.imports
    assert len(imports.src) == 1 and len(computes) == 2


def repomap_key(root, files):
    rm = make_repomap(root)
    return rm.ranking_key([(f, ranking_cache.stamp_of(f)) for f in set(files)])


@pytest.mark.parametrize("damage", [
    lambda data: b"",
    lambda data: data[:len(data) // 2],
    lambda data: data[:data.index(b"\n") + 1] + b"not a pickle",
    lambda data: b"garbage\n" + data[data.index(b"\n") + 1:],
    lambda data: data.replace(b"repomap-ranking v1", b"repomap-ranking v9", 1),
    lambda data: data[:-3] + b"\x00\x00\x00",
])
def test_a_damaged_file_is_recomputed_and_rewritten(tmp_path, computes, damage):
    files = write(tmp_path, PROJECT)
    fresh = make_repomap(tmp_path).build_ranking([], files)
    path = cache_file(tmp_path)
    path.write_bytes(damage(path.read_bytes()))
    from_disk()

    recomputed = make_repomap(tmp_path).build_ranking([], files)

    assert len(computes) == 2
    same_ranking(fresh, recomputed)
    from_disk()
    make_repomap(tmp_path).build_ranking([], files)
    assert len(computes) == 2


def test_a_payload_of_the_wrong_shape_is_a_miss(tmp_path):
    import pickle
    import zlib
    path = tmp_path / "ranking.v1"
    path.write_bytes(ranking_cache.header("k") + zlib.compress(pickle.dumps({"nodes": []})))

    assert ranking_cache.load(str(path), "k") is None
    assert ranking_cache.load(str(tmp_path / "missing"), "k") is None


def test_unwritable_cache_location_is_silent(tmp_path, computes):
    files = write(tmp_path, PROJECT)
    (tmp_path / ".repomap").write_text("a file where the directory should be")

    ranking = make_repomap(tmp_path).build_ranking([], files)

    assert ranking.file_ranks


def test_writes_leave_no_temp_files_and_replace_atomically(tmp_path, computes, monkeypatch):
    files = write(tmp_path, PROJECT)
    replaced = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda src, dst: (replaced.append((src, dst)), real_replace(src, dst))[1])

    make_repomap(tmp_path).build_ranking([], files)

    assert str(cache_file(tmp_path)) in [dst for _src, dst in replaced]   # and the base graph's file
    assert all(src != dst for src, dst in replaced)
    assert not [p for p in cache_file(tmp_path).parent.iterdir() if p.name.endswith(".tmp")]


def test_a_failed_write_leaves_the_old_file_and_no_temp_file(tmp_path, computes, monkeypatch):
    files = write(tmp_path, PROJECT)
    make_repomap(tmp_path).build_ranking([], files)
    old = cache_file(tmp_path).read_bytes()
    (tmp_path / "alone.ts").write_text("export function unrelated() { return 3; }\nexport function more() {}\n")

    def broken_replace(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", broken_replace)
    make_repomap(tmp_path).build_ranking([], files)

    assert cache_file(tmp_path).read_bytes() == old
    assert not [p for p in cache_file(tmp_path).parent.iterdir() if p.name.endswith(".tmp")]


def test_the_cache_can_be_switched_off(tmp_path, computes):
    files = write(tmp_path, PROJECT)
    config = Config(map=MapConfig(ranking_cache=False))

    make_repomap(tmp_path, config).build_ranking([], files)
    make_repomap(tmp_path, config).build_ranking([], files)

    assert len(computes) == 2 and not cache_file(tmp_path).exists()


def test_seeded_rankings_are_never_stored_or_reused(tmp_path, computes):
    files = write(tmp_path, PROJECT)
    make_repomap(tmp_path).build_ranking([], files)
    from_disk()
    make_repomap(tmp_path).build_ranking([files[0]], files[1:])
    make_repomap(tmp_path).build_ranking([], files, mentioned_idents={"helper"})
    make_repomap(tmp_path).build_ranking([], files, mentioned_fnames={"used.ts"})

    assert len(computes) == 4


def test_rankings_with_missing_files_or_parse_errors_are_not_stored(tmp_path, computes):
    files = write(tmp_path, PROJECT)
    ranking = make_repomap(tmp_path).build_ranking([], files + [str(tmp_path / "gone.ts")])
    assert ranking.report.excluded and not cache_file(tmp_path).exists()

    rm = make_repomap(tmp_path)
    original = RepoMap.get_tags_raw

    def failing(self, fname, rel_fname):
        if rel_fname == "alone.ts":
            self.record_parse_error(fname, "boom")
            return []
        return original(self, fname, rel_fname)
    RepoMap.get_tags_raw = failing
    try:
        ranking = rm.build_ranking([], files, force_refresh=True)
    finally:
        RepoMap.get_tags_raw = original
    assert ranking.report.parse_errors and not cache_file(tmp_path).exists()


def test_memo_keeps_a_few_roots_and_drops_the_least_recently_used(tmp_path):
    for i in range(ranking_cache.MEMO_ROOTS + 1):
        ranking_cache.memo_put(f"/root{i}", "key", object(), [])
        if i == 1:
            ranking_cache.memo_get("/root0", "key")   # touched, so /root1 is the oldest

    assert ranking_cache.memo_get("/root1", "key") is None
    assert ranking_cache.memo_get("/root0", "key") is not None
    assert ranking_cache.memo_get("/root0", "other key") is None


def test_the_hit_returns_a_report_the_caller_may_change(tmp_path, computes):
    files = write(tmp_path, PROJECT)
    first = make_repomap(tmp_path).build_ranking([], files)
    first.report.excluded["x"] = "changed by the caller"

    second = make_repomap(tmp_path).build_ranking([], files)

    assert not second.report.excluded


def test_cache_dir_is_git_ignored_by_the_repomap_gitignore(tmp_path, computes):
    from repomapper.repomap_dir import init_repomap_dir
    files = write(tmp_path, PROJECT)
    init_repomap_dir(str(tmp_path))
    make_repomap(tmp_path).build_ranking([], files)

    assert cache_file(tmp_path).is_file()
    assert "cache/" in (tmp_path / ".repomap" / ".gitignore").read_text().splitlines()


# --- MCP server: repeated calls in one process ---

def call_repo_map():
    fn = getattr(repomap_server.repo_map, "fn", repomap_server.repo_map)
    return asyncio.run(fn())


@pytest.fixture
def server_project(tmp_path, monkeypatch):
    monkeypatch.setattr(repomap_server, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(repomap_server, "CREATED_FILES", [])
    return tmp_path


def test_repeat_server_calls_reuse_the_ranking_until_a_file_changes(server_project, computes):
    write(server_project, PROJECT)
    first = call_repo_map()
    cache_file(server_project).unlink()   # so only the in-process memo can answer
    second = call_repo_map()

    assert len(computes) == 1
    assert second["map"] == first["map"] and second["report"] == first["report"]

    (server_project / "alone.ts").write_text("export function unrelated() { return 3; }\nexport function addedLater() {}\n")
    third = call_repo_map()

    assert len(computes) == 2
    assert "addedLater" in third["map"] or third["map"] != first["map"]


def test_server_sees_a_new_file_on_the_next_call(server_project, computes):
    write(server_project, PROJECT)
    call_repo_map()
    write(server_project, {"extra/new_module.py": "def brand_new_function():\n    return 1\n"})

    response = call_repo_map()

    assert len(computes) == 2
    assert response["report"]["total_files_considered"] == len(PROJECT) + 1
