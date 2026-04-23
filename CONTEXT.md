# MSP Engineering ONCALL Dashboard — Project Context

## Overview

A **live, auto-refreshing dashboard** for Jira filters tracking MSP Engineering tickets. Built with a **FastAPI backend** + **Chart.js frontend**, it fetches data from Jira via the Atlassian MCP bridge every 30 minutes.

Currently tracks three primary filters:
- **ONCALLs**: Filter [#174525](https://jira.nutanix.com/issues/?filter=174525)
- **Top CFDs**: Filter [#127170](https://jira.nutanix.com/issues/?filter=127170)
- **Top CFIs**: Filter [#126304](https://jira.nutanix.com/issues/?filter=126304)

**Dashboard URL:** `http://manish-sharma.r8.ubvm.nutanix.com:8050`

---

## Architecture

```
Browser  ──GET /──────────>  FastAPI (server.py :8050)  ──serves──>  dashboard.html
Browser  ──GET /api/data──>  FastAPI                    ──MCP────>   Jira Filters (#174525, #127170, #126304)
Browser  ──POST /api/refresh──> triggers immediate re-fetch
```

### Data Flow

1. **Background thread** in `server.py` runs every 30 min
2. Connects to the **Atlassian MCP server** at `http://10.113.24.33:3008/mcp` using the MCP Streamable-HTTP protocol (JSON-RPC over SSE)
3. Calls `jira_search` tool with the respective filters (`174525`, `127170`, `126304`), paginating 50 issues at a time
4. Calls `jira_search` with targeted `affectedVersion = "X"` JQL for 31 major version patterns to build the Affects Version/s mapping (since the MCP tool doesn't expose the `versions` field per-issue)
5. Processes all issues into lightweight JSON rows and caches in memory
6. Frontend fetches `/api/data`, receives all issue rows for all three tabs, and does **client-side aggregation** based on the selected tab and time-range filter

### Key Technical Decisions

- **Client-side aggregation**: The server sends all lightweight issue rows to the browser. The browser aggregates (monthly counts, component counts, version counts, etc.) based on the selected tab and time filter. This means filter and tab changes are instant — no server round-trip.
- **MCP bridge for Jira auth**: The Jira instance at `jira.nutanix.com` requires authentication. The Atlassian MCP server at `10.113.24.33:3008` handles all auth. The dashboard server talks to Jira exclusively through MCP tool calls.
- **Affects Version/s workaround**: The MCP Atlassian tool does NOT return the standard Jira `versions` (Affects Version/s) field in its serialized output. As a workaround, `server.py` runs JQL count queries like `filter=174525 AND affectedVersion = "pc.2024.2"` for each major version pattern and maps results back to issue keys.

---

## Files

| File | Purpose |
|------|---------|
| `server.py` | FastAPI backend — MCP client, Jira data fetcher, affects-version resolver, in-memory cache, API endpoints |
| `dashboard.html` | Dynamic frontend — fetches `/api/data`, client-side aggregation, Chart.js charts, filterable/sortable tables |
| `requirements.txt` | Python dependencies: `fastapi`, `uvicorn`, `requests`, `apscheduler` |
| `start.sh` | Helper script to launch the server |
| `server.log` | Runtime logs |

---

## Dashboard Tabs

The dashboard is split into three main tabs, each with identical widgets and tables but driven by different Jira filters:
- **ONCALLs** (Filter 174525)
- **Top CFDs** (Filter 127170)
- **Top CFIs** (Filter 126304)

## Dashboard Sections (8 widgets per tab)

1. **Monthly ONCALL Trend** — Line chart showing oncall volume per month
2. **Open vs Closed per Month** — Dual line chart (red=Open, green=Closed)
3. **Top Fix Version/s** — Horizontal bar chart from `fixVersions` field
4. **Top Affects Version/s** — Horizontal bar chart from `affectedVersion` JQL queries (31 version patterns)
5. **Top Components (CFDs)** — Horizontal bar chart from `components` field
6. **Priority Distribution** — Doughnut chart (P0/P1/P2/P3/P4)
7. **Top Reporters (Customer Proxy)** — Horizontal bar chart of who files the most oncalls
8. **Top Labels** — Horizontal bar chart of label frequency

### Tables

- **Top CFDs with High Salesforce Cases** — Sorted by `customfield_12364` (Number of Salesforce Cases) descending
- **High Impact ONCalls (P0/P1/P2)** — Filtered to critical priorities within the selected time range
- **Currently Open ONCalls** — All issues where status is not Done/Closed/Resolved
- **All ONCalls in Selected Range** — Full list for the selected time window

### Time-Range Filter

A button bar at the top controls ALL widgets simultaneously:
- Last 1 Qtr (3 months)
- Last 2 Qtrs (6 months)
- Last 3 Qtrs (9 months)
- **Last 1 Year** (default)
- Last 2 Years
- All Time

---

## Jira Fields Used

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

## MCP Protocol Details

The server uses the **MCP Streamable-HTTP transport** to talk to the Atlassian MCP server:

1. **Initialize**: `POST /mcp` with `method: "initialize"` — returns `Mcp-Session-Id` header
2. **Notify**: `POST /mcp` with `method: "notifications/initialized"`
3. **Call tools**: `POST /mcp` with `method: "tools/call"`, `params: {name, arguments}`
4. **Parse response**: Server returns SSE (`text/event-stream`), parse `data:` lines as JSON-RPC

Key MCP tools used:
- `jira_search` — paginated issue search with JQL
- `jira_get_project_versions` — list all versions in ONCALL project (3,719 versions)

Session may expire — the `MCPClient` class auto-reconnects on failure.

---

## Affects Version Patterns Queried

The server queries these 31 major version strings against the filter:

```
pc.2025.2, pc.2025.1,
pc.2024.3, pc.2024.2, pc.2024.1,
pc.2023.4, pc.2023.3, pc.2023.2, pc.2023.1,
pc.2022.6, pc.2022.9, pc.2022.4, pc.2022.1,
7.5, 7.3, 7.2, 7.1, 7.0,
6.8, 6.7, 6.6, 6.5, 6.1, 6.0,
5.20, 5.19, 5.18, 5.17, 5.15, 5.11, 5.10
```

These are expanded by editing `_AFFECTS_VERSION_PREFIXES` in `server.py`.

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
| `JIRA_FILTER_ONCALL` | `filter=174525 ORDER BY created DESC` | Jira filter query for ONCALLs |
| `JIRA_FILTER_CFD` | `filter=127170 ORDER BY created DESC` | Jira filter query for CFDs |
| `JIRA_FILTER_CFI` | `filter=126304 ORDER BY created DESC` | Jira filter query for CFIs |
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

- **Host**: `manish-sharma.r8.ubvm.nutanix.com` (Linux 5.10, RHEL 8)
- **Python**: 3.9.25 (`/usr/bin/python3.9`)
- **Dependencies installed via**: `python3.9 -m pip install --user fastapi uvicorn requests apscheduler`
- **Port**: 8050 (firewall opened)

---

## Current Data Stats (as of Mar 20, 2026)

- **Total issues in filter**: 828
- **Currently open**: 24 (14 Need Info, 9 Customer Relief Provided, 1 In Progress)
- **Closed/Done**: 804
- **Issues with Affects Version data**: 220
- **Issues with SF Cases > 0**: 826
- **Top components**: MSP, Objects(OSS), Prism Central, Poseidon, PC-Infra

---

## Pending / Future Enhancements

- Fix Version/s chart is empty because no ONCALL issues in this filter have `fixVersions` set — may need to check if a different field is used for this purpose
- Expand `_AFFECTS_VERSION_PREFIXES` list if more granular sub-versions are needed (e.g., `pc.2024.1.0.1`, `7.5.0.1`)
- Add customer name tracking (fields `customfield_25260`, `customfield_28660`, `customfield_33472` exist but are not populated on ONCALL issues)
- User mentioned "there are some changes in the dashboard" — awaiting specifics on further layout/widget modifications
