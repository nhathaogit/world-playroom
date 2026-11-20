"""Private, single-owner hosted entry point. Secrets come only from environment."""
import base64
import hmac
import os
from urllib.parse import urlsplit


class PrivateSite:
    def __init__(self, app, username, password):
        if not username or len(password) < 20:
            raise RuntimeError('Set PLAYROOM_LOGIN_USERNAME and PLAYROOM_LOGIN_PASSWORD (at least 20 characters) before publishing')
        self.app = app
        self.username, self.password = username.encode('utf-8'), password.encode('utf-8')

    async def reply(self, send, status, body, headers=()):
        await send({'type':'http.response.start','status':status,'headers':[
            (b'content-type', b'text/plain; charset=utf-8'),(b'cache-control',b'no-store'),
            (b'x-content-type-options',b'nosniff'), *headers]})
        await send({'type':'http.response.body','body':body})

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            return await self.app(scope, receive, send)
        if scope['type'] != 'http':
            return await send({'type':'websocket.close','code':1008})
        if scope.get('path') == '/healthz' and scope.get('method') in {'GET','HEAD'}:
            return await self.reply(send,200,b'ok')
        headers = {k.lower():v for k,v in scope.get('headers',[])}
        try:
            scheme, value = headers.get(b'authorization',b'').split(b' ',1)
            user, password = base64.b64decode(value,validate=True).split(b':',1)
            valid = scheme.lower()==b'basic' and hmac.compare_digest(user,self.username) & hmac.compare_digest(password,self.password)
        except (ValueError,TypeError):
            valid = False
        if not valid:
            return await self.reply(send,401,b'Authentication required',[(b'www-authenticate',b'Basic realm="Private Playroom", charset="UTF-8"')])
        if scope.get('method') not in {'GET','HEAD','OPTIONS'}:
            origin = headers.get(b'origin',b'').decode('latin-1')
            if headers.get(b'sec-fetch-site') == b'cross-site' or (origin and urlsplit(origin).netloc.lower() != headers.get(b'host',b'').decode('latin-1').lower()):
                return await self.reply(send,403,b'Cross-site request refused')
        async def private_send(message):
            if message['type']=='http.response.start':
                message['headers']=[(k,v) for k,v in message.get('headers',[]) if k.lower()!=b'cache-control']+[
                    (b'cache-control',b'private, no-store'),(b'x-robots-tag',b'noindex, nofollow'),
                    (b'x-frame-options',b'DENY'),(b'referrer-policy',b'no-referrer')]
            await send(message)
        await self.app(scope,receive,private_send)


def create_app():
    # Validate login before importing any engine or loading its private saves.
    username=os.environ.get('PLAYROOM_LOGIN_USERNAME','')
    password=os.environ.get('PLAYROOM_LOGIN_PASSWORD','')
    gate=PrivateSite(None,username,password)
    from main import app
    if os.environ.get('PLAYROOM_CLOUD_STORAGE')=='supabase':
        from cloud_snapshot import DurableRequests,Store
        from pathlib import Path
        app=DurableRequests(app,Path(__file__).resolve().parent,
            os.environ['PLAYROOM_SOURCE_DIR'],Store(os.environ.get('SUPABASE_URL',''),os.environ.get('SUPABASE_SERVICE_ROLE_KEY','')))
    gate.app=app
    return gate
