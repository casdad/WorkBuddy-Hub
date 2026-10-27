import httpx
import pytest_asyncio
from hub.api import create_app
from .fakes import Upstream


@pytest_asyncio.fixture
async def env(tmp_path):
    upstream=Upstream()
    client=httpx.AsyncClient(transport=httpx.MockTransport(upstream.handle))
    app=create_app(tmp_path,client=client,password='test-password-123',scheduler=False)
    browser=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver')
    login=await browser.post('/api/login',json={'password':'test-password-123'})
    browser.headers['x-csrf-token']=login.json()['csrf']
    yield app.state.hub,browser,upstream,tmp_path
    await browser.aclose()
    await client.aclose()
    await app.state.hub.store.aclose()
