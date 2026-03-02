import os
from databricks.sdk import WorkspaceClient

# Detect environment: Databricks App vs local dev
IS_DATABRICKS_APP = bool(os.environ.get("DATABRICKS_APP_NAME"))

WORKSPACE_HOST = "https://fe-sandbox-akash-finance-app.cloud.databricks.com"
GENIE_SPACE_ID = os.environ.get("GENIE_SPACE_ID", "01f1138320f719fb844d052d96e39383")
SERVING_ENDPOINT = os.environ.get("SERVING_ENDPOINT", "databricks-meta-llama-3-3-70b-instruct")

_workspace_client: WorkspaceClient | None = None


def get_workspace_client() -> WorkspaceClient:
    global _workspace_client
    if _workspace_client is None:
        if IS_DATABRICKS_APP:
            _workspace_client = WorkspaceClient()
        else:
            profile = os.environ.get("DATABRICKS_PROFILE", "fevm-akash-finance-app")
            _workspace_client = WorkspaceClient(profile=profile)
    return _workspace_client


def get_oauth_token() -> str:
    """Get a fresh OAuth token for API calls (Genie, LLM, etc.)."""
    client = get_workspace_client()
    auth_headers = client.config.authenticate()
    if auth_headers and "Authorization" in auth_headers:
        return auth_headers["Authorization"].replace("Bearer ", "")
    raise RuntimeError("Could not get OAuth token")


def get_database_token(instance_name: str = "akash-finance-app") -> str:
    """Get a Lakebase-scoped credential token for PostgreSQL authentication."""
    client = get_workspace_client()
    cred = client.database.generate_database_credential(instance_names=[instance_name])
    if cred and cred.token:
        return cred.token
    raise RuntimeError("Could not generate database credential")


def get_workspace_host() -> str:
    """Get workspace host with https:// prefix."""
    if IS_DATABRICKS_APP:
        host = os.environ.get("DATABRICKS_HOST", WORKSPACE_HOST)
        if host and not host.startswith("http"):
            host = f"https://{host}"
        return host
    return WORKSPACE_HOST
