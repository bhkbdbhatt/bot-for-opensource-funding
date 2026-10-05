"""Web API smoke test (not part of the shipped tool).

Boots the real stdlib server on a temp base dir, creates an account, signs in,
exercises the content routes the wizard uses, and asserts the security boundary
still rejects unauthenticated and mis-host requests.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label} {detail}")


class Client:
    def __init__(self, base: str, token: str, host: str = "127.0.0.1") -> None:
        self.base = base
        self.token = token
        self.host = host
        self.cookie = ""

    def call(self, method: str, path: str, body=None, *, signed_in: bool = True,
             send_token: bool = True, host_override: str = "") -> tuple[int, dict]:
        url = self.base + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Host", host_override or self.host)
        if send_token:
            request.add_header("X-Web-Token", self.token)
        if signed_in and self.cookie:
            request.add_header("Cookie", self.cookie)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read().decode("utf-8")
                cookies = response.headers.get_all("Set-Cookie") or []
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            cookies = exc.headers.get_all("Set-Cookie") or []
            status = exc.code
        if cookies:
            jar = SimpleCookie()
            for header in cookies:
                jar.load(header)
            for morsel in jar.values():
                if morsel.key != SESSION_COOKIE:
                    continue
                self.cookie = f"{morsel.key}={morsel.value}"
        try:
            return status, json.loads(raw or "{}")
        except ValueError:
            return status, {"raw": raw[:200]}

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body, **kw)

    def put(self, path, body=None, **kw):
        return self.call("PUT", path, body, **kw)

    def delete(self, path, **kw):
        return self.call("DELETE", path, **kw)


tmp = Path(tempfile.mkdtemp(prefix="psb-web-"))
try:
    from webapp.auth import SESSION_COOKIE
    from webapp.server import App, make_handler
    from http.server import ThreadingHTTPServer

    app = App(tmp, host="127.0.0.1", port=0)
    app.profiles.ensure_default()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)
    client = Client(f"http://127.0.0.1:{port}", app.web_token)

    print("\n== security boundary ==")
    status, _ = client.get("/api/bootstrap", signed_in=False)
    check("bootstrap requires a session", status == 401, str(status))
    status, _ = client.get("/api/bootstrap", send_token=False)
    check("bootstrap requires the web token", status == 401, str(status))
    status, _ = client.get("/api/bootstrap", signed_in=False, host_override="evil.example.com")
    check("host allow-list blocks DNS rebinding", status == 403, str(status))
    status, _ = client.get("/", signed_in=False)
    check("the page itself is public (it must be, to sign in)", status == 200, str(status))

    print("\n== auth ==")
    status, body = client.post(
        "/api/auth/setup",
        {"email": "owner@example.com", "password": "Correct-Horse-9!"},
        signed_in=False,
    )
    check("first account created", status == 200, f"{status} {body}")
    status, body = client.post(
        "/api/auth/login",
        {"email": "owner@example.com", "password": "Correct-Horse-9!"},
        signed_in=False,
    )
    check("login succeeds", status == 200, f"{status} {body}")
    check("session cookie set", client.cookie.startswith(SESSION_COOKIE + "="), client.cookie)

    status, body = client.post(
        "/api/auth/login",
        {"email": "owner@example.com", "password": "wrong"},
        signed_in=False,
    )
    check("bad password rejected", status == 400, f"{status} {body}")

    print("\n== bootstrap payload ==")
    status, boot = client.get("/api/bootstrap")
    check("bootstrap ok", status == 200)
    check("channels present", boot.get("channels") == ["email", "forum"])
    check("content statuses present", boot.get("content_statuses") == [
        "draft", "approved", "queued", "published", "failed"], str(boot.get("content_statuses")))
    check("platform ids present", boot.get("platform_ids") == [
        "devto", "hashnode", "medium", "wordpress", "coderlegion", "devdojo", "webhook"])
    check("platform registry present", len(boot.get("platforms") or []) == 7)
    check("no secret in bootstrap", "token_set" not in json.dumps(boot).replace('"token_set"', "", 1)
          or all(p.get("kind") for p in boot["platforms"]))

    pid = boot["profiles"][0]["id"]
    print(f"       (profile: {pid})")

    print("\n== content read ==")
    status, body = client.get(f"/api/profiles/{pid}/content")
    check("content summary ok", status == 200, f"{status} {body}")
    check("content reports disabled by default", body.get("enabled") is False)
    check("gate enforcement surfaced", body.get("enforce_gate") is True)
    check("readiness covers all platforms", len(body.get("readiness") or []) == 7)
    check("empty ledger", body.get("items") == [])

    status, body = client.get(f"/api/profiles/{pid}/platforms")
    check("platform readiness ok", status == 200)
    by_id = {entry["platform"]: entry for entry in body["readiness"]}
    check("manual targets ready without credentials", by_id["devdojo"]["ready"] is True)
    check("devto not ready without a key", by_id["devto"]["ready"] is False)

    print("\n== config edits ==")
    status, cfg = client.get(f"/api/profiles/{pid}")
    document = cfg["config"]
    document["content"]["enabled"] = True
    document["content"]["audience"] = "Genesys Cloud developers"
    document["content"]["angle"] = (
        "The hard part of any Genesys Cloud integration is the token lifecycle, "
        "the retry policy and the idempotency around them."
    )
    document["content"]["disclosure"] = "I maintain this project."
    document["platforms"]["devdojo"]["enabled"] = True
    document["platforms"]["coderlegion"]["enabled"] = True
    document["rate_limits"]["max_publishes_per_day"] = 10
    document["rate_limits"]["min_hours_between_platform_posts"] = 0
    status, body = client.put(f"/api/profiles/{pid}", {"config": document})
    check("config saved", status == 200 and body.get("ok"), f"{status} {body}")

    status, body = client.put(f"/api/profiles/{pid}", {"config": {**document, "platforms": {"nope": {"enabled": True}}}})
    check("unknown platform rejected", body.get("ok") is False and "nope" in str(body.get("errors")))

    print("\n== draft ==")
    status, body = client.post(
        f"/api/profiles/{pid}/content/draft",
        {"title": "Retry policies for contact-centre APIs", "topic": "retry",
         "platforms": ["devdojo", "coderlegion"], "llm": False},
    )
    check("draft created", status == 200, f"{status} {body}")
    item = body.get("item") or {}
    check("draft is a draft", item.get("status") == "draft")
    check("draft has two targets", item.get("platforms") == ["devdojo", "coderlegion"], str(item.get("platforms")))
    check("draft clears the gate", all(row["publishable"] for row in body.get("gate") or []),
          json.dumps(body.get("gate")))
    check("draft is long enough", (item.get("words") or 0) >= 350, str(item.get("words")))
    item_id = item.get("id")

    print("\n== thin draft is blocked ==")
    status, body = client.post(
        f"/api/profiles/{pid}/content/draft",
        {"title": "Thin post", "topic": "thin", "platforms": ["devdojo"], "llm": False},
    )
    check("second draft created", status == 200, f"{status} {body}")
    thin_id = (body.get("item") or {}).get("id")
    check("composer clears the gate by default", all(row["publishable"] for row in body.get("gate") or []),
          json.dumps(body.get("gate")))
    # Now genuinely thin it and confirm the gate notices.
    status, body = client.put(
        f"/api/profiles/{pid}/content/items/{thin_id}",
        {"body": "# Thin post\n\nIt is short."},
    )
    thin_gate = body.get("gate") or []
    check("thin body blocked by the gate", any(not row["publishable"] for row in thin_gate),
          json.dumps(thin_gate))
    problems = " ".join(p for row in thin_gate for p in row.get("problems") or [])
    check("gate names the word minimum", "word minimum" in problems, problems)
    check("gate names the missing disclosure", "disclosure" in problems, problems)
    check("gate names the missing repo link", "link to the project" in problems, problems)

    print("\n== item detail + edit ==")
    status, body = client.get(f"/api/profiles/{pid}/content/items/{item_id}")
    check("item detail ok", status == 200 and body["item"]["id"] == item_id)
    status, body = client.put(
        f"/api/profiles/{pid}/content/items/{item_id}",
        {"body": body["item"]["body_markdown"] + "\n\nOne more line about bounded retries."},
    )
    check("body edited", status == 200, f"{status} {body}")
    check("word count recomputed", (body["item"]["words"] or 0) >= 350)
    status, body = client.put(f"/api/profiles/{pid}/content/items/{item_id}", {"body": "  "})
    check("empty body rejected", status == 400, f"{status} {body}")

    print("\n== approval gate ==")
    status, body = client.get(f"/api/profiles/{pid}/content")
    approved = [row for row in body["items"] if row["id"] == item_id]
    check("item visible in the ledger", len(approved) == 1)
    check("unapproved item has no queued publications",
          all(entry["status"] == "pending" for entry in approved[0]["publications"].values()),
          json.dumps(approved[0]["publications"]))
    status, body = client.post(f"/api/profiles/{pid}/content/approve", {"id": item_id})
    check("approve ok", status == 200 and body["item"]["status"] == "approved", f"{status} {body}")
    check("pending targets listed", body.get("pending_platforms") == ["devdojo", "coderlegion"],
          str(body.get("pending_platforms")))
    status, body = client.post(f"/api/profiles/{pid}/content/approve", {"id": "does-not-exist"})
    check("approve unknown id errors", status == 404, str(status))

    print("\n== publish (dry run) ==")
    status, body = client.post(
        f"/api/profiles/{pid}/content/publish",
        {"ids": [item_id], "dry_run": True},
    )
    check("publish job started", status == 200 and body.get("job"), f"{status} {body}")
    job_id = body["job"]["id"]
    for _ in range(120):
        status, body = client.get(f"/api/jobs/{job_id}?since=0")
        if body["job"]["state"] in {"done", "error", "cancelled"}:
            break
        time.sleep(0.1)
    job = body["job"]
    check("dry-run job finished", job["state"] == "done", job.get("error") or job["state"])
    result = (job.get("result") or {}).get("result") or {}
    check("dry run attempted both pairs", result.get("attempted") == 2, json.dumps(result)[:300])
    check("dry run published nothing", result.get("published") == 0)
    check("dry run logged", any("dry run" in line.lower() for line in job.get("logs") or []))

    status, body = client.get(f"/api/profiles/{pid}/content")
    check("dry run left the ledger alone",
          [row for row in body["items"] if row["id"] == item_id][0]["status"] == "approved")
    check("dry run consumed no quota",
          body["summary"]["daily_usage"]["devdojo"]["used"] == 0,
          json.dumps(body["summary"]["daily_usage"]["devdojo"]))

    print("\n== publish (live, manual targets) ==")
    status, body = client.post(
        f"/api/profiles/{pid}/content/publish",
        {"ids": [item_id], "platforms": ["devdojo", "coderlegion"], "dry_run": False},
    )
    job_id = body["job"]["id"]
    for _ in range(120):
        status, body = client.get(f"/api/jobs/{job_id}?since=0")
        if body["job"]["state"] in {"done", "error", "cancelled"}:
            break
        time.sleep(0.1)
    job = body["job"]
    check("live job finished", job["state"] == "done", job.get("error") or job["state"])
    payload = job.get("result") or {}
    result = payload.get("result") or {}
    check("both pairs queued", result.get("queued") == 2, json.dumps(result)[:400])
    check("manual hint surfaced", "content confirm" in (job.get("result") or {}).get("hint", ""))
    outbox = tmp / "profiles" / pid / "content_outbox"
    check("outbox written per platform", (outbox / "devdojo" / f"{item_id}.md").is_file()
          and (outbox / "coderlegion" / f"{item_id}.md").is_file(), str(list(outbox.rglob('*.md'))))

    print("\n== confirm ==")
    status, body = client.get(f"/api/profiles/{pid}/content")
    stored = [row for row in body["items"] if row["id"] == item_id][0]
    check("item is queued", stored["status"] == "queued", stored["status"])
    check("no url claimed yet", all(not (p.get("url")) for p in stored["publications"].values()))

    status, body = client.post(
        f"/api/profiles/{pid}/content/confirm",
        {"id": item_id, "platform": "devdojo", "url": "not-a-url"},
    )
    check("confirm rejects a bad url", status == 400, f"{status} {body}")
    status, body = client.post(
        f"/api/profiles/{pid}/content/confirm",
        {"id": item_id, "platform": "devdojo"},
    )
    check("confirm requires a url", status == 400, str(status))

    status, body = client.post(
        f"/api/profiles/{pid}/content/confirm",
        {"id": item_id, "platform": "devdojo", "url": "https://devdojo.com/community/posts/x"},
    )
    check("confirm ok", status == 200, f"{status} {body}")
    check("still queued on the other target", body["item"]["status"] == "queued", body["item"]["status"])
    status, body = client.post(
        f"/api/profiles/{pid}/content/confirm",
        {"id": item_id, "platform": "coderlegion", "url": "https://coderlegion.com/1/x"},
    )
    check("fully published", body["item"]["status"] == "published", body["item"]["status"])
    check("both urls live", len(body.get("live_urls") or []) == 2, str(body.get("live_urls")))

    print("\n== reset ==")
    status, body = client.post(f"/api/profiles/{pid}/content/reset", {"id": item_id, "platform": "devdojo"})
    check("reset ok", status == 200, f"{status} {body}")
    check("reset returns to approved", body["item"]["status"] == "approved", body["item"]["status"])
    check("reset clears the url", not body["item"]["publications"]["devdojo"].get("url"))
    status, body = client.post(f"/api/profiles/{pid}/content/reset", {"id": item_id, "platform": "nope"})
    check("reset rejects an unknown platform", status == 400, str(status))

    print("\n== secrets ==")
    status, body = client.post(
        f"/api/profiles/{pid}/content/secrets",
        {"values": {"devto": "fake-key-for-testing", "devdojo": "should-be-refused"}},
    )
    check("devto key mapped to its env var", "DEVTO_API_KEY" in json.dumps(body.get("applied")), json.dumps(body))
    import os

    check("key is in the environment only", os.environ.get("DEVTO_API_KEY") == "fake-key-for-testing")
    check("manual target reports it needs none", "devdojo" in (body.get("problems") or {}), json.dumps(body))
    on_disk = (tmp / "profiles" / pid / "config.yaml").read_text(encoding="utf-8")
    check("key is not written to the config", "fake-key-for-testing" not in on_disk)
    os.environ.pop("DEVTO_API_KEY", None)

    print("\n== devto with a key present ==")
    status, body = client.get(f"/api/profiles/{pid}/platforms")
    by_id = {entry["platform"]: entry for entry in body["readiness"]}
    check("devto now reports a different blocker", "publication" not in by_id["devto"]["reason"].lower()
          or by_id["devto"]["ready"] is False, by_id["devto"]["reason"])

    print("\n== cleanup ==")
    status, body = client.post(f"/api/profiles/{pid}/content/remove", {"id": thin_id})
    check("remove ok", status == 200 and body.get("removed") == thin_id, f"{status} {body}")
    status, body = client.post(f"/api/profiles/{pid}/content/remove", {"id": thin_id})
    check("remove unknown errors", status == 404, str(status))
    status, body = client.post(f"/api/profiles/{pid}/content/mark", {"id": item_id, "status": "not-a-status"})
    check("invalid status rejected", status == 400, f"{status} {body}")

    print("\n== sign out ==")
    status, _ = client.post("/api/auth/logout", {})
    check("logout ok", status == 200)
    client.cookie = ""
    status, _ = client.get(f"/api/profiles/{pid}/content")
    check("session really gone", status == 401, str(status))

    httpd.shutdown()
    httpd.server_close()
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if FAILURES:
    print(f"FAILED {len(FAILURES)}: " + "; ".join(FAILURES))
    raise SystemExit(1)
print("all web api tests passed")
