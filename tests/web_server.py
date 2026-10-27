"""Browser test server with isolated temporary storage and fake upstream only."""
import asyncio
import tempfile
from pathlib import Path
import httpx
from hub.api import create_app
from .fakes import Upstream,credential

upstream=Upstream()
folder=Path(tempfile.mkdtemp(prefix='wbh-browser-test-'))
app=create_app(folder,client=httpx.AsyncClient(transport=httpx.MockTransport(upstream.handle)),password='browser-test-password',scheduler=False)
async def seed():
    aid=await app.state.hub.add_account('演示账号 · 自动化测试','workbuddy_cn',credential())
    await app.state.hub.sync(aid)
asyncio.run(seed())
