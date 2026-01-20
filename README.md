# Controller Variance Analysis (Databricks App)

Executive-focused dashboard for month-over-month variance analysis across accounts. The app is built in Streamlit and designed to run in Databricks Apps, using Databricks Postgres for data and a Databricks serving endpoint for AI summaries.

## What This App Does

- Shows KPIs for month-over-month performance.
- Provides an executive summary across all months (independent of filters).
- Provides key insights for the selected month and account filters.
- Displays a variance table with conditional formatting.
- Streams AI summaries token-by-token for responsive UX.

## Architecture

- **Streamlit UI**: `app.py`
- **Data source**: Databricks Postgres (via `JDBC_URL`)
- **AI summaries**: Databricks Serving Endpoint (OpenAI-compatible API)
- **Databricks Apps config**: `app.yaml`, `manifest.yaml`

The app can run **without** the local FastAPI backend. If `API_BASE_URL` is set, it will call the backend; otherwise it connects directly to Postgres and the serving endpoint.

## Key Files

- `app.py`  
  Main Streamlit app with layout, filters, KPIs, data queries, and AI streaming.

- `app.yaml`  
  Databricks App runtime command configuration.

- `manifest.yaml`  
  Databricks App resources and permission scopes (serving endpoint).

- `backend/`  
  Optional local FastAPI backend (not required for Databricks Apps).

- `config/env.local`  
  Local env file for development. Do not commit secrets.

## Environment Variables

### Required (Databricks App / Local)

- `JDBC_URL`  
  Databricks Postgres JDBC URL  
  Example:  
  `jdbc:postgresql://<host>:5432/<db>?sslmode=require`

- `DB_TABLE_FULL_NAME`  
  Fully qualified table name  
  Example:  
  `acndemo.akash_s.sales_variance_synced`

- `DATABRICKS_TOKEN`  
  Token with access to the serving endpoint and Postgres identity login.

### Required for AI summaries

- `DATABRICKS_HOST`  
  Workspace URL, e.g. `https://e2-demo-field-eng.cloud.databricks.com`

- `SERVING_ENDPOINT_NAME`  
  Name of the serving endpoint configured in Databricks Apps.

### Optional (local backend)

- `API_BASE_URL`  
  If set, the app uses the local FastAPI backend instead of direct DB access.

## Running Locally

1. Create `config/env.local` (already in repo) and set:
   - `JDBC_URL`
   - `DB_TABLE_FULL_NAME`
   - `DATABRICKS_HOST`
   - `DATABRICKS_TOKEN`
   - `SERVING_ENDPOINT_NAME`

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Run Streamlit:

```bash
streamlit run app.py
```

If you want to use the optional backend:

```bash
pip install -r backend/requirements.txt
uvicorn backend.main:app --reload
```

Set `API_BASE_URL=http://localhost:8000` in `config/env.local`.

## Deploying to Databricks Apps

1. Ensure `app.yaml` and `manifest.yaml` are present at repo root.
2. Create a Databricks App and add a **Serving Endpoint** resource.
3. Set environment variables in the App settings:
   - `JDBC_URL`
   - `DB_TABLE_FULL_NAME`
   - `DATABRICKS_HOST`
   - `DATABRICKS_TOKEN`
   - `SERVING_ENDPOINT_NAME` (from app resource)
4. Deploy the app from this repo root.

## Data Expectations

The table must include:

- `account_name` (text)
- `sale_date` (date)
- `amount` (numeric)

The UI filters derive:

- Months from `sale_date` (month-level)
- Accounts from `account_name`

## Streaming Behavior

AI summaries stream token-by-token from the Databricks serving endpoint. The overall summary is cached in session state and does not re-run when filters change.

## Troubleshooting

- **Empty filters / table**: Check Postgres token validity.
- **Summary unavailable**: Verify serving endpoint permissions and token.
- **Slow summaries**: Large prompts can take time; streaming keeps UI responsive.

## Security Notes

- Do not commit tokens in `config/env.local`.
- Use Databricks App secrets or environment variable configuration in production.
