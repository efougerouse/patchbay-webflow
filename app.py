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
            servers.append({
                "name": name,
                "scope": scope,                      # "user" ou chemin du dossier
                "authorized": bool(tok.get("accessToken")),
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


def webflow_sites(name):
    """Périmètre réel du token : liste des sites que Webflow accepte de montrer."""
    tok = token_for(name)
    if not tok:
        return {"error": "Pas de token pour ce serveur."}
    req = urllib.request.Request(
        "https://api.webflow.com/v2/sites",
        headers={"Authorization": "Bearer " + tok, "User-Agent": "patchbay-webflow"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.load(r)
        return {"sites": [{"name": s.get("displayName") or s.get("shortName", "?"),
                           "id": s.get("id", "")} for s in data.get("sites", [])]}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"restricted": True}
        return {"error": "Webflow a répondu HTTP %d." % e.code}
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
