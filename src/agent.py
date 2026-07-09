"""
src/agent.py  ─  Privacy-Preserving Staffing Agent  (Google ADK)
═══════════════════════════════════════════════════════════════════════════
Deterministic tool-calling agent for the healthcare staffing dashboard.
All spatial reasoning uses H3 hexagonal indexes — no coordinates anywhere.

Architecture:
    •  Three Pydantic-typed Python tools handle all data retrieval
       (lookup_client, find_nearby_staff, filter_staff_by_hours).
    •  Google ADK `Agent` orchestrates tool calls via Gemini.
    •  `InMemoryRunner` provides session persistence so follow-up
       questions ("What about RNs instead?") work automatically.

Privacy:  The secure DB has no Lat/Lng.  Only H3 indexes are used.
"""

from __future__ import annotations

import asyncio
import math
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Optional

import h3
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from google.genai import types

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = str(_PROJECT_ROOT / "data" / "staffing_engine_secure.db")
load_dotenv(str(_PROJECT_ROOT / ".env"))

import os
import opik
from opik import track
from src.logger import logger
os.environ["OPIK_PROJECT_NAME"] = "agentic_healthcare_staffing"


# ═══════════════════════════════════════════════════════════════════════════════
#  PYDANTIC INPUT SCHEMAS
# ═══════════════════════════════════════════════════════════════════════════════

class ClientLookupInput(BaseModel):
    """Input schema for looking up a client by name."""
    client_name: str = Field(description="Full or partial client name, e.g. 'Claire Ferguson'")


class NearbyStaffInput(BaseModel):
    """Input schema for finding staff within a radius of a client."""
    client_name: str = Field(description="Full or partial client name")
    radius_miles: float = Field(default=10.0, description="Search radius in miles (default 10)")
    role: str = Field(default="", description="Role filter: 'PCA', 'LPN', 'RN', or empty for all")


class StaffFilterInput(BaseModel):
    """Input schema for filtering a list of staff by hours/role."""
    staff_ids: list[str] = Field(description="List of Staff_IDs to filter, e.g. ['S001','S054']")
    min_hours: float = Field(default=10.0, description="Minimum available weekly hours (default 10)")
    role: str = Field(default="", description="Role filter: 'PCA', 'LPN', 'RN', or empty for all")


# ═══════════════════════════════════════════════════════════════════════════════
#  DETERMINISTIC PYTHON TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

# Global side-channel to let tools export UI context
AGENT_CONTEXT: dict = {}

@track(ignore_arguments=["client_name"])
def lookup_client(client_name: str) -> dict:
    """Look up a client by name and return their Client_ID, full name, and H3_Index.

    Args:
        client_name: Full or partial client name, e.g. 'Claire Ferguson' or 'Claire'.

    Returns:
        dict with status, client_id, full_name, h3_index on success,
        or status='error' with error_message on failure.
    """
    try:
        logger.debug("Executing lookup_client")
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            'SELECT Client_ID, "First Name", "Last Name", H3_Index '
            "FROM clients "
            "WHERE \"First Name\" || ' ' || \"Last Name\" LIKE ?",
            (f"%{client_name}%",),
        ).fetchone()
        conn.close()

        if not row:
            logger.warning("lookup_client found no matching client")
            return {"status": "error",
                    "error_message": "No client found."}

        logger.info(f"lookup_client returning Client_ID={row[0]}")
        return {
            "status": "success",
            "client_id": row[0],
            "h3_index": row[3],
        }
    except Exception as exc:
        return {"status": "error", "error_message": str(exc)}


@track(ignore_arguments=["client_name"])
def find_nearby_staff(
    client_name: str,
    radius_miles: float = 10.0,
    role: str = "",
) -> dict:
    """Find staff within a given mile radius of a client using H3 grid distance.

    Converts miles to H3 k-rings (~1.9 rings per mile at Resolution 8),
    then computes exact h3.grid_distance() for every staff member.
    No coordinates are used — only H3 indexes.

    Args:
        client_name: Full or partial client name, e.g. 'Claire Ferguson'.
        radius_miles: Search radius in miles. Default is 10 miles.
        role: Optional role filter — 'PCA', 'LPN', or 'RN'. Empty means all roles.

    Returns:
        dict with status, client info, and a list of matching staff
        sorted by grid_steps ascending.
    """
    try:
        conn = sqlite3.connect(DB_PATH)

        # ── Look up client ───────────────────────────────────────────────────
        client = conn.execute(
            'SELECT Client_ID, "First Name", "Last Name", H3_Index '
            "FROM clients "
            "WHERE \"First Name\" || ' ' || \"Last Name\" LIKE ?",
            (f"%{client_name}%",),
        ).fetchone()

        if not client:
            conn.close()
            logger.warning("find_nearby_staff found no matching client")
            return {"status": "error",
                    "error_message": "No client found."}

        c_id, c_first, c_last, c_hex = client
        if not c_hex:
            conn.close()
            return {"status": "error",
                    "error_message": f"Client ID {c_id} has no H3 index."}

        # ── Convert miles → k-ring threshold ─────────────────────────────────
        k_threshold = math.ceil(radius_miles * 1.9)

        # ── Query staff ──────────────────────────────────────────────────────
        query = (
            'SELECT Staff_ID, "First Name", "Last Name", Role, Mobile, '
            "Available_Hours, H3_Index FROM staff_capacity"
        )
        params: list = []
        if role:
            query += " WHERE Role = ?"
            params.append(role.strip().upper())

        staff_rows = conn.execute(query, params).fetchall()
        conn.close()

        # ── Compute H3 grid distance and filter ─────────────────────────────
        matches = []
        for row in staff_rows:
            s_id, s_first, s_last, s_role, s_mobile, s_avail, s_hex = row
            if not s_hex:
                continue
            try:
                dist = h3.grid_distance(c_hex, s_hex)
            except Exception:
                continue

            if dist <= k_threshold:
                matches.append({
                    "staff_id": s_id,
                    "role": s_role,
                    "available_hours": round(s_avail, 1) if s_avail else None,
                    "grid_steps": dist,
                    "approx_miles": round(dist / 1.9, 1),
                })

        matches.sort(key=lambda x: x["grid_steps"])

        if not matches:
            return {
                "status": "success",
                "client_id": c_id,
                "radius_miles": radius_miles,
                "k_threshold": k_threshold,
                "staff": [],
                "message": (
                    f"No {role + ' ' if role else ''}staff found within "
                    f"{radius_miles} miles (k<={k_threshold}). "
                    "Try increasing the radius."
                ),
            }

        result = {
            "status": "success",
            "client_id": c_id,
            "radius_miles": radius_miles,
            "k_threshold": k_threshold,
            "total_found": len(matches),
            "staff": matches,
        }
        logger.info(f"find_nearby_staff found {len(matches)} staff within {radius_miles} miles of Client_ID={c_id}")
        AGENT_CONTEXT["map_update"] = {
            "client_name": f"{c_first} {c_last}",
            "radius": radius_miles,
            "staff_ids": [s["staff_id"] for s in matches]
        }
        return result

    except Exception as exc:
        return {"status": "error", "error_message": str(exc)}


@track
def filter_staff_by_hours(
    staff_ids: list[str],
    min_hours: float = 10.0,
    role: str = "",
) -> dict:
    """Filter a list of staff IDs by minimum available weekly hours and/or role.

    Use this AFTER find_nearby_staff to narrow results down to staff with
    sufficient availability.

    Args:
        staff_ids: List of Staff_ID strings to filter, e.g. ['S001', 'S054'].
        min_hours: Minimum available hours per week. Default is 10.
        role: Optional role filter. Empty means don't filter by role.

    Returns:
        dict with the filtered staff list.
    """
    try:
        if not staff_ids:
            logger.warning("filter_staff_by_hours called with empty staff_ids")
            return {"status": "error",
                    "error_message": "No staff_ids provided to filter."}

        logger.debug(f"Executing filter_staff_by_hours for {len(staff_ids)} staff, min_hours={min_hours}, role='{role}'")
        conn = sqlite3.connect(DB_PATH)
        placeholders = ", ".join("?" for _ in staff_ids)
        query = (
            'SELECT Staff_ID, "First Name", "Last Name", Role, Mobile, '
            f"Available_Hours FROM staff_capacity "
            f"WHERE Staff_ID IN ({placeholders})"
        )
        params = list(staff_ids)

        if role:
            query += " AND Role = ?"
            params.append(role.strip().upper())

        if min_hours > 0:
            query += " AND Available_Hours >= ?"
            params.append(min_hours)

        rows = conn.execute(query, params).fetchall()
        conn.close()

        results = []
        for row in rows:
            results.append({
                "staff_id": row[0],
                "role": row[3],
                "available_hours": round(row[5], 1) if row[5] else None,
            })

        if "map_update" in AGENT_CONTEXT:
            AGENT_CONTEXT["map_update"]["staff_ids"] = [s["staff_id"] for s in results]

        logger.info(f"filter_staff_by_hours retained {len(results)}/{len(staff_ids)} staff members")
        return {
            "status": "success",
            "total_found": len(results),
            "min_hours_filter": min_hours,
            "staff": results,
            "note": ("Note: Weekly hour capacities are currently pending "
                     "entry for many staff. Verify directly with the caregiver."
                     if not results else ""),
        }

    except Exception as exc:
        return {"status": "error", "error_message": str(exc)}


@track
def query_staff_summary(
    group_by: str = "Role",
    role: str = "",
    limit: int = 50,
) -> dict:
    """Query the staff database for summaries, counts, or listings.

    Use this tool for general, NON-spatial questions like:
    - "How many staff do we have?" (group_by="total")
    - "How many RNs, LPNs, PCAs?" (group_by="Role")
    - "List all LPNs" (role="LPN")
    - "Show me staff with the most available hours" (group_by="hours")

    Args:
        group_by: How to group results. Options:
            - "Role"  — count staff by role (default)
            - "total" — just return total headcount
            - "hours" — list staff ordered by available hours desc
            - "list"  — flat list of staff (optionally filtered by role)
        role: Optional role filter — 'PCA', 'LPN', or 'RN'. Empty = all.
        limit: Max rows to return for list/hours queries. Default 50.

    Returns:
        dict with status and the requested summary data.
    """
    try:
        conn = sqlite3.connect(DB_PATH)

        if group_by == "total":
            query = "SELECT COUNT(*) FROM staff_capacity"
            params: list = []
            if role:
                query += " WHERE Role = ?"
                params.append(role.strip().upper())
            count = conn.execute(query, params).fetchone()[0]
            conn.close()
            label = f"{role.upper()} staff" if role else "total staff"
            return {"status": "success", "count": count, "label": label}

        elif group_by == "Role":
            rows = conn.execute(
                "SELECT Role, COUNT(*) as cnt FROM staff_capacity GROUP BY Role ORDER BY cnt DESC"
            ).fetchall()
            conn.close()
            return {
                "status": "success",
                "breakdown": [{"role": r[0], "count": r[1]} for r in rows],
                "total": sum(r[1] for r in rows),
            }

        elif group_by == "hours":
            query = (
                'SELECT Staff_ID, "First Name", "Last Name", Role, Mobile, '
                "Available_Hours FROM staff_capacity "
            )
            params = []
            if role:
                query += "WHERE Role = ? "
                params.append(role.strip().upper())
            query += "ORDER BY Available_Hours DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            conn.close()
            return {
                "status": "success",
                "staff": [
                    {
                        "staff_id": r[0],
                        "role": r[3],
                        "available_hours": round(r[5], 1) if r[5] else None,
                    } for r in rows
                ],
            }

        else:  # "list"
            query = (
                'SELECT Staff_ID, "First Name", "Last Name", Role, Mobile, '
                "Available_Hours FROM staff_capacity "
            )
            params = []
            if role:
                query += "WHERE Role = ? "
                params.append(role.strip().upper())
            query += "LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            conn.close()
            return {
                "status": "success",
                "total_returned": len(rows),
                "staff": [
                    {
                        "staff_id": r[0],
                        "role": r[3],
                        "available_hours": round(r[5], 1) if r[5] else None,
                    } for r in rows
                ],
            }

    except Exception as exc:
        return {"status": "error", "error_message": str(exc)}


# ═══════════════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPT
# ═══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """\
You are a privacy-first healthcare staffing assistant for a home care agency.
You help the Director of Nursing (DON) find available caregivers near clients.

## YOUR TOOLS

You have four deterministic Python tools that NEVER leak PII to you. 
You will ONLY see IDs, Roles, and Distances. You will NEVER see staff names or phone numbers.
Wait for the UI Map to display names and phone numbers. Your job is ONLY to answer the question conceptually based on the IDs and counts found.

1. **lookup_client** — Find a client's ID and H3 hex index by name.
2. **find_nearby_staff** — Find staff within a mile radius of a client.
   Returns list of staff (staff_id, role, approx_miles).
3. **filter_staff_by_hours** — Narrow a staff list by minimum available hours.
4. **query_staff_summary** — Count, list, or summarize staff by role/hours.

## DEFAULT BEHAVIOR

If the user asks for "nearby staff" without specifying a radius, default to **10 miles**.
Do not filter by hours unless the user explicitly requests it.

## SPATIAL MODEL

All locations are stored as H3 hexagonal indexes (Resolution 8).
The conversion is approximately **1.9 k-rings per mile**.
You NEVER have access to Latitude / Longitude — do not reference them.

## FOLLOW-UP QUESTIONS

You remember the conversation context.  If the user says:
- "What about RNs instead?" → Reuse the same client + radius, change role to RN.
- "Try 20 miles" → Reuse the same client + role, change radius to 20.

## RULES
1. **NO PII HALUCINATION**: Do not invent staff names or phone numbers. You only see Staff IDs (e.g., S001).
2. **MAP SYNC IS AUTOMATIC**: The UI Map automatically centers on your searches, draws the radius, and displays a detailed data table with names and phone numbers. You do not need to instruct the UI to do anything or apologize for missing names.
3. **CONCISE ANSWERS**: Do not output reasoning, tool parameters, or markdown tables. Just answer conceptually (e.g., "I found 3 PCAs within 10 miles. They are highlighted on the map.").
- NEVER ask the user for a client's address or location.
- NEVER generate SQL yourself — always use the provided tools.
"""


# ═══════════════════════════════════════════════════════════════════════════════
#  ADK AGENT + RUNNER
# ═══════════════════════════════════════════════════════════════════════════════

root_agent = Agent(
    name="staffing_assistant",
    model="gemini-3.1-flash-lite-preview",
    description="Healthcare staffing assistant that finds caregivers near clients.",
    instruction=SYSTEM_PROMPT,
    tools=[lookup_client, find_nearby_staff, filter_staff_by_hours, query_staff_summary],
)

_runner: Optional[InMemoryRunner] = None


def _get_runner() -> InMemoryRunner:
    """Lazy-initialize the global InMemoryRunner."""
    global _runner
    if _runner is None:
        _runner = InMemoryRunner(
            agent=root_agent,
            app_name="staffing_dashboard",
        )
    return _runner


_created_sessions: set[str] = set()


async def _ensure_session(runner: InMemoryRunner, user_id: str, session_id: str):
    """Create a session in the runner's session service if it doesn't exist yet."""
    if session_id not in _created_sessions:
        await runner.session_service.create_session(
            app_name="staffing_dashboard",
            user_id=user_id,
            session_id=session_id,
        )
        _created_sessions.add(session_id)


@track(ignore_arguments=["question"], capture_output=False)
async def _ask_async(
    question: str,
    user_id: str = "don_user",
    session_id: str = "default",
) -> dict[str, Any]:
    runner = _get_runner()
    await _ensure_session(runner, user_id, session_id)

    message = types.Content(
        role="user",
        parts=[types.Part.from_text(text=question)],
    )

    final_answer = ""
    logger.info(f"Running agent async loop for user_id={user_id}, session_id={session_id}")
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=message,
    ):
        if event.is_final_response():
            if event.content and event.content.parts:
                final_answer = "\n".join(
                    p.text for p in event.content.parts if p.text
                )

    return {
        "answer": final_answer or "I wasn't able to find an answer.",
        "success": True,
        "error": "",
        "context": dict(AGENT_CONTEXT),
    }


@track(ignore_arguments=["question"], capture_output=False)
def ask_agent(
    question: str,
    session_id: str = "default",
    **_kwargs,
) -> dict[str, Any]:
    """
    Synchronous public API for the Streamlit UI.

    Args:
        question:   The user's natural-language query.
        session_id: A unique session ID.  Same ID = same conversation memory.
                    Typically a UUID stored in st.session_state.

    Returns:
        dict with "answer" (str), "success" (bool), "error" (str).
    """
    try:
        global AGENT_CONTEXT
        AGENT_CONTEXT.clear()

        logger.info(f"Starting synchronous agent invocation for session_id={session_id}")

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(
                    asyncio.run,
                    _ask_async(question, session_id=session_id),
                )
                return future.result(timeout=120)
        else:
            return asyncio.run(
                _ask_async(question, session_id=session_id)
            )

    except Exception as exc:
        return {
            "answer": "",
            "success": False,
            "error": f"Agent error: {exc}",
            "context": {},
        }
