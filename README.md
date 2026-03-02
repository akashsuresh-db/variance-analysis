# AgentBricks Finance Assistant

A full-stack Databricks App for natural language analytics over insurance data. Users ask questions in a chat interface; a Multi-Agent Supervisor (MAS) orchestrates a Genie analytics agent to run SQL and return streamed, markdown-formatted answers. Every conversation is persisted in Lakebase so users can resume any past session with full context.

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
    │  POST /api/chat  ←→  SSE streaming response
    │  GET/POST /api/sessions
    ▼
FastAPI Backend  (uvicorn, port 8000)
    │
    ├─ Resolve user identity (X-Forwarded-Email header)
    ├─ Look up user → SP mapping in Lakebase
    ├─ Fetch full conversation history from Lakebase
    ├─ Append new user message → build messages[]
    │
    │  OpenAI Responses API  (stream=True, SP OAuth token)
    ▼
Multi-Agent Supervisor  (mas-ea793b7b-endpoint)
    │
    ├── Supervisor agent  — plans the response
    ├── Genie agent  ────► Genie Space (Akash Finance Analytics)
    │                          └── SQL Warehouse → main.akash_finance.*
    │                                              UC Row Filter applied per SP
    └── Final answer  ───► streamed token-by-token back to browser
    │
    ▼
Lakebase (PostgreSQL)
    ├── chat_sessions    — one row per conversation
    ├── chat_messages    — all user + assistant turns
    └── user_sp_mapping  — user email → SP for RLS routing
```

---

## Context Management

Every request carries the full conversation history so the MAS has complete context regardless of whether it's a new chat or a resumed past session.

### How it works

**1. Storing messages**

Every user message and assistant response is saved to Lakebase immediately:

```python
# On user send — saved before MAS is called
INSERT INTO chat_messages (session_id, role, content) VALUES ($1, 'user', $2)

# On stream complete — saved after last token
INSERT INTO chat_messages (session_id, role, content) VALUES ($1, 'assistant', $2)
```

**2. Fetching history on every request**

When `POST /api/chat` is called, the backend fetches all prior messages for the session, ordered oldest-first:

```python
SELECT role, content FROM chat_messages
WHERE session_id = $1
ORDER BY created_at ASC
```

**3. Building the MAS input**

The full history is prepended to the new user message and sent to the MAS as a single `messages` array:

```python
messages = history + [{"role": "user", "content": req.message}]
# → passed directly to client.responses.create(input=messages)
```

The MAS receives the complete conversation on every turn. There is no summarisation, windowing, or truncation — all turns are included.

**4. Resuming a past session (frontend)**

When a user clicks a past session in the sidebar:

```
handleSelectSession(session_id)
  → GET /api/sessions/{id}/messages   ← loads history for display
  → setActiveSessionId(session_id)    ← subsequent sends use this ID
```

On the next send, `POST /api/chat` is called with that `session_id`. The backend fetches the complete history (all previous turns from Lakebase) and passes it to the MAS exactly as it would for a live session. From the MAS's perspective, there is no difference between a new chat and a resumed one.

### Context flow diagram

```
User opens past chat
        │
        ▼
GET /api/sessions/{id}/messages
        │  loads turns for UI display
        ▼
User types new question → POST /api/chat { session_id, message }
        │
        ▼
SELECT role, content FROM chat_messages WHERE session_id = ? ORDER BY created_at ASC
        │  returns all prior turns
        ▼
messages = [
  {"role": "user",      "content": "How many claims are there?"},   ← turn 1
  {"role": "assistant", "content": "There are 20 claims…"},         ← turn 1 reply
  {"role": "user",      "content": "Break them down by type"},      ← turn 2
  {"role": "assistant", "content": "Auto: 8, Home: 6…"},            ← turn 2 reply
  {"role": "user",      "content": "<new question>"},               ← current
]
        │
        ▼
MAS endpoint — sees full conversation, answers in context
```

---

## Components

### 1. Databricks App

The runtime host. Databricks Apps injects the app service principal credentials as environment variables and forwards the authenticated user's email via `X-Forwarded-Email`.

| Property | Value |
|---|---|
| **App name** | `agentbricks-finance` |
| **Service Principal** | `app-3dc4d8 agentbricks-finance` (`eb060b89-c400-4e1b-813a-226f955b95bc`) |
| **Runtime** | Python 3.11, uvicorn on port 8000 |

### 2. Multi-Agent Supervisor (MAS)

The core AI layer. Receives the full `messages` array, orchestrates sub-agents, and streams back the final answer.

| Property | Value |
|---|---|
| **Endpoint** | `mas-ea793b7b-endpoint` |
| **API** | OpenAI Responses API (`client.responses.create(stream=True)`) |
| **Input** | `messages[]` — full conversation history + new user message |
| **Output** | Streamed via `response.output_text.delta` events |

**Streaming event structure:**

The MAS produces multiple output items separated by `response.output_item.done` events. Only the final text item is shown to the user; the planning text is discarded.

```
response.output_text.delta  ← supervisor planning ("I'll query…")  → discarded
response.output_item.done   ← end of planning item
response.output_item.done   ← Genie SQL running…
response.output_item.done   ← ...
response.output_text.delta  ← final answer, token by token          → streamed to user
response.output_item.done
```

`stream_mas_agent()` buffers all text until the first `response.output_item.done` fires, then streams every subsequent `response.output_text.delta` chunk immediately.

### 3. Genie Space

The analytics sub-agent. The MAS delegates data questions here; Genie translates them to SQL and runs them against the finance tables.

| Property | Value |
|---|---|
| **Space** | `Akash Finance Analytics` (`01f1138320f719fb844d052d96e39383`) |
| **Warehouse** | Serverless Starter Warehouse (`1b1d59e180e4ac26`) |
| **Tables** | `main.akash_finance.claims`, `.policies`, `.financial_summary` |

### 4. Lakebase (PostgreSQL)

Persistent storage for sessions, messages, and SP mappings.

| Property | Value |
|---|---|
| **Instance** | `akash-finance-app` |
| **Host** | `instance-383773af-2ab5-4bfd-971d-9dba95011ab4.database.cloud.databricks.com` |
| **Auth** | Databricks SDK `generate_database_credential()` — short-lived OAuth token |

#### Schema

```sql
-- One row per conversation
CREATE TABLE chat_sessions (
    session_id  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     TEXT        NOT NULL DEFAULT 'anonymous',
    title       TEXT        NOT NULL DEFAULT 'New Chat',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Every user and assistant turn
CREATE TABLE chat_messages (
    message_id  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  UUID        NOT NULL REFERENCES chat_sessions ON DELETE CASCADE,
    role        TEXT        NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_messages_session ON chat_messages(session_id, created_at);

-- Maps user email → service principal for RLS routing
CREATE TABLE user_sp_mapping (
    user_email   TEXT        PRIMARY KEY,
    sp_client_id TEXT        NOT NULL,
    role_name    TEXT        NOT NULL DEFAULT 'analyst',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### 5. SP-Routing & Row-Level Security

On each chat request the backend looks up the user's email in `user_sp_mapping`, obtains an OAuth token for the mapped SP via client credentials, and passes it to the MAS. Unity Catalog enforces data access based on that SP's grants.

| SP | Role | Data access |
|---|---|---|
| `finance-sp-manager` | manager | All 20 claims (Paid + Denied + Pending) |
| `finance-sp-analyst` | analyst | 15 claims (Paid only — UC Row Filter) |

**UC Row Filter** on `main.akash_finance.claims`:

```sql
CREATE FUNCTION main.akash_finance.claims_rls_filter(claim_status STRING)
RETURNS BOOLEAN
RETURN IF(CURRENT_USER() = '<analyst-sp-client-id>', claim_status = 'Paid', TRUE);

ALTER TABLE main.akash_finance.claims
  SET ROW FILTER main.akash_finance.claims_rls_filter ON (status);
```

Users not in `user_sp_mapping` fall back to the app SP (unrestricted access).

> **Note:** Genie runs SQL as the space owner, so the UC Row Filter applies to direct SQL warehouse calls only. To enforce RLS end-to-end through Genie, enable _Run as Viewer_ in the Genie space settings.

### 6. Finance Data (Unity Catalog)

| Table | Schema | Description |
|---|---|---|
| `claims` | `main.akash_finance` | 20 rows — insurance claims with status, amounts, fraud flags |
| `policies` | `main.akash_finance` | 10 rows — active and lapsed policies |
| `financial_summary` | `main.akash_finance` | View — claims aggregated by year, quarter, and type |

---

## Backend

**Language:** Python 3.11 · **Framework:** FastAPI + uvicorn · **Package manager:** uv

### File Structure

```
agentbricks-finance/
├── app.py                  # FastAPI entry point, lifespan, SPA serving
├── app.yaml                # Databricks App config (command, env vars)
├── pyproject.toml          # Python dependencies
├── requirements.txt        # Flat requirements for deployment
└── server/
    ├── config.py           # Auth — WorkspaceClient, OAuth token, workspace host
    ├── db.py               # Lakebase pool, schema creation, get_sp_for_user()
    ├── llm.py              # stream_mas_agent() — filters planning text, streams final answer
    ├── sp_auth.py          # SP credential lookup + OAuth token generation
    └── routes/
        ├── chat.py         # POST /api/chat — context fetch, SP lookup, SSE streaming
        └── sessions.py     # Session/message CRUD + admin SP-mapping endpoints
```

### API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Health check |
| `POST` | `/api/sessions` | Create a new chat session |
| `GET` | `/api/sessions` | List all sessions (most recent first) |
| `GET` | `/api/sessions/{id}/messages` | Get all messages for a session |
| `PATCH` | `/api/sessions/{id}` | Rename a session |
| `DELETE` | `/api/sessions/{id}` | Delete session and messages |
| `POST` | `/api/chat` | Send message, stream response via SSE |
| `GET` | `/api/admin/sp-mappings` | List user → SP mappings |
| `POST` | `/api/admin/sp-mappings` | Upsert a mapping `{user_email, sp_client_id, role_name}` |
| `DELETE` | `/api/admin/sp-mappings/{email}` | Remove a mapping |

### SSE Event Format

```
data: {"type": "chunk",  "text": "…"}           // token arriving
data: {"type": "done",   "message_id": "…"}     // stream complete
data: {"type": "error",  "message": "…"}        // failure
```

### Key Dependencies

| Package | Purpose |
|---|---|
| `fastapi` + `uvicorn` | HTTP framework and ASGI server |
| `openai` | MAS endpoint via Responses API |
| `asyncpg` | Async PostgreSQL driver for Lakebase |
| `databricks-sdk` | SP auth, `generate_database_credential()` |
| `pydantic` | Request/response models |

---

## Frontend

**Framework:** React 18 + TypeScript · **Bundler:** Vite 6 · **Served from:** `frontend/dist/` (static files via FastAPI)

### File Structure

```
frontend/
├── src/
│   ├── App.tsx      # Sidebar, chat panel, streaming render logic
│   ├── api.ts       # Fetch wrappers for all endpoints + SSE reader
│   └── types.ts     # Session, Message TypeScript types
├── vite.config.ts   # Dev proxy: /api → localhost:8000
└── dist/            # Production build (committed for deployment)
```

### Streaming Render

SSE chunks arrive as Promise resolutions (microtasks). React 18 batches microtasks and would render the full response at once — making it appear static. `flushSync` forces a synchronous DOM update for each chunk, so every token renders the moment it arrives.

```
MAS delta events
  → stream_mas_agent  (discards planning text, yields final answer tokens)
  → FastAPI SSE       (data: {"type":"chunk","text":"…"})
  → api.ts reader     (onChunk callback per event)
  → flushSync(setMessages)
  → immediate DOM update → token appears in UI
```

While streaming, responses render as plain text with a blinking cursor. On the `done` event the full content is re-rendered as markdown (tables, headings, code blocks, bold/italic).

### Key Dependencies

| Package | Purpose |
|---|---|
| `react-markdown` | Render markdown in completed assistant responses |

---

## Deployment

```bash
# 1. Build the frontend
cd frontend && npm run build

# 2. Sync to Databricks workspace (excludes source, deps, build artifacts)
databricks sync . /Workspace/Users/<you>/agentbricks-finance \
  -p <your-cli-profile> \
  --exclude node_modules --exclude .venv --exclude __pycache__ \
  --exclude ".git" --exclude "frontend/src" --exclude "frontend/public"

# 3. Deploy
databricks apps deploy agentbricks-finance \
  --source-code-path /Workspace/Users/<you>/agentbricks-finance \
  -p <your-cli-profile>
```

Before deploying, fill in the SP credentials in `app.yaml`:

```yaml
- name: SP_ANALYST_CLIENT_ID
  value: "<your-analyst-sp-client-id>"
- name: SP_ANALYST_CLIENT_SECRET
  value: "<your-analyst-sp-client-secret>"
- name: SP_MANAGER_CLIENT_ID
  value: "<your-manager-sp-client-id>"
- name: SP_MANAGER_CLIENT_SECRET
  value: "<your-manager-sp-client-secret>"
```

---

## Permissions Summary

| Resource | Principal | Permission |
|---|---|---|
| MAS endpoint | App SP | `CAN_QUERY` |
| Genie space | App SP + both role SPs | `CAN_RUN` |
| SQL Warehouse | App SP + both role SPs | `CAN_USE` |
| UC catalog `main` | App SP + `account users` | `USE_CATALOG` |
| UC schema `main.akash_finance` | App SP + both role SPs | `USE_CATALOG, USE_SCHEMA` |
| UC table `claims` | `finance-sp-manager` | `SELECT` (all 20 rows) |
| UC table `claims` | `finance-sp-analyst` | `SELECT` (15 rows via row filter) |
| UC function `claims_rls_filter` | Both role SPs | `EXECUTE` |
| Lakebase `public` schema | App SP | `USAGE, CREATE, ALL ON TABLES` |

---

## Known Issues

- **Genie runs SQL as space owner** — the UC Row Filter (`CURRENT_USER()`) evaluates to the space owner for all users. RLS is enforced for direct SQL warehouse calls per SP. To enforce it end-to-end through Genie, enable _Run as Viewer_ in the Genie space settings UI.
- **Lakebase token expiry** — the database credential token expires after ~1 hour. The connection pool is refreshed via `db.refresh_token()`, which can be wired to a background task for long-running deployments.
