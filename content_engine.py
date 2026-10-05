"""Content engine: author an article once, shape it for every platform.

The counterpart to `prompt_engine.py`. Same contract, different medium:

* `compose()` builds a deterministic markdown post from the `content` and
  `plugin` config sections. No LLM, no network - the same default as the
  outreach engine.
* `render_prompt()` turns the same facts into a self-contained prompt for an
  external renderer (`scheduler.llm_command`).
* `generate()` picks between them and falls back to the deterministic composer
  whenever the renderer errors, returns nothing, or produces too little.

What makes this more than a template file is the **quality gate**. Promotional
content that reads as marketing gets removed from these communities, so
`validate()` enforces the same discipline the outreach prompts already state:

* a minimum word count - thin posts are rejected by reviewers everywhere;
* at least one outbound link to the project;
* an explicit disclosure when `content.disclosure_required` is set, because
  writing about your own project without saying so is the single fastest way to
  lose a community account;
* no hype vocabulary (the same list the outreach engine forbids, plus whatever
  `content.forbid_words` adds);
* title length and heading structure within the platform's limits.

`prepare_for_platform()` is the last step before a publisher runs: it clamps the
title, trims the tags to the platform limit, normalises the body to the
platform's content format and re-runs the gate against the final payload.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Sequence

from logging_setup import get_logger
from models import (
    CONTENT_APPROVED,
    CONTENT_DRAFT,
    CONTENT_FAILED,
    CONTENT_PUBLISHED,
    CONTENT_QUEUED,
    CONTENT_STATUSES,
    is_valid_url,
    word_count,
)
from platforms import PLATFORMS, PlatformSpec, get_platform

LOG = get_logger("content-engine")

TOKEN_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
LINK_RE = re.compile(r"\[[^\]]+\]\((https?://[^)\s]+)\)")
BARE_URL_RE = re.compile(r"(?<![(<\w])(https?://[^\s<>()\[\]]+)")
H1_RE = re.compile(r"^#\s+\S", re.MULTILINE)
H2_RE = re.compile(r"^##\s+\S", re.MULTILINE)

#: Shared with `prompt_engine.RULES`: the words that read as hype and are the
#: fastest route to a removed post.
HYPE_WORDS = (
    "revolutionary",
    "game-changing",
    "game changing",
    "best-in-class",
    "best in class",
    "world-class",
    "cutting-edge",
    "cutting edge",
    "next-generation",
    "next generation",
    "unleash",
    "supercharge",
    "10x",
    "seamless",
    "effortlessly",
    "delve",
    "leverage synergies",
    "synergy",
    "disrupt",
)

#: Disclosure wording that satisfies the gate. Matched case-insensitively on
#: word boundaries so "I do not maintain this" cannot slip through by accident.
DISCLOSURE_PATTERNS = (
    r"\bi maintain\b",
    r"\bi am the (?:maintainer|author|creator|owner)\b",
    r"\bi'?m the (?:maintainer|author|creator|owner)\b",
    r"\bwe maintain\b",
    r"\bwe built (?:this|it)\b",
    r"\bi built (?:this|it)\b",
    r"\bthis is (?:my|our) (?:own )?(?:project|plugin|tool|library)\b",
    r"\baffiliate (?:link|disclosure)\b",
    r"\bdisclosure\b",
)

#: Default section skeleton. Users override it with `content.sections`, but the
#: shape below is what community posts actually reward: problem, what it does,
#: how it is built, how to try it, and where it stops being useful.
DEFAULT_SECTIONS = (
    "The problem",
    "What the project does",
    "How it is built",
    "Try it",
    "Where it stops being useful",
    "Feedback",
)

MIN_WORDS_DEFAULT = 350
MAX_WORDS_DEFAULT = 1800
SUMMARY_MAX_CHARS = 300
MAX_TITLE_WORDS_DEFAULT = 12


# --------------------------------------------------------------------------- #
# Config projection
# --------------------------------------------------------------------------- #


def content_data_from_config(config) -> Dict[str, Any]:  # noqa: ANN001 - avoid import cycle
    """Project the `content` section plus the reusable `plugin` facts."""
    plugin = {
        "name": config.str("plugin.name"),
        "description": config.str("plugin.description"),
        "value_prop": config.str("plugin.value_prop"),
        "features": config.list("plugin.features"),
        "repo_url": config.str("plugin.repo_url"),
        "appfoundry_url": config.str("plugin.appfoundry_url"),
        "demo_url": config.str("plugin.demo_url"),
        "maintainer_name": config.str("plugin.maintainer_name"),
        "maintainer_email": config.str("plugin.maintainer_email"),
        "cta": config.str("plugin.cta"),
        "ask": config.str("plugin.ask"),
        "license": config.str("content.license"),
    }
    return {
        "enabled": config.bool("content.enabled", False),
        "audience": config.str("content.audience"),
        "angle": config.str("content.angle"),
        "tone": config.str("content.tone", "practitioner writing for peers"),
        "persona": config.str("content.persona"),
        "language": config.str("content.language", "en"),
        "disclosure": config.str("content.disclosure"),
        "disclosure_required": config.bool("content.disclosure_required", True),
        "disclosure_note": config.str("content.disclosure_note"),
        "tags": config.list("content.tags"),
        "topics": config.records("content.topics"),
        "sections": config.list("content.sections") or list(DEFAULT_SECTIONS),
        "min_words": config.int("content.min_words", MIN_WORDS_DEFAULT, minimum=0, maximum=50000),
        "max_words": config.int("content.max_words", MAX_WORDS_DEFAULT, minimum=50, maximum=100000),
        "summary_max_chars": config.int(
            "content.summary_max_chars", SUMMARY_MAX_CHARS, minimum=40, maximum=2000
        ),
        "max_title_words": config.int(
            "content.max_title_words", MAX_TITLE_WORDS_DEFAULT, minimum=3, maximum=30
        ),
        "forbid_words": config.list("content.forbid_words"),
        "canonical_base_url": config.str("content.canonical_base_url"),
        "call_to_action": config.str("content.call_to_action"),
        "closing": config.str("content.closing"),
        "plugin": plugin,
    }


# --------------------------------------------------------------------------- #
# Token rendering (shared behaviour with prompt_engine.render_template)
# --------------------------------------------------------------------------- #


def render_template(template: str, values: Dict[str, Any]) -> str:
    """Replace ``{{TOKEN}}`` placeholders. Unknown tokens are left intact."""

    def _sub(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key in values:
            value = values[key]
            return "" if value is None else str(value)
        LOG.debug("Unresolved template token {{%s}}", key)
        return match.group(0)

    return TOKEN_RE.sub(_sub, template)


def forbidden_words(content: Dict[str, Any]) -> List[str]:
    extra = [str(word).strip().lower() for word in (content or {}).get("forbid_words") or []]
    return sorted(set(HYPE_WORDS) | {word for word in extra if word})


def sections_for(content: Dict[str, Any]) -> List[str]:
    raw = (content or {}).get("sections") or DEFAULT_SECTIONS
    cleaned = [str(item).strip() for item in raw if str(item).strip()]
    return cleaned or list(DEFAULT_SECTIONS)


def title_limit(content: Dict[str, Any], platform: str = "") -> int:
    limit = int((content or {}).get("max_title_words") or MAX_TITLE_WORDS_DEFAULT)
    spec = get_platform(platform)
    if spec is not None and spec.max_title_words:
        limit = min(limit, spec.max_title_words)
    return max(limit, 3)


def clamp_title(title: str, limit: int) -> str:
    """Trim a title to at most ``limit`` words on a word boundary."""
    words = re.split(r"\s+", (title or "").strip())
    if len(words) <= limit:
        return " ".join(words)
    return " ".join(words[:limit]).rstrip(",;:-")


def normalise_tags(tags: Sequence[str], platform: str = "", *, limit: int = 0) -> List[str]:
    """Lowercase, de-duplicate and clamp a tag list to the platform's limit.

    Tags are normalised to the identifier form every platform here expects:
    lowercase alphanumerics with hyphens, no `#`, no spaces.
    """
    spec = get_platform(platform)
    cap = limit or (spec.tag_limit if spec else 0)
    seen: List[str] = []
    for tag in tags or []:
        token = re.sub(r"[^a-z0-9]+", "", str(tag).strip().lower().lstrip("#"))
        if token and token not in seen:
            seen.append(token)
    if spec is not None and not spec.supports_tags:
        return []
    if cap > 0:
        return seen[:cap]
    return seen


# --------------------------------------------------------------------------- #
# Deterministic composer
# --------------------------------------------------------------------------- #


def _first_sentence(text: str, limit: int = 240) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    boundary = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if boundary > limit // 2:
        return cut[: boundary + 1].strip()
    return cut.rstrip() + "..."


def _link(label: str, url: str) -> Optional[str]:
    return f"- [{label}]({url})" if url and is_valid_url(url) else None


def _plugin_block(plugin: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    links = [
        _link("Source code", str(plugin.get("repo_url") or "")),
        _link("Package listing", str(plugin.get("appfoundry_url") or "")),
        _link("Live demo", str(plugin.get("demo_url") or "")),
    ]
    links = [link for link in links if link]
    if links:
        lines += ["", "Links:"] + links
    if plugin.get("license"):
        lines += ["", f"Licence: {plugin['license']}"]
    return lines


def _bullets(features: List[str]) -> List[str]:
    return [f"- {feature}" for feature in features[:6]]


def _section_body(
    index: int, project: str, plugin: Dict[str, Any], features: List[str]
) -> List[str]:
    """Body for section ``index`` (1-based, after the opening section).

    Keyed by position rather than by heading text so a campaign can rename or
    reorder `content.sections` without producing empty sections. Positions past
    the fourth reuse the closing material.
    """
    if index == 1:
        if features:
            lines = [f"{project} ships the recurring parts as a reusable building block:", ""]
            lines += _bullets(features)
            lines += [
                "",
                "The point is not to hide the platform behind an abstraction. It is to "
                "put the failure-prone part - token lifecycle, retry policy, "
                "idempotency - in one place that can be tested once, so the code you "
                "actually care about stays readable.",
                "",
            ]
            return lines
        return [
            "The project packages the recurring integration work so a new consumer can "
            "depend on it instead of re-deriving it, and keeps the part that breaks in "
            "production - token lifecycle, retry policy, idempotency - in one testable "
            "place.",
            "",
        ]

    if index == 2:
        return [
            "The implementation favours boring, inspectable choices over clever ones. "
            "The public surface is deliberately small, the failure modes are enumerated "
            "rather than emergent, and every external call is bounded by an explicit "
            "timeout with a documented retry policy that does not repeat "
            "non-idempotent operations.",
            "",
            "Bounded retries matter more than they sound. An unbounded retry on a write "
            "that partially succeeded turns a transient error into duplicated records, "
            "which is far more expensive to unpick than the original outage. Where an "
            "operation cannot be made safe to repeat, the honest choice is to surface "
            "the ambiguity instead of guessing at it.",
            "",
            "There is no server to operate and no vendor runtime to configure, which is "
            "the other half of the appeal: the cost of adopting this should be a "
            "dependency in a manifest, not a new system to run and monitor.",
            "",
        ]

    if index == 3:
        steps = [
            f"Clone or install {project} from the repository and read the README end "
            "to end before changing anything.",
            "Run the reference flow against your own sandbox credentials, so you can "
            "see the real request and response traffic.",
            "Walk the architecture notes, then adapt the pattern to your stack - "
            "keeping the retry and idempotency rules intact.",
        ]
        lines = ["A short path to a working result:", ""]
        lines += [f"{step_no}. {step}" for step_no, step in enumerate(steps, start=1)]
        lines += [
            "",
            "If a step does not work as written, that is a bug worth reporting rather "
            "than a detour to plan around. Getting stuck is the most useful signal a "
            "reader can send.",
            "",
        ]
        return lines

    if index == 4:
        return [
            "Naming the limits is more useful than claiming completeness. This does not "
            "attempt to cover every surface of the underlying platform, it does not "
            "smooth over the differences between hosted and on-premises deployments, "
            "and it will not tell you whether your particular workflow is a good fit. "
            "Those are judgement calls that need a human.",
            "",
            "What it does claim is narrower and more testable: the parts it covers "
            "behave the same way in your environment as they do in the reference flow, "
            "because the tests exercise the same contract the library implements.",
            "",
        ]

    return [
        "Feedback is the most useful thing you can send back. If the pattern does not "
        "fit your stack, the gap is a documentation bug worth reporting. If it does "
        "fit, an issue describing your use case is the clearest possible signal about "
        "what to build next - far more informative than a star, which tells us nothing "
        "about where to aim.",
        "",
        "Pull requests are welcome, including small ones. A documentation improvement, "
        "a worked example in another language, or a test for an edge case you hit are "
        "all genuinely useful contributions.",
        "",
    ]


def compose(
    content: Dict[str, Any],
    *,
    title: str = "",
    topic: str = "",
    platform: str = "",
) -> Dict[str, str]:
    """Build a deterministic article. Returns ``{title, body, summary, words}``.

    Every sentence is drawn from config facts or from hard-won general advice
    about integration work - nothing is invented about the project, and the
    disclosure line is included whenever one is configured.

    The composer is a *starting point*, not a finished post, and it does not pad
    itself to reach `min_words`. If your config is too thin to clear the gate,
    :func:`validate` says so and the fix is to add real detail to
    ``plugin.features`` / ``content.angle``, or to render with ``--llm``. A
    template that silently inflated itself to satisfy a word count would defeat
    the only check standing between this tool and community-run spam filters.
    """
    data = dict(content or {})
    plugin = dict(data.get("plugin") or {})
    sections = sections_for(data)
    project = str(plugin.get("name") or "the project").strip()

    headline = (title or topic or data.get("angle") or project).strip()
    headline = clamp_title(headline or project, title_limit(data, platform))

    body: List[str] = [f"# {headline}", ""]

    if data.get("disclosure"):
        body += [str(data["disclosure"]).strip(), ""]

    opening = str(data.get("angle") or plugin.get("value_prop") or plugin.get("description") or "").strip()
    if topic and topic.strip() and topic.strip().lower() not in headline.lower():
        opening = (
            f"{opening} This post focuses on {topic.strip()}."
            if opening
            else f"This post focuses on {topic.strip()}."
        )
    body += [opening, ""] if opening else []

    features = [str(item).strip() for item in (plugin.get("features") or []) if str(item).strip()]

    # Section bodies are keyed by position so a custom `sections:` list still
    # produces sensible content instead of empty headings.
    if sections:
        body += [f"## {sections[0]}", ""]
        body += [
            "Every integration project opens with the same handful of unglamorous "
            "problems: obtaining a credential, refreshing it before it expires, "
            "surviving a transient 5xx without duplicating a side effect, and "
            "leaving enough trace behind to debug the thing at 2am. None of that is "
            "the part you want to spend the project on, and all of it has to be "
            "somebody's job.",
            "",
            "The pattern is stable enough to extract. What changes per project is the "
            "domain vocabulary on top of it, which is why the interesting engineering "
            "keeps getting crowded out by the plumbing underneath.",
            "",
        ]
        body += [
            str(plugin.get("value_prop") or "").strip(),
            "",
        ] if plugin.get("value_prop") else []

    for index, heading in enumerate(sections[1:], start=1):
        body += [f"## {heading}", ""]
        filler = _section_body(index, project, plugin, features)
        body += filler
        if index in (1, 3):
            body += _plugin_block(plugin)
            body += [""]
        if index == 3 and plugin.get("cta"):
            body += [str(plugin["cta"]).strip(), ""]

    call_to_action = str(data.get("call_to_action") or plugin.get("ask") or "").strip()
    if call_to_action and call_to_action.lower() not in "\n".join(body).lower():
        body += ["## Next steps", "", call_to_action, ""]

    if data.get("closing"):
        body += [str(data["closing"]).strip(), ""]

    if data.get("disclosure_note"):
        body += ["---", "", str(data["disclosure_note"]).strip(), ""]

    rendered = "\n".join(body).strip()
    summary = _first_sentence(
        str(data.get("angle") or plugin.get("description") or headline),
        int(data.get("summary_max_chars") or SUMMARY_MAX_CHARS),
    )
    return {
        "title": headline,
        "body": rendered,
        "summary": summary,
        "words": str(word_count(rendered)),
    }


# --------------------------------------------------------------------------- #
# Quality gate
# --------------------------------------------------------------------------- #


def _has_disclosure(text: str) -> bool:
    lowered = (text or "").lower()
    return any(re.search(pattern, lowered) for pattern in DISCLOSURE_PATTERNS)


def validate(
    content: Dict[str, Any],
    *,
    title: str,
    body: str,
    platform: str = "",
    summary: str = "",
    strict: bool = True,
) -> List[str]:
    """Return the list of problems with a draft ([] == publishable).

    `strict=False` downgrades every finding to advisory, which is what dry-run
    previews want: report, do not block.
    """
    problems: List[str] = []
    data = dict(content or {})
    spec: Optional[PlatformSpec] = get_platform(platform)
    text = body or ""

    if not title.strip():
        problems.append("title is empty")
    if not text.strip():
        problems.append("body is empty")

    words = word_count(text)
    min_words = int(data.get("min_words") or 0)
    max_words = int(data.get("max_words") or 0)
    floor = max(min_words, spec.min_body_words if spec else 0)
    if words < floor:
        problems.append(f"body is {words} words, under the {floor} word minimum")
    if max_words and words > max_words:
        problems.append(f"body is {words} words, over the {max_words} word maximum")

    if spec is not None:
        limit = title_limit(data, platform)
        title_words = word_count(title)
        if title_words > limit:
            problems.append(f"title is {title_words} words, over the {limit} word limit")
        if spec.max_title_chars and len(title) > spec.max_title_chars:
            problems.append(
                f"title is {len(title)} characters, over the {spec.max_title_chars} limit"
            )
        if spec.max_body_bytes and len(text.encode("utf-8")) > spec.max_body_bytes:
            problems.append(
                f"body is {len(text.encode('utf-8'))} bytes, over the "
                f"{spec.max_body_bytes} byte limit"
            )

    if spec is not None and spec.min_body_words and words < spec.min_body_words:
        problems.append(f"{spec.label} expects at least {spec.min_body_words} words")

    plugin = data.get("plugin") or {}
    project_links = [
        str(plugin.get(key) or "")
        for key in ("repo_url", "appfoundry_url", "demo_url", "canonical_base_url")
    ]
    project_links = [link for link in project_links if is_valid_url(link)]
    if project_links:
        found = LINK_RE.findall(text) + BARE_URL_RE.findall(text)
        if not any(any(link.rstrip("/") == hit.rstrip("/") for hit in found) for link in project_links):
            problems.append("body does not link to the project repository")

    if not H1_RE.search(text):
        problems.append("body has no level-1 heading")
    elif len(H2_RE.findall(text)) < 2:
        problems.append("body has fewer than two level-2 sections")

    banned = forbidden_words(data)
    lowered = text.lower()
    hits = sorted({word for word in banned if re.search(rf"\b{re.escape(word)}\b", lowered)})
    if hits:
        problems.append("banned wording present: " + ", ".join(hits))

    if data.get("disclosure_required", True) and not _has_disclosure(text):
        problems.append("no author disclosure - say plainly that you maintain the project")

    if summary and len(summary) > int(data.get("summary_max_chars") or SUMMARY_MAX_CHARS):
        problems.append(
            f"summary is {len(summary)} characters, over the "
            f"{data.get('summary_max_chars')} limit"
        )

    if strict and problems:
        LOG.warning("Content gate rejected a draft for %s: %s", platform or "-", "; ".join(problems))
    return problems


def enforce_word_limit(text: str, limit: int, *, ellipsis: str = " ...") -> str:
    """Trim at a sentence boundary, then at a word boundary if needed.

    Mirrors `prompt_engine.enforce_word_limit` so both media trim the same way.
    """
    text = (text or "").strip()
    if word_count(text) <= limit:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept: List[str] = []
    for sentence in sentences:
        candidate = " ".join(kept + [sentence])
        if word_count(candidate) > limit:
            break
        kept.append(sentence)
    if kept:
        return " ".join(kept).strip()
    words = text.split()
    return " ".join(words[: max(limit - 1, 1)]).rstrip(",;:-") + ellipsis


# --------------------------------------------------------------------------- #
# Platform shaping (the last step before a publisher runs)
# --------------------------------------------------------------------------- #


def prepare_for_platform(
    content: Dict[str, Any],
    *,
    title: str,
    body: str,
    summary: str = "",
    tags: Optional[Sequence[str]] = None,
    platform: str = "",
) -> Dict[str, Any]:
    """Clamp, normalise and validate a draft for one platform.

    Returns the exact payload a publisher will transmit, plus the gate findings
    under ``problems``. The caller decides whether findings are fatal - the
    promoter refuses to publish when they are.
    """
    spec = get_platform(platform)
    data = dict(content or {})

    final_title = clamp_title(title, title_limit(data, platform))
    if spec is not None and spec.max_title_chars:
        final_title = final_title[: spec.max_title_chars].rstrip()

    max_words = int(data.get("max_words") or 0)
    final_body = enforce_word_limit(body, max_words) if max_words else (body or "").strip()

    final_tags = normalise_tags(
        tags if tags is not None else (data.get("tags") or []), platform
    )

    summary_cap = int(data.get("summary_max_chars") or SUMMARY_MAX_CHARS)
    final_summary = _first_sentence(summary or "", summary_cap)

    canonical = ""
    if spec is not None and spec.supports_canonical:
        canonical = str(data.get("canonical_base_url") or "").strip()

    problems = validate(
        data,
        title=final_title,
        body=final_body,
        summary=final_summary,
        platform=platform,
    )

    return {
        "platform": platform,
        "title": final_title,
        "body": final_body,
        "summary": final_summary,
        "tags": final_tags,
        "canonical_url": canonical,
        "content_format": spec.content_format if spec else "markdown",
        "mode_hint": spec.kind if spec else "manual",
        "words": word_count(final_body),
        "problems": problems,
        "publishable": not problems,
    }


# --------------------------------------------------------------------------- #
# LLM prompt
# --------------------------------------------------------------------------- #

PROMPT_TEMPLATE = """\
Write a technical article for {platform_label} ({platform_id}).

AUDIENCE
{audience}

VOICE
- {tone}
- Written by: {persona}
- Language: {language}

FORMAT
- GitHub-flavoured markdown. Start with a single `# H1`, then `## H2` sections.
- Title: at most {max_title_words} words.
- Body: {min_words}-{max_words} words.
- {tag_line}

STRUCTURE
{sections}

HARD RULES
{rules}

FACTS (use only these - never invent anything else)
project: {plugin_name}
what it replaces: {value_prop}
description: {description}
capabilities:
{features}
repository: {repo_url}
listing: {appfoundry_url}
demo: {demo_url}
licence: {license}
editorial angle: {angle}
call to action: {cta}
sponsorship ask: {ask}

DISCLOSURE
{disclosure}

Return only the article, starting with the `# H1`. No preamble, no explanation
of what you are doing, no placeholder text in braces."""


def platform_label(platform: str) -> str:
    spec = PLATFORMS.get((platform or "").strip().lower())
    return spec.label if spec else (platform or "a developer community")


def render_prompt(
    content: Dict[str, Any],
    *,
    title: str = "",
    topic: str = "",
    platform: str = "",
) -> str:
    """Render `PROMPT_TEMPLATE` into a complete prompt for an external model."""
    data = dict(content or {})
    plugin = dict(data.get("plugin") or {})
    spec = get_platform(platform)
    features = [str(item) for item in (plugin.get("features") or []) if str(item)]

    if spec is not None:
        tag_line = (
            f"End with a `Tags:` line of {spec.tag_limit} lowercase, hyphen-free tags."
            if spec.supports_tags and spec.tag_limit
            else "Do not add a tags line; tags are supplied separately."
        )
    else:
        tag_line = "End with a `Tags:` line of up to 4 lowercase tags."

    rules = "\n".join(
        [
            "- Open with the concrete problem, not with a greeting or a preamble.",
            "- Show working code, configuration or output for every claim you make.",
            "- No hype words (revolutionary, game-changing, best-in-class, seamless, "
            "supercharge, unleash, 10x, delve, synergy, disrupt).",
            "- No fabricated metrics, customers, funding or endorsements.",
            "- Link the repository in the body. A reader must be able to act on this.",
            "- State plainly that you maintain the project. Do not hide the affiliation.",
            "- Exactly one call to action.",
            "- Respect the word budget strictly. Drop adjectives, not facts.",
        ]
    )

    values: Dict[str, Any] = {
        "platform_id": platform or "any",
        "platform_label": platform_label(platform),
        "audience": str(data.get("audience") or "practitioners who already work in this space"),
        "tone": str(data.get("tone") or "practitioner writing for peers"),
        "persona": str(data.get("persona") or "an engineer who maintains the project"),
        "language": str(data.get("language") or "en"),
        "max_title_words": title_limit(data, platform),
        "min_words": int(data.get("min_words") or MIN_WORDS_DEFAULT),
        "max_words": int(data.get("max_words") or MAX_WORDS_DEFAULT),
        "tag_line": tag_line,
        "sections": "\n".join(
            f"{index}. {heading}" for index, heading in enumerate(sections_for(data), start=1)
        ),
        "rules": rules,
        "plugin_name": plugin.get("name", ""),
        "value_prop": plugin.get("value_prop", ""),
        "description": plugin.get("description", ""),
        "features": "\n".join(f"  - {feature}" for feature in features[:8]) or "  - (none configured)",
        "repo_url": plugin.get("repo_url", ""),
        "appfoundry_url": plugin.get("appfoundry_url", ""),
        "demo_url": plugin.get("demo_url", ""),
        "license": plugin.get("license", ""),
        "angle": topic or str(data.get("angle") or ""),
        "cta": plugin.get("cta", ""),
        "ask": plugin.get("ask", ""),
        "disclosure": str(data.get("disclosure") or "required - state the affiliation in the first paragraph"),
    }
    return render_template(PROMPT_TEMPLATE, values).strip()


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def _split_rendered(text: str) -> Dict[str, str]:
    """Separate a rendered article into title / summary / body.

    An external renderer is trusted to return markdown, but not to return *our*
    metadata conventions, so an optional `Title:` / `Summary:` header is
    tolerated and anything else is treated as the body.
    """
    raw = (text or "").strip()
    title = ""
    summary = ""
    lines = raw.splitlines()
    cursor = 0
    for _ in range(2):
        if cursor >= len(lines):
            break
        head = lines[cursor].strip()
        lowered = head.lower()
        if lowered.startswith("title:") and not title:
            title = head.split(":", 1)[1].strip()
            cursor += 1
            continue
        if lowered.startswith("summary:") and not summary:
            summary = head.split(":", 1)[1].strip()
            cursor += 1
            continue
        break
    body = "\n".join(lines[cursor:]).strip() or raw
    return {"title": title, "summary": summary, "body": body}


def generate(
    content: Dict[str, Any],
    *,
    title: str = "",
    topic: str = "",
    platform: str = "",
    llm_renderer: Optional[Callable[[str], str]] = None,
    fallback_title: str = "",
) -> Dict[str, Any]:
    """Produce an article for ``platform``.

    Returns ``{title, body, summary, words, renderer}``. With no
    ``llm_renderer`` this is fully deterministic. Any renderer failure - error,
    empty output, or output too short to be an article - falls back to
    :func:`compose`, which is the same fail-safe contract `prompt_engine` uses.
    """
    data = dict(content or {})
    spec = get_platform(platform)
    seed_title = title or topic or data.get("angle") or fallback_title or str(
        (data.get("plugin") or {}).get("name") or "the project"
    )

    if llm_renderer is None:
        draft = compose(data, title=seed_title, topic=topic, platform=platform)
        draft["renderer"] = "deterministic"
        return draft

    prompt = render_prompt(data, title=seed_title, topic=topic, platform=platform)
    LOG.info("Rendering %s content via external renderer", platform_label(platform))
    try:
        rendered = llm_renderer(prompt)
    except Exception as exc:  # external process/user code
        LOG.error("LLM renderer failed (%s); falling back to the built-in composer", exc)
        rendered = ""

    rendered = (rendered or "").strip()
    if not rendered:
        LOG.warning("LLM renderer returned nothing; falling back to the built-in composer")
        draft = compose(data, title=seed_title, topic=topic, platform=platform)
        draft["renderer"] = "deterministic"
        return draft

    parts = _split_rendered(rendered)
    body = parts["body"]
    min_words = int(data.get("min_words") or MIN_WORDS_DEFAULT)
    floor = max(min_words, spec.min_body_words if spec else 0)
    if word_count(body) < floor:
        LOG.warning(
            "LLM renderer returned %d words (minimum %d); falling back to the built-in composer",
            word_count(body),
            floor,
        )
        draft = compose(data, title=seed_title, topic=topic, platform=platform)
        draft["renderer"] = "deterministic"
        return draft

    max_words = int(data.get("max_words") or 0)
    if max_words:
        body = enforce_word_limit(body, max_words)

    resolved_title = parts["title"] or seed_title
    h1 = H1_RE.search(body)
    if h1 and not parts["title"]:
        heading = body[h1.start():].splitlines()[0].lstrip("#").strip()
        if heading:
            resolved_title = heading
            body = body[h1.end():].lstrip("\n")

    return {
        "title": clamp_title(resolved_title, title_limit(data, platform)),
        "body": body.strip(),
        "summary": parts["summary"] or _first_sentence(body, int(data.get("summary_max_chars") or SUMMARY_MAX_CHARS)),
        "words": word_count(body),
        "renderer": "llm",
    }


def approve_hint() -> str:
    """Human-readable reminder of the manual confirmation step."""
    return (
        "Manual targets are handed out as markdown and left 'queued' until you "
        "confirm them with: python main.py content confirm <id> --platform <id> --url <live-url>"
    )


#: One-line meaning of each content status, for `content list` and the UI.
STATUS_HELP: Dict[str, str] = {
    CONTENT_DRAFT: "composed, not yet approved",
    CONTENT_APPROVED: "human-approved, waiting for a publish slot",
    CONTENT_QUEUED: "handed out for manual submission, awaiting a confirmed URL",
    CONTENT_PUBLISHED: "live on every target platform",
    CONTENT_FAILED: "a publisher reported an error; fix the config and retry",
}


def status_help() -> Dict[str, str]:
    """Status -> meaning, for `content list`, `content show` and the UI."""
    return {status: STATUS_HELP.get(status, "") for status in CONTENT_STATUSES}


__all__ = [
    "DEFAULT_SECTIONS",
    "DISCLOSURE_PATTERNS",
    "HYPE_WORDS",
    "STATUS_HELP",
    "approve_hint",
    "clamp_title",
    "compose",
    "content_data_from_config",
    "enforce_word_limit",
    "forbidden_words",
    "generate",
    "normalise_tags",
    "platform_label",
    "prepare_for_platform",
    "render_prompt",
    "render_template",
    "sections_for",
    "status_help",
    "title_limit",
    "validate",
]
