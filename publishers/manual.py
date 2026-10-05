"""Manual hand-off publisher - the base for platforms with no publishing API.

Not every developer community publishes an API. CoderLegion and DevDojo both
accept articles only through a signed-in web form, and inventing an endpoint
that does not exist would be worse than useless: it would either fail at 3am or,
worse, appear to succeed.

So this publisher does the part that genuinely can be automated - shaping the
article to the platform's limits, writing a ready-to-paste markdown file into
``content_outbox/<platform>/``, and printing the submission steps - and reports
``queued`` rather than ``published``. The ledger stays honest: nothing is
claimed as live until a human runs ``content confirm`` with the real URL.

``front_matter`` and ``submission_steps`` are the two extension points; a
platform that publishes via email, a Google Form or a git commit only has to
override them.
"""

from __future__ import annotations

from typing import Any, Dict, List

from logging_setup import get_logger
from models import ContentItem
from publishers.base import BasePublisher

LOG = get_logger("publisher-manual")


class ManualPublisher(BasePublisher):
    """Writes the finished article to the outbox for a human to submit."""

    def __init__(self, config, *, dry_run: bool = False) -> None:
        super().__init__(config, dry_run=dry_run)
        self.tags = self.opt_list("tags")
        self.categories = self.opt_list("categories")
        self.submit_url = self.opt_str("submit_url", self.spec.submission_url)
        self.guidelines_url = self.opt_str("guidelines_url", self.spec.docs_url)

    # -- configuration ----------------------------------------------------- #

    def check_ready(self) -> tuple[bool, str]:
        if not self.submit_url:
            return False, f"platforms.{self.platform_id}.submit_url is empty"
        return True, "ok"

    # -- hand-off ---------------------------------------------------------- #

    def _front_matter(self, payload: Dict[str, Any], tags: List[str]) -> str:
        """A short editorial header: title, tags and categories, as plain text.

        Written *inside* the markdown as a comment-free block so it survives a
        paste into a web form; it is not YAML front matter because these editors
        do not parse it.
        """
        lines: List[str] = []
        tags = tags or self.tags
        if tags:
            lines.append("Suggested tags: " + ", ".join(tags[: self.spec.tag_limit]))
        if self.categories:
            lines.append("Category: " + ", ".join(self.categories))
        if not lines:
            return ""
        return "\n".join(lines) + "\n\n"

    def submission_instructions(self, item: ContentItem, title: str) -> List[str]:
        steps = [f"Open {self.submit_url} and sign in."]
        if self.guidelines_url and self.guidelines_url != self.submit_url:
            steps.append(f"Skim the guidelines first: {self.guidelines_url}")
        steps.append("Paste the markdown below into the editor.")
        steps.append(
            f"When it is live, record the URL: "
            f"python main.py content confirm {item.id} --platform {self.platform_id} --url <url>"
        )
        return steps

    def check(self) -> Dict[str, Any]:
        described = super().check()
        described["submit_url"] = self.submit_url
        described["guidelines_url"] = self.guidelines_url
        described["tags"] = self.tags
        described["categories"] = self.categories
        described["no_api"] = True
        return described


def build(config, *, dry_run: bool = False) -> ManualPublisher:
    """Entry point used by the publisher factory."""
    return ManualPublisher(config, dry_run=dry_run)

