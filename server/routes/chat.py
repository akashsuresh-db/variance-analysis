"""
Chat route — streams response from MAS via Server-Sent Events.
Saves question + final answer to Lakebase, tagged with user_id.
"""
import uuid
import json
from datetime import datetime
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from ..db import db
from ..llm import stream_mas_agent
from .sessions import get_demo_storage

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    session_id: str
    message: str


@router.post("")
async def send_message(req: ChatRequest, request: Request):
    """
    Stream a response from the Multi-Agent Supervisor via SSE.

    SSE event types:
      data: {"type": "chunk",  "text": "..."}         — token chunk
      data: {"type": "done",   "message_id": "..."}   — stream complete
      data: {"type": "error",  "message": "..."}       — failure
    """
    user_id = request.headers.get("X-Forwarded-Email", "anonymous")

    pool = await db.get_pool()
    demo_mode = db.demo_mode or pool is None

    # Resolve SP token for this user (falls back to app SP if unmapped or demo mode)
    sp_token: str | None = None
    if not demo_mode:
        from ..sp_auth import get_token_for_sp
        from ..db import get_sp_for_user
        sp_client_id = await get_sp_for_user(pool, user_id)
        if sp_client_id:
            sp_token = get_token_for_sp(sp_client_id)
    _demo_sessions, _demo_messages = get_demo_storage()

    # Validate session
    if not demo_mode:
        async with pool.acquire() as conn:
            session = await conn.fetchrow(
                "SELECT session_id FROM chat_sessions WHERE session_id = $1",
                uuid.UUID(req.session_id),
            )
            if not session:
                async def _err():
                    yield f'data: {json.dumps({"type": "error", "message": "Session not found"})}\n\n'
                return StreamingResponse(_err(), media_type="text/event-stream")
    elif req.session_id not in _demo_sessions:
        async def _err():
            yield f'data: {json.dumps({"type": "error", "message": "Session not found"})}\n\n'
        return StreamingResponse(_err(), media_type="text/event-stream")

    # Fetch full conversation history for this session (all messages, oldest first)
    history: list[dict] = []
    if not demo_mode:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role, content FROM chat_messages WHERE session_id = $1 ORDER BY created_at ASC",
                uuid.UUID(req.session_id),
            )
            history = [{"role": r["role"], "content": r["content"]} for r in rows]
    else:
        history = [{"role": m["role"], "content": m["content"]}
                   for m in _demo_messages.get(req.session_id, [])]

    messages = history + [{"role": "user", "content": req.message}]

    # Save user message
    user_msg_id = str(uuid.uuid4())
    if not demo_mode:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO chat_messages (message_id, session_id, role, content) VALUES ($1, $2, 'user', $3)",
                uuid.UUID(user_msg_id), uuid.UUID(req.session_id), req.message,
            )
    else:
        _demo_messages.setdefault(req.session_id, []).append({
            "message_id": user_msg_id, "session_id": req.session_id,
            "role": "user", "content": req.message,
            "created_at": datetime.utcnow().isoformat(),
        })

    async def generate():
        full_response = ""
        assistant_msg_id = str(uuid.uuid4())

        try:
            async for chunk in stream_mas_agent(messages, user_token=sp_token):
                full_response += chunk
                yield f"data: {json.dumps({'type': 'chunk', 'text': chunk})}\n\n"

            if not full_response.strip():
                full_response = "I could not generate a response. Please try again."
                yield f"data: {json.dumps({'type': 'chunk', 'text': full_response})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
            return

        # Save assistant response + tag session with user_id
        if not demo_mode:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO chat_messages (message_id, session_id, role, content) VALUES ($1, $2, 'assistant', $3)",
                    uuid.UUID(assistant_msg_id), uuid.UUID(req.session_id), full_response,
                )
                await conn.execute(
                    """
                    UPDATE chat_sessions
                    SET
                        user_id = CASE WHEN user_id = 'anonymous' THEN $1 ELSE user_id END,
                        title   = CASE WHEN title = 'New Chat' THEN LEFT($2, 60) ELSE title END
                    WHERE session_id = $3
                    """,
                    user_id, req.message, uuid.UUID(req.session_id),
                )
        else:
            _demo_messages.setdefault(req.session_id, []).append({
                "message_id": assistant_msg_id, "session_id": req.session_id,
                "role": "assistant", "content": full_response,
                "created_at": datetime.utcnow().isoformat(),
            })
            if req.session_id in _demo_sessions:
                _demo_sessions[req.session_id]["updated_at"] = datetime.utcnow()
                if _demo_sessions[req.session_id]["title"] == "New Chat":
                    _demo_sessions[req.session_id]["title"] = req.message[:50]

        yield f"data: {json.dumps({'type': 'done', 'message_id': assistant_msg_id, 'session_id': req.session_id})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
