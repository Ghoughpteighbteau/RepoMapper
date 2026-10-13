import tomllib

from repomapper.repomap_dir import init_repomap_dir


def test_init_creates_every_file_and_cache_dir(tmp_path):
    created = init_repomap_dir(str(tmp_path))

    assert sorted(created) == [".repomap/.gitignore", ".repomap/areas.toml", ".repomap/config.toml", ".repomap/ignore"]
    assert (tmp_path / ".repomap" / "cache").is_dir()
    assert (tmp_path / ".repomap" / ".gitignore").read_text().splitlines()[-1] == "cache/"


def test_templates_are_valid_toml(tmp_path):
    init_repomap_dir(str(tmp_path))

    assert set(tomllib.loads((tmp_path / ".repomap" / "areas.toml").read_text())) == {"globs"}
    assert "ranking" in tomllib.loads((tmp_path / ".repomap" / "config.toml").read_text())


def test_init_only_creates_missing_files(tmp_path):
    (tmp_path / ".repomap").mkdir()
    (tmp_path / ".repomap" / "areas.toml").write_text("# mine\n")

    created = init_repomap_dir(str(tmp_path))

    assert ".repomap/areas.toml" not in created
    assert (tmp_path / ".repomap" / "areas.toml").read_text() == "# mine\n"
    assert init_repomap_dir(str(tmp_path)) == []
