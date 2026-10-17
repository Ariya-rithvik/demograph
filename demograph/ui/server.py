"""Live dashboard: watch explorer agents build the graph, plan a demo, and play the video.

    python -m demograph ui http://127.0.0.1:8765 --port 8800

Everything the page shows is read from FalkorDB on each poll.
"""

import asyncio
import json
import mimetypes
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
from ..ids import graph_name_for, normalize_url
from ..planner import Planner, PlanError

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

STATE_CRAWL = "MATCH (c:Crawl) RETURN c.id, c.status, c.started_at, c.finished_at ORDER BY c.started_at DESC LIMIT 1"
STATE_FRONTIER = """
MATCH (:Crawl {id: $c})-[f:FRONTIER]->(p:Page)
OPTIONAL MATCH (p)-[:HAS_ELEMENT]->(e:Element) WHERE NOT coalesce(e.removed, false)
RETURN p.id, p.path, p.title, f.status, f.agent, f.depth, p.screenshot, count(e), f.from
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
STATE_VIDEOS = """
MATCH (v:Video)-[:RENDERS]->(d:Demo)
RETURN d.id, d.goal, v.path, v.duration ORDER BY v.rendered_at DESC LIMIT 10
"""


class Dashboard:
    def __init__(self, site_url: str):
        self.site = normalize_url(site_url)
        self.graph_name = graph_name_for(self.site)
        db = FalkorDB(host=settings.host, port=settings.port, password=settings.password)
        self.g = db.select_graph(self.graph_name)
        self.jobs: dict[str, dict] = {}  # name -> {proc, log}

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
               "visits": [], "videos": [], "jobs": {k: self._job_state(k) for k in self.jobs}}
        if crawl:
            cid, status, started, finished = crawl[0]
            out["crawl"] = {"id": cid, "status": status, "started": started, "finished": finished}
            out["pages"] = [{"id": r[0], "path": r[1], "title": r[2], "status": r[3], "agent": r[4],
                             "depth": r[5], "shot": r[6], "facts": r[7], "from": r[8]}
                            for r in self.q(STATE_FRONTIER, {"c": cid})]
            out["visits"] = [{"path": r[0], "agent": r[1], "at": r[2], "facts": r[3], "unchanged": r[4]}
                             for r in self.q(STATE_VISITS, {"c": cid})]
        out["edges"] = [{"source": r[0], "target": r[1], "action": r[2]} for r in self.q(STATE_EDGES)]
        out["videos"] = [{"demo": r[0], "goal": r[1], "path": r[2], "duration": r[3]} for r in self.q(STATE_VIDEOS)]
        out["demos"] = [{"id": r[0], "goal": r[1], "scenes": r[3], "stale": int(r[4])} for r in self.q(Q.DEMO_HEALTH)]
        out["stale_segments"] = [{"video": r[1], "scene": r[2], "start": r[3], "end": r[4], "was": r[5], "now": r[6]}
                                 for r in self.q(Q.STALE_VIDEO_SEGMENTS)]
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

    def start(self, name: str, args: list[str]) -> dict:
        job = self.jobs.get(name)
        if job and job["proc"].poll() is None:
            return {"error": f"{name} is already running"}
        proc = subprocess.Popen([sys.executable, "-u", "-m", "demograph", *args], cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        job = {"proc": proc, "log": []}
        self.jobs[name] = job
        threading.Thread(target=lambda: [job["log"].append(line.rstrip()) for line in proc.stdout], daemon=True).start()
        return {"started": name}

    def _job_state(self, name: str) -> dict:
        job = self.jobs[name]
        code = job["proc"].poll()
        return {"running": code is None, "exit": code, "log": job["log"][-14:]}


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
                # Only files under the data directory (screenshots, videos) are served.
                target = (ROOT / unquote(url.path[len("/files/"):])).resolve()
                data_root = (ROOT / settings.data_dir).resolve()
                if data_root in target.parents and target.is_file():
                    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                    return self._send(200, target.read_bytes(), ctype)
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            url = urlsplit(self.path)
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if url.path == "/api/explore":
                pace = str(float(body.get("pace", 1.0)))
                return self._json(dash.start("explore", ["explore", dash.site, "--agents", "3", "--pace", pace]))
            if url.path == "/api/record":
                goal = str(body.get("goal", "")).strip()
                if not goal:
                    return self._json({"error": "goal required"}, 400)
                return self._json(dash.start("record", ["record", dash.site, goal]))
            self._json({"error": "not found"}, 404)

    return Handler


def serve(site_url: str, port: int = 8800) -> None:
    dash = Dashboard(site_url)
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(dash))
    print(f"DemoGraph dashboard on http://127.0.0.1:{port}  (graph {dash.graph_name})", flush=True)
    srv.serve_forever()
