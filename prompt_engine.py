"""Prompt engine.

One template, two channels. `PROMPT_TEMPLATE` encodes the outreach rules
(problem-first, no hype, concrete CTA, word budgets) and is rendered into:

* a ready-to-send message  -> `generate()`
* a self-contained prompt  -> `render_prompt()`  (feed this to your own LLM,
  or to a local CLI via `--llm`)

The bot itself never calls an LLM API; `--llm` is opt-in and shells out to a
command you configure in `scheduler.llm_command`.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

from logging_setup import get_logger
from models import CHANNEL_EMAIL, CHANNEL_FORUM, CHANNELS, word_count

LOG = get_logger("prompt-engine")

TOKEN_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")

# --------------------------------------------------------------------------- #
# The single source of truth for message rules
# --------------------------------------------------------------------------- #

CHANNEL_RULES: Dict[str, Dict[str, Any]] = {
    CHANNEL_EMAIL: {
        "label": "direct email to a named maintainer / team",
        "word_limit": 200,
        "tone": "professional, concise, peer-to-peer",
        "format": (
            "A subject line of at most 8 words, then a short body. No salutation "
            "longer than one line, no signatures with contact details beyond a "
            "name, project URL and appfoundry link."
        ),
        "structure": (
            "1. Subject line\n"
            "2. One-line greeting naming the recipient\n"
            "3. Why you are writing (the problem solved, not a feature list)\n"
            "4. Two to four concrete capabilities as short bullets\n"
            "5. Links: repository and AppFoundry listing\n"
            "6. A single low-friction call to action\n"
            "7. Signature and an unsubscribe line"
        ),
    },
    CHANNEL_FORUM: {
        "label": "post in the Genesys Cloud community forum / AppFoundry discussion",
        "word_limit": 300,
        "tone": "community member contributing, not a vendor pitching",
        "format": (
            "A markdown title of at most 10 words, then a markdown body with a "
            "short intro, a capability list, links and a discussion question."
        ),
        "structure": (
            "1. Markdown title\n"
            "2. One paragraph of context for community readers\n"
            "3. What the plugin replaces (the problem) and what it includes\n"
            "4. Links: repository and AppFoundry listing\n"
            "5. An explicit ask: feedback, contributions or sponsorship\n"
            "6. Closing line inviting replies"
        ),
    },
}

RULES = """- Open with the problem the recipient already feels, never with a greeting-filler sentence.
- Be specific: name the integration pattern, the Genesys Cloud object, the workflow.
- Professional, peer-to-peer tone. No hype words (revolutionary, game-changing, best-in-class, unlock, supercharge).
- No fabricated metrics, customers, funding or endorsements.
- Exactly one call to action.
- Never invent facts about the recipient; only use the context supplied.
- Respect the word budget strictly. Drop adjectives, not facts."""

# NOTE: placeholders are `{{TOKEN}}` (double braces) because render_template()
# uses those to distinguish substitution from literal braces in the message
# rules. Single-brace `{TOKEN}` names are left verbatim and never resolved.
PROMPT_TEMPLATE = """\
You are writing outreach for an open-source Genesys Cloud plugin to a potential
sponsor (ISV, Genesys partner, enterprise or active community contributor).

OUTPUT FORMAT
{{channel_format}}

TONE
{{channel_tone}}

STRUCTURE
{{channel_structure}}

HARD RULES
{{rules}}

WORD BUDGET
Maximum {{word_limit}} words for the body (excluding the subject line / title and
the signature). Trim to fit - never exceed it.

PLUGIN FACTS (only these may be used)
name: {{plugin_name}}
problem solved: {{value_prop}}
description: {{description}}
features:
{{features}}
repository: {{repo_url}}
AppFoundry listing: {{appfoundry_url}}
maintainer: {{maintainer_name}} <{{maintainer_email}}>
call to action: {{cta}}
sponsorship ask: {{ask}}

RECIPIENT
name: {{sponsor_name}}
context: {{sponsor_context}}

Write only the message. No preamble, no meta commentary, no placeholders in
braces, and never mention that the text was generated or templated."""

# --------------------------------------------------------------------------- #
# Token rendering
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


def channel_rules(channel: str) -> Dict[str, Any]:
    key = (channel or "").strip().lower()
    if key not in CHANNELS:
        raise ValueError(f"unknown channel {channel!r}; expected one of {', '.join(CHANNELS)}")
    return CHANNEL_RULES[key]


def word_limit(channel: str) -> int:
    return int(channel_rules(channel)["word_limit"])


def plugin_data_from_config(config) -> Dict[str, Any]:  # noqa: ANN001 - avoid import cycle
    """Project the `plugin` section into the dict the engine expects."""
    return {
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
        "unsubscribe_note": config.str("plugin.unsubscribe_note"),
    }


def _feature_lines(features: List[str], limit: int) -> str:
    if not features:
        return "  - (no features configured)"
    lines = [f"- {feature.strip()}" for feature in features[:limit] if feature.strip()]
    return "\n".join(lines)


def _context_line(context: Optional[Dict[str, Any]]) -> str:
    if not context:
        return "No additional context available. Keep the message generic."
    bits: List[str] = []
    if context.get("github_url"):
        bits.append(f"GitHub profile {context['github_url']}")
    if context.get("owner_type"):
        bits.append(str(context["owner_type"]))
    if context.get("repos_count"):
        bits.append(f"{context['repos_count']} public repositories")
    if context.get("genesys_repos_count"):
        bits.append(f"{context['genesys_repos_count']} Genesys-tagged repositories")
    if context.get("top_repo"):
        stars = f" ({context['stars']} stars)" if context.get("stars") else ""
        bits.append(f"top repository {context['top_repo']}{stars}")
    if context.get("language"):
        bits.append(f"primary language {context['language']}")
    if context.get("notes"):
        bits.append(str(context["notes"]))
    return "; ".join(bits) if bits else "No additional context available."


# --------------------------------------------------------------------------- #
# Word budget enforcement
# --------------------------------------------------------------------------- #


def enforce_word_limit(text: str, limit: int, *, ellipsis: str = " ...") -> str:
    """Trim at a sentence boundary, then at a word boundary if needed."""
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
# Deterministic composers (default path - no LLM involved)
# --------------------------------------------------------------------------- #


def _signature(plugin: Dict[str, Any], *, include_repo: bool = True) -> List[str]:
    lines = [""]
    name = plugin.get("maintainer_name") or "The maintainers"
    email = plugin.get("maintainer_email") or ""
    if email:
        lines.append(f"-- {name} <{email}>")
    else:
        lines.append(f"-- {name}")
    if include_repo and plugin.get("repo_url"):
        lines.append(str(plugin["repo_url"]))
    return lines


def _link_line(label: str, url: str) -> Optional[str]:
    return f"{label}: {url}" if url else None


def _compose_email(plugin: Dict[str, Any], sponsor_name: str, context: Optional[Dict[str, Any]]) -> List[str]:
    greeting_name = (sponsor_name or "").strip() or "there"
    hook = _personalized_hook(sponsor_name, context)

    body: List[str] = [
        f"Hi {greeting_name},",
        "",
    ]
    if hook:
        body += [hook, ""]
    body += [
        str(plugin.get("value_prop") or plugin.get("description") or "").strip(),
        "",
        f"{plugin.get('name', 'The plugin')} covers the recurring work:",
    ]
    body += [f"- {feature}" for feature in (plugin.get("features") or [])[:4]]
    links = [
        _link_line("Repository", str(plugin.get("repo_url") or "")),
        _link_line("AppFoundry", str(plugin.get("appfoundry_url") or "")),
        _link_line("Demo", str(plugin.get("demo_url") or "")),
    ]
    links = [link for link in links if link]
    if links:
        body += [""] + links
    body += [
        "",
        str(plugin.get("cta") or "Would you be open to a short call about sponsorship?").strip(),
    ]
    body += _signature(plugin)
    return body


def _personalized_hook(sponsor_name: str, context: Optional[Dict[str, Any]]) -> str:
    if not context:
        return ""
    top_repo = str(context.get("top_repo") or "").strip()
    stars = int(context.get("stars") or 0)
    if top_repo:
        suffix = f" ({stars} stars)" if stars else ""
        return f"I have been following your work on {top_repo}{suffix} and the surrounding tooling."
    if context.get("github_url"):
        return f"I have been following your work on {context['github_url']} and the surrounding tooling."
    return ""


def _compose_forum(plugin: Dict[str, Any], sponsor_name: str, context: Optional[Dict[str, Any]]) -> List[str]:
    name = (plugin.get("name") or "the plugin").strip()
    features = (plugin.get("features") or [])[:4]

    body: List[str] = [
        f"Sharing an open-source project for Genesys Cloud teams: **{name}**.",
        "",
        str(plugin.get("value_prop") or plugin.get("description") or "").strip(),
        "",
        "What it includes:",
    ]
    body += [f"- {feature}" for feature in features]
    links = [
        _link_line("Repository", str(plugin.get("repo_url") or "")),
        _link_line("AppFoundry", str(plugin.get("appfoundry_url") or "")),
    ]
    links = [link for link in links if link]
    if links:
        body += [""] + links
    body += [
        "",
        str(plugin.get("ask") or plugin.get("cta") or "Feedback and contributions are welcome.").strip(),
        "",
        "If you are working on something similar, I would be glad to compare approaches - "
        "and if your team would like to support the maintenance work, let me know.",
    ]
    return body


def compose(plugin_data: Dict[str, Any], channel: str, sponsor_name: str) -> str:
    """Build the deterministic message. Subject line included for email."""
    channel = (channel or "").strip().lower()
    plugin = dict(plugin_data or {})
    features = plugin.get("features") or []
    context = plugin.get("sponsor_context") or {}

    if channel == CHANNEL_EMAIL:
        feature_headline = _feature_headline(features)
        subject = f"{feature_headline} for Genesys Cloud teams" if feature_headline else "Open-source Genesys Cloud plugin"
        body_lines = _compose_email(plugin, sponsor_name, context)
        footer = str(plugin.get("unsubscribe_note") or "").strip()
        footer_lines = ["", footer] if footer else []
        limit = word_limit(channel)
        body = enforce_word_limit("\n".join(body_lines), limit)
        chunks = [f"Subject: {subject}", "", body] + footer_lines
        return "\n".join(chunks).strip()

    if channel == CHANNEL_FORUM:
        title = f"Open-source plugin for Genesys Cloud: {_feature_headline(features) or 'community tooling'}"
        body_lines = _compose_forum(plugin, sponsor_name, context)
        limit = word_limit(channel)
        body = enforce_word_limit("\n".join(body_lines), limit)
        return f"Title: {title}\n\n{body}".strip()

    raise ValueError(f"unknown channel {channel!r}; expected one of {', '.join(CHANNELS)}")


def _feature_headline(features: List[str]) -> str:
    for feature in features:
        text = str(feature).strip()
        if text:
            # First clause of the first feature reads best as a headline.
            return text.split(" - ", 1)[0].split(" with ", 1)[0][:60].strip()
    return ""


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def render_prompt(
    plugin_data: Dict[str, Any],
    channel: str,
    sponsor_name: str,
    *,
    sponsor_context: Optional[Dict[str, Any]] = None,
) -> str:
    """Render `PROMPT_TEMPLATE` into a complete LLM prompt."""
    rules = channel_rules(channel)
    plugin = dict(plugin_data or {})
    features = plugin.get("features") or []
    values: Dict[str, Any] = {
        "channel": channel,
        "channel_format": rules["format"],
        "channel_tone": rules["tone"],
        "channel_label": rules["label"],
        "channel_structure": rules["structure"],
        "word_limit": rules["word_limit"],
        "rules": RULES,
        "plugin_name": plugin.get("name", ""),
        "value_prop": plugin.get("value_prop", ""),
        "description": plugin.get("description", ""),
        "features": _feature_lines([str(item) for item in features], 6),
        "repo_url": plugin.get("repo_url", ""),
        "appfoundry_url": plugin.get("appfoundry_url", ""),
        "maintainer_name": plugin.get("maintainer_name", ""),
        "maintainer_email": plugin.get("maintainer_email", ""),
        "cta": plugin.get("cta", ""),
        "ask": plugin.get("ask", ""),
        "sponsor_name": sponsor_name or "the Genesys Cloud community",
        "sponsor_context": _context_line(sponsor_context),
    }
    return render_template(PROMPT_TEMPLATE, values).strip()


def generate(
    plugin_data: Dict[str, Any],
    channel: str,
    sponsor_name: str,
    *,
    llm_renderer: Optional[Callable[[str], str]] = None,
    sponsor_context: Optional[Dict[str, Any]] = None,
) -> str:
    """Produce the outreach message for ``channel``.

    With no ``llm_renderer`` this is fully deterministic template output. Pass a
    callable (see `llm.SubprocessLLM`) to render the same prompt through your own
    local model instead.
    """
    channel = (channel or "").strip().lower()
    channel_rules(channel)  # validate early

    if llm_renderer is None:
        plugin = dict(plugin_data or {})
        plugin["sponsor_context"] = sponsor_context or {}
        return compose(plugin, channel, sponsor_name)

    prompt = render_prompt(plugin_data, channel, sponsor_name, sponsor_context=sponsor_context)
    LOG.info("Rendering %s message via external renderer", channel)
    try:
        rendered = llm_renderer(prompt)
    except Exception as exc:  # external process/user code
        LOG.error("LLM renderer failed (%s); falling back to built-in composer", exc)
        plugin = dict(plugin_data or {})
        plugin["sponsor_context"] = sponsor_context or {}
        return compose(plugin, channel, sponsor_name)

    rendered = (rendered or "").strip()
    if not rendered:
        LOG.warning("LLM renderer returned nothing; falling back to built-in composer")
        plugin = dict(plugin_data or {})
        plugin["sponsor_context"] = sponsor_context or {}
        return compose(plugin, channel, sponsor_name)

    return enforce_word_limit(rendered, word_limit(channel))


def word_count_message(channel: str, message: str) -> int:
    """Body word count (subject/title line excluded)."""
    text = message or ""
    if "\n" in text:
        text = text.split("\n", 1)[1] if text.lower().startswith(("subject:", "title:")) else text
    return word_count(text)