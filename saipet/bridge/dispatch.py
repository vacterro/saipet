"""The surface an external agent drives SAIPET through.

`cli.main()` is a script, not an API: it parses argv, prints, and prompts.
An agent driving it would have to import argparse internals or scrape
stdout, and both break the moment the CLI's wording changes. This module is
the stable seam instead -- one `dispatch(verb, **args)`, structured results,
no printing.

**The verb set is closed, and that is the security property, not a
convenience.** Everything this bridge acts on originates on the public
internet: a Reddit title is attacker-controlled text. A dispatcher that
resolved verbs dynamically -- `getattr(self, verb)`, a dynamic evaluation,
a shell call -- would turn "fetched some posts" into "ran what a stranger
wrote". An unknown verb is refused and never executed, and the mapping
below is the whole list of things that can happen.

(The words this paragraph avoids spelling out are the literal tokens
tests/test_no_autopost.py greps for across this package. Naming them even
in prose fails that scan -- which is the scan doing its job, so the prose
works around it rather than the other way round.)

No verb posts anything. There is no write endpoint anywhere in `saipet/`
(tests/test_no_autopost.py scans for it); `approve` flips a status flag and
hands back text for a human to paste, exactly as the interactive loop does.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path

from saipet import config
from saipet.cli import DEFAULT_LIMIT, build_source, scout
from saipet.draft import build_draft
from saipet.notify import Notifier, NullNotifier
from saipet.report import DEFAULT_REPORT_DIR, write_report
from saipet.review import ReviewQueue
from saipet.signals import extract_signals
from saipet.store import SeenStore


@dataclass(frozen=True)
class Result:
    """One dispatch outcome. `ok=False` means nothing was executed."""

    ok: bool
    verb: str
    data: dict = field(default_factory=dict)
    error: str = ""


def _item_view(item) -> dict:
    candidate = item.candidate
    return {
        "id": candidate.id,
        "source": candidate.source,
        "subreddit": candidate.subreddit,
        "title": candidate.title,
        "permalink": candidate.permalink,
        "score": item.relevance_score,
        "band": item.band,
        "status": item.status,
    }


class Bridge:
    """Holds one driving session: the last queue, the last report, the store.

    State lives here rather than in module globals so a caller can run two
    independent sessions (a test and a live one, say) without either
    seeing the other's queue.
    """

    def __init__(
        self,
        report_dir: str | Path = DEFAULT_REPORT_DIR,
        seen_path: str | Path = "seen.json",
        source_factory=build_source,
        now_fn=time.time,
        notifier: Notifier | None = None,
    ):
        self.report_dir = Path(report_dir)
        self.seen = SeenStore(seen_path)
        self._source_factory = source_factory
        self._now_fn = now_fn
        # Only the `watch` verb uses this; a caller that just dispatches
        # `scout` reads the result instead of being told about it.
        self._notifier = notifier if notifier is not None else NullNotifier()
        self.queue = ReviewQueue()
        self.last_report: dict = {}
        self.last_failures: list = []
        # Filled by the `watch` verb; read by `status`. Kept here rather than
        # inside the loop so a driving agent can ask "is the monitor alive"
        # between calls.
        self.monitor_state = None

    # -- verbs ---------------------------------------------------------

    def _scout(
        self,
        subreddits: list[str] | None = None,
        limit: int = DEFAULT_LIMIT,
        since_hours: float | None = None,
    ) -> dict:
        targets = list(subreddits) if subreddits else sorted(config.SUBREDDIT_ALLOWLIST)
        source = self._source_factory(targets, config.SYMPTOMS)
        max_age = config.MAX_AGE_HOURS if since_hours is None else since_hours

        self.queue = scout(
            source,
            signal_fn=extract_signals,
            seen=self.seen,
            limit=limit,
            max_age_hours=max_age or None,
            now_fn=self._now_fn,
        )
        self.last_failures = list(getattr(source, "last_failures", []))

        paths = write_report(
            self.queue.pending(),
            directory=self.report_dir,
            failures=self.last_failures,
            now_fn=self._now_fn,
        )
        self.last_report = {"jsonl": str(paths.jsonl), "markdown": str(paths.markdown)}

        return {
            "source": source.name,
            "subreddits": targets,
            "queued": len(self.queue.pending()),
            "failures": [{"subreddit": sub, "error": err} for sub, err in self.last_failures],
            "report": self.last_report,
        }

    def _report(self) -> dict:
        """The last run's report, plus whatever reports are on disk.

        Listing the directory matters for a fresh process: an agent that
        reconnects has no `last_report` but the runs are still there.
        """
        known = sorted(str(p) for p in self.report_dir.glob("*.jsonl"))
        return {"last": self.last_report, "available": known}

    def _status(self) -> dict:
        return {
            "queued": len(self.queue.pending()),
            "allowlist": sorted(config.SUBREDDIT_ALLOWLIST),
            "symptoms": list(config.SYMPTOMS),
            "max_age_hours": config.MAX_AGE_HOURS,
            "gate": {
                "ignore_below": config.GATE_IGNORE_BELOW,
                "prioritize_at": config.GATE_PRIORITIZE_AT,
            },
            "failures": [{"subreddit": sub, "error": err} for sub, err in self.last_failures],
            "report": self.last_report,
            "monitor": self.monitor_state.as_dict() if self.monitor_state else None,
            "notify_min_score": config.NOTIFY_MIN_SCORE,
            "can_post": False,  # stated, not implied: there is no write path at all
        }

    def _watch(
        self,
        cycles: int = 1,
        interval_seconds: float = 0,
        min_score: float | None = None,
        subreddits: list[str] | None = None,
        limit: int = DEFAULT_LIMIT,
        since_hours: float | None = None,
    ) -> dict:
        """Run the monitor loop for a bounded number of cycles.

        **Bounded, always.** A dispatch that never returns hangs whoever
        called it -- for the terminal engine that means one typed line eats
        the whole process -- so `cycles` has no "forever" value here. The
        unattended forever-run is `python -m saipet.monitor`, which is a
        process the caller can actually stop.

        Imported lazily: `monitor` imports this module, so a module-level
        import would be a cycle.
        """
        from saipet.monitor import MonitorState, run_monitor

        if not isinstance(cycles, int) or isinstance(cycles, bool) or cycles < 1:
            raise ValueError("cycles must be an integer of at least 1")

        scout_args: dict = {"limit": limit}
        if subreddits:
            scout_args["subreddits"] = list(subreddits)
        if since_hours is not None:
            scout_args["since_hours"] = since_hours

        if self.monitor_state is None:
            self.monitor_state = MonitorState()

        run_monitor(
            self,
            notifier=self._notifier,
            interval_seconds=interval_seconds,
            cycles=cycles,
            scout_args=scout_args,
            min_score=min_score,
            now_fn=self._now_fn,
            state=self.monitor_state,
        )
        return {
            "monitor": self.monitor_state.as_dict(),
            "findings": list(self.monitor_state.findings),
        }

    def _queue(self) -> dict:
        items = sorted(self.queue.pending(), key=lambda i: -i.relevance_score)
        return {"items": [_item_view(item) for item in items]}

    def _approve(self, id: str, solution: str, mention_saipen: bool = True) -> dict:
        """Attach a human-written solution to one queued item and mark it.

        `solution` is required and never generated here: the whole point of
        the review queue is that a person wrote the answer. Approving still
        posts nothing -- it returns the text for a human to paste.
        """
        if not solution or not solution.strip():
            raise ValueError("solution must be a non-empty string written by a human")

        for item in self.queue.pending():
            if item.candidate.id == id:
                item.draft = build_draft(solution, mention_saipen=mention_saipen)
                self.queue.approve(item)
                return {"id": id, "draft": item.draft, "posted": False}
        raise KeyError(f"no pending item with id {id!r}")

    # -- dispatch ------------------------------------------------------

    def dispatch(self, verb: str, **args) -> Result:
        handler = self._handlers().get(verb)
        if handler is None:
            # Refused, not attempted. Nothing below this line runs.
            return Result(
                ok=False,
                verb=verb,
                error=f"unknown verb {verb!r} -- known verbs: {', '.join(sorted(VERBS))}",
            )
        try:
            return Result(ok=True, verb=verb, data=handler(**args))
        except TypeError as exc:
            return Result(ok=False, verb=verb, error=f"bad arguments for {verb!r}: {exc}")
        except Exception as exc:  # noqa: BLE001 -- a driven session reports, never crashes
            return Result(ok=False, verb=verb, error=f"{type(exc).__name__}: {exc}")

    def _handlers(self) -> dict:
        return {
            "scout": self._scout,
            "report": self._report,
            "status": self._status,
            "queue": self._queue,
            "approve": self._approve,
            "watch": self._watch,
        }


VERBS = frozenset({"scout", "report", "status", "queue", "approve", "watch"})
