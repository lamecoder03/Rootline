# Keeps request volume under a tokens-per-minute quota by waiting BEFORE sending, not after 429.
# Exists because reacting to rate-limit errors livelocks on a free tier: one investigation request
# is ~7,700 of an 8,000/minute budget, so a retry that under-sleeps gets refused again, and four
# 65-second waits in a row made no progress at all.
# Models the quota as a rolling window and sleeps only for the shortfall.

from __future__ import annotations

import time


class TokenPacer:
    """Rolling-window meter over one provider's TPM quota.

    Groq bills prompt + max_tokens together and up front, so the cost of a request is knowable
    before it is sent. That is what makes proactive pacing possible at all - the alternative,
    discovering the cost from a rejection, throws away a full window every time it is wrong.
    """

    def __init__(self, tokens_per_minute, window=60.0, headroom=0.95, verbose=True):
        self.limit = max(int(tokens_per_minute * headroom), 1)
        self.window = window
        self.verbose = verbose
        self.entries = []          # (timestamp, tokens)
        self.total_waited = 0.0

    def _prune(self, now):
        cutoff = now - self.window
        self.entries = [e for e in self.entries if e[0] > cutoff]
        return sum(tokens for _, tokens in self.entries)

    def reserve(self, tokens):
        """Blocks until `tokens` fit in the window, then records the spend."""
        now = time.monotonic()
        spent = self._prune(now)

        while spent + tokens > self.limit and self.entries:
            # Wait exactly long enough for the oldest entry to age out - no more. Sleeping a flat
            # 60s would be correct but wastes most of a window when only a little must expire.
            oldest_at = self.entries[0][0]
            delay = max(oldest_at + self.window - now, 0.0) + 0.25
            if self.verbose:
                print(f"      [pacing] {spent:,} tokens in the last minute, this request needs "
                      f"{tokens:,} of {self.limit:,} - waiting {delay:.0f}s")
            time.sleep(delay)
            self.total_waited += delay
            now = time.monotonic()
            spent = self._prune(now)

        self.entries.append((now, tokens))

    def correct(self, estimated, actual):
        """Replaces the estimate with the billed figure once it is known, so a systematically
        low estimator cannot drift the meter out of step with the provider's own accounting."""
        if not self.entries:
            return
        timestamp, _ = self.entries[-1]
        self.entries[-1] = (timestamp, max(actual, estimated))
