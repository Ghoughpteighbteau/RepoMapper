import os
import re
import subprocess
from datetime import datetime, timedelta, timezone

from repomapper.config import GitConfig
from repomapper.areas import Area, Areas, annotation, load_areas, readme_sentence, stale_entries
from repomapper.repomap_dir import AREAS_TEMPLATE, init_repomap_dir


def write_areas(root, text):
    (root / ".repomap").mkdir(exist_ok=True)
    (root / ".repomap" / "areas.toml").write_text(text)


def test_template_has_no_path_entries_or_warnings(tmp_path):
    write_areas(tmp_path, AREAS_TEMPLATE)

    areas = load_areas(str(tmp_path))

    assert areas.entries == {} and areas.warnings == []
    assert "*_test.*" in areas.globs


def test_nearest_entry_wins_per_key():
    areas = Areas({
        "maps": Area(importance="low", status="legacy", fold=True, note="GIS"),
        "maps/api": Area(fold=False),
    })

    child = areas.resolve("maps/api/views.py")

    assert (child.importance, child.status, child.fold, child.note) == ("low", "legacy", False, "GIS")
    assert areas.resolve("src/x.py") == Area(importance="normal", status="active", fold=False)


def test_paths_are_normalized(tmp_path):
    (tmp_path / "src").mkdir()
    write_areas(tmp_path, '[paths."./src/"]\nimportance = "core"\n')

    assert load_areas(str(tmp_path)).resolve("src/a.py").importance == "core"


def test_bad_entries_are_reported_and_skipped(tmp_path):
    (tmp_path / "src").mkdir()
    write_areas(tmp_path, '[paths."src"]\nimportance = "huge"\ncolour = "red"\nfold = "yes"\nnote = "kept"\n'
                          '[paths."gone"]\nnote = "x"\n')

    areas = load_areas(str(tmp_path))

    assert areas.entries["src"] == Area(note="kept")
    assert len(areas.warnings) == 4
    assert any("'gone'" in w or '"gone"' in w for w in areas.warnings)


def test_annotation_shows_status_replacement_and_note():
    assert annotation(Area(status="legacy", replaced_by="src/api", note="GIS")) == " [legacy, replaced by src/api] — GIS"
    assert annotation(Area(importance="core")) == ""


def git(root, *args, date=None):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, env=env)


def commit_file(root, rel, text, date=None):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git(root, "add", rel)
    git(root, "commit", "-q", "-m", rel, date=date)


def test_readme_sentence_skips_headings_and_badges(tmp_path):
    (tmp_path / "README.md").write_text("# Title\n\n[![ci](x)](y)\n\nThe REST backend. More detail here.\n")

    assert readme_sentence(str(tmp_path)) == "The REST backend."


def test_starter_entries_describe_areas_and_suggest_idle_ones_as_legacy(tmp_path):
    git(tmp_path, "init", "-q")
    commit_file(tmp_path, "old/a.py", "x = 1\n", date="2020-01-01T00:00:00")
    commit_file(tmp_path, "src/b.ts", "let y = 1;\n")
    commit_file(tmp_path, "src/README.md", 'Main "app" code.\n')
    commit_file(tmp_path, "top.py", "z = 1\n")

    init_repomap_dir(str(tmp_path))
    text = (tmp_path / ".repomap" / "areas.toml").read_text()

    assert "# old: 1 files (py 1), last commit 2020-01-01" in text
    assert '# status = "legacy"  # suggestion only: no commits since 2020-01-01' in text
    assert '# note = "Main \\"app\\" code."' in text
    assert "top.py" not in text
    # Uncommented, the entries are valid
    generated = text[len(AREAS_TEMPLATE):]
    uncommented = re.sub(r"^# (\[paths|importance|status|note)", r"\1", generated, flags=re.M)
    write_areas(tmp_path, uncommented)
    areas = load_areas(str(tmp_path))
    assert areas.warnings == []
    assert areas.entries["old"].status == "legacy"
    assert areas.entries["src"].note == 'Main "app" code.'


def test_stale_entries_counts_commits_since_entry_was_edited(tmp_path):
    git(tmp_path, "init", "-q")
    def days_ago(n):
        return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%S+0000")

    commit_file(tmp_path, "src/a.py", "x = 0\n", date=days_ago(40))
    commit_file(tmp_path, ".repomap/areas.toml", '[paths."src"]\nnote = "x"\n', date=days_ago(30))
    for i in range(3):
        commit_file(tmp_path, f"src/b{i}.py", "x = 1\n", date=days_ago(20 - i))
    commit_file(tmp_path, "other/c.py", "x = 1\n", date=days_ago(10))
    areas = load_areas(str(tmp_path))
    edited = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()

    stale = stale_entries(str(tmp_path), areas, GitConfig(stale_area_commits=3))
    assert len(stale) == 1 and f'paths."src" was last edited {edited}, 3 commits' in stale[0]
    assert stale_entries(str(tmp_path), areas, GitConfig(stale_area_commits=4)) == []


def test_glob_entries_apply_to_matching_files_and_beat_folder_entries(tmp_path):
    (tmp_path / "pkg").mkdir()
    write_areas(tmp_path, '[paths."pkg"]\nimportance = "core"\nnote = "the package"\n'
                          '[globs."*_test.*"]\nimportance = "low"\nkind = "test"\n')

    areas = load_areas(str(tmp_path))
    test = areas.resolve("pkg/sub/a_test.py")

    assert areas.warnings == []
    assert (test.importance, test.kind, test.note) == ("low", "test", "the package")
    assert areas.resolve("pkg/a.py").importance == "core" and areas.resolve("pkg/a.py").kind is None


def test_exact_path_entry_beats_glob(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "x_test.py").write_text("")
    write_areas(tmp_path, '[paths."pkg/x_test.py"]\nimportance = "core"\n'
                          '[globs."*_test.*"]\nimportance = "low"\nkind = "test"\n')

    resolved = load_areas(str(tmp_path)).resolve("pkg/x_test.py")

    assert (resolved.importance, resolved.kind) == ("core", "test")


def test_glob_syntax_anchors_paths_with_slashes():
    areas = Areas(globs={"tests/**": Area(kind="test"), "**/__tests__/**": Area(kind="test"),
                         "karma*.conf.js": Area(kind="config")})

    assert areas.resolve("tests/a/b.py").kind == "test"
    assert areas.resolve("tests").kind == "test"          # the folder itself, so the outline demotes it
    assert areas.resolve("src/tests/b.py").kind is None   # a slash anchors the pattern at the root
    assert areas.resolve("src/x/__tests__/a.js").kind == "test"
    assert areas.resolve("ui/karma.conf.js").kind == "config"   # no slash: matches the file name anywhere
    assert areas.resolve("ui/src/app.js").kind is None


def test_bad_kind_is_reported(tmp_path):
    write_areas(tmp_path, '[globs."*.spec.*"]\nkind = "spec"\n')

    areas = load_areas(str(tmp_path))

    assert areas.globs == {"*.spec.*": Area()}
    assert len(areas.warnings) == 1 and "kind" in areas.warnings[0]


def test_template_globs_demote_tests_and_configs(tmp_path):
    write_areas(tmp_path, AREAS_TEMPLATE)

    areas = load_areas(str(tmp_path))

    assert areas.resolve("src/app.spec.ts").kind == "test"
    assert areas.resolve("src/app.spec.ts").importance == "low"
    assert areas.resolve("pkg/test_x.py").kind == "test"
    assert areas.resolve("ui/eslint.config.mjs").kind == "config"
    assert areas.resolve("ui/karma.conf.js").kind == "config"
    assert areas.resolve("docker-bake.hcl").kind == "config"
    assert areas.resolve("src/app.ts").kind is None
