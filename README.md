# rot-finder

`fleet status` tells you what's failing. This tells you what looks fine and isn't.

Four detectors, all deterministic (no LLM -- cheap enough to run weekly):

| Section | What it finds | How |
|---|---|---|
| Gone quiet | Tools that stopped running without ever erroring | Collapses `processing_log` runs to distinct days, takes each tool's median gap, flags silence over `max(14d, 4x median)` |
| Stale claims | Docs saying a symbol is unused or unbuilt while non-test code uses it | Short lines (<300 chars, at most two code-like identifiers) matching "nothing uses / unused / not implemented", checked against fleet source, excluding the defining file and tests |
| Aging deferrals | Written decisions not to deal with something | "not worth fixing", "won't fix", "known bug", "revisit later" in docs; TODO/FIXME/HACK/workaround comments in code -- aged by `git blame`, or mtime outside git |
| Stalled work | Work marked in progress that nobody has touched | Unchecked boxes in repo planning docs (ROADMAP, TODO, NOTES, README...) aged by `git blame`; vault notes with `status: developing / in-progress / wip` untouched 90+ days |

`archive/`, `templates/`, `examples/`, `fixtures/` and dot-dirs are skipped. Contexta's `status: active` means "this claim is current", not "in progress", so it doesn't count as stalled.

## Usage

```bash
rot-finder                                  # all three vaults + ~/projects/local-first
rot-finder --limit 30 -o ~/vaults/BrainSync/_ROT.md
rot-finder --vault ~/vaults/Contexta --json
rot-finder --ignore-tool pedantic-troll     # replaces the default on-demand ignore list
```

`--dry-run` skips writing `--output`. Gone-quiet skips the tools listed as `ignore_tools` in `~/.config/local-first/rot-finder.toml` (default: the on-demand `model-comparison-harness`, `template-tool`, `fleet`, `rot-finder`) -- record a restart/retire/ignore decision there so the same finding isn't raised every week.

## First run (2026-09-29)

Every section found something real in 3.5 seconds:

- **Gone quiet:** `pebble` ran on 16 days in a row, then stopped 168 days ago; `social-post-reader`, `weekly-thread-triage`, `resource-summarizer`, `yt-transcription-summarizer` and `series-cross-link-suggester` too. None is failing, so `fleet status` never mentioned them.
- **Stale claim:** BrainSync's enhancements-status doc still reports the ContentMetadata model as having no users, 169 days after the auto-tagger adopted it.
- **Stalled work:** four `developing` Contexta seeds untouched for 5-6 months, and obsidian-hugo-bridge's unfinished verification checklist.

The first version was noisy (449 "stalled" items, mostly test fixtures; stale claims triggered by plain words like `draft` on long lines). Every threshold above exists because a false positive showed up in real data -- a digest that's mostly noise gets ignored, which is the rot this tool exists to prevent.
