"""Web package. Uvicorn target: ``app.web:app``.

The ASGI callable is LAZY: importing this package (e.g. by the test-suite)
must not boot an application against the developer's ./dev.db. The real app
is created on the first ASGI call.
"""

from app.web.app import create_app


class _LazyApp:
    _instance = None

    async def __call__(self, scope, receive, send):
        if self._instance is None:
            self._instance = create_app()
        return await self._instance(scope, receive, send)


app = _LazyApp()
