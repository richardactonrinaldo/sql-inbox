# Analyst Inbox: SQL

Students in HIFN1009 chat with **Dana Morgan**, a pretend Manager of Decision Support at a fictional Ontario hospital network, and answer her requests by writing SQL. They write and run queries in a SQL workspace beside the chat, then send the query and its results to Dana. She re-runs their query, runs her own, compares them, gives hints instead of answers, and asks follow-up questions.

Module focus options:

- **Module A:** SELECT, WHERE and ORDER BY (one table)
- **Module B:** Aggregates and GROUP BY (COUNT, AVG, SUM, MIN, MAX, HAVING)
- **Module C:** JOINs (INNER and LEFT)
- **Module D:** CASE and presenting results

Dana doesn't use or suggest subqueries, CTEs or window functions.

Built in to discourage offloading the thinking: a prediction before each request, results accepted only from the workspace (with the query), occasional "explain your query" checks, an end-of-session reflection, and Dana's end-of-day check-in (gentle, performance-review style formative feedback). Dana asks at most two numbered questions at a time and follows up on any that are missed.

## The database

`data/lakeshore_ed.db` (SQLite, read-only in the app) holds the same 300 emergency department visits as the Excel version, split into four tables:

| Table | Rows | Notes |
|---|---|---|
| `sites` | 5 | Bracebridge is "Opening 2026" and has no visits (for LEFT JOIN) |
| `triage_levels` | 5 | CTAS levels with target wait times |
| `patients` | 251 | 56 patients have repeat visits; 12 have no visits (for LEFT JOIN) |
| `visits` | 300 | 7 `cost_cad` values are NULL (billing not finalized) |

`python make_database.py` rebuilds it from `data/ED_Visits_2025.csv` (fixed seed, same result every time).

Students' queries run read-only: anything other than SELECT is refused, and a query is stopped after 3 seconds (e.g. a JOIN with no ON).

## Deploy on Streamlit Community Cloud (free)

1. At share.streamlit.io, click **Create app**, choose this repo, branch `main`, main file `app.py`.
2. In **Advanced settings > Secrets** (or the app's Settings > Secrets later), paste:
   ```toml
   ANTHROPIC_API_KEY = "sk-ant-..."
   CLASS_CODE = "your-class-code"
   MODEL = "claude-sonnet-5-5"
   ```
   You can reuse the API key from the Excel version of Analyst Inbox.
3. Post the app URL and the class code on Blackboard.

## Cost controls

- The class code keeps strangers from using your credits.
- Each session is capped at 60 student messages (`MAX_STUDENT_MESSAGES` in `app.py`).
- Prepaid credits with auto-reload off can't be exceeded.

## Run locally

```bash
pip install -r requirements.txt
cp .streamlit/secrets.example.toml .streamlit/secrets.toml   # then edit it
streamlit run app.py
```
