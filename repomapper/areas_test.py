from repomapper.areas import Area, Areas, annotation, load_areas
from repomapper.repomap_dir import AREAS_TEMPLATE


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


def test_covering_lists_entries_over_the_given_files():
    areas = Areas({"maps": Area(status="legacy"), "src": Area(importance="core"), "ui": Area(note="x")})

    assert areas.covering(["maps/a.py", "src/b.py"]) == ["maps"]
