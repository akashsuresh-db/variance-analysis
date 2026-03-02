"""
Genie Space API client for querying the Finance Genie space.
Uses the Databricks Genie conversational analytics API.
"""
import asyncio
import aiohttp
from typing import Optional
from .config import get_oauth_token, get_workspace_host, GENIE_SPACE_ID


class GenieClient:
    """Client for Databricks Genie Space API."""

    def __init__(self, space_id: str | None = None):
        self.space_id = space_id or GENIE_SPACE_ID
        self._genie_conversation_id: Optional[str] = None

    def _get_headers(self) -> dict:
        token = get_oauth_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _base_url(self) -> str:
        host = get_workspace_host()
        return f"{host}/api/2.0/genie/spaces/{self.space_id}"

    async def start_conversation(self, question: str) -> dict:
        """Start a new Genie conversation with a question."""
        url = f"{self._base_url()}/start-conversation"
        payload = {"content": question}

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=self._get_headers()) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise RuntimeError(f"Genie start-conversation failed [{resp.status}]: {text}")
                data = await resp.json()
                return data

    async def create_message(self, conversation_id: str, question: str) -> dict:
        """Add a message to an existing Genie conversation."""
        url = f"{self._base_url()}/conversations/{conversation_id}/messages"
        payload = {"content": question}

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=self._get_headers()) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise RuntimeError(f"Genie create-message failed [{resp.status}]: {text}")
                data = await resp.json()
                return data

    async def poll_message(self, conversation_id: str, message_id: str, max_wait: int = 60) -> dict:
        """Poll until Genie message is complete."""
        url = f"{self._base_url()}/conversations/{conversation_id}/messages/{message_id}"
        elapsed = 0
        poll_interval = 2

        async with aiohttp.ClientSession() as session:
            while elapsed < max_wait:
                async with session.get(url, headers=self._get_headers()) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        raise RuntimeError(f"Genie poll failed [{resp.status}]: {text}")
                    data = await resp.json()
                    status = data.get("status", "")
                    if status in ("COMPLETED", "FAILED", "CANCELLED"):
                        return data
                    await asyncio.sleep(poll_interval)
                    elapsed += poll_interval

        raise TimeoutError(f"Genie message timed out after {max_wait}s")

    async def get_query_result(self, conversation_id: str, message_id: str) -> dict:
        """Get the SQL query result from a completed Genie message."""
        url = f"{self._base_url()}/conversations/{conversation_id}/messages/{message_id}/query-result"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self._get_headers()) as resp:
                if resp.status == 200:
                    return await resp.json()
                return {}

    async def query(self, question: str, conversation_id: str | None = None) -> dict:
        """
        Ask Genie a question and return the full response.
        Returns dict with: conversation_id, message_id, answer, sql, data
        """
        try:
            if conversation_id:
                msg_resp = await self.create_message(conversation_id, question)
                conv_id = conversation_id
            else:
                start_resp = await self.start_conversation(question)
                conv_id = start_resp.get("conversation_id") or start_resp.get("id")
                msg_resp = start_resp.get("message", start_resp)

            message_id = msg_resp.get("message_id") or msg_resp.get("id")
            if not message_id:
                raise RuntimeError(f"No message_id in Genie response: {msg_resp}")

            # Poll for completion
            completed = await self.poll_message(conv_id, message_id)
            status = completed.get("status")

            if status == "FAILED":
                error = completed.get("error", "Unknown error")
                return {
                    "conversation_id": conv_id,
                    "message_id": message_id,
                    "answer": f"Genie could not answer this question: {error}",
                    "sql": None,
                    "data": None,
                    "status": "FAILED",
                }

            # Extract text answer
            answer = ""
            attachments = completed.get("attachments", [])
            for att in attachments:
                if att.get("type") == "text" or "text" in att:
                    answer = att.get("text", {}).get("content", "") or att.get("content", "")
                    break

            if not answer:
                answer = completed.get("content", completed.get("message", ""))

            # Get query result (SQL + data)
            query_result = await self.get_query_result(conv_id, message_id)
            sql = None
            data_rows = []

            if query_result:
                statement = query_result.get("statement_response", {})
                sql = statement.get("statement", "")
                result = statement.get("result", {})
                if result:
                    data_rows = result.get("data_array", [])[:20]  # Limit to 20 rows

            return {
                "conversation_id": conv_id,
                "message_id": message_id,
                "answer": answer,
                "sql": sql,
                "data": data_rows,
                "status": "COMPLETED",
                "raw": completed,
            }

        except Exception as e:
            return {
                "conversation_id": conversation_id,
                "message_id": None,
                "answer": f"Genie query error: {str(e)}",
                "sql": None,
                "data": None,
                "status": "ERROR",
            }


# Singleton client
genie_client = GenieClient()
