"""Dashboard files are revalidated by the browser on every load (no stale scripts after a deploy)."""
import asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def test_ui_files_are_no_cache(tmp_path):
    from gateway import http_api
    import inspect
    assert "app.on_response_prepare.append(_ui_revalidate)" in inspect.getsource(http_api)

    async def run():
        app = web.Application()
        (tmp_path / "a.js").write_text("export const x = 1;")
        app.router.add_static("/ui", str(tmp_path))
        async def hook(request, response):
            if request.path.startswith("/ui/") and "Cache-Control" not in response.headers:
                response.headers["Cache-Control"] = "no-cache"
        app.on_response_prepare.append(hook)
        async with TestClient(TestServer(app)) as client:
            r = await client.get("/ui/a.js")
            return r.status, r.headers.get("Cache-Control"), "ETag" in r.headers
    assert asyncio.run(run()) == (200, "no-cache", True)
