"""zabbix-mcp-lite: a Zabbix MCP server designed for small local LLMs.

  host_list       hosts with monitoring status and agent availability
  problem_list    active problems: host, severity, start time, duration
  latest_data     current values of a host's metrics (like Monitoring > Latest data)
  metric_history  min / avg / max / last of a metric over the last N minutes
  history_recent  the same for explicit item IDs (advanced)

Models around 4B parameters cannot reliably convert dates to Unix timestamps, compute
averages, or pick the right item among the ~150 on a typical host ("CPU usage" is called
"CPU utilization", and a search for "CPU" also returns "CPU guest nice time"). This server
does all of that itself. The model only says host="Zabbix server", metric="cpu", minutes=60
and gets back a short, ready-to-quote answer with units.

It calls the Zabbix API directly and stores no token: the client's Authorization header is
forwarded as is, so Zabbix enforces the permissions of that token. Only read methods are used
(host.get, item.get, history.get, problem.get, trigger.get).
"""

import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

ZABBIX_API_URL = os.environ.get("ZABBIX_API_URL", "http://zabbix-web:8080/api_jsonrpc.php")
TZ = ZoneInfo(os.environ.get("TZ", "UTC"))
ALLOWED_HOSTS = os.environ.get(
    "ALLOWED_HOSTS", "zabbix-mcp-lite:8000,127.0.0.1:8001,localhost:8001").split(",")
MAX_MINUTES = 7 * 24 * 60
MAX_ITEMS = 10          # history_recent: item IDs per call
MAX_CONCEPT_ITEMS = 4   # metric_history: e.g. several disks, or network in + out
MAX_LATEST = 15         # latest_data: items per answer
MAX_PROBLEMS = 20
MAX_HOSTS_IN_ERRORS = 20
NUMERIC = {"0", "3"}    # Zabbix value_type: 0 float, 3 unsigned
TEXT = {"1", "2", "4"}  # 1 character, 2 log, 4 text
AVAILABILITY = {"0": "unknown", "1": "available", "2": "unavailable"}
SEVERITY = ["not classified", "information", "warning", "average", "high", "disaster"]
ITEM_FIELDS = ["itemid", "name", "name_resolved", "key_", "value_type", "units",
               "lastvalue", "lastclock", "state"]

mcp = MCPServer(
    name="zabbix-mcp-lite",
    instructions="Read-only Zabbix tools for small models. Hosts: host_list. Problems: "
                 "problem_list. Current values: latest_data. Values over time: metric_history.",
)


class ZabbixError(ToolError):
    """An expected failure; its message reaches the model (other exceptions are hidden)."""


# ------------------------------------------------------------------ Zabbix API
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


# ------------------------------------------------------------------ formatting
def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M:%S")


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m" if minutes else f"{seconds}s"


def _scaled(value: float, units: str, base: int) -> str:
    size = abs(value)
    for prefix in ("", "K", "M", "G", "T"):
        if size < base or prefix == "T":
            break
        size /= base
    return f"{size if value >= 0 else -size:.2f} {prefix}{units}"


def _display(value: float, units: str) -> str:
    """The value as ready-to-quote text with its unit, so the model does not reinterpret
    the number (a 4B model turned a raw 0.8166 into "81.66 %")."""
    if units.startswith("!"):                 # Zabbix: "!" = show the unit without a prefix
        return f"{value:.4g} {units[1:]}".strip()
    if units in ("B", "Bps"):
        return _scaled(value, units, 1024)
    if units in ("b", "bps"):
        return _scaled(value, units, 1000)
    if units == "%":
        return f"{value:.2f} %"
    if units == "uptime":
        return _duration(value)
    if units == "unixtime":
        return _fmt(int(value)) if value > 0 else "never"
    if units == "s" and 0 < abs(value) < 1:
        return f"{value * 1000:.3g} ms"
    return f"{value:.4g} {units}".strip()


def _value(item: dict, raw: str) -> str:
    if item["value_type"] in NUMERIC:
        try:
            return _display(float(raw), item.get("units", ""))
        except ValueError:
            pass
    raw = str(raw)
    return raw if len(raw) <= 120 else raw[:117] + "..."


def _name(item: dict) -> str:
    return item.get("name_resolved") or item.get("name") or item.get("key_", "")


# ------------------------------------------------------------------ hosts
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
    names = ", ".join(h["name"] for h in hosts[:MAX_HOSTS_IN_ERRORS])
    more = f" (+{len(hosts) - MAX_HOSTS_IN_ERRORS} more)" if len(hosts) > MAX_HOSTS_IN_ERRORS else ""
    return {"count": len(hosts), "summary": f"{len(hosts)} host(s): {names}{more}", "hosts": hosts}


def _resolve_host(host: str | None, auth: str) -> dict:
    """Find exactly one host by visible name, technical name or ID; explain otherwise."""
    output = ["hostid", "host", "name"]
    host = (host or "").strip()
    if not host:
        hosts = _api("host.get", {"output": output, "sortfield": "name",
                                  "limit": MAX_HOSTS_IN_ERRORS + 1}, auth)
        if len(hosts) == 1:
            return hosts[0]
        if not hosts:
            raise ZabbixError("No hosts are visible with this token.")
        raise ZabbixError("Several hosts exist, pass host. Hosts: "
                          + ", ".join(h["name"] for h in hosts[:MAX_HOSTS_IN_ERRORS]))
    if host.isdigit():
        hosts = _api("host.get", {"output": output, "hostids": [host]}, auth)
        if hosts:
            return hosts[0]
    # "Zabbix server'ın" -> "Zabbix server" (Turkish suffix after an apostrophe)
    for candidate in dict.fromkeys([host, re.split(r"['’]", host)[0].strip()]):
        if not candidate:
            continue
        hosts = _api("host.get", {"output": output, "sortfield": "name", "searchByAny": True,
                                  "search": {"host": candidate, "name": candidate}}, auth)
        exact = [h for h in hosts
                 if _fold(candidate) in (_fold(h["host"]), _fold(h["name"]))]
        if len(exact) == 1:
            return exact[0]
        if len(hosts) == 1:
            return hosts[0]
        if hosts:
            raise ZabbixError(f"Several hosts match '{host}', pass the exact name: "
                              + ", ".join(h["name"] for h in hosts[:MAX_HOSTS_IN_ERRORS]))
    known = _api("host.get", {"output": ["name"], "sortfield": "name",
                              "limit": MAX_HOSTS_IN_ERRORS}, auth)
    raise ZabbixError(f"No host matches '{host}'. Hosts: " + ", ".join(h["name"] for h in known))


def _host_items(hostid: str, auth: str) -> list[dict]:
    return _api("item.get", {"hostids": [hostid], "filter": {"status": 0},
                             "output": ITEM_FIELDS}, auth)


# ------------------------------------------------------------------ finding the item
_FOLD = str.maketrans("ıİşŞğĞüÜöÖçÇâÂîÎûÛ", "iissgguuooccaaiiuu")

STOPWORDS = set("""
a an the of on in at for to and or is are what how much many show me
usage use used using utilization utilisation util percent percentage pct
value values current currently now latest last recent average avg mean max maximum min
minimum peak history trend over during past total level status
host server sunucu sunucusu sunucunun sistem sistemi system
kullanim kullanimi kullanimini kullanma orani oran orani yuzde yuzdesi yuzdelik
deger degeri degerleri nedir nasil ne kadar son su an anlik simdi ortalama ortalamasi
en yuksek dusuk gecmis durum durumu saat saatlik gun gunluk dakika hafta
hour hours day days minute minutes week
""".split())

# Plain-language metric names -> standard Zabbix template items. Most specific first.
#   primary    words that select the concept
#   companion  words allowed next to it ("cpu load" is still the load concept)
#   keys       item key regexps (full match), best first
#   names      item name fragments, used when no key matches (other templates)
CONCEPTS = [
    {"primary": {"load", "yuk", "loadavg"}, "companion": {"cpu", "islemci"},
     "keys": [r"system\.cpu\.load\[(all|percpu)?,avg1\]", r"system\.cpu\.load(\[.*\])?"],
     "names": ["load average"]},
    {"primary": {"swap", "takas"}, "companion": {"free", "bos"},
     "keys": [r"system\.swap\.size\[,pfree\]", r"system\.swap\.pfree"],
     "names": ["free swap space in %"]},
    {"primary": {"disk", "fs", "filesystem", "storage", "depolama", "dosya", "space", "alan"},
     "companion": {"doluluk", "dolu", "kullanilan", "alani"},
     "keys": [r"vfs\.fs\.(dependent\.)?size\[.*,pused\]"],
     "names": ["space: used, in %", "used, in %"]},
    {"primary": {"network", "net", "ag", "trafik", "traffic", "bandwidth", "bant",
                 "interface", "arayuz"},
     "companion": {"genisligi", "in", "out", "gelen", "giden"},
     "keys": [r"net\.if(\.get)?\.(in|out)\[[^,\]]*\]"],
     "names": ["bits received", "bits sent"]},
    {"primary": {"memory", "mem", "ram", "bellek", "hafiza"}, "companion": set(),
     "keys": [r"vm\.memory\.(util|utilization)", r"vm\.memory\.size\[pused\]"],
     "names": ["memory utilization"]},
    {"primary": {"cpu", "islemci"}, "companion": set(),
     "keys": [r"system\.cpu\.util", r"system\.cpu\.util\[,,avg1\]"],
     "names": ["cpu utilization"]},
    {"primary": {"uptime"}, "companion": set(),
     "keys": [r"system\.uptime"], "names": ["system uptime", "uptime"]},
    {"primary": {"process", "processes", "proses", "surec"}, "companion": {"sayisi", "number"},
     "keys": [r"proc\.num"], "names": ["number of processes"]},
    {"primary": {"ping", "reachable", "erisilebilir", "erisim", "availability"},
     "companion": {"agent"},
     "keys": [r"agent\.ping", r"icmpping", r"zabbix\[host,agent,available\]"],
     "names": ["agent ping", "icmp ping"]},
]


OVERVIEW = ["cpu", "memory", "load", "disk", "swap", "network", "uptime", "processes", "ping"]


def _fold(text: str) -> str:
    return text.translate(_FOLD).lower()


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", _fold(text)) if t]


_SOFTEN = {"k": "g", "p": "b", "t": "d", "c": "c"}  # Turkish: bellek -> bellegi, trafik -> trafigi


def _word_match(tok: str, word: str) -> bool:
    """Tolerant word match for suffixes: 'cpus' ~ 'cpu', 'bellegi' ~ 'bellek'."""
    if tok == word:
        return True
    if min(len(tok), len(word)) < 3:
        return False
    stems = {word, word[:-1] + _SOFTEN.get(word[-1], word[-1])}
    return any(tok.startswith(s) for s in stems) or word.startswith(tok)


# Common Turkish words -> the English words used in Zabbix item names
TRANSLATE = {
    "kullanilabilir": "available", "musait": "available", "bos": "free", "toplam": "total",
    "bellek": "memory", "hafiza": "memory", "islemci": "cpu", "gelen": "received",
    "giden": "sent", "hata": "errors", "bosta": "idle", "kullanici": "user",
    "bekleme": "iowait", "yazma": "write", "okuma": "read", "surec": "processes",
}


def _query_tokens(query: str, ignore: set[str]) -> list[str]:
    out = []
    for tok in _tokens(query):
        if tok in STOPWORDS or tok in ignore:
            continue
        out.append(next((en for tr, en in TRANSLATE.items() if _word_match(tok, tr)), tok))
    return out


def _has_data(item: dict) -> bool:
    return item.get("lastclock", "0") not in ("0", "", None) and item.get("state") != "1"


def _concept_rank(item: dict, concept: dict) -> tuple | None:
    key, name = item["key_"], _fold(_name(item))
    for i, pattern in enumerate(concept["keys"]):
        if re.fullmatch(pattern, key):
            # among several file systems prefer "/"
            return (i, 0 if re.search(r"\[/,", key) else 1)
    for i, fragment in enumerate(concept["names"]):
        if fragment in name:
            return (len(concept["keys"]) + i, 1)
    return None


def _match_items(items: list[dict], query: str, ignore: set[str]) -> tuple[str, list[dict]]:
    """Return ("concept" | "words", items best first) for a plain-language metric name."""
    toks = _query_tokens(query, ignore)
    for concept in CONCEPTS:
        primary = [t for t in toks if any(_word_match(t, w) for w in concept["primary"])]
        if not primary:
            continue
        allowed = concept["primary"] | concept["companion"]
        leftover = [t for t in toks if not any(_word_match(t, w) for w in allowed)]
        if leftover:
            break  # more specific than the concept ("cpu iowait"): match words instead
        ranked = []
        for item in items:
            rank = _concept_rank(item, concept)
            if rank is not None:
                ranked.append(((rank[0], not _has_data(item), rank[1], _name(item)), item))
        if ranked:
            ranked.sort(key=lambda r: r[0])
            best = ranked[0][0][0]
            return "concept", [item for (rank, *_), item in ranked if rank == best]
        break
    if not toks:
        return "words", []
    scored = []
    for item in items:
        words = _tokens(_name(item) + " " + item["key_"])
        hits = sum(any(_word_match(t, w) for w in words) for t in toks)
        if hits:
            scored.append(((-hits, not _has_data(item), item["value_type"] not in NUMERIC,
                            len(_name(item)), _name(item)), item))
    scored.sort(key=lambda s: s[0])
    return "words", [item for _, item in scored]


# ------------------------------------------------------------------ history
def _summarize(item: dict, values: list[dict], minutes: int) -> dict:
    name = _name(item)
    out = {"itemid": item["itemid"], "name": name, "key": item.get("key_"),
           "count": len(values)}
    if not values:
        out["summary"] = f"{name}: no data in the last {minutes} minutes."
        return out
    values = sorted(values, key=lambda v: int(v["clock"]))
    out["first_time"] = _fmt(int(values[0]["clock"]))
    out["last_time"] = _fmt(int(values[-1]["clock"]))
    if item["value_type"] in NUMERIC:
        nums = [float(v["value"]) for v in values]
        units = item.get("units", "")
        stats = {"min": min(nums), "avg": sum(nums) / len(nums), "max": max(nums),
                 "last": nums[-1]}
        out.update({k: _display(v, units) for k, v in stats.items()})
        out["summary"] = (f"{name}: average {out['avg']}, maximum {out['max']}, "
                          f"minimum {out['min']}, last {out['last']} "
                          f"({len(nums)} values, {out['first_time']} - {out['last_time']}).")
    else:
        out["last_values"] = [v["value"][:200] for v in values[-5:]]
        out["summary"] = f"{name}: {len(values)} values; latest: {values[-1]['value'][:200]}"
    return out


def _history(items: list[dict], minutes: int, auth: str) -> dict:
    minutes = max(1, min(int(minutes), MAX_MINUTES))
    now = int(time.time())
    time_from = now - minutes * 60
    summaries, notes = {}, []
    for vtype in sorted({i["value_type"] for i in items}):
        group = [i for i in items if i["value_type"] == vtype]
        if vtype not in NUMERIC | TEXT:
            notes += [f"{_name(i)}: unsupported value type {vtype}" for i in group]
            continue
        values = _api("history.get", {
            "itemids": [i["itemid"] for i in group], "history": int(vtype),
            "time_from": time_from, "time_till": now, "output": ["itemid", "clock", "value"],
        }, auth)
        per_item: dict[str, list[dict]] = {}
        for value in values:
            per_item.setdefault(value["itemid"], []).append(value)
        for i in group:
            summaries[i["itemid"]] = _summarize(i, per_item.get(i["itemid"], []), minutes)
    result = {
        "range": {"from": _fmt(time_from), "till": _fmt(now), "minutes": minutes,
                  "timezone": str(TZ)},
        "items": [summaries[i["itemid"]] for i in items if i["itemid"] in summaries],
    }
    if notes:
        result["notes"] = notes
    return result


def _metric_history(host: str | None, metric: str, minutes: int, auth: str) -> dict:
    h = _resolve_host(host, auth)
    items = _host_items(h["hostid"], auth)
    kind, matches = _match_items(items, metric, set(_tokens(h["host"] + " " + h["name"])))
    if not matches:
        raise ZabbixError(
            f"No item on host '{h['name']}' matches '{metric}'. Try a plain word such as "
            "cpu, memory, disk, load, network, swap or uptime, or call latest_data with a "
            "search word to see item names.")
    chosen = matches[:MAX_CONCEPT_ITEMS] if kind == "concept" else matches[:1]
    result = {"host": h["name"], **_history(chosen, minutes, auth)}
    others = [_name(i) for i in matches[len(chosen):len(chosen) + 5]]
    if others:
        result["other_matches"] = others
    return result


def _history_recent(itemids: list[str], minutes: int, auth: str) -> dict:
    itemids = [str(i) for i in itemids][:MAX_ITEMS]
    items = _api("item.get", {"itemids": itemids, "output": ITEM_FIELDS}, auth)
    result = _history(items, minutes, auth)
    found = {i["itemid"] for i in items}
    missing = [f"Item not found or no access: {i}" for i in itemids if i not in found]
    if missing:
        result["notes"] = result.get("notes", []) + missing
    return result


# ------------------------------------------------------------------ latest data
def _latest(item: dict) -> dict:
    return {"itemid": item["itemid"], "name": _name(item),
            "value": _value(item, item.get("lastvalue", "")) if _has_data(item) else "no data",
            "updated": _fmt(int(item["lastclock"])) if _has_data(item) else "never"}


def _latest_data(host: str | None, search: str | None, auth: str) -> dict:
    h = _resolve_host(host, auth)
    items = _host_items(h["hostid"], auth)
    if search and search.strip():
        _, matches = _match_items(items, search, set(_tokens(h["host"] + " " + h["name"])))
        if not matches:
            raise ZabbixError(f"No item on host '{h['name']}' matches '{search}'. Leave "
                              "search empty for an overview of the main metrics.")
    else:  # overview: the best item of each common metric
        matches, seen = [], set()
        for word in OVERVIEW:
            kind, found = _match_items(items, word, set())
            if kind != "concept":
                continue
            for item in found[:2]:
                if item["itemid"] not in seen:
                    seen.add(item["itemid"])
                    matches.append(item)
    result = {"host": h["name"], "items": [_latest(i) for i in matches[:MAX_LATEST]]}
    if len(matches) > MAX_LATEST:
        result["more"] = f"{len(matches) - MAX_LATEST} more items match; use a narrower search."
    return result


# ------------------------------------------------------------------ problems
def _problem_list(host: str | None, auth: str) -> dict:
    params = {"output": ["eventid", "objectid", "name", "severity", "clock", "acknowledged",
                         "suppressed"],
              "source": 0, "object": 0, "sortfield": ["eventid"], "sortorder": "DESC"}
    scope = ""
    if host and host.strip():
        h = _resolve_host(host, auth)
        params["hostids"] = [h["hostid"]]
        scope = f" on {h['name']}"
    problems = _api("problem.get", params, auth)
    hosts_by_trigger = {}
    if problems:
        triggers = _api("trigger.get", {"triggerids": list({p["objectid"] for p in problems}),
                                        "output": ["triggerid"], "selectHosts": ["name"]}, auth)
        hosts_by_trigger = {t["triggerid"]: ", ".join(x["name"] for x in t.get("hosts", []))
                            for t in triggers}
    problems.sort(key=lambda p: (-int(p["severity"]), -int(p["clock"])))
    now = time.time()
    rows = [{
        "name": p["name"],
        "host": hosts_by_trigger.get(p["objectid"], "?"),
        "severity": SEVERITY[int(p["severity"])],
        "since": _fmt(int(p["clock"])),
        "duration": _duration(now - int(p["clock"])),
        "acknowledged": p["acknowledged"] == "1",
    } for p in problems[:MAX_PROBLEMS]]
    if not problems:
        summary = f"There are no active problems{scope}."
    else:
        counts = {}
        for p in problems:
            counts[SEVERITY[int(p["severity"])]] = counts.get(SEVERITY[int(p["severity"])], 0) + 1
        summary = (f"{len(problems)} active problem(s){scope}: "
                   + ", ".join(f"{n} {s}" for s, n in counts.items()) + ".")
    result = {"count": len(problems), "summary": summary, "problems": rows}
    if len(problems) > MAX_PROBLEMS:
        result["more"] = f"{len(problems) - MAX_PROBLEMS} more, less severe or older."
    return result


# ------------------------------------------------------------------ MCP tools
HostParam = Annotated[str | None, Field(
    description="Host name as shown by host_list, for example 'Zabbix server'. "
                "May be left empty when there is only one host.")]


@mcp.tool()
async def host_list(
    search: Annotated[str | None, Field(description="Optional part of the host name. "
                                                    "Leave empty to list all hosts.")] = None,
    ctx: Context | None = None,
) -> dict:
    """List the monitored hosts: how many, their names, monitoring status and agent
    availability. Use for "how many hosts", "which hosts" or to get a host's exact name."""
    return await asyncio.to_thread(_host_list, search, _auth(ctx))


@mcp.tool()
async def problem_list(host: HostParam = None, ctx: Context | None = None) -> dict:
    """Active (unresolved) problems, most severe first, with host, severity, start time and
    duration. Use for "are there any problems?" or "what is wrong on host X?".
    Leave host empty to check all hosts."""
    return await asyncio.to_thread(_problem_list, host, _auth(ctx))


@mcp.tool()
async def latest_data(
    host: HostParam = None,
    search: Annotated[str | None, Field(
        description="Plain words for the metric, for example 'cpu', 'memory', 'disk', "
                    "'network', 'uptime'. Leave empty for an overview of the main metrics.")] = None,
    ctx: Context | None = None,
) -> dict:
    """Current (latest) values of a host's metrics, like Monitoring > Latest data in Zabbix.
    Use for "what is the CPU usage now?" or "how is host X doing?"."""
    return await asyncio.to_thread(_latest_data, host, search, _auth(ctx))


@mcp.tool()
async def metric_history(
    metric: Annotated[str, Field(
        description="The metric in plain words, for example 'cpu', 'memory', 'disk', 'load', "
                    "'network', 'swap'. The server finds the matching Zabbix item.")],
    host: HostParam = None,
    minutes: Annotated[int, Field(description="How many minutes back from now, 1-10080. "
                                              "Last hour = 60, last 24 hours = 1440.")] = 60,
    ctx: Context | None = None,
) -> dict:
    """Minimum, average, maximum and last value of a metric over the last N minutes, with
    units and a ready-to-quote summary. Use for questions like "CPU usage in the last hour"
    (metric='cpu', minutes=60) or "memory over the last 24 hours" (metric='memory',
    minutes=1440). No timestamps or item IDs needed."""
    return await asyncio.to_thread(_metric_history, host, metric, minutes, _auth(ctx))


@mcp.tool()
async def history_recent(
    itemids: Annotated[list[str], Field(description="Item IDs, for example from latest_data "
                                                    "(max 10).")],
    minutes: Annotated[int, Field(description="How many minutes back from now, 1-10080. "
                                              "Last hour = 60, last day = 1440.")] = 60,
    ctx: Context | None = None,
) -> dict:
    """Advanced: minimum, average, maximum and last value for explicit item IDs over the
    last N minutes. Prefer metric_history, which finds the item by itself."""
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
