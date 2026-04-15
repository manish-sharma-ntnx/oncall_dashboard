#!/usr/bin/env python3
"""
ONCALL Live Dashboard Server
Fetches data from Jira via the Atlassian MCP bridge and serves an interactive dashboard.
Auto-refreshes every 30 minutes (configurable).
"""

import json
import logging
import re
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MCP_BASE_URL = "http://10.113.24.33:3008/mcp/"
JIRA_FILTER_ONCALL = "filter=174525 ORDER BY created DESC"
JIRA_FILTER_CFD = "filter=127170 ORDER BY created DESC"
JIRA_FIELDS = (
    "summary,status,created,updated,priority,assignee,labels,"
    "reporter,fixVersions,components,resolution,versions,customfield_12364,customfield_10011"
)
PAGE_SIZE = 50
REFRESH_INTERVAL_SECONDS = 30 * 60  # 30 minutes
SERVER_PORT = 8050

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("oncall-dashboard")

# ---------------------------------------------------------------------------
# MCP Client
# ---------------------------------------------------------------------------
class MCPClient:
    """Thin client that talks to the Atlassian MCP server over Streamable-HTTP."""

    def __init__(self, base_url: str):
        self.base_url = base_url
        self.session_id = None
        self._req_id = 0
        self._lock = threading.Lock()

    def _next_id(self):
        with self._lock:
            self._req_id += 1
            return self._req_id

    def _headers(self):
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _parse_sse(self, text):
        for line in text.split("\n"):
            if line.startswith("data: "):
                return json.loads(line[6:])
        return None

    def connect(self):
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "oncall-dashboard", "version": "2.0"},
            },
        }
        r = requests.post(self.base_url, json=payload, timeout=60,
                          headers=self._headers(), allow_redirects=True)
        if r.status_code >= 400:
            raise RuntimeError(f"MCP initialize failed: HTTP {r.status_code} — {r.text[:200]}")
        self.session_id = r.headers.get("mcp-session-id")
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        notif_resp = requests.post(self.base_url, json=notif, timeout=30,
                                  headers=self._headers(), allow_redirects=True)
        if notif_resp.status_code >= 400:
            raise RuntimeError(f"MCP initialized notification failed: HTTP {notif_resp.status_code} — {notif_resp.text[:200]}")
        log.info("MCP session established: %s", self.session_id)

    def call_tool(self, name: str, arguments: dict, timeout: int = 120):
        if not self.session_id:
            self.connect()
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        try:
            r = requests.post(self.base_url, json=payload, timeout=timeout,
                              headers=self._headers(), allow_redirects=True)
            if r.status_code >= 400:
                raise RuntimeError(f"MCP tool call failed: HTTP {r.status_code} — {r.text[:200]}")
        except Exception:
            self.session_id = None
            self.connect()
            r = requests.post(self.base_url, json=payload, timeout=timeout,
                              headers=self._headers(), allow_redirects=True)
            if r.status_code >= 400:
                raise RuntimeError(f"MCP tool call failed: HTTP {r.status_code} — {r.text[:200]}")

        data = self._parse_sse(r.text)
        if not data:
            try:
                data = json.loads(r.text)
            except Exception:
                log.error("MCP raw response (first 500 chars): %s", r.text[:500])
                raise RuntimeError(f"Unparseable MCP response for {name}")
        if "error" in data:
            raise RuntimeError(f"MCP error: {data['error']}")
        content = data.get("result", {}).get("content", [])
        for c in content:
            if c.get("type") == "text":
                txt = c["text"]
                try:
                    return json.loads(txt)
                except json.JSONDecodeError:
                    log.error("MCP text content not JSON (first 500 chars): %s", txt[:500])
                    raise
        return None

# ---------------------------------------------------------------------------
# Jira Data Fetcher
# ---------------------------------------------------------------------------
def fetch_all_issues(mcp: MCPClient, jql: str) -> list:
    """Paginate through the full filter result set."""
    all_issues = []
    start = 0
    total = None
    while True:
        log.info("Fetching issues %d-%d ...", start, start + PAGE_SIZE - 1)
        result = mcp.call_tool("jira_search", {
            "jql": jql,
            "fields": JIRA_FIELDS,
            "limit": PAGE_SIZE,
            "start_at": start,
        })
        if total is None:
            total = result.get("total", 0)
            log.info("Total issues in filter: %d", total)
        issues = result.get("issues", [])
        all_issues.extend(issues)
        start += PAGE_SIZE
        if start >= total or not issues:
            break
    log.info("Fetched %d issues", len(all_issues))
    return all_issues


# Major version prefixes to probe for Affects Version/s counts
_AFFECTS_VERSION_PREFIXES = [
    "pc.2025.2", "pc.2025.1",
    "pc.2024.3", "pc.2024.2", "pc.2024.1",
    "pc.2023.4", "pc.2023.3", "pc.2023.2", "pc.2023.1",
    "pc.2022.6", "pc.2022.9", "pc.2022.4", "pc.2022.1",
    "7.5", "7.3", "7.2", "7.1", "7.0",
    "6.8", "6.7", "6.6", "6.5", "6.1", "6.0",
    "5.20", "5.19", "5.18", "5.17", "5.15", "5.11", "5.10",
]


def fetch_affects_version_counts(mcp: MCPClient, filter_id: str) -> dict:
    """Query JQL for each major version to build affects-version counts.
    Returns {issue_key: [version, ...]} mapping.
    """
    key_versions: dict[str, list[str]] = defaultdict(list)

    def _query_version(ver: str):
        jql = f'filter={filter_id} AND affectedVersion = "{ver}"'
        try:
            result = mcp.call_tool("jira_search", {
                "jql": jql, "fields": "summary", "limit": 50, "start_at": 0,
            }, timeout=30)
            total = result.get("total", 0)
            keys = [i["key"] for i in result.get("issues", [])]
            # If more than 50, paginate
            while len(keys) < total:
                more = mcp.call_tool("jira_search", {
                    "jql": jql, "fields": "summary",
                    "limit": 50, "start_at": len(keys),
                }, timeout=30)
                batch = [i["key"] for i in more.get("issues", [])]
                if not batch:
                    break
                keys.extend(batch)
            return ver, keys
        except Exception as e:
            log.warning("Failed to query affectedVersion=%s: %s", ver, e)
            return ver, []

    log.info("Fetching affects-version counts for %d versions...", len(_AFFECTS_VERSION_PREFIXES))
    for ver in _AFFECTS_VERSION_PREFIXES:
        ver_name, keys = _query_version(ver)
        for k in keys:
            key_versions[k].append(ver_name)

    log.info("Affects-version mapping built: %d issues have version data", len(key_versions))
    return dict(key_versions)

# ---------------------------------------------------------------------------
# Data Processing — send lightweight rows; client does all aggregation
# ---------------------------------------------------------------------------
def is_open(status_name, category):
    closed_statuses = {"Closed", "Resolved", "Done", "Complete", "Cancelled"}
    closed_cats = {"Done", "Complete"}
    return status_name not in closed_statuses and category not in closed_cats


def process_issues(issues: list, affects_map: dict = None) -> dict:
    affects_map = affects_map or {}
    rows = []
    for issue in issues:
        created_str = issue.get("created", "")
        if not created_str:
            continue

        status_name = issue.get("status", {}).get("name", "Unknown")
        category = issue.get("status", {}).get("category", "Unknown")
        if isinstance(category, dict):
            category = category.get("name", "Unknown")

        components = []
        for c in issue.get("components", []):
            n = c.get("name") if isinstance(c, dict) else str(c)
            if n:
                components.append(n)

        fix_versions = []
        for v in issue.get("fix_versions", []):
            n = v.get("name") if isinstance(v, dict) else str(v)
            if n:
                fix_versions.append(n)

        assignee = issue.get("assignee", {}) or {}
        reporter = issue.get("reporter", {}) or {}
        sf = issue.get("custom_fields", {}).get("customfield_12364", {})
        sf_cases = sf.get("value", 0) if isinstance(sf, dict) else 0

        impacts = []
        impact_field = issue.get("custom_fields", {}).get("customfield_10011", {})
        if isinstance(impact_field, dict) and "value" in impact_field:
            val = impact_field["value"]
            if isinstance(val, list):
                for imp in val:
                    if isinstance(imp, dict) and "value" in imp:
                        impacts.append(imp["value"])
                    elif isinstance(imp, str):
                        impacts.append(imp)
            elif isinstance(val, str):
                impacts.append(val)

        issue_key = issue["key"]
        rows.append({
            "key": issue_key,
            "summary": issue.get("summary", ""),
            "status": status_name,
            "priority": issue.get("priority", {}).get("name", "Unknown"),
            "impacts": impacts,
            "created": created_str[:10],
            "assignee": assignee.get("display_name", "Unassigned") if assignee else "Unassigned",
            "reporter": reporter.get("display_name", "Unknown") if reporter else "Unknown",
            "components": components,
            "fix_versions": fix_versions,
            "affects_versions": affects_map.get(issue_key, []),
            "sf_cases": int(sf_cases or 0),
            "labels": issue.get("labels", []),
            "is_open": is_open(status_name, category),
        })

    return {
        "issues": rows,
        "last_refreshed": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "refresh_interval_min": REFRESH_INTERVAL_SECONDS // 60,
    }

# ---------------------------------------------------------------------------
# In-Memory Cache
# ---------------------------------------------------------------------------
class DataCache:
    def __init__(self):
        self.data = None
        self.lock = threading.Lock()
        self.refreshing = False
        self.last_error = None
        self.error_count = 0

    def get(self):
        with self.lock:
            return self.data

    def set(self, data):
        with self.lock:
            self.data = data
            self.refreshing = False
            self.last_error = None
            self.error_count = 0

    def set_refreshing(self):
        with self.lock:
            self.refreshing = True

    def set_error(self, msg):
        with self.lock:
            self.last_error = str(msg)
            self.error_count += 1
            self.refreshing = False

cache = DataCache()
mcp_client = MCPClient(MCP_BASE_URL)

def refresh_data():
    log.info("Starting data refresh...")
    cache.set_refreshing()
    try:
        log.info("--- Fetching ONCALLs ---")
        oncall_issues = fetch_all_issues(mcp_client, JIRA_FILTER_ONCALL)
        oncall_affects = fetch_affects_version_counts(mcp_client, "174525")
        oncall_data = process_issues(oncall_issues, oncall_affects)

        log.info("--- Fetching CFDs ---")
        cfd_issues = fetch_all_issues(mcp_client, JIRA_FILTER_CFD)
        cfd_affects = fetch_affects_version_counts(mcp_client, "127170")
        cfd_data = process_issues(cfd_issues, cfd_affects)

        data = {
            "oncall": oncall_data["issues"],
            "cfd": cfd_data["issues"],
            "last_refreshed": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "refresh_interval_min": REFRESH_INTERVAL_SECONDS // 60,
        }
        cache.set(data)
        log.info("Data refresh complete: %d ONCALLs, %d CFDs", len(oncall_issues), len(cfd_issues))
    except Exception as e:
        log.error("Data refresh failed: %s", e, exc_info=True)
        cache.set_error(e)

def background_refresh_loop():
    while True:
        try:
            refresh_data()
        except Exception as e:
            log.error("Background refresh error: %s", e)
        time.sleep(REFRESH_INTERVAL_SECONDS)

# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------
app = FastAPI(title="MSP ONCALL Dashboard")

@app.on_event("startup")
def startup():
    t = threading.Thread(target=background_refresh_loop, daemon=True)
    t.start()
    log.info("Background refresh thread started (interval=%d min)", REFRESH_INTERVAL_SECONDS // 60)

@app.get("/")
def serve_dashboard():
    return FileResponse(Path(__file__).parent / "dashboard.html", media_type="text/html")

@app.get("/api/data")
def get_data():
    data = cache.get()
    if data is None:
        resp = {"status": "loading", "message": "Initial data load in progress, please wait..."}
        if cache.last_error:
            resp["status"] = "error"
            resp["error"] = cache.last_error
            resp["error_count"] = cache.error_count
            resp["message"] = f"MCP server unreachable (failed {cache.error_count}x). Will retry every {REFRESH_INTERVAL_SECONDS//60} min."
        return JSONResponse(resp, status_code=202)
    return JSONResponse(data)

@app.post("/api/refresh")
def trigger_refresh():
    t = threading.Thread(target=refresh_data, daemon=True)
    t.start()
    return JSONResponse({"status": "ok", "message": "Refresh triggered"})

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    log.info("Starting ONCALL Dashboard Server on port %d", SERVER_PORT)
    log.info("Refresh interval: %d minutes", REFRESH_INTERVAL_SECONDS // 60)
    log.info("Dashboard URL: http://0.0.0.0:%d", SERVER_PORT)
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT, log_level="info")
