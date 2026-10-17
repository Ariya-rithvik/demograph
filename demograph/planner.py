"""Demo planner: turn a goal like "export csv" into a route through the product and
a list of scenes, where every scene cites the exact page elements it talks about.

1. Find the element the goal is about (full-text index over Element.text).
2. Find the cheapest route to it from the start page (algo.SPpaths over LEADS_TO,
   weighted by cost; links that no longer exist are never used).
3. Turn each hop into a scene and cite what the scene shows or says.
4. Save Demo -> Scene -> CITES -> Element, with each element's content hash at
   planning time, so a later crawl shows exactly which scenes went stale.
"""

import json
import re
import secrets
import time
from dataclasses import asdict, dataclass, field

from . import queries as Q
from .db import GraphStore
from .ids import normalize_url, origin, page_id
from .queries import STALE_COST
from .schema import apply_schema

STOPWORDS = {"a", "an", "the", "to", "of", "and", "or", "in", "on", "for", "my", "how", "i", "do", "can",
             "show", "me", "demo", "is", "with", "from", "into", "your", "you", "it", "this", "that"}
# Lower is better: show the thing the user can act on before text that merely mentions it.
KIND_RANK = {"button": 0, "link": 0, "heading": 1, "price": 2, "text": 2, "input": 3}
RESULT_KINDS = ("text", "heading", "price")


class PlanError(Exception):
    pass


@dataclass
class Fact:
    id: str
    kind: str
    text: str
    selector: str
    bbox: list
    content_hash: str
    form: str | None = None
    input_type: str | None = None
    tag: str | None = None
    options: list | None = None
    click_note: str | None = None


@dataclass
class Candidate:
    fact_id: str
    kind: str
    text: str
    page_id: str
    page_path: str
    is_submit: bool
    click_effect: str | None
    click_note: str | None
    score: float
    cost: float | None = None


@dataclass
class Hop:
    src: str
    dst: str
    via: str
    action: str
    label: str
    inputs: dict | None
    cost: float


@dataclass
class Citation:
    fact_id: str
    role: str  # shown | click | fill | result
    text: str
    hash: str
    bbox: list


@dataclass
class Scene:
    index: int
    kind: str  # intro | navigate | form | action | result | highlight
    page_id: str
    page_path: str
    page_title: str
    screenshot: str | None
    narration: str
    steps: list
    cites: list[Citation] = field(default_factory=list)

    @property
    def zoom(self) -> list | None:
        boxes = [c.bbox for c in self.cites if c.bbox]
        if not boxes:
            return None
        x0 = min(b[0] for b in boxes)
        y0 = min(b[1] for b in boxes)
        x1 = max(b[0] + b[2] for b in boxes)
        y1 = max(b[1] + b[3] for b in boxes)
        return [x0, y0, x1 - x0, y1 - y0]


@dataclass
class DemoPlan:
    id: str
    goal: str
    crawl_id: str
    target: Candidate
    route_pages: list[str]
    route_paths: list[str]
    hops: list[Hop]
    cost: float
    scenes: list[Scene]
    considered: list[Candidate]

    def to_dict(self) -> dict:
        d = asdict(self)
        for s, sd in zip(self.scenes, d["scenes"]):
            sd["zoom"] = s.zoom
        return d


def fts_query(goal: str, any_word: bool = False) -> str | None:
    words = [w for w in re.findall(r"[a-z0-9]+", goal.lower()) if w not in STOPWORDS]
    if not words:
        return None
    return ("|" if any_word else " ").join(words)


def short_title(title: str | None, fallback: str) -> str:
    """'Pricing · Ledgerly' -> 'Pricing'."""
    if not title:
        return fallback
    return re.split(r"\s+[·|–—-]\s+", title)[0].strip() or fallback


def product_name(title: str | None, fallback: str) -> str:
    """'Home · Ledgerly' -> 'Ledgerly'."""
    parts = re.split(r"\s+[·|–—-]\s+", title or "")
    return parts[-1].strip() if len(parts) > 1 else fallback


def join_names(names: list[str]) -> str:
    names = [n for n in names if n]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


NUMBER = re.compile(r"\d[\d,.]*")


def unsupported_numbers(scene: Scene, extra: list[str]) -> list[str]:
    """Every number a scene says must appear in something it cites (or a page title it names)."""
    sources = " ".join([c.text for c in scene.cites] + extra)
    have = {n.rstrip(".,") for n in NUMBER.findall(sources)}
    return [n.rstrip(".,") for n in NUMBER.findall(scene.narration) if n.rstrip(".,") not in have]


class Planner:
    def __init__(self, store: GraphStore, root_url: str):
        self.store = store
        self.root_url = root_url
        self.site_origin = origin(root_url)
        self._pages: dict[str, dict] = {}
        self._elements: dict[str, list[Fact]] = {}
        self._routes: dict[tuple[str, str], tuple[list[str], list[Hop], float] | None] = {}
        self.product = self.site_origin

    # --- graph reads ----------------------------------------------------------

    async def _page(self, pid: str) -> dict:
        if pid not in self._pages:
            row = await self.store.one(Q.PAGES, {"ids": [pid]})
            if not row:
                raise PlanError(f"page {pid} is not in the graph")
            self._pages[pid] = {"id": row[0], "url": row[1], "path": row[2], "title": row[3], "screenshot": row[4]}
        return self._pages[pid]

    async def _facts(self, pid: str) -> list[Fact]:
        if pid not in self._elements:
            rows = await self.store.query(Q.PAGE_ELEMENTS, {"page_id": pid})
            self._elements[pid] = [Fact(*r) for r in rows]
        return self._elements[pid]

    async def _fact(self, pid: str, fid: str) -> Fact:
        for f in await self._facts(pid):
            if f.id == fid:
                return f
        raise PlanError(f"element {fid} is no longer on page {pid}")

    async def _route(self, start: str, target: str, max_len: int) -> tuple[list[str], list[Hop], float] | None:
        key = (start, target)
        if key in self._routes:
            return self._routes[key]
        if start == target:
            self._routes[key] = ([start], [], 0.0)
            return self._routes[key]
        row = await self.store.one(Q.SHORTEST_PATH, {"start": start, "target": target, "max_len": max_len})
        route = None
        if row and row[2] < STALE_COST:
            pages, rels, weight = row
            hops = [Hop(src=pages[i], dst=pages[i + 1], via=r["via"], action=r["action"], label=r["label"],
                        inputs=json.loads(r["inputs"]) if r["inputs"] else None, cost=r["cost"] or 1)
                    for i, r in enumerate(rels)]
            route = (pages, hops, float(weight))
        self._routes[key] = route
        return route

    async def _final_hop(self, c: Candidate) -> Hop | None:
        """If acting on the target navigates somewhere, that is the last hop of the demo."""
        if c.kind not in ("button", "link"):
            return None
        row = await self.store.one(Q.ELEMENT_NAVIGATION, {"fact_id": c.fact_id})
        if not row:
            return None
        dst, action, label, inputs, cost = row
        return Hop(src=c.page_id, dst=dst, via=c.fact_id, action=action, label=label,
                   inputs=json.loads(inputs) if inputs else None, cost=cost or 1)

    async def latest_crawl(self) -> str:
        row = await self.store.one(Q.LATEST_COMPLETED_CRAWL, {"site_id": self.site_origin})
        if not row:
            raise PlanError("No completed crawl yet. Run `demograph explore` first.")
        return row[0]

    # --- step 1: what is the goal about? ------------------------------------

    async def candidates(self, goal: str) -> list[Candidate]:
        rows = []
        for any_word in (False, True):  # all words first, then any word
            q = fts_query(goal, any_word)
            if q:
                rows = await self.store.query(Q.FIND_ELEMENTS, {"query": q})
            if rows:
                break
        return [Candidate(fact_id=r[0], kind=r[1], text=r[2], page_id=r[3], page_path=r[4],
                          is_submit=bool(r[5]), click_effect=r[6], click_note=r[7], score=float(r[8]))
                for r in rows]

    # --- the whole plan -------------------------------------------------------

    async def plan(self, goal: str, start_path: str = "/", max_len: int = 8) -> DemoPlan:
        await self.store.query(Q.BACKFILL_LINK_COSTS)
        crawl_id = await self.latest_crawl()
        start = page_id(normalize_url(start_path, self.site_origin + "/"))
        home = await self._page(start)
        self.product = product_name(home["title"], self.site_origin)

        found = await self.candidates(goal)
        if not found:
            raise PlanError(f"Nothing in the product matches {goal!r}.")
        reachable = []
        for c in found:
            route = await self._route(start, c.page_id, max_len)
            if route is None:
                continue
            final = await self._final_hop(c)
            c.cost = route[2] + (final.cost if final else 0)
            reachable.append(c)
        if not reachable:
            raise PlanError(f"Found {len(found)} matches for {goal!r}, but none can be reached from {start_path}.")
        reachable.sort(key=lambda c: (KIND_RANK.get(c.kind, 4), -c.score, c.cost, len(c.text)))
        target = reachable[0]

        pages, hops, cost = await self._route(start, target.page_id, max_len)
        final = await self._final_hop(target)
        scenes = await self._scenes(target, pages, hops, final, start_path)
        demo_id = "demo-" + "-".join(re.findall(r"[a-z0-9]+", goal.lower()))[:32] + "-" + secrets.token_hex(2)
        route_pages = pages + ([final.dst] if final else [])
        route_paths = [(await self._page(pid))["path"] for pid in route_pages]
        return DemoPlan(id=demo_id, goal=goal, crawl_id=crawl_id, target=target, route_pages=route_pages,
                        route_paths=route_paths, hops=hops + ([final] if final else []),
                        cost=target.cost, scenes=scenes, considered=reachable[:8])

    # --- step 3: scenes -------------------------------------------------------

    async def _headline(self, pid: str) -> Fact | None:
        facts = await self._facts(pid)
        for kind in ("heading", "text"):
            for f in facts:
                if f.kind == kind and f.text:
                    return f
        return None

    async def _new_message(self, src: str, dst: str) -> Fact | None:
        """The first thing on the destination page that was not on the page before it."""
        before = {f.text for f in await self._facts(src)}
        for f in await self._facts(dst):
            if f.kind in RESULT_KINDS and f.text and f.text not in before:
                return f
        return await self._headline(dst)

    @staticmethod
    def _cite(f: Fact, role: str, text: str | None = None) -> Citation:
        return Citation(fact_id=f.id, role=role, text=text or f.text, hash=f.content_hash, bbox=f.bbox)

    async def _scene(self, index: int, kind: str, pid: str, narration: str, steps: list, cites: list) -> Scene:
        p = await self._page(pid)
        return Scene(index=index, kind=kind, page_id=pid, page_path=p["path"],
                     page_title=short_title(p["title"], p["path"]), screenshot=p["screenshot"],
                     narration=narration, steps=steps, cites=cites)

    async def _scenes(self, target: Candidate, pages: list[str], hops: list[Hop], final: Hop | None,
                      start_path: str) -> list[Scene]:
        scenes: list[Scene] = []
        start = pages[0]
        start_page = await self._page(start)
        head = await self._headline(start)
        quote = f" “{head.text}”" if head else ""
        intro = (f"This is {self.product}.{quote}" if start_path == "/"
                 else f"We start on the {short_title(start_page['title'], start_page['path'])} page.{quote}")
        scenes.append(await self._scene(0, "intro", start, intro, [{"do": "goto", "url": start_page["url"]}],
                                        [self._cite(head, "shown")] if head else []))

        for hop in hops + ([final] if final else []):
            via = await self._fact(hop.src, hop.via)
            dst = await self._page(hop.dst)
            dst_title = short_title(dst["title"], dst["path"])
            if hop.action == "submit":
                fields = [f for f in await self._facts(hop.src) if f.kind == "input" and f.form == via.form]
                values = hop.inputs or {}
                steps = []
                for f in fields:
                    value = values.get(f.text)
                    if value is None:
                        continue
                    steps.append({"do": "select" if f.tag == "select" else "fill", "selector": f.selector, "value": value})
                steps.append({"do": "click", "selector": via.selector, "expect_url": dst["url"]})
                narration = f"Fill in {join_names([f.text for f in fields])}, then click “{via.text}”."
                cites = [self._cite(f, "fill") for f in fields] + [self._cite(via, "click")]
                scenes.append(await self._scene(len(scenes), "form", hop.src, narration, steps, cites))
            else:
                narration = f"Click “{via.text}” to open the {dst_title} page."
                steps = [{"do": "click", "selector": via.selector, "expect_url": dst["url"]}]
                scenes.append(await self._scene(len(scenes), "navigate", hop.src, narration, steps,
                                                [self._cite(via, "click")]))

        if final:
            msg = await self._new_message(final.src, final.dst)
            if msg:
                verb = "opens" if msg.kind == "heading" else "shows"
                stop = "" if msg.text.endswith((".", "!", "?")) else "."
                scenes.append(await self._scene(len(scenes), "result", final.dst, f"{self.product} {verb} “{msg.text}”{stop}",
                                                [{"do": "wait", "ms": 1500}], [self._cite(msg, "shown")]))
            return scenes

        fact = await self._fact(target.page_id, target.fact_id)
        if target.kind in ("button", "link"):
            scenes.append(await self._scene(len(scenes), "action", target.page_id, f"Click “{fact.text}”.",
                                            [{"do": "click", "selector": fact.selector}], [self._cite(fact, "click")]))
            if target.click_effect == "feedback" and target.click_note:
                scenes.append(await self._scene(
                    len(scenes), "result", target.page_id, f"{self.product} confirms: “{target.click_note}”",
                    [{"do": "wait", "ms": 1500}], [self._cite(fact, "result", target.click_note)]))
        else:
            title = short_title((await self._page(target.page_id))["title"], target.page_path)
            scenes.append(await self._scene(len(scenes), "highlight", target.page_id, f"On the {title} page: “{fact.text}”",
                                            [{"do": "scroll_to", "selector": fact.selector}], [self._cite(fact, "shown")]))
        return scenes

    # --- step 4: save -------------------------------------------------------

    async def verify(self, plan: DemoPlan) -> list[str]:
        problems = []
        titles = [s.page_title for s in plan.scenes] + [self.product]
        for s in plan.scenes:
            if not s.cites:
                problems.append(f"scene {s.index} cites nothing")
            bad = unsupported_numbers(s, titles)
            if bad:
                problems.append(f"scene {s.index} says {', '.join(bad)} but nothing it cites does")
        return problems

    async def save(self, plan: DemoPlan) -> None:
        await apply_schema(self.store)
        problems = await self.verify(plan)
        if problems:
            raise PlanError("Refusing to save an unverifiable demo: " + "; ".join(problems))
        now = int(time.time() * 1000)
        await self.store.query(Q.SAVE_DEMO, {
            "crawl_id": plan.crawl_id, "target_id": plan.target.fact_id, "demo_id": plan.id, "goal": plan.goal,
            "now": now, "path_cost": plan.cost, "scene_count": len(plan.scenes),
            "start_path": plan.scenes[0].page_path})
        scene_ids = [f"{plan.id}-s{s.index}" for s in plan.scenes]
        await self.store.query(Q.SAVE_SCENES, {"demo_id": plan.id, "scenes": [
            {"id": sid, "index": s.index, "kind": s.kind, "narration": s.narration, "steps": json.dumps(s.steps),
             "screenshot": s.screenshot, "zoom": s.zoom, "page_title": s.page_title, "page_id": s.page_id}
            for sid, s in zip(scene_ids, plan.scenes)]})
        await self.store.query(Q.SAVE_CITATIONS, {"cites": [
            {"scene_id": sid, "fact_id": c.fact_id, "role": c.role, "hash": c.hash, "text": c.text}
            for sid, s in zip(scene_ids, plan.scenes) for c in s.cites]})
        if len(scene_ids) > 1:
            await self.store.query(Q.LINK_SCENES, {"pairs": [[a, b] for a, b in zip(scene_ids, scene_ids[1:])]})
