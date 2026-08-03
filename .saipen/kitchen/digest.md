done: goal run complete -- 8 tickets, v0.7.0 to v0.14.0. Scout now actually runs live (T-007), is tunable from a config file (T-008), skips stale threads (T-009), survives a dead subreddit (T-010), persists every run (T-011) and runs unattended (T-012). Control plane added: Bridge.dispatch (T-013) and the `python -m saipet.bridge` command engine (T-014). 120/120 tests green, all commits local-only.
remaining: nothing on the board.
awaiting: nothing. Reddit credentials + a subreddit allowlist are what stand between this and a real live run; both are the user's call, not a blocker.
