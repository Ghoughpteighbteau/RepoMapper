# TODO

## Priorities (value / effort estimate)
Highest value per effort first. S = under an hour or two, M = half a day, L = a day or more.
1. Search token cuts (S, in progress): group hits by file, strip repeated paths from context lines, add an
   `N more hits` count when `max_results` truncated (the `services` agent can't trust search for "every usage"
   without it).
2. Outline leftovers from `services` feedback (S-M each): idle code expanded inside core areas, shared-types depth,
   sample noise (see "Outline leftovers").
3. Improve the performance of repomap on general queries (M, maybe L; see "Performance").
4. Workspace packages with a non-`src` main (`@marathon/core-steps`) (S): `main`/`types`/`module` entries and
   build dirs mapped to `src/` are handled; check on `services` whether seeded `lib/steps/core` now shows
   "used by api/red" lines, and fix the entry resolution if not.
5. Seeded trunk cost on deep paths (M): the 20% share is a floor, not a cap (see "Zoom step 2: leftovers").
6. Terser stat syntax (`125c 6d`, `2131f`) and header text moved into the tool description (M): user skipped it in
   the last round; revisit if the outline still needs to shrink. Changes the format agents and tests rely on.
7. New tools `callers(symbol)` / `file_summary(path)` (M).
8. Import edges for other languages (Python, Java/Kotlin, C) (L).
9. Measure the map on real tasks (M): 5-10 past tasks with/without it; files opened before the right one, tokens,
   whether legacy code was edited by mistake (suggested by the `services` agent).

## Performance (priority 3)
Improve the performance of repomap on general queries. Known from the worklog: ~11-13s on `services`, ~6s warm on
django; graph build and ranking dominate; the outline builds the tree twice (merged and unmerged); the first run
after a cache fingerprint change is cold at 50-90s. Details and targets will come from the profile another agent
is running; fill in here when it reports.

## Output size and token efficiency
- Built: terse file/dir lines, language and test counts only on truncated dirs, 1% PageRank cutoff, merged
  near-single-child dirs, nameless files unlisted (see worklog). Unseeded outline ~4k tokens on `services` at
  `--map-tokens 4096`; the agent measured ~8k at the default budget.
- MCP responses repeat the generic ignore-file hint on every call (what init created is reported once); send it
  only when results look generated.
- Dogfooding: in signature blocks (seeded maps), test files list every test function and big classes list every
  method. Cap definitions per file, collapse test files to one line.

## New tools: targeted lookups
What an agent asks most is "who uses X?" and "what is this file?". Consider `callers(symbol)` returning
`file:line` per reference, and `file_summary(path)` returning a file's definitions plus its importers.
These replace several greps and are cheaper than a full map.

## Explore: reduce pygments reference noise
Unseeded maps (before the outline) had `id`, `key`, `data`, `result`, `randomUUID` at the top; the outline's sample
names now filter by definer counts, so this mostly matters for seeded maps and ranking. Splitting edge weight
across definers just swapped `id` for `key`/`data`/`date`. A stoplist or minimum length for pygments refs
(tree-sitter refs kept) is the next try. Merging every pygments name token adds many edges for generic names
(`get`, `set`, `id`, `value`): ~1.47M references on `services`; check whether a stoplist or minimum name length
improves maps and shrinks the cache.
Also: minified detection for `.repomap/ignore` only runs when the file is created, so later minified files are
missed there (the outline skips mostly-single-letter files on its own).

## Import-based edges: leftovers
JS/TS import edges are built (see worklog). Not done: other languages (Python `import`, Java/Kotlin packages, C `#include`),
tsconfig `paths`/`baseUrl` aliases and `exports` maps in package.json, the `@marathon/core-steps` case (see
priority 4), edges between separately deployed services unless explicit, and regex-vs-tree-sitter
(an `import` at line start inside a template string would match). Seeded maps barely use import edges yet (chat-file
reference edges weigh 50 vs 10 per import): consider a rank boost for imported files' definitions.

## `.repomap/` leftovers
- README/CLAUDE.md first sentence as a live default `note` (only written into the starter file today).
- `*.properties` config glob and the `*_spec.*` globs are starter defaults, but existing `areas.toml` files (the
  user's `services` one) don't have them; `moved_tests_hint` only covers the old `[tests]` section.

## Outline leftovers (from `services` dogfooding)
Built since the first round: priority-queue allocator, test/config demotion, sample scoring, non-code dirs, edge
stopgaps and import edges, doc_globs, merged chains (see worklog). Still open:
- Idle code inside core areas still gets file lines (`api/flex` `search_filter/`, `join_record/`, `account_event/`
  idle >2y); fold idle subtrees of a core area, or list their names on one line.
- Shared types (`lib/types`, 42.9% rank from being referenced everywhere) take ~60 lines: cap depth, or let
  activity count more against reference-driven rank.
- Sample noise left: `Data`, `Create`, `Metadata`, `Reports`, `isLoading`, `setValue`, `Initialize`, `isHidden`;
  `utils/testing` still samples `forEach`. The 3-sibling / 12-file thresholds are guesses; tune on `services`.
- Pure asset dirs (png/svg/scss) show as count lines; consider a binary-extension filter.
- `ui/src/app/ ... (+142 files outside)`: the outside count hides ~134 non-code files; lowering
  `outline.merge_dir_share` merges more, raising it shows more.
- Non-code dir limits (3 files, 12 lines, 15%) and `MIN_SHOWN_RANK`, `HOT_FILE_COMMITS`, `FIRST_LEVEL_DIRS`,
  `MIN_LEAN_DIRS`, `PLAIN_*` are module constants; move to config.toml if they need per-repo tuning.
- `services` areas.toml (user's repo, untracked): `static/` deprecated (per CLAUDE.md), notes for `helm/`
  (prototype), `source/red`, a flyway note, plus the spec/properties globs above.
- `core_importance` (4) against sqrt(files) in the allocator is untuned.
- Performance: see "Performance".
- Dropped by the user: naming hidden dirs (`+N more dirs`), relabelling counts as parsed files.

## Git history signals: leftovers
History pass, activity seeds, outline notes and the areas.py migration are built (see worklog). Not done:
- Does the activity tilt (`git.max_activity_boost`, default 3) move the ranking too much or too little?
- Co-change edges: files committed together are related even without shared names (a migration and its
  repository, a component and its template). Candidate for seeded maps: "files that change with this one".
  Overlaps with import-based edges; compare on `services`. `History.commits` already has what's needed.
- Lock files/changelogs aren't filtered from history: they're unparseable so never ranked or outlined; add a
  filter if co-change edges use them.
- Cold `git log` time on a large, non-shallow repo is unmeasured (django checkout here is shallow).

## Zoom by context, step 2: leftovers
Built (see worklog). Not done:
- Trunk cost on deep paths: the forced path (root to the seeded directory, every sibling folded with docs and notes) took ~1.7k of 4096 tokens on
  `services`, so the 20% share is a floor, not a cap; shorten sibling lines on the path (no docs lists) or cap siblings per level.
- Seeded files' own directory lists no file lines (blocks cover the top-ranked; unlisted files only count in the summary).
- Leftover trunk budget is not handed back to the leaves, and an unused leaf budget is not handed to the trunk.
- Idents seed only the 5 best-ranked defining files; a directory focus has no depth/file cap beyond the token budget.
- Blank lines inside blocks may still cost tokens (unchecked).
