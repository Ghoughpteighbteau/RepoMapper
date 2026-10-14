import json

from repomapper.imports import ImportResolver, extract_imports


def specs(code):
    return [spec for spec, _line in extract_imports(code)]


def test_extracts_static_dynamic_and_commonjs_imports_with_lines():
    code = (
        "import a from './a';\n"
        "import { b,\n  c } from \"../b\";\n"
        "import type { T } from '@opsready/types';\n"
        "import './side-effect';\n"
        "export * from './barrel';\n"
        "export { d } from './d'\n"
        "const e = require('./e');\n"
        "const f = await import('./f');\n"
    )
    assert extract_imports(code) == [
        ("./a", 1), ("../b", 2), ("@opsready/types", 4), ("./side-effect", 5), ("./barrel", 6), ("./d", 7),
        ("./e", 8), ("./f", 9)]


def test_ignores_strings_that_only_look_like_imports():
    code = "const s = 'import x from \"./nope\"';\nexport const from = 1;\nlet y = \"from './no'\";\n"
    assert specs(code) == []


def make(files, packages=None, barrels=None):
    all_files = set(files)
    root_files = {**{f: "" for f in files}, **(barrels or {})}
    return ImportResolver(all_files, lambda rel: root_files.get(rel), packages or {})


def test_relative_imports_try_extensions_and_index_files():
    r = make(["src/a.ts", "src/b/index.tsx", "src/c.js", "src/d.mjs", "lib/e.ts", "src/f.ts"])
    assert r.resolve("src/main.ts", "./a") == ["src/a.ts"]
    assert r.resolve("src/main.ts", "./b") == ["src/b/index.tsx"]
    assert r.resolve("src/main.ts", "./c") == ["src/c.js"]
    assert r.resolve("src/main.ts", "./d") == ["src/d.mjs"]
    assert r.resolve("src/x/main.ts", "../../lib/e") == ["lib/e.ts"]
    assert r.resolve("src/main.ts", "./f.ts") == ["src/f.ts"]
    assert r.resolve("src/main.ts", "./f.js") == ["src/f.ts"]   # ESM style TypeScript
    assert r.resolve("src/main.ts", "./missing") == []
    assert r.resolve("src/main.ts", "rxjs") == []
    assert r.resolve("src/main.ts", "../../outside") == []


def test_workspace_packages_resolve_to_source_entry():
    files = ["lib/types/src/index.ts", "lib/types/src/user.ts", "lib/lone/package.json", "lib/lone/lib/main.ts"]
    packages = {"@opsready/types": ("lib/types", {"main": "./dist/index.js", "types": "./dist/index.d.ts"}),
                "plain": ("lib/lone", {"main": "lib/main.js"})}
    r = make(files, packages)
    assert r.resolve("ui/a.ts", "@opsready/types") == ["lib/types/src/index.ts"]
    assert r.resolve("ui/a.ts", "@opsready/types/src/user") == ["lib/types/src/user.ts"]
    assert r.resolve("ui/a.ts", "@opsready/types/user") == ["lib/types/src/user.ts"]
    assert r.resolve("ui/a.ts", "plain") == ["lib/lone/lib/main.ts"]
    assert r.resolve("ui/a.ts", "@opsready/unknown") == []


def test_imports_of_a_barrel_follow_its_reexports():
    files = ["t/src/user.ts", "t/src/task.ts", "t/src/deep/x.ts"]
    barrels = {"t/src/index.ts": "export * from './user';\nexport { T } from './deep/index';\n",
               "t/src/deep/index.ts": "export * from './x';\nexport * from '../index';\n"}
    r = make(files, {"@t/t": ("t", {})}, barrels)
    assert sorted(r.resolve("ui/a.ts", "@t/t")) == ["t/src/deep/x.ts", "t/src/user.ts"]   # cycle terminates


def test_load_packages_reads_names_and_entries(tmp_path):
    from repomapper.imports import load_packages
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "package.json").write_text(json.dumps({"name": "@o/lib", "main": "dist/i.js"}))
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "package.json").write_text("{nope")
    (tmp_path / "anon").mkdir()
    (tmp_path / "anon" / "package.json").write_text("{}")
    got = load_packages(str(tmp_path), ["lib/package.json", "bad/package.json", "anon/package.json", "x.ts"])
    assert got == {"@o/lib": ("lib", {"main": "dist/i.js"})}
