from repomapper.areas import Area, Areas
from repomapper.config import IdentifierWeightConfig, OutlineConfig
from repomapper.outline import HEADER, MAX_PLAIN_DIRS, OutlineFile, find_docs, render_outline
from repomapper.utils import count_tokens


def tokens(text):
    return count_tokens(text, "gpt-4")


FILES = {
    "lib/types/user.ts": OutlineFile(0.5, [(0.4, "User"), (0.1, "UserId")]),
    "lib/types/task.ts": OutlineFile(0.2, [(0.2, "Task")]),
    "ui/app.ts": OutlineFile(0.2, [(0.05, "App")]),
    "front_end/legacy.js": OutlineFile(0.1, [(0.01, "init")]),
}
EDGES = {("ui/app.ts", "lib/types/user.ts"): 3.0, ("ui/app.ts", "lib/types/task.ts"): 1.0}


def test_small_budget_folds_directories_to_one_line():
    out = render_outline(FILES, EDGES, 167, tokens)
    assert "lib/types/  2 files (ts 2) 70.0%: User, UserId, Task" in out
    assert "user.ts" not in out
    assert "ui/ -> lib/types/" in out


def test_directory_too_big_to_open_stays_folded_and_smaller_ones_open():
    out = render_outline(FILES, EDGES, 230, tokens)
    assert "lib/types/  2 files" in out and "user.ts" not in out
    assert "app.ts 20.0%: App" in out
    assert "legacy.js 10.0%: init" in out   # every top-level area opens a level before lib/ goes deeper


def test_large_budget_opens_everything_and_links_files():
    out = render_outline(FILES, EDGES, 10_000, tokens)
    assert "legacy.js" in out
    assert "ui/app.ts -> lib/types/user.ts" in out


def test_single_subdirectory_chains_open_together():
    files = {"a/b/c/x.py": OutlineFile(1.0, [(1.0, "x")])}
    out = render_outline(files, {}, 10_000, tokens)
    assert "x.py 100.0%: x" in out


def test_points_to_docs():
    out = render_outline(FILES, EDGES, 120, tokens, docs=lambda d: ["CLAUDE.md"] if d == "ui" else [])
    assert "ui/  1 file (ts 1) 20.0%: App  [CLAUDE.md]" in out


def test_settings_limit_names_files_and_edges():
    settings = OutlineConfig(defs_per_file=1, files_per_dir=1, max_edges=1)
    out = render_outline(FILES, EDGES, 10_000, tokens, settings=settings)
    assert "user.ts 50.0%: User\n" in out
    assert "+1 more files" in out
    assert out.count(" -> ") == 1


def test_area_notes_and_status_show_on_their_lines():
    areas = Areas({"front_end": Area(status="legacy", replaced_by="ui", note="old jQuery app")})
    out = render_outline(FILES, EDGES, 10_000, tokens, areas=areas)
    assert "front_end/  1 file (js 1) 10.0% [legacy, replaced by ui] — old jQuery app" in out


def test_folded_area_stays_one_line_with_room_to_spare():
    out = render_outline(FILES, EDGES, 10_000, tokens, areas=Areas({"lib": Area(fold=True)}))
    assert "lib/types/  2 files" in out
    assert "user.ts" not in out


def test_directory_lines_show_git_activity():
    notes = {"lib/types": "3 commits/90d, last 2d", "front_end": "idle 3y"}
    out = render_outline(FILES, EDGES, 10_000, tokens, activity=notes.get)
    assert "lib/types/  2 files (ts 2) 70.0%, 3 commits/90d, last 2d" in out
    assert "front_end/  1 file (js 1) 10.0%, idle 3y" in out
    assert "ui/  1 file (ts 1) 20.0%\n" in out or "ui/  1 file (ts 1) 20.0%:" in out


def leaf_files(prefix, n, rank):
    return {f"{prefix}/f{i}.py": OutlineFile(rank, [(rank, f"{prefix.replace('/', '_')}_{i}")]) for i in range(n)}


# The `services` case: ui/ is popular and nested, api/ is big but nothing imports it
SERVICES = {
    **leaf_files("ui/app/shell/core", 4, 0.1),
    **leaf_files("ui/app/shell/widgets", 4, 0.08),
    **leaf_files("api/handlers", 12, 0.001),
    **leaf_files("api/db", 12, 0.001),
    **leaf_files("api/auth", 8, 0.001),
}


def test_popular_small_dir_does_not_starve_big_unreferenced_one():
    out = render_outline(SERVICES, {}, 500, tokens)
    assert "handlers/  12 files" in out and "db/  12 files" in out
    assert "ui/app/shell/  8 files" in out


def test_importance_tilts_the_split():
    files = {**leaf_files("x/a/deep", 5, 0.01), **leaf_files("x/b", 5, 0.01),
             **leaf_files("y/a/deep", 7, 0.1), **leaf_files("y/b", 7, 0.1)}
    out = render_outline(files, {}, 500, tokens, areas=Areas({"x": Area(importance="core")}))
    y_part, x_part = out.split("\n  x/")
    assert "f0.py" in x_part        # core x/ opens down to files despite y/'s higher rank
    assert "f0.py" not in y_part


def test_many_similar_children_show_biggest_few_and_more_line():
    files = {**leaf_files("pkgs/big", 5, 0.01)}
    for i in range(40):
        files.update(leaf_files(f"pkgs/p{i:02}", 1, 0.01))
    out = render_outline(files, {}, 10_000, tokens)
    assert "big/  5 files" in out
    assert "+29 more dirs" in out
    assert out.count("1 file (py 1)") == 11


def test_fits_budget_at_any_size():
    for budget in (200, 250, 300, 400, 500, 800, 1500):
        out = render_outline(SERVICES, {("ui/app/shell/core/f0.py", "api/db/f0.py"): 1.0}, budget, tokens)
        assert tokens(out) <= budget, budget


def test_importance_weights_are_configurable():
    files = {**leaf_files("x/a/deep", 5, 0.01), **leaf_files("x/b", 5, 0.01),
             **leaf_files("y/a/deep", 7, 0.1), **leaf_files("y/b", 7, 0.1)}
    areas = Areas({"x": Area(importance="core")})
    flat = render_outline(files, {}, 450, tokens, areas=areas, importance={"core": 1.0, "normal": 1.0, "low": 1.0})
    tilted = render_outline(files, {}, 450, tokens, areas=areas)
    assert flat != tilted


def test_reverse_edges_merge_into_one_line():
    edges = {("ui/app.ts", "lib/types/user.ts"): 3.0, ("lib/types/user.ts", "ui/app.ts"): 1.0}
    out = render_outline(FILES, edges, 10_000, tokens)
    assert "ui/app.ts <-> lib/types/user.ts" in out
    assert " -> " not in out


def test_directory_does_not_point_at_its_own_listed_file():
    files = {"pkg/a.py": OutlineFile(0.6, [(0.6, "a")]), "pkg/b.py": OutlineFile(0.4, [(0.4, "b")])}
    settings = OutlineConfig(files_per_dir=1)
    out = render_outline(files, {("pkg/b.py", "pkg/a.py"): 1.0}, 10_000, tokens, settings=settings)
    assert "Dependencies" not in out


def test_edge_labels_are_relative_to_their_common_directory():
    edges = {("lib/types/task.ts", "lib/types/user.ts"): 2.0}
    out = render_outline(FILES, edges, 10_000, tokens)
    assert "  lib/types/: task.ts -> user.ts" in out   # only internal edges exist, so they show
    grouped = {"web/ui/a.ts": OutlineFile(0.5, [(0.5, "A")]), "web/api/b.ts": OutlineFile(0.5, [(0.5, "B")])}
    out = render_outline(grouped, {("web/ui/a.ts", "web/api/b.ts"): 1.0}, 10_000, tokens)
    assert "  web/: ui/a.ts -> api/b.ts" in out   # `web/` holds no files, so ui and api are separate areas


def test_lone_top_level_dir_is_not_repeated_by_a_root_line():
    files = {"pkg/a.py": OutlineFile(0.6, [(0.6, "a")]), "pkg/b.py": OutlineFile(0.4, [(0.4, "b")])}
    out = render_outline(files, {}, 10_000, tokens)
    assert "./" not in out
    assert "\npkg/  2 files" in out
    assert "\n  a.py 60.0%: a" in out


TEST_GLOBS = Areas(globs={"*_test.*": Area(importance="low", kind="test"), "karma.conf.js": Area(kind="config")})


def test_test_and_config_files_are_counted_but_give_no_names_or_edges():
    files = {
        "pkg/a.py": OutlineFile(0.3, [(0.3, "Real")]),
        "pkg/a_test.py": OutlineFile(0.5, [(0.5, "test_real")]),
        "pkg/b_test.py": OutlineFile(0.1, [(0.1, "test_other")]),
        "ui/app.js": OutlineFile(0.05, [(0.05, "App")]),
        "ui/karma.conf.js": OutlineFile(0.05, [(0.05, "config")]),
    }
    edges = {("pkg/a_test.py", "pkg/a.py"): 5.0, ("ui/karma.conf.js", "pkg/a.py"): 5.0}
    small = render_outline(files, edges, 120, tokens, areas=TEST_GLOBS)
    assert "pkg/  3 files (py 3; 2 test) 90.0%: Real\n" in small
    assert small.endswith("ui/  2 files (js 2; 1 config) 10.0%: App")
    assert "Dependencies" not in small

    large = render_outline(files, edges, 10_000, tokens, areas=TEST_GLOBS)
    assert "a_test.py" not in large and "karma.conf.js" not in large      # counted, never listed
    assert "test_real" not in large and "Dependencies" not in large


def test_test_files_take_no_lines_or_budget_and_do_not_count_as_more_files():
    files = {**leaf_files("pkg", 3, 0.1), **{f"pkg/t{i}_test.py": OutlineFile(0.01, []) for i in range(30)}}
    out = render_outline(files, {}, 10_000, tokens, settings=OutlineConfig(files_per_dir=2), areas=TEST_GLOBS)
    assert "pkg/  33 files (py 33; 30 test)" in out
    assert "_test.py" not in out
    assert "+1 more files" in out
    # the lines the tests would have cost are free: this budget holds the directory open with its files
    budget = (tokens(out) + tokens(HEADER)) * 5 // 4 + 1   # the header and a dependency reserve come off the top
    assert render_outline(files, {}, budget, tokens, settings=OutlineConfig(files_per_dir=2), areas=TEST_GLOBS) == out


def test_names_defined_in_many_directories_are_not_samples():
    files = {f"d{i}/c.ts": OutlineFile(0.1, [(0.09, "ngOnInit"), (0.01, f"Comp{i}")]) for i in range(4)}
    out = render_outline(files, {}, 10_000, tokens, settings=OutlineConfig(common_name_dirs=3))
    assert "ngOnInit" not in out
    assert "c.ts 10.0%: Comp0" in out
    kept = render_outline(files, {}, 10_000, tokens, settings=OutlineConfig(common_name_dirs=4))
    assert "ngOnInit" in kept


def test_file_without_definitions_is_a_plain_line():
    files = {"ops/docker-bake.hcl": OutlineFile(0.4, []), "ops/run.py": OutlineFile(0.6, [(0.6, "run")])}
    out = render_outline(files, {}, 10_000, tokens)
    assert out.endswith("\n  docker-bake.hcl 40.0%")


def test_minified_files_give_no_sample_names():
    minified = [(0.5 - i / 100, chr(97 + i)) for i in range(12)] + [(0.01, "format")]
    files = {"lib/vendor.js": OutlineFile(0.5, minified), "lib/app.js": OutlineFile(0.5, [(0.1, "App")])}
    out = render_outline(files, {}, 10_000, tokens)
    assert "lib/  2 files (js 2) 100.0%: App\n" in out or "\nlib/  2 files (js 2) 100.0%\n" in out
    assert "vendor.js 50.0%\n" in out


def test_directories_of_only_test_files_stay_folded():
    files = {**leaf_files("src", 3, 0.3), **leaf_files("tests/unit", 5, 0.01), **leaf_files("tests/e2e", 5, 0.01)}
    areas = Areas(globs={"tests/**": Area(importance="low", kind="test")})
    out = render_outline(files, {}, 10_000, tokens, areas=areas)
    assert "\n  tests/  10 files (py 10; 10 test) 10.0%" in out
    assert "unit/" not in out
    assert "src_0" in out


# The `services` feedback: one huge, wide, popular dir (web/src/app/, 30 domain folders) next to small deep
# Java-style services whose code sits under single-child chains (src/main/java/com/acme/<svc>/)
def java_service(name, packages, rank):
    files = {}
    for pkg, n in packages.items():
        for i in range(n):
            files[f"{name}/src/main/java/com/acme/{name}/{pkg}/C{pkg}{i}.java"] = OutlineFile(rank, [(rank, f"{name}_{pkg}_{i}")])
    return files


WIDE = {
    **{rel: f for d in range(30) for rel, f in leaf_files(f"web/src/app/domain{d:02}", 20 + d, 0.0005).items()},
    **leaf_files("web/src/libs", 3, 0.0001),
    **java_service("email", {"domain": 6, "utils": 6, "service": 6, "exceptions": 3}, 0.0001),
    **java_service("cas", {"auth": 3, "web": 2}, 0.0001),
}


def test_wide_popular_dir_opens_before_small_deep_ones_open_to_files():
    # web/ is where the commits are; the services are idle
    recent = recent_of({"web": 50, "web/src": 50, "web/src/app": 50, **{f"web/src/app/domain{d:02}": 5 for d in range(30)}})
    out = render_outline(WIDE, {}, 900, tokens, recent_commits=recent)
    assert "app/  " in out and "domain29/  49 files" in out    # biggest children listed folded
    assert "more dirs" in out
    assert ".java" not in out                                     # email/ and cas/ don't open down to files


def test_large_dir_opens_a_level_even_when_fully_opening_is_expensive():
    # The budget can't fit app/ with its files or all 12 children; it still lists its biggest few
    out = render_outline(WIDE, {}, 600, tokens)
    assert "domain29/  49 files" in out
    assert "more dirs" in out


def test_small_light_sibling_does_not_get_the_leftover_budget():
    files = {**{rel: f for d in range(20) for rel, f in leaf_files(f"big/d{d:02}", 30, 0.001).items()},
             **leaf_files("tiny", 3, 0.0001)}
    out = render_outline(files, {}, 700, tokens)
    assert "tiny/  3 files" in out
    assert "tiny/f0.py" not in out and "\n  f0.py" not in out and "  f0.py 0.0%: tiny_0" not in out


def test_single_child_chains_collapse_into_one_line():
    out = render_outline(WIDE, {}, 20_000, tokens)
    assert "java/com/acme/email/  21 files" in out
    assert "\n        com/" not in out and "  acme/  " not in out
    assert "domain/  6 files" in out    # still opens below the chain


def test_chain_at_top_collapses_and_edges_use_full_paths():
    files = {"solo/a/b/x.py": OutlineFile(0.5, [(0.5, "x")]), "other/y.py": OutlineFile(0.5, [(0.5, "y")]),
             "other/z.py": OutlineFile(0.0, [(0.0, "z")])}
    out = render_outline(files, {("other/y.py", "solo/a/b/x.py"): 1.0}, 10_000, tokens)
    assert "\nsolo/a/b/  1 file (py 1) 50.0%" in out or "\n  solo/a/b/  1 file (py 1) 50.0%" in out
    assert "  a/  " not in out
    assert "other/y.py -> solo/a/b/x.py" in out
    folded = render_outline(files, {("other/y.py", "solo/a/b/x.py"): 1.0}, 140, tokens)
    assert "solo/a/b/  1 file" in folded
    assert "-> solo/a/b/" in folded


def test_collapsed_chain_keeps_docs_and_notes_of_its_upper_dirs():
    files = {"svc/src/main/a.py": OutlineFile(0.5, [(0.5, "a")]), "svc/src/main/b.py": OutlineFile(0.5, [(0.5, "b")])}
    areas = Areas({"svc": Area(note="mail service")})
    out = render_outline(files, {}, 10_000, tokens, areas=areas, docs=lambda d: ["README.md"] if d == "svc" else [])
    assert "svc/src/main/  2 files (py 2) 100.0%  [svc/README.md] — mail service" in out


def test_generic_builtin_names_are_not_samples():
    files = {"lib/queue.ts": OutlineFile(1.0, [(0.5, "push"), (0.3, "set"), (0.2, "JobQueue"), (0.1, "id")])}
    out = render_outline(files, {}, 10_000, tokens)
    assert "queue.ts 100.0%: JobQueue\n" in out or out.endswith("queue.ts 100.0%: JobQueue")
    kept = render_outline(files, {}, 10_000, tokens, settings=OutlineConfig(generic_names=[]))
    assert "queue.ts 100.0%: JobQueue, push, set, id" in kept


def test_type_names_are_sampled_before_higher_ranked_methods():
    files = {"lib/queue.ts": OutlineFile(1.0, [(0.5, "enqueueAll"), (0.4, "dequeueOne"), (0.2, "JobQueue")], {"JobQueue"})}
    out = render_outline(files, {}, 10_000, tokens)
    assert "queue.ts 100.0%: JobQueue, " in out
    flat = render_outline(files, {}, 10_000, tokens, settings=OutlineConfig(type_boost=1.0))
    assert "queue.ts 100.0%: enqueueAll, dequeueOne, JobQueue" in flat


def test_short_names_are_sampled_after_longer_ones():
    files = {"lib/q.ts": OutlineFile(1.0, [(0.5, "item"), (0.4, "Worker"), (0.3, "Pending")])}
    out = render_outline(files, {}, 10_000, tokens)
    assert "q.ts 100.0%: Worker, Pending, item" in out
    off = render_outline(files, {}, 10_000, tokens, settings=OutlineConfig(short_name_penalty=1.0))
    assert "q.ts 100.0%: item, Worker, Pending" in off


def test_names_defined_in_many_files_rank_lower_as_samples():
    files = {f"d/f{i}.ts": OutlineFile(0.1, [(0.09, "renderRow"), (0.05, f"Widget{i}")]) for i in range(3)}
    weights = IdentifierWeightConfig(common_definers=2, common=0.1)
    out = render_outline(files, {}, 10_000, tokens, weights=weights)
    assert "f0.ts 10.0%: Widget0, renderRow" in out
    loose = render_outline(files, {}, 10_000, tokens, weights=IdentifierWeightConfig(common_definers=5))
    assert "f0.ts 10.0%: renderRow, Widget0" in loose


def test_rank_below_a_tenth_of_a_percent_is_hidden():
    files = {"a/big.py": OutlineFile(0.9995, [(0.9, "Big")]), "a/tiny.py": OutlineFile(0.0005, [(0.0005, "Tiny")])}
    out = render_outline(files, {}, 10_000, tokens)
    assert "\n  tiny.py: Tiny" in out
    assert "big.py 100.0%: Big" in out
    assert "0.1%" in out.splitlines()[0]


def test_file_lines_show_activity_only_when_hot():
    notes = {"lib/types/user.ts": "9 commits/90d, last 1d", "lib/types/task.ts": "1 commit/90d, last 80d"}
    recent = {"lib/types/user.ts": 9, "lib/types/task.ts": 1}
    out = render_outline(FILES, EDGES, 10_000, tokens, activity=notes.get, recent_commits=recent.get)
    assert "user.ts 50.0%, 9 commits/90d, last 1d: User, UserId" in out
    assert "task.ts 20.0%: Task" in out and "80d" not in out


AREAS = {
    "ui/app/z.py": OutlineFile(0.2, [(0.2, "Z")]),
    "ui/app/a/x.py": OutlineFile(0.2, [(0.2, "X")]),
    "ui/app/b/y.py": OutlineFile(0.2, [(0.2, "Y")]),
    "api/s.py": OutlineFile(0.2, [(0.2, "S")]),
    "lib/m.py": OutlineFile(0.2, [(0.2, "M")]),
    "old/o.py": OutlineFile(0.1, [(0.1, "O")]),
}


def test_edges_inside_one_top_level_area_are_dropped_when_others_exist():
    edges = {("ui/app/a/x.py", "ui/app/b/y.py"): 9.0, ("api/s.py", "lib/m.py"): 1.0}
    out = render_outline(AREAS, edges, 10_000, tokens)
    assert "api/s.py -> lib/m.py" in out
    assert "a/x.py" not in out.split("Dependencies")[1]


def test_edges_inside_one_area_show_when_they_are_all_there_is():
    out = render_outline(AREAS, {("ui/app/a/x.py", "ui/app/b/y.py"): 9.0}, 10_000, tokens)
    assert "ui/app/: a/x.py -> b/y.py" in out


def test_edges_from_sibling_folders_collapse_to_their_area_and_free_slots():
    edges = {("ui/app/a/x.py", "api/s.py"): 3.0, ("ui/app/b/y.py", "api/s.py"): 2.0, ("ui/app/a/x.py", "lib/m.py"): 1.0}
    out = render_outline(AREAS, edges, 10_000, tokens, settings=OutlineConfig(max_edges=2))
    deps = out.split("Dependencies")[1]
    assert "  ui/app/ -> api/s.py\n" in deps
    assert "  ui/app/ -> lib/m.py" in deps


def test_files_directly_in_an_area_keep_their_name_in_edges():
    out = render_outline(AREAS, {("ui/app/z.py", "api/s.py"): 1.0}, 10_000, tokens)
    assert "  ui/app/z.py -> api/s.py" in out


def test_edges_touching_legacy_or_deprecated_areas_are_dropped():
    areas = Areas({"old": Area(status="legacy"), "lib": Area(status="deprecated")})
    edges = {("ui/app/z.py", "old/o.py"): 9.0, ("lib/m.py", "api/s.py"): 8.0, ("ui/app/z.py", "api/s.py"): 1.0}
    out = render_outline(AREAS, edges, 10_000, tokens, areas=areas)
    deps = out.split("Dependencies")[1]
    assert "old/" not in deps and "lib/" not in deps and "ui/app/z.py -> api/s.py" in deps
    only = render_outline(AREAS, {("ui/app/z.py", "old/o.py"): 9.0}, 10_000, tokens, areas=areas)
    assert "Dependencies" not in only


def plain(directory, n, ext="sql"):
    return [f"{directory}/f{i}.{ext}" for i in range(n)]


def test_directory_of_unparseable_files_is_a_count_only_line_with_its_note():
    areas = Areas({"flyway": Area(note="Flyway migrations")})
    out = render_outline(FILES, EDGES, 10_000, tokens, areas=areas, plain_files=plain("flyway", 6))
    assert "flyway/  6 files (sql 6) — Flyway migrations\n" in out
    assert "f0.sql" not in out


def test_unparseable_files_in_a_directory_with_code_are_not_listed():
    out = render_outline(FILES, EDGES, 10_000, tokens, plain_files=["ui/data.json", "ui/more.json", "ui/x.json", "README.md"])
    assert "json" not in out and "ui/  1 file (ts 1)" in out


def test_non_code_directories_do_not_change_their_parents_counts_or_shares():
    out = render_outline(FILES, EDGES, 10_000, tokens, plain_files=plain("ui/migrations", 5))
    assert "ui/  1 file (ts 1) 20.0%" in out
    assert "migrations/  5 files (sql 5)" in out


def test_non_code_directory_is_one_line_however_deep_and_has_no_share_or_edges():
    out = render_outline(FILES, EDGES, 10_000, tokens, plain_files=plain("helm/charts/api", 4, "yaml") + plain("helm", 3, "tpl"))
    assert "helm/  7 files (yaml 4, tpl 3)\n" in out
    assert "charts" not in out and "%" not in out.split("helm/")[1].split("\n")[0]


def test_small_non_code_directories_and_all_but_the_biggest_few_are_dropped():
    paths = plain("tiny", 2) + sum((plain(f"d{i:02}", 3 + i) for i in range(20)), [])
    out = render_outline(FILES, EDGES, 100_000, tokens, plain_files=paths, settings=OutlineConfig(dirs_per_dir=50))
    assert "tiny/" not in out
    assert sum(1 for line in out.split("\n") if "(sql" in line) == MAX_PLAIN_DIRS
    assert "d19/" in out and "d00/" not in out


def test_non_code_lines_are_budgeted_like_other_lines():
    paths = sum((plain(f"d{i:02}", 5) for i in range(10)), [])
    small = render_outline(FILES, EDGES, 230, tokens, plain_files=paths)
    assert tokens(small) <= 230
    assert tokens(render_outline(FILES, EDGES, 10_000, tokens, plain_files=paths)) > tokens(small)


def test_hidden_directories_are_skipped():
    out = render_outline(FILES, EDGES, 10_000, tokens, plain_files=plain(".github/workflows", 6, "yml"))
    assert "github" not in out


# Priority-queue expansion: importance x sqrt(files) x activity / token cost, best first until the budget is spent
def recent_of(table):
    return lambda path: table.get(path, 0)


def test_core_area_opens_a_first_level_before_busier_cheaper_dirs():
    files = {**leaf_files("core/src/alpha", 6, 0.001), **leaf_files("core/src/beta", 6, 0.001),
             **{rel: f for i in range(8) for rel, f in leaf_files(f"busy/d{i}", 2, 0.05).items()}}
    areas = Areas({"core": Area(importance="core")})
    recent = recent_of({f"busy/d{i}": 30 for i in range(8)} | {"busy": 240})
    out = render_outline(files, {}, 290, tokens, areas=areas, recent_commits=recent)
    assert "core/src/  12 files" in out and "alpha/  6 files" in out     # core/src opened its first level...
    assert "\n  busy/  16 files (py 16) 80.0%:" in out and "d0/" not in out   # ...while the busy dirs are still folded
    assert tokens(out) <= 290


def test_idle_directory_stays_folded_while_an_active_one_of_the_same_size_opens():
    files = {**leaf_files("busy/a", 5, 0.1), **leaf_files("busy/b", 5, 0.1),
             **leaf_files("idle/a", 5, 0.1), **leaf_files("idle/b", 5, 0.1)}
    recent = recent_of({"busy": 40, "busy/a": 20, "busy/b": 20})
    probe = render_outline(files, {}, 10_000, tokens, recent_commits=recent)
    budget = tokens(probe) * 2 // 3     # room for part of the layout, not all of it
    out = render_outline(files, {}, budget, tokens, recent_commits=recent)
    assert "busy/  10 files" in out and "a/  5 files" in out
    assert "idle/  10 files" in out and "\n  a/  5 files" not in out.split("idle/")[1]
    assert tokens(out) <= budget


def test_flat_large_core_directory_lists_its_top_files():
    files = {**{f"repos/src/r{i:03}.ts": OutlineFile(0.002, [(0.002, f"Repo{i:03}")]) for i in range(221)},
             **leaf_files("api/routes", 4, 0.01), **leaf_files("misc/a", 3, 0.01), **leaf_files("misc/b", 3, 0.01)}
    for i in range(20):    # 4,000 files elsewhere: the flat directory is no "large dir" (a tenth of the repo)
        files.update(leaf_files(f"bulk/d{i:02}", 200, 0.0001))
    areas = Areas({"repos": Area(importance="core")})
    out = render_outline(files, {}, 700, tokens, areas=areas)
    assert "r000.ts" in out and "+211 more files" in out
    assert tokens(out) <= 700


def test_children_are_ordered_by_rank_and_activity_not_rank_alone():
    files = {**leaf_files("pkg/shared", 4, 0.02), **leaf_files("pkg/work", 4, 0.01), **leaf_files("pkg/quiet", 4, 0.01)}
    settings = OutlineConfig(dirs_per_dir=1)
    out = render_outline(files, {}, 10_000, tokens, settings=settings, recent_commits=recent_of({"pkg/work": 30}))
    assert "work/  4 files" in out and "shared/" not in out
    assert "+2 more dirs" in out


def test_activity_settings_are_configurable():
    files = {**leaf_files("work/a", 5, 0.1), **leaf_files("work/b", 5, 0.1),
             **leaf_files("idle/a", 5, 0.1), **leaf_files("idle/b", 5, 0.1)}   # the idle one sorts first on ties
    recent = recent_of({"work": 40, "work/a": 20, "work/b": 20})
    budget = tokens(render_outline(files, {}, 10_000, tokens, recent_commits=recent)) * 2 // 3
    default = render_outline(files, {}, budget, tokens, recent_commits=recent)
    blind = render_outline(files, {}, budget, tokens, recent_commits=recent, settings=OutlineConfig(idle_activity=1.0))
    assert "\n    a/  5 files" in default.split("work/")[1] and "idle/  10 files (py 10) 100.0%:" in default
    assert "idle/  10 files (py 10) 100.0%\n" in blind


def test_a_wide_directory_lists_a_few_subdirectories_first_and_more_as_the_budget_grows():
    files = {rel: f for i in range(20) for rel, f in leaf_files(f"wide/d{i:02}", 5, 0.01).items()}
    sizes = []
    for budget in (350, 600, 10_000):
        out = render_outline(files, {}, budget, tokens)
        assert tokens(out) <= budget
        sizes.append(sum(1 for line in out.split("\n") if " 5 files (py 5)" in line))
    assert 3 <= sizes[0] <= 6 < sizes[1] <= 12 == sizes[2]


def test_shallow_directories_open_before_deeper_ones():
    files = {rel: f for top in ("m", "n") for sub in ("a", "b") for rel, f in leaf_files(f"{top}/{sub}", 10, 0.01).items()}
    out = render_outline(files, {}, 400, tokens)
    assert out.count(" 10 files (py 10)") == 4      # both m/ and n/ list their subdirectories...
    assert "f0.py" not in out                       # ...before either opens one of them down to files


def test_import_edges_give_direction_where_shared_names_point_both_ways():
    names = {("ui/app.ts", "lib/types/user.ts"): 3.0, ("lib/types/user.ts", "ui/app.ts"): 1.0}
    out = render_outline(FILES, names, 10_000, tokens, import_edges={("ui/app.ts", "lib/types/user.ts"): 1.0})
    assert "ui/app.ts -> lib/types/user.ts" in out
    assert "<->" not in out


def test_pairs_without_imports_keep_the_direction_of_shared_names():
    names = {("ui/app.ts", "lib/types/user.ts"): 3.0, ("lib/types/user.ts", "ui/app.ts"): 1.0,
             ("front_end/legacy.js", "lib/types/task.ts"): 2.0, ("lib/types/task.ts", "front_end/legacy.js"): 1.0}
    out = render_outline(FILES, names, 10_000, tokens, import_edges={("ui/app.ts", "lib/types/user.ts"): 1.0})
    assert "front_end/legacy.js <-> lib/types/task.ts" in out
    assert "ui/app.ts -> lib/types/user.ts" in out


def test_imports_both_ways_stay_a_double_arrow_and_imports_alone_make_a_line():
    both = {("ui/app.ts", "lib/types/user.ts"): 2.0, ("lib/types/user.ts", "ui/app.ts"): 1.0}
    assert "ui/app.ts <-> lib/types/user.ts" in render_outline(FILES, {}, 10_000, tokens, import_edges=both)
    one_way = {("ui/app.ts", "lib/types/user.ts"): 1.0}
    assert "ui/app.ts -> lib/types/user.ts" in render_outline(FILES, {}, 10_000, tokens, import_edges=one_way)


def test_collapsed_chain_shows_only_the_deepest_annotation_per_key():
    files = {"static/ls-p2x/src/a.py": OutlineFile(0.5, [(0.5, "a")]), "static/ls-p2x/src/b.py": OutlineFile(0.5, [(0.5, "b")])}
    areas = Areas({"static": Area(status="deprecated", note="old static assets"),
                   "static/ls-p2x": Area(status="legacy", replaced_by="source/p3x", note="p2x bundle")})
    out = render_outline(files, {}, 10_000, tokens, areas=areas)
    assert "[legacy, replaced by source/p3x] — p2x bundle" in out
    assert "deprecated" not in out and "old static assets" not in out


def test_collapsed_chain_keeps_upper_keys_the_deeper_entry_leaves_unset():
    files = {"static/ls-p2x/src/a.py": OutlineFile(1.0, [(1.0, "a")])}
    areas = Areas({"static": Area(status="deprecated", note="old static assets"),
                   "static/ls-p2x": Area(note="p2x bundle")})
    out = render_outline(files, {}, 10_000, tokens, areas=areas)
    assert "[deprecated] — p2x bundle" in out
    assert "old static assets" not in out


def test_doc_globs_pick_doc_files_by_name():
    names = ["README.md", "CLAUDE.md", "api.README.md", "readme.md", "notes.txt", "README.rst", "src"]
    assert find_docs(names, ["README.md", "CLAUDE.md", "*.README.md"]) == ["CLAUDE.md", "README.md", "api.README.md", "readme.md"]
    assert find_docs(names, ["*.rst"]) == ["README.rst"]
    assert find_docs(names, []) == []
