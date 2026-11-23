"""Student-facing HTML transcript.

The page is readable in any browser, and it carries the plain transcript text inside it, so the same file can be
uploaded to continue a conversation (and opened in the instructor's transcript viewer).
"""
import html
import json
import re

EMBED_ID = "analyst-inbox-transcript"


def embedded_text(content: str) -> str:
    """The plain transcript inside an HTML transcript, or the text itself if it's already plain."""
    m = re.search(r'<script[^>]*id="' + EMBED_ID + r'"[^>]*>(.*?)</script>', content, re.S)
    if not m:
        return content
    try:
        return json.loads(m.group(1))
    except ValueError:
        return content


def inline(text: str) -> str:
    """Escape, then apply **bold**, *italic* and `code`."""
    parts = re.split(r"(`[^`]+`)", text)
    out = []
    for p in parts:
        if p.startswith("`") and p.endswith("`") and len(p) > 1:
            out.append(f"<code>{html.escape(p[1:-1])}</code>")
        else:
            s = html.escape(p)
            s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
            s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", s)
            out.append(s)
    return "".join(out)


def table_html(rows: list[str]) -> str:
    cells = [[c.strip() for c in r.split(" | ")] for r in rows]
    head, body = cells[0], cells[1:]
    h = "".join(f"<th>{html.escape(c)}</th>" for c in head)
    b = "".join("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in r) + "</tr>" for r in body)
    return f'<div class="tablewrap"><table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table></div>'


def render_body(text: str) -> str:
    """Small Markdown subset: paragraphs, bullet and numbered lists, code fences, and SQL submissions."""
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            lang = line.strip()[3:].strip()
            j = i + 1
            code = []
            while j < len(lines) and not lines[j].strip().startswith("```"):
                code.append(lines[j])
                j += 1
            out.append(f'<pre class="code {html.escape(lang)}">{html.escape(chr(10).join(code))}</pre>')
            i = j + 1
            continue
        if line.strip() == "Query:":
            j = i + 1
            code = []
            while j < len(lines) and not lines[j].startswith("Result:"):
                code.append(lines[j])
                j += 1
            out.append('<div class="label">Query</div><pre class="code sql">' + html.escape("\n".join(code).strip()) + "</pre>")
            i = j
            continue
        if line.startswith("Result:"):
            cls = "result error" if "error" in line[:16] else "result"
            out.append(f'<div class="{cls}">{html.escape(line)}</div>')
            j = i + 1
            rows = []
            while j < len(lines) and " | " in lines[j]:
                rows.append(lines[j])
                j += 1
            if not rows and j < len(lines) and lines[j].strip() and "|" not in lines[j] and "error" not in line:
                rows = []  # single-column result without separators
                while j < len(lines) and lines[j].strip():
                    rows.append(lines[j])
                    j += 1
            if rows:
                out.append(table_html(rows))
            i = j
            continue
        if re.match(r"^\s*[-*•] ", line):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*•] ", lines[i]):
                items.append(re.sub(r"^\s*[-*•] ", "", lines[i]))
                i += 1
            out.append("<ul>" + "".join(f"<li>{inline(x)}</li>" for x in items) + "</ul>")
            continue
        if re.match(r"^\s*\d+[.)] ", line):
            items = []
            while i < len(lines) and re.match(r"^\s*\d+[.)] ", lines[i]):
                items.append(re.sub(r"^\s*\d+[.)] ", "", lines[i]))
                i += 1
            out.append("<ol>" + "".join(f"<li>{inline(x)}</li>" for x in items) + "</ol>")
            continue
        if not line.strip():
            i += 1
            continue
        para = [line]
        i += 1
        while (i < len(lines) and lines[i].strip() and not re.match(r"^\s*([-*•]|\d+[.)]) ", lines[i])
               and not lines[i].strip().startswith("```") and lines[i].strip() != "Query:"
               and not lines[i].startswith("Result:")):
            para.append(lines[i])
            i += 1
        out.append("<p>" + "<br>".join(inline(p) for p in para) + "</p>")
    return "\n".join(out)



CSS = """
:root { color-scheme: light dark; --ink: light-dark(#1d292e, #e3ecee); --muted: light-dark(#5b6a71, #9db0b5);
        --bg: light-dark(#f7f9fa, #0f1719); --card: light-dark(#fff, #16211f); --line: light-dark(#dbe3e6, #2a3a3d);
        --accent: light-dark(#0b5563, #6fc3cf); --dana: light-dark(#eef3f5, #1b2a2d); --you: light-dark(#e4f1e8, #1d3026); }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 16px/1.55 "Segoe UI", -apple-system, Arial, sans-serif; }
.wrap { max-width: 900px; margin: 0 auto; padding: 28px 16px 60px; }
h1 { font-size: 26px; margin: 0 0 6px; color: var(--accent); }
.sub { color: var(--muted); margin-bottom: 18px; white-space: pre-line; }
.note { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 10px 14px; font-size: 14px;
        color: var(--muted); margin-bottom: 18px; }
.msg { display: flex; margin: 10px 0; } .msg.student { justify-content: flex-end; }
.bubble { max-width: 85%; background: var(--dana); border: 1px solid var(--line); border-radius: 14px; padding: 10px 14px; }
.student .bubble { background: var(--you); }
.checkin .bubble { max-width: 100%; background: var(--card); border: 2px solid var(--accent); }
.who { font-size: 14px; font-weight: 600; color: var(--muted); margin-bottom: 4px; }
.bubble p { margin: 6px 0; } .bubble ul, .bubble ol { margin: 6px 0; padding-left: 22px; }
code { background: light-dark(#e6edf0, #243437); padding: 1px 4px; border-radius: 4px; font: 14px Consolas, Menlo, monospace; }
pre.code { background: #1d292e; color: #e6eef1; padding: 10px 12px; border-radius: 8px; overflow-x: auto;
           font: 13px/1.45 Consolas, Menlo, monospace; white-space: pre-wrap; }
.label { font-size: 13px; font-weight: 600; color: var(--muted); }
.result { font-size: 14px; color: light-dark(#2e7d4f, #7fd19f); margin: 6px 0 4px; } .result.error { color: #c0581f; }
.tablewrap { overflow-x: auto; } table { border-collapse: collapse; font-size: 14px; background: var(--card); }
th, td { border: 1px solid var(--line); padding: 3px 8px; text-align: left; } th { background: var(--dana); }
.chip { display: inline-block; font-size: 13px; border: 1px solid var(--line); border-radius: 12px; padding: 1px 9px;
        margin: 4px 4px 0 0; color: var(--muted); background: var(--card); }
@media print { body { background: #fff; } .msg { break-inside: avoid; } }
"""


def to_html(text: str, title: str) -> str:
    """Render a plain transcript as a readable chat page with the original text embedded."""
    lines = text.replace("\r\n", "\n").split("\n")
    header = []
    i = 0
    while i < len(lines) and lines[i].strip():
        header.append(lines[i])
        i += 1
    m = re.match(r"Student:\s*(.+?)\s*\(", header[0]) if header else None
    student = m.group(1) if m else "Student"
    start = re.compile(r"^(?:\[(?P<ts>[^\]]+)\] )?(?P<who>Dana Morgan|" + re.escape(student) + r"): ?(?P<rest>.*)$")
    msgs, cur = [], None
    for line in lines[i:]:
        mm = start.match(line)
        if mm:
            if cur:
                msgs.append(cur)
            cur = {"who": mm.group("who"), "ts": mm.group("ts") or "", "text": mm.group("rest")}
        elif cur is not None:
            cur["text"] += "\n" + line
    if cur:
        msgs.append(cur)
    parts = []
    for msg in msgs:
        att = re.findall(r"^\[Attachment: (.+?)\]\s*$", msg["text"], re.M)
        body = re.sub(r"^\[Attachment: .+?\]\s*$", "", msg["text"], flags=re.M).strip()
        dana = msg["who"] == "Dana Morgan"
        cls = "msg " + ("dana" if dana else "student") + (" checkin" if body.lstrip("*").lower().startswith("end-of-day check-in") else "")
        chips = "".join(f'<span class="chip">Attachment: {html.escape(a)}</span>' for a in att)
        parts.append(f'<div class="{cls}"><div class="bubble"><div class="who">{html.escape(msg["who"])} '
                     f'<span style="font-weight:400">{html.escape(msg["ts"])}</span></div>'
                     f'{render_body(body) if body else ""}{("<div>" + chips + "</div>") if chips else ""}</div></div>')
    raw = json.dumps(text).replace("</", "<\\/")   # valid JSON; can't close the script tag early
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>{html.escape(title)}</h1>
<div class="sub">{html.escape(chr(10).join(header))}</div>
<div class="note">This is your Analyst Inbox transcript. Keep this file: to continue your conversation with Dana next time,
upload it when you sign in (or from the sidebar: Continue from a transcript).</div>
{"".join(parts)}
</div>
<script type="application/json" id="{EMBED_ID}">{raw}</script>
</body></html>"""
