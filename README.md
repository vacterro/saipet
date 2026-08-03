# SAIPET

v0.21.0 -- see [CHANGELOG.md](CHANGELOG.md)

Read-only internet scout. Finds threads where people describe a real
problem (symptom search, not brand search), scores relevance, drafts a
solve-first reply, and stops for a human to approve before anything gets
posted. Nothing in this codebase calls a submit/comment/post endpoint --
posting, if it happens, is a manual action by the human outside this
program.

Reddit is the first source (`saipet/sources/reddit.py`, read-only OAuth
script app, 100 req/min free tier, no auto-post). The core (`scorer.py`,
`draft.py`, `review.py`) is source-agnostic so other platforms can plug in
behind the same `Source` interface later.

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
