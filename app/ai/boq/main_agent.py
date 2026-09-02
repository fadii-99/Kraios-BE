from app.ai.boq.agents import analyze_requirements, extract_project_materials
from app.ai.boq.calc import calculate, calculate_boq_totals
from app.ai.boq.search_agent import search_material_pricing
from strands_tools import calculator
from strands.models.openai import OpenAIModel
import asyncio
import os
import shutil
import tempfile
from typing import Any, Optional
from strands import Agent
from strands.session.file_session_manager import FileSessionManager
from app.ai.config import ai_settings
from app.ai.llm_call_logging import log_llm_call

from app.ai.boq.session_store import session_directory


def _session_manager(session_id: str) -> FileSessionManager:
    """Build a FileSessionManager, first repairing a half-created session dir.

    If a prior request was interrupted after strands made the session folders
    but before it wrote session.json, read_session() returns None yet
    create_session() sees the dir and raises "Session <id> already exists",
    wedging that project's BOQ chat forever. Deleting the empty dir lets strands
    recreate it cleanly. Nothing is lost — a session with no session.json holds
    no persisted messages, and the UI history lives in the DB separately.
    """
    sdir = session_directory(session_id)
    if os.path.isdir(sdir) and not os.path.exists(os.path.join(sdir, "session.json")):
        shutil.rmtree(sdir, ignore_errors=True)
    return FileSessionManager(session_id=session_id)



from strands import Agent


main_prompt = """
You are a senior construction project manager and BOQ (Bill of Quantities) specialist. Your primary responsibility is to create comprehensive, accurate BOQs from project documentation and handle all BOQ-related modifications requested by users.

## CORE RESPONSIBILITIES

### 0. FINALIZED PROJECT CONTEXT
When a message includes a "PROJECT FINALIZED CONTEXT" section, treat it as the source of truth for the current project.
- Use the finalized/current 2D floor plan, approved/current 3D render, latest room summaries, and uploaded BOQ documents from that context.
- Do not ask the user to re-upload plans or re-enter details that are already present in the context.
- If local file paths are provided, you may use the analysis tools to inspect those files when needed.
- If a 3D render image is attached to the message, visually inspect it to sanity-check the space, materials/finishes, and rough quantities before compiling.
- If a finalized 3D render is missing, continue from the available 2D plan, summaries, and uploaded files, and mention that the 3D reference was unavailable.
- Keep the project context internal; do not paste the entire context back to the user unless they ask for it.

### 0.1 OPTIONAL SUPPLEMENTARY DOCUMENTS
The UPLOADED CONTENT section may include optional supplementary documents, each labeled with a Type:
- "MEP Drawing" — mechanical, electrical, and plumbing drawings (PDF or DWG).
- "HVAC Drawing" — heating, ventilation, and air conditioning drawings (PDF or DWG).
- "Door & Window Schedule" — door/window schedules (Excel or PDF), typically listing mark, type, size, material, and count.
Rules for these documents:
- They are OPTIONAL. If none are provided, generate the BOQ from the remaining context as usual and never block or ask for them.
- When provided, you MUST incorporate them into the BOQ: extract MEP items (wiring, conduits, piping, sanitary fixtures, distribution boards, etc.), HVAC items (units, ducting, grilles, insulation, controls, etc.), and door/window items (per the schedule's marks, sizes, and counts) alongside the civil/architectural items.
- In the final BOQ table (Step 4), group these under clearly labeled sections, e.g. "MEP Works", "HVAC Works", "Doors & Windows".
- DXF drawings ARE readable. Their block counts, layers, dimensions and text labels appear
  in the context. Block counts are takeoff data — "DOOR_900 x12" means 12 of that door, so
  use it as the quantity. Layer names indicate discipline (E-* electrical, M-* mechanical).
- DWG files cannot be read. Use their stated type and filename as context, state any
  assumptions you make, ask for specifications you cannot infer, and mention that exporting
  the drawing as DXF would let you read it directly.
- If a Door & Window Schedule is provided, its marks, sizes, and counts override quantities inferred from the floor plan.

### 0.2 ARITHMETIC RULE — NEVER CALCULATE IN YOUR HEAD
Every number you report must come from a tool. Pick in this order:
1. calculate_boq_totals — the whole BOQ table: line amounts, subtotal, overhead,
   contingency, profit margin, VAT, grand total. Always use this for the BOQ itself.
2. calculate — a single add / subtract / multiply / divide over a list of numbers
   (e.g. subtracting a discount, scaling a quantity, splitting a cost). Use its
   "result_rounded_2dp" for anything you present as money.
3. calculator — only for expressions the above cannot express, such as parentheses,
   powers or roots. Note it rejects numbers containing commas, so strip them first.
State results exactly as the tool returned them. Never adjust, re-round, or "check"
a tool's number by redoing it yourself.

### 1. BOQ GENERATION WORKFLOW
When asked to create a BOQ from project documentation, follow this exact sequence:

## HARD APPROVAL GATE
The BOQ workflow is multi-turn. You MUST complete only one numbered step per assistant response.

Approval rule:
- After Step 1, show the requirements output and stop. Ask: "Please approve Step 1 so I can extract the materials."
- Do not run Step 2 until the user explicitly approves or asks to continue.
- After Step 2, show the materials output and stop. Then ask the PRICING PREFERENCE QUESTION (see Step 3) and wait for the user's answer.
- Do not run Step 3 until the user has answered the pricing preference question and explicitly approves or asks to continue.
- After Step 3, show the pricing output and stop. Ask: "Please approve Step 3 so I can compile the BOQ table."
- Do not run Step 4 until the user explicitly approves or asks to continue.
- After Step 4, show the BOQ table and ask the user to approve/finalize it.

Tool-use rule:
- In one assistant response, call at most one BOQ workflow tool.
- calculate_boq_totals, calculate and calculator are NOT workflow tools. They do not advance
  a step and are exempt from this limit — call them whenever you need a number, in any step.
- For Step 1, the only workflow tool you may call is analyze_requirements.
- For Step 2, the only workflow tool you may call is extract_project_materials.
- For Step 3, you may call search_material_pricing ONLY in Branch A (user chose web/real-time pricing). In Branch B (user will input prices themselves), do not call any workflow tool.
- For Step 4, do not call a workflow tool; compile the BOQ from prior requirements, materials, and pricing already in the conversation.
- If the user asks to "do everything", still perform only the next pending step and ask for approval.

State rule:
- Determine the next pending step from the conversation history.
- If there is no prior BOQ step output in the conversation, start with Step 1 only.
- Treat user messages such as "yes", "approved", "continue", "proceed", or "next" as approval to perform only the next pending step.

**Step 1: Requirements Analysis**
- Use the analyze_requirements to analyze project documents (PDFs, images, etc.) by asking the analyze _requirements to use the name of the pdf file. 
- Extract structured requirements including scope, specifications, and measurements
- Identify any missing critical information

**Step 2: Materials Extraction**
- Use the extract_project_materials to extract materials list with estimated quantities from the requirements
- Ensure all materials have realistic quantities and proper units
- Cross-reference with industry standards for completeness

**Step 3: Pricing Research**

PRICING PREFERENCE QUESTION — before doing ANY pricing work, ask the user this verbatim and wait for their reply:

"Would you like me to compute and complete with a research of real time market prices for the items included, or do you have a preferred supplier — in which case you can input the prices yourself?
If you wish me to compute with prices from the web, should I add any margin or VAT now or later? And if you will input the prices, will you add any profit margin or VAT now or later on?"

From the user's answer, determine TWO things and then follow the matching branch:
1. Pricing source: web/real-time research (Branch A) vs. user-supplied/preferred-supplier prices (Branch B).
2. Margin/VAT timing: apply "now" (during Step 4 compilation) or "later" (defer until the user explicitly asks).

Branch A — Web / real-time pricing:
- Use the search_material_pricing to find current market prices for each material.
- The results will be from both internal pinecone knowledge base and web search.
- Present that output in neat form. For the web search results, include the title, snippet, and URL for each result(where applicable).
- You must always provide urls for web search results.
- Batch all materials into a single search_material_pricing call whenever possible, and keep the search brief and to the point.

Branch B — User-supplied (preferred supplier) prices:
- Do NOT call search_material_pricing or any other workflow tool.
- Present the extracted materials list (with Unit and Quantity) as a table and ask the user to fill in the unit rate for each item.
- Wait for the user's prices before continuing to Step 4.

Margin / VAT handling (both branches):
- If the user said "now", record the requested profit margin % and/or VAT % and apply them transparently as separate lines during Step 4 compilation.
- If the user said "later" (or gave no margin/VAT), do not apply them; proceed and apply only when the user later requests it.

**Step 4: BOQ Compilation**
Once every rate is in place and the user approves the pricing, your NEXT reply IS the
final BOQ — do not ask further questions or defer it.

OUTPUT CONTRACT (enforced by the system — a reply that violates it is rejected and retried):
- Start the compilation with the exact heading: ## FINAL BOQ
- Immediately under the heading, output the complete markdown table — every line item,
  followed by the subtotal, overhead, contingency, profit, VAT and grand-total rows.
- Never output the heading without the full table, and never split the table across replies.
- Use this heading ONLY for the final compiled table, never in ordinary prose.

Create a comprehensive BOQ table with the following columns:
- Item No. | Description | Unit | Quantity | Rate (Unit Price) | Amount | Remarks

MANDATORY — do not compute any of these numbers yourself:
- Call calculate_boq_totals with every line item (description, unit, quantity, rate) plus
  any overhead %, contingency %, profit margin % and VAT % that apply.
- Build the table from the values it returns. Copy them verbatim — do not re-derive,
  re-round, or "tidy" any figure.
- If it returns an "error", fix the field it names and call it again. Never fall back to
  doing the arithmetic in your head.
- If it reports unpriced_items, list those items with a blank rate and say they are unpriced.
- For any other calculation (a percentage, a unit conversion, a quantity build-up), use the
  calculate tool rather than working it out yourself.



### 2. BOQ MODIFICATIONS & USER REQUESTS
Handle the following user requests efficiently:

**Profit Margin Addition:**
- When asked to add profit margin, re-run calculate_boq_totals with the same line items
  and the requested profit_margin_percent (and vat_percent if asked) — never patch the
  totals by hand.
- Clearly show: Subtotal + Overhead + Contingency + Profit = Final Amount
- Explain the profit calculation transparently. Note that profit is taken on
  subtotal + overhead + contingency, and VAT on the net total.

**BOQ Revisions:**
- Modify quantities, add/remove items, update prices as requested
- Re-run calculate_boq_totals with the full revised item list so every dependent total
  is recalculated. Do not adjust a total by adding or subtracting the delta yourself.
- Highlight what changed from the previous version




Always show the completed step output before asking for approval. Never silently continue into the next step in the same response. Always ensure accuracy and clarity in all calculations and presentations.

"""

# Create a session manager with a unique session ID
# main_model = OpenAIModel(
#     client_args={
#         "api_key": os.getenv("OPENAI_API_KEY"),
#     },
#     model_id="gpt-5-mini",
    
# )
# session_manager = FileSessionManager(session_id="test-session-60924")

# # # Create an agent with the session manager
# # main_agent = Agent(session_manager=session_manager,system_prompt=main_prompt,tools=[analyze_requirements, extract_project_materials, search_material_pricing],model=main_model)
# # # Create an agent with the session manager
# main_agent = Agent(session_manager=session_manager,system_prompt=main_prompt,tools=[analyze_requirements, extract_project_materials, search_material_pricing],model=main_model)

# # Use the agent - all messages and state are automatically persisted
# # print(main_agent(" search For the cement, price per 50kg bag in UAE") )
# # # Use the agent - all messages and state are automatically persisted
# #print(main_agent("yes, please move to the next step") )

# # async def process_streaming_response(query: str):
# #     agent_stream = main_agent.stream_async(query)
# #     async for event in agent_stream:
# #         print(event) 
# # # Run the async function
# # asyncio.run(process_streaming_response("can you please prepare requirement from Rosherville Roof Repairs scope.pdf"))
# # async def process_streaming_response(query: str):
# #     agent_stream = main_agent.stream_async(query)
# #     async for event in agent_stream:
# #         print(event) 
# # # Run the async function
# # asyncio.run(process_streaming_response("Can you please prepare a boq for temp_uploaded.pdf?"))
# main_agent.py
# ... (keep your imports and prompt as they are)

def _message_with_project_context(message: str, project_context: Optional[str]) -> str:
    if not project_context:
        return message

    return f"""PROJECT FINALIZED CONTEXT
Use this context as the authoritative basis for BOQ generation and revisions.

{project_context}

USER REQUEST
{message}
"""


def _build_agent_input(message: str, project_context: Optional[str], images=None):
    """Build the agent input. Plain string when there are no images; otherwise a
    list of content blocks (text + one image block per (bytes, format) tuple) so
    the vision model actually sees the approved 3D render, not just its path.
    """
    text = _message_with_project_context(message, project_context)
    if not images:
        return text
    blocks = [{"text": text}]
    for data, fmt in images:
        blocks.append({"image": {"format": fmt, "source": {"bytes": data}}})
    return blocks


def _tool_status_event(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    tool_stream = event.get("tool_stream_event") if isinstance(event, dict) else None
    if not isinstance(tool_stream, dict):
        return None

    data = tool_stream.get("data")
    if not isinstance(data, dict) or data.get("type") != "pricing_progress":
        return None

    tool_use = tool_stream.get("tool_use") or {}
    return {
        "type": "status",
        "tool": tool_use.get("name", "search_material_pricing"),
        "stage": data.get("stage"),
        "status": data.get("status"),
        "material": data.get("material"),
        "materials": data.get("materials"),
        "sources": data.get("sources", []),
        "result_count": data.get("result_count"),
        "has_internal_data": data.get("has_internal_data"),
        "has_web_data": data.get("has_web_data"),
    }


def run_main_agent(message: str, session_id: str, project_context: Optional[str] = None, images=None):
    # Initialize model
    main_model = OpenAIModel(
        client_args={
            "api_key": ai_settings.OPENROUTER_API_KEY,
            "base_url": ai_settings.OPENROUTER_BASE_URL,
        },
        model_id=ai_settings.OPENROUTER_MODEL_BOQ
    )

    # Dynamically create session manager based on the session_id from API
    session_manager = _session_manager(session_id)

    # Create the agent instance
    agent = Agent(
        session_manager=session_manager,
        system_prompt=main_prompt,
        tools=[analyze_requirements, extract_project_materials, search_material_pricing,
               calculate_boq_totals, calculate, calculator],
        model=main_model
    )

    # Execute the agent call
    with log_llm_call(
        provider="OpenRouter",
        model=ai_settings.OPENROUTER_MODEL_BOQ,
        api=f"{ai_settings.OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
        operation="BOQ main agent",
    ):
        return agent(_build_agent_input(message, project_context, images))


async def stream_main_agent(message: str, session_id: str, project_context: Optional[str] = None, images=None):
    # Initialize model
    main_model = OpenAIModel(
        client_args={
            "api_key": ai_settings.OPENROUTER_API_KEY,
            "base_url": ai_settings.OPENROUTER_BASE_URL,
        },
        model_id=ai_settings.OPENROUTER_MODEL_BOQ
    )

    # Dynamically create session manager based on the session_id from API
    session_manager = _session_manager(session_id)

    # Create the agent instance
    agent = Agent(
        session_manager=session_manager,
        system_prompt=main_prompt,
        tools=[analyze_requirements, extract_project_materials, search_material_pricing,
               calculate_boq_totals, calculate, calculator],
        model=main_model
    )

    with log_llm_call(
        provider="OpenRouter",
        model=ai_settings.OPENROUTER_MODEL_BOQ,
        api=f"{ai_settings.OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
        operation="BOQ streaming main agent",
    ):
        async for event in agent.stream_async(_build_agent_input(message, project_context, images)):
            status_event = _tool_status_event(event)
            if status_event:
                yield status_event
                continue

            chunk = event.get("data") if isinstance(event, dict) else None
            if chunk:
                yield str(chunk)
