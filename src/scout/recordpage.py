"""One record of Scout's store as a page on the web UI (GET /r/<id>, docs/links.md):
its title, when and who, its fields, its text (Scout's own records; a few lines of the
others), an Open button to where it lives (the Granola note, the message in Mail, the
Slack thread), and the records linked to it. Plain HTML, no scripts; everything from
the store is escaped."""

from __future__ import annotations

import datetime as dt
import html
import re
import urllib.parse
from typing import Any

from . import index, links

CSP = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
KIND_NAMES = {
    "task": "Task", "note": "Note", "turn": "Conversation", "memory": "Fact", "meeting": "Meeting",
    "mail": "Email", "slack": "Slack", "list": "List", "github": "GitHub", "person": "Person",
    "behavior": "Behavior rule",
}  # fmt: skip
REL_NAMES = {
    ("found_in", "out"): "Found in", ("found_in", "in"): "A task found here",
    ("merged_from", "out"): "Also from", ("merged_from", "in"): "A task that repeats this",
    ("same_as", "out"): "Same meeting", ("same_as", "in"): "Same meeting",
    ("based_on", "out"): "Used", ("based_on", "in"): "Used by",
    ("about", "out"): "About", ("about", "in"): "Mentioned in",
    ("told_in", "out"): "Told in", ("told_in", "in"): "A fact told here",
    ("mentions", "out"): "Mentions", ("mentions", "in"): "Mentioned by",
    ("discusses", "out"): "Discusses", ("discusses", "in"): "Discussed in",
    ("same_person", "out"): "Theirs", ("same_person", "in"): "Same person",
    ("with", "out"): "With", ("with", "in"): "With them",
    ("from", "out"): "From", ("from", "in"): "From them",
    ("to", "out"): "To", ("to", "in"): "To them",
}  # fmt: skip
# Fields shown for a task, in this order (the reminder's ids and titles aren't).
TASK_FIELDS = ("status", "owner", "due", "importance", "urgency", "effort", "pin", "source", "github", "found",
               "done")  # fmt: skip
# And for a GitHub item.
GITHUB_FIELDS = ("repo", "number", "type", "state", "author", "assignees", "updated_at")
OWN_TEXT = 6000  # characters of a Scout record's own text shown
OTHER_TEXT = 600  # of a meeting, email or Slack message (open it for the rest)
_SAFE_URL = re.compile(r"^(?:https?|message)://\S+$")


def safe_url(url: Any) -> str | None:
    """A link the page may carry: http(s) or Mail's message://, nothing else."""
    s = str(url or "")
    return s if _SAFE_URL.match(s) else None


def page_href(id: str) -> str:
    return "/r/" + urllib.parse.quote(id, safe="")


def _when(ts: float) -> str:
    t = dt.datetime.fromtimestamp(ts)
    return f"{t:%a %b} {t.day}, {t:%Y %H:%M}"


def render(rec: dict[str, Any], hit: index.Hit | None) -> str:
    """The page for a resolved record (index.Index.resolve) and its stored item."""
    e = html.escape
    kind = str(rec.get("kind") or "")
    title = str(rec.get("title") or "") or "(untitled)"
    parts = [f'<p class="kind">{e(KIND_NAMES.get(kind, kind.title()))}</p>', f"<h1>{e(title)}</h1>"]
    if hit is not None:
        meta = [_when(hit.ts)] + ([hit.who] if hit.who else [])
        parts.append(f'<p class="meta">{e(" · ".join(meta))}</p>')
    if (url := safe_url(rec.get("url"))) and url != rec.get("page"):
        label = "Open in Mail" if url.startswith("message:") else "Open"
        parts.append(f'<p><a class="open" href="{e(url)}">{label}</a></p>')
    if hit is not None and kind in ("task", "github"):
        shown = TASK_FIELDS if kind == "task" else GITHUB_FIELDS
        rows = [
            (k, ", ".join(v) if isinstance(v := hit.meta[k], list) else v) for k in shown if hit.meta.get(k)
        ]
        if rows:
            parts.append(
                "<table>"
                + "".join(f"<tr><th>{e(k)}</th><td>{e(str(v))}</td></tr>" for k, v in rows)
                + "</table>"
            )
    if hit is not None and kind == "person":
        parts.append(_identities(hit.meta))
    if hit is not None and hit.body and kind != "person":
        own = kind in links.OWN_KINDS
        body = hit.body if own else hit.snippet(OTHER_TEXT)
        body = body[:OWN_TEXT]
        parts.append(f'<div class="body">{e(body)}</div>')
    related = rec.get("related") or []
    if related:
        items = []
        for r in related:
            name = REL_NAMES.get((str(r.get("rel")), str(r.get("dir") or "out")), str(r.get("rel")))
            what = KIND_NAMES.get(str(r.get("kind")), str(r.get("kind") or ""))
            line = (
                f'<li><span class="rel">{e(name)}</span> {e(what)}: '
                f'<a href="{e(page_href(str(r.get("id"))))}">{e(str(r.get("title") or "(untitled)"))}</a>'
            )
            if (out := safe_url(r.get("url"))) and not out.endswith(page_href(str(r.get("id")))):
                line += f' · <a href="{e(out)}">open</a>'
            items.append(line + "</li>")
        parts.append("<h2>Linked</h2><ul>" + "".join(items) + "</ul>")
    return _PAGE.format(title=e(title), body="\n".join(parts))


def _identities(meta: dict[str, Any]) -> str:
    """A person's other identities (people.py): the names they go by, their email
    addresses Scout has seen, their Slack user."""
    e = html.escape
    rows = [("contact", str(meta.get("contact") or ""))]
    rows += [("name", str(n)) for n in meta.get("names") or []]
    rows += [("email", str(x)) for x in meta.get("emails") or []]
    rows += [("phone", str(x)) for x in meta.get("phones") or []]
    rows += [
        (
            "slack",
            " · ".join(
                v for v in (str(s.get("shown") or ""), str(s.get("real") or ""), str(s.get("id") or "")) if v
            ),
        )
        for s in meta.get("slack") or []
    ]
    rows += [("github", str(g)) for g in meta.get("github") or []]
    return (
        "<h2>Also known as</h2><table>"
        + "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in rows if v)
        + "</table>"
    )


def not_found(id: str) -> str:
    e = html.escape
    return _PAGE.format(title="Not found", body=f"<h1>Not found</h1><p>Scout has no record {e(id)}.</p>")


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{title} · Scout</title>
<style>
:root {{ color-scheme: light dark; --fg: #1d1d1f; --muted: #6e6e73; --bg: #fff; --line: #d2d2d7; --accent: #0a66d8; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --fg: #f5f5f7; --muted: #a1a1a6; --bg: #1c1c1e; --line: #3a3a3c; --accent: #4c9aff; }}
}}
body {{ font: 16px/1.5 -apple-system, system-ui, sans-serif; color: var(--fg); background: var(--bg);
       margin: 0 auto; max-width: 42rem; padding: 1rem 16px 3rem; }}
h1 {{ font-size: 1.4rem; margin: .2rem 0; }} h2 {{ font-size: 1rem; margin-top: 2rem; }}
.kind, .meta, .rel {{ color: var(--muted); font-size: .9rem; margin: 0; }}
a {{ color: var(--accent); }}
a.open {{ display: inline-block; padding: .5rem 1.2rem; border-radius: .5rem; background: var(--accent);
         color: #fff; text-decoration: none; font-weight: 600; }}
table {{ border-collapse: collapse; margin: 1rem 0; }}
th {{ text-align: left; color: var(--muted); font-weight: normal; padding: .15rem 1rem .15rem 0; }}
.body {{ white-space: pre-wrap; overflow-wrap: anywhere; border-top: 1px solid var(--line); padding-top: 1rem; }}
ul {{ padding-left: 1.1rem; }} li {{ margin: .3rem 0; overflow-wrap: anywhere; }}
</style></head>
<body>
{body}
</body></html>
"""
