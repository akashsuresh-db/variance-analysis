"""
Multi-Agent Supervisor for AgentBricks Finance Assistant.

Architecture:
  SupervisorAgent
      ├── GenieAgent   - Queries the Finance Genie space for data-driven answers
      └── SummaryAgent - Synthesizes Genie data into clear financial insights

Flow:
  User query → Supervisor decides routing
             → GenieAgent fetches data from Genie space
             → SummaryAgent creates executive summary
             → Combined response returned
"""
import json
from dataclasses import dataclass, field
from typing import Optional
from .genie import GenieClient
from .llm import chat_completion
from .config import GENIE_SPACE_ID


@dataclass
class AgentState:
    """Shared state flowing through the agent pipeline."""
    user_query: str
    conversation_id: Optional[str] = None  # Genie conversation ID for session continuity
    genie_response: Optional[dict] = None
    summary: Optional[str] = None
    final_answer: Optional[str] = None
    agent_trace: list[str] = field(default_factory=list)


class SupervisorAgent:
    """
    Orchestrates the multi-agent pipeline:
    1. Analyzes user query
    2. Dispatches to GenieAgent for data retrieval
    3. Dispatches to SummaryAgent for synthesis
    4. Returns structured response
    """

    def __init__(self):
        self.genie_agent = GenieAgent()
        self.summary_agent = SummaryAgent()

    async def run(self, user_query: str, conversation_id: Optional[str] = None) -> AgentState:
        state = AgentState(user_query=user_query, conversation_id=conversation_id)
        state.agent_trace.append(f"[Supervisor] Received query: {user_query[:100]}")

        # Step 1: Route decision - is this a data/analytics question?
        route = await self._decide_route(state)
        state.agent_trace.append(f"[Supervisor] Routing decision: {route}")

        if route == "genie":
            # Step 2: Query Genie for financial data
            state = await self.genie_agent.run(state)
            # Step 3: Summarize the Genie response
            state = await self.summary_agent.run(state)
        elif route == "direct":
            # For general/greeting queries, answer directly
            state = await self._direct_answer(state)
        else:
            # Default: always try Genie first for a finance assistant
            state = await self.genie_agent.run(state)
            state = await self.summary_agent.run(state)

        # Step 4: Build final answer
        state.final_answer = state.summary or state.final_answer
        state.agent_trace.append("[Supervisor] Pipeline complete")
        return state

    async def _decide_route(self, state: AgentState) -> str:
        """Use LLM to decide if query needs Genie data or can be answered directly."""
        system = (
            "You are a routing agent for a Finance Analytics Assistant. "
            "Decide if the user query needs financial data from the database (route: 'genie') "
            "or can be answered as a general greeting/question (route: 'direct'). "
            "Almost all financial, analytical, or data questions should go to 'genie'. "
            "Only use 'direct' for pure greetings like 'hello', 'thanks', or 'what can you do?'. "
            "Respond with ONLY one word: 'genie' or 'direct'."
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": state.user_query},
        ]
        try:
            decision = await chat_completion(messages)
            decision = decision.strip().lower()
            return "genie" if "genie" in decision else ("direct" if "direct" in decision else "genie")
        except Exception:
            return "genie"  # Default to Genie for finance app

    async def _direct_answer(self, state: AgentState) -> AgentState:
        """Handle non-data queries directly."""
        system = (
            "You are AgentBricks Finance Assistant, an AI-powered analytics tool. "
            "You help users analyze insurance claims, policies, premiums, and fraud indicators. "
            "Respond helpfully and concisely."
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": state.user_query},
        ]
        try:
            answer = await chat_completion(messages)
            state.final_answer = answer
            state.agent_trace.append("[Direct] Answered without Genie")
        except Exception as e:
            state.final_answer = f"I'm your Finance Analytics Assistant. How can I help you analyze financial data today?"
        return state


class GenieAgent:
    """
    Agent that queries the Databricks Genie space for financial data.
    Maintains conversation continuity within a chat session.
    """

    def __init__(self):
        self.client = GenieClient(space_id=GENIE_SPACE_ID)

    async def run(self, state: AgentState) -> AgentState:
        state.agent_trace.append(f"[GenieAgent] Querying Genie space: {GENIE_SPACE_ID}")
        try:
            result = await self.client.query(
                question=state.user_query,
                conversation_id=state.conversation_id,
            )
            state.genie_response = result
            state.conversation_id = result.get("conversation_id")
            status = result.get("status", "")
            state.agent_trace.append(f"[GenieAgent] Status: {status}, Answer length: {len(result.get('answer', ''))}")
        except Exception as e:
            state.genie_response = {
                "answer": f"Unable to query financial data: {str(e)}",
                "sql": None,
                "data": None,
                "status": "ERROR",
            }
            state.agent_trace.append(f"[GenieAgent] Error: {e}")
        return state


class SummaryAgent:
    """
    Agent that synthesizes Genie data into a clear executive summary.
    Formats the response for the end user with key insights.
    """

    async def run(self, state: AgentState) -> AgentState:
        state.agent_trace.append("[SummaryAgent] Generating financial summary")

        genie = state.genie_response or {}
        answer = genie.get("answer", "")
        sql = genie.get("sql", "")
        data = genie.get("data", [])
        status = genie.get("status", "")

        if status == "ERROR" or not answer:
            state.summary = answer or "Unable to retrieve financial data at this time."
            return state

        # Build context for the summary
        context_parts = [f"User asked: {state.user_query}"]

        if answer:
            context_parts.append(f"Genie's data analysis: {answer}")

        if sql:
            context_parts.append(f"SQL used: {sql}")

        if data and len(data) > 0:
            # Format sample data rows
            data_preview = json.dumps(data[:5], indent=2)
            context_parts.append(f"Sample data rows (first 5): {data_preview}")

        context = "\n\n".join(context_parts)

        system = (
            "You are a financial analytics expert creating executive summaries. "
            "Given data from a financial database query, create a clear, concise summary. "
            "Structure your response as:\n"
            "1. **Key Finding**: One sentence answer\n"
            "2. **Details**: Bullet points with specific numbers/metrics\n"
            "3. **Insight**: One actionable insight or observation\n\n"
            "Be precise with numbers. Use financial terminology appropriately. "
            "Keep it under 200 words unless data complexity requires more detail."
        )

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": context},
        ]

        try:
            state.summary = await chat_completion(messages)
            state.agent_trace.append(f"[SummaryAgent] Summary generated ({len(state.summary)} chars)")
        except Exception as e:
            # Fallback to raw Genie answer
            state.summary = answer
            state.agent_trace.append(f"[SummaryAgent] Fallback to raw answer: {e}")

        return state


# Singleton supervisor
supervisor = SupervisorAgent()
