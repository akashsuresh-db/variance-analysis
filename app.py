"""
AgentBricks Finance Assistant
FastAPI application entry point.
"""
import asyncio
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from server.db import db
from server.routes.sessions import router as sessions_router, admin_router
from server.routes.chat import router as chat_router

TOKEN_REFRESH_INTERVAL = 45 * 60  # 45 minutes — Lakebase OAuth tokens expire after ~60 min


async def _token_refresh_loop():
    """Background task: refresh Lakebase connection pool token every 45 minutes."""
    await asyncio.sleep(TOKEN_REFRESH_INTERVAL)
    while True:
        try:
            if not db.demo_mode:
                print("Refreshing Lakebase token...")
                await db.refresh_token()
                print("Lakebase token refreshed successfully")
        except Exception as e:
            print(f"Token refresh failed: {e}")
        await asyncio.sleep(TOKEN_REFRESH_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    print("Starting AgentBricks Finance Assistant...")
    await db.initialize()
    print(f"DB mode: {'demo (in-memory)' if db.demo_mode else 'Lakebase (PostgreSQL)'}")
    refresh_task = asyncio.create_task(_token_refresh_loop())
    yield
    refresh_task.cancel()
    await db.close()
    print("Shutting down...")


app = FastAPI(
    title="AgentBricks Finance Assistant",
    description="Multi-agent finance analytics powered by Databricks Genie",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routes
app.include_router(sessions_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
app.include_router(admin_router, prefix="/api")


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "db_mode": "demo" if db.demo_mode else "lakebase",
        "app": "AgentBricks Finance Assistant",
    }


# Serve React SPA
frontend_dist = os.path.join(os.path.dirname(__file__), "frontend", "dist")
if os.path.exists(frontend_dist):
    app.mount(
        "/assets",
        StaticFiles(directory=os.path.join(frontend_dist, "assets")),
        name="assets",
    )

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        return FileResponse(os.path.join(frontend_dist, "index.html"))
