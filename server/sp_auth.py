"""
SP authentication helpers for per-user Row-Level Security routing.

Maps sp_client_id → OAuth token using client-credentials flow so that
each user's requests run as their assigned service principal, letting
Unity Catalog GRANT-based permissions enforce RLS automatically.
"""
import os
from databricks.sdk import WorkspaceClient
from .config import get_workspace_host

# Map sp_client_id → name of the env var that holds its secret
_SP_SECRETS: dict[str, str] = {
    os.environ.get("SP_ANALYST_CLIENT_ID", ""): "SP_ANALYST_CLIENT_SECRET",
    os.environ.get("SP_MANAGER_CLIENT_ID", ""): "SP_MANAGER_CLIENT_SECRET",
}


def get_token_for_sp(sp_client_id: str) -> str:
    """Return a fresh OAuth token for the given SP using client credentials."""
    secret_env = _SP_SECRETS.get(sp_client_id)
    if not secret_env:
        raise ValueError(f"Unknown SP client_id: {sp_client_id}")
    client_secret = os.environ.get(secret_env)
    if not client_secret:
        raise RuntimeError(f"Missing env var {secret_env}")
    host = get_workspace_host()
    w = WorkspaceClient(host=host, client_id=sp_client_id, client_secret=client_secret)
    auth_headers = w.config.authenticate()
    if auth_headers and "Authorization" in auth_headers:
        return auth_headers["Authorization"].replace("Bearer ", "")
    raise RuntimeError(f"Could not authenticate SP {sp_client_id}")
