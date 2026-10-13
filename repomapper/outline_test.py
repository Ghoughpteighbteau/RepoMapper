from repomapper.areas import Area, Areas
from repomapper.config import OutlineConfig
from repomapper.outline import OutlineFile, render_outline
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
    out = render_outline(FILES, EDGES, 150, tokens)
    assert "lib/  2 files (ts 2) 70.0%: User, Task, UserId" in out
    assert "user.ts" not in out
    assert "ui/ -> lib/" in out


def test_directory_too_big_to_open_stays_folded_and_smaller_ones_open():
    out = render_outline(FILES, EDGES, 215, tokens)
    assert "lib/  2 files" in out and "user.ts" not in out
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
    assert "lib/  2 files" in out
    assert "user.ts" not in out


def test_directory_lines_show_git_activity():
    notes = {"lib": "3 commits/90d, last 2d", "front_end": "idle 3y"}
    out = render_outline(FILES, EDGES, 10_000, tokens, activity=notes.get)
    assert "lib/  2 files (ts 2) 70.0%, 3 commits/90d, last 2d" in out
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
    assert "ui/  8 files" in out


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
    flat = render_outline(files, {}, 500, tokens, areas=areas, importance={"core": 1.0, "normal": 1.0, "low": 1.0})
    tilted = render_outline(files, {}, 500, tokens, areas=areas)
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
    edges = {("lib/types/task.ts", "lib/types/user.ts"): 2.0, ("ui/app.ts", "lib/types/user.ts"): 1.0}
    out = render_outline(FILES, edges, 10_000, tokens)
    assert "  lib/types/: task.ts -> user.ts\n" in out
    assert "  ui/app.ts -> lib/types/user.ts" in out


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
    assert "a_test.py 50.0%\n" in large      # still listed, without names
    assert "test_real" not in large and "Dependencies" not in large


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
    assert "\n  tests/  10 files (py 10; 10 test) 10.0%\n" in out
    assert "unit/" not in out
    assert "src_0" in out
