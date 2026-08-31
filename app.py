#!/usr/bin/env python3
"""Patchbay — connexions MCP Webflow de Claude Code.

Serveur local (stdlib uniquement) + fenêtre Chrome --app.
Lit ~/.claude.json et ~/.claude/.credentials.json ; n'expose JAMAIS un token
au frontend : seuls des booléens et des listes de sites en sortent.
"""
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CLAUDE_JSON = os.path.join(HOME, ".claude.json")
CREDS_JSON = os.path.join(HOME, ".claude", ".credentials.json")
WEBFLOW_MCP_URL = "https://mcp.webflow.com/mcp"
PROJECTS_ROOT = os.path.join(HOME, "Bureau", "Webflow")
LOCK = os.path.join(APP_DIR, "lock")
SITES_CACHE = os.path.join(APP_DIR, "sites-cache.json")
NAME_RE = re.compile(r"^wf-[A-Za-z0-9._-]{1,64}$")
SAFE_RE = re.compile(r"^[A-Za-z0-9._ -]{1,80}$")

SECRET = secrets.token_urlsafe(16)
last_ping = {"t": time.time(), "seen": False}


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def credkey(name, entry):
    """Clé du credential telle que Claude Code la calcule : nom|sha256({type,url,headers})[:16]."""
    blob = json.dumps(
        {"type": entry.get("type", "http"), "url": entry.get("url", ""),
         "headers": entry.get("headers") or {}},
        separators=(",", ":"))
    return name + "|" + hashlib.sha256(blob.encode()).hexdigest()[:16]


def read_state():
    cfg = load(CLAUDE_JSON)
    creds = load(CREDS_JSON).get("mcpOAuth", {})

    servers = []
    def collect(entries, scope):
        for name, entry in (entries or {}).items():
            if not isinstance(entry, dict) or entry.get("url") != WEBFLOW_MCP_URL:
                continue
            tok = creds.get(credkey(name, entry), {})
            cached = load(SITES_CACHE).get(name) or {}
            servers.append({
                "name": name,
                "scope": scope,                      # "user" ou chemin du dossier
                "authorized": bool(tok.get("accessToken")),
                "sites": cached.get("sites"),
            })

    collect(cfg.get("mcpServers"), "user")
    for path, pc in (cfg.get("projects") or {}).items():
        collect((pc or {}).get("mcpServers"), path)

    # tokens Webflow orphelins (serveur retiré mais credential conservé)
    known = {s["name"] for s in servers}
    orphans = sorted(
        k.split("|")[0] for k, v in creds.items()
        if v.get("serverUrl") == WEBFLOW_MCP_URL
        and v.get("accessToken") and k.split("|")[0] not in known)

    # dossiers projet proposables : Bureau/Webflow/* et Bureau/Webflow/R&D/*
    wired = {s["scope"] for s in servers}
    folders = []
    try:
        for e in sorted(os.scandir(PROJECTS_ROOT), key=lambda x: x.name.lower()):
            if not e.is_dir() or e.name.startswith("."):
                continue
            if e.name == "R&D":
                for s in sorted(os.scandir(e.path), key=lambda x: x.name.lower()):
                    if s.is_dir() and not s.name.startswith("."):
                        folders.append({"path": s.path, "label": "R&D / " + s.name,
                                        "wired": s.path in wired})
            else:
                folders.append({"path": e.path, "label": e.name,
                                "wired": e.path in wired})
    except FileNotFoundError:
        pass

    servers.sort(key=lambda s: (not s["authorized"], s["name"].lower()))
    return {"servers": servers, "orphans": orphans, "folders": folders,
            "home": HOME}


def claude_mcp(args, cwd=None):
    try:
        r = subprocess.run(["claude", "mcp"] + args, cwd=cwd,
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except FileNotFoundError:
        return False, "CLI « claude » introuvable dans le PATH."
    except subprocess.TimeoutExpired:
        return False, "claude mcp n'a pas répondu (30 s)."


def token_for(name):
    creds = load(CREDS_JSON).get("mcpOAuth", {})
    cfg = load(CLAUDE_JSON)
    pools = [cfg.get("mcpServers") or {}] + \
        [(p or {}).get("mcpServers") or {} for p in (cfg.get("projects") or {}).values()]
    for pool in pools:
        entry = pool.get(name)
        if isinstance(entry, dict) and entry.get("url") == WEBFLOW_MCP_URL:
            tok = creds.get(credkey(name, entry), {}).get("accessToken")
            if tok:
                return tok
    return None


def _cache_put(name, result):
    cache = load(SITES_CACHE)
    cache[name] = dict(result, ts=int(time.time()))
    tmp = SITES_CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, SITES_CACHE)


MCP_UA = "claude-code/2.1.247 (patchbay-webflow)"  # UA nu type python-urllib -> 403 WAF


def _mcp_post(tok, payload, sid=None, expect=True):
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream",
               "Authorization": "Bearer " + tok,
               "User-Agent": MCP_UA,
               "MCP-Protocol-Version": "2025-03-26"}
    if sid:
        headers["Mcp-Session-Id"] = sid
    req = urllib.request.Request(WEBFLOW_MCP_URL, json.dumps(payload).encode(), headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        new_sid = r.headers.get("Mcp-Session-Id") or sid
        ctype = r.headers.get("Content-Type", "")
        raw = r.read().decode()
    if not expect:
        return new_sid, None
    if "event-stream" in ctype:
        for line in raw.splitlines():
            if line.startswith("data:"):
                try:
                    obj = json.loads(line[5:])
                except json.JSONDecodeError:
                    continue
                if obj.get("id") == payload.get("id"):
                    return new_sid, obj
        return new_sid, None
    return new_sid, json.loads(raw) if raw else None


def webflow_sites(name):
    """Périmètre réel du token, demandé au serveur MCP (seule audience qui l'accepte)."""
    tok = token_for(name)
    if not tok:
        return {"error": "Pas de token pour ce serveur."}
    try:
        sid, _ = _mcp_post(tok, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                            "clientInfo": {"name": "patchbay-webflow", "version": "1.0"}}})
        _mcp_post(tok, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid, expect=False)
        _, res = _mcp_post(tok, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                 "params": {"name": "data_sites_tool",
                                            "arguments": {"actions": [{"label": "périmètre", "list_sites": {}}]}}}, sid)
        payload = (res or {}).get("result", {})
        if payload.get("isError"):
            return {"error": "Le serveur MCP a refusé l'appel."}
        for c in payload.get("content", []):
            if c.get("type") != "text":
                continue
            data = json.loads(c["text"])
            sites = (data.get("result") or {}).get("sites", [])
            result = {"sites": [{"name": s.get("displayName") or s.get("shortName", "?"),
                                 "id": s.get("id", ""),
                                 "shortName": s.get("shortName", "")} for s in sites]}
            _cache_put(name, result)
            return result
        return {"error": "Réponse MCP sans contenu lisible."}
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"error": "Token refusé — refaire /mcp → Authenticate sur ce serveur."}
        return {"error": "Serveur MCP : HTTP %d." % e.code}
    except Exception as e:
        return {"error": "Injoignable : %s" % e.__class__.__name__}


def open_terminal(cwd):
    for cmd in (["gnome-terminal", "--working-directory=" + cwd, "--", "claude"],
                ["x-terminal-emulator", "-e", "claude"],
                ["kgx", "--working-directory=" + cwd, "-e", "claude"]):
        try:
            subprocess.Popen(cmd, cwd=cwd, start_new_session=True)
            return True
        except FileNotFoundError:
            continue
    return False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _route(self):
        if self.path == "/favicon.ico":
            with open(os.path.join(APP_DIR, "icon.svg"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.end_headers()
            self.wfile.write(body)
            return None
        if not self.path.startswith("/" + SECRET):
            self.send_error(404)
            return None
        return self.path[len(SECRET) + 1:] or "/"

    def do_GET(self):
        route = self._route()
        if route is None:
            return
        if route in ("/", "/index.html"):
            with open(os.path.join(APP_DIR, "index.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        elif route == "/icon.svg":
            with open(os.path.join(APP_DIR, "icon.svg"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.end_headers()
            self.wfile.write(body)
        elif route == "/api/state":
            last_ping["t"] = time.time()
            last_ping["seen"] = True
            self._json(read_state())
        elif route.startswith("/api/sites"):
            name = route.split("name=", 1)[-1]
            name = urllib.parse.unquote(name)
            self._json(webflow_sites(name) if SAFE_RE.match(name)
                       else {"error": "nom invalide"})
        else:
            self.send_error(404)

    def do_POST(self):
        route = self._route()
        if route is None:
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            payload = {}
        name = payload.get("name", "")
        d = payload.get("dir", "")

        if route == "/api/add":
            if not NAME_RE.match(name):
                return self._json({"ok": False, "msg": "Nom invalide (attendu : wf-…)."})
            scope = "user" if payload.get("scope") == "user" else "local"
            cwd = None
            if scope == "local":
                if not (os.path.isdir(d) and os.path.realpath(d).startswith(HOME)):
                    return self._json({"ok": False, "msg": "Dossier introuvable."})
                cwd = d
            ok, msg = claude_mcp(["add", "--transport", "http", "-s", scope,
                                  name, WEBFLOW_MCP_URL], cwd=cwd)
            self._json({"ok": ok, "msg": msg})
        elif route == "/api/remove":
            scope = payload.get("scope", "")
            if not SAFE_RE.match(name):
                return self._json({"ok": False, "msg": "Nom invalide."})
            if scope == "user":
                ok, msg = claude_mcp(["remove", "-s", "user", name])
            elif os.path.isdir(scope):
                ok, msg = claude_mcp(["remove", "-s", "local", name], cwd=scope)
            else:
                ok, msg = False, "Dossier du serveur introuvable."
            self._json({"ok": ok, "msg": msg})
        elif route == "/api/vscode":
            known = {s["scope"] for s in read_state()["servers"]} | \
                    {f["path"] for f in read_state()["folders"]}
            if d in known and os.path.isdir(d):
                subprocess.Popen(["code", d], start_new_session=True)
                self._json({"ok": True, "msg": ""})
            else:
                self._json({"ok": False, "msg": "Dossier inconnu."})
        elif route == "/api/designer":
            short = str(payload.get("shortName", ""))
            if re.match(r"^[a-z0-9][a-z0-9-]{0,80}$", short):
                subprocess.Popen(["xdg-open", "https://webflow.com/design/" + short],
                                 start_new_session=True)
                self._json({"ok": True, "msg": ""})
            else:
                self._json({"ok": False, "msg": "shortName invalide."})
        elif route == "/api/terminal":
            ok = os.path.isdir(d) and open_terminal(d)
            self._json({"ok": ok, "msg": "" if ok else "Aucun terminal lançable trouvé."})
        else:
            self.send_error(404)


def watchdog():
    while True:
        time.sleep(5)
        idle = time.time() - last_ping["t"]
        if (last_ping["seen"] and idle > 30) or (not last_ping["seen"] and idle > 120):
            os._exit(0)


def main():
    serve_only = "--serve-only" in sys.argv

    # instance déjà ouverte → rouvrir sa fenêtre plutôt qu'en lancer une deuxième
    if not serve_only and os.path.exists(LOCK):
        try:
            pid_s, url = open(LOCK).read().split("\n")[:2]
            os.kill(int(pid_s), 0)
            subprocess.Popen(["google-chrome", "--app=" + url], start_new_session=True)
            return
        except Exception:
            os.remove(LOCK)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = "http://127.0.0.1:%d/%s/" % (srv.server_address[1], SECRET)

    if not serve_only:
        fd = os.open(LOCK, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, ("%d\n%s\n" % (os.getpid(), url)).encode())
        os.close(fd)
        threading.Thread(target=watchdog, daemon=True).start()
        try:
            subprocess.Popen(["google-chrome", "--app=" + url,
                              "--window-size=760,780"], start_new_session=True)
        except FileNotFoundError:
            subprocess.Popen(["xdg-open", url], start_new_session=True)
        signal.signal(signal.SIGTERM, lambda *a: os._exit(0))
    else:
        print(url, flush=True)

    try:
        srv.serve_forever()
    finally:
        if not serve_only and os.path.exists(LOCK):
            os.remove(LOCK)


if __name__ == "__main__":
    main()
