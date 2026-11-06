"""Prepare Zabbix 8.0 for MCP access (safe to run again).

1. Points the agent interface of the "Zabbix server" host to the zabbix-agent container.
2. Creates a read-only "mcp-reader" user, a role that allows only the read API methods
   used by the MCP tools, and a user group with read access to all host/template groups.
3. Generates an API token for that user and writes it to ZABBIX_MCP_TOKEN in librechat.env.

Usage:  python3 -I setup_zabbix.py [--admin-password zabbix] [--api-url URL]
"""

import argparse
import json
import re
import secrets
import time
import urllib.request
from pathlib import Path

API = "http://localhost:8080/api_jsonrpc.php"
ENV_FILE = Path(__file__).resolve().parent / "librechat.env"
GROUP_NAME = "MCP read-only"
ROLE_NAME = "MCP read-only role"
USERNAME = "mcp-reader"
# Zabbix API methods behind the official MCP read tools
# (zabbix-mcp-lite needs only host.get, item.get, history.get, problem.get, trigger.get)
READ_METHODS = [
    "hostgroup.get", "templategroup.get", "host.get", "template.get", "item.get",
    "discoveryrule.get", "hostinterface.get", "usermacro.get", "problem.get",
    "maintenance.get", "history.get", "trigger.get",
]
TOKEN_NAME = "librechat-mcp"
TOKEN_DAYS = 365


def call(method, params, auth=None):
    headers = {"Content-Type": "application/json-rpc"}
    if auth:
        headers["Authorization"] = f"Bearer {auth}"
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    req = urllib.request.Request(API, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)
    if "error" in data:
        raise RuntimeError(f"{method}: {data['error'].get('data') or data['error']}")
    return data["result"]


def login(password):
    last = None
    for _ in range(40):  # on first start the database may still be initializing
        try:
            return call("user.login", {"username": "Admin", "password": password})
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(3)
    raise SystemExit(f"Admin login failed: {last}")


def fix_agent_interface(auth):
    hosts = call("host.get", {"filter": {"host": ["Zabbix server"]}, "output": ["hostid"]}, auth)
    if not hosts:
        print("- Host 'Zabbix server' not found, skipped")
        return
    hostid = hosts[0]["hostid"]
    ifaces = call("hostinterface.get", {"hostids": hostid, "filter": {"type": 1}}, auth)
    if all(i["useip"] == "0" and i["dns"] == "zabbix-agent" for i in ifaces):
        print("- Agent interface already points to zabbix-agent")
        return
    # Zabbix 8.0 has no hostinterface.update; interfaces are changed through host.update
    new_ifaces = [{"interfaceid": i["interfaceid"], "type": 1, "main": i["main"], "useip": 0,
                   "ip": i["ip"], "dns": "zabbix-agent", "port": i["port"]} for i in ifaces]
    call("host.update", {"hostid": hostid, "interfaces": new_ifaces}, auth)
    print("- Agent interface set to zabbix-agent:10050")


def ensure_group(auth):
    hostgroups = call("hostgroup.get", {"output": ["groupid"]}, auth)
    templategroups = call("templategroup.get", {"output": ["groupid"]}, auth)
    rights = {
        "hostgroup_rights": [{"id": g["groupid"], "permission": 2} for g in hostgroups],
        "templategroup_rights": [{"id": g["groupid"], "permission": 2} for g in templategroups],
    }
    existing = call("usergroup.get", {"filter": {"name": [GROUP_NAME]}, "output": ["usrgrpid"]}, auth)
    if existing:
        gid = existing[0]["usrgrpid"]
        call("usergroup.update", {"usrgrpid": gid, **rights}, auth)
        print(f"- Group '{GROUP_NAME}' updated ({len(hostgroups)} host groups, read)")
    else:
        gid = call("usergroup.create", {"name": GROUP_NAME, "gui_access": 3, **rights},
                   auth)["usrgrpids"][0]
        print(f"- Group '{GROUP_NAME}' created ({len(hostgroups)} host groups, read)")
    return gid


def ensure_role(auth):
    # In Zabbix 8.0 the default User and Admin roles have API access disabled.
    # This role allows only the methods behind the MCP read tools (allow list).
    rules = {"api.access": 1, "api.mode": 1, "api": READ_METHODS}
    existing = call("role.get", {"filter": {"name": [ROLE_NAME]}, "output": ["roleid"]}, auth)
    if existing:
        roleid = existing[0]["roleid"]
        call("role.update", {"roleid": roleid, "rules": rules}, auth)
        print(f"- Role '{ROLE_NAME}' updated ({len(READ_METHODS)} API methods)")
    else:
        roleid = call("role.create", {"name": ROLE_NAME, "type": 1, "rules": rules},
                      auth)["roleids"][0]
        print(f"- Role '{ROLE_NAME}' created ({len(READ_METHODS)} API methods)")
    return roleid


def ensure_user(auth, gid, roleid):
    existing = call("user.get", {"filter": {"username": [USERNAME]}, "output": ["userid"]}, auth)
    if existing:
        uid = existing[0]["userid"]
        call("user.update", {"userid": uid, "roleid": roleid, "usrgrps": [{"usrgrpid": gid}]}, auth)
        print(f"- User '{USERNAME}' updated")
        return uid
    uid = call("user.create", {
        "username": USERNAME,
        "name": "LibreChat MCP",
        "passwd": secrets.token_urlsafe(24),  # no web login needed, only the API token is used
        "roleid": roleid,
        "usrgrps": [{"usrgrpid": gid}],
    }, auth)["userids"][0]
    print(f"- User '{USERNAME}' created ('{ROLE_NAME}', read-only)")
    return uid


def new_token(auth, uid):
    old = call("token.get", {"userids": uid, "filter": {"name": TOKEN_NAME}, "output": ["tokenid"]}, auth)
    if old:
        call("token.delete", [t["tokenid"] for t in old], auth)
        print("- Old token deleted")
    tid = call("token.create", {
        "name": TOKEN_NAME,
        "userid": uid,
        "expires_at": int(time.time()) + TOKEN_DAYS * 86400,
        "description": "LibreChat -> Zabbix MCP",
    }, auth)["tokenids"][0]
    token = call("token.generate", [tid], auth)[0]["token"]
    print(f"- Token '{TOKEN_NAME}' generated (valid for {TOKEN_DAYS} days)")
    return token


def write_env(token):
    text = ENV_FILE.read_text()
    text, n = re.subn(r"(?m)^ZABBIX_MCP_TOKEN=.*$", f"ZABBIX_MCP_TOKEN={token}", text)
    if n == 0:
        text += f"\nZABBIX_MCP_TOKEN={token}\n"
    ENV_FILE.write_text(text)
    print(f"- Token written to {ENV_FILE.name}")


def main():
    global API
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--admin-password", default="zabbix")
    ap.add_argument("--api-url", default=API)
    args = ap.parse_args()
    API = args.api_url
    if not ENV_FILE.exists():
        raise SystemExit(f"{ENV_FILE.name} not found: copy librechat.env.example to librechat.env first")
    auth = login(args.admin_password)
    print(f"Zabbix {call('apiinfo.version', {})} - logged in as Admin")
    fix_agent_interface(auth)
    gid = ensure_group(auth)
    roleid = ensure_role(auth)
    uid = ensure_user(auth, gid, roleid)
    write_env(new_token(auth, uid))
    call("user.logout", [], auth)


if __name__ == "__main__":
    main()
