"""
BOQ Chat Module - Agent-based conversation system for Bill of Quantities.

This module provides an AI agent-based chat system for generating BOQ through
natural conversation. The agent can:
- Analyze PDF documents
- Extract requirements
- Search pricing information
- Generate formatted BOQ tables

Usage:
    from app.ai.boq import run_boq_agent
    response = run_boq_agent(message, session_id, project_context)
"""

import re
from typing import Optional

# Guardrail: the prompt requires Step 4 replies to open with a "## FINAL BOQ"
# heading followed by the complete table. The heading is the machine-checkable
# half of that contract — if it appears without table rows, the compilation
# went wrong and the adapter (projects.ai_pipeline._extract_markdown_boq) would
# find no table and drop the version as still-pending. One corrective turn
# fixes it; no retry loop.
# Not line-anchored: in live streams the model glues the heading to the end of
# its prose ("...recalculating the full BOQ.## FINAL BOQ"). The '#' still
# separates it from prose mentions like "the final BOQ table".
_FINAL_HEADING = re.compile(r"#{1,6}\s*final\s+boq\b", re.IGNORECASE)
_TABLE_SEPARATOR = re.compile(r"^[\|\s\-:]+$")

_FIX_MISSING_TABLE = (
    "GUARDRAIL NOTICE: Your previous reply announced the FINAL BOQ but did not "
    "include the BOQ table itself. Output the complete final BOQ now: the "
    "'## FINAL BOQ' heading, then the full markdown table with every line item "
    "(Item No. | Description | Unit | Quantity | Rate | Amount | Remarks) and "
    "the subtotal/total rows from your last calculate_boq_totals result. "
    "Do not apologize or explain — output the table."
)


def _table_row_count(text: str) -> int:
    """Markdown table rows in the text (separator rows excluded)."""
    return sum(
        1
        for line in text.splitlines()
        if line.strip().count("|") >= 2 and not _TABLE_SEPARATOR.match(line.strip())
    )


def final_table_missing(text: str) -> bool:
    """True when the reply declares a final BOQ but carries no table.

    Anchored to the heading, not the phrase — prose like "shall I add VAT to
    the final BOQ?" must not trigger a retry. A header plus at least one data
    row counts as a table.
    """
    return bool(_FINAL_HEADING.search(text or "")) and _table_row_count(text or "") < 2


def clear_boq_session(session_id: str) -> bool:
    """Forget a project's BOQ conversation. See `session_store.clear_session`."""
    from app.ai.boq.session_store import clear_session

    return clear_session(session_id)


def run_boq_agent(message: str, session_id: str, project_context: Optional[str] = None, images=None) -> str:
    """
    Run the BOQ agent with a message and session ID.

    Args:
        message: User message
        session_id: Session ID for conversation context
        project_context: Optional finalized project context from prior workflow steps
        images: Optional list of (bytes, format) tuples fed to the vision model
            (e.g. the approved 3D render)

    Returns:
        Agent response as string
    """
    from app.ai.boq.main_agent import run_main_agent

    response = str(
        run_main_agent(message, session_id, project_context=project_context, images=images)
    )
    if final_table_missing(response):
        # Same session, so the corrective turn continues the conversation.
        response += "\n\n" + str(run_main_agent(_FIX_MISSING_TABLE, session_id))
    return response


async def stream_boq_agent(message: str, session_id: str, project_context: Optional[str] = None, images=None):
    """
    Stream the BOQ agent response with a message and session ID.

    Args:
        message: User message
        session_id: Session ID for conversation context
        project_context: Optional finalized project context from prior workflow steps
        images: Optional list of (bytes, format) tuples fed to the vision model
            (e.g. the approved 3D render)

    Yields:
        Text chunks (str) and status events (dict) from the agent response
    """
    from app.ai.boq.main_agent import stream_main_agent

    collected: list[str] = []
    async for chunk in stream_main_agent(
        message, session_id, project_context=project_context, images=images
    ):
        if isinstance(chunk, str):
            collected.append(chunk)
        yield chunk

    if final_table_missing("".join(collected)):
        yield "\n\n"
        async for chunk in stream_main_agent(_FIX_MISSING_TABLE, session_id):
            yield chunk


__all__ = ["run_boq_agent", "stream_boq_agent", "final_table_missing"]
