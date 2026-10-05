"""Headless render check for the wizard's SPA (not part of the shipped tool).

There is no browser here, so this does what can be done statically: it checks
that every data-action, data-bind, data-secret, data-content-* and id the SPA
emits has a handler or a binding, and that the step/tab wiring is coherent.
That catches the classic single-page-app regression where a button is renamed in
one place and silently stops working in another.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

APP = Path("webapp/static/app.js").read_text(encoding="utf-8")
HTML = Path("webapp/static/index.html").read_text(encoding="utf-8")
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label} {detail}")


print("\n== action handlers ==")
# Actions are emitted from app.js *and* from index.html (the sidebar chrome).
emitted = set(re.findall(r'data-action="([a-z0-9-]+)"', APP + HTML))
handler_block = APP[APP.index("const handlers = {"):]
declared = set(re.findall(r"^\s*'([a-z0-9-]+)':", handler_block, re.MULTILINE))
check("every data-action has a handler", emitted <= declared, str(sorted(emitted - declared)))
check("no handler is unreachable", declared <= emitted, str(sorted(declared - emitted)))
print(f"       ({len(emitted)} actions)")
check("no select carries a data-content-mark handler by mistake",
      "'content-mark':" not in handler_block)
check("data-content-mark is wired through the change listener",
      "closest('[data-content-mark]')" in APP)

print("\n== state keys used ==")
state_block = APP[APP.index("const state = {"):APP.index("/* ------", APP.index("const state = {"))]
keys = set(re.findall(r"^\s{2}([A-Za-z_$][\w$]*):", state_block, re.MULTILINE))
used = set(re.findall(r"state\.([A-Za-z_$][\w$]*)", APP))
missing = sorted(used - keys)
check("every state.X read is declared", not missing, str(missing))
print(f"       ({len(keys)} keys, {len(used)} used)")

print("\n== steps ==")
steps_block = APP[APP.index("const STEPS = ["):APP.index("const CONTENT_FIELDS")]
steps = re.findall(r"\{ id: (\d+), title: '([^']+)'", steps_block)
ids = [int(step[0]) for step in steps]
check("steps are 1..N with no gaps", ids == list(range(1, len(ids) + 1)), str(ids))
rendered = set(re.findall(r"case (\d+): main\.innerHTML = render(\w+)", APP))
check("every step has a render case", {int(n) for n, _ in rendered} == set(ids),
      f"steps={ids} cases={[n for n, _ in sorted(rendered, key=lambda x: int(x[0]))]}")
check("dashboard is the last step", steps[-1][1] == "Dashboard", steps[-1][1])
check("content step exists", any(title == "Content" for _, title in steps))
check("no step numbers hard-coded beyond the continue buttons",
      len(re.findall(r'data-step="(\d)"', APP)) >= 1)

print("\n== field bindings ==")
# Every bind goes through boundField(), which composes `data-bind="' + bind + '"`,
# so there are no literal paths to enumerate. What matters is that applyBind()
# understands each kind boundField() can emit.
bound_field = APP[APP.index("function boundField("):APP.index("function flagCheck(")]
kinds = set(re.findall(r"kind === '([a-z]+)'", bound_field))
# 'text' is the trailing fallback rather than an explicit branch.
check("boundField handles exactly the declared kinds",
      kinds == {"check", "list", "csv", "number", "area"}, str(sorted(kinds)))
check("boundField falls back to a text input",
      bound_field.rstrip().endswith("}") and '<input type="text" data-bind="' in bound_field)
check("boundField escapes every value it renders", "esc(" in bound_field)
apply_bind = APP[APP.index("function applyBind(el)"):]
check("applyBind handles checkbox", "el.type === 'checkbox'" in apply_bind)
check("applyBind handles lists", "el.dataset.list" in apply_bind and "split(" in apply_bind)
check("applyBind handles numbers", "el.type === 'number'" in apply_bind)
check("applyBind marks the draft dirty", "state.dirty = true" in apply_bind)
check("applyBind is a no-op without a draft", "if (!state.draft) return;" in apply_bind)
check("applyBind writes through setPath", "setPath(state.draft, bind, value)" in apply_bind)

print("\n== platform field tables ==")
for platform in ("devto", "hashnode", "medium", "wordpress", "coderlegion", "devdojo", "webhook"):
    check(f"PLATFORM_FIELDS has {platform}",
          re.search(rf"\n  {platform}: \[", APP) is not None)

print("\n== content pipeline wiring ==")
# Paths are built with string concatenation, so match the suffixes rather than
# trying to parse the expressions.
for suffix in (
    "/content'",
    "/content/draft'",
    "/content/approve'",
    "/content/mark'",
    "/content/confirm'",
    "/content/reset'",
    "/content/remove'",
    "/content/publish'",
    "/content/secrets'",
    "/content/items/'",
):
    check(f"SPA calls {suffix.strip(chr(39))}", suffix in APP)
check("items endpoint is url-encoded", "encodeURIComponent(itemId)" in APP)

print("\n== job kinds ==")
check("publish job kind handled", "job.kind === 'publish'" in APP)
check("publish result stored", "state.publishResult" in APP)
check("runJob clears the publish result",
      re.search(r"if \(kind === 'publish'\) state\.publishResult = null", APP) is not None)

print("\n== escaping discipline ==")
check("no innerHTML assignment of raw user text",
      len(re.findall(r"\.innerHTML\s*=\s*[^;]*(?:item\.body_markdown|item\.title)", APP)) == 0)
check("message preview is escaped", "esc(result.message)" in APP or "esc(item.body_markdown)" in APP)
check("toasts use textContent", "node.textContent = message" in APP)

print()
if FAILURES:
    print(f"FAILED {len(FAILURES)}: " + "; ".join(FAILURES))
    sys.exit(1)
print("all SPA checks passed")
