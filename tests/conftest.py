"""Shared fixtures: a Ledgerly sample app on a free port and a throwaway FalkorDB graph.

Tests need FalkorDB on FALKORDB_HOST:FALKORDB_PORT (skipped otherwise) and Playwright Chromium.
"""

import asyncio
import json
import secrets
import shutil
import socket
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sample_app"))

import server as ledgerly  # noqa: E402

from demograph.config import settings  # noqa: E402
from demograph.db import GraphStore  # noqa: E402
from demograph.explorer import CrawlConfig, explore  # noqa: E402


def _falkordb_up() -> bool:
    try:
        with socket.create_connection((settings.host, settings.port), timeout=1):
            return True
    except OSError:
        return False


def pytest_collection_modifyitems(items):
    if not _falkordb_up():
        skip = pytest.mark.skip(reason="FalkorDB is not running")
        for item in items:
            item.add_marker(skip)


class App:
    def __init__(self, base: Path):
        self.catalog = base / "catalog.json"
        shutil.copy(ledgerly.DEFAULT_CATALOG, self.catalog)
        self.server = ledgerly.make_server(port=0, catalog=self.catalog)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def edit(self, change) -> None:
        """Ship a 'release': change(catalog_dict) edits the live product."""
        data = json.loads(self.catalog.read_text(encoding="utf-8"))
        change(data)
        self.catalog.write_text(json.dumps(data), encoding="utf-8")

    def stop(self) -> None:
        self.server.shutdown()


def _drop_graph(name: str) -> None:
    async def drop():
        store = GraphStore(name)
        await store.delete()
        await store.close()
    asyncio.run(drop())
    shutil.rmtree(settings.data_dir / "screenshots" / name, ignore_errors=True)
    shutil.rmtree(settings.data_dir / "videos" / name, ignore_errors=True)
    shutil.rmtree(settings.data_dir / "docs" / name, ignore_errors=True)


@pytest.fixture()
def app(tmp_path):
    a = App(tmp_path)
    yield a
    a.stop()


@pytest.fixture()
def graph():
    name = f"demograph_test_{secrets.token_hex(3)}"
    yield name
    _drop_graph(name)


@pytest.fixture(scope="module")
def crawled(tmp_path_factory):
    """One app + one completed crawl, shared by read-only tests in a module."""
    a = App(tmp_path_factory.mktemp("app"))
    name = f"demograph_test_{secrets.token_hex(3)}"
    crawl(a.url, name)
    yield a, name
    a.stop()
    _drop_graph(name)


def q(graph: str, cypher: str, params: dict | None = None) -> list[list]:
    async def run():
        store = GraphStore(graph)
        try:
            return await store.query(cypher, params)
        finally:
            await store.close()
    return asyncio.run(run())


def crawl(url: str, graph: str, **kw) -> dict:
    cfg = CrawlConfig(root_url=url, graph=graph, agents=kw.pop("agents", 3), **kw)
    return asyncio.run(explore(cfg, log=lambda _: None))
