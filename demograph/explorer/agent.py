"""One Explorer agent: claims a page from the graph's shared frontier, reads it in a
real browser, and writes what it found back to the graph.

Several agents (in one process or many) cooperate through the graph alone: the
frontier, what each page said last time, and what each button did last time all
live in FalkorDB, so nothing is repeated and a crashed crawl can be resumed.
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.robotparser import RobotFileParser

from playwright.async_api import BrowserContext, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .. import queries as Q
from ..db import GraphStore
from ..ids import content_hash, fact_id, normalize_url, origin, page_id
from .extract import EXTRACT_JS, FEEDBACK_JS, is_dangerous, synthetic_value

SKIP_EXTENSIONS = (".pdf", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".mp4", ".csv", ".xlsx", ".dmg", ".exe")
KNOWN_EFFECTS = ("navigates", "feedback", "none")


def now_ms() -> int:
    return int(time.time() * 1000)


def describe(text: str | None, options: list | None) -> str | None:
    """Human-readable value of a fact, used in change reports (a dropdown is its options)."""
    return f"{text}: {' | '.join(options)}" if options else text


@dataclass
class AgentStats:
    pages: int = 0
    clicks_run: int = 0
    clicks_reused: int = 0
    failures: int = 0
    changes: int = 0


@dataclass
class AgentEnv:
    """Everything an agent needs that is shared across agents in one crawl."""
    store: GraphStore
    crawl_id: str
    root_url: str
    max_depth: int
    interact: bool
    shots_dir: Path
    max_clicks_per_page: int = 12
    stale_after_ms: int = 90_000
    pace_s: float = 0.0  # pause after each page so a live dashboard can be followed
    robots: RobotFileParser | None = None
    log: Callable[[str], None] = print
    site_origin: str = field(init=False)

    def __post_init__(self):
        self.site_origin = origin(self.root_url)


async def settle(page: Page, timeout: int = 4000) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except PlaywrightTimeout:
        pass


class ExplorerAgent:
    def __init__(self, agent_id: str, env: AgentEnv, context: BrowserContext):
        self.id = agent_id
        self.env = env
        self.ctx = context
        self.stats = AgentStats()

    # --- main loop ------------------------------------------------------------

    async def run(self) -> AgentStats:
        store, crawl = self.env.store, self.env.crawl_id
        while True:
            row = await store.one(Q.CLAIM_NEXT_PAGE, {"crawl_id": crawl, "agent_id": self.id, "now": now_ms()})
            if row:
                pid, url, depth = row
                await self._visit_safely(pid, url, int(depth))
                if self.env.pace_s:
                    await asyncio.sleep(self.env.pace_s)
                continue
            status, max_pages, pending, claimed, done = await store.one(Q.FRONTIER_COUNTS, {"crawl_id": crawl})
            pending, claimed, done = int(pending or 0), int(claimed or 0), int(done or 0)
            if status != "running" or (claimed == 0 and (pending == 0 or done >= max_pages)):
                return self.stats
            # Another agent is still working and may add pages. Reclaim work from dead agents.
            await store.query(Q.REQUEUE_STALE_CLAIMS, {
                "crawl_id": crawl, "stale_before": now_ms() - self.env.stale_after_ms, "max_attempts": 3})
            await asyncio.sleep(0.4)

    async def _visit_safely(self, pid: str, url: str, depth: int) -> None:
        try:
            await self.visit(pid, url, depth)
        except Exception as e:  # one bad page must not stop the crawl
            self.stats.failures += 1
            self.env.log(f"[{self.id}] FAILED {url}: {type(e).__name__}: {str(e)[:160]}")
            await self.env.store.query(Q.MARK_FRONTIER, {
                "crawl_id": self.env.crawl_id, "page_id": pid, "status": "failed",
                "now": now_ms(), "error": f"{type(e).__name__}: {str(e)[:300]}"})

    # --- one page -----------------------------------------------------------

    async def visit(self, pid: str, url: str, depth: int) -> None:
        env, store = self.env, self.env.store
        started = time.monotonic()
        page = await self.ctx.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            await settle(page)
            final = normalize_url(page.url)
            if final and final != url:
                await self._record_redirect(pid, url, final, depth)
                return
            snap = await page.evaluate(EXTRACT_JS)
            shot = env.shots_dir / f"{pid}.png"
            shot.parent.mkdir(parents=True, exist_ok=True)
            try:
                await page.screenshot(path=str(shot), full_page=True)
            except Exception:  # some environments cannot capture full pages; the viewport is enough
                await page.screenshot(path=str(shot))
        finally:
            await page.close()

        facts = self._facts(url, snap["elements"])
        memory = {r[0]: {"hash": r[1], "text": describe(r[2], r[8]), "click_hash": r[3], "click_effect": r[4],
                         "click_target_url": r[5], "click_note": r[6], "click_inputs": r[7]}
                  for r in await store.query(Q.PAGE_MEMORY, {"page_id": pid})}
        fingerprint = content_hash(*sorted(f"{f['id']}:{f['content_hash']}" for f in facts.values()))
        prev = await store.one(Q.PAGE_FINGERPRINT, {"page_id": pid})
        unchanged = bool(memory) and prev is not None and prev[0] == fingerprint

        now = now_ms()
        if unchanged:
            await store.query(Q.TOUCH_PAGE_ELEMENTS, {"page_id": pid, "now": now})
        else:
            await self._write_facts(pid, facts, memory, now)

        links = await self._navigation(url, depth, facts, memory)
        if links:
            await store.query(Q.RECORD_NAVIGATION, {"page_id": pid, "crawl_id": env.crawl_id, "now": now_ms(), "links": links})

        await store.query(Q.RECORD_VISIT, {
            "crawl_id": env.crawl_id, "page_id": pid, "url": url, "title": snap["title"], "now": now_ms(),
            "fingerprint": fingerprint, "screenshot": shot.as_posix(), "status_code": resp.status if resp else None,
            "agent_id": self.id, "unchanged": unchanged, "element_count": len(facts),
            "ms": int((time.monotonic() - started) * 1000)})
        self.stats.pages += 1
        path = url.split(env.site_origin, 1)[-1] or "/"
        env.log(f"[{self.id}] {path:<32} depth {depth}  {len(facts):>3} facts  {len(links):>2} links"
                f"{'  (unchanged, from memory)' if unchanged else ''}")

    def _facts(self, url: str, elements: list[dict]) -> dict[str, dict]:
        path = url.split(self.env.site_origin, 1)[-1] or "/"
        facts: dict[str, dict] = {}
        for el in elements:
            fid = fact_id(url, el["selector"])
            options = el.get("options")
            facts[fid] = {
                "id": fid, "url": url, "path": path, "selector": el["selector"], "tag": el["tag"],
                "role": el.get("role") or None, "kind": el["kind"], "text": el["text"], "href": el.get("href"),
                "input_type": el.get("input_type"), "name": el.get("name"), "required": el.get("required"),
                "options": options or None, "form": el.get("form"), "is_submit": bool(el.get("is_submit")),
                "bbox": el["bbox"],
                "content_hash": content_hash(el["kind"], el["text"], el.get("href"), "|".join(options or [])),
            }
        return facts

    async def _write_facts(self, pid: str, facts: dict, memory: dict, now: int) -> None:
        store, crawl = self.env.store, self.env.crawl_id
        await store.query(Q.UPSERT_ELEMENTS, {"page_id": pid, "now": now, "elements": list(facts.values())})
        if not memory:
            return  # first time we see this page: nothing to compare against
        changes = []
        for fid, f in facts.items():
            old = memory.get(fid)
            new_text = describe(f["text"], f["options"])
            if old is None:
                changes.append({"id": fid, "kind": "added", "old_text": None, "new_text": new_text,
                                "old_hash": None, "new_hash": f["content_hash"]})
            elif old["hash"] != f["content_hash"]:
                changes.append({"id": fid, "kind": "changed", "old_text": old["text"], "new_text": new_text,
                                "old_hash": old["hash"], "new_hash": f["content_hash"]})
        removed = [fid for fid in memory if fid not in facts]
        for fid in removed:
            changes.append({"id": fid, "kind": "removed", "old_text": memory[fid]["text"], "new_text": None,
                            "old_hash": memory[fid]["hash"], "new_hash": None})
        if removed:
            await store.query(Q.REMOVE_ELEMENTS, {"ids": removed, "now": now})
        if changes:
            await store.query(Q.RECORD_CHANGES, {"crawl_id": crawl, "now": now, "changes": changes})
            self.stats.changes += len(changes)

    async def _record_redirect(self, pid: str, url: str, final: str, depth: int) -> None:
        env = self.env
        if origin(final) == env.site_origin:
            await env.store.query(Q.RECORD_REDIRECT, {
                "page_id": pid, "crawl_id": env.crawl_id, "dst_id": page_id(final), "dst_url": final,
                "dst_path": final.split(env.site_origin, 1)[-1] or "/", "depth": depth, "now": now_ms()})
        await env.store.query(Q.MARK_FRONTIER, {
            "crawl_id": env.crawl_id, "page_id": pid, "status": "redirect", "now": now_ms(), "error": None})
        env.log(f"[{self.id}] {url} redirects to {final}")

    # --- navigation: links, clicks, and form submits ------------------------

    def _allowed(self, url: str) -> bool:
        if origin(url) != self.env.site_origin or url.lower().split("?")[0].endswith(SKIP_EXTENSIONS):
            return False
        robots = self.env.robots
        return robots is None or robots.can_fetch("*", url)

    async def _navigation(self, url: str, depth: int, facts: dict, memory: dict) -> list[dict]:
        env = self.env
        links: list[dict] = []

        def add(via: str, target: str, action: str, label: str, inputs: dict | None = None):
            dst = normalize_url(target)
            if not dst or origin(dst) != env.site_origin or dst == url:
                return
            links.append({
                "via": via, "dst_id": page_id(dst), "dst_url": dst, "dst_path": dst.split(env.site_origin, 1)[-1] or "/",
                "action": action, "label": label[:120], "inputs": json.dumps(inputs) if inputs else None,
                "enqueue": depth + 1 <= env.max_depth and self._allowed(dst), "depth": depth + 1})

        clicks_left = env.max_clicks_per_page
        recorded = []
        for fid, f in facts.items():
            if is_dangerous(f["text"]):
                continue
            if f["kind"] == "link":
                if f["href"]:
                    add(fid, f["href"], "link", f["text"])
                continue
            if f["kind"] != "button":
                continue
            fields = [x for x in facts.values() if f["is_submit"] and x["kind"] == "input" and x["form"] == f["form"]]
            click_hash = content_hash(f["content_hash"], *sorted(x["content_hash"] for x in fields))
            known = memory.get(fid)
            if known and known["click_hash"] == click_hash and known["click_effect"] in KNOWN_EFFECTS:
                self.stats.clicks_reused += 1
                result = {"effect": known["click_effect"], "target_url": known["click_target_url"],
                          "inputs": json.loads(known["click_inputs"]) if known["click_inputs"] else None}
            elif env.interact and clicks_left > 0:
                clicks_left -= 1
                self.stats.clicks_run += 1
                result = await self._click(url, f, fields)
                recorded.append({"id": fid, "effect": result["effect"], "target_url": result.get("target_url"),
                                 "note": result.get("note"), "hash": click_hash,
                                 "inputs": json.dumps(result["inputs"]) if result.get("inputs") else None})
            elif f["href"]:
                result = {"effect": "navigates", "target_url": f["href"], "inputs": None}  # read from markup
            else:
                continue
            if result["effect"] == "navigates" and result.get("target_url"):
                add(fid, result["target_url"], "submit" if f["is_submit"] else "click", f["text"], result.get("inputs"))

        if recorded:
            await env.store.query(Q.RECORD_CLICKS, {"clicks": recorded, "now": now_ms()})
        return links

    async def _click(self, url: str, fact: dict, fields: list[dict]) -> dict:
        """Click one element on a fresh copy of the page and report what happened."""
        page = await self.ctx.new_page()
        dialogs: list[str] = []

        async def on_dialog(d):
            dialogs.append(d.message)
            await d.dismiss()  # never confirm anything

        page.on("dialog", on_dialog)
        inputs: dict[str, str] = {}
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            await settle(page, 3000)
            if any((x["input_type"] or "") == "password" for x in fields):
                return {"effect": "skipped", "note": "form asks for a password"}
            for x in fields:
                loc = page.locator(x["selector"]).first
                if x["tag"] == "select":
                    # Keep what the page preselected (e.g. the plan picked on /pricing).
                    chosen = await loc.evaluate("el => el.selectedIndex > 0 || (el.options[0] && el.options[0].value) "
                                                "? el.options[el.selectedIndex].text : null")
                    if not chosen and len(x["options"] or []) > 1:
                        await loc.select_option(index=1)
                        chosen = x["options"][1]
                    inputs[x["text"] or x["name"] or "select"] = chosen or ""
                elif x["input_type"] in ("checkbox", "radio"):
                    if x["required"]:
                        await loc.check()
                else:
                    value = synthetic_value(x)
                    if value is not None:
                        await loc.fill(value)
                        inputs[x["text"] or x["name"] or "field"] = value
            before = normalize_url(page.url)
            await page.locator(fact["selector"]).first.click(timeout=5000)
            await settle(page, 3000)
            await page.wait_for_timeout(300)
            after = normalize_url(page.url)
            if after and after != before:
                return {"effect": "navigates", "target_url": after, "inputs": inputs or None}
            note = await page.evaluate(FEEDBACK_JS)
            if dialogs:
                note = f"dialog: {dialogs[0]}"
            return {"effect": "feedback" if note else "none", "note": note, "inputs": inputs or None}
        except Exception as e:
            return {"effect": "error", "note": f"{type(e).__name__}: {str(e)[:200]}"}
        finally:
            await page.close()
