"""DevDojo publisher - manual submission.

DevDojo's community section ("Write a Post", posts and tutorials) is the
supported publishing path; there is no public API for it. DevDojo also runs
sponsorship and contributor campaigns, so the prepared file keeps the tags
generous enough to land in the right feed.

Same contract as every other manual target: shape the draft, write it to the
outbox, print the steps, report ``queued``, and wait for a human to confirm the
live URL.
"""

from __future__ import annotations

from publishers.manual import ManualPublisher

DEFAULT_SUBMIT_URL = "https://devdojo.com/community/posts/write"
DEFAULT_GUIDELINES_URL = "https://devdojo.com/community/posts"


class DevDojoPublisher(ManualPublisher):
    platform = "devdojo"

    def __init__(self, config, *, dry_run: bool = False) -> None:
        super().__init__(config, dry_run=dry_run)
        self.submit_url = self.opt_str("submit_url", DEFAULT_SUBMIT_URL)
        self.guidelines_url = self.opt_str("guidelines_url", DEFAULT_GUIDELINES_URL)


def build(config, *, dry_run: bool = False) -> DevDojoPublisher:
    """Entry point used by the publisher factory."""
    return DevDojoPublisher(config, dry_run=dry_run)

