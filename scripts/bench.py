"""Benchmark RepoMapper speed and output size, and compare two result files.

    uv run python scripts/bench.py                       # this repo, 3 runs
    uv run python scripts/bench.py --repo all --cold     # + services, django, cold tags cache
    uv run python scripts/bench.py --compare OLD.json NEW.json

Targets: this repo, services (SERVICES_ROOT, default ~/dev/ls/services), django (DJANGO_ROOT, default
~/src/django). A missing target or a missing seed is skipped with a note. The seeds are the CASES dict below.

Time cases (wall seconds, in process, same RepoMap calls as the CLI; median and min over --runs):
  time.unseeded_nocache   unseeded outline with [map] ranking_cache off (tags cache warm)
  time.unseeded_hit       unseeded outline, ranking.v1 hit (one untimed-for-stats warm-up, kept as "first")
  time.seeded_chat / seeded_dir / seeded_idents   seeded maps, base-graph hit (warm-up kept as "first")
  time.search             search_identifiers for one name
  time.cold_unseeded      (--cold only, one run) empty scratch tags cache, ranking cache off. The scratch dir is
                          a temp directory via the TAGS_CACHE_DIR override; the real caches are never touched.
Size cases: size.<case>@4096 and @20000 as {tokens, lines} (gpt-4 token counter), size.search as {chars, more_hits}.

Output: JSON with "meta" and one key per target holding the cases. Default path
bench/results/<date>-<sha>[-dirty].json (commit those); if any target other than this repo ran, the name gets the targets
and ends in -services.local.json etc. instead (gitignored: services/django depend on your working tree). Run `--repo this` and
`--repo services` separately to get both. --compare prints a table, flags regressions above 10%, exits 0.
The parse pool re-imports __main__, hence the main guard.
"""

import argparse
import asyncio
import datetime
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUDGETS = (4096, 20000)
REGRESSION = 0.10

# Per target: chat file, mentioned directory, mentioned identifiers, search name (paths relative to the root).
CASES = {
    "this": {
        "chat": "repomapper/outline.py",
        "dir": "repomapper",
        "idents": ["RepoMap", "count_tokens", "load_config"],
        "search": "get_tags",
    },
    "services": {
        "chat": "source/lib/steps/core/src/task/task.ts",
        "dir": "source/lib/steps/core/src/task",
        "idents": ["TaskDescriptor", "TaskData"],
        "search": "Task",
    },
    "django": {
        "chat": "django/db/models/query.py",
        "dir": "django/db/models/sql",
        "idents": ["QuerySet", "Manager"],
        "search": "QuerySet",
    },
}


def target_roots():
    return {
        "this": str(REPO_ROOT),
        "services": os.path.expanduser(os.environ.get("SERVICES_ROOT", "~/dev/ls/services")),
        "django": os.path.expanduser(os.environ.get("DJANGO_ROOT", "~/src/django")),
    }


# ---- pure helpers (tested in bench_test.py) ----

def summarize(times):
    return {"median": round(statistics.median(times), 3), "min": round(min(times), 3),
            "runs": [round(t, 3) for t in times]}


def result_name(date, sha, dirty, others=()):
    """`others`: targets other than this repo that ran; they go in the name, which then ends in .local.json."""
    tail = "".join(f"-{t}" for t in others) + ".local" if others else ""
    return f"{date}-{sha}{'-dirty' if dirty else ''}{tail}.json"


def flatten(data):
    """{target: {case: ...}} -> {"target/case[.metric]": number}. Timings use the median."""
    flat = {}
    for target, cases in data.items():
        if target == "meta":
            continue
        for case, value in cases.items():
            if "median" in value:
                flat[f"{target}/{case}"] = value["median"]
            else:
                for metric, number in value.items():
                    flat[f"{target}/{case}.{metric}"] = number
    return flat


def compare_table(a, b):
    fa, fb = flatten(a), flatten(b)
    rows = [("case", "A", "B", "change")]
    for key in sorted(set(fa) | set(fb)):
        if key not in fb:
            rows.append((key, str(fa[key]), "-", "only in A"))
        elif key not in fa:
            rows.append((key, "-", str(fb[key]), "only in B"))
        else:
            old, new = fa[key], fb[key]
            change = (new - old) / old if old else (0.0 if new == 0 else float("inf"))
            flag = "   <<< REGRESSION" if change > REGRESSION else ""
            rows.append((key, str(old), str(new), f"{change * 100:+.1f}%{flag}"))
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    return "\n".join(f"{r[0]:<{widths[0]}}  {r[1]:>{widths[1]}}  {r[2]:>{widths[2]}}  {r[3]}" for r in rows)


# ---- git metadata ----

def git(root, *args):
    try:
        out = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=60)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def git_state(root):
    return {"sha": git(root, "rev-parse", "--short", "HEAD"),
            "dirty": bool(git(root, "status", "--porcelain", "--untracked-files=no"))}


# ---- running the library ----

def build_map(root, seed, budget, ranking_cache=True):
    from repomapper.config import load_config
    from repomapper.areas import load_areas
    from repomapper.repomap_class import RepoMap
    from repomapper.utils import count_tokens, read_text, find_src_files

    config, areas = load_config(root), load_areas(root)
    config.map.ranking_cache = ranking_cache
    others = find_src_files(root)
    chat = [str(Path(root) / f) for f in seed.get("chat", [])]
    others = [f for f in others if f not in set(chat)]
    rm = RepoMap(map_tokens=budget, root=root, token_counter_func=lambda t: count_tokens(t, "gpt-4"),
                 file_reader_func=read_text, output_handler_funcs={"info": lambda *a: None,
                 "warning": lambda *a: None, "error": lambda *a: None},
                 config=config, areas=areas)
    text, _ = rm.get_repo_map(chat_files=chat, other_files=others,
                              mentioned_fnames=set(seed["files"]) if seed.get("files") else None,
                              mentioned_idents=set(seed["idents"]) if seed.get("idents") else None)
    return text or ""


def clear_memos():
    from repomapper import ranking_cache
    ranking_cache.memo_clear()


def search(root, name):
    from repomapper import repomap_server
    repomap_server.PROJECT_ROOT = root
    return asyncio.run(repomap_server.search_identifiers(name))


def timed(fn):
    start = time.perf_counter()
    result = fn()
    return time.perf_counter() - start, result


def run_cases(name, root, runs, cold, note):
    from repomapper import repomap_class
    from repomapper.utils import count_tokens

    cfg = CASES[name]
    seeds = {"unseeded": {}}
    if os.path.isfile(os.path.join(root, cfg["chat"])):
        seeds["seeded_chat"] = {"chat": [cfg["chat"]]}
    else:
        note(f"{name}: chat file {cfg['chat']} missing, skipping seeded_chat")
    if os.path.isdir(os.path.join(root, cfg["dir"])):
        seeds["seeded_dir"] = {"files": [cfg["dir"]]}
    else:
        note(f"{name}: directory {cfg['dir']} missing, skipping seeded_dir")
    seeds["seeded_idents"] = {"idents": cfg["idents"]}

    out = {}
    small = BUDGETS[0]

    # unseeded, caches off: every run recomputes the ranking
    times = []
    for _ in range(runs):
        clear_memos()
        times.append(timed(lambda: build_map(root, {}, small, ranking_cache=False))[0])
    out["time.unseeded_nocache"] = summarize(times)

    # hit cases: first run (may be a miss) is kept apart, the timed runs read the disk caches
    for case, seed in seeds.items():
        label = "time.unseeded_hit" if case == "unseeded" else f"time.{case}"
        first = timed(lambda: build_map(root, seed, small))[0]
        times = []
        for _ in range(runs):
            clear_memos()
            times.append(timed(lambda: build_map(root, seed, small))[0])
        out[label] = {**summarize(times), "first": round(first, 3)}

    first = timed(lambda: search(root, cfg["search"]))[0]
    times, result = [], None
    for _ in range(runs):
        t, result = timed(lambda: search(root, cfg["search"]))
        times.append(t)
    out["time.search"] = {**summarize(times), "first": round(first, 3)}

    if cold:
        saved = repomap_class.TAGS_CACHE_DIR
        with tempfile.TemporaryDirectory(prefix="bench-tags-") as scratch:
            repomap_class.TAGS_CACHE_DIR = os.path.join(scratch, "tags")  # absolute: the real cache is untouched
            try:
                clear_memos()
                out["time.cold_unseeded"] = summarize([timed(
                    lambda: build_map(root, {}, small, ranking_cache=False))[0]])
            finally:
                repomap_class.TAGS_CACHE_DIR = saved

    for case, seed in seeds.items():
        for budget in BUDGETS:
            text = build_map(root, seed, budget)
            out[f"size.{case}@{budget}"] = {"tokens": count_tokens(text, "gpt-4"), "lines": len(text.splitlines())}
    out["size.search"] = {"chars": len(json.dumps(result.get("results", []))), "more_hits": result.get("more_hits", 0)}
    return out


def metadata(targets, roots, args):
    from repomapper.repomap_class import extractor_fingerprint
    from repomapper.config import load_config
    from repomapper.utils import find_src_files

    here = git_state(str(REPO_ROOT))
    meta = {"date": datetime.date.today().isoformat(), "sha": here["sha"], "dirty": here["dirty"],
            "python": platform.python_version(), "cpu_count": os.cpu_count(),
            "extractor_fingerprint": extractor_fingerprint(), "runs": args.runs, "cold": args.cold,
            "targets": {}}
    for name in targets:
        config = load_config(roots[name])
        meta["targets"][name] = {**git_state(roots[name]), "files": len(find_src_files(roots[name])),
                                 "parse_workers": config.map.parse_workers,
                                 "ranking_cache": config.map.ranking_cache}
    return meta


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", choices=["this", "services", "django", "all"], default="this")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--out", help="result path (default bench/results/<date>-<sha>[-dirty][.local].json)")
    p.add_argument("--cold", action="store_true", help="also time a cold tags cache (slow on services)")
    p.add_argument("--compare", nargs=2, metavar=("A", "B"), help="print a table comparing two result files")
    args = p.parse_args(argv)

    if args.compare:
        a, b = (json.loads(Path(f).read_text()) for f in args.compare)
        print(compare_table(a, b))
        return 0

    def note(msg):
        print(f"note: {msg}", file=sys.stderr)

    roots = target_roots()
    wanted = ["this", "services", "django"] if args.repo == "all" else [args.repo]
    targets = []
    for name in wanted:
        if os.path.isdir(roots[name]):
            targets.append(name)
        else:
            note(f"{name} not found at {roots[name]}, skipping")
    if not targets:
        return 1

    result = {"meta": metadata(targets, roots, args)}
    for name in targets:
        print(f"== {name} ({roots[name]})", file=sys.stderr)
        result[name] = run_cases(name, roots[name], max(1, args.runs), args.cold, note)
        for case, value in result[name].items():
            print(f"  {case}: {value}", file=sys.stderr)

    meta = result["meta"]
    out = Path(args.out) if args.out else REPO_ROOT / "bench" / "results" / result_name(
        meta["date"], meta["sha"], meta["dirty"], [t for t in targets if t != "this"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
