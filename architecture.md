# YTI Homecare — Privacy & Architecture Design

This document details the architecture of the Agentic Staffing Dashboard, focusing on how we maintain strong data privacy (PII/PHI protection) while enabling advanced AI and spatial features.

## 1. Core Principles

1. **No Raw Coordinates in Database:** Exact GPS coordinates are considered highly sensitive. They exist only transiently in memory and are discarded. The database only stores Uber H3 hexagonal indices (Resolution 8).
2. **Minimal PII Retention:** Addresses, ZIP codes, and Birth Dates are strictly purged from the system after geographical and hashing steps are complete.
3. **Opaque Hashing for Sync:** To sync Excel updates without storing addresses, we use deterministic SHA-256 hashes of the `Name + Address` string.
4. **LLM Sandboxing:** The AI (Google Gemini) never executes raw SQL and never sees the whole database. It only receives targeted, minimized contextual data returned by strictly defined Python tools.
5. **Deterministic Map Control:** The map UI doesn't rely on the LLM to format JSON to control the map focus. Instead, it uses an invisible "Side-Channel" written by the deterministic Python tools.

---

## 2. The Ingestion Pipeline (ETL)

The ETL process runs locally on the DON's machine. It takes the agency Excel exports and converts them into a privacy-safe SQLite database.

```mermaid
flowchart TD
    E_Client[CustomerData.xlsx] --> ETL
    E_Staff[CaregiverData.xlsx] --> ETL

    subgraph "ETL Memory (Transient)"
    ETL[ETL Process]
    ETL --> |1. Check _match_hash| Match[Compare with DB Hashes]
    Match --> |2. If New/Changed Address| Geo[Geocodio API]
    Geo --> |Lat/Lng| H3[Convert to H3 Index]
    H3 --> |3. Purge PII| Purge{Drop Address, DOB, etc.}
    end

    Purge --> |4. Save to DB| DB[(Secure SQLite DB)]

    style E_Client fill:#f9f,stroke:#333,stroke-width:2px
    style E_Staff fill:#f9f,stroke:#333,stroke-width:2px
    style DB fill:#9cf,stroke:#333,stroke-width:2px
```

### The SHA-256 Surrogate Key

To know if a caregiver moved (and needs re-geocoding), the system must compare the new Excel file to the database. However, rule #2 dictates we cannot store addresses in the database.

**The Solution:** During ETL, we compute `hashlib.sha256("first_name|last_name|fulladdress")` and store it as `_match_hash`.
- **Privacy:** It is impossible to reverse-engineer an address from the hash.
- **Robustness:** A hash mismatch on an existing name immediately flags an address change, triggering a targeted re-geocode without the DB ever holding the raw address string.

---

## 3. The AI & Map Architecture

When the DON asks the Assistant a question, the request flows through the Google ADK, into Gemini, back to local Python tools, and finally synchronizes with the Streamlit Map UI using a **Side-Channel**.

```mermaid
sequenceDiagram
    participant User
    participant UI as Streamlit (UI)
    participant ADK as Agent (ADK Tools)
    participant LLM as Google Gemini
    participant DB as Secure DB (SQLite)

    User->>UI: "Find PCAs near Claire Ferguson"
    UI->>ADK: ask_agent(query, session_context)

    ADK->>LLM: Text Query + Tool Signatures

    LLM-->>ADK: Tool Call: lookup_client(name="Claire Ferguson")
    ADK->>DB: Query Client logic
    DB-->>ADK: Client C001, H3 Index: 882a...
    ADK-->>LLM: Text Result: Client C001 found at index 882a...

    LLM-->>ADK: Tool Call: find_nearby_staff(client="C001", radius=10, role="PCA")

    activate ADK
    ADK->>DB: Query PCAs within grid_distance <= K
    DB-->>ADK: Return 2 PCAs

    note right of ADK: 🔒 SIDE-CHANNEL WRITE<br>Record `{"client_name": "Claire", "radius": 10}`<br>into local AGENT_CONTEXT memory.

    ADK-->>LLM: Text Result: Found 2 PCAs...
    deactivate ADK

    LLM-->>ADK: Final Response: "I found 2 PCAs near Claire..."

    ADK-->>UI: {"answer": "I found...", "context": {"map_update": {...}}}

    note left of UI: Read context dictionary<br>Update st.session_state params
    UI->>UI: st.rerun() (Map redraws on Claire)
    UI->>User: Display Chat Answer + Centered Map
```

### Why the Side-Channel matters
If we relied on the LLM to output a JSON object to control the UI, it could hallucinate names, invent radii, or format the JSON incorrectly, crashing the app.

By having the deterministic Python tool (`find_nearby_staff`) write the actual `client_name` and `radius` to a local dictionary *while it executes*, we guarantee that the map perfectly reflects the exact data the underlying database tool queried. The LLM never even knows the UI map is updating.
