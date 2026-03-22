# MSP Engineering ONCALL Dashboard

A **live, auto-refreshing dashboard** for [Jira Filter #174525](https://jira.nutanix.com/issues/?filter=174525) that tracks MSP Engineering ONCALL tickets. Built with a **FastAPI backend** + **Chart.js frontend**, it fetches data from Jira via the Atlassian MCP bridge every 30 minutes.

**Dashboard URL:** `http://manish-sharma.r8.ubvm.nutanix.com:8050`

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              BROWSER                                     │
│  dashboard.html (Single-page app, Chart.js, client-side aggregation)     │
│                                                                          │
│  ┌──────────┐  ┌─────────────┐  ┌────────────────────────────────────┐  │
│  │ Time-    │  │ 8 Charts    │  │ 4 Filterable/Sortable Tables       │  │
│  │ Range    │  │ (Chart.js)  │  │ (SF Cases, High Impact, Open, All) │  │
│  │ Filter   │  └─────────────┘  └────────────────────────────────────┘  │
│  │ Bar      │  ┌─────────────┐                                          │
│  └──────┬───┘  │ 5 KPI Cards │                                          │
│         │      └─────────────┘                                          │
│         │  All aggregation happens client-side (instant filter changes)  │
└─────────┼───────────────────────────────────────────────────────────────┘
          │
          │  GET /           → serves dashboard.html
          │  GET /api/data   → returns ~828 lightweight JSON rows
          │  POST /api/refresh → triggers immediate re-fetch
          │
┌─────────▼───────────────────────────────────────────────────────────────┐
│                     FastAPI SERVER  (server.py :8050)                     │
│                                                                          │
│  ┌──────────────┐   ┌──────────────┐   ┌─────────────────────────────┐  │
│  │ DataCache    │   │ Background   │   │ MCPClient                   │  │
│  │ (in-memory,  │◄──│ Refresh      │──►│ (Streamable-HTTP / SSE)     │  │
│  │ thread-safe) │   │ Thread       │   │                             │  │
│  └──────┬───────┘   │ (30 min)     │   │ connect() → initialize      │  │
│         │           └──────────────┘   │ call_tool() → JSON-RPC      │  │
│         │                              │ auto-reconnect on failure    │  │
│         │                              └──────────┬──────────────────┘  │
│         ▼                                         │                      │
│   /api/data returns                               │                      │
│   cached JSON payload                             │                      │
└───────────────────────────────────────────────────┼─────────────────────┘
                                                    │
                     MCP Streamable-HTTP protocol    │
                     POST http://10.113.24.33:3008/mcp
                     (JSON-RPC 2.0 over SSE)         │
                                                    │
┌───────────────────────────────────────────────────▼─────────────────────┐
│              ATLASSIAN MCP SERVER  (10.113.24.33:3008)                    │
│              Handles all Jira authentication                             │
│                                                                          │
│  Tools used:                                                             │
│  ├─ jira_search(jql, fields, limit, start_at)                           │
│  └─ jira_get_project_versions(project)                                  │
└───────────────────────────────────────────────────┬─────────────────────┘
                                                    │
                                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    JIRA  (jira.nutanix.com)                               │
│                    Filter #174525 — MSP Engineering ONCALL                │
│                    ~828 issues                                            │
└─────────────────────────────────────────────────────────────────────────┘
```

### Data Flow

1. **Background thread** in `server.py` runs every 30 minutes
2. Connects to the **Atlassian MCP server** at `http://10.113.24.33:3008/mcp` using the MCP Streamable-HTTP protocol (JSON-RPC over SSE)
3. Calls `jira_search` with `filter=174525`, paginating 50 issues at a time (~828 total issues)
4. Calls `jira_search` with targeted `affectedVersion = "X"` JQL for 31 major version patterns to build the Affects Version/s mapping
5. Processes all issues into lightweight JSON rows and caches in memory
6. Frontend fetches `/api/data`, receives all issue rows, and does **client-side aggregation** based on the selected time-range filter

### Key Design Decisions

- **Client-side aggregation**: The server sends all ~828 lightweight issue rows to the browser. The browser aggregates (monthly counts, component counts, version counts, etc.) based on the selected time filter. Filter changes are instant — no server round-trip.
- **MCP bridge for Jira auth**: The Jira instance at `jira.nutanix.com` requires authentication. The Atlassian MCP server at `10.113.24.33:3008` handles all auth. The dashboard server talks to Jira exclusively through MCP tool calls.
- **Affects Version/s workaround**: The MCP Atlassian tool does NOT return the standard Jira `versions` (Affects Version/s) field in its serialized output. As a workaround, `server.py` runs JQL count queries like `filter=174525 AND affectedVersion = "pc.2024.2"` for each major version pattern and maps results back to issue keys.

---

## Component Breakdown

### 1. Frontend — `dashboard.html`

**Technology:** Vanilla JS + Chart.js 4.4.0 (CDN), dark-theme CSS

**Layout:**

- **Header** — title, live-dot indicator, last-refreshed timestamp, countdown timer, manual refresh button
- **Time-Range Filter Bar** — 6 buttons (1Q / 2Q / 3Q / 1Y / 2Y / All Time), default = Last 1 Year
- **5 KPI Cards** — Total, Open, Closed, High Impact (P0/P1/P2), With SF Cases
- **8 Charts:**
  - Monthly ONCALL Trend (line)
  - Open vs Closed per Month (dual line)
  - Top Fix Versions (horizontal bar)
  - Top Affects Versions (horizontal bar)
  - Top Components/CFDs (horizontal bar)
  - Priority Distribution (doughnut)
  - Top Reporters (horizontal bar)
  - Top Labels (horizontal bar)
- **4 Tables** — each with search filter, column sort, sticky headers, scrollable:
  - Top CFDs with High SF Cases (sorted by `sf_cases` desc)
  - High Impact ONCalls — P0/P1/P2 only
  - Currently Open ONCalls
  - All ONCalls in Selected Range

**Client-side data flow:**

```
loadDashboard()
  → fetch('/api/data')
  → RAW = response.issues        (all ~828 rows)
  → renderFromFilter()
      → filtered()                (apply time-range cutoff)
      → aggregate(issues)         (compute all chart/table data)
      → render 8 charts + 4 tables + 5 KPIs
```

### 2. Backend — `server.py`

**Technology:** Python 3.9 + FastAPI + Uvicorn

**4 major components:**

| Component | Purpose |
|-----------|---------|
| `MCPClient` | Thin MCP Streamable-HTTP client (initialize, call_tool, SSE parsing, auto-reconnect) |
| `fetch_all_issues()` | Paginate through filter #174525, 50 issues/page |
| `fetch_affects_version_counts()` | Query 31 `affectedVersion = "X"` JQL queries, build issue→versions map |
| `process_issues()` | Transform raw Jira responses into lightweight row dicts |

**API Endpoints:**

| Endpoint | Method | Response |
|----------|--------|----------|
| `/` | GET | Serves `dashboard.html` |
| `/api/data` | GET | Returns cached JSON (`{issues, last_refreshed, refresh_interval_min}`) or HTTP 202 if still loading |
| `/api/refresh` | POST | Spawns a background thread to re-fetch immediately |

**Caching & Threading:**

```
startup()
  └─ background_refresh_loop() [daemon thread]
       └─ while True:
            refresh_data()
              ├─ fetch_all_issues()              → ~17 MCP calls (828/50 pages)
              ├─ fetch_affects_version_counts()   → 31 MCP calls (one per version)
              └─ process_issues()                → cache.set(data)
            sleep(1800)                          → 30 min
```

The `DataCache` class is thread-safe (uses `threading.Lock`), so API reads and background writes don't conflict.

### 3. MCP Protocol — Communication with Jira

The server **never talks to Jira directly**. All auth is handled by the Atlassian MCP server.

**Protocol sequence:**

```
1. POST /mcp  {"method": "initialize", ...}
   ← Response: Mcp-Session-Id header + capabilities

2. POST /mcp  {"method": "notifications/initialized"}
   ← Acknowledged

3. POST /mcp  {"method": "tools/call", "params": {"name": "jira_search", ...}}
   ← text/event-stream (SSE):  data: {"result": {"content": [{"type":"text","text":"..."}]}}
```

**MCP tools used:**

| Tool | Arguments | Purpose |
|------|-----------|---------|
| `jira_search` | `jql`, `fields`, `limit`, `start_at` | Fetch all issues + per-version counts |
| `jira_get_project_versions` | `project` | List all versions (3,719 in ONCALL project) |

### 4. Affects Version Workaround

The MCP `jira_search` tool does **not** serialize the `versions` (Affects Version/s) field. The workaround:

```
For each of 31 version strings (e.g., "pc.2024.2", "7.5"):
  → JQL: filter=174525 AND affectedVersion = "pc.2024.2"
  → Collect all matching issue keys
  → Build map: {issue_key: [version, ...]}
```

**31 version patterns queried:**

```
pc.2025.2, pc.2025.1,
pc.2024.3, pc.2024.2, pc.2024.1,
pc.2023.4, pc.2023.3, pc.2023.2, pc.2023.1,
pc.2022.6, pc.2022.9, pc.2022.4, pc.2022.1,
7.5, 7.3, 7.2, 7.1, 7.0,
6.8, 6.7, 6.6, 6.5, 6.1, 6.0,
5.20, 5.19, 5.18, 5.17, 5.15, 5.11, 5.10
```

Expand by editing `_AFFECTS_VERSION_PREFIXES` in `server.py`.

---

## Issue Data Shape (server → browser)

Each issue row sent to the browser:

```json
{
  "key": "ONCALL-1234",
  "summary": "...",
  "status": "Need Info",
  "priority": "Major - P2",
  "created": "2025-11-15",
  "assignee": "John Doe",
  "reporter": "Jane Smith",
  "components": ["MSP", "Poseidon"],
  "fix_versions": [],
  "affects_versions": ["pc.2024.2"],
  "sf_cases": 3,
  "labels": ["msp-oncall"],
  "is_open": true
}
```

### Jira Fields Used

| Field | Jira Field ID | Usage |
|-------|---------------|-------|
| Summary | `summary` | Issue title in all tables |
| Status | `status` | Open/Closed determination; status badges |
| Created | `created` | Monthly trend; time-range filtering |
| Priority | `priority` | Priority chart; high-impact filtering (P0/P1/P2) |
| Assignee | `assignee` | Displayed in tables |
| Reporter | `reporter` | "Customer Proxy" chart — top reporters |
| Components | `components` | CFD/component chart |
| Fix Version/s | `fixVersions` | Fix Version chart |
| Affects Version/s | `versions` (via JQL) | Affects Version chart (MCP workaround) |
| Labels | `labels` | Labels chart |
| # SF Cases | `customfield_12364` | Salesforce case count badge; CFD ranking |
| Resolution | `resolution` | Status classification |

### Open/Closed Logic

An issue is considered **open** if:
- Status name is NOT in: `{Closed, Resolved, Done, Complete, Cancelled}`
- Status category is NOT in: `{Done, Complete}`

---

## File Map

```
~/Nutanix/github/oncall_dashboard/
├── server.py             ← FastAPI backend — MCP client, data pipeline, API
├── dashboard.html        ← Frontend — Chart.js, client-side aggregation
├── requirements.txt      ← fastapi, uvicorn, requests, apscheduler
├── start.sh              ← Launch helper (runs python3.9 server.py)
├── CONTEXT.md            ← Project context document
├── README.md             ← This file
└── server.log            ← Runtime logs
```

---

## Server Management

```bash
# Start
cd ~/Nutanix/github/oncall_dashboard && ./start.sh

# Start in background
cd ~/Nutanix/github/oncall_dashboard && nohup python3.9 server.py > server.log 2>&1 &

# Stop
kill $(pgrep -f "python3.9 server.py")

# Check logs
tail -f ~/Nutanix/github/oncall_dashboard/server.log

# Verify API
curl -s http://localhost:8050/api/data | python3.9 -m json.tool | head -20
```

### Configuration (top of `server.py`)

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_BASE_URL` | `http://10.113.24.33:3008/mcp` | Atlassian MCP endpoint |
| `JIRA_FILTER_JQL` | `filter=174525 ORDER BY created DESC` | Jira filter query |
| `PAGE_SIZE` | `50` | Issues per API page |
| `REFRESH_INTERVAL_SECONDS` | `1800` (30 min) | Auto-refresh interval |
| `SERVER_PORT` | `8050` | HTTP port |

### Firewall

Port 8050 was opened via:

```bash
sudo firewall-cmd --add-port=8050/tcp --permanent && sudo firewall-cmd --reload
```

---

## Runtime Environment

| Property | Value |
|----------|-------|
| Host | `manish-sharma.r8.ubvm.nutanix.com` |
| OS | Linux 5.10 / RHEL 8 |
| Python | 3.9.25 (`/usr/bin/python3.9`) |
| Port | 8050 (firewall opened) |
| MCP endpoint | `http://10.113.24.33:3008/mcp` |
| Jira filter | #174525 |
| Refresh cycle | 30 minutes |
| Data size | ~828 issues, ~220 with affects-version data |

### Dependencies

```bash
python3.9 -m pip install --user fastapi uvicorn requests apscheduler
```
