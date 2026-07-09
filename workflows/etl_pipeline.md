# ETL Pipeline Workflow

The Data Synchronization Pipeline (ETL) is responsible for taking raw Excel exports from the agency system, processing them securely, and writing privacy-safe location data (H3 grid cells) to the local SQLite database.

## Workflow Diagram

```mermaid
flowchart TD
    Start([Manual Trigger or App Boot]) --> ReadExcel[Read CustomerData.xlsx and CaregiverData.xlsx]

    ReadExcel --> Validate{Validate Required Columns}
    Validate -->|Missing| Abort([Abort Sync Show Error in UI])
    Validate -->|Valid| FillOptional[Fill Missing Optional Columns with NA]

    FillOptional --> Exclusions[Apply Hardcoded Name Exclusions]

    Exclusions --> SurrogateKey[Compute Secure Hash Keys for Changes]

    SurrogateKey --> Compare{Compare with Existing DB}

    Compare -->|New or Address Changed| GeocodeQueue[Add to Geocoding Queue]
    Compare -->|Existing No Address Change| Preserve[Preserve Existing H3 Index]

    GeocodeQueue --> CheckCache{Check Local Geocode Cache}

    CheckCache -->|Cached| FetchCache[Fetch Cached Lat Lng]
    CheckCache -->|Not Cached| CallGeocodio[Batch Call Geocodio API Send Address ONLY]

    CallGeocodio --> SaveCache[Save Lat Lng to Cache]
    SaveCache --> FetchCache

    FetchCache --> H3Conversion[Convert Lat Lng instantly to H3 Hex Index Res 8]

    H3Conversion --> Discard[Discard Raw Coordinates Never Store Lat Lng]
    Preserve --> Discard

    Discard --> RebuildDB[Rebuild SQLite Tables clients and staff]
    RebuildDB --> RebuildView[Rebuild vw_staff_capacity Calculating Available Hours]

    RebuildView --> End([Sync Complete Update UI Summary])

    style Start fill:#e1f5e1
    style End fill:#e1f5e1
    style Abort fill:#ffe6e6
    style CallGeocodio fill:#e6f3ff
    style H3Conversion fill:#f0e6ff
    style Discard fill:#ffe6e6
```

## Key Components

### 1. Data Ingestion
- Reads `CustomerData.xlsx` and `CaregiverData.xlsx` via pandas.
- Enforces strict required columns (`First Name`, `Last Name`, `Address 1`, `City`, `State`, `Zip`).
- Silently ignores unneeded PII columns.

### 2. Opaque Tracking (Surrogate Keys)
Rows are tracked without storing their raw addresses for matching.
- A stable surrogate key is generated combining `SHA-256(fname | lname | address_string)`.
- Compare against existing keys to detect new addresses.

### 3. Incremental Geocoding
- Uses the **Geocodio API** to convert string addresses into raw coordinates.
- Only the specific address string is sent over the network (No Names, No Phone Numbers).
- Caches results locally in SQLite to prevent redundant API calls.

### 4. Privacy via H3 Grid
- The raw `Latitude` and `Longitude` returned by Geocodio are converted immediately in memory into **H3 Hexagonal Indexes**.
- The raw coordinates are then discarded before writing to the secure application database.

### 5. Capacity Calculation
- The `vw_staff_capacity` view calculation is baked directly into the staff table so that the AI can quickly filter staff by integer logic on available versus committed hours.
