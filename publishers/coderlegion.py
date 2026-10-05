"""CoderLegion publisher - manual submission.

CoderLegion publishes no API. Their "Publish with us" page describes the only
supported route: create an account, draft the article, submit it through the
platform, and wait for the editorial team to review it. The category split they
advertise is Articles / Tutorials, so the prepared file carries a suggested
category line.

The bot's job here is to save the author the fiddly part - correct length,
correct tags, project links, disclosure - and hand over a file they can paste.
The article then sits in ``queued`` until they confirm the live URL.
"""

from __future__ import annotations

from publishers.manual import ManualPublisher

DEFAULT_SUBMIT_URL = "https://coderlegion.com/publish-with-us"
DEFAULT_CATEGORIES = ("Articles",)


class CoderLegionPublisher(ManualPublisher):
    platform = "coderlegion"

    def __init__(self, config, *, dry_run: bool = False) -> None:
        super().__init__(config, dry_run=dry_run)
        self.submit_url = self.opt_str("submit_url", DEFAULT_SUBMIT_URL)
        self.guidelines_url = self.opt_str("guidelines_url", DEFAULT_SUBMIT_URL)
        if not self.categories:
            self.categories = list(DEFAULT_CATEGORIES)


def build(config, *, dry_run: bool = False) -> CoderLegionPublisher:
    """Entry point used by the publisher factory."""
    return CoderLegionPublisher(config, dry_run=dry_run)

