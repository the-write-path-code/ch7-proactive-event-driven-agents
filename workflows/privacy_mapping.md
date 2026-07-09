# Privacy Map Rendering Workflow

The application places a deep structural emphasis on shielding Personal Health Information (PHI) and Personally Identifiable Information (PII). Location coordinates are completely wiped from the DB infrastructure.

## Workflow Diagram

```mermaid
flowchart TD
    Start([Side Channel Update from Agent System]) --> ReadDict[Read AGENT_CONTEXT for Staff IDs]

    ReadDict --> SyncState[Stash IDs in Streamlit Session State]

    SyncState --> QueryDB{Intersect with Local SQLite DB}

    QueryDB -->|Fetch H3 Indexes| MapHex[Render Hexagonal Folium Overlays]
    QueryDB -->|Fetch PII| RenderTable[Render Data Table Names Phones Hours]

    MapHex --> Colors[Apply Heatmap Colors by Available Hours]
    MapHex --> Tooltip[Bind Hover Tooltips Client PII is local only]

    RenderTable --> Sort[Sort by Distance Miles]

    Colors --> MapComponent[st_folium component]
    Tooltip --> MapComponent

    MapComponent --> ClickMap[User clicks Hexagon]
    ClickMap --> Override[Override Agent Context Center newly clicked client]

    Override --> Start

    style Start fill:#e1f5e1
    style SyncState fill:#e6f3ff
    style MapHex fill:#f0e6ff
    style ClickMap fill:#fff4e6
```

## Key Components

### 1. The Discard Protocol
The raw Latitude and Longitude variables are never inserted into SQLite. `etl/sync.py` purges them the moment H3 resolution conversion completes.

### 2. Hexagonal Clustering (Folium)
Instead of plotting traditional exact pins using Folium `Marker`, the Map Engine looks up the pre-computed H3 Index, pulls its explicit `h3.cell_to_boundary()`, and draws an obfuscated polygon area.
- Gives the Director of Nursing spatial awareness up to ~0.74 square kilometers (enough to coordinate rideshares and commutes).
- **Security Check**: This spatial resolution prevents precise street or housing deduplication, natively protecting both Staff home addresses and Client private facilities.

### 3. Color Depth Signals
- Staff members glow heavier and brighter depending on their `Available_Hours`.
- PCA, LPN, and RNs each use a distinct Hexagon border color mapping.

### 4. Interactive Feedback
Clicking any hexagon overrides the LLM's current targeted focus, routing the focus context strictly back through Streamlit `st.rerun()` directly bypassing the API completely.
