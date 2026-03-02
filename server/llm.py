import os
import re
from openai import AsyncOpenAI
from .config import get_oauth_token, get_workspace_host, SERVING_ENDPOINT

MAS_ENDPOINT = "mas-ea793b7b-endpoint"


def get_llm_client(user_token: str | None = None) -> AsyncOpenAI:
    """Get OpenAI-compatible client for Databricks Foundation Models.

    If user_token is provided it is used directly (SP-routing for RLS);
    otherwise falls back to the app's own SP token.
    """
    host = get_workspace_host()
    token = user_token or get_oauth_token()
    return AsyncOpenAI(
        api_key=token,
        base_url=f"{host}/serving-endpoints",
    )


async def chat_completion(messages: list[dict], model: str | None = None) -> str:
    """Get chat completion from Foundation Model API."""
    client = get_llm_client()
    endpoint = model or SERVING_ENDPOINT
    response = await client.chat.completions.create(
        model=endpoint,
        messages=messages,
        max_tokens=2048,
        temperature=0.3,
    )
    return response.choices[0].message.content


async def call_mas_agent(messages: list[dict]) -> str:
    """Call the Databricks Multi-Agent Supervisor endpoint via the Responses API.
    Returns only the final assistant message, skipping reasoning/tool-call outputs.
    """
    client = get_llm_client()
    response = await client.responses.create(
        model=MAS_ENDPOINT,
        input=messages,
    )
    # Return the last text output item (the final answer)
    last_text = ""
    for output in response.output:
        if (getattr(output, "type", "") == "message"
                and getattr(output, "role", "") == "assistant"):
            for content in getattr(output, "content", []):
                text = getattr(content, "text", "")
                if text:
                    last_text = text
    return last_text.strip()


async def stream_mas_agent(messages: list[dict], user_token: str | None = None):
    """Stream MAS response, yielding only the final answer token by token.

    The MAS produces multiple output items in sequence:
      1. A text item — the supervisor's thinking/planning text
         (e.g. "I'll query the system…"). Followed by response.output_item.done.
      2. Tool-call items — Genie SQL, function calls, etc. Each ends with
         response.output_item.done.
      3. A final text item — the actual answer, streamed token by token.

    Strategy: buffer all text deltas until the first response.output_item.done
    fires (marking the end of the thinking item), then stream every subsequent
    text delta immediately. For direct responses with no tool calls (no done
    event), emit the full buffer at the end.
    """
    client = get_llm_client(user_token=user_token)
    pre_done_buffer = ""  # Accumulates thinking text; discarded on first done
    seen_done = False

    try:
        stream = await client.responses.create(
            model=MAS_ENDPOINT,
            input=messages,
            stream=True,
        )
        async for event in stream:
            etype = getattr(event, "type", "")

            if etype == "response.output_text.delta":
                chunk = getattr(event, "delta", "")
                if not chunk:
                    continue
                if seen_done:
                    yield chunk              # Final answer — stream immediately
                else:
                    pre_done_buffer += chunk  # Thinking text — buffer, discard later

            elif etype == "response.output_item.done":
                if not seen_done:
                    pre_done_buffer = ""  # Discard the thinking/planning text
                    seen_done = True
                # Subsequent done events (tool calls): just keep streaming

        # No done event fired → direct response with no tool calls; emit buffer
        if not seen_done and pre_done_buffer:
            yield pre_done_buffer.strip()

    except Exception as e:
        yield f"\n\n[Agent error: {e}]"
