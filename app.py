import html
import json
import logging
import os
import queue
import re
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from urllib.parse import parse_qs, unquote, urlparse

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv
from databricks.sdk import WorkspaceClient
from requests import RequestException

ENV_PATHS = [
    os.path.join(os.path.dirname(__file__), "config", "env.app"),
    os.path.join(os.path.dirname(__file__), "config", "env.local"),
]
for env_path in ENV_PATHS:
    if os.path.exists(env_path):
        load_dotenv(env_path, override=True)

API_BASE = os.getenv("API_BASE_URL")
USE_BACKEND = bool(API_BASE)
DATA_CACHE_TTL = 120
DEBUG_LOGS = os.getenv("DEBUG_LOGS", "false").lower() == "true"
USE_SP_AUTH = os.getenv("USE_SP_AUTH", "true").lower() == "true"

logging.basicConfig(level=logging.INFO if DEBUG_LOGS else logging.WARNING)
logger = logging.getLogger("variance_app")

workspace_client = WorkspaceClient()
postgres_password = None
last_password_refresh = 0
connection_pool = None


def get_service_principal_client() -> WorkspaceClient | None:
    host = os.getenv("DATABRICKS_HOST")
    if not host:
        return None

    azure_client_id = os.getenv("DATABRICKS_AZURE_CLIENT_ID")
    azure_client_secret = os.getenv("DATABRICKS_AZURE_CLIENT_SECRET")
    azure_tenant_id = os.getenv("DATABRICKS_AZURE_TENANT_ID")
    if azure_client_id and azure_client_secret and azure_tenant_id:
        try:
            return WorkspaceClient(
                host=host,
                azure_client_id=azure_client_id,
                azure_client_secret=azure_client_secret,
                azure_tenant_id=azure_tenant_id,
            )
        except Exception:
            logger.exception("Failed to initialize Azure SP client")
            return None

    client_id = os.getenv("DATABRICKS_CLIENT_ID")
    client_secret = os.getenv("DATABRICKS_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None
    try:
        return WorkspaceClient(host=host, client_id=client_id, client_secret=client_secret)
    except Exception:
        logger.exception("Failed to initialize service principal client")
        return None


def parse_jdbc_url() -> dict:
    jdbc_url = os.getenv("JDBC_URL")
    if jdbc_url:
        if jdbc_url.startswith("jdbc:postgresql://"):
            parsed = urlparse(jdbc_url.replace("jdbc:", "", 1))
        elif jdbc_url.startswith("postgresql://"):
            parsed = urlparse(jdbc_url)
        else:
            raise RuntimeError("JDBC_URL must start with jdbc:postgresql:// or postgresql://")

        params = parse_qs(parsed.query)
        return {
            "host": parsed.hostname,
            "port": parsed.port or 5432,
            "dbname": parsed.path.lstrip("/"),
            "user": unquote(parsed.username or ""),
            "password": unquote(parsed.password or ""),
            "sslmode": params.get("sslmode", ["require"])[0],
        }

    host = os.getenv("DB_HOST")
    user = os.getenv("DB_USER")
    password = os.getenv("DB_PASSWORD", "")
    name = os.getenv("DB_NAME")
    port = int(os.getenv("DB_PORT", "5432"))
    sslmode = os.getenv("DB_SSLMODE", "require")
    if not host or not user or not name:
        raise RuntimeError("Set JDBC_URL or DB_HOST/DB_USER/DB_NAME for Postgres access")
    return {
        "host": host,
        "port": port,
        "dbname": name,
        "user": user,
        "password": password,
        "sslmode": sslmode,
    }


def get_pg_env_config() -> dict | None:
    host = os.getenv("PGHOST")
    user = os.getenv("PGUSER")
    dbname = os.getenv("PGDATABASE")
    port = int(os.getenv("PGPORT", "5432"))
    sslmode = os.getenv("PGSSLMODE", "require")
    appname = os.getenv("PGAPPNAME")
    instance_name = os.getenv("PGINSTANCE_NAME")

    if not host or not user or not dbname:
        return None

    return {
        "host": host,
        "user": user,
        "dbname": dbname,
        "port": port,
        "sslmode": sslmode,
        "application_name": appname,
        "instance_name": instance_name,
    }


def refresh_oauth_token() -> bool:
    global postgres_password, last_password_refresh
    if postgres_password is None or time.time() - last_password_refresh > 900:
        try:
            if USE_SP_AUTH:
                postgres_password = get_db_oauth_token()
                if DEBUG_LOGS:
                    logger.info("Using service principal database credential")
            else:
                postgres_password = workspace_client.config.oauth_token().access_token
                if DEBUG_LOGS:
                    logger.info("Using user OAuth token for Postgres")
            last_password_refresh = time.time()
            if DEBUG_LOGS:
                logger.info("Refreshed Postgres OAuth token")
        except Exception:
            logger.exception("Failed to refresh Postgres OAuth token")
            return False
    return True


@st.cache_resource(ttl=55 * 60)
def get_db_oauth_token() -> str:
    sp_client = get_service_principal_client()
    if not sp_client:
        raise RuntimeError("Service principal client not configured")
    pg_env = get_pg_env_config()
    if not pg_env or not pg_env.get("instance_name"):
        raise RuntimeError("PGINSTANCE_NAME not configured for SP auth")
    instance_name = pg_env["instance_name"]
    expected_sp_id = os.getenv("DATABRICKS_CLIENT_ID") or os.getenv("DATABRICKS_AZURE_CLIENT_ID")
    try:
        current_identity = sp_client.current_user.me().user_name
        if expected_sp_id and current_identity != expected_sp_id:
            raise RuntimeError(
                f"SDK identity mismatch: expected {expected_sp_id}, got {current_identity}"
            )
    except Exception:
        logger.exception("Failed to verify SP identity")
        raise

    try:
        sp_client.database.get_database_instance(name=instance_name)
    except Exception:
        logger.exception("Database instance not found or inaccessible: %s", instance_name)
        raise

    credential = generate_db_credential(sp_client, instance_name)
    return credential["token"]


def generate_db_credential(client: WorkspaceClient, instance_name: str) -> dict:
    request_id = str(uuid.uuid4())
    database_api = getattr(client, "database", None)
    if database_api and hasattr(database_api, "generate_database_credential"):
        cred = database_api.generate_database_credential(
            request_id=request_id,
            instance_names=[instance_name],
        )
        return {"token": cred.token}
    res = client.api_client.do(
        "POST",
        "/api/2.0/database/credentials",
        body={"request_id": request_id, "instance_names": [instance_name]},
    )
    return res


@st.cache_resource
def get_connection():
    try:
        import psycopg2
        from psycopg2 import pool
    except ImportError as exc:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "psycopg2-binary"])
        import psycopg2  # type: ignore[no-redef]
        from psycopg2 import pool  # type: ignore[no-redef]

    global connection_pool

    pg_env = get_pg_env_config()
    if pg_env:
        if not refresh_oauth_token():
            raise RuntimeError("Failed to refresh OAuth token for Postgres")
        conn_string = (
            f"dbname={pg_env['dbname']} user={pg_env['user']} password={postgres_password} "
            f"host={pg_env['host']} port={pg_env['port']} sslmode={pg_env['sslmode']} "
            f"application_name={pg_env.get('application_name') or ''}"
        )
    else:
        cfg = parse_jdbc_url()
        conn_string = (
            f"dbname={cfg['dbname']} user={cfg['user']} password={cfg['password']} "
            f"host={cfg['host']} port={cfg['port']} sslmode={cfg['sslmode']}"
        )

    if connection_pool is None:
        if DEBUG_LOGS:
            logger.info("Creating Postgres connection pool")
        connection_pool = pool.SimpleConnectionPool(2, 10, conn_string)

    return connection_pool.getconn()


def run_query(query: str, params: tuple | list | None = None):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params or [])
            if cur.description:
                return cur.fetchall()
            return []
    except Exception:
        logger.exception("Query failed")
        raise
    finally:
        try:
            if connection_pool is not None:
                connection_pool.putconn(conn)
        except Exception:
            logger.exception("Failed to return connection to pool")


def get_table_name() -> str:
    return os.getenv("DB_TABLE_FULL_NAME", "acndemo.akash_s.sales_variance_synced")


def parse_endpoint_url(endpoint_url: str) -> tuple[str, str] | None:
    parsed = urlparse(endpoint_url)
    if not parsed.scheme or not parsed.netloc:
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2 or parts[0] != "serving-endpoints":
        return None
    return f"{parsed.scheme}://{parsed.netloc}", parts[1]


def get_serving_endpoint():
    endpoint_url = os.getenv("DATABRICKS_ENDPOINT_URL")
    endpoint_name = os.getenv("SERVING_ENDPOINT_NAME")
    if endpoint_url:
        parsed = parse_endpoint_url(endpoint_url)
        if parsed:
            return parsed
    if endpoint_name:
        host = os.getenv("DATABRICKS_HOST")
        if host:
            return host.rstrip("/"), endpoint_name
    return None

def fetch_json(
    path: str,
    params: dict | None = None,
    fallback: dict | list | None = None,
    timeout: int = 10,
):
    if not USE_BACKEND:
        return fallback
    try:
        if DEBUG_LOGS:
            logger.info("GET %s params=%s", path, params)
        resp = requests.get(f"{API_BASE}{path}", params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except RequestException:
        logger.exception("API request failed: %s", path)
        return fallback


def get_accounts():
    if USE_BACKEND:
        return fetch_json("/filters/accounts", fallback=[])
    rows = run_query(
        f"SELECT DISTINCT account_name FROM {get_table_name()} ORDER BY account_name"
    )
    return [row[0] for row in rows]


def get_months():
    if USE_BACKEND:
        return fetch_json("/filters/months", fallback=[])
    query = (
        f"""
        SELECT DISTINCT to_char(date_trunc('month', sale_date), 'YYYY-MM') AS month_key
        FROM {get_table_name()}
        ORDER BY month_key DESC
        """
    )
    rows = run_query(query)
    return [row[0] for row in rows]


def fetch_account_rollup(month: str, accounts: list[str]):
    account_filter = ""
    params: list[object] = [month]
    if accounts:
        account_filter = "AND account_name = ANY(%s)"
        params.append(accounts)

    query = (
        f"""
        WITH params AS (
            SELECT to_date(%s || '-01', 'YYYY-MM-DD') AS selected_month
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
        FROM {get_table_name()}, params
        WHERE 1=1
        {account_filter}
        GROUP BY account_name
        ORDER BY account_name
        """
    )
    return run_query(query, params)


def fetch_monthly_rollup():
    query = (
        f"""
        SELECT
            account_name,
            to_char(date_trunc('month', sale_date), 'YYYY-MM') AS month_key,
            SUM(amount) AS total_amount
        FROM {get_table_name()}
        GROUP BY account_name, month_key
        ORDER BY account_name, month_key
        """
    )
    return run_query(query)


def get_metrics(month: str, accounts_key: str):
    cache_key = f"metrics::{month}::{accounts_key}"
    cached = st.session_state.get(cache_key)
    if cached:
        return cached
    if USE_BACKEND:
        result = fetch_json(
            "/metrics",
            params={"month": month, "accounts": accounts_key},
            fallback={
                "accounts_handled": 0,
                "net_returns_k": 0,
                "net_returns_pct": 0,
            },
        )
    else:
        account_list = [a for a in accounts_key.split(",") if a] if accounts_key else []
        rows = fetch_account_rollup(month, account_list)
        accounts_handled = len(rows)
        current_total = sum(row[1] for row in rows)
        last_total = sum(row[2] for row in rows)
        net_returns_k = round(current_total / 1000, 1)
        net_returns_pct = round(((current_total - last_total) / last_total) * 100, 1) if last_total > 0 else 0.0
        result = {
            "accounts_handled": accounts_handled,
            "net_returns_k": net_returns_k,
            "net_returns_pct": net_returns_pct,
        }
    if result.get("accounts_handled", 0) > 0:
        st.session_state[cache_key] = result
    return result


def get_table(month: str, accounts_key: str):
    cache_key = f"table::{month}::{accounts_key}"
    cached = st.session_state.get(cache_key)
    if cached:
        return cached
    if USE_BACKEND:
        result = fetch_json(
            "/table",
            params={"month": month, "accounts": accounts_key},
            fallback=[],
        )
    else:
        account_list = [a for a in accounts_key.split(",") if a] if accounts_key else []
        rows = fetch_account_rollup(month, account_list)
        result = []
        for name, current_sum, last_sum in rows:
            variance_pct = round(((current_sum - last_sum) / last_sum) * 100, 1) if last_sum > 0 else 0.0
            result.append(
                {
                    "Account": name,
                    "Last Month": round(last_sum, 2),
                    "Current Month": round(current_sum, 2),
                    "Variance %": f"{variance_pct}%",
                }
            )
    if result:
        st.session_state[cache_key] = result
    return result


def stream_summary_to_queue(path: str, params: dict, output: queue.Queue):
    if USE_BACKEND:
        attempts = 0
        while attempts < 2:
            try:
                if DEBUG_LOGS:
                    logger.info("Streaming from %s params=%s", path, params)
                with requests.get(f"{API_BASE}{path}", params=params, stream=True, timeout=90) as resp:
                    resp.raise_for_status()
                    for chunk in resp.iter_content(chunk_size=1, decode_unicode=True):
                        if not chunk:
                            continue
                        output.put(chunk)
                output.put(None)
                return
            except RequestException:
                attempts += 1
                time.sleep(1)
        output.put("Summary unavailable.")
        output.put(None)
        return

    prompt = build_overall_prompt() if path.endswith("/overall/stream") else build_drilldown_prompt(params)
    for token in stream_llm_tokens(prompt):
        output.put(token)
    output.put(None)


def build_overall_prompt() -> str:
    payload = build_overall_payload()
    return (
        "You are summarizing data sourced from Postgres. "
        "Return a concise, readable summary with short bullet points. "
        "Use the full dataset below. Focus on notable trends, top movers, and consistency. "
        "Limit the response to 6-8 lines total.\n\n"
        f"{json.dumps(payload)}"
    )


def build_drilldown_prompt(params: dict) -> str:
    month = params.get("month")
    accounts = params.get("accounts", "")
    account_list = [a for a in accounts.split(",") if a]
    payload = build_drilldown_payload(month, account_list)
    return (
        "You are summarizing data sourced from Postgres. "
        "Return a concise, readable summary with short bullet points. "
        "Use the full dataset below for the selected month and filters, and highlight outliers and changes. "
        "Limit the response to 6-8 lines total.\n\n"
        f"{json.dumps(payload)}"
    )


def build_overall_payload() -> dict:
    rows = fetch_monthly_rollup()
    return {
        "rows": [
            {"account_name": name, "month": month_key, "total_amount": float(total_amount)}
            for name, month_key, total_amount in rows
        ]
    }


def build_drilldown_payload(month: str, accounts: list[str]) -> dict:
    rows = fetch_account_rollup(month, accounts)
    if not rows:
        return {"rows": []}
    return {
        "selected_month": month,
        "accounts_filter": accounts,
        "rows": [
            {
                "account_name": name,
                "current_sum": float(current_sum),
                "last_sum": float(last_sum),
                "variance_pct": float(((current_sum - last_sum) / last_sum) * 100) if last_sum > 0 else 0.0,
            }
            for name, current_sum, last_sum in rows
        ],
    }


def stream_llm_tokens(prompt: str):
    endpoint = get_serving_endpoint()
    token = os.getenv("DATABRICKS_TOKEN")
    if not endpoint or not token:
        logger.warning("LLM stream skipped: missing endpoint or token")
        yield "Summary unavailable."
        return
    host, endpoint_name = endpoint
    try:
        try:
            from openai import OpenAI

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
            return
        except ImportError:
            pass

        response = requests.post(
            f"{host}/serving-endpoints/{endpoint_name}/invocations",
            headers={"Authorization": f"Bearer {token}"},
            json={"messages": [{"role": "user", "content": prompt}], "stream": True},
            stream=True,
            timeout=90,
        )
        response.raise_for_status()
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            if line.startswith("data: "):
                data = line[len("data: ") :].strip()
                if data == "[DONE]":
                    break
                try:
                    payload = json.loads(data)
                    delta = payload.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        yield content
                except json.JSONDecodeError:
                    continue
        return
    except Exception:
        logger.exception("LLM streaming failed")
        yield "Summary unavailable."


def format_summary_html(text: str) -> str:
    cleaned = text.replace("Generating overall summary...", "").replace(
        "Generating drill-down summary...", ""
    )
    cleaned = cleaned.strip()
    if not cleaned:
        return "Loading summary..."

    escaped = html.escape(cleaned)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"\*(.+?)\*", r"<em>\1</em>", escaped)

    lines = [line.strip() for line in escaped.splitlines() if line.strip()]
    paragraphs = []
    bullets = []

    for line in lines:
        if line.startswith(("-", "•")):
            bullets.append(line.lstrip("-• ").strip())
        elif line.startswith("#"):
            paragraphs.append(f"<div class=\"summary-heading\">{line.lstrip('# ').strip()}</div>")
        else:
            paragraphs.append(f"<p>{line}</p>")

    if not bullets:
        sentences = re.split(r"(?<=[.!?])\s+", cleaned)
        bullets = [html.escape(s.strip()) for s in sentences if s.strip()][:4]
        paragraphs = []

    bullet_html = ""
    if bullets:
        bullet_items = "".join(f"<li>{item}</li>" for item in bullets)
        bullet_html = f"<ul>{bullet_items}</ul>"

    return "".join(paragraphs) + bullet_html


st.set_page_config(page_title="Controller Variance Analysis", layout="wide")
st.markdown(
    """
    <style>
        .block-container {
            padding-top: 1.25rem;
            padding-bottom: 1.25rem;
        }
        div[data-testid="stVerticalBlock"] > div {
            gap: 0.75rem;
        }
        .muted {
            color: #6b6b6b;
        }
        .summary-text {
            font-size: 16px;
            line-height: 1.4;
            white-space: normal;
        }
        .summary-text ul {
            margin: 0.2rem 0 0.2rem 1.1rem;
            padding: 0;
        }
        .summary-text li {
            margin-bottom: 0.2rem;
        }
        .summary-text p {
            margin: 0.3rem 0;
        }
        .summary-heading {
            font-weight: 700;
            margin: 0.2rem 0 0.4rem 0;
        }
        .loading {
            opacity: 0.5;
            filter: grayscale(1);
        }
        .stMultiSelect [data-baseweb="tag"] {
            background-color: #f1f3f5;
            color: #333333;
        }
        .stMultiSelect [data-baseweb="tag"] span {
            color: #333333;
        }
        .stMultiSelect div[data-baseweb="select"] > div {
            max-height: 3rem;
            overflow-y: auto;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

# Band 1: Title + Executive Insight
with st.container():
    st.title("Controller Variance Analysis")
    st.markdown(
        '<span class="muted">Executive summary of account-level variance, month-over-month performance, and key drivers across the portfolio.</span>',
        unsafe_allow_html=True,
    )

# Band 2: Filters + KPIs
with st.container():
    filter_left, filter_right = st.columns([3, 2])
    with filter_left:
        accounts = get_accounts()
        if accounts is None:
            accounts = []
        if not accounts:
            st.warning("API unavailable. Start the backend to load accounts.")
        selected_accounts = st.multiselect(
            "Accounts",
            accounts,
            default=accounts,
            placeholder="All Accounts",
            label_visibility="collapsed",
        )
    with filter_right:
        months = get_months()
        if months is None:
            months = []
        default_month = months[0] if months else date.today().strftime("%Y-%m")
        selected_month = st.selectbox(
            "Month",
            months or [default_month],
            index=0,
            label_visibility="collapsed",
        )

    accounts_key = ",".join(selected_accounts) if selected_accounts else ""
    with ThreadPoolExecutor(max_workers=2) as executor:
        metrics_future = executor.submit(get_metrics, selected_month, accounts_key)
        table_future = executor.submit(get_table, selected_month, accounts_key)

    kpi1, kpi2, kpi3 = st.columns(3)
    with kpi1:
        kpi_primary = st.empty()
        kpi_primary.metric("Net Returns %", "--", delta="--")
    with kpi2:
        kpi_secondary = st.empty()
        kpi_secondary.metric("Net Returns ($)", "--")
    with kpi3:
        kpi_tertiary = st.empty()
        kpi_tertiary.metric("Accounts Handled", "--")

# Executive Summary
st.subheader("Executive Summary")
overall_placeholder = st.empty()
overall_placeholder.markdown(
    '<div class="summary-text muted">Loading executive summary...</div>',
    unsafe_allow_html=True,
)

# Account Variance + Drilldown
table_col, drill_col = st.columns([2, 1])
with table_col:
    st.subheader("Account Variance")
    table_placeholder = st.empty()
    table_placeholder.markdown(
        '<div class="summary-text muted loading">Loading account variance table...</div>',
        unsafe_allow_html=True,
    )
with drill_col:
    month_label = date.fromisoformat(f"{selected_month}-01").strftime("%b %Y")
    st.subheader(f"Key Insights – {month_label}")
    drilldown_placeholder = st.empty()
    drilldown_placeholder.markdown(
        '<div class="summary-text muted loading">Loading key insights...</div>',
        unsafe_allow_html=True,
    )

metrics = metrics_future.result()
kpi_primary.metric(
    "Net Returns %",
    f"{metrics['net_returns_pct']}%",
    delta=f"{metrics['net_returns_pct']} pp",
)
kpi_secondary.metric("Net Returns ($)", f"${metrics['net_returns_k']}k")
kpi_tertiary.metric("Accounts Handled", metrics["accounts_handled"])

table = table_future.result()
table_df = pd.DataFrame(table)
if not table_df.empty:
    table_df["Variance %"] = table_df["Variance %"].str.replace("%", "", regex=False).astype(float)
    table_df["Last Month"] = pd.to_numeric(table_df["Last Month"], errors="coerce")
    table_df["Current Month"] = pd.to_numeric(table_df["Current Month"], errors="coerce")
    min_variance = table_df["Variance %"].min()

    def variance_color(val):
        if val < 0:
            return "color: #b00020"
        if val > 0:
            return "color: #1b5e20"
        return "color: #333333"

    def highlight_min(val):
        return "font-weight: 700" if val == min_variance else ""

    styled = (
        table_df.style.format(
            {
                "Last Month": "{:,.0f}",
                "Current Month": "{:,.0f}",
                "Variance %": "{:.1f}%",
            }
        )
        .applymap(variance_color, subset=["Variance %"])
        .applymap(highlight_min, subset=["Variance %"])
        .set_properties(
            subset=["Last Month", "Current Month", "Variance %"],
            **{"text-align": "right"},
        )
    )
    table_placeholder.dataframe(styled, width="stretch")
else:
    table_placeholder.dataframe(table_df, width="stretch")

if "overall_summary_text" not in st.session_state:
    st.session_state["overall_summary_text"] = ""
if "overall_loaded" not in st.session_state:
    st.session_state["overall_loaded"] = False

overall_text = st.session_state["overall_summary_text"]
overall_done = st.session_state["overall_loaded"]

if overall_done and overall_text:
    overall_placeholder.markdown(
        f"<div class=\"summary-text\">{format_summary_html(overall_text)}</div>",
        unsafe_allow_html=True,
    )
else:
    overall_text = ""

overall_queue = queue.Queue()
drilldown_queue = queue.Queue()

if not overall_done:
    ThreadPoolExecutor(max_workers=1).submit(
        stream_summary_to_queue, "/summary/overall/stream", {}, overall_queue
    )
ThreadPoolExecutor(max_workers=1).submit(
    stream_summary_to_queue,
    "/summary/drilldown/stream",
    {"month": selected_month, "accounts": accounts_key},
    drilldown_queue,
)

drilldown_text = ""
drilldown_done = False

while not (overall_done and drilldown_done):
    if not overall_done:
        while True:
            try:
                item = overall_queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                overall_done = True
                st.session_state["overall_loaded"] = True
                break
            overall_text += item
            overall_placeholder.markdown(
                f"<div class=\"summary-text\">{format_summary_html(overall_text)}</div>",
                unsafe_allow_html=True,
            )

    while True:
        try:
            item = drilldown_queue.get_nowait()
        except queue.Empty:
            break
        if item is None:
            drilldown_done = True
            break
        drilldown_text += item
        drilldown_placeholder.markdown(
            f"<div class=\"summary-text\">{format_summary_html(drilldown_text)}</div>",
            unsafe_allow_html=True,
        )

    time.sleep(0.02)

if not st.session_state["overall_summary_text"] and overall_text:
    st.session_state["overall_summary_text"] = overall_text
