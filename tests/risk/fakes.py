"""Minimal asyncpg.Pool stand-in: routes queries to canned rows by substring."""


class FakePool:
    def __init__(self, routes=None):
        self.routes = routes or {}
        self.executed = []

    def _match(self, query):
        for needle, result in self.routes.items():
            if needle in query:
                if isinstance(result, Exception):
                    raise result
                return result
        return None

    async def fetchrow(self, query, *args):
        return self._match(query)

    async def fetch(self, query, *args):
        return self._match(query) or []

    async def execute(self, query, *args):
        self.executed.append((query, args))
