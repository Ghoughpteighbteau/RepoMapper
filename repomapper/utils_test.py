import subprocess
from pathlib import Path

from repomapper.utils import find_src_files


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
