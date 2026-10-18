"""Live dashboard: watch explorer agents build the graph, plan a demo, record it, and let the
watcher keep every video and guide true when the product changes.

    python -m demograph ui http://127.0.0.1:8765 --port 8800

Everything the page shows is read from FalkorDB on each poll.
"""

import asyncio
import json
import mimetypes
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from falkordb import FalkorDB

from .. import queries as Q
from ..config import settings
from ..db import GraphStore
from ..explorer.crawl import is_local
from ..ids import graph_name_for, normalize_url
from ..planner import Planner, PlanError

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SAMPLE_APP = "http://127.0.0.1:8765/"
SAMPLE_LABELS = ("Export CSV", "Download CSV")  # what "Simulate release" toggles in the sample app

STATE_CRAWL = "MATCH (c:Crawl) RETURN c.id, c.status, c.started_at, c.finished_at ORDER BY c.started_at DESC LIMIT 1"
STATE_FRONTIER = """
MATCH (c:Crawl {id: $c})-[f:FRONTIER]->(p:Page)
OPTIONAL MATCH (c)-[v:VISITED]->(p)
OPTIONAL MATCH (p)-[:HAS_ELEMENT]->(e:Element) WHERE NOT coalesce(e.removed, false)
RETURN p.id, p.path, p.title, f.status, f.agent, f.depth, p.screenshot, count(e), f.from, v.at
"""
STATE_EDGES = """
MATCH (a:Page)-[r:LEADS_TO]->(b:Page)
WHERE NOT coalesce(r.stale, false)
RETURN DISTINCT a.id, b.id, r.action
"""
STATE_VISITS = """
MATCH (:Crawl {id: $c})-[v:VISITED]->(p:Page)
RETURN p.path, v.agent, v.at, v.elements, v.unchanged
ORDER BY v.at DESC LIMIT 12
"""
# Current demos, the pages they walk through, the page of their target, and their artifacts.
STATE_DEMOS = """
MATCH (d:Demo) WHERE coalesce(d.status, 'current') <> 'superseded'
OPTIONAL MATCH (d)-[:HAS_SCENE]->(:Scene)-[:ON_PAGE]->(p:Page)
WITH d, collect(DISTINCT p.id) AS pages
OPTIONAL MATCH (d)-[:TARGETS]->(:Element)<-[:HAS_ELEMENT]-(tp:Page)
WITH d, pages, collect(DISTINCT tp.id) AS targets
OPTIONAL MATCH (v:Video)-[:RENDERS]->(d)
WITH d, pages, targets, collect({path: v.path, at: v.rendered_at, duration: v.duration}) AS videos
OPTIONAL MATCH (doc:Doc)-[:DOCUMENTS]->(d)
RETURN d.id, d.goal, coalesce(d.status, 'current'), pages, targets, videos,
       collect({path: doc.html, at: doc.rendered_at}), d.created_at, d.target_text
ORDER BY d.created_at DESC
LIMIT 12
"""
# Pages with changes recorded in a crawl (what the release touched).
STATE_CHANGES = """
MATCH (:Crawl {id: $c})<-[ch:CHANGED_IN]-(x)
OPTIONAL MATCH (p:Page)-[:HAS_ELEMENT]->(x)
RETURN coalesce(p.id, x.id), ch.kind, ch.old_text, ch.new_text
LIMIT 40
"""


def latest(items: list[dict]) -> dict | None:
    items = [i for i in items if i.get("path")]
    return max(items, key=lambda i: i.get("at") or 0) if items else None


class Dashboard:
    def __init__(self, site_url: str):
        self.jobs: dict[str, dict] = {}  # name -> {proc, log}
        self.db = FalkorDB(host=settings.host, port=settings.port, password=settings.password)
        self.set_site(site_url)

    def set_site(self, site_url: str) -> dict:
        site = normalize_url(site_url if "://" in site_url else "https://" + site_url)
        if not site:
            return {"error": f"not an http(s) url: {site_url}"}
        if any(self._running(k) for k in ("explore", "watch", "record")):
            return {"error": "stop the running job before switching sites"}
        self.site = site
        self.graph_name = graph_name_for(site)
        self.g = self.db.select_graph(self.graph_name)
        return {"site": self.site, "graph": self.graph_name}

    def q(self, cypher: str, params: dict | None = None) -> list[list]:
        try:
            return self.g.ro_query(cypher, params or {}).result_set
        except Exception as e:
            if "empty key" in str(e).lower() or "invalid graph" in str(e).lower():
                return []
            raise

    def state(self) -> dict:
        crawl = self.q(STATE_CRAWL)
        out = {"site": self.site, "graph": self.graph_name, "crawl": None, "pages": [], "edges": [],
               "visits": [], "changes": [], "local": is_local(self.site),
               "sample_app": self.site == SAMPLE_APP,
               "jobs": {k: self._job_state(k) for k in self.jobs}}
        if crawl:
            cid, status, started, finished = crawl[0]
            out["crawl"] = {"id": cid, "status": status, "started": started, "finished": finished}
            out["pages"] = [{"id": r[0], "path": r[1], "title": r[2], "status": r[3], "agent": r[4], "depth": r[5],
                             "shot": r[6], "facts": r[7], "from": r[8], "visited_at": r[9]}
                            for r in self.q(STATE_FRONTIER, {"c": cid})]
            out["visits"] = [{"path": r[0], "agent": r[1], "at": r[2], "facts": r[3], "unchanged": r[4]}
                             for r in self.q(STATE_VISITS, {"c": cid})]
            out["changes"] = [{"page": r[0], "kind": r[1], "old": r[2], "new": r[3]}
                              for r in self.q(STATE_CHANGES, {"c": cid})]
        out["edges"] = [{"source": r[0], "target": r[1], "action": r[2]} for r in self.q(STATE_EDGES)]
        health = {r[0]: int(r[4] or 0) for r in self.q(Q.DEMO_HEALTH)}
        out["demos"] = []
        for r in self.q(STATE_DEMOS):
            video, doc = latest(r[5]), latest(r[6])
            out["demos"].append({"id": r[0], "goal": r[1], "status": r[2], "pages": r[3],
                                 "target_page": (r[4] or [None])[0], "stale": health.get(r[0], 0),
                                 "video": video, "doc": doc, "created": r[7], "target_text": r[8]})
        out["stale_segments"] = [{"video": r[1], "scene": r[2], "start": r[3], "end": r[4], "was": r[5],
                                  "now": r[6], "role": r[8]} for r in self.q(Q.STALE_VIDEO_SEGMENTS)]
        out["stale_sections"] = [{"doc": r[1], "section": r[2], "was": r[3], "now": r[4], "role": r[6]}
                                 for r in self.q(Q.STALE_DOC_SECTIONS)]
        out["updates"] = [{"crawl": r[0], "new": r[1], "old": r[2], "goal": r[3], "at": r[4]}
                          for r in self.q(Q.DEMO_UPDATES)]
        out["broken"] = [{"crawl": r[0], "demo": r[1], "goal": r[2], "reason": r[3], "at": r[4]}
                         for r in self.q(Q.BROKEN_DEMOS)]
        if out["sample_app"]:
            out["sample_label"] = current_sample_label()
        return out

    def plan(self, goal: str) -> dict:
        async def run():
            store = GraphStore(self.graph_name)
            try:
                p = await Planner(store, self.site).plan(goal)
                return {"goal": goal, "route": p.route_pages, "paths": p.route_paths, "cost": p.cost,
                        "target": {"text": p.target.text, "kind": p.target.kind, "page": p.target.page_path},
                        "scenes": [{"kind": s.kind, "page": s.page_path, "narration": s.narration,
                                    "cites": [c.text for c in s.cites]} for s in p.scenes]}
            finally:
                await store.close()
        try:
            return asyncio.run(run())
        except PlanError as e:
            return {"error": str(e)}

    def _running(self, name: str) -> bool:
        job = self.jobs.get(name)
        return bool(job and job["proc"].poll() is None)

    def start(self, name: str, args: list[str], exclusive: tuple[str, ...] = ()) -> dict:
        if self._running(name):
            return {"error": f"{name} is already running"}
        busy = [k for k in exclusive if self._running(k)]
        if busy:
            return {"error": f"wait for {busy[0]} to finish first"}
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.Popen([sys.executable, "-u", "-m", "demograph", *args], cwd=ROOT, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace")
        job = {"proc": proc, "log": []}
        self.jobs[name] = job
        threading.Thread(target=lambda: [job["log"].append(line.rstrip()) for line in proc.stdout], daemon=True).start()
        return {"started": name}

    def stop(self, name: str) -> dict:
        if self._running(name):
            self.jobs[name]["proc"].terminate()
            return {"stopped": name}
        return {"error": f"{name} is not running"}

    def _job_state(self, name: str) -> dict:
        job = self.jobs[name]
        code = job["proc"].poll()
        return {"running": code is None, "exit": code, "log": job["log"][-16:]}

    def release(self) -> dict:
        """Ship a fake release of the bundled sample app: toggle one button label."""
        if self.site != SAMPLE_APP:
            return {"error": "Simulate release only works on the bundled sample app"}
        now = current_sample_label()
        new = SAMPLE_LABELS[1] if now == SAMPLE_LABELS[0] else SAMPLE_LABELS[0]
        subprocess.run([sys.executable, str(ROOT / "sample_app" / "mutate.py"), "label", "export_csv", new],
                       cwd=ROOT, check=True, capture_output=True)
        return {"from": now, "to": new}


def current_sample_label() -> str:
    working = ROOT / settings.data_dir / "sample_app" / "catalog.json"
    path = working if working.exists() else ROOT / "sample_app" / "catalog.json"
    return json.loads(path.read_text(encoding="utf-8"))["labels"]["export_csv"]


def make_handler(dash: Dashboard):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, default=str).encode("utf-8"), "application/json")

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path in ("/", "/index.html"):
                return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/state":
                return self._json(dash.state())
            if url.path == "/api/plan":
                goal = (parse_qs(url.query).get("goal") or [""])[0]
                return self._json(dash.plan(goal))
            if url.path.startswith("/files/"):
                # Only files under the data directory (screenshots, videos, guides) are served.
                target = (ROOT / unquote(url.path[len("/files/"):])).resolve()
                data_root = (ROOT / settings.data_dir).resolve()
                if data_root in target.parents and target.is_file():
                    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                    return self._send(200, target.read_bytes(), ctype)
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            url = urlsplit(self.path)
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if url.path == "/api/site":
                return self._json(dash.set_site(str(body.get("url", ""))))
            if url.path == "/api/explore":
                pace = str(float(body.get("pace", 1.0)))
                return self._json(dash.start("explore", ["explore", dash.site, "--agents", "3", "--pace", pace],
                                             exclusive=("watch",)))
            if url.path == "/api/record":
                goal = str(body.get("goal", "")).strip()
                if not goal:
                    return self._json({"error": "goal required"}, 400)
                return self._json(dash.start("record", ["record", dash.site, goal], exclusive=("watch",)))
            if url.path == "/api/watch":
                if body.get("on"):
                    every = str(float(body.get("every", 15)))
                    return self._json(dash.start("watch", ["watch", dash.site, "--every", every, "--pace", "0.4"],
                                                 exclusive=("explore", "record")))
                return self._json(dash.stop("watch"))
            if url.path == "/api/release":
                return self._json(dash.release())
            self._json({"error": "not found"}, 404)

    return Handler


def serve(site_url: str, port: int = 8800) -> None:
    dash = Dashboard(site_url)
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(dash))
    print(f"DemoGraph dashboard on http://127.0.0.1:{port}  (graph {dash.graph_name})", flush=True)
    srv.serve_forever()
