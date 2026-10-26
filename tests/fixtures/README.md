# Fixtures

`opencode_rate_limit.txt` is the text of a real OpenCode provider rate-limit error: the message and the
provider response body exactly as OpenCode stored them (`provider.quota`, status 429, `FreeUsageLimitError`),
recorded in OpenCode's own log and database. The framing of the TUI around it was not
captured anywhere; the drills print this text into a mock pane.
