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
from datetime import datetime, timedelta
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
MAX_EVENTS = 1000        # problem_list with a period: events read
MAX_PERIOD_DAYS = 31
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
minimum peak history trend over during past level
host server sunucu sunucusu sunucunun sistem sistemi system
kullanim kullanimi kullanimini kullanma orani oran orani yuzde yuzdesi yuzdelik
deger degeri degerleri nedir nasil ne kadar son su an anlik simdi ortalama ortalamasi
en yuksek dusuk gecmis durum durumu saat saatlik gun gunluk dakika hafta
hour hours day days minute minutes week
which highest lowest top most least busiest hosts hangi hangisi hangisinde hangisinin fazla
hostta hostda hostun hostunda hostlar hostlarda hostlardan hostlarin sunucuda sunucular
sunucularda sunuculardan sunucularin
""".split())

# Plain-language metric names -> standard Zabbix template items. Most specific first.
#   primary    words that select the concept
#   companion  words allowed next to it ("cpu load" is still the load concept)
#   keys       item key regexps (full match), best first
#   names      item name fragments, used when no key matches (other templates)
#   family     item keys of the same kind; preferred when the request is more specific
#              ("disk free" -> a vfs.fs item, not "Free swap space")
CONCEPTS = [
    {"primary": {"load", "yuk", "loadavg"}, "companion": {"cpu", "islemci"},
     "keys": [r"system\.cpu\.load\[(all|percpu)?,avg1\]", r"system\.cpu\.load(\[.*\])?"],
     "names": ["load average"], "family": r"system\.cpu\.load.*"},
    {"primary": {"swap", "takas"}, "companion": {"free", "bos"},
     "keys": [r"system\.swap\.size\[,pfree\]", r"system\.swap\.pfree"],
     "names": ["free swap space in %"], "family": r"system\.swap\..*"},
    {"primary": {"disk", "fs", "filesystem", "storage", "depolama", "dosya", "space", "alan"},
     "companion": {"doluluk", "dolu", "kullanilan", "alani"},
     "keys": [r"vfs\.fs\.(dependent\.)?size\[.*,pused\]"],
     "names": ["space: used, in %", "used, in %"], "family": r"vfs\.(fs|dev)\..*",
     "fullest_first": True},
    {"primary": {"network", "net", "ag", "trafik", "traffic", "bandwidth", "bant",
                 "interface", "arayuz"},
     "companion": {"genisligi", "in", "out", "gelen", "giden"},
     "keys": [r"net\.if(\.get)?\.(in|out)\[[^,\]]*\]"],
     "names": ["bits received", "bits sent"], "family": r"net\.if\..*"},
    {"primary": {"memory", "mem", "ram", "bellek", "hafiza"}, "companion": set(),
     "keys": [r"vm\.memory\.(util|utilization)", r"vm\.memory\.size\[pused\]"],
     "names": ["memory utilization"], "family": r"vm\.memory\..*"},
    {"primary": {"cpu", "islemci"}, "companion": set(),
     "keys": [r"system\.cpu\.util", r"system\.cpu\.util\[,,avg1\]"],
     "names": ["cpu utilization"], "family": r"system\.cpu\..*"},
    {"primary": {"uptime"}, "companion": set(),
     "keys": [r"system\.uptime"], "names": ["system uptime", "uptime"],
     "family": r"system\.(uptime|boottime)"},
    {"primary": {"process", "processes", "proses", "surec"}, "companion": {"sayisi", "number"},
     "keys": [r"proc\.num"], "names": ["number of processes"], "family": r"proc\..*"},
    {"primary": {"ping", "reachable", "erisilebilir", "erisim", "availability"},
     "companion": {"agent"},
     "keys": [r"agent\.ping", r"icmpping", r"zabbix\[host,agent,available\]"],
     "names": ["agent ping", "icmp ping"],
     "family": r"agent\.ping|icmpping.*|zabbix\[host,agent,available\]"},
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


# Words that mean the same in item names and keys ("free" disk space is "Space: Available")
SYNONYMS = {"free": ("available", "pfree"), "available": ("free", "pavailable")}


def _token_hits(tok: str, words: list[str]) -> bool:
    candidates = (tok, *SYNONYMS.get(tok, ()))
    return any(_word_match(c, w) for c in candidates for w in words)


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


# A file system named in the question: "/", "/var/log", "C:" ("/var'ın doluluğu", "disk C:")
_MOUNT = re.compile(r"(?<!\S)(/[^\s'’,;?!]*|[A-Za-z]:)(?:['’]\w*)?(?=[\s\\,;?!.]|$)")
_FS_KEY = re.compile(r"vfs\.fs\.(?!get\b|discovery\b)[\w.]*\[\s*\"?([^,\"\]]*)")
_DISK_WORDS = next(c["primary"] for c in CONCEPTS if "disk" in c["primary"])


def _norm_mount(mount: str) -> str:
    mount = mount.rstrip(".")
    if re.fullmatch(r"[A-Za-z]:", mount):
        return mount.upper()
    return mount.rstrip("/") or "/"


def _mount_of(item: dict) -> str | None:
    m = _FS_KEY.match(item["key_"])
    return _norm_mount(m.group(1)) if m and m.group(1) else None


def _mounts(items: list[dict]) -> list[str]:
    return sorted({m for m in map(_mount_of, items) if m})


def _number(item: dict) -> float:
    try:
        return float(item.get("lastvalue") or 0)
    except ValueError:
        return 0.0


def _mount_hint(items: list[dict], query: str) -> str:
    if not _MOUNT.search(query):
        return ""
    mounts = _mounts(items)
    if not mounts:
        return "This host has no file system items. "
    return f"File systems on this host: {', '.join(mounts[:MAX_HOSTS_IN_ERRORS])}. "


def _match_items(items: list[dict], query: str, ignore: set[str]) -> tuple[str, list[dict]]:
    """Return ("concept" | "words", items best first) for a plain-language metric name."""
    mounts = {_norm_mount(m) for m in _MOUNT.findall(query)}
    if mounts:  # only that file system; "/var" alone means its disk usage
        items = [i for i in items if _mount_of(i) in mounts]
        query = _MOUNT.sub(" ", query)
    toks = _query_tokens(query, ignore)
    if mounts and not any(_word_match(t, w) for t in toks for w in _DISK_WORDS):
        toks = ["disk", *toks]
    family = None
    for concept in CONCEPTS:
        primary = [t for t in toks if any(_word_match(t, w) for w in concept["primary"])]
        if not primary:
            continue
        family = concept["family"]
        allowed = concept["primary"] | concept["companion"]
        leftover = [t for t in toks if not any(_word_match(t, w) for w in allowed)]
        # a leftover word that is in no item name ("hostta cpu") does not make it more specific
        leftover = [t for t in leftover
                    if any(_token_hits(t, _tokens(_name(i) + " " + i["key_"])) for i in items)]
        if leftover:
            break  # more specific than the concept ("cpu iowait"): match words instead
        ranked = []
        for item in items:
            rank = _concept_rank(item, concept)
            if rank is not None:  # several disks: "/" first, then the fullest
                fullest = -_number(item) if concept.get("fullest_first") else 0
                ranked.append(((rank[0], not _has_data(item), rank[1], fullest, _name(item)),
                               item))
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
        hits = sum(_token_hits(t, words) for t in toks)
        if hits and family and re.fullmatch(family, item["key_"]):
            hits += 1
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
            f"No item on host '{h['name']}' matches '{metric}'. " + _mount_hint(items, metric)
            + "Try a plain word such as cpu, memory, disk, load, network, swap or uptime, or "
            "call latest_data with a search word to see item names.")
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


def _literal(pattern: str) -> str:
    """The fixed start of a key regexp, for example "vfs.fs." for the disk keys."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            c, i = pattern[i + 1], i + 2
        elif c in "()[]{}?*+|.^$":
            break
        else:
            i += 1
        if i < len(pattern) and pattern[i] in "?*{":  # optional character: stop before it
            break
        out.append(c)
    return "".join(out)


def _search_terms(query: str) -> list[str]:
    """Key and name fragments for one item.get over all hosts; _match_items then picks."""
    terms = {"vfs.fs."} if _MOUNT.search(query) else set()
    for tok in _query_tokens(_MOUNT.sub(" ", query), set()):
        if len(tok) >= 3:
            terms.update((tok, *SYNONYMS.get(tok, ())))
        for concept in CONCEPTS:
            if any(_word_match(tok, w) for w in concept["primary"]):
                terms.update(_literal(k) for k in concept["keys"])
                terms.update(concept["names"])
    return sorted(t for t in terms if len(t) >= 3)


def _compare_hosts(search: str, hosts: list[dict], auth: str) -> dict:
    """One metric on every monitored host, highest first ("which host has the most CPU?")."""
    terms = _search_terms(search)
    if not terms:
        raise ZabbixError(f"'{search}' does not name a metric. Try cpu, memory, disk, load, "
                          "network, swap or uptime.")
    items = _api("item.get", {"hostids": [h["hostid"] for h in hosts], "monitored": True,
                              "output": ITEM_FIELDS + ["hostid"], "searchByAny": True,
                              "search": {"key_": terms, "name": terms}}, auth)
    by_host: dict[str, list[dict]] = {}
    for item in items:
        by_host.setdefault(item["hostid"], []).append(item)
    rows, missing = [], []
    for h in hosts:
        kind, matches = _match_items(by_host.get(h["hostid"], []), search, set())
        numeric = [i for i in matches if i["value_type"] in NUMERIC and _has_data(i)]
        if not numeric:
            missing.append(h["name"])
            continue
        # several disks or interfaces on one host: its highest one counts
        best = max(numeric, key=_number) if kind == "concept" else numeric[0]
        rows.append((_number(best), {"host": h["name"], **_latest(best)}))
    if not rows:
        raise ZabbixError(f"No host has a numeric item with data matching '{search}'. "
                          "Pass a host, or call latest_data with a host and a search word "
                          "to see item names.")
    rows.sort(key=lambda r: -r[0])
    top, low = rows[0][1], rows[-1][1]
    summary = f"Highest: {top['host']} ({top['name']} = {top['value']})"
    if len(rows) > 1:
        summary += f"; lowest: {low['host']} ({low['name']} = {low['value']})"
    summary += f". {len(rows)} host(s) compared"
    summary += f", {len(missing)} without this metric." if missing else "."
    result = {"metric": search, "summary": summary,
              "hosts": [row for _, row in rows[:MAX_LATEST]]}
    if len(rows) > MAX_LATEST:
        result["more"] = f"{len(rows) - MAX_LATEST} more hosts with lower values."
    if missing:
        result["without_this_metric"] = missing[:MAX_HOSTS_IN_ERRORS]
    return result


def _latest_data(host: str | None, search: str | None, auth: str) -> dict:
    if not (host and host.strip()) and search and search.strip():
        hosts = _api("host.get", {"output": ["hostid", "name"], "monitored_hosts": True,
                                  "sortfield": "name"}, auth)
        if len(hosts) > 1:  # no host but a metric: compare all hosts
            return _compare_hosts(search, hosts, auth)
    h = _resolve_host(host, auth)
    items = _host_items(h["hostid"], auth)
    if search and search.strip():
        _, matches = _match_items(items, search, set(_tokens(h["host"] + " " + h["name"])))
        if not matches:
            raise ZabbixError(f"No item on host '{h['name']}' matches '{search}'. "
                              + _mount_hint(items, search) + "Leave search empty for an "
                              "overview of the main metrics.")
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


# ------------------------------------------------------------------ past problems
NIGHT = (20, 8)  # "last night" is 20:00 - 08:00
_UNITS = (  # exact words, word prefixes, seconds
    ({"m", "dk"}, ("min", "dakika"), 60),
    ({"h", "sa", "hr"}, ("hour", "saat", "hrs"), 3600),
    ({"d", "g"}, ("day", "gun"), 86400),
    ({"w", "wk"}, ("week", "hafta"), 7 * 86400),
)
_NOW = {"now", "current", "currently", "active", "simdi", "aktif", "su an", "su anda"}


def _unit(word: str, exact: bool = True) -> int | None:
    for words, prefixes, seconds in _UNITS:
        if (exact and word in words) or word.startswith(prefixes):
            return seconds
    return None


def _period(text: str, now: datetime) -> tuple[datetime, datetime] | None:
    """A time range from the user's words: "last night", "dün", "son 6 saat", "22:00", ...
    None means "now" (the active problems)."""
    raw = _fold(text).strip()
    words = re.findall(r"[a-z]+", raw)
    has = lambda *phrases: any(re.search(rf"\b{p}\b", raw) for p in phrases)  # noqa: E731
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ t]+(\d{1,2})[:.](\d{2}))?", raw)
    if m:  # "2026-11-06", "2026-11-06 22:00"
        y, mo, d, hh, mm = (int(x or 0) for x in m.groups())
        return datetime(y, mo, d, hh, mm, tzinfo=now.tzinfo), now
    m = re.search(r"(?<![\d.:])(\d{1,2})[:.](\d{2})(?![\d.:])", raw)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:  # "22:00", "dün 22:00'den beri"
        start = today.replace(hour=int(m.group(1)), minute=int(m.group(2)))
        if start > now or (has("dun", "yesterday") and start.date() == today.date()):
            start -= timedelta(days=1)
        return start, now
    m = re.search(r"(\d+)\s*([a-z]+)", raw)
    if m and _unit(m.group(2)):  # "son 6 saat", "last 3 days", "24h"
        return now - timedelta(seconds=int(m.group(1)) * _unit(m.group(2))), now
    if has("hafta ?sonu", "weekend"):
        saturday = today - timedelta(days=(today.weekday() - 5) % 7)
        return saturday, min(saturday + timedelta(days=2), now)
    monday = today - timedelta(days=today.weekday())
    if has("gecen hafta", "last week", "previous week"):
        return monday - timedelta(days=7), monday
    if has("bu hafta", "this week"):
        return monday, now
    if has("gece", "night", "overnight", "tonight"):
        if now.hour < NIGHT[1]:  # still the same night
            return yesterday.replace(hour=NIGHT[0]), now
        if now.hour >= NIGHT[0] and not has("dun", "yesterday", "last"):
            return today.replace(hour=NIGHT[0]), now
        return yesterday.replace(hour=NIGHT[0]), today.replace(hour=NIGHT[1])
    if has("dun", "yesterday"):
        return yesterday, today
    if has("bugun", "today"):
        return today, now
    if has("sabah", "morning"):
        day = today if now.hour >= 6 else yesterday
        return day.replace(hour=6), min(day.replace(hour=12), now)
    for word in words:  # "last hour", "son gün"
        if len(word) >= 3 and _unit(word, exact=False):
            return now - timedelta(seconds=_unit(word, exact=False)), now
    if raw in _NOW or any(w in _NOW for w in words):
        return None
    raise ZabbixError(f"Could not read the period '{text}'. Use words such as 'last night', "
                      "'yesterday', 'today', 'last 6 hours', '3 days' or a time like '22:00'.")


def _problem_history(host: str | None, start: datetime, end: datetime, auth: str) -> dict:
    """Problems that started in [start, end], resolved or not, grouped by host and name."""
    oldest = datetime.now(start.tzinfo) - timedelta(days=MAX_PERIOD_DAYS)
    start = max(start, oldest)
    if end <= start:
        raise ZabbixError("The period is empty or in the future.")
    since, till = _fmt(int(start.timestamp())), _fmt(int(end.timestamp()))
    params = {"source": 0, "object": 0, "value": 1, "time_from": int(start.timestamp()),
              "time_till": int(end.timestamp()), "selectHosts": ["name"],
              "output": ["eventid", "name", "severity", "clock", "r_eventid", "acknowledged"],
              "sortfield": ["clock", "eventid"], "sortorder": "DESC", "limit": MAX_EVENTS}
    scope = ""
    if host and host.strip():
        h = _resolve_host(host, auth)
        params["hostids"] = [h["hostid"]]
        scope = f" on {h['name']}"
    try:
        events = _api("event.get", params, auth)
    except ZabbixError as e:
        if "permission" not in str(e).lower():
            raise
        raise ZabbixError("Past problems need the event.get API method, which this token's "
                          "Zabbix role does not allow. Without a period, problem_list still "
                          "shows the active problems.") from e
    recovery = [e["r_eventid"] for e in events if e["r_eventid"] != "0"]
    ended = {}
    if recovery:
        ended = {r["eventid"]: int(r["clock"]) for r in _api(
            "event.get", {"eventids": recovery, "output": ["eventid", "clock"]}, auth)}
    # problems that began earlier and are still going on
    before = {"source": 0, "object": 0, "time_till": params["time_from"], "output": ["eventid"]}
    if "hostids" in params:
        before["hostids"] = params["hostids"]
    earlier = _api("problem.get", before, auth)
    now = time.time()
    groups: dict[tuple, dict] = {}
    for e in sorted(events, key=lambda e: (int(e["clock"]), int(e["eventid"]))):
        hosts = ", ".join(x["name"] for x in e.get("hosts", [])) or "?"
        g = groups.setdefault((hosts, e["name"]), {"host": hosts, "problem": e["name"],
                                                   "severity": 0, "times": 0, "longest": 0.0})
        begin, finish = int(e["clock"]), ended.get(e["r_eventid"])
        g["severity"] = max(g["severity"], int(e["severity"]))
        g["times"] += 1
        g.setdefault("first", begin)
        g["last"], g["end"] = begin, finish
        g["longest"] = max(g["longest"], (finish or now) - begin)
    rows = []
    for g in groups.values():
        row = {"host": g["host"], "problem": g["problem"], "severity": SEVERITY[g["severity"]],
               "started": _fmt(g["first"])}
        if g["times"] == 1:
            row["ended"] = _fmt(g["end"]) if g["end"] else "still active"
            row["duration"] = _duration(g["longest"])
        else:
            row.update({"times": g["times"], "last_started": _fmt(g["last"]),
                        "longest": _duration(g["longest"]),
                        "now": "still active" if g["end"] is None else "resolved"})
        rows.append((g, row))
    if len(rows) > MAX_PROBLEMS:  # keep the most severe, then show them in time order
        rows = sorted(rows, key=lambda r: (-r[0]["severity"], r[0]["first"]))[:MAX_PROBLEMS]
    rows.sort(key=lambda r: r[0]["first"])
    if not events:
        summary = f"No problems started{scope} between {since} and {till}."
    else:
        active = sum(1 for g in groups.values() if g["end"] is None)
        worst = max(groups.values(), key=lambda g: (g["severity"], g["longest"]))
        hosts = {g["host"] for g in groups.values()}
        summary = (f"{len(events)} problem event(s){scope} between {since} and {till}: "
                   f"{len(groups)} different problem(s) on {len(hosts)} host(s), "
                   f"{len(groups) - active} resolved, {active} still active. Most severe: "
                   f"{SEVERITY[worst['severity']]} '{worst['problem']}' on {worst['host']} at "
                   f"{_fmt(worst['first'])}"
                   + (f", {worst['times']} times." if worst["times"] > 1 else
                      f", lasted {_duration(worst['longest'])}." if worst["end"] else
                      ", still active."))
    if earlier:
        summary += (f" {len(earlier)} problem(s) that started before this period are still "
                    "active (problem_list without a period shows them).")
    result = {"period": {"from": since, "till": till, "timezone": str(TZ)}, "summary": summary,
              "problems": [row for _, row in rows]}
    if len(groups) > MAX_PROBLEMS:
        result["more"] = f"{len(groups) - MAX_PROBLEMS} more, less severe problems."
    if len(events) == MAX_EVENTS:
        result["note"] = f"Only the latest {MAX_EVENTS} events were read; use a shorter period."
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
async def problem_list(
    host: HostParam = None,
    period: Annotated[str | None, Field(
        description="Leave empty for the problems active now. For the past, the time in the "
                    "user's own words, any language: 'last night', 'dün gece', 'yesterday', "
                    "'today', 'son 6 saat', '3 days'.")] = None,
    ctx: Context | None = None,
) -> dict:
    """Problems with host, severity, start time and duration. Without a period: the active
    problems now, most severe first ("are there any problems?", "what is wrong on host X?").
    With a period: what happened then, resolved or not, in time order ("what happened last
    night?"). Leave host empty to check all hosts."""
    auth = _auth(ctx)
    if period and period.strip():
        span = _period(period, datetime.now(TZ))
        if span:
            return await asyncio.to_thread(_problem_history, host, *span, auth)
    return await asyncio.to_thread(_problem_list, host, auth)


@mcp.tool()
async def latest_data(
    host: HostParam = None,
    search: Annotated[str | None, Field(
        description="The metric in the user's own words, any language, for example 'cpu', "
                    "'free memory', 'disk', 'disk /var', 'ağ trafiği'. Leave empty for an overview "
                    "of the main metrics.")] = None,
    ctx: Context | None = None,
) -> dict:
    """Current (latest) values of a host's metrics, like Monitoring > Latest data in Zabbix.
    Use for "what is the CPU usage now?" or "how is host X doing?". Leave host empty and give
    a search to compare all hosts, highest first: "which host has the highest CPU?"."""
    return await asyncio.to_thread(_latest_data, host, search, _auth(ctx))


@mcp.tool()
async def metric_history(
    metric: Annotated[str, Field(
        description="The metric in the user's own words, any language, for example 'cpu', "
                    "'memory', 'free memory', 'disk', 'disk /var', 'ağ trafiği'. The server finds "
                    "the matching Zabbix item.")],
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
