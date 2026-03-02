"""
Session management routes.
Handles creating, listing, and retrieving chat sessions from Lakebase.
Also provides admin endpoints for managing user→SP mappings (RLS routing).
"""
import uuid
from datetime import datetime
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from ..db import db

router = APIRouter(prefix="/sessions", tags=["sessions"])


class CreateSessionRequest(BaseModel):
    title: str = "New Chat"
    user_id: str = "anonymous"


class SessionResponse(BaseModel):
    session_id: str
    user_id: str = "anonymous"
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int = 0


# In-memory fallback for demo mode
_demo_sessions: dict[str, dict] = {}
_demo_messages: dict[str, list] = {}


@router.post("", response_model=SessionResponse)
async def create_session(req: CreateSessionRequest):
    """Create a new chat session."""
    pool = await db.get_pool()

    if db.demo_mode or pool is None:
        session_id = str(uuid.uuid4())
        now = datetime.utcnow()
        _demo_sessions[session_id] = {
            "session_id": session_id,
            "user_id": req.user_id,
            "title": req.title,
            "created_at": now,
            "updated_at": now,
            "message_count": 0,
        }
        _demo_messages[session_id] = []
        return SessionResponse(**_demo_sessions[session_id])

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO chat_sessions (title, user_id)
            VALUES ($1, $2)
            RETURNING session_id, user_id, title, created_at, updated_at
            """,
            req.title,
            req.user_id,
        )
        return SessionResponse(
            session_id=str(row["session_id"]),
            user_id=row["user_id"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            message_count=0,
        )


@router.get("", response_model=list[SessionResponse])
async def list_sessions():
    """List all sessions, ordered by most recently updated."""
    pool = await db.get_pool()

    if db.demo_mode or pool is None:
        sessions = sorted(_demo_sessions.values(), key=lambda s: s["updated_at"], reverse=True)
        return [SessionResponse(**s) for s in sessions]

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                s.session_id,
                s.user_id,
                s.title,
                s.created_at,
                s.updated_at,
                COUNT(m.message_id) as message_count
            FROM chat_sessions s
            LEFT JOIN chat_messages m ON s.session_id = m.session_id
            GROUP BY s.session_id, s.user_id, s.title, s.created_at, s.updated_at
            ORDER BY s.updated_at DESC
            LIMIT 100
            """,
        )
        return [
            SessionResponse(
                session_id=str(r["session_id"]),
                user_id=r["user_id"],
                title=r["title"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                message_count=r["message_count"],
            )
            for r in rows
        ]


@router.get("/{session_id}/messages")
async def get_session_messages(session_id: str):
    """Get all messages for a specific session."""
    pool = await db.get_pool()

    if db.demo_mode or pool is None:
        messages = _demo_messages.get(session_id, [])
        return {"session_id": session_id, "messages": messages}

    async with pool.acquire() as conn:
        # Verify session exists
        session = await conn.fetchrow(
            "SELECT session_id, title FROM chat_sessions WHERE session_id = $1",
            uuid.UUID(session_id),
        )
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        rows = await conn.fetch(
            """
            SELECT message_id, session_id, role, content, genie_response, summary, created_at
            FROM chat_messages
            WHERE session_id = $1
            ORDER BY created_at ASC
            """,
            uuid.UUID(session_id),
        )
        messages = [
            {
                "message_id": str(r["message_id"]),
                "session_id": str(r["session_id"]),
                "role": r["role"],
                "content": r["content"],
                "genie_response": r["genie_response"],
                "summary": r["summary"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]
        return {
            "session_id": session_id,
            "title": session["title"],
            "messages": messages,
        }


@router.patch("/{session_id}")
async def update_session_title(session_id: str, req: CreateSessionRequest):
    """Update a session's title."""
    pool = await db.get_pool()

    if db.demo_mode or pool is None:
        if session_id in _demo_sessions:
            _demo_sessions[session_id]["title"] = req.title
            return {"session_id": session_id, "title": req.title}
        raise HTTPException(status_code=404, detail="Session not found")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE chat_sessions SET title = $1 WHERE session_id = $2 RETURNING session_id, title",
            req.title,
            uuid.UUID(session_id),
        )
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")
        return {"session_id": str(row["session_id"]), "title": row["title"]}


@router.delete("/{session_id}")
async def delete_session(session_id: str):
    """Delete a session and all its messages."""
    pool = await db.get_pool()

    if db.demo_mode or pool is None:
        _demo_sessions.pop(session_id, None)
        _demo_messages.pop(session_id, None)
        return {"deleted": session_id}

    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM chat_sessions WHERE session_id = $1",
            uuid.UUID(session_id),
        )
        return {"deleted": session_id}


# Expose demo storage for chat route
def get_demo_storage():
    return _demo_sessions, _demo_messages


# ---------------------------------------------------------------------------
# Admin router — SP mapping management (user → service principal)
# ---------------------------------------------------------------------------

admin_router = APIRouter(prefix="/admin", tags=["admin"])


class SpMappingRequest(BaseModel):
    user_email: str
    sp_client_id: str
    role_name: str = "analyst"


class SpMappingResponse(BaseModel):
    user_email: str
    sp_client_id: str
    role_name: str
    created_at: datetime


@admin_router.get("/sp-mappings", response_model=list[SpMappingResponse])
async def list_sp_mappings():
    """List all user→SP mappings."""
    pool = await db.get_pool()
    if db.demo_mode or pool is None:
        raise HTTPException(status_code=503, detail="Database unavailable in demo mode")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_email, sp_client_id, role_name, created_at FROM user_sp_mapping ORDER BY user_email"
        )
        return [SpMappingResponse(**dict(r)) for r in rows]


@admin_router.post("/sp-mappings", response_model=SpMappingResponse, status_code=201)
async def upsert_sp_mapping(req: SpMappingRequest):
    """Create or update a user→SP mapping."""
    pool = await db.get_pool()
    if db.demo_mode or pool is None:
        raise HTTPException(status_code=503, detail="Database unavailable in demo mode")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO user_sp_mapping (user_email, sp_client_id, role_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_email) DO UPDATE
                SET sp_client_id = EXCLUDED.sp_client_id,
                    role_name    = EXCLUDED.role_name
            RETURNING user_email, sp_client_id, role_name, created_at
            """,
            req.user_email, req.sp_client_id, req.role_name,
        )
        return SpMappingResponse(**dict(row))


@admin_router.delete("/sp-mappings/{email}", status_code=200)
async def delete_sp_mapping(email: str):
    """Remove a user→SP mapping (user will fall back to app SP)."""
    pool = await db.get_pool()
    if db.demo_mode or pool is None:
        raise HTTPException(status_code=503, detail="Database unavailable in demo mode")
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM user_sp_mapping WHERE user_email = $1", email
        )
        deleted = int(result.split()[-1])  # "DELETE N"
        if deleted == 0:
            raise HTTPException(status_code=404, detail="Mapping not found")
        return {"deleted": email}
