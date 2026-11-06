#!/usr/bin/env python3
"""Benchmark: can a local LLM answer Zabbix questions through MCP tools?

Each question goes to an OpenAI-compatible endpoint (LM Studio) together with one of two
tool sets. The tool calls are executed for real against the MCP servers, and the final
answer is graded against ground truth read from the Zabbix API right after the run.

Tool sets (tool names follow LibreChat's "<tool>_mcp_<server>" convention):
  official  official Zabbix MCP server: host_get, item_get, problem_get, trigger_get, history_get
  helper    zabbix-helper host_list + history_recent, plus item_get, problem_get, trigger_get

Outcomes:
  PASS       answer contains the expected values
  WRONG      answer given, but the expected values are missing
  NO_ANSWER  empty final answer (e.g. the model stopped after thinking)
  NO_TOOL    answered without calling any tool (a guess)
  TIMEOUT    over the per-question time limit (runaway thinking or tool loops)
  ERROR      request failed (e.g. context window exceeded)

Usage:
  python3 -I bench/run_bench.py --model qwen3.5-4b-mlx --toolset helper --runs 3
  python3 -I bench/run_bench.py --report bench/results/*.jsonl
"""

import argparse
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

QUESTIONS = [
    {"id": "host_count", "text": "Zabbix'te kaç host izleniyor? Host adlarını da yaz."},
    {"id": "problems", "text": "Şu an aktif problem var mı?"},
    {"id": "cpu_1h", "text": "Zabbix server'ın son 1 saatteki CPU kullanımı nasıl? "
                             "Ortalama ve en yüksek değeri söyle."},
    {"id": "mem_24h", "text": "Zabbix server'ın son 24 saatteki bellek kullanımı (memory "
                              "utilization) nasıl? Ortalama ve en yüksek değeri söyle."},
]

PROMPT_COMMON_HEAD = ("Sen bir Zabbix asistanısın. Şu anki tarih ve saat: {now}.\n"
                      "- Zabbix ile ilgili her soruda önce araç çağır, asla tahmin etme.\n")
PROMPT_COMMON_TAIL = ("- Trigger tanımlarını problem gibi sunma; aktif problemler için problem_get kullan.\n"
                      "- Cevapları kısa ve Türkçe ver.")
PROMPTS = {
    "official": PROMPT_COMMON_HEAD
    + "- item_get'i her zaman hostids ile çağır; host ID'sini bilmiyorsan önce host_get kullan.\n"
    + "- history_get'te time_from ve time_till Unix zaman damgasıdır; şu anki zamandan hesapla.\n"
    + PROMPT_COMMON_TAIL,
    "helper": PROMPT_COMMON_HEAD
    + "- Host saymak, listelemek veya host ID bulmak için host_list kullan. item_get'i her zaman hostids ile çağır.\n"
    + "- Geçmiş değer sorularında (son 1 saat, son 1 gün gibi) history_recent kullan; zaman damgası hesaplama.\n"
    + PROMPT_COMMON_TAIL,
}
TOOLSETS = {
    "official": {"zabbix": ["host_get", "item_get", "problem_get", "trigger_get", "history_get"]},
    "helper": {"zabbix": ["item_get", "problem_get", "trigger_get"],
               "zabbix-helper": ["host_list", "history_recent"]},
}
NEGATIONS = ("yok", "bulunmuyor", "bulunmamakta", "hayır", "no active", "none", "there are no")


# ------------------------------------------------------------------ HTTP helpers
def post_json(url, payload, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode()


def mcp_call(url, token, method, params):
    raw = post_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    {"Authorization": f"Bearer {token}",
                     "Accept": "application/json, text/event-stream"}, 60).strip()
    if raw.startswith(("event:", "data:")):
        raw = "\n".join(line[5:].strip() for line in raw.splitlines() if line.startswith("data:"))
    return json.loads(raw)["result"]


def zabbix_api(url, token, method, params):
    msg = json.loads(post_json(url, {"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
                               {"Authorization": f"Bearer {token}"}, 30))
    if "error" in msg:
        raise RuntimeError(f"{method}: {msg['error']}")
    return msg["result"]


# ------------------------------------------------------------------ ground truth & grading
def ground_truth(args, qid):
    api = lambda m, p: zabbix_api(args.zabbix_api, args.token, m, p)  # noqa: E731
    if qid == "host_count":
        hosts = api("host.get", {"output": ["name"]})
        return {"count": len(hosts), "names": [h["name"] for h in hosts]}
    if qid == "problems":
        return {"count": int(api("problem.get", {"countOutput": True}))}
    key, minutes = {"cpu_1h": ("system.cpu.util", 60), "mem_24h": ("vm.memory.util", 1440)}[qid]
    item = api("item.get", {"host": "Zabbix server", "filter": {"key_": key},
                            "output": ["itemid", "value_type"]})[0]
    vals = [float(v["value"]) for v in api("history.get", {
        "itemids": item["itemid"], "history": int(item["value_type"]),
        "time_from": int(time.time()) - minutes * 60, "output": ["value"]})]
    return {"avg": round(sum(vals) / len(vals), 2), "max": round(max(vals), 2), "n": len(vals)}


def numbers_in(text):
    return [float(n.replace(",", ".")) for n in re.findall(r"\d+(?:[.,]\d+)?", text)]


def close(found, expected):
    tol = max(0.06, abs(expected) * 0.08)  # data keeps moving between tool call and grading
    return any(abs(n - expected) <= tol for n in found)


def grade(qid, answer, truth):
    low, nums = answer.lower(), numbers_in(answer)
    if qid == "host_count":
        names_ok = all(n.lower() in low for n in truth["names"][:5])
        return truth["count"] in [int(n) for n in nums if n.is_integer()] and names_ok
    if qid == "problems":
        if truth["count"] == 0:
            return any(w in low for w in NEGATIONS)
        return truth["count"] in [int(n) for n in nums if n.is_integer()]
    return close(nums, truth["avg"]) and close(nums, truth["max"])


# ------------------------------------------------------------------ one question
def load_tools(args, toolset):
    servers = {"zabbix": args.zabbix_mcp, "zabbix-helper": args.helper_mcp}
    tools, route = [], {}
    for server, wanted in TOOLSETS[toolset].items():
        for t in mcp_call(servers[server], args.token, "tools/list", {})["tools"]:
            if t["name"] in wanted:
                fn = f"{t['name']}_mcp_{server}"
                route[fn] = (servers[server], t["name"])
                tools.append({"type": "function", "function": {
                    "name": fn, "description": t.get("description", ""), "parameters": t["inputSchema"]}})
    return tools, route


def run_question(args, toolset, tools, route, question):
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    msgs = [{"role": "system", "content": PROMPTS[toolset].format(now=now)},
            {"role": "user", "content": question["text"]}]
    rec = {"model": args.model, "toolset": toolset, "question": question["id"], "tool_calls": [],
           "prompt_tokens_max": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    t0, answer, outcome = time.time(), "", None
    try:
        for _ in range(args.max_rounds):
            remaining = args.time_limit - (time.time() - t0)
            if remaining <= 0:
                outcome = "TIMEOUT"
                break
            body = {"model": args.model, "messages": msgs, "tools": tools, "max_tokens": args.max_tokens}
            try:
                resp = json.loads(post_json(f"{args.llm_url}/chat/completions", body, {}, remaining))
            except TimeoutError:
                outcome = "TIMEOUT"
                break
            usage = resp.get("usage", {})
            rec["prompt_tokens_max"] = max(rec["prompt_tokens_max"], usage.get("prompt_tokens", 0))
            rec["completion_tokens"] += usage.get("completion_tokens", 0)
            rec["reasoning_tokens"] += (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
            msg = resp["choices"][0]["message"]
            if not msg.get("tool_calls"):
                answer = (msg.get("content") or "").strip()
                break
            msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": msg["tool_calls"]})
            for tc in msg["tool_calls"]:
                url, name = route.get(tc["function"]["name"], (None, tc["function"]["name"]))
                try:
                    tool_args = json.loads(tc["function"]["arguments"] or "{}")
                    result = mcp_call(url, args.token, "tools/call", {"name": name, "arguments": tool_args}) \
                        if url else {"isError": True, "content": [{"type": "text", "text": f"Unknown tool {name}"}]}
                    text = "".join(c.get("text", "") for c in result.get("content", []))
                except Exception as exc:  # noqa: BLE001
                    result, text = {"isError": True}, f"Tool call failed: {exc}"
                rec["tool_calls"].append({"name": name, "args": tc["function"]["arguments"],
                                          "error": bool(result.get("isError")), "chars": len(text)})
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": text})
        else:
            outcome = "TIMEOUT"  # too many tool rounds
    except urllib.error.HTTPError as exc:
        outcome, rec["error"] = "ERROR", exc.read().decode()[:300]
    except Exception as exc:  # noqa: BLE001
        outcome, rec["error"] = "ERROR", str(exc)[:300]

    rec["seconds"] = round(time.time() - t0, 1)
    rec["answer"] = answer[:1500]
    rec["truth"] = ground_truth(args, question["id"])
    if outcome is None:
        if not answer:
            outcome = "NO_ANSWER"
        elif not rec["tool_calls"]:
            outcome = "NO_TOOL"
        else:
            outcome = "PASS" if grade(question["id"], answer, rec["truth"]) else "WRONG"
    rec["outcome"] = outcome
    return rec


# ------------------------------------------------------------------ report
def report(paths):
    rows = [json.loads(line) for p in paths for line in Path(p).read_text().splitlines() if line.strip()]
    cells = defaultdict(list)
    for r in rows:
        cells[(r["model"], r["toolset"], r["question"])].append(r)
    models = list(dict.fromkeys(r["model"] for r in rows))
    qids = [q["id"] for q in QUESTIONS]
    print("| Model | Tool set | " + " | ".join(qids) + " | Total | Median time |")
    print("|---|---|" + "---|" * len(qids) + "---|---|")
    for model in models:
        for toolset in ("official", "helper"):
            runs = [r for r in rows if r["model"] == model and r["toolset"] == toolset]
            if not runs:
                continue
            parts = []
            for q in qids:
                c = cells[(model, toolset, q)]
                ok = sum(r["outcome"] == "PASS" for r in c)
                parts.append(f"{ok}/{len(c)} ({statistics.median(r['seconds'] for r in c):.0f}s)" if c else "-")
            ok = sum(r["outcome"] == "PASS" for r in runs)
            print(f"| {model} | {toolset} | " + " | ".join(parts)
                  + f" | **{ok}/{len(runs)}** | {statistics.median(r['seconds'] for r in runs):.0f}s |")
    print("\nFailures by outcome:")
    fails = defaultdict(lambda: defaultdict(int))
    for r in rows:
        if r["outcome"] != "PASS":
            fails[(r["model"], r["toolset"])][r["outcome"]] += 1
    for (model, toolset), counts in fails.items():
        print(f"- {model} / {toolset}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))


# ------------------------------------------------------------------ main
def read_token(env_file):
    for line in Path(env_file).read_text().splitlines():
        if line.startswith("ZABBIX_MCP_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"ZABBIX_MCP_TOKEN not found in {env_file}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model")
    ap.add_argument("--toolset", choices=list(TOOLSETS))
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--questions", nargs="*", default=[q["id"] for q in QUESTIONS])
    ap.add_argument("--out", default=None, help="JSONL file to append results to")
    ap.add_argument("--report", nargs="*", help="print a summary table of JSONL result files")
    ap.add_argument("--llm-url", default="http://127.0.0.1:1234/v1")
    ap.add_argument("--zabbix-mcp", default="http://127.0.0.1:8443/mcp")
    ap.add_argument("--helper-mcp", default="http://127.0.0.1:8001/mcp")
    ap.add_argument("--zabbix-api", default="http://127.0.0.1:8080/api_jsonrpc.php")
    ap.add_argument("--env-file", default=str(ROOT / "librechat.env"))
    ap.add_argument("--max-tokens", type=int, default=4096, help="per model request")
    ap.add_argument("--max-rounds", type=int, default=8, help="tool-call rounds per question")
    ap.add_argument("--time-limit", type=float, default=240, help="seconds per question")
    args = ap.parse_args()

    if args.report:
        return report(args.report)
    if not (args.model and args.toolset):
        ap.error("--model and --toolset are required (or use --report)")
    args.token = read_token(args.env_file)
    out = Path(args.out or ROOT / "bench" / "results" / f"{datetime.now():%Y%m%d-%H%M%S}.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    tools, route = load_tools(args, args.toolset)
    questions = [q for q in QUESTIONS if q["id"] in args.questions]
    for run in range(1, args.runs + 1):
        for q in questions:
            rec = run_question(args, args.toolset, tools, route, q)
            rec["run"] = run
            with out.open("a") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            calls = " -> ".join(c["name"] + ("!" if c["error"] else "") for c in rec["tool_calls"]) or "-"
            print(f"[{args.model} | {args.toolset} | run {run}] {q['id']:10s} {rec['outcome']:9s} "
                  f"{rec['seconds']:6.1f}s  tools: {calls}", flush=True)
    print(f"results: {out}")


if __name__ == "__main__":
    sys.exit(main())
