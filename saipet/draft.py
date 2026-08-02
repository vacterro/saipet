LINK = "https://github.com/vacterro/saipen"

_ASIDE_MARK = "\n\n---\n"


def build_draft(solution: str, mention_saipen: bool = True) -> str:
    """Compose a solve-first reply: useful text first, link as a trailing aside.

    `solution` must already be a real, standalone-useful answer to the
    thread's actual problem -- this function only enforces shape, it does
    not invent technical content. The invariant it protects: deleting
    everything from `_ASIDE_MARK` onward must still leave a genuinely
    useful comment (see strip_aside / tests).
    """
    if not solution.strip():
        raise ValueError("solution must be non-empty -- no link-only drafts")
    draft = solution.strip()
    if mention_saipen:
        draft += (
            f"{_ASIDE_MARK}(For what it's worth, I'm building an open protocol, "
            f"SAIPEN, around exactly this class of problem -- {LINK})"
        )
    return draft


def strip_aside(draft: str) -> str:
    """Return the draft with any trailing SAIPEN aside removed."""
    return draft.split(_ASIDE_MARK)[0].strip()
