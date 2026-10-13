import subprocess
from pathlib import Path

from repomapper.utils import find_src_files, init_ignore_file


def rel_files(root):
    return sorted(str(Path(f).relative_to(root)) for f in find_src_files(str(root)))


def test_walks_directory_without_git(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "x.py").write_text("")

    assert rel_files(tmp_path) == ["src/a.py"]


def test_skips_gitignored_files(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("dist/\n")
    (tmp_path / "a.ts").write_text("")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "a.js").write_text("")

    assert rel_files(tmp_path) == ["a.ts"]


def test_single_file_path(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("")

    assert find_src_files(str(f)) == [str(f)]


def test_skips_vendor_directories(tmp_path):
    (tmp_path / "a.js").write_text("")
    (tmp_path / "js" / "vendor").mkdir(parents=True)
    (tmp_path / "js" / "vendor" / "lib.js").write_text("")

    assert rel_files(tmp_path) == ["a.js"]


def test_repomapignore_excludes_matching_files(tmp_path):
    (tmp_path / ".repomapignore").write_text("# generated\nutils/mcp/server.js\n*.bundle.js\n")
    (tmp_path / "a.ts").write_text("")
    (tmp_path / "x.bundle.js").write_text("")
    (tmp_path / "utils" / "mcp").mkdir(parents=True)
    (tmp_path / "utils" / "mcp" / "server.js").write_text("")
    (tmp_path / "utils" / "mcp" / "index.ts").write_text("")

    assert rel_files(tmp_path) == ["a.ts", "utils/mcp/index.ts"]


def test_repomapignore_applies_inside_git_repos(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".repomapignore").write_text("generated/\n")
    (tmp_path / "a.ts").write_text("")
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "b.ts").write_text("")

    assert rel_files(tmp_path) == ["a.ts"]


def test_repomapignore_is_read_from_ignore_root(tmp_path):
    (tmp_path / ".repomapignore").write_text("src/gen.ts\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("")
    (tmp_path / "src" / "gen.ts").write_text("")

    files = find_src_files(str(tmp_path / "src"), ignore_root=str(tmp_path))

    assert [Path(f).name for f in files] == ["a.ts"]


def test_init_ignore_file_writes_defaults_and_detected_minified(tmp_path):
    (tmp_path / "a.js").write_text("function a() {\n  return 1;\n}\n")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "dep-min.js").write_text(";".join(f"function f{i}(){{}}" for i in range(200)))

    assert init_ignore_file(str(tmp_path))

    lines = (tmp_path / ".repomapignore").read_text().splitlines()
    assert "vendor/" in lines
    assert "node_modules/" in lines
    assert "lib/dep-min.js" in lines
    assert "a.js" not in lines
    assert rel_files(tmp_path) == ["a.js"]


def test_init_ignore_file_leaves_existing_file_alone(tmp_path):
    (tmp_path / ".repomapignore").write_text("custom\n")

    assert not init_ignore_file(str(tmp_path))
    assert (tmp_path / ".repomapignore").read_text() == "custom\n"


def test_existing_ignore_file_replaces_defaults(tmp_path):
    (tmp_path / ".repomapignore").write_text("# nothing ignored\n")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "lib.js").write_text("")

    assert rel_files(tmp_path) == ["vendor/lib.js"]


def test_init_ignore_file_only_flags_parseable_files(tmp_path):
    (tmp_path / "data.json").write_text("{" + ",".join(f'"k{i}": {i}' for i in range(500)) + "}")

    init_ignore_file(str(tmp_path))

    assert "data.json" not in (tmp_path / ".repomapignore").read_text().splitlines()
