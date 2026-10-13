import os
import re
import subprocess

from repomapper.areas import Area, Areas, annotation, load_areas, readme_sentence, stale_entries
from repomapper.repomap_dir import AREAS_TEMPLATE, init_repomap_dir


def write_areas(root, text):
    (root / ".repomap").mkdir(exist_ok=True)
    (root / ".repomap" / "areas.toml").write_text(text)


def test_template_has_no_entries_or_warnings(tmp_path):
    write_areas(tmp_path, AREAS_TEMPLATE)

    areas = load_areas(str(tmp_path))

    assert areas.entries == {} and areas.warnings == []


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
    commit_file(tmp_path, "src/a.py", "x = 0\n", date="2024-01-01T00:00:00")
    commit_file(tmp_path, ".repomap/areas.toml", '[paths."src"]\nnote = "x"\n', date="2024-01-02T00:00:00")
    for i in range(3):
        commit_file(tmp_path, f"src/b{i}.py", "x = 1\n", date=f"2024-02-0{i + 1}T00:00:00")
    areas = load_areas(str(tmp_path))

    stale = stale_entries(str(tmp_path), areas, 3)
    assert len(stale) == 1 and 'paths."src" was last edited 2024-01-02, 3 commits' in stale[0]
    assert stale_entries(str(tmp_path), areas, 4) == []
