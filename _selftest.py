"""Ad-hoc verification harness (not part of the shipped tool).

Run with:  .venv\Scripts\python.exe _selftest.py
Covers the pieces that are easy to get subtly wrong: markdown->HTML rendering,
the content quality gate, the content store's ledger and rate limits, and the
promoter tick in dry-run and manual mode.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label} {detail}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


def raises(exc_type, fn) -> bool:
    try:
        fn()
    except exc_type:
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"       (raised {type(exc).__name__}: {exc})")
        return False
    return False


# --------------------------------------------------------------------------- #
section("markdown -> html")

from publishers.markdown_html import markdown_to_html, render_inline, strip_front_matter

sample = "\n".join(
    [
        "# Title here",
        "",
        "Intro with **bold**, *italic*, `code` and a [link](https://example.com/a).",
        "",
        "## Section one",
        "",
        "- first item",
        "- second item",
        "",
        "1. one",
        "2. two",
        "",
        "> quoted line",
        "> second quote line",
        "",
        "```python",
        "def f(x):",
        "    return x < 1 and x > 0",
        "```",
        "",
        "| A | B |",
        "|---|---|",
        "| 1 | 2 |",
        "",
        "---",
        "",
        "Tail with https://bare.example.com/p autolinked.",
    ]
)
html = markdown_to_html(sample)
check("h1", "<h1>Title here</h1>" in html)
check("h2", "<h2>Section one</h2>" in html)
check("strong", "<strong>bold</strong>" in html)
check("em", "<em>italic</em>" in html)
check("inline code", "<code>code</code>" in html)
check("link", '<a href="https://example.com/a">link</a>' in html)
check("ul", "<ul><li>first item</li><li>second item</li></ul>" in html)
check("ol", "<ol><li>one</li><li>two</li></ol>" in html)
check("blockquote", html.count("<blockquote>") == 1)
check("fenced code escaped", "return x &lt; 1" in html)
check("code language class", 'class="language-python"' in html)
check("table", "<table><thead><tr><th>A</th><th>B</th></tr></thead>" in html)
check("hr", "<hr>" in html)
check("autolink", '<a href="https://bare.example.com/p">' in html)

danger = markdown_to_html("<script>alert(1)</script> [x](javascript:alert(1)) <b>y</b>")
check("script escaped", "&lt;script&gt;" in danger and "<script>" not in danger)
check("javascript: never becomes an href", 'href="javascript' not in danger.lower())
check("javascript: link degraded to text", "<a " not in danger, danger)
check("raw html escaped", "&lt;b&gt;y&lt;/b&gt;" in danger)
data_uri = markdown_to_html("[x](data:text/html;base64,PHNjcmlwdD4=)")
check("data uri never becomes an href", 'href="data:' not in data_uri.lower() and "<a " not in data_uri)
check(
    "img src scheme filtered",
    'src="data:' not in markdown_to_html("![a](data:image/svg+xml;base64,PHN2Zz4=)").lower(),
)

front, body = strip_front_matter("---\ntitle: x\n---\n# H\n\ntext")
check("front matter split", front.strip() == "title: x" and body.startswith("# H"))
check("no front matter", strip_front_matter("# H") == ("", "# H"))
check("inline empty safe", render_inline("") == "")


# --------------------------------------------------------------------------- #
section("content engine")

import content_engine as ce
from models import ContentItem
from platforms import PLATFORMS

content = {
    "audience": "Genesys Cloud developers",
    "angle": "Stop rebuilding the same integration in every project.",
    "disclosure": "I maintain this project.",
    "disclosure_required": True,
    "tags": ["Genesys", "genesys-cloud", "Open Source", "genesys"],
    "sections": list(ce.DEFAULT_SECTIONS),
    "min_words": 350,
    "max_words": 1800,
    "summary_max_chars": 300,
    "max_title_words": 12,
    "forbid_words": ["kubernetes"],
    "plugin": {
        "name": "Genesys Cloud Community Plugin",
        "description": "Open-source plugin.",
        "value_prop": "Stop rebuilding the same Genesys Cloud integration in every project.",
        "features": ["Drop-in API wrapper", "Event-driven architecture"],
        "repo_url": "https://github.com/purecloudlabs/community-plugin-example",
        "appfoundry_url": "https://appfoundry.genesys.cloud/",
        "demo_url": "",
        "maintainer_name": "Maintainer",
        "maintainer_email": "you@example.com",
        "cta": "Would a 15-minute walkthrough be useful?",
        "ask": "We are looking for sponsors.",
        "license": "MIT",
    },
}

draft = ce.compose(content, title="A tested integration pattern", topic="integration")
words = len(draft["body"].split())
check("composed has h1", draft["body"].startswith("# "))
check("composed discloses", "I maintain this project" in draft["body"])
check("composed links repo", "github.com/purecloudlabs" in draft["body"])
check("composed has >=2 h2", draft["body"].count("\n## ") >= 2)
print(f"       (composed {words} words)")
check("composed meets min_words", words >= 350, f"got {words}")

problems = ce.validate(content, title=draft["title"], body=draft["body"], platform="devto")
check("gate passes own output", problems == [], str(problems))

thin = ce.validate(
    content, title="t", body="# T\n\n## a\n\n## b\n\nhttps://github.com/purecloudlabs/community-plugin-example",
    platform="devto",
)
check("gate flags thin body", any("word minimum" in p for p in thin), str(thin))

hype_body = (
    "# T\n\n## a\n\n## b\n\n"
    + ("word " * 400)
    + "\n\nThis is a seamless integration.\n\n"
    + "https://github.com/purecloudlabs/community-plugin-example\n\n"
    + "I maintain this project."
)
hype = ce.validate(content, title="t", body=hype_body, platform="devto")
check("gate flags hype", any("banned wording" in p for p in hype), str(hype))

forbidden = ce.validate(
    content,
    title="t",
    body=hype_body.replace("seamless", "runs on kubernetes"),
    platform="devto",
)
check("gate honours content.forbid_words", any("kubernetes" in p for p in forbidden), str(forbidden))

nodisc = ce.validate(
    content,
    title="t",
    body="# T\n\n## a\n\n## b\n\n" + ("word " * 400)
    + "\n\nhttps://github.com/purecloudlabs/community-plugin-example",
    platform="devto",
)
check("gate flags missing disclosure", any("disclosure" in p for p in nodisc), str(nodisc))

norepo = ce.validate(
    content,
    title="t",
    body="# T\n\n## a\n\n## b\n\n" + ("word " * 400)
    + "\n\nI maintain this project.",
    platform="devto",
)
check("gate flags missing repo link", any("link to the project" in p for p in norepo), str(norepo))

check(
    "tags normalised",
    ce.normalise_tags(["Genesys Cloud", "#DevTo", "genesys"], "devto") == ["genesyscloud", "devto", "genesys"],
)
check("tags clamped to devto limit", len(ce.normalise_tags(["a", "b", "c", "d", "e", "f"], "devto")) == 4)
check("tags clamped to hashnode limit", len(ce.normalise_tags(["a", "b", "c", "d", "e", "f"], "hashnode")) == 5)
check("title clamped", ce.clamp_title("one two three four five", 3) == "one two three")

check("registry complete", set(PLATFORMS) == {
    "devto", "hashnode", "medium", "wordpress", "coderlegion", "devdojo", "webhook"})
check("manual targets flagged", PLATFORMS["coderlegion"].kind == "manual" and PLATFORMS["devdojo"].kind == "manual")
check("medium flagged legacy", PLATFORMS["medium"].legacy is True)


# --------------------------------------------------------------------------- #
section("content store")

from content_store import GLOBAL_KEY, ContentStore, ContentStoreError

tmp = Path(tempfile.mkdtemp(prefix="selftest-"))
try:
    store = ContentStore(
        tmp / "content.json",
        rate_limits={"devto": 1, "hashnode": 1, "coderlegion": 5, GLOBAL_KEY: 2},
    )
    item = ContentItem(
        id="post-one",
        title=draft["title"],
        body_markdown=draft["body"],
        summary=draft["summary"],
        platforms=["devto", "coderlegion"],
        tags=["genesys"],
    )
    item.fingerprint = item.compute_fingerprint()
    stored, created = store.add(item)
    check("item created", created and stored.id == "post-one")
    check("status starts draft", stored.status == "draft")
    store.save()

    reopened = ContentStore(
        tmp / "content.json",
        rate_limits={"devto": 1, "hashnode": 1, "coderlegion": 5, GLOBAL_KEY: 2},
    )
    check("round-trips through disk", reopened.get("post-one") is not None)
    check("publications serialised", isinstance(reopened.get("post-one").publications, dict))

    check("not publishable before approval", reopened.next_targets(5) == [])

    reopened.approve("post-one")
    targets = reopened.next_targets(5)
    check("fan-out produces one target per platform", len(targets) == 2)
    check("targets ordered by item platform list", [t.platform for t in targets] == ["devto", "coderlegion"])

    reopened.record_publish(targets[0], ok=True, mode="api", live=True, url="https://dev.to/x/one", external_id="9")
    reopened.save()
    item = reopened.require("post-one")
    check("api publish marks live", item.publication("devto").is_live)
    check("partial progress stays approved", item.status == "approved", item.status)
    check("devto counter bumped", reopened.used_today("devto") == 1)

    remaining = reopened.next_targets(5)
    check("live target no longer queued", [t.platform for t in remaining] == ["coderlegion"])

    manual_target = remaining[0]
    reopened.record_publish(manual_target, ok=True, mode="manual", detail="written to outbox")
    reopened.save()
    item = reopened.require("post-one")
    check("manual publish is queued not published", item.status == "queued", item.status)
    check("manual has no url", item.publication("coderlegion").url == "")
    check("manual sets queued_at", bool(item.publication("coderlegion").queued_at))

    reopened.confirm("post-one", "coderlegion", url="https://coderlegion.com/1/one")
    item = reopened.require("post-one")
    check("confirm reaches published", item.status == "published", item.status)
    check("both urls recorded", len(item.live_urls()) == 2, str(item.live_urls()))
    check("nothing left to publish", reopened.next_targets(5) == [])

    try:
        reopened.confirm("post-one", "coderlegion", url="not-a-url")
        check("confirm rejects non-url", False)
    except ContentStoreError:
        check("confirm rejects non-url", True)

    reopened.unconfirm("post-one", "coderlegion")
    item = reopened.require("post-one")
    check("unconfirm drops back to approved", item.status == "approved", item.status)
    check("unconfirm clears url", item.publication("coderlegion").url == "")
    check("unconfirm makes it publishable again", len(reopened.next_targets(5)) == 1)

    # per-platform cap / missing-limit safety
    reopened.record_publish(reopened.next_targets(1)[0], ok=False, mode="api", detail="boom")
    capped = reopened.next_targets(1)
    check("failed target is offered again", len(capped) == 1)
    allowed, reason = reopened.can_publish(capped[0])
    check("global cap bites", not allowed and "global" in reason, reason)
    check("global counter includes failures", reopened.used_today(GLOBAL_KEY) == 3,
          str(reopened.used_today(GLOBAL_KEY)))

    unconfigured = ContentStore(tmp / "c5.json", rate_limits={})
    unconfigured.add(ContentItem(id="p11", title="t", body_markdown="# t", platforms=["devdojo"]))
    unconfigured.approve("p11")
    no_limit, why = unconfigured.can_publish(unconfigured.next_targets(1)[0])
    check("missing limit is refused, not unlimited", not no_limit and "no daily publish limit" in why, why)

    # cooldown: a manual hand-off that has not been confirmed must not be
    # re-dumped every tick
    store2 = ContentStore(tmp / "c2.json", rate_limits={"coderlegion": 5, GLOBAL_KEY: 50})
    i2 = ContentItem(id="p2", title="t", body_markdown="# t\n\nbody", platforms=["coderlegion"])
    store2.add(i2)
    store2.approve("p2")
    store2.record_publish(store2.next_targets(1)[0], ok=True, mode="manual")
    retry = store2.next_targets(1)
    check("unconfirmed manual target is still pending", len(retry) == 1)
    ok2, reason2 = store2.can_publish(retry[0], min_hours_between=24)
    check("cooldown blocks re-dumping a queued file", not ok2 and "cooling" in reason2, reason2)
    ok3, reason3 = store2.can_publish(retry[0])
    check("cooldown off means no wait", ok3, reason3)
    store2.unconfirm("p2", "coderlegion")
    ok5, reason5 = store2.can_publish(store2.next_targets(1)[0], min_hours_between=24)
    check("unconfirm is a deliberate override", ok5, reason5)

    # a failed attempt still starts the cooldown
    store2b = ContentStore(tmp / "c2b.json", rate_limits={"devto": 5, GLOBAL_KEY: 50})
    i2b = ContentItem(id="p2b", title="t", body_markdown="# t", platforms=["devto"])
    store2b.add(i2b)
    store2b.approve("p2b")
    store2b.record_publish(store2b.next_targets(1)[0], ok=False, mode="api", detail="HTTP 401")
    ok4, reason4 = store2b.can_publish(store2b.next_targets(1)[0], min_hours_between=24)
    check("failed attempt also cools down", not ok4 and "cooling" in reason4, reason4)

    # corrupt quarantine
    bad = tmp / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    store3 = ContentStore(bad, rate_limits={"devto": 1})
    check("corrupt file quarantined", (tmp / "bad.json.corrupt").is_file())
    check("corrupt file self-heals", store3.path.is_file() and store3.all() == [])

    # duplicate refresh keeps publication history
    store4 = ContentStore(tmp / "c4.json", rate_limits={"devto": 5, GLOBAL_KEY: 50})
    i4 = ContentItem(id="p4", title="t", body_markdown="# t", platforms=["devto"])
    store4.add(i4)
    store4.approve("p4")
    store4.record_publish(store4.next_targets(1)[0], ok=True, mode="api", live=True, url="https://dev.to/y")
    refreshed = ContentItem(id="p4", title="t", body_markdown="# t v2", platforms=["devto", "hashnode"])
    merged, was_new = store4.add(refreshed)
    check("refresh not duplicated", was_new is False)
    check("refresh keeps live url", merged.publication("devto").url == "https://dev.to/y")
    check("refresh updates body", merged.body_markdown == "# t v2")
    check("refresh merges platforms", merged.platforms == ["devto", "hashnode"])

    check("unknown platform rejected", raises(ContentStoreError, lambda: store4.add(
        ContentItem(id="p9", title="t", body_markdown="# t", platforms=["nope"]))))
    check("missing platform rejected", raises(ContentStoreError, lambda: store4.add(
        ContentItem(id="p10", title="t", body_markdown="# t", platforms=[]))))
    check("empty body rejected", raises(ContentStoreError, lambda: store4.add(
        ContentItem(id="p11", title="t", body_markdown="   ", platforms=["devto"]))))
finally:
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
section("promoter (dry run + manual)")

from config_loader import Config

work = Path(tempfile.mkdtemp(prefix="selftest-cfg-"))
import yaml

try:

    cfg = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    cfg["content"] = dict(content, enabled=True)
    cfg["platforms"] = {
        "devto": {"enabled": True},
        "coderlegion": {"enabled": True},
        "devdojo": {"enabled": True},
    }
    cfg["publishing"] = {"dry_run": True, "batch_size": 5, "require_approval": True}
    cfg["rate_limits"] = cfg.get("rate_limits") or {}
    cfg["rate_limits"].update(
        {
            "max_publishes_per_day": 100,
            "min_hours_between_platform_posts": 0,
            "platform_daily_limits": {
                "devto": 5,
                "hashnode": 5,
                "wordpress": 5,
                "coderlegion": 5,
                "devdojo": 5,
            },
        }
    )
    cfg["paths"] = cfg.get("paths") or {}
    cfg["paths"]["content_file"] = "content.json"
    cfgpath = work / "config.yaml"
    cfgpath.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def isolated(name: str, mutate=None) -> "Config":
        """A fresh config + its own content.json, so sub-tests do not share state."""
        document = yaml.safe_load(cfgpath.read_text(encoding="utf-8"))
        if mutate:
            mutate(document)
        document["paths"]["content_file"] = f"{name}.json"
        target = work / f"config-{name}.yaml"
        target.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return Config.load(target, create_from_example=False)

    config = Config.load(cfgpath, create_from_example=False)
    check("config validates", True)
    check("publishing_enabled true", config.publishing_enabled is True)
    check("platforms_enabled", set(config.platforms_enabled()) == {"devto", "coderlegion", "devdojo"})

    from promoter import Promoter, build_store

    store = build_store(config)
    check("store file resolved under base_dir", store.path.parent == work.resolve(), str(store.path))

    promoter = Promoter(config, store, dry_run=True, batch_size=5)
    item = ContentItem(
        id="dry-one",
        title=draft["title"],
        body_markdown=draft["body"],
        summary=draft["summary"],
        platforms=["devto", "coderlegion"],
        tags=["genesys"],
    )
    store.add(item)
    store.approve("dry-one")

    result = promoter.tick()
    check("dry run attempted 2", result.attempted == 2, json.dumps(result.to_dict()))
    check("dry run published nothing", result.published == 0)
    check("dry run leaves quota alone", store.used_today("devto") == 0)
    check("dry run notes each pair", len([n for n in result.notes if "dry run" in n]) == 2, str(result.notes))
    check("dry run does not mark live", not store.require("dry-one").publication("devto").is_live)
    check("dry run records no attempt", store.require("dry-one").publication("devto").attempts == 0)
    check("dry run is repeatable", promoter.tick().attempted == 2)
    check("dry run writes no outbox", not (work / "content_outbox" / "coderlegion" / "dry-one.md").exists())

    # unapproved item is skipped
    store.add(ContentItem(id="draft-one", title="x", body_markdown="# x", platforms=["devto"]))
    result2 = promoter.tick()
    check("unapproved draft not attempted", all(r.item_id != "draft-one" for r in result2.records))

    # live run: manual target, no credentials needed
    cfg_live = isolated("live", lambda d: d["publishing"].update({"dry_run": False}))
    store2 = build_store(cfg_live)
    store2.add(ContentItem(
        id="manual-one", title=draft["title"], body_markdown=draft["body"],
        summary=draft["summary"], platforms=["devdojo", "coderlegion"], tags=["genesys"],
    ))
    store2.approve("manual-one")
    live = Promoter(cfg_live, store2, dry_run=False, batch_size=5)
    live.request_delay = 0.0
    import contextlib
    import io

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        res = live.tick(platforms=["devdojo", "coderlegion"])
    printed = captured.getvalue()
    check("manual publish counted as queued", res.queued == 2, json.dumps(res.to_dict()))
    check("manual not counted as published", res.published == 0)
    check("manual outcome is not live", all(not r.live for r in res.records))
    stored = store2.require("manual-one")
    check("manual status queued", stored.status == "queued", stored.status)
    outbox = work / "content_outbox" / "devdojo" / "manual-one.md"
    check("outbox file written", outbox.is_file())
    check("outbox header present", "submit at:" in outbox.read_text(encoding="utf-8"))
    check("outbox has the article body", "# " + draft["title"] in outbox.read_text(encoding="utf-8"))
    check("manual consumed quota", store2.used_today("devdojo") == 1)
    check("console shows submission steps", "MANUAL POST" in printed and "content confirm" in printed)
    check("console names the platform", "DevDojo" in printed and "CoderLegion" in printed)

    # devto live run without a token must be a skip, never a crash
    store2.add(ContentItem(
        id="needs-token", title=draft["title"], body_markdown=draft["body"],
        summary=draft["summary"], platforms=["devto"], tags=["genesys"],
    ))
    store2.approve("needs-token")
    with contextlib.redirect_stdout(io.StringIO()):
        res3 = live.tick(platforms=["devto"])
    check("missing credential is a skip", res3.skipped == 1 and res3.failed == 0, json.dumps(res3.to_dict()))
    check("skip names the missing env var", "DEVTO_API_KEY" in res3.notes[0], str(res3.notes))
    check("skipped pair is still pending", store2.require("needs-token").publication("devto").status == "pending")
    check("skipped pair consumed no quota", store2.used_today("devto") == 0)

    # quality gate blocks a thin article
    cfg_gate = isolated("gate", lambda d: d["publishing"].update({"dry_run": False}))
    store3 = build_store(cfg_gate)
    store3.add(ContentItem(id="thin", title="Thin", body_markdown="# Thin\n\nshort", platforms=["devdojo"]))
    store3.approve("thin")
    gate = Promoter(cfg_gate, store3, dry_run=False, batch_size=5)
    gate.request_delay = 0.0
    with contextlib.redirect_stdout(io.StringIO()):
        res4 = gate.tick(platforms=["devdojo"])
    check("quality gate blocks thin draft", res4.skipped == 1 and res4.attempted == 0, json.dumps(res4.to_dict()))
    check("gate failure is recorded as a problem", "quality gate" in res4.notes[0])
    check("gate blocks before any request", store3.used_today("devdojo") == 0)

    # ... unless the operator turns the gate off
    cfg_lax = isolated(
        "lax",
        lambda d: d["publishing"].update({"enforce_quality_gate": False, "dry_run": True}),
    )
    store4 = build_store(cfg_lax)
    store4.add(ContentItem(id="thin2", title="Thin", body_markdown="# Thin\n\nshort", platforms=["devdojo"]))
    store4.approve("thin2")
    lax = Promoter(cfg_lax, store4, dry_run=True, batch_size=5)
    res5 = lax.tick(platforms=["devdojo"])
    check("gate can be downgraded", res5.attempted == 1, json.dumps(res5.to_dict()))

    # platforms report
    from publishers import check_all

    report = check_all(config)
    check("check_all covers every platform", len(report) == 7, str(len(report)))
    by_id = {entry["platform"]: entry for entry in report}
    check("devto reports missing token", by_id["devto"]["ready"] is False)
    check("devto reason names the env var", "DEVTO_API_KEY" in by_id["devto"]["reason"], by_id["devto"]["reason"])
    check("hashnode reports missing token", "HASHNODE_PAT" in by_id["hashnode"]["reason"])
    check("manual target is ready", by_id["devdojo"]["ready"] is True)
    check("medium flagged legacy in report", by_id["medium"]["legacy"] is True)
    check("no report leaks a secret", all("Bearer" not in str(entry.get("reason", "")) for entry in report))

    # with a token present, hashnode's next requirement is the publication id
    import os

    os.environ["HASHNODE_PAT"] = "test-token-not-real"
    try:
        by_id2 = {entry["platform"]: entry for entry in check_all(config)}
        check("hashnode then demands publication_id", "publication_id" in by_id2["hashnode"]["reason"], by_id2["hashnode"]["reason"])
    finally:
        os.environ.pop("HASHNODE_PAT", None)
finally:
    shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
section("publisher payloads (shaped, never transmitted)")

import contextlib
import io
import os as _os
from typing import List

import content_engine as ce2
from config_loader import Config
from models import PLATFORM_IDS
from platforms import PLATFORMS
from publishers import get_publisher
from webapp.service import content_data_from_config

CANONICAL = "https://blog.example.com/posts/stop-refreshing-tokens"

pw = Path(tempfile.mkdtemp(prefix="selftest-pay-"))
try:
    payload_doc = yaml.safe_load(
        (Path(__file__).resolve().parent / "config.example.yaml").read_text(encoding="utf-8")
    )
    payload_doc["content"].update(
        {"enabled": True, "audience": content["audience"], "angle": content["angle"],
         "disclosure": content["disclosure"], "canonical_base_url": CANONICAL}
    )
    for pid in PLATFORM_IDS:
        payload_doc.setdefault("platforms", {}).setdefault(pid, {})["enabled"] = True
    payload_doc["platforms"]["devto"]["state_published"] = True
    payload_doc["platforms"]["hashnode"]["publication_id"] = "65d99cd9447d15d98a2fc264"
    payload_doc["platforms"]["wordpress"]["site"] = "example.wordpress.com"
    payload_doc["platforms"]["medium"]["author_id"] = "abc123"
    payload_doc["rate_limits"]["max_publishes_per_day"] = 50
    payload_doc["rate_limits"]["min_hours_between_platform_posts"] = 0
    (pw / "config.yaml").write_text(
        yaml.safe_dump(payload_doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    pconfig = Config.load(pw / "config.yaml", create_from_example=False)

    article = ContentItem(
        id="payload-one",
        title="Stop refreshing tokens by hand",
        body_markdown=draft["body"],
        summary=draft["summary"],
        platforms=list(PLATFORM_IDS),
        tags=["genesys", "genesys-cloud", "opentelemetry", "opensource", "cloud", "extra1", "extra2"],
        canonical_url=CANONICAL,
    )
    shaped = {
        pid: ce2.prepare_for_platform(
            content_data_from_config(pconfig),
            title=article.title,
            body=article.body_markdown,
            summary=article.summary,
            tags=article.tags,
            platform=pid,
        )
        for pid in PLATFORM_IDS
    }

    for name, value in (
        ("DEVTO_API_KEY", "fake-devto-key"),
        ("HASHNODE_PAT", "fake-hashnode-pat"),
        ("MEDIUM_TOKEN", "fake-medium-token"),
        ("WP_USERNAME", "fake-user"),
        ("WP_APP_PASSWORD", "fake-app-password"),
        ("WEBHOOK_URL", "https://example.invalid/hook"),
    ):
        _os.environ[name] = value

    for pid in PLATFORM_IDS:
        probe = get_publisher(pid, pconfig, dry_run=True).check()
        check(f"{pid}: ready with credentials present", probe["ready"] is True, str(probe.get("reason")))

    devto = get_publisher("devto", pconfig, dry_run=True)
    article_payload = devto.build_article(article, shaped["devto"])
    check("devto: nested under 'article'",
          set(article_payload) >= {"title", "body_markdown", "published", "tags"})
    check("devto: markdown body passed through",
          article_payload["body_markdown"] == article.body_markdown)
    check("devto: tags clamped to 4", len(article_payload["tags"]) == 4, str(article_payload["tags"]))
    check("devto: canonical_url forwarded", article_payload.get("canonical_url") == CANONICAL)
    check("devto: description set", bool(article_payload.get("description")))
    check("devto: published flag follows config", article_payload["published"] is True)
    check("devto: content_format is markdown", shaped["devto"]["content_format"] == "markdown")
    check("devto: payload carries no credential", "fake-devto-key" not in str(article_payload))
    check("devto: dry run transmits nothing",
          devto.publish(article, shaped["devto"]).dry_run is True)

    # A draft-vs-live decision is asserted on the payload, never by calling
    # publish() in live mode: this harness must not transmit to any third party.
    draft_devto = get_publisher("devto", pconfig, dry_run=True)
    check("devto: published=true by default here",
          draft_devto.build_article(article, shaped["devto"])["published"] is True)
    draft_devto.state_published = False
    check("devto: draft mode sets published=false",
          draft_devto.build_article(article, shaped["devto"])["published"] is False)

    # Hard guarantee that dry_run short-circuits before any socket is opened.
    for platform_id in ("devto", "hashnode", "medium", "wordpress", "webhook"):
        probe = get_publisher(platform_id, pconfig, dry_run=True)
        opened: List[str] = []

        def _tripwire(*_args, **_kwargs):  # noqa: ANN002, ANN003
            opened.append(platform_id)
            raise AssertionError(f"{platform_id} attempted a network call during a dry run")

        probe._request = _tripwire  # type: ignore[method-assign]
        probe.publish(article, shaped[platform_id])
        check(f"{platform_id}: dry run opens no socket", not opened, str(opened))

    hashnode = get_publisher("hashnode", pconfig, dry_run=True)
    hn = hashnode.build_input(article, shaped["hashnode"])
    check("hashnode: publicationId present", hn["publicationId"] == "65d99cd9447d15d98a2fc264")
    check("hashnode: contentMarkdown field", hn["contentMarkdown"] == article.body_markdown)
    check("hashnode: tags are slug objects",
          all(isinstance(tag, dict) and "slug" in tag for tag in hn["tags"]))
    check("hashnode: tags clamped to 5", len(hn["tags"]) == 5, str(hn["tags"]))
    check("hashnode: canonical via originalArticleURL", hn.get("originalArticleURL") == CANONICAL)
    check("hashnode: enableToc present", hn.get("enableToc") is True)
    check("hashnode: payload carries no credential", "fake-hashnode-pat" not in str(hn))

    wordpress = get_publisher("wordpress", pconfig, dry_run=True)
    wp = wordpress.build_post(article, shaped["wordpress"])
    check("wordpress: content is html not markdown",
          "<h1>" in wp["content"] and "\n## " not in wp["content"])
    check("wordpress: no script tag survives", "<script" not in wp["content"].lower())
    check("wordpress: repo link converted to an anchor",
          '<a href="https://github.com/purecloudlabs/community-plugin-example">' in wp["content"])
    check("wordpress: title and status", wp["title"] == article.title and wp["status"] == "publish")
    check("wordpress: tags comma separated", isinstance(wp["tags"], str) and "," in wp["tags"])
    check("wordpress: endpoint is the v1.1 sites path",
          wordpress.endpoint().endswith("/sites/example.wordpress.com/posts/new"),
          wordpress.endpoint())
    check("wordpress: basic auth header built",
          wordpress.auth_headers()["Authorization"].startswith("Basic "))
    self_hosted = get_publisher("wordpress", pconfig, dry_run=True)
    self_hosted.flavor = "self_hosted"
    self_hosted.site_url = "https://blog.example.com"
    self_hosted.api_base = self_hosted._default_api_base()
    check("wordpress: self_hosted endpoint",
          self_hosted.endpoint() == "https://blog.example.com/wp-json/wp/v2/posts",
          self_hosted.endpoint())

    medium = get_publisher("medium", pconfig, dry_run=True)
    md = medium.build_post(article, shaped["medium"])
    check("medium: html body", "<h1>" in md["body"])
    check("medium: license + licenseUrl",
          md["license"] == "all-rights-reserved"
          and md["licenseUrl"].startswith("https://medium.com/policy"))
    check("medium: publishStatus public", md["publishStatus"] == "public")
    check("medium: endpoint uses the author id",
          medium.endpoint().endswith("/users/abc123/posts"), medium.endpoint())
    medium_md = get_publisher("medium", pconfig, dry_run=True)
    medium_md.content_format = "markdown"
    check("medium: markdown mode passes through",
          medium_md.build_post(article, shaped["medium"])["body"] == article.body_markdown)

    webhook = get_publisher("webhook", pconfig, dry_run=True)
    wh = webhook.build_payload(article, shaped["webhook"])
    check("webhook: has item/markdown/platforms/source",
          set(wh) == {"item", "platforms", "source", "markdown"}, str(set(wh)))
    check("webhook: item carries the canonical url", wh["item"]["canonical_url"] == CANONICAL)
    check("webhook: markdown included", wh["markdown"] == article.body_markdown)
    check("webhook: no credential in payload",
          "fake-app-password" not in str(wh) and "fake-devto-key" not in str(wh))

    coderlegion = get_publisher("coderlegion", pconfig, dry_run=False)
    front = coderlegion._front_matter(shaped["coderlegion"], shaped["coderlegion"]["tags"])
    check("coderlegion: front matter suggests tags", "Suggested tags:" in front)
    check("coderlegion: front matter suggests a category", "Category:" in front)
    check("coderlegion: instructions name the confirm command",
          any("content confirm" in step
              for step in coderlegion.submission_instructions(article, article.title)))

    with contextlib.redirect_stdout(io.StringIO()):
        manual_out = coderlegion.publish(article, shaped["coderlegion"])
    check("coderlegion: publish reports queued not live",
          manual_out.ok and manual_out.mode == "manual" and not manual_out.live)
    check("coderlegion: outbox file written", Path(manual_out.outbox_path).is_file(),
          manual_out.outbox_path)
    check("coderlegion: outbox carries the body",
          article.body_markdown[:60] in Path(manual_out.outbox_path).read_text(encoding="utf-8"))

    devdojo = get_publisher("devdojo", pconfig, dry_run=True)
    check("devdojo: submit url is the write form",
          devdojo.submit_url.endswith("/community/posts/write"), devdojo.submit_url)

    for name in ("DEVTO_API_KEY", "HASHNODE_PAT", "MEDIUM_TOKEN",
                 "WP_USERNAME", "WP_APP_PASSWORD", "WEBHOOK_URL"):
        _os.environ.pop(name, None)
finally:
    shutil.rmtree(pw, ignore_errors=True)


# --------------------------------------------------------------------------- #
section("tracker regression")

tw = Path(tempfile.mkdtemp(prefix="selftest-trk-"))
try:
    from tracker import SponsorTracker

    tpath = tw / "sponsors.json"
    tracker = SponsorTracker(tpath, seed_sponsors=[{"name": "Acme", "email": "a@b.com"}], rate_limits={"email": 2, "forum": 1})
    check("seeded", tracker.get("Acme") is not None)
    ok, _ = tracker.can_send(tracker.require("Acme"))
    check("can send initially", ok)
    tracker.record_send("Acme", "email", ok=True)
    tracker.save()
    check("counter bumped", tracker.used_today("email") == 1)
    ok2, reason = tracker.can_send(tracker.require("Acme"), min_hours_between_attempts=72)
    check("cooldown applies", not ok2 and "cooling" in reason, reason)
    tracker.record_send("Acme", "email", ok=True)
    tracker.save()
    ok3, reason3 = tracker.can_send(tracker.require("Acme"))
    check("daily cap applies", not ok3 and "limit reached" in reason3, reason3)
    check("status contacted", tracker.require("Acme").status == "contacted")
    summary = tracker.summary()
    check("summary shape intact", set(summary) >= {"tracker_file", "total", "by_status", "by_channel", "daily_usage", "recent_history"})
    check("daily_usage has both channels", set(summary["daily_usage"]) == {"email", "forum"})

    reloaded = SponsorTracker(tpath, rate_limits={"email": 2, "forum": 1})
    check("counters survive reload", reloaded.used_today("email") == 2)
    check("history survives reload", len(reloaded.history()) >= 2)

    tbad = tw / "bad.json"
    tbad.write_text("}}}", encoding="utf-8")
    SponsorTracker(tbad, rate_limits={"email": 1, "forum": 1})
    check("tracker quarantines corrupt file", (tw / "bad.json.corrupt").is_file())
finally:
    shutil.rmtree(tw, ignore_errors=True)


# --------------------------------------------------------------------------- #
print()
if FAILURES:
    print(f"FAILED {len(FAILURES)}: " + "; ".join(FAILURES))
    raise SystemExit(1)
print("all self-tests passed")
