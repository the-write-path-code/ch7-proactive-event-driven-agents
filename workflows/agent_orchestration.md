# Agent Orchestration Workflow

The AI Staffing Assistant acts as a conversational bridge between the Director of Nursing (DON) and the secured H3 database. It utilizes **Google ADK** to perform deterministic Tool Calling.

## Workflow Diagram

```mermaid
flowchart TD
    Start([User Chat Input]) --> App[Streamlit UI ask_agent]

    App --> ADK[Google ADK Runner gemini-3.1-flash-lite]

    ADK --> ParsePrompt{Intent Parser}

    ParsePrompt -->|Needs Client Info| Tool1[Client Lookup Tool finds Client ID and H3]
    ParsePrompt -->|Spatial Search| Tool2[Nearby Staff Tool finds Staff in Radius]
    ParsePrompt -->|Role/Hour Filter| Tool3[Hours Filter Tool filters Staff by Availability]

    Tool1 --> ToolContext[AGENT_CONTEXT Side Channel Dictionary]
    Tool2 --> ToolContext
    Tool3 --> ToolContext

    ToolContext --> UpdateUI[UI Map Markers and Tables Bypass LLM Hallucines]

    Tool1 --> DB1[(Secure SQLite DB Clients Table)]
    Tool2 --> DB2[(Secure SQLite DB H3 Grid Distance)]
    Tool3 --> DB3[(Secure SQLite DB Capacity View)]

    DB1 --> ReturnTool1[Tool Result Client ID and Roles]
    DB2 --> ReturnTool2[Tool Result List of Staff IDs]
    DB3 --> ReturnTool3[Tool Result Filtered Staff IDs]

    ReturnTool1 --> Compose[LLM Synthesizes Answer]
    ReturnTool2 --> Compose
    ReturnTool3 --> Compose

    Compose --> Output([Markdown Chat Response])

    style Start fill:#e1f5e1
    style Output fill:#e1f5e1
    style App fill:#fff4e6
    style ADK fill:#e6f3ff
    style ToolContext fill:#ffe6e6
    style UpdateUI fill:#e6e6fa
    style Compose fill:#f0e6ff
```

## Key Components

### 1. In-Memory Runner
- Preserves context across chat messages via `InMemoryRunner`, simulating LangChain's memory buffer.
- Tracks active `session_id` directly to a local Python dictionary inside ADK.

### 2. Pydantic Constraints
Each Python tool is decorated with strict Pydantic `BaseModel` schemas:
- `ClientLookupInput`
- `NearbyStaffInput`
- `StaffFilterInput`

### 3. H3 Traversal Rules
- `find_nearby_staff` computes search arrays recursively. It translates `miles` directly into H3 k-ring bounds (e.g., 5 miles ≈ 10 H3 k-rings at integer Resolution 8).
- Runs `h3.grid_distance()` natively in Python rather than depending on heavy PostGIS databases.

### 4. Deterministic UI State
- Rather than forcing the LLM to write out structured JSON mapping states, the `AGENT_CONTEXT` global dictionary exposes a side channel.
- As tools execute (e.g. `find_nearby_staff`), they write their raw output to `AGENT_CONTEXT["map_update"]` — Streamlit reads this to render the map instantly.
- **Privacy Redaction**: The Tools only return Staff IDs to the LLM (so the LLM cannot hallucinate or steal names/contact info). The Map looks up the names locally.
