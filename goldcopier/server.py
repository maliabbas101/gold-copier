import hashlib
import json
import os
import re
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .signals import parse
from .store import Store


def create_app(config_path=None, db_path=None):
    config = json.loads(Path(config_path or os.environ.get('COPIER_CONFIG', 'config.json')).read_text())
    admin = os.environ.get('COPIER_ADMIN_TOKEN', '')
    hashes = config['accounts']
    if (len(admin) < 32 or not hashes
            or any(not re.fullmatch(r'[0-9a-f]{64}', v) for v in hashes.values())
            or len(set(hashes.values())) != len(hashes)
            or hashlib.sha256(admin.encode()).hexdigest() in hashes.values()):
        raise ValueError('Set a random admin token (32+ chars) and account SHA256 token hashes')
    store = Store(db_path or os.environ.get('COPIER_DB', 'copier.db'))
    app = FastAPI(title='Gold community copier', docs_url=None, redoc_url=None)
    app.state.store = store

    def token(authorization: str = Header(default='')):
        if not authorization.startswith('Bearer '):
            raise HTTPException(401, 'Bearer token required')
        return authorization[7:]

    def require_admin(value=Depends(token)):
        if not secrets.compare_digest(value, admin):
            raise HTTPException(403, 'Invalid token')

    def account(value=Depends(token)):
        if len(value) < 32:
            raise HTTPException(403, 'Invalid token')
        digest = hashlib.sha256(value.encode()).hexdigest()
        for name, expected in hashes.items():
            if secrets.compare_digest(digest, expected):
                return name
        raise HTTPException(403, 'Invalid token')

    @app.get('/health')
    def health():
        return {'ok': True, 'mode': 'demo-only'}

    @app.get('/admin/status', dependencies=[Depends(require_admin)])
    def status():
        return store.status()

    class Pause(BaseModel):
        paused: bool

    @app.post('/admin/pause', dependencies=[Depends(require_admin)])
    def pause(body: Pause):
        store.pause(body.paused)
        return {'paused': body.paused}

    class Preview(BaseModel):
        text: str = Field(max_length=4000)

    @app.post('/admin/preview', dependencies=[Depends(require_admin)])
    def preview(body: Preview):
        try:
            return parse(body.text).asdict()
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    class Result(BaseModel):
        state: str
        detail: str = Field(max_length=500)
        ticket: int | None = None

    @app.post('/agent/claim')
    def claim(name=Depends(account)):
        return store.claim(name)

    @app.post('/agent/jobs/{job_id}/result')
    def result(job_id: str, body: Result, name=Depends(account)):
        try:
            store.finish(name, job_id, body.model_dump())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {'ok': True}

    return app
