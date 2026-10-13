import subprocess
from pathlib import Path

from repomapper.utils import find_src_files
from repomapper.repomap_dir import init_repomap_dir


def init_ignore_file(root):
    """Run init and say whether it wrote .repomap/ignore."""
    return ".repomap/ignore" in init_repomap_dir(root)


def write_ignore(root, text):
    (root / ".repomap").mkdir(exist_ok=True)
    (root / ".repomap" / "ignore").write_text(text)


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


def test_ignore_file_excludes_matching_files(tmp_path):
    write_ignore(tmp_path, "# generated\nutils/mcp/server.js\n*.bundle.js\n")
    (tmp_path / "a.ts").write_text("")
    (tmp_path / "x.bundle.js").write_text("")
    (tmp_path / "utils" / "mcp").mkdir(parents=True)
    (tmp_path / "utils" / "mcp" / "server.js").write_text("")
    (tmp_path / "utils" / "mcp" / "index.ts").write_text("")

    assert rel_files(tmp_path) == ["a.ts", "utils/mcp/index.ts"]


def test_ignore_file_applies_inside_git_repos(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    write_ignore(tmp_path, "generated/\n")
    (tmp_path / "a.ts").write_text("")
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "b.ts").write_text("")

    assert rel_files(tmp_path) == ["a.ts"]


def test_ignore_file_is_read_from_ignore_root(tmp_path):
    write_ignore(tmp_path, "src/gen.ts\n")
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

    lines = (tmp_path / ".repomap" / "ignore").read_text().splitlines()
    assert "vendor/" in lines
    assert "node_modules/" in lines
    assert "lib/dep-min.js" in lines
    assert "a.js" not in lines
    assert rel_files(tmp_path) == ["a.js"]


def test_init_ignore_file_leaves_existing_file_alone(tmp_path):
    write_ignore(tmp_path, "custom\n")

    assert not init_ignore_file(str(tmp_path))
    assert (tmp_path / ".repomap" / "ignore").read_text() == "custom\n"


def test_existing_ignore_file_replaces_defaults(tmp_path):
    write_ignore(tmp_path, "# nothing ignored\n")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "lib.js").write_text("")

    assert rel_files(tmp_path) == ["vendor/lib.js"]


def test_init_ignore_file_only_flags_parseable_files(tmp_path):
    (tmp_path / "data.json").write_text("{" + ",".join(f'"k{i}": {i}' for i in range(500)) + "}")

    init_ignore_file(str(tmp_path))

    assert "data.json" not in (tmp_path / ".repomap" / "ignore").read_text().splitlines()


def ignore_sections(root):
    """Map each '# ...' heading in .repomap/ignore to the patterns listed under it."""
    sections, current = {}, None
    for line in (root / ".repomap" / "ignore").read_text().splitlines():
        if line.startswith("#"):
            current = sections.setdefault(line, [])
        elif line and current is not None:
            current.append(line)
    return sections


def section_with(sections, word):
    return next(patterns for heading, patterns in sections.items() if "Detected" in heading and word in heading)


def test_init_flags_rollup_bundles(tmp_path):
    body = "\n".join(f"function create$${i}() {{ return process$1; }}" for i in range(30))
    (tmp_path / "server.js").write_text(f"import process$1 from 'node:process';\n{body}\n")
    (tmp_path / "price.js").write_text("const label = 'costs $1';\n")

    init_ignore_file(str(tmp_path))

    assert section_with(ignore_sections(tmp_path), "bundle") == ["server.js"]


def test_init_flags_webpack_bundles(tmp_path):
    (tmp_path / "app.js").write_text("var x = __webpack_require__(12);\n")

    init_ignore_file(str(tmp_path))

    assert section_with(ignore_sections(tmp_path), "bundle") == ["app.js"]


def test_init_flags_very_large_files(tmp_path):
    (tmp_path / "big.py").write_text("x = 1\n" * 10_001)
    (tmp_path / "small.py").write_text("x = 1\n" * 100)

    init_ignore_file(str(tmp_path))

    assert section_with(ignore_sections(tmp_path), "large") == ["big.py"]


def test_init_flags_js_barrels(tmp_path):
    (tmp_path / "index.ts").write_text(
        "// public API\n"
        "export * from './a';\n"
        "export { b, c as d } from './b';\n"
        "export type { T } from './types';\n"
        "import { e } from './e';\n"
        "export {\n  e,\n};\n"
    )
    (tmp_path / "a.ts").write_text("import { x } from './x';\nexport const a = x + 1;\n")
    (tmp_path / "imports.ts").write_text("import './polyfill';\nimport { y } from './y';\n")
    (tmp_path / "__init__.py").write_text("from .a import *\n")

    init_ignore_file(str(tmp_path))

    assert section_with(ignore_sections(tmp_path), "barrel") == ["index.ts"]



def test_init_flags_files_whose_definitions_are_mostly_single_letters(tmp_path):
    body = "\n".join(f"function {chr(97 + i)}(t, e) {{\n  return t + e;\n}}" for i in range(15))
    (tmp_path / "lib.js").write_text(body + "\nfunction format(x) {\n  return x;\n}\n")
    (tmp_path / "app.js").write_text("function a(x) {}\nfunction render(x) {}\nfunction main() {}\n")

    init_ignore_file(str(tmp_path))

    assert section_with(ignore_sections(tmp_path), "minified") == ["lib.js"]


def test_mostly_single_letters():
    from repomapper.utils import mostly_single_letters
    assert mostly_single_letters(list("abcdefghij") + ["format"])
    assert not mostly_single_letters(["a", "b", "c"])           # too few to judge
    assert not mostly_single_letters(list("abcde") + [f"name{i}" for i in range(10)])
