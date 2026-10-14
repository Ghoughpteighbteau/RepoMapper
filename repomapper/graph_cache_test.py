"""The base graph cache (.repomap/cache/graph.v1): seeded rankings reuse the seed independent graph."""
import os
import pickle
import time
import zlib

import pytest

from repomapper import ranking_cache, repomap_class
from repomapper.config import Config, MapConfig, RankingConfig
from repomapper.ranking_cache_test import BARREL_PROJECT, PROJECT, make_repomap, write
from repomapper.repomap_class import RepoMap

PROJECT_FILES = {
    **PROJECT,
    "app/a.ts": "import { helper } from '../core';\nexport const a = helper(1);\nexport function more() { return a; }\n",
    "app/b.ts": "import { a } from './a';\nexport function b() { return a + helper(2); }\n",
    "pkg/other.py": "from tool import make_tool\n\n\ndef run():\n    return make_tool() + use_tool()\n",
}

SEEDS = {
    "chat": dict(chat=["core.ts"]),
    "dir": dict(mentioned=["app"]),
    "file": dict(mentioned=["used.ts"]),
    "idents": dict(idents={"helper", "make_tool"}),
    "chat and mentioned": dict(chat=["used.ts"], mentioned=["pkg"], idents={"more"}),
    "none": dict(),
}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(repomap_class, "TAGS_CACHE_DIR", str(tmp_path / ".cache"))
    ranking_cache.memo_clear()
    yield
    ranking_cache.memo_clear()


@pytest.fixture
def builds(monkeypatch):
    """Counts the base graphs that were built (missed the memo and the file)."""
    calls = []
    original = RepoMap.build_base_graph

    def counting(self, *args, **kwargs):
        calls.append(1)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(RepoMap, "build_base_graph", counting)
    return calls


def graph_file(root):
    return root / ".repomap" / "cache" / "graph.v1"


def from_disk():
    ranking_cache.memo_clear()


def seeded(rm, root, files, chat=(), mentioned=(), idents=None):
    return rm.build_ranking([str(root / f) for f in chat], files, set(mentioned), idents)


def same_ranking(a, b, ordered=True):
    """Equal rankings; `included` is in set order, which differs between builds with different seeds."""
    assert a.file_ranks == b.file_ranks
    assert a.ident_ranks == b.ident_ranks
    assert a.definitions == b.definitions and a.nested == b.nested and a.types == b.types
    assert (a.included == b.included) if ordered else (sorted(a.included) == sorted(b.included))
    assert a.report == b.report


def project(tmp_path, extra=None):
    return write(tmp_path, {**PROJECT_FILES, **(extra or {})})


@pytest.mark.parametrize("seed", SEEDS.values(), ids=SEEDS.keys())
def test_a_stored_graph_gives_the_same_ranking_as_a_fresh_build(tmp_path, builds, seed):
    files = project(tmp_path)
    off = Config(map=MapConfig(ranking_cache=False))
    fresh = seeded(make_repomap(tmp_path, off), tmp_path, files, **seed)
    assert not graph_file(tmp_path).exists()

    first = seeded(make_repomap(tmp_path), tmp_path, files, **seed)
    from_disk()
    loaded = seeded(make_repomap(tmp_path), tmp_path, files, **seed)
    remembered = seeded(make_repomap(tmp_path), tmp_path, files, **seed)

    assert len(builds) == 2   # the uncached build and the first cached one
    for ranking in (first, loaded, remembered):
        same_ranking(fresh, ranking)
    rm = make_repomap(tmp_path)
    expected = make_repomap(tmp_path, off).ranked_tags_of(fresh)
    assert rm.ranked_tags_of(loaded) == expected


def test_one_graph_serves_every_seed_set(tmp_path, builds):
    files = project(tmp_path)
    off = Config(map=MapConfig(ranking_cache=False))
    for seed in SEEDS.values():
        from_disk()
        same_ranking(seeded(make_repomap(tmp_path, off), tmp_path, files, **seed),
                     seeded(make_repomap(tmp_path), tmp_path, files, **seed), ordered=False)

    assert len(builds) == 1 + len(SEEDS)   # one cached build, the rest uncached comparisons


@pytest.mark.parametrize("seed", [SEEDS["chat"], SEEDS["dir"], SEEDS["idents"], SEEDS["chat and mentioned"]],
                         ids=["chat", "dir", "idents", "chat and mentioned"])
def test_seeded_outline_from_a_stored_graph_is_identical_to_a_fresh_one(tmp_path, builds, seed):
    files = project(tmp_path)
    off = Config(map=MapConfig(ranking_cache=False))
    chat = [str(tmp_path / f) for f in seed.get("chat", [])]
    focus = {m for m in seed.get("mentioned", []) if (tmp_path / m).is_dir()}
    names = {m for m in seed.get("mentioned", []) if m not in focus}

    def outline(rm):
        return rm.get_seeded_outline(chat, files, 2000, names, seed.get("idents"), focus)
    fresh_text, fresh_report = outline(make_repomap(tmp_path, off))
    outline(make_repomap(tmp_path))
    from_disk()
    disk_text, disk_report = outline(make_repomap(tmp_path))
    memo_text, _ = outline(make_repomap(tmp_path))

    assert len(builds) == 2
    assert fresh_text == disk_text == memo_text
    assert fresh_report == disk_report


def test_a_hit_reads_no_tags_and_a_seeded_map_reads_no_tags_a_second_time(tmp_path, builds, monkeypatch):
    files = project(tmp_path)
    seeded(make_repomap(tmp_path), tmp_path, files, chat=["core.ts"])
    from_disk()

    def forbidden(*args, **kwargs):
        raise AssertionError("tags were read")
    monkeypatch.setattr(RepoMap, "get_tags", forbidden)
    monkeypatch.setattr(RepoMap, "iter_tags", forbidden)
    rm = make_repomap(tmp_path)
    ranking = seeded(rm, tmp_path, files, chat=["used.ts"], idents={"helper"})
    tags = rm.ranked_tags_of(ranking)

    assert len(builds) == 1
    assert {tag.name for _rank, tag in tags if tag.rel_fname == "core.ts"} == {"helper"}


def test_a_build_ranks_its_definitions_without_reading_the_tags_twice(tmp_path, monkeypatch):
    files = project(tmp_path)
    rm = make_repomap(tmp_path, Config(map=MapConfig(ranking_cache=False)))
    ranking = seeded(rm, tmp_path, files, chat=["core.ts"])

    def forbidden(*args, **kwargs):
        raise AssertionError("tags were read again")
    monkeypatch.setattr(RepoMap, "get_tags", forbidden)

    assert rm.ranked_tags_of(ranking)


def test_the_ranked_tags_match_the_ones_made_from_the_files(tmp_path):
    files = project(tmp_path)
    for seed in SEEDS.values():
        rm = make_repomap(tmp_path)
        ranking = seeded(rm, tmp_path, files, **seed)
        assert ranking.base is not None
        from_base = rm.ranked_tags_of(ranking)
        ranking.base = None
        from_files = rm.ranked_tags_of(ranking)
        assert [(r, t.rel_fname, t.line, t.name, t.kind) for r, t in from_base] == \
               [(r, t.rel_fname, t.line, t.name, t.kind) for r, t in from_files]


def test_the_unseeded_ranking_reuses_the_graph_when_only_the_day_changed(tmp_path, builds, monkeypatch):
    files = project(tmp_path)
    make_repomap(tmp_path).build_ranking([], files)
    from_disk()
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 2 * 86400)
    import subprocess
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": os.environ["PATH"]}
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, env=env)
    subprocess.run(["git", "add", "core.ts"], cwd=tmp_path, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "one"], cwd=tmp_path, check=True, env=env)

    make_repomap(tmp_path).build_ranking([], files)   # new HEAD and day: the ranking is rebuilt, the graph is not

    assert len(builds) == 1


def test_a_changed_file_invalidates_on_disk_and_in_memory(tmp_path, builds):
    files = project(tmp_path)
    seeded(make_repomap(tmp_path), tmp_path, files, chat=["core.ts"])
    seeded(make_repomap(tmp_path), tmp_path, files, chat=["core.ts"])
    assert len(builds) == 1
    (tmp_path / "alone.ts").write_text("export function unrelated() { return 3; }\nexport function extra() {}\n")

    ranking = seeded(make_repomap(tmp_path), tmp_path, files, chat=["core.ts"])

    assert len(builds) == 2 and "extra" in ranking.definitions["alone.ts"]
    from_disk()
    seeded(make_repomap(tmp_path), tmp_path, files, chat=["core.ts"])
    assert len(builds) == 2
    (tmp_path / "alone.ts").write_text("export function unrelated() { return 4; }\nexport function extra2() {}\n")
    from_disk()
    assert "extra2" in seeded(make_repomap(tmp_path), tmp_path, files, chat=["core.ts"]).definitions["alone.ts"]
    assert len(builds) == 3


def test_a_new_or_removed_file_invalidates(tmp_path, builds):
    files = project(tmp_path)
    seeded(make_repomap(tmp_path), tmp_path, files)
    from_disk()
    added = files + write(tmp_path, {"extra.ts": "export function added() { return helper(1); }\n"})

    ranking = seeded(make_repomap(tmp_path), tmp_path, added)

    assert len(builds) == 2 and "extra.ts" in ranking.file_ranks
    from_disk()
    ranking = seeded(make_repomap(tmp_path), tmp_path, files)
    assert len(builds) == 3 and "extra.ts" not in ranking.file_ranks


def test_a_chat_file_outside_the_listed_files_is_part_of_the_key(tmp_path, builds):
    files = project(tmp_path)
    extra = write(tmp_path, {"extra.ts": "export const x = 1;\n"})
    seeded(make_repomap(tmp_path), tmp_path, files)
    from_disk()

    ranking = make_repomap(tmp_path).build_ranking(extra, files)

    assert "extra.ts" in ranking.file_ranks and len(builds) == 2


def test_changed_ranking_settings_invalidate(tmp_path, builds):
    files = project(tmp_path)
    seeded(make_repomap(tmp_path), tmp_path, files, chat=["core.ts"])
    from_disk()

    heavier = make_repomap(tmp_path, Config(ranking=RankingConfig(import_weight=50.0)))
    imports_weighed = seeded(heavier, tmp_path, files, chat=["core.ts"])

    assert len(builds) == 2
    assert imports_weighed.file_ranks != seeded(make_repomap(tmp_path), tmp_path, files, chat=["core.ts"]).file_ranks
    assert len(builds) == 3


def test_config_toml_and_areas_edits_invalidate(tmp_path, builds):
    from repomapper.areas import load_areas
    from repomapper.config import load_config
    files = project(tmp_path)

    def build():
        return seeded(make_repomap(tmp_path, load_config(str(tmp_path)), load_areas(str(tmp_path))), tmp_path, files,
                      chat=["core.ts"])
    build()
    from_disk()
    build()
    assert len(builds) == 1

    write(tmp_path, {".repomap/config.toml": "[ranking]\nimport_weight = 30.0\n"})
    from_disk()
    build()
    assert len(builds) == 2

    write(tmp_path, {".repomap/areas.toml": '[paths."alone.ts"]\nimportance = "core"\n'})
    from_disk()
    build()
    assert len(builds) == 3
    from_disk()
    build()
    assert len(builds) == 3


def test_a_new_extractor_fingerprint_invalidates(tmp_path, builds, monkeypatch):
    files = project(tmp_path)
    seeded(make_repomap(tmp_path), tmp_path, files)
    from_disk()
    seeded(make_repomap(tmp_path), tmp_path, files)
    assert len(builds) == 1

    monkeypatch.setattr(repomap_class, "extractor_fingerprint", lambda: "another extractor")
    seeded(make_repomap(tmp_path), tmp_path, files)

    assert len(builds) == 2


def test_an_unlisted_barrel_that_import_resolution_reads_invalidates(tmp_path, builds):
    files = write(tmp_path, BARREL_PROJECT)
    barrel = tmp_path / "lib/types/src/index.ts"
    barrel.write_text("export * from './user';\n")   # not in the map, as auto-ignored barrels are
    first = seeded(make_repomap(tmp_path), tmp_path, files, chat=["ui/app.ts"])
    assert first.graph.has_edge("ui/app.ts", "lib/types/src/user.ts")
    from_disk()
    stored = ranking_cache.load_graph(str(graph_file(tmp_path)), make_repomap(tmp_path).graph_key(
        [(f, ranking_cache.stamp_of(f)) for f in files]))
    assert stored is not None and stored.reads

    barrel.write_text("export * from './user';\nexport * from './task';\n")
    second = seeded(make_repomap(tmp_path), tmp_path, files, chat=["ui/app.ts"])

    assert len(builds) == 2
    assert second.graph.has_edge("ui/app.ts", "lib/types/src/task.ts")
    # the memo checks the unlisted files too
    barrel.write_text("export * from './user';\n")
    third = seeded(make_repomap(tmp_path), tmp_path, files, chat=["ui/app.ts"])
    assert len(builds) == 3 and not third.graph.has_edge("ui/app.ts", "lib/types/src/task.ts")


def test_an_unlisted_barrel_appearing_where_resolution_found_nothing_invalidates(tmp_path, builds):
    files = write(tmp_path, BARREL_PROJECT)
    seeded(make_repomap(tmp_path), tmp_path, files, chat=["ui/app.ts"])   # no src/index.ts yet: the probe found nothing
    from_disk()
    (tmp_path / "lib/types/src/index.ts").write_text("export * from './user';\n")

    ranking = seeded(make_repomap(tmp_path), tmp_path, files, chat=["ui/app.ts"])

    assert len(builds) == 2 and ranking.graph.has_edge("ui/app.ts", "lib/types/src/user.ts")


@pytest.mark.parametrize("damage", [
    lambda data: b"",
    lambda data: data[:len(data) // 2],
    lambda data: data[:data.index(b"\n") + 1] + b"not a pickle",
    lambda data: b"garbage\n" + data[data.index(b"\n") + 1:],
    lambda data: data.replace(b"repomap-graph v1", b"repomap-graph v9", 1),
    lambda data: data[:-3] + b"\x00\x00\x00",
])
def test_a_damaged_file_is_recomputed_silently_and_rewritten(tmp_path, builds, damage):
    files = project(tmp_path)
    fresh = seeded(make_repomap(tmp_path), tmp_path, files, chat=["core.ts"])
    path = graph_file(tmp_path)
    path.write_bytes(damage(path.read_bytes()))
    from_disk()

    recomputed = seeded(make_repomap(tmp_path), tmp_path, files, chat=["core.ts"])

    assert len(builds) == 2
    same_ranking(fresh, recomputed)
    from_disk()
    seeded(make_repomap(tmp_path), tmp_path, files, chat=["core.ts"])
    assert len(builds) == 2


def test_a_payload_of_the_wrong_shape_is_a_miss(tmp_path, builds):
    files = project(tmp_path)
    rm = make_repomap(tmp_path)
    key = rm.graph_key([(f, ranking_cache.stamp_of(f)) for f in set(files)])
    seeded(rm, tmp_path, files)
    path = graph_file(tmp_path)
    assert ranking_cache.load_graph(str(path), key) is not None

    payload = ranking_cache.pack_graph(ranking_cache.load_graph(str(path), key))
    for broken in ({"nodes": []}, {**payload, "src": payload["src"][:-1]},
                   {**payload, "dst": payload["dst"] + 10_000}, {**payload, "extra": 1}):
        path.write_bytes(ranking_cache.graph_header(key) + zlib.compress(pickle.dumps(broken)))
        assert ranking_cache.load_graph(str(path), key) is None
    assert ranking_cache.load_graph(str(tmp_path / "missing"), key) is None
    assert ranking_cache.load_graph(str(path), "another key") is None


def test_the_ranking_key_does_not_make_a_graph_hit(tmp_path, builds):
    files = project(tmp_path)
    rm = make_repomap(tmp_path)
    stamps = [(f, ranking_cache.stamp_of(f)) for f in set(files)]
    assert rm.graph_key(stamps) != rm.ranking_key(stamps)


def test_unwritable_cache_location_is_silent(tmp_path, builds):
    files = project(tmp_path)
    (tmp_path / ".repomap").write_text("a file where the directory should be")

    assert seeded(make_repomap(tmp_path), tmp_path, files, chat=["core.ts"]).file_ranks


def test_writes_are_atomic_and_leave_no_temp_files(tmp_path, builds, monkeypatch):
    files = project(tmp_path)
    replaced = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda src, dst: (replaced.append((src, dst)), real_replace(src, dst))[1])

    seeded(make_repomap(tmp_path), tmp_path, files, chat=["core.ts"])

    assert [dst for _src, dst in replaced] == [str(graph_file(tmp_path))]
    assert replaced[0][0] != replaced[0][1]
    assert not [p for p in graph_file(tmp_path).parent.iterdir() if p.name.endswith(".tmp")]


def test_a_failed_write_leaves_the_old_file_and_no_temp_file(tmp_path, builds, monkeypatch):
    files = project(tmp_path)
    seeded(make_repomap(tmp_path), tmp_path, files)
    old = graph_file(tmp_path).read_bytes()
    (tmp_path / "alone.ts").write_text("export function unrelated() { return 3; }\nexport function extra() {}\n")

    def broken_replace(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", broken_replace)
    seeded(make_repomap(tmp_path), tmp_path, files)

    assert graph_file(tmp_path).read_bytes() == old
    assert not [p for p in graph_file(tmp_path).parent.iterdir() if p.name.endswith(".tmp")]


def test_the_ranking_cache_switch_disables_the_graph_cache_too(tmp_path, builds):
    files = project(tmp_path)
    config = Config(map=MapConfig(ranking_cache=False))

    seeded(make_repomap(tmp_path, config), tmp_path, files, chat=["core.ts"])
    seeded(make_repomap(tmp_path, config), tmp_path, files, chat=["core.ts"])

    assert len(builds) == 2 and not graph_file(tmp_path).exists()
    # and it does not use one that is there
    seeded(make_repomap(tmp_path), tmp_path, files, chat=["core.ts"])
    assert graph_file(tmp_path).exists()
    seeded(make_repomap(tmp_path, config), tmp_path, files, chat=["core.ts"])
    assert len(builds) == 4


def test_force_refresh_rebuilds_and_stores_again(tmp_path, builds):
    files = project(tmp_path)
    seeded(make_repomap(tmp_path), tmp_path, files)

    make_repomap(tmp_path).build_ranking([], files, force_refresh=True)

    assert len(builds) == 2


def test_graphs_with_missing_files_or_parse_errors_are_not_stored(tmp_path, builds):
    files = project(tmp_path)
    ranking = make_repomap(tmp_path).build_ranking([], files + [str(tmp_path / "gone.ts")])
    assert ranking.report.excluded and not graph_file(tmp_path).exists()

    original = RepoMap.get_tags_raw

    def failing(self, fname, rel_fname):
        if rel_fname == "alone.ts":
            self.record_parse_error(fname, "boom")
            return []
        return original(self, fname, rel_fname)
    RepoMap.get_tags_raw = failing
    try:
        ranking = make_repomap(tmp_path).build_ranking([], files, force_refresh=True)
    finally:
        RepoMap.get_tags_raw = original
    assert ranking.report.parse_errors and not graph_file(tmp_path).exists()


def test_a_seeded_hit_does_not_change_what_the_next_one_gets(tmp_path, builds):
    files = project(tmp_path)
    first = seeded(make_repomap(tmp_path), tmp_path, files, chat=["core.ts"], idents={"helper"})
    first.report.excluded["x"] = "changed by the caller"
    plain = seeded(make_repomap(tmp_path), tmp_path, files)
    seeded(make_repomap(tmp_path), tmp_path, files, chat=["used.ts"], idents={"unrelated"})
    again = seeded(make_repomap(tmp_path), tmp_path, files)

    same_ranking(plain, again)
    assert not again.report.excluded and len(builds) == 1


def test_the_graph_memo_keeps_a_few_roots(tmp_path):
    for i in range(ranking_cache.MEMO_ROOTS + 1):
        ranking_cache.graph_memo.put(f"/root{i}", "key", object(), [])

    assert ranking_cache.graph_memo.get("/root0", "key") is None
    assert ranking_cache.graph_memo.get("/root1", "key") is not None
    ranking_cache.memo_clear()
    assert ranking_cache.graph_memo.get("/root1", "key") is None
