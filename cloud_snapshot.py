"""Private Supabase Storage snapshots for the single-process free hosting tier.

Only runtime data and newly created assets are sent. Credentials stay in env.
"""
import asyncio
import io
import json
import os
import tarfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote
import httpx

MUTABLE_ROOT={'llm_settings.json','token_usage.jsonl','token_usage_session.json'}
MAX_BYTES=48*1024*1024


def files(root, source):
    root,source=Path(root),Path(source)
    for p in root.rglob('*'):
        if not p.is_file():continue
        rel=p.relative_to(root)
        if p.is_symlink() or any(a.startswith('.') or a=='__pycache__' for a in rel.parts):continue
        if rel.as_posix() in MUTABLE_ROOT:
            yield p;continue
        if rel.parts[0]=='playrooms' and p.suffix.lower() in {'.json','.md','.txt'}:
            yield p;continue
        if rel.parts[0] in {'playrooms','static','avatar'} and p.suffix.lower() in {'.png','.jpg','.jpeg','.webp'} and not (source/rel).exists():
            yield p


def snapshot(root,source):
    buffer=io.BytesIO()
    with tarfile.open(fileobj=buffer,mode='w:gz') as archive:
        for p in files(root,source):archive.add(p,arcname=p.relative_to(root).as_posix(),recursive=False)
    data=buffer.getvalue()
    if len(data)>MAX_BYTES:raise RuntimeError('Private save snapshot exceeds the free storage object limit')
    return data


def restore(data,root):
    root=Path(root).resolve()
    with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as archive:
        members=archive.getmembers()
        if len(members)>20000 or sum(m.size for m in members)>256*1024*1024:
            raise RuntimeError('Invalid save snapshot size')
        checked=[]
        for member in members:
            rel=PurePosixPath(member.name)
            if not member.isfile() or rel.is_absolute() or '..' in rel.parts or '\\' in member.name:
                raise RuntimeError('Invalid save snapshot path')
            if not (member.name in MUTABLE_ROOT or (rel.parts[0] in {'playrooms','static','avatar'}
                    and Path(member.name).suffix.lower() in {'.json','.md','.txt','.png','.jpg','.jpeg','.webp'})):
                raise RuntimeError('Unexpected file in save snapshot')
            target=(root/member.name).resolve()
            if root not in target.parents:raise RuntimeError('Save snapshot escaped data directory')
            checked.append((member,target))
        for member,target in checked:
            target.parent.mkdir(parents=True,exist_ok=True)
            temp=target.with_suffix(target.suffix+'.restore.tmp')
            temp.write_bytes(archive.extractfile(member).read());temp.replace(target)


class Store:
    def __init__(self,url,key,bucket='playroom-private'):
        if not url.startswith('https://') or not key:raise RuntimeError('Configure SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in server secrets')
        self.url=url.rstrip('/')+'/storage/v1'
        self.bucket=bucket
        self.headers={'apikey':key}
        # New sb_secret_* keys are API keys, not JWTs. Only legacy service_role
        # JWTs belong in the Authorization bearer header.
        if key.startswith('eyJ'):
            self.headers['Authorization']='Bearer '+key

    def initialize(self):
        with httpx.Client(timeout=45) as client:
            response=client.get(self.url+'/bucket/'+quote(self.bucket,safe=''),headers=self.headers)
            if response.status_code==404:
                response=client.post(self.url+'/bucket',headers=self.headers,json={'id':self.bucket,'name':self.bucket,'public':False,'file_size_limit':MAX_BYTES})
                if response.status_code not in {200,201}:raise RuntimeError('Cannot create private save bucket; check Supabase configuration')
            elif response.status_code!=200:
                raise RuntimeError('Cannot access private save bucket; check Supabase configuration')
            elif response.json().get('public') is not False:
                raise RuntimeError('Save bucket must be PRIVATE before hosting can start')

    @property
    def object_url(self):return self.url+'/object/'+quote(self.bucket,safe='')+'/runtime-v1.tar.gz'

    def get(self):
        response=httpx.get(self.object_url,headers=self.headers,timeout=45)
        if response.status_code in {400,404}:
            try:code=response.json().get('code') or response.json().get('error')
            except ValueError:code=None
            if code in {'NoSuchKey','not_found','Object not found'}:return None
        if response.status_code!=200:raise RuntimeError('Private save download failed; refusing to start with stale saves')
        return response.content

    def put(self,data):
        response=httpx.post(self.object_url,content=data,headers={**self.headers,'Content-Type':'application/gzip','x-upsert':'true'},timeout=45)
        if response.status_code not in {200,201}:raise RuntimeError('Private save upload failed')


class DurableRequests:
    """Serialize cloud commits with responses; retry pending writes before new work."""
    def __init__(self,app,root,source,store):
        self.app,self.root,self.source,self.store=app,Path(root),Path(source),store
        self.lock=asyncio.Lock()
        self.pending=False

    async def __call__(self,scope,receive,send):
        mutation=scope['type']=='http' and scope.get('path','').startswith('/api/') and scope.get('method') in {'POST','PUT','PATCH','DELETE'}
        if not mutation:return await self.app(scope,receive,send)
        async with self.lock:
            # A failed cloud write must not be overwritten by another browser tab.
            if self.pending:
                try:
                    await asyncio.to_thread(self.store.put,snapshot(self.root,self.source));self.pending=False
                except Exception:
                    return await self.unavailable(send)
            messages=[]
            async def buffer(message):messages.append(message)
            await self.app(scope,receive,buffer)
            status=next((m['status'] for m in messages if m['type']=='http.response.start'),500)
            if status<400:
                try:await asyncio.to_thread(self.store.put,snapshot(self.root,self.source))
                except Exception:
                    self.pending=True
                    # The completed turn remains local. Explicitly report this rather
                    # than claiming it was never saved or asking for duplicate input.
                    for message in messages:
                        if message['type']=='http.response.start':
                            message['headers']=message.get('headers',[])+[(b'x-playroom-save',b'cloud-pending')]
                    print('[Warning] Cloud save pending; retrying before the next mutation (credentials withheld)')
            for message in messages:await send(message)

    async def unavailable(self,send):
        body=json.dumps({'detail':'Cloud save is pending; this new request was not executed. Check Supabase and retry.'}).encode()
        await send({'type':'http.response.start','status':503,'headers':[(b'content-type',b'application/json')]})
        await send({'type':'http.response.body','body':body})
