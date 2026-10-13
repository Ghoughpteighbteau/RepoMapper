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
    out = render_outline(FILES, EDGES, 160, tokens)
    assert "lib/  2 files" in out and "user.ts" not in out
    assert "app.ts 20.0%: App" in out
    assert "legacy.js" not in out


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
