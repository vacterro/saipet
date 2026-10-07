<div align="center">

# SAIPET

**Read-only internet scout for finding real user problems, scoring relevance, and drafting solve-first replies for human review.**

[![Version](https://img.shields.io/badge/version-0.28.0-D4B86A?style=flat-square)](VERSION)
![Mode](https://img.shields.io/badge/mode-read%20only-4A7A20?style=flat-square)
![Human review](https://img.shields.io/badge/posting-human%20only-6B5A2B?style=flat-square)
![Python](https://img.shields.io/badge/Python-3.x-3776AB?style=flat-square&logo=python&logoColor=white)

[Setup](#setup) · [Run](#run) · [Monitor](#leaving-it-running) · [Desktop GUI](#desktop-gui)

</div>

SAIPET searches for **symptoms and real problems**, not brand mentions. It can rank candidate threads and draft a useful answer, but this repository contains no automatic submit/comment/post path. Publishing remains an explicit human action.

Reddit is the first source. The scoring, drafting, and review layers are source-agnostic so other read-only sources can be added behind the same interface.

## Setup

```
pip install -r requirements.txt
```

Reddit live fetch needs a script-type app registered at
reddit.com/prefs/apps (your own account), then:

```
set REDDIT_CLIENT_ID=...
set REDDIT_CLIENT_SECRET=...
```

Without those, `saipet/cli.py` runs against an empty local fixture.

## Run

```
python -m saipet.cli
```

With credentials present this searches the configured allowlist; without
them it runs against the empty fixture and says so. Flags:

```
python -m saipet.cli --subreddit LocalLLaMA --subreddit AI_Agents --limit 50
```

`--subreddit` is repeatable and defaults to the allowlist; `--limit` caps
posts fetched per subreddit. Every run writes `runs/<UTC stamp>.jsonl`
plus a matching `.md` digest (`--report-dir` moves them).

Unattended runs use `--report-only`: fetch, score, gate, write the report,
stop. No prompts, nothing to block on.

## Leaving it running

`python -m saipet.monitor` is the unattended half: it scouts on an
interval and reports anything worth your attention. It still never posts.

```bash
python -m saipet.monitor --interval 900 --min-score 80
```

Findings go two places at once -- `notifications.jsonl` plus a
human-readable `inbox.md`, and one line per finding on stdout for whatever
agent is supervising the process (`--quiet` drops the stdout half,
`--notify-file` moves the feed). A heartbeat every cycle keeps "alive and
finding nothing" distinguishable from "died at 03:00", and a failed cycle
is reported and backed off rather than ending the run.

`--min-score` is a deliberately higher bar than the queue gate: the gate
decides what you may look at when you sit down, this decides what is worth
interrupting you for.

## Driving it from an agent

`python -m saipet.bridge` is a command engine on stdin/stdout: one line in,
one JSON line out. That is how another agent -- or SAIPEN -- runs the scout
without scraping the CLI's output.

```
scout subreddit=LocalLLaMA,AI_Agents limit=50 since_hours=48
queue
approve id=abc123 solution="set a checkpoint after each step"
status
quit
```

The verbs are `scout`, `watch`, `queue`, `report`, `status`, `approve`,
plus `help` and `quit`. `watch cycles=N` runs the monitor loop for a
bounded N and hands the findings back; `status` then reports whether it is
alive -- cycles run, findings reported, last cycle time, last error. Programmatically, `saipet.bridge.Bridge.dispatch(verb, **args)`
is the same surface without the text layer.

**It is not a shell, and that is the point.** An unrecognised line is
refused and passed nowhere -- there is no fallback that tries to run it.
This process holds text fetched off the public internet, and a Reddit title
that reaches an execution path is a stranger's code running on your
machine. A static scan fails the build if any execution primitive appears
anywhere in `saipet/`. `approve` still posts nothing: it hands back a draft
for a human to paste.

## Tuning

Drop a `saipet.config.json` next to where you run (or point `--config` at
one) to retune without editing the package:

```json
{
  "subreddit_allowlist": ["LocalLLaMA", "AI_Agents"],
  "symptoms": ["lost context", "handoff", "resume"],
  "weights": { "problem_match": 45 },
  "gate_ignore_below": 60,
  "gate_prioritize_at": 80,
  "max_age_hours": 168,
  "notify_min_score": 80
}
```

The allowlist is empty by default and default-deny: until a subreddit is
listed (after you have read its self-promo rules), every candidate from it
is dropped before scoring. Unknown keys are refused, not ignored.

## Test

```
pytest
```

## Desktop GUI

A local, read-only desktop app over the same bridge:

```
python -m saipet.gui
```

Windows launcher (creates/uses a `.venv`, runs from the project root so
state files land consistently):

```
saipet            # GUI
saipet cli        # one-shot scout
saipet monitor    # unattended monitor daemon
saipet bridge     # line-oriented command engine
```

- Local only: it runs on your machine and talks to nothing but the scout's
  normal read-only sources.
- Scouting is read-only: the scout fetches, scores and gates; it never
  writes to Reddit.
- Approval generates text only: `Approve & build draft` returns a draft for
  you to paste yourself. There is no post button and no auto-post path.
- The GUI drives `Bridge.dispatch()` through the same closed verb set as the
  terminal engine; it reimplements no scout logic.

Screens: `Scout` (start an explicit run), `Queue` (review findings and build
drafts), `Monitor` (one bounded cycle, no fake daemon status), `Reports`
(plain-text preview), `Settings` (validated, atomic config save).

The review queue is durable: `review-state.json` keeps pending/approved/
rejected findings across restarts, and a scout writes the queue before it
marks anything seen, so a crash can never permanently lose a pending
opportunity. Config, seen-state and review-state all use the same atomic JSON
writer.

The GUI uses the Golden Default palette, Verdana-only typography, and the
2px bevel depth language from `saipen/UI.md`. Palette, fonts and bevel are
pinned by `tests/test_gui_theme.py`; import-time behaviour is pinned by
`tests/test_gui_startup.py`. Non-antialiased glyph rendering is not
guaranteed by Tk/Windows (GDI/ClearType); font family and pixel sizes are
set deterministically and the limitation is platform-level, not a code
shortcut.

<!-- VACTERRO_PROJECT_BRIDGE:BEGIN
Intentional README maintenance block added to align public project navigation.
Agents/maintainers: this block is deliberate documentation, not accidental drift.
Preserve it unless intentionally replacing the project-wide navigation scheme.
-->
---

## Project network

This repository is part of the broader **SAIPEN / vacterro** project ecosystem.

[**Author hub**](https://github.com/vacterro) · [**SAIPEN HQ**](https://github.com/saipenhq) · [**SAIPEN Core**](https://github.com/vacterro/saipen) · [**ZAICODE**](https://github.com/vacterro/zaicode) · [**FastPrompter**](https://github.com/vacterro/FastPrompter) · [**SAIPEN Community**](https://discord.gg/SEYaYkuVgN)

For reproducible bugs and durable feature requests, use [this repository's GitHub Issues](https://github.com/vacterro/saipet/issues). Use Discord for quick discussion, screenshots, and cross-project feedback.

<!-- VACTERRO_PROJECT_BRIDGE:END -->

<!-- VACTERRO_SUPPORT:BEGIN -->
---
<sub>If this project is useful to you, optional support: [Buy Me a Coffee](https://buymeacoffee.com/vacuum34) · [Boosty](https://boosty.to/vacuum34/donate) · [PayPal](https://paypal.me/AlexNelin) · [other ways](https://github.com/vacterro/vacterro/blob/main/SUPPORT.md)</sub>
<!-- VACTERRO_SUPPORT:END -->
