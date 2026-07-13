"""Bilipod web management UI — FastAPI + Jinja2 + SQLite."""

from .server import create_app


class _ResolverProxy:
    async def resolve_url(self, url: str) -> dict:
        import importlib

        module = importlib.import_module(f"{__name__}.resolver")
        return await module.resolve_url(url)

    def __getattr__(self, name: str):
        import importlib

        module = importlib.import_module(f"{__name__}.resolver")
        return getattr(module, name)


resolver = _ResolverProxy()

__all__ = ["create_app", "resolver"]
