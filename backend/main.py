import json
import logging
import os
import time
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from dotenv import load_dotenv
from databricks.sdk import WorkspaceClient
from openai import OpenAI

from backend.db import get_engine
from backend.models import MetricsResponse, SummaryResponse

ENV_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "env.local")
if os.path.exists(ENV_PATH):
    load_dotenv(ENV_PATH)

logger = logging.getLogger("variance_api")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Variance Analysis API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ENGINE = get_engine()
TABLE_NAME = os.getenv("DB_TABLE_FULL_NAME", "acndemo.akash_s.sales_variance_synced")


def parse_accounts_param(accounts: str | None) -> list[str]:
    if not accounts:
        return []
    return [account.strip() for account in accounts.split(",") if account.strip()]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/filters/accounts")
def list_accounts():
    query = text(f"SELECT DISTINCT account_name FROM {TABLE_NAME} ORDER BY account_name")
    with ENGINE.connect() as conn:
        rows = conn.execute(query).fetchall()
    return [row[0] for row in rows]


@app.get("/filters/months")
def list_months():
    query = text(
        f"""
        SELECT DISTINCT to_char(date_trunc('month', sale_date), 'YYYY-MM') AS month_key
        FROM {TABLE_NAME}
        ORDER BY month_key DESC
        """
    )
    with ENGINE.connect() as conn:
        rows = conn.execute(query).fetchall()
    return [row[0] for row in rows]


def fetch_account_rollup(month: str, accounts: list[str]):
    account_filter = ""
    params: dict[str, object] = {"month": month}
    if accounts:
        account_filter = "AND account_name = ANY(:accounts)"
        params["accounts"] = accounts

    query = text(f"""
        WITH params AS (
            SELECT to_date(:month || '-01', 'YYYY-MM-DD') AS selected_month
        )
        SELECT
            account_name,
            COALESCE(
                SUM(CASE WHEN date_trunc('month', sale_date) = params.selected_month THEN amount END),
                0
            ) AS current_sum,
            COALESCE(
                SUM(CASE WHEN date_trunc('month', sale_date) = params.selected_month - INTERVAL '1 month' THEN amount END),
                0
            ) AS last_sum
        FROM {TABLE_NAME}, params
        WHERE 1=1
        {account_filter}
        GROUP BY account_name
        ORDER BY account_name
    """)
    with ENGINE.connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return rows


def fetch_monthly_rollup():
    query = text(f"""
        SELECT
            account_name,
            to_char(date_trunc('month', sale_date), 'YYYY-MM') AS month_key,
            SUM(amount) AS total_amount
        FROM {TABLE_NAME}
        GROUP BY account_name, month_key
        ORDER BY account_name, month_key
    """)
    with ENGINE.connect() as conn:
        rows = conn.execute(query).fetchall()
    return rows


def build_overall_payload():
    overall_rows = fetch_monthly_rollup()
    return {
        "rows": [
            {
                "account_name": name,
                "month": month_key,
                "total_amount": float(total_amount),
            }
            for name, month_key, total_amount in overall_rows
        ]
    }


def build_drilldown_payload(month: str, accounts: list[str]):
    rows = fetch_account_rollup(month, accounts)
    if not rows:
        return None
    variances = []
    for name, current_sum, last_sum in rows:
        variance_pct = ((current_sum - last_sum) / last_sum) * 100 if last_sum > 0 else 0.0
        variances.append((name, variance_pct, current_sum, last_sum))
    return {
        "selected_month": month,
        "accounts_filter": accounts,
        "rows": [
            {
                "account_name": name,
                "current_sum": float(current_sum),
                "last_sum": float(last_sum),
                "variance_pct": float(variance_pct),
            }
            for name, variance_pct, current_sum, last_sum in variances
        ],
    }


def parse_endpoint_url(endpoint_url: str) -> tuple[str, str] | None:
    parsed = urlparse(endpoint_url)
    if not parsed.scheme or not parsed.netloc:
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2 or parts[0] != "serving-endpoints":
        return None
    return f"{parsed.scheme}://{parsed.netloc}", parts[1]


def maybe_call_databricks(prompt: str, label: str) -> str | None:
    endpoint = os.getenv("DATABRICKS_ENDPOINT_URL")
    token = os.getenv("DATABRICKS_TOKEN")
    if not endpoint or not token:
        logger.info("Databricks summary skipped (%s): missing endpoint or token", label)
        return None

    try:
        parsed = parse_endpoint_url(endpoint)
        if parsed:
            host, endpoint_name = parsed
            try:
                client = OpenAI(api_key=token, base_url=f"{host}/serving-endpoints")
                response = client.chat.completions.create(
                    model=endpoint_name,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=2000,
                )
                content = response.choices[0].message.content if response.choices else None
                if content:
                    logger.info("Databricks summary used (%s) via OpenAI", label)
                else:
                    logger.warning("Databricks summary empty (%s) via OpenAI", label)
                return content
            except Exception:
                logger.exception("Databricks OpenAI call failed (%s), falling back to SDK", label)
                client = WorkspaceClient(host=host, token=token)
                data = client.api_client.do(
                    "POST",
                    f"/serving-endpoints/{endpoint_name}/invocations",
                    body={"messages": [{"role": "user", "content": prompt}]},
                )
        else:
            resp = requests.post(
                endpoint,
                json={"messages": [{"role": "user", "content": prompt}]},
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
    except requests.RequestException:
        logger.exception("Databricks summary failed (%s)", label)
        return None
    except Exception:
        logger.exception("Databricks SDK failed (%s)", label)
        return None

    content = data.get("choices", [{}])[0].get("message", {}).get("content")
    if content:
        logger.info("Databricks summary used (%s)", label)
    else:
        logger.warning("Databricks summary empty (%s)", label)
    return content


def stream_text(text: str):
    if not text:
        yield ""
        return
    for line in text.splitlines():
        yield line + "\n"
        time.sleep(0.02)


def stream_databricks_tokens(prompt: str, label: str):
    endpoint = os.getenv("DATABRICKS_ENDPOINT_URL")
    token = os.getenv("DATABRICKS_TOKEN")
    if not endpoint or not token:
        logger.info("Databricks stream skipped (%s): missing endpoint or token", label)
        yield from stream_text("Summary unavailable.")
        return

    parsed = parse_endpoint_url(endpoint)
    if not parsed:
        logger.warning("Databricks stream skipped (%s): invalid endpoint URL", label)
        yield from stream_text("Summary unavailable.")
        return

    host, endpoint_name = parsed
    try:
        client = OpenAI(api_key=token, base_url=f"{host}/serving-endpoints")
        stream = client.chat.completions.create(
            model=endpoint_name,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
            stream=True,
        )
        for event in stream:
            delta = event.choices[0].delta if event.choices else None
            if delta and delta.content:
                yield delta.content
        logger.info("Databricks summary streamed (%s) via OpenAI", label)
        return
    except Exception:
        logger.exception("Databricks streaming failed (%s), falling back", label)
        fallback = maybe_call_databricks(prompt, label)
        if not fallback:
            fallback = "Summary unavailable."
        yield from stream_text(fallback)


@app.get("/metrics", response_model=MetricsResponse)
def get_metrics(month: str, accounts: str | None = Query(default=None)):
    account_list = parse_accounts_param(accounts)
    rows = fetch_account_rollup(month, account_list)

    accounts_handled = len(rows)
    current_total = sum(row[1] for row in rows)
    last_total = sum(row[2] for row in rows)
    net_returns_k = round(current_total / 1000, 1)
    net_returns_pct = round(((current_total - last_total) / last_total) * 100, 1) if last_total > 0 else 0.0

    return {
        "accounts_handled": accounts_handled,
        "net_returns_k": net_returns_k,
        "net_returns_pct": net_returns_pct,
    }


@app.get("/table")
def get_table(month: str, accounts: str | None = Query(default=None)):
    account_list = parse_accounts_param(accounts)
    rows = fetch_account_rollup(month, account_list)

    table = []
    for name, current_sum, last_sum in rows:
        variance_pct = round(((current_sum - last_sum) / last_sum) * 100, 1) if last_sum > 0 else 0.0
        table.append(
            {
                "Account": name,
                "Last Month": round(last_sum, 2),
                "Current Month": round(current_sum, 2),
                "Variance %": f"{variance_pct}%",
            }
        )
    return table


@app.get("/summary", response_model=SummaryResponse)
def get_summary(month: str, accounts: str | None = Query(default=None)):
    account_list = parse_accounts_param(accounts)
    rows = fetch_account_rollup(month, account_list)

    if not rows:
        return {"summary_text": "No accounts found for the selected filters.", "drilldown_text": ""}

    variances = []
    for name, current_sum, last_sum in rows:
        variance_pct = ((current_sum - last_sum) / last_sum) * 100 if last_sum > 0 else 0.0
        variances.append((name, variance_pct, current_sum, last_sum))

    largest = max(variances, key=lambda item: item[1])
    avg_variance = sum(value for _, value, _, _ in variances) / len(variances)
    above_avg = [name for name, value, _, _ in variances if value > avg_variance + 10]

    drilldown_payload = build_drilldown_payload(month, account_list)
    overall_payload = build_overall_payload()

    overall_prompt = (
        "You are summarizing data sourced from Postgres. "
        "Return a concise, readable summary with short bullet points. "
        "Use the full dataset below. Focus on notable trends, top movers, and consistency. "
        "Limit the response to 6-8 lines total.\n\n"
        f"{json.dumps(overall_payload)}"
    )
    drilldown_prompt = (
        "You are summarizing data sourced from Postgres. "
        "Return a concise, readable summary with short bullet points. "
        "Use the full dataset below for the selected month and filters, and highlight outliers and changes. "
        "Limit the response to 6-8 lines total.\n\n"
        f"{json.dumps(drilldown_payload)}"
    )

    overall_summary = maybe_call_databricks(overall_prompt, "overall")
    drilldown_summary = maybe_call_databricks(drilldown_prompt, "drilldown")

    summary_text = (
        overall_summary
        if overall_summary
        else (
            f"Out of all accounts, {largest[0]} has the largest variance of {largest[1]:.1f}%. "
            f"Overall variance across accounts averages {avg_variance:.1f}%. "
            f"{'Some accounts exceed the average by over 10%.' if above_avg else 'No accounts exceed the average by over 10%.'}"
        )
    )
    drilldown_text = (
        drilldown_summary
        if drilldown_summary
        else (
            f"{largest[0]} has the largest variance of {largest[1]:.1f}%. "
            + (f"Accounts needing review: {', '.join(above_avg)}." if above_avg else "")
        )
    )

    return {"summary_text": summary_text, "drilldown_text": drilldown_text}


@app.get("/summary/overall/stream")
def stream_overall_summary():
    overall_payload = build_overall_payload()
    overall_prompt = (
        "You are summarizing data sourced from Postgres. "
        "Return a concise, readable summary with short bullet points. "
        "Use the full dataset below. Focus on notable trends, top movers, and consistency. "
        "Limit the response to 6-8 lines total.\n\n"
        f"{json.dumps(overall_payload)}"
    )

    def generator():
        yield "Generating overall summary...\n"
        yield from stream_databricks_tokens(overall_prompt, "overall")

    return StreamingResponse(generator(), media_type="text/plain")


@app.get("/summary/drilldown/stream")
def stream_drilldown_summary(month: str, accounts: str | None = Query(default=None)):
    account_list = parse_accounts_param(accounts)
    drilldown_payload = build_drilldown_payload(month, account_list)
    if not drilldown_payload:
        return StreamingResponse(iter(["No accounts found for the selected filters.\n"]), media_type="text/plain")

    drilldown_prompt = (
        "You are summarizing data sourced from Postgres. "
        "Return a concise, readable summary with short bullet points. "
        "Use the full dataset below for the selected month and filters, and highlight outliers and changes. "
        "Limit the response to 6-8 lines total.\n\n"
        f"{json.dumps(drilldown_payload)}"
    )

    def generator():
        yield "Generating drill-down summary...\n"
        yield from stream_databricks_tokens(drilldown_prompt, "drilldown")

    return StreamingResponse(generator(), media_type="text/plain")
