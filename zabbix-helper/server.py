"""zabbix-helper: a small MCP server with Zabbix tools that small local LLMs can use reliably.

  host_list       compact host list: id, names, status (instead of a ~16 KB host_get record)
  history_recent  last N minutes of item history, summarized (min / avg / max / last)

Small models (around 4B parameters) cannot reliably convert dates to Unix timestamps or
compute averages, and they lose track in large tool responses. This server does that work
itself and returns short, ready-to-quote values (for example "0.78 %" or "6.34 GB").

It calls the Zabbix API directly, so it is not bound by the MCP server's response size limit.
It stores no token: the client's Authorization header is forwarded to the Zabbix API as is,
and Zabbix enforces the permissions of that token.
"""

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

ZABBIX_API_URL = os.environ.get("ZABBIX_API_URL", "http://zabbix-web:8080/api_jsonrpc.php")
TZ = ZoneInfo(os.environ.get("TZ", "UTC"))
ALLOWED_HOSTS = os.environ.get(
    "ALLOWED_HOSTS", "zabbix-helper:8000,127.0.0.1:8001,localhost:8001").split(",")
MAX_MINUTES = 7 * 24 * 60
MAX_ITEMS = 10
NUMERIC = {"0", "3"}    # Zabbix value_type: 0 float, 3 unsigned
TEXT = {"1", "2", "4"}  # 1 character, 2 log, 4 text
AVAILABILITY = {"0": "unknown", "1": "available", "2": "unavailable"}

mcp = MCPServer(
    name="zabbix-helper",
    instructions="Lightweight Zabbix tools: compact host list and summarized recent history.",
)


class ZabbixError(Exception):
    pass


def _api(method: str, params: dict, auth: str):
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    req = urllib.request.Request(ZABBIX_API_URL, data=body, headers={
        "Authorization": auth, "Content-Type": "application/json-rpc"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            msg = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise ZabbixError(f"Zabbix API HTTP {exc.code}") from exc
    if "error" in msg:
        err = msg["error"]
        raise ZabbixError(f"{method}: {err.get('data') or err.get('message')}")
    return msg["result"]


def _auth(ctx: Context | None) -> str:
    headers = (ctx.headers if ctx else None) or {}
    auth = headers.get("authorization")
    if not auth:
        raise ZabbixError("Missing Authorization header (Bearer <Zabbix API token>).")
    return auth


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M:%S")


def _display(value: float, units: str) -> str:
    """Return the value as ready-to-quote text with its unit, so the model does not
    reinterpret the number (a 4B model turned a raw 0.8166 into "81.66 %")."""
    if units in ("B", "Bps"):
        size, suffix = abs(value), units[1:]
        for prefix in ("", "K", "M", "G", "T"):
            if size < 1024 or prefix == "T":
                break
            size /= 1024
        return f"{size if value >= 0 else -size:.2f} {prefix}B{suffix}"
    if units == "%":
        return f"{value:.2f} %"
    return f"{value:.4g} {units}".strip()


def _host_list(search: str | None, auth: str) -> dict:
    params = {
        "output": ["hostid", "host", "name", "status"],
        "selectInterfaces": ["type", "available"],
        "sortfield": "name",
    }
    if search:
        params["search"] = {"host": search, "name": search}
        params["searchByAny"] = True
    hosts = []
    for h in _api("host.get", params, auth):
        agent = [i for i in h.get("interfaces", []) if i.get("type") == "1"]
        hosts.append({
            "hostid": h["hostid"],
            "host": h["host"],
            "name": h["name"],
            "monitoring": "enabled" if h["status"] == "0" else "disabled",
            "agent": AVAILABILITY.get(agent[0]["available"], "unknown") if agent else "no agent",
        })
    return {"count": len(hosts), "hosts": hosts}


def _summarize(item: dict, values: list[dict]) -> dict:
    out = {
        "itemid": item["itemid"],
        "name": item.get("name_resolved") or item.get("name"),
        "key": item.get("key_"),
        "units": item.get("units", ""),
        "count": len(values),
    }
    if not values:
        out["note"] = "No data in this time range."
        return out
    values = sorted(values, key=lambda v: int(v["clock"]))
    out["first_time"] = _fmt(int(values[0]["clock"]))
    out["last_time"] = _fmt(int(values[-1]["clock"]))
    if item["value_type"] in NUMERIC:
        nums = [float(v["value"]) for v in values]
        units = item.get("units", "")
        out.update({k: _display(v, units) for k, v in (
            ("min", min(nums)), ("avg", sum(nums) / len(nums)),
            ("max", max(nums)), ("last", nums[-1]))})
    else:
        out["last_values"] = [v["value"] for v in values[-5:]]
    return out


def _history_recent(itemids: list[str], minutes: int, auth: str) -> dict:
    minutes = max(1, min(int(minutes), MAX_MINUTES))
    itemids = [str(i) for i in itemids][:MAX_ITEMS]
    now = int(time.time())
    time_from = now - minutes * 60

    items = _api("item.get", {"itemids": itemids,
                              "output": ["itemid", "name", "name_resolved", "key_", "units",
                                         "value_type"]}, auth)
    found = {i["itemid"] for i in items}
    notes = [f"Item not found or no access: {i}" for i in itemids if i not in found]

    summaries = []
    for vtype in sorted({i["value_type"] for i in items}):
        group = [i for i in items if i["value_type"] == vtype]
        if vtype not in NUMERIC | TEXT:
            notes += [f"{i['itemid']}: unsupported value type {vtype}" for i in group]
            continue
        values = _api("history.get", {
            "itemids": [i["itemid"] for i in group], "history": int(vtype),
            "time_from": time_from, "time_till": now, "output": ["itemid", "clock", "value"],
        }, auth)
        per_item: dict[str, list[dict]] = {}
        for value in values:
            per_item.setdefault(value["itemid"], []).append(value)
        summaries += [_summarize(i, per_item.get(i["itemid"], [])) for i in group]

    result = {
        "range": {"from": _fmt(time_from), "till": _fmt(now), "minutes": minutes,
                  "timezone": str(TZ)},
        "items": summaries,
    }
    if notes:
        result["notes"] = notes
    return result


@mcp.tool()
async def host_list(
    search: Annotated[str | None, Field(description="Optional part of the host name. "
                                                    "Leave empty to list all hosts.")] = None,
    ctx: Context | None = None,
) -> dict:
    """List Zabbix hosts compactly: hostid, technical name, visible name, monitoring status
    and agent availability. Use this to count hosts or to find a host ID before item_get.
    Leave search empty to get all hosts."""
    return await asyncio.to_thread(_host_list, search, _auth(ctx))


@mcp.tool()
async def history_recent(
    itemids: Annotated[list[str], Field(description="Item IDs from item_get (max 10).")],
    minutes: Annotated[int, Field(description="How many minutes back from now, 1-10080. "
                                              "Last hour = 60, last day = 1440.")] = 60,
    ctx: Context | None = None,
) -> dict:
    """Get the recent history of Zabbix items for the last N minutes, already summarized
    (count, min, avg, max, last value and time range). Use this for questions like
    "CPU usage in the last hour": it needs no Unix timestamps and no value type.
    Find item IDs first with item_get using hostids."""
    return await asyncio.to_thread(_history_recent, itemids, minutes, _auth(ctx))


if __name__ == "__main__":
    app = mcp.streamable_http_app(
        json_response=True,
        stateless_http=True,
        host="0.0.0.0",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=ALLOWED_HOSTS,
            allowed_origins=[],
        ),
    )
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
