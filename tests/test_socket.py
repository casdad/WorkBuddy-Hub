import asyncio
import socket
import threading
import time

import httpx
import uvicorn

from hub.api import create_app
from .fakes import Upstream,credential


async def test_real_http_disconnect_closes_upstream(tmp_path):
    upstream=Upstream()
    upstream.mode='slow'
    app=create_app(tmp_path,client=httpx.AsyncClient(transport=httpx.MockTransport(upstream.handle)),password='socket-password-123',scheduler=False)
    sock=socket.socket()
    sock.bind(('127.0.0.1',0))
    port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(.02)
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',trust_env=False) as client:
            login=await client.post('/api/login',json={'password':'socket-password-123'})
            client.headers['X-CSRF-Token']=login.json()['csrf']
            added=await client.post('/api/accounts',json={'name':'socket','credential':credential()})
            assert added.status_code==200
            key=(await client.post('/api/keys',json={'name':'socket'})).json()['key']
            async with client.stream('POST','/v1/chat/completions',headers={'Authorization':'Bearer '+key},json={'model':'test-model','messages':[{'role':'user','content':'hi'}],'stream':True}) as response:
                assert response.status_code==200
                async for line in response.aiter_lines():
                    if line.startswith('data:'):
                        break
            for _ in range(100):
                row=app.state.hub.store.one('SELECT * FROM requests')
                if row and row['settled'] and app.state.hub.inflight[added.json()['id']]==0:
                    break
                await asyncio.sleep(.02)
            assert row['settled']==1
            assert upstream.last_stream.closed
            assert app.state.hub.inflight[added.json()['id']]==0
            # Only one dispatch is allowed, regardless of cancellation timing.
            assert upstream.chat_calls==1
    finally:
        server.should_exit=True
        await asyncio.to_thread(thread.join,5)
        sock.close()
