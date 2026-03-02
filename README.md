# AgentBricks Finance Assistant

A full-stack Databricks App that lets users ask natural language questions about finance data. A Multi-Agent Supervisor (MAS) orchestrates a Genie analytics agent to query insurance data and return streamed, markdown-formatted answers. Conversation history is persisted in Lakebase so users can resume sessions from any point in time.

---

## Live App

| | |
|---|---|
| **App URL** | https://agentbricks-finance-7474643771848083.aws.databricksapps.com |
| **Logs** | https://agentbricks-finance-7474643771848083.aws.databricksapps.com/logz |
| **Workspace** | https://fe-sandbox-akash-finance-app.cloud.databricks.com |

---

## Architecture

```
Browser (React SPA)
    │
    │  SSE streaming (text/event-stream)
    ▼
FastAPI Backend  (uvicorn, port 8000)
    │
    ├── POST /api/chat  ──────────────────────────────────────────────────┐
    │       │                                                              │
    │       │  1. Look up user_sp_mapping in Lakebase                      │
    │       │  2. Get OAuth token for mapped SP (or fall back to app SP)   │
    │       │  3. Call MAS with that token (enforces UC RLS)               │
    │       │                                                              │
    │       │  OpenAI Responses API (user SP token)                        │
    │       ▼                                                              │
    │   Multi-Agent Supervisor  (mas-ea793b7b-endpoint)                    │
    │       │                                                              │
    │       ├── Genie Agent  ──► Genie Space (Akash Finance Analytics)     │
    │       │                       └── SQL Warehouse  ──► main.akash_finance
    │       └── (other sub-agents as needed)                   UC Row Filter applied
    │                                                                      │
    ├── GET/POST /api/sessions ─────────────────────────────────────────► Lakebase (PostgreSQL)
    ├── GET/POST/DELETE /api/admin/sp-mappings ────────────────────────►  │  (user_sp_mapping table)
    │                                                                      │
    └── GET /api/health                                                    │
                                                                           │
    User identity: X-Forwarded-Email header (injected by Databricks Apps) ┘
```

---

## Databricks Components

### 1. Databricks App
| Property | Value |
|---|---|
| **App name** | `agentbricks-finance` |
| **App URL** | `https://agentbricks-finance-7474643771848083.aws.databricksapps.com` |
| **Service Principal client ID** | `eb060b89-c400-4e1b-813a-226f955b95bc` |
| **Service Principal name** | `app-3dc4d8 agentbricks-finance` |
| **Service Principal ID** | `71191723482429` |
| **Owner** | `akash.s@databricks.com` |
| **Runtime** | Python 3.11, uvicorn on port 8000 |

### 2. Multi-Agent Supervisor (MAS)
The core AI orchestration layer. Receives user messages (with full conversation history) and routes to sub-agents.

| Property | Value |
|---|---|
| **Endpoint name** | `mas-ea793b7b-endpoint` |
| **Endpoint numeric ID** | `147214d1845d4bfbbd86b328031042ea` |
| **API** | OpenAI Responses API (`client.responses.create()`) |
| **Input** | Array of `{role, content}` messages (full history, no limit) |
| **Output** | Streamed via `response.output_text.delta` events |
| **SP permission** | `CAN_QUERY` on the endpoint |

**Streaming event structure:** The MAS produces multiple output items in sequence, separated by `response.output_item.done` events:

```
response.output_text.delta  ← supervisor planning text ("I'll query…")  ← discarded
response.output_item.done   ← end of thinking item
response.output_item.done   ← tool call / Genie query running
response.output_item.done   ← ...
response.output_text.delta  ← final answer, streamed token by token      ← shown to user
response.output_item.done
```

`stream_mas_agent()` buffers all text until the first `response.output_item.done` fires (discarding the planning text), then streams every subsequent `response.output_text.delta` chunk immediately. For direct responses with no tool calls, the full buffer is emitted at the end.

### 3. Genie Space
The analytics agent that the MAS calls to run SQL queries against finance data.

| Property | Value |
|---|---|
| **Space name** | `Akash Finance Analytics` |
| **Space ID** | `01f1138320f719fb844d052d96e39383` |
| **SQL Warehouse** | `Serverless Starter Warehouse` (`1b1d59e180e4ac26`) |
| **Tables** | `main.akash_finance.claims`, `main.akash_finance.policies`, `main.akash_finance.financial_summary` |
| **SP permission** | `CAN_RUN` on the Genie space |

### 4. Lakebase (PostgreSQL)
Used for persisting chat sessions and message history. All conversations survive app restarts and redeploys.

| Property | Value |
|---|---|
| **Instance name** | `akash-finance-app` |
| **Host** | `instance-383773af-2ab5-4bfd-971d-9dba95011ab4.database.cloud.databricks.com` |
| **Port** | `5432` |
| **Database** | `databricks_postgres` |
| **Auth user (PGUSER)** | `eb060b89-c400-4e1b-813a-226f955b95bc` (SP client ID) |
| **Auth method** | Databricks SDK `generate_database_credential()` — short-lived token, refreshed on pool restart |
| **SSL** | `require` |

#### Schema

```sql
-- chat_sessions: one row per conversation
CREATE TABLE chat_sessions (
    session_id  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     TEXT        NOT NULL DEFAULT 'anonymous',   -- from X-Forwarded-Email
    title       TEXT        NOT NULL DEFAULT 'New Chat',    -- first 60 chars of first message
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()          -- auto-updated via trigger
);

-- chat_messages: every user and assistant message
CREATE TABLE chat_messages (
    message_id  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  UUID        NOT NULL REFERENCES chat_sessions ON DELETE CASCADE,
    role        TEXT        NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_messages_session ON chat_messages(session_id, created_at);

-- user_sp_mapping: maps user email → service principal for SP-routing RLS
CREATE TABLE user_sp_mapping (
    user_email   TEXT        PRIMARY KEY,
    sp_client_id TEXT        NOT NULL,
    role_name    TEXT        NOT NULL DEFAULT 'analyst',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### 5. SP-Routing Service Principals

Two dedicated SPs are used for per-user Row-Level Security. On each `POST /api/chat`, the backend looks up `X-Forwarded-Email` in `user_sp_mapping`, fetches an OAuth token for the mapped SP via client credentials, and passes it to the MAS call. Users not in the mapping fall back to the app SP (no RLS).

| SP | Client ID | Role | Data access |
|---|---|---|---|
| `finance-sp-manager` | `0832d2b6-0b7b-483f-aa0b-729724806b31` | manager | Full access — all 20 claims (Paid + Denied + Pending) |
| `finance-sp-analyst` | `7e817ad5-0e4f-4fd1-8dbf-31b41432115e` | analyst | Restricted — 15 claims (Paid only, via UC Row Filter) |

**UC Row Filter** (`main.akash_finance.claims_rls_filter`) applied to `claims` table:
```sql
CREATE FUNCTION main.akash_finance.claims_rls_filter(claim_status STRING)
RETURNS BOOLEAN
RETURN IF(CURRENT_USER() = '7e817ad5-0e4f-4fd1-8dbf-31b41432115e', claim_status = 'Paid', TRUE);

ALTER TABLE main.akash_finance.claims
  SET ROW FILTER main.akash_finance.claims_rls_filter ON (status);
```

**Demo user → SP mapping** (stored in `user_sp_mapping`):
| Email | SP | Role |
|---|---|---|
| `akash.s@databricks.com` | `finance-sp-manager` | manager |
| `demo-analyst@databricks.com` | `finance-sp-analyst` | analyst |

> **Note:** Genie runs SQL as the space owner (`akash.s`), so the UC Row Filter currently applies to **direct SQL warehouse calls** only. To enforce RLS end-to-end through Genie, enable _Run as Viewer_ on the Genie space (Genie UI → space settings → Run queries as → Viewer).

### 6. Finance Data (Unity Catalog)
Insurance data used by the Genie space. Stored in the `main` catalog (metastore default storage) due to an IAM misconfiguration in the originally provisioned `akash_finance_app_catalog`.

| Table | Catalog.Schema | Description |
|---|---|---|
| `claims` | `main.akash_finance` | 20 rows — individual insurance claims (Q1 2025 + Q1 2026) |
| `policies` | `main.akash_finance` | 10 rows — active and lapsed insurance policies |
| `financial_summary` | `main.akash_finance` | View — claims aggregated by year, quarter, and type |

**Claims columns:** `claim_id`, `policy_id`, `claim_type`, `status` (Paid/Denied/Pending), `claim_amount`, `approved_amount`, `fraud_flag`, `fraud_score`, `damage_severity`, `claim_date`, `settlement_date`

**Policies columns:** `policy_id`, `policy_type` (Auto/Home/Health/Life), `status`, `annual_premium`, `coverage_amount`, `customer_id`, `state`, `start_date`, `end_date`

**UC permissions on `main` catalog:** `USE CATALOG + USE SCHEMA + SELECT` granted to app SP `eb060b89-...` and to `account users`.

### 7. Foundation Model Endpoint
Used for general-purpose completions (available but not used for the main chat flow — MAS handles that).

| Property | Value |
|---|---|
| **Endpoint name** | `databricks-claude-sonnet-4-5` |
| **Model** | Claude Sonnet 4.5 (Anthropic, served by Databricks) |
| **Env var** | `SERVING_ENDPOINT` |

---

## Backend

**Language:** Python 3.11
**Framework:** FastAPI 0.115+ with uvicorn
**Package manager:** uv

### File Structure

```
agentbricks-finance/
├── app.py                      # FastAPI entry point, lifespan, SPA serving
├── app.yaml                    # Databricks App config (command, env vars, SP credentials)
├── pyproject.toml              # Python dependencies (uv)
├── requirements.txt            # Flat requirements for Databricks deployment
└── server/
    ├── config.py               # Dual-mode auth (App SP vs local CLI profile)
    ├── db.py                   # Lakebase pool, table creation, get_sp_for_user()
    ├── llm.py                  # MAS streaming client (stream_mas_agent, user_token override)
    ├── sp_auth.py              # SP credential lookup + OAuth token generation (RLS routing)
    ├── genie.py                # Genie API wrapper (not used in current flow)
    ├── agents.py               # Legacy agent code (superseded by MAS endpoint)
    └── routes/
        ├── chat.py             # POST /api/chat — SP lookup + SSE streaming
        └── sessions.py         # CRUD for sessions/messages + admin SP-mapping endpoints
```

### Key Python Dependencies

| Package | Version | Purpose |
|---|---|---|
| `fastapi` | ≥0.115 | HTTP framework |
| `uvicorn[standard]` | ≥0.30 | ASGI server |
| `openai` | ≥1.52 | MAS endpoint via Responses API |
| `asyncpg` | ≥0.29 | Async PostgreSQL driver for Lakebase |
| `databricks-sdk` | ≥0.40 | Auth, `generate_database_credential()` |
| `aiohttp` | ≥3.9 | Async HTTP (Genie API) |
| `pydantic` | ≥2.0 | Request/response models |

### API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Health check, reports `db_mode` |
| `POST` | `/api/sessions` | Create a new chat session |
| `GET` | `/api/sessions` | List all sessions (most recent first, max 100) |
| `GET` | `/api/sessions/{id}/messages` | Get all messages for a session |
| `PATCH` | `/api/sessions/{id}` | Rename a session |
| `DELETE` | `/api/sessions/{id}` | Delete session and all its messages |
| `POST` | `/api/chat` | Send a message, stream response via SSE |
| `GET` | `/api/admin/sp-mappings` | List all user → SP mappings |
| `POST` | `/api/admin/sp-mappings` | Upsert a user → SP mapping `{user_email, sp_client_id, role_name}` |
| `DELETE` | `/api/admin/sp-mappings/{email}` | Remove a mapping (user falls back to app SP) |

### SSE Event Format (`POST /api/chat`)

```
data: {"type": "chunk",  "text": "..."}          // token-by-token
data: {"type": "done",   "message_id": "..."}    // stream complete
data: {"type": "error",  "message": "..."}       // failure
```

### Authentication (Dual Mode)

| Environment | Auth method |
|---|---|
| **Databricks App** | `WorkspaceClient()` — auto-uses injected SP credentials (`DATABRICKS_APP_NAME` env var is set) |
| **Local dev** | `WorkspaceClient(profile="fevm-akash-finance-app")` — uses `~/.databrickscfg` |
| **Lakebase password** | `client.database.generate_database_credential(instance_names=["akash-finance-app"])` |

User identity is read from `X-Forwarded-Email` header (injected by Databricks Apps at runtime; falls back to `"anonymous"` locally).

---

## Frontend

**Framework:** React 18 + TypeScript
**Bundler:** Vite 6
**Served from:** `frontend/dist/` (built SPA, served by FastAPI as static files)

### File Structure

```
frontend/
├── src/
│   ├── App.tsx         # Main component — sidebar, chat panel, streaming logic
│   ├── api.ts          # Fetch wrappers for all backend endpoints + SSE consumer
│   └── types.ts        # TypeScript types (Session, Message, ChatResponse)
├── vite.config.ts      # Proxy /api → localhost:8000 for local dev
└── dist/               # Production build output (committed for deployment)
```

### Key Dependencies

| Package | Purpose |
|---|---|
| `react-markdown` | Render markdown in assistant responses (headings, tables, code, bold, italic) |
| `lucide-react` | Icon library (send, plus, trash, message icons) |

### Streaming Architecture

SSE chunks arrive as JavaScript Promise resolutions (microtasks), which all queue up before any macrotask (like `requestAnimationFrame`) fires. Using RAF batching causes the entire response to accumulate before a single render — it looks static. `flushSync` forces a synchronous React DOM update for each chunk, bypassing React 18's automatic batching, so every token renders immediately as it arrives.

```
MAS delta events → stream_mas_agent (filters planning text) → FastAPI SSE
    → api.ts reader.read() → onChunk callback → flushSync(setMessages)
    → immediate DOM update → user sees token appear
```

### Features

- **Session sidebar** — lists all conversations, most recent first, with message count and date
- **Streaming** — tokens appear word-by-word as the MAS final answer generates; loading spinner shows during the orchestration/Genie phase before first token arrives
- **Markdown rendering** — assistant responses render full markdown including tables, headings, code blocks, and lists; plain text is shown during streaming, markdown is parsed on completion
- **Session persistence** — click any session in the sidebar to resume it; full history is re-injected as context
- **Demo mode** — if Lakebase is unavailable, falls back to in-memory storage (sessions lost on restart)

---

## Local Development

### Prerequisites
- `uv` installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- `node` + `npm`
- Databricks CLI configured with profile `fevm-akash-finance-app`

### Run backend

```bash
cd agentbricks-finance
export DATABRICKS_PROFILE=fevm-akash-finance-app
uv run uvicorn app:app --reload --port 8000
```

### Run frontend (dev mode)

```bash
cd frontend
npm install
npm run dev    # http://localhost:5173, proxies /api → :8000
```

### Build frontend for deployment

```bash
cd frontend
npm run build  # outputs to frontend/dist/
```

---

## Deployment

### Sync and deploy

```bash
# Sync files (excludes node_modules, .venv, source files)
databricks sync . /Users/akash.s@databricks.com/agentbricks-finance \
  -p fevm-akash-finance-app \
  --exclude node_modules --exclude .venv --exclude __pycache__ \
  --exclude ".git" --exclude "frontend/src" --exclude "frontend/public" \
  --full

# Deploy
databricks apps deploy agentbricks-finance \
  --source-code-path /Workspace/Users/akash.s@databricks.com/agentbricks-finance \
  -p fevm-akash-finance-app
```

### Add Lakebase resource (REST API — CLI doesn't support `database` type yet)

```bash
TOKEN=$(databricks auth token -p fevm-akash-finance-app | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
curl -X PATCH "https://fe-sandbox-akash-finance-app.cloud.databricks.com/api/2.0/apps/agentbricks-finance" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "resources": [{
      "name": "database",
      "database": {
        "instance_name": "akash-finance-app",
        "database_name": "databricks_postgres",
        "permission": "CAN_CONNECT_AND_CREATE"
      }
    }]
  }'
```

### Grant Lakebase permissions (after app SP is created)

```python
# Run via: uv run python3 grant.py
import asyncio, asyncpg, subprocess, json

async def grant():
    token = json.loads(subprocess.run(
        ['databricks', 'auth', 'token', '-p', 'fevm-akash-finance-app'],
        capture_output=True, text=True).stdout)['access_token']
    conn = await asyncpg.connect(
        host='instance-383773af-2ab5-4bfd-971d-9dba95011ab4.database.cloud.databricks.com',
        port=5432, database='databricks_postgres',
        user='akash.s@databricks.com', password=token, ssl='require')
    sp = 'eb060b89-c400-4e1b-813a-226f955b95bc'
    for sql in [
        f'GRANT USAGE ON SCHEMA public TO "{sp}"',
        f'GRANT CREATE ON SCHEMA public TO "{sp}"',
        f'GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO "{sp}"',
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO "{sp}"',
    ]:
        await conn.execute(sql)
    await conn.close()

asyncio.run(grant())
```

---

## Permissions Summary

| Resource | Principal | Permission |
|---|---|---|
| MAS endpoint `mas-ea793b7b-endpoint` | App SP `eb060b89-...` | `CAN_QUERY` |
| MAS endpoint `mas-ea793b7b-endpoint` | Both role SPs | accessible (no explicit grant needed) |
| Genie space `01f1138320f719fb844d052d96e39383` | App SP `eb060b89-...` | `CAN_RUN` |
| Genie space `01f1138320f719fb844d052d96e39383` | `finance-sp-analyst`, `finance-sp-manager` | `CAN_RUN` |
| SQL Warehouse `1b1d59e180e4ac26` | App SP + both role SPs | `CAN_USE` |
| UC catalog `main` | App SP + `account users` | `USE_CATALOG` |
| UC schema `main.akash_finance` | App SP + `account users` + both role SPs | `USE_CATALOG, USE_SCHEMA` |
| UC table `main.akash_finance.claims` | `finance-sp-manager` | `SELECT` (all 20 rows) |
| UC table `main.akash_finance.claims` | `finance-sp-analyst` | `SELECT` (15 rows — Paid only via row filter) |
| UC view `main.akash_finance.claims_analyst` | `finance-sp-analyst` | `SELECT` (Paid claims only) |
| UC function `main.akash_finance.claims_rls_filter` | Both role SPs | `EXECUTE` |
| UC schema `main.akash_finance` | `finance-sp-manager` | `SELECT` (all tables) |
| Lakebase `public` schema | App SP `eb060b89-...` | `USAGE, CREATE, ALL ON TABLES, ALL ON SEQUENCES` |
| Lakebase `user_sp_mapping` table | App SP `eb060b89-...` | `SELECT, INSERT, UPDATE, DELETE` |

---

## Known Issues

- **`akash_finance_app_catalog` is broken** — the AWS IAM role backing its Unity Catalog storage credential (`akash-finance-app-ext-role-332745928618-iud74m`) was not configured correctly at FEVM provisioning time. All finance tables were recreated in `main.akash_finance` as a workaround.
- **Genie space tables** — after any recreation, the Genie space tables must be updated manually in the UI to point to `main.akash_finance.*` (no public API for this).
- **Lakebase token expiry** — the database credential token expires after ~1 hour. The pool is refreshed via `db.refresh_token()` which can be wired to a background task if needed.
- **Genie runs SQL as space owner** — Genie executes warehouse queries as the space owner (`akash.s@databricks.com`), so the UC Row Filter (`CURRENT_USER()`) evaluates to the owner's identity for all users. RLS is correctly enforced for **direct SQL warehouse calls** per SP. To enforce RLS end-to-end through Genie, enable _Run as Viewer_ in the Genie space settings UI.
