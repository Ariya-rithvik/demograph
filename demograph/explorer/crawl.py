"""Crawl lifecycle: start, join, or resume a crawl, run N explorer agents, finalize."""

import asyncio
import datetime as dt
import os
import secrets
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from playwright.async_api import async_playwright

from .. import queries as Q
from ..config import settings
from ..db import GraphStore
from ..ids import graph_name_for, normalize_url, origin, page_id
from ..schema import apply_schema
from .agent import AgentEnv, ExplorerAgent, now_ms

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def is_local(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in LOCAL_HOSTS or host.endswith((".localhost", ".test"))


@dataclass
class CrawlConfig:
    root_url: str
    agents: int = 2
    max_pages: int = 40
    max_depth: int = 5
    interact: bool | None = None  # click buttons / submit forms; defaults to on for local apps only
    headless: bool = True
    graph: str | None = None
    join: str | None = None       # crawl id to help with (another process is already running it)
    resume: bool = False          # pick up the latest unfinished crawl after a crash
    pace: float = 0.0             # seconds each agent waits after a page (for watching live)


async def _robots_for(root: str) -> RobotFileParser | None:
    if is_local(root):
        return None
    rp = RobotFileParser(origin(root) + "/robots.txt")
    try:
        await asyncio.to_thread(rp.read)
    except Exception:
        return None
    return rp


async def explore(cfg: CrawlConfig, log: Callable[[str], None] = print) -> dict:
    root = normalize_url(cfg.root_url)
    if not root:
        raise ValueError(f"not an http(s) url: {cfg.root_url}")
    store = GraphStore(cfg.graph or graph_name_for(root))
    try:
        await apply_schema(store)
        site_id = origin(root)
        await store.query(Q.UPSERT_SITE, {"site_id": site_id, "root_url": root, "origin": site_id, "now": now_ms()})
        interact = is_local(root) if cfg.interact is None else cfg.interact
        robots = await _robots_for(root)
        if robots and not robots.can_fetch("*", root):
            raise SystemExit(f"robots.txt does not allow crawling {root}")

        if cfg.join:
            crawl_id = cfg.join
            log(f"Joining crawl {crawl_id}")
        elif cfg.resume:
            row = await store.one(Q.LATEST_RUNNING_CRAWL, {"site_id": site_id})
            if not row:
                raise SystemExit("No unfinished crawl to resume.")
            crawl_id = row[0]
            # The process that held these claims is gone, so hand them back now.
            n = await store.one(Q.REQUEUE_STALE_CLAIMS, {"crawl_id": crawl_id, "stale_before": now_ms() + 1, "max_attempts": 3})
            log(f"Resuming crawl {crawl_id} ({int(n[0])} interrupted pages put back in the queue)")
        else:
            crawl_id = f"crawl-{dt.datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
            prev = await store.one(Q.LATEST_COMPLETED_CRAWL, {"site_id": site_id})
            await store.query(Q.CREATE_CRAWL, {
                "site_id": site_id, "crawl_id": crawl_id, "root_url": root, "now": now_ms(),
                "max_pages": cfg.max_pages, "max_depth": cfg.max_depth, "interact": interact})
            if prev:
                await store.query(Q.LINK_PREVIOUS_CRAWL, {"crawl_id": crawl_id, "prev_id": prev[0]})
            await store.query(Q.SEED_FRONTIER, {
                "crawl_id": crawl_id, "page_id": page_id(root), "url": root,
                "path": root.split(site_id, 1)[-1] or "/", "now": now_ms()})
            log(f"Started {crawl_id} on {root} (graph {store.name}"
                f"{', compared against ' + prev[0] if prev else ', first crawl'})")

        env = AgentEnv(
            store=store, crawl_id=crawl_id, root_url=root, max_depth=cfg.max_depth, interact=interact,
            shots_dir=settings.data_dir / "screenshots" / store.name / crawl_id,
            robots=robots, log=log, pace_s=cfg.pace)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=cfg.headless, args=["--disable-gpu"])
            agents = []
            for i in range(cfg.agents):
                ctx = await browser.new_context(viewport={"width": 1280, "height": 800}, accept_downloads=False)
                agents.append(ExplorerAgent(f"explorer-{os.getpid()}-{i + 1}", env, ctx))
            stats = await asyncio.gather(*(a.run() for a in agents))
            await browser.close()

        clicks_run = sum(s.clicks_run for s in stats)
        clicks_reused = sum(s.clicks_reused for s in stats)
        await store.query(Q.ADD_CRAWL_STATS, {"crawl_id": crawl_id, "clicks_run": clicks_run, "clicks_reused": clicks_reused})
        await _finalize(store, crawl_id, log)
        summary = await crawl_summary(store, crawl_id)
        summary["graph"] = store.name
        return summary
    finally:
        await store.close()


async def _finalize(store: GraphStore, crawl_id: str, log: Callable[[str], None]) -> None:
    if not await store.one(Q.BEGIN_FINALIZE, {"crawl_id": crawl_id}):
        return  # another process already finalized this crawl
    status, max_pages, pending, claimed, done = await store.one(Q.FRONTIER_COUNTS, {"crawl_id": crawl_id})
    capped = int(pending or 0) > 0 and int(done or 0) >= max_pages
    failed = (await store.one(Q.FAILED_PAGES, {"crawl_id": crawl_id}) or [0])[0]
    now = now_ms()
    added = (await store.one(Q.PAGES_ADDED, {"crawl_id": crawl_id, "now": now}) or [0])[0]
    removed = 0
    if not capped and not failed:  # an incomplete crawl did not see everything, so missing pages prove nothing
        removed = (await store.one(Q.PAGES_REMOVED, {"crawl_id": crawl_id, "now": now}) or [0])[0]
    await store.query(Q.MARK_STALE_LINKS, {"crawl_id": crawl_id, "stale_cost": Q.STALE_COST})
    await store.query(Q.COMPLETE_CRAWL, {"crawl_id": crawl_id, "now": now, "pages": int(done or 0), "capped": capped})
    if added or removed:
        log(f"Pages added: {added}, pages removed: {removed}")


async def crawl_summary(store: GraphStore, crawl_id: str) -> dict:
    row = await store.one(Q.CRAWL_SUMMARY, {"crawl_id": crawl_id})
    keys = ["crawl", "status", "visited", "unchanged", "agents", "changes", "clicks_run", "clicks_reused", "started_at", "finished_at"]
    out = dict(zip(keys, row))
    for k in ("visited", "unchanged", "agents", "changes", "clicks_run", "clicks_reused"):
        out[k] = int(out[k] or 0)
    return out
