from falkordb.asyncio import FalkorDB

from .config import settings


class GraphStore:
    """Thin async wrapper around one FalkorDB graph (one graph per product)."""

    def __init__(self, graph_name: str):
        self.name = graph_name
        self._db = FalkorDB(host=settings.host, port=settings.port, password=settings.password)
        self.graph = self._db.select_graph(graph_name)

    async def query(self, cypher: str, params: dict | None = None) -> list[list]:
        result = await self.graph.query(cypher, params or {})
        return result.result_set

    async def ro(self, cypher: str, params: dict | None = None) -> list[list]:
        result = await self.graph.ro_query(cypher, params or {})
        return result.result_set

    async def one(self, cypher: str, params: dict | None = None):
        rows = await self.query(cypher, params)
        return rows[0] if rows else None

    async def delete(self) -> None:
        try:
            await self.graph.delete()
        except Exception as e:  # graph may not exist yet
            if "empty key" not in str(e).lower() and "invalid graph" not in str(e).lower():
                raise

    async def close(self) -> None:
        await self._db.aclose()
