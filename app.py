"""Analyst Inbox: SQL. Students chat with Dana Morgan, a pretend Decision Support manager,
and answer her requests by writing SQL against a small SQLite database, right in the app.

Runs on Streamlit. Needs these secrets (Streamlit Cloud > App > Settings > Secrets):
    ANTHROPIC_API_KEY = "sk-ant-..."
    CLASS_CODE = "something-you-give-students"   # keeps strangers from using your credits
    MODEL = "claude-sonnet-5-5"                   # optional; "claude-haiku-5-5" is cheaper
"""
import difflib
import json
import re
import secrets
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import anthropic
import pandas as pd
import streamlit as st

DB_PATH = Path(__file__).parent / "data" / "lakeshore_ed.db"
MAX_STUDENT_MESSAGES = 60     # per session; caps cost if someone leaves a script running
MAX_RESULT_ROWS = 1000        # rows a student's query can return in the workspace
ROWS_SENT_TO_DANA = 50        # rows of a submitted result that Dana reads
QUERY_TIME_LIMIT = 3          # seconds before a runaway query (e.g. a JOIN with no ON) is stopped

st.set_page_config(page_title="Analyst Inbox: SQL", page_icon="🗄️", layout="wide")

# ---------------------------------------------------------------- the database
TABLES = {
    "sites": ("One row per ED site.", [
        ("site_id", "INTEGER", "primary key"),
        ("site_name", "TEXT", "Barrie, Orillia, Midland, Collingwood, Bracebridge"),
        ("city", "TEXT", ""),
        ("ed_beds", "INTEGER", "number of ED beds"),
        ("status", "TEXT", "'Open', or 'Opening 2026' for Bracebridge (no visits yet)"),
    ]),
    "triage_levels": ("The five CTAS triage levels.", [
        ("triage_level", "INTEGER", "primary key; 1 = most urgent, 5 = least urgent"),
        ("description", "TEXT", "Resuscitation, Emergent, Urgent, Less urgent, Non-urgent"),
        ("target_wait_min", "INTEGER", "CTAS target time to see a physician: 0, 15, 30, 60, 120"),
    ]),
    "patients": ("One row per patient. Some have several visits; 12 have none in 2025.", [
        ("patient_id", "TEXT", "primary key, e.g. P0001"),
        ("sex", "TEXT", "'F' or 'M'"),
        ("birth_year", "INTEGER", ""),
        ("home_town", "TEXT", "e.g. Barrie, Innisfil, Wasaga Beach"),
    ]),
    "visits": ("One row per ED visit in 2025 (300 visits).", [
        ("visit_id", "TEXT", "primary key, e.g. ED0001"),
        ("patient_id", "TEXT", "links to patients"),
        ("site_id", "INTEGER", "links to sites"),
        ("visit_date", "TEXT", "'YYYY-MM-DD', 2025-01-01 to 2025-12-31"),
        ("triage_level", "INTEGER", "links to triage_levels"),
        ("chief_complaint", "TEXT", "Abdominal pain, Chest pain, Shortness of breath, Injury/Fracture, "
                                    "Fever, Headache, Laceration, Mental health"),
        ("wait_time_min", "INTEGER", "minutes from triage to physician"),
        ("length_of_stay_hrs", "REAL", ""),
        ("disposition", "TEXT", "Discharged, Admitted, LWBS (left without being seen), Transferred"),
        ("cost_cad", "INTEGER", "NULL for 7 visits whose billing isn't finalized"),
    ]),
}
SCHEMA = "SQLite database lakeshore_ed.db (fictional data):\n" + "\n".join(
    f"- {t} ({desc}): " + "; ".join(f"{c} {typ}" + (f" ({note})" if note else "") for c, typ, note in cols)
    for t, (desc, cols) in TABLES.items())

FOREIGN_KEYS = [("visits", "patient_id", "patients"), ("visits", "site_id", "sites"),
                ("visits", "triage_level", "triage_levels")]
LOOKUP_TABLES = ["sites", "triage_levels"]  # shown in full in the sidebar
PRIMARY_KEYS = {"sites": "site_id", "triage_levels": "triage_level", "patients": "patient_id", "visits": "visit_id"}


def key_tag(table: str, col: str, fks: set) -> str:
    tag = "PK" if PRIMARY_KEYS[table] == col else "FK" if (table, col) in fks else ""
    return f'<font color="#0f6e78"><b>{tag}</b></font>' if tag else " "


def diagram_dot() -> str:
    """Entity-relationship diagram of the database, in Graphviz DOT."""
    fks = {(t, c) for t, c, _ in FOREIGN_KEYS}
    nodes = []
    for t, (_, cols) in TABLES.items():
        rows = "".join(
            f'<tr><td port="{c}" align="left">{"<b>" + c + "</b>" if PRIMARY_KEYS[t] == c else c}</td>'
            f'<td align="left"><font color="#666666">{typ.lower()}</font></td>'
            f'<td port="k_{c}" align="left">{key_tag(t, c, fks)}</td></tr>'
            for c, typ, _ in cols)
        nodes.append(f'{t} [label=<<table border="0" cellborder="1" cellspacing="0" cellpadding="4">'
                     f'<tr><td colspan="3" bgcolor="#0f6e78"><font color="white"><b>{t}</b></font></td></tr>{rows}</table>>];')
    edges = [f'{t}:k_{c}:e -> {ref}:{PRIMARY_KEYS[ref]}:w [dir=both, arrowtail=crow, arrowhead=tee];'
             for t, c, ref in FOREIGN_KEYS]
    return ("digraph { rankdir=LR; bgcolor=transparent; node [shape=plain, fontname=Helvetica, fontsize=11]; "
            "edge [color=\"#555555\"]; " + " ".join(nodes + edges) + " }")


_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}


def _authorizer(action, *_):
    # Read-only: queries may only read tables and call functions. No writes, ATTACH or PRAGMA.
    return sqlite3.SQLITE_OK if action in _ALLOWED else sqlite3.SQLITE_DENY


def run_query(sql: str, max_rows: int = MAX_RESULT_ROWS) -> dict:
    """Run one read-only SELECT. Returns columns, rows (up to max_rows) and whether more existed."""
    sql = (sql or "").strip()
    if not sql:
        raise ValueError("The query is empty.")
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    started = time.time()
    try:
        con.set_authorizer(_authorizer)
        con.set_progress_handler(lambda: int(time.time() - started > QUERY_TIME_LIMIT), 10_000)
        cur = con.execute(sql)
        if cur.description is None:
            raise ValueError("Only SELECT queries can be run here.")
        cols = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchmany(max_rows + 1)]
    except sqlite3.DatabaseError as e:
        msg = str(e)
        if "interrupted" in msg:
            msg = (f"The query ran longer than {QUERY_TIME_LIMIT} seconds and was stopped. "
                   "Check that every JOIN has an ON condition.")
        elif "not authorized" in msg:
            msg = "Only SELECT queries can be run here. The database is read-only."
        elif "one statement at a time" in msg:
            msg = "Run one query at a time. Remove everything after the first semicolon."
        raise ValueError(msg) from None
    finally:
        con.close()
    return {"columns": cols, "rows": rows[:max_rows], "truncated": len(rows) > max_rows}


def run_sql_tool(query: str) -> dict:
    """Dana's tool: run a query and get a compact result."""
    res = run_query(query, max_rows=5000)
    return {"columns": res["columns"], "row_count": len(res["rows"]) if not res["truncated"] else "5000+",
            "rows": res["rows"][:ROWS_SENT_TO_DANA],
            "note": f"showing first {ROWS_SENT_TO_DANA} rows" if len(res["rows"]) > ROWS_SENT_TO_DANA else ""}


def result_text(sql: str, res: dict | None, error: str | None, max_rows: int) -> str:
    """Plain-text version of a submitted query and its result, for Dana and the transcript."""
    lines = ["Query:", sql.strip()]
    if error:
        lines.append(f"Result: error: {error}")
        return "\n".join(lines)
    n = len(res["rows"])
    count = f"{n}+" if res["truncated"] else str(n)
    lines.append(f"Result: {count} row{'s' if n != 1 else ''}" + (f" (first {max_rows} shown)" if n > max_rows else ""))
    lines.append(" | ".join(res["columns"]))
    for r in res["rows"][:max_rows]:
        lines.append(" | ".join("NULL" if v is None else str(v) for v in r))
    return "\n".join(lines)


TOOLS = [{
    "name": "run_sql",
    "description": "Run a read-only SQLite SELECT on lakeshore_ed.db and see the result. Use it to check every "
                   "number and every query the analyst submits: re-run their query and your own correct one, "
                   "and compare. Returns columns, row_count and up to 50 rows.",
    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
}]
TOOL_FUNCS = {"run_sql": run_sql_tool}

# ---------------------------------------------------------------- persona
TOPICS = {
    "Module A · SELECT, WHERE and ORDER BY": (
        "Retrieving and filtering rows from ONE table: SELECT (specific columns, *), WHERE with =, <>, <, >, "
        "AND, OR, IN, BETWEEN, LIKE, IS NULL / IS NOT NULL, ORDER BY (ASC/DESC), LIMIT, DISTINCT, and column "
        "aliases with AS. No aggregates and no joins yet. If a request needs a site's ID, tell the analyst they can "
        "look it up in the sites list in the sidebar, or by running SELECT * FROM sites first. Requests should return a short list (e.g. 'the 10 longest waits "
        "at Barrie')."),
    "Module B · Aggregates and GROUP BY": (
        "Summarising ONE table: COUNT(*), COUNT(column), COUNT(DISTINCT column), SUM, AVG, MIN, MAX, ROUND, "
        "GROUP BY one or two columns, HAVING, and ORDER BY an aggregate. Still no joins (use site_id and look "
        "up names in sites). Ask for a small summary table and one observation from it. A good trap: "
        "COUNT(*) vs COUNT(cost_cad), because 7 costs are NULL."),
    "Module C · JOINs": (
        "Combining tables: INNER JOIN visits to sites, triage_levels or patients with ON, table aliases "
        "(v, s, t, p), joining three tables, and LEFT JOIN to find rows with no match (Bracebridge has no "
        "visits; 12 patients have none) using WHERE ... IS NULL. Joins can be combined with Module A and B "
        "skills. Good requests: wait time vs the CTAS target by site, visits per home town, returning patients."),
    "Module D · CASE and presenting results": (
        "CASE WHEN ... THEN ... ELSE ... END to group values (e.g. age groups from 2025 - birth_year, "
        "'within target' vs 'over target' by comparing wait_time_min with target_wait_min, weekday vs weekend), "
        "often combined with GROUP BY and JOINs. Then presenting: the analyst writes a 3-5 sentence summary or "
        "3 talking points for the VP of Patient Care from their results. Coach on leading with the answer, plain "
        "language, one key number per point, the 'so what', and a recommendation. After their summary, play "
        "the VP for one tough question, then step back out as Dana to give feedback."),
}


def system_prompt(topic: str, student: str) -> str:
    return f"""You are Dana Morgan, Manager of Decision Support at Lakeshore Regional Health, a fictional hospital network in Central Ontario. You are chatting with {student}, a junior healthcare data analyst on your team. In reality they are a college student in a health informatics course, a beginner with SQL. Stay in character the whole time.

{SCHEMA}

The analyst can also download the database to explore it in their own tool, but they write and run their answers in a SQL workspace beside this chat and sends you the query with its results. Submissions arrive marked "[Submitted from the SQL workspace]" with the query and the result rows.

This module's focus: {TOPICS[topic]}

How you work:
- Write like a busy, supportive manager in a work chat: short, plain, friendly. Under 150 words per reply, except a new request may run to 200. No headings. Short bullet lists are fine. Put any SQL in a code block.
- Each request gives: why you need it (business context), the specific question(s), who it's for, and when (e.g. "by end of day"). Ask for at most 3 things per request. Your first message greets them briefly and sends the first request; point them to the table list in the sidebar.
- End every new request with one prediction question to answer BEFORE they write any SQL, e.g. "Before you run anything: how many rows do you expect back, roughly? Why?" or "Which site do you expect at the top?" When they answer, don't say whether they're right; just acknowledge it ("Noted, let's see what the data says"). When their results come in, compare them with the prediction and ask what surprised them, if anything.
- Stay within this module's SQL skills and a beginner's level. "A little harder" means a new question, an extra condition or a judgement call using the same skills, never new techniques. Never use or suggest subqueries, CTEs (WITH), window functions, views or anything from a later module. The course doesn't teach subqueries.
- If you write your message and then call a tool, don't repeat that message after the tool result. Add only what's new, or nothing.
- One thing at a time. Don't stack a new request on top of an unfinished one. When you ask a follow-up question, wait for the answer, respond to it, and only then send the next request.
- Now and then leave one detail a little vague so the analyst has to ask a clarifying question. Answer clarifying questions the way a real manager would.
- Results count only when they are sent from the SQL workspace, with the query. If they type numbers or results into the chat, ask them to run the query and send it from the workspace before you confirm anything; you can still answer questions and clarify in the meantime. A typed query without results doesn't count either.
- Before you comment on ANY result, check it with the run_sql tool: re-run their query, and run your own correct query for the request. Compare the two. Never guess. A query can return the right-looking numbers for the wrong reason (e.g. a missing condition that happens not to matter), so read the query itself too.
- If a result is right, say so specifically. If it is off, say what doesn't match and give a hint (which clause, condition, join or function to recheck). Don't write the corrected query or reveal the correct value until they've missed it twice; then show it and explain briefly. If they send an error, help them read the error message rather than fixing it for them.
- The same goes for interpretation. If their takeaway is wrong, point them back at their own result with a guiding question before you explain the right reading. Explain it only after a second miss.
- Occasionally (about every second or third request, and always when a query looks copied rather than written, e.g. a correct query they can't seem to explain), once their result is right, run a quick check that they understand their own query: ask them to explain one clause in their own words ("walk me through your WHERE"), or to predict a small change ("if you changed that JOIN to a LEFT JOIN, what would change in the result?"). Check any result they give with run_sql. Pick a different clause or change each time.
- Then ask one realistic follow-up a manager would ask: what explains it, why it matters, what we should do, or who else should see it.
- Ask at most two questions per message. When you ask two, number them (1. and 2.) so neither gets lost. Never more than two.
- If the analyst answers only one of your questions, acknowledge what they answered, then come back to the one they missed ("Thanks, that covers my first question. What about the second: ...?"). Don't move on until both are answered.
- When they say they're stopping for the day (or say goodbye), don't sign off yet. Ask one reflection question: what tripped them up today, and how did they work through it? After they answer, respond in a sentence or two and sign off; your end-of-day check-in notes follow automatically. If they said goodbye in the chat instead of using the "Finish for today" button, ask them to click it so you can send them their check-in.
- Don't do the analysis for them. Show an example query only if they're stuck after a hint, and keep it to the clause they need.
- If they ask you to just give them the answers, or go off topic, redirect them kindly, in character.
- A request is finished when the prediction, results, any understanding check, and follow-up are done. Then thank them briefly and send the next request in the same message.

SQLite facts to get right:
- There is no MEDIAN function in SQLite. Don't ask for medians.
- Aliases: a column alias in SELECT (wait_time_min AS wait_minutes) is good practice, because it gives the output a readable label. Using that alias in WHERE works in SQLite but not in standard SQL or most other databases, because WHERE is processed before SELECT names the columns. If they do it, praise the alias itself and frame the fix clearly: "keep the alias for your output, but refer to the original column (wait_time_min) in WHERE." Never make it sound as if aliasing is bad practice. (Aliases in ORDER BY are fine everywhere.)
- Text comparison with = is case-sensitive ('Barrie' is not 'barrie'); LIKE is case-insensitive for plain letters. % matches any run of characters, _ exactly one.
- Integer division truncates: COUNT(...) / COUNT(...) gives 0 or 1. For a percentage use 100.0 * COUNT(...) / COUNT(...). AVG always returns a decimal. ROUND(x, 1) rounds to one decimal.
- COUNT(*) counts rows; COUNT(column) skips NULLs. AVG, SUM, MIN and MAX ignore NULLs. Test for NULL with IS NULL, never = NULL.
- Dates are text 'YYYY-MM-DD': compare them as text (visit_date BETWEEN '2025-01-01' AND '2025-03-31'), or use strftime('%m', visit_date) for the month and strftime('%w', visit_date) for the weekday (0 = Sunday).
- A column in SELECT that is neither aggregated nor in GROUP BY is allowed by SQLite but gives an arbitrary value: point it out if they do it.
- A JOIN with no ON condition multiplies the tables (every row with every row); the workspace stops such queries after {QUERY_TIME_LIMIT} seconds.
- In a LEFT JOIN, a condition on the right-hand table in WHERE turns it back into an inner join; COUNT(right_table.column) gives 0 for unmatched rows, COUNT(*) gives 1.
- Never mention that you are an AI, a simulation or a tool."""


# ---------------------------------------------------------------- Claude calls
def as_input_block(b) -> dict:
    """Copy a block of Claude's reply into the history with only the fields the API accepts back.
    (Newer SDK versions add extra response-only fields, which the API rejects if echoed.)"""
    if b.type == "text":
        return {"type": "text", "text": b.text}
    return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}


def api_error_detail(e: Exception) -> str:
    """Short, readable reason from an API error, for the instructor."""
    msg = getattr(e, "message", "") or str(e)
    body = getattr(e, "body", None)
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        msg = body["error"].get("message", msg)
    return f"{e.__class__.__name__}: {msg}"[:300]


@st.cache_resource
def client():
    return anthropic.Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])


def trim_unfinished(text: str) -> str:
    """If a reply ran out of room, cut it back to its last complete sentence."""
    cut = max(text.rfind(". "), text.rfind(".\n"), text.rfind("! "), text.rfind("? "))
    return text[:cut + 1] if cut > len(text) // 2 else text


def ask_dana() -> str:
    """Run the tool loop until Dana produces a text reply."""
    model = st.secrets.get("MODEL", "claude-sonnet-5-5")
    system = [{"type": "text", "text": system_prompt(st.session_state.topic, st.session_state.student),
               "cache_control": {"type": "ephemeral"}}]
    msgs = st.session_state.api_messages
    # Dana may write part of her reply and call a tool in the same step, then add a line after the
    # tool result. Keep the text from every step, but drop a step that just repeats an earlier one.
    parts = []
    for _ in range(10):  # safety limit on tool rounds
        resp = client().messages.create(model=model, max_tokens=4000, system=system, tools=TOOLS, messages=msgs)
        u = resp.usage
        st.session_state.tokens["in"] += u.input_tokens + (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0)
        st.session_state.tokens["out"] += u.output_tokens
        msgs.append({"role": "assistant", "content": [as_input_block(b) for b in resp.content if b.type in ("text", "tool_use")]})
        step_text = "".join(b.text for b in resp.content if b.type == "text").strip()
        repeated = any(step_text in p or difflib.SequenceMatcher(None, step_text, p).ratio() > 0.8 for p in parts)
        if step_text and not repeated:
            parts.append(step_text)
        if resp.stop_reason != "tool_use":
            if resp.stop_reason == "max_tokens" and parts:
                parts[-1] = trim_unfinished(parts[-1])
            return "\n\n".join(parts)
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                try:
                    out, err = json.dumps(TOOL_FUNCS[b.name](**b.input), default=str), False
                except Exception as e:  # tell Dana what went wrong so she can retry
                    out, err = f"Error: {e}", True
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": out, "is_error": err})
        msgs.append({"role": "user", "content": results})
    fallback = "Sorry, got pulled into a meeting. Can you resend that?"
    msgs.append({"role": "assistant", "content": fallback})
    return "\n\n".join(parts + [fallback])


CHECKIN_PROMPT = """You are Dana Morgan, Manager of Decision Support at Lakeshore Regional Health. You've just finished a working session with {student}, a junior data analyst on your team (in reality, a beginner SQL student in a health informatics course). This session's focus: {topic}.

Write {student} a short end-of-day check-in, in the style of a gentle, constructive workplace performance review. It is formative feedback only: no grades, scores or ratings.

Use exactly this format (Markdown):

**End-of-day check-in: {student}, {module}**

**What went well**
2-3 sentences naming specific strengths, with concrete examples from their actual work today (a query, a clause, a check they ran, a good question, a solid takeaway).

**Where to grow**
- *Skill name.* At most two bullets. Frame each as a skill to build, not a mistake. Say briefly what you noticed and one concrete way to practise it.

**Focus for next time**
One sentence: the single most useful habit to work on.

End with one warm, sincere sentence.

Rules:
- Base everything on the conversation below. Never invent strengths or problems. If they did little work, say so kindly and keep it short.
- Be warm and encouraging but honest: real feedback, delivered kindly.
- Mention their reflection answer if it showed insight.
- Under 220 words. Plain, friendly language. Never mention that you are an AI or that this is a simulation.

The conversation:
{transcript}"""


def message_text(m: dict, rows: int = 10) -> str:
    """One chat message as plain text (submissions include the query and a few result rows)."""
    text = m["text"]
    if m.get("sql"):
        sub = result_text(m["sql"], m.get("result"), m.get("error"), rows)
        text = f"{text}\n{sub}" if text else sub
    return text


def write_checkin() -> str:
    """Dana's end-of-day performance-review style feedback, from the whole conversation."""
    convo = "\n\n".join(f"{'Dana' if m['who'] == 'Dana' else st.session_state.student}: {message_text(m)}"
                        for m in st.session_state.chat if not m.get("checkin"))
    module = st.session_state.topic.split(" · ")[0]
    prompt = CHECKIN_PROMPT.format(student=st.session_state.student, topic=TOPICS[st.session_state.topic],
                                   module=module, transcript=convo)
    resp = client().messages.create(model=st.secrets.get("MODEL", "claude-sonnet-5-5"), max_tokens=3000,
                                    messages=[{"role": "user", "content": prompt}])
    st.session_state.tokens["in"] += resp.usage.input_tokens
    st.session_state.tokens["out"] += resp.usage.output_tokens
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    return trim_unfinished(text) if resp.stop_reason == "max_tokens" else text


# ---------------------------------------------------------------- keep conversations across reconnects
# Streamlit forgets a browser's session when it disconnects. Conversations are kept on the server under
# the last digits of the student number, so a returning student only types those. A random link token
# in the page address also restores the conversation on a plain reload. Saved conversations are lost
# only when the app itself restarts (after a long idle sleep or a new deploy).
SAVED_KEYS = ["student", "student_number", "topic", "api_messages", "chat", "tokens", "wrap_stage"]
KEEP_DAYS = 14


@st.cache_resource
def conversation_store() -> dict:
    return {"by_number": {}, "by_link": {}}


def norm_code(raw: str) -> str:
    """Class codes ignore capitals, spaces and dashes, so HIFN1009W27 = hifn1009-w27."""
    return re.sub(r"[\s\-_]+", "", raw or "").lower()


def clean_number(raw: str) -> str:
    return re.sub(r"\s+", "", raw or "")


def save_conversation():
    number = st.session_state.get("student_number")
    if not number:
        return
    store = conversation_store()
    store["by_number"][number] = {"saved": time.time(),
                                  **{k: st.session_state[k] for k in SAVED_KEYS if k in st.session_state}}
    cutoff = time.time() - KEEP_DAYS * 86400
    for old in [n for n, v in store["by_number"].items() if v["saved"] < cutoff]:
        store["by_number"].pop(old, None)
    for tok in [t for t, n in store["by_link"].items() if n not in store["by_number"]]:
        store["by_link"].pop(tok, None)


def link_session(number: str):
    """Put a random token in the page address so a reload restores this conversation."""
    token = secrets.token_urlsafe(8)
    conversation_store()["by_link"][token] = number
    st.query_params["s"] = token


def restore_conversation(number: str) -> bool:
    saved = conversation_store()["by_number"].get(number)
    if not saved:
        return False
    for k in SAVED_KEYS:
        st.session_state[k] = saved.get(k, 0 if k == "wrap_stage" else None)
    return True


def reset(topic=None):
    st.session_state.wrap_stage = 0
    st.session_state.api_messages = []
    st.session_state.chat = []  # dicts: who, text, and for submissions: sql, result, error
    st.session_state.tokens = {"in": 0, "out": 0}
    st.session_state.pop("last_run", None)
    if topic:
        st.session_state.topic = topic
    save_conversation()


if "student" not in st.session_state and st.query_params.get("s"):
    linked = conversation_store()["by_link"].get(st.query_params["s"])
    if not (linked and restore_conversation(linked)):
        st.query_params.clear()

# ---------------------------------------------------------------- sign-in gate
if "student" not in st.session_state:
    st.title("Analyst Inbox: SQL")
    st.write("Dana Morgan, Manager of Decision Support at Lakeshore Regional Health, will send you "
             "analysis requests. Write and run SQL queries in the app and send her your results.")
    with st.form("signin"):
        number = st.text_input("Last 4 digits of your student number", max_chars=5)
        st.caption("Coming back to a conversation? These four digits are all you need.")
        name = st.text_input("First name (first visit only)")
        code = st.text_input("Class code (first visit only)", type="password")
        if st.form_submit_button("Open my inbox", type="primary"):
            number = clean_number(number)
            saved = conversation_store()["by_number"].get(number)
            other_name = saved and name.strip() and saved["student"].lower() != name.strip().lower()
            if not re.fullmatch(r"\d{4,5}", number):
                st.error("Enter the last 4 digits of your student number.")
            elif saved and not other_name:
                restore_conversation(number)
                link_session(number)
                st.rerun()
            elif not st.secrets.get("CLASS_CODE") or norm_code(code) != norm_code(str(st.secrets["CLASS_CODE"])):
                st.error("No saved conversation was found for those digits, so this is a new start. "
                         "Enter your first name and the class code your instructor posted on Blackboard.")
            elif not name.strip():
                st.error("Enter your first name so Dana knows who she's talking to.")
            elif other_name:
                st.error("Another student in this class has the same last four digits. Use the last "
                         "5 digits of your student number instead, here and every time you come back.")
            else:
                st.session_state.student = " ".join(w[:1].upper() + w[1:] for w in name.split())
                st.session_state.student_number = number
                reset(next(iter(TOPICS)))
                link_session(number)
                st.rerun()
    st.stop()

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.subheader("This module's focus")
    topic = st.selectbox("Focus", list(TOPICS), index=list(TOPICS).index(st.session_state.topic),
                         label_visibility="collapsed")
    if topic != st.session_state.topic:
        reset(topic)
        st.rerun()

    st.subheader("Database")
    st.download_button("Download the database (.db)", DB_PATH.read_bytes(), "lakeshore_ed.db",
                       "application/vnd.sqlite3", width="stretch")
    st.caption("Explore it in DB Browser for SQLite if you like. Send your results to Dana from the SQL "
               "workspace here.")
    st.subheader("Tables")
    for t, (desc, cols) in TABLES.items():
        with st.expander(t):
            st.caption(desc)
            st.markdown("\n".join(f"- `{c}` {typ.lower()}" + (f": {note}" if note else "") for c, typ, note in cols))
            if t in LOOKUP_TABLES:  # small lookup tables: show every row, so IDs are easy to find
                full = run_query(f"SELECT * FROM {t}")
                st.dataframe(pd.DataFrame(full["rows"], columns=full["columns"]), hide_index=True, width="stretch")

    st.subheader("Conversation")
    transcript = (f"Student: {st.session_state.student} (student number ending {st.session_state.student_number})\n\n"
                  + "\n\n".join(f"{'Dana Morgan' if m['who'] == 'Dana' else st.session_state.student}: "
                                f"{message_text(m, rows=20)}" for m in st.session_state.chat))
    st.download_button("Download transcript", transcript,
                       f"transcript_SQL_{st.session_state.student}_{st.session_state.student_number}_{datetime.now():%Y%m%d}.txt",
                       width="stretch")
    if st.button("Finish for today", type="primary", width="stretch", disabled=not st.session_state.chat):
        st.session_state.wrap_up = True
    if st.button("Start over", width="stretch"):
        reset()
        st.rerun()
    st.caption("Click Finish for today and answer Dana's last question, then download your transcript "
               "and submit it on Blackboard.")
    st.caption(f"Signed in as {st.session_state.student} (…{st.session_state.student_number}). If the page "
               "reloads or you come back later, enter these digits to continue.")

# ---------------------------------------------------------------- layout: chat | SQL workspace
st.markdown("#### Dana Morgan")
st.caption("Manager, Decision Support · Lakeshore Regional Health")
chat_col, sql_col = st.columns([5, 4], gap="large")


def show_result(res: dict | None, error: str | None, height: int | str = "auto"):
    if error:
        st.error(error)
        return
    df = pd.DataFrame(res["rows"], columns=res["columns"])
    st.dataframe(df, hide_index=True, width="stretch", height=height)
    n = len(res["rows"])
    st.caption(f"{n}{'+' if res['truncated'] else ''} row{'s' if n != 1 else ''}"
               + (f" (showing the first {MAX_RESULT_ROWS})" if res["truncated"] else ""))


# ---- SQL workspace
def queue_submission():
    run = st.session_state.get("last_run")
    if run:
        st.session_state.pending_submission = {**run, "note": st.session_state.get("sql_note", "").strip()}
        st.session_state.sql_note = ""


with sql_col:
    st.markdown("##### SQL workspace")
    with st.expander("Database diagram", expanded=len(st.session_state.chat) <= 1):
        st.graphviz_chart(diagram_dot(), width="stretch")
        st.caption("PK = primary key, FK = foreign key. Each visit links to one patient, one site and one "
                   "triage level; the forked end of a line is the 'many' side. Column notes are in the sidebar.")
    sql = st.text_area("Your query", key="sql_box", height=180, label_visibility="collapsed",
                       placeholder="Type your SQL here. For example, to see the sites table:  SELECT * FROM sites;")
    if st.button("Run query", type="primary"):
        try:
            st.session_state.last_run = {"sql": sql, "result": run_query(sql), "error": None}
        except ValueError as e:
            st.session_state.last_run = {"sql": sql, "result": None, "error": str(e)}
    run = st.session_state.get("last_run")
    if run:
        show_result(run["result"], run["error"], height=280)
        stale = run["sql"].strip() != (sql or "").strip()
        if stale:
            st.caption("You've changed the query since it last ran. Run it again before sending it.")
        st.text_input("Note for Dana (optional)", key="sql_note",
                      placeholder="e.g. Here's my query for your request")
        st.button("Send query and results to Dana", on_click=queue_submission, disabled=stale,
                  help="Dana sees your exact query and its results.")
    else:
        st.caption("Write a query and click Run query. When the result looks right, send it to Dana. "
                   "The table list is in the sidebar.")


# ---- chat
def post_dana(text: str):
    st.session_state.chat.append({"who": "Dana", "text": text})
    save_conversation()


with chat_col:
    box = st.container(height=560)
    with box:
        for m in st.session_state.chat:
            with st.chat_message("assistant" if m["who"] == "Dana" else "user",
                                 avatar="🩺" if m["who"] == "Dana" else None):
                if m.get("checkin"):
                    with st.container(border=True):
                        st.markdown(m["text"])
                    continue
                if m["text"]:
                    st.markdown(m["text"])
                if m.get("sql"):
                    st.code(m["sql"].strip(), language="sql")
                    show_result(m.get("result"), m.get("error"))

    if not st.session_state.chat:
        with box:
            st.info(f"Welcome to the team, {st.session_state.student}. Dana's first request is on its way.")
        st.session_state.api_messages = [{"role": "user", "content": "Hi Dana, I'm ready for my first request."}]
        save_conversation()
        try:
            with box, st.spinner("Dana is typing…"):
                post_dana(ask_dana())
            st.rerun()
        except anthropic.APIError as e:
            st.session_state.api_messages = []
            save_conversation()
            st.error("Dana can't be reached right now. Wait a minute and reload the page. "
                     f"(Details for your instructor: {api_error_detail(e)})")
            st.stop()

    if st.session_state.pop("checkin_failed", False):
        st.warning("Dana couldn't write your end-of-day check-in just now. Click Finish for today to try again.")

    sent = sum(1 for m in st.session_state.chat if m["who"] != "Dana")
    if sent >= MAX_STUDENT_MESSAGES:
        st.warning("You've reached the message limit for this session. Download your transcript, then press Start over.")
        st.stop()

    prompt = st.chat_input("Reply to Dana. Send query results from the SQL workspace.")

submission = st.session_state.pop("pending_submission", None)
wrap_up = st.session_state.pop("wrap_up", False)
if prompt or submission or wrap_up:
    if submission:
        entry = {"who": "You", "text": submission["note"], "sql": submission["sql"],
                 "result": submission["result"], "error": submission["error"]}
        api_text = ("[Submitted from the SQL workspace]\n"
                    + (f"Note: {submission['note']}\n" if submission["note"] else "")
                    + result_text(submission["sql"], submission["result"], submission["error"], ROWS_SENT_TO_DANA))
    else:
        text = prompt.strip() if prompt else "I need to stop for today."
        entry, api_text = {"who": "You", "text": text}, text
    st.session_state.chat.append(entry)
    answering_reflection = st.session_state.get("wrap_stage", 0) == 1 and not wrap_up
    before = len(st.session_state.api_messages)
    st.session_state.api_messages.append({"role": "user", "content": api_text})
    try:
        with chat_col, st.spinner("Dana is checking your query…" if submission else "Dana is typing…"):
            post_dana(ask_dana())
    except anthropic.APIError as e:
        st.session_state.chat.pop()
        del st.session_state.api_messages[before:]  # drop the message and any half-finished tool rounds
        with chat_col:
            st.error("Your message didn't go through. Wait a moment and send it again. "
                     f"(Details for your instructor: {api_error_detail(e)})")
        st.stop()
    if wrap_up:
        st.session_state.wrap_stage = 1          # Dana has asked the reflection question
    elif answering_reflection:
        st.session_state.wrap_stage = 0
        try:
            with chat_col, st.spinner("Dana is writing your end-of-day check-in…"):
                note = write_checkin()
            st.session_state.chat.append({"who": "Dana", "text": note, "checkin": True})
        except anthropic.APIError:
            st.session_state.checkin_failed = True
    save_conversation()
    st.rerun()
