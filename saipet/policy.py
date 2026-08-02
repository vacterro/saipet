from saipet.config import SUBREDDIT_ALLOWLIST


def is_subreddit_allowed(subreddit: str) -> bool:
    """Default-deny: only a subreddit explicitly added to the allowlist
    (config.SUBREDDIT_ALLOWLIST) -- after actually reading its rules --
    ever gets a candidate past this gate."""
    return subreddit in SUBREDDIT_ALLOWLIST
