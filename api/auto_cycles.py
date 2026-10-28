"""One shared Workspace configuration, scoped library and per-cycle reports."""
from dataclasses import dataclass
from typing import Callable
import json

from fastapi import APIRouter, Depends, HTTPException
from schemas.auto_cycles import AlbumPayload, CycleRequest, CycleSettings, GlobalStatusPayload, PairCheckRequest, TemplatePayload
from services.auto_posting_cycles import content, engine, reports, repository as repo


@dataclass(frozen=True)
class Deps:
    db: Callable
    require_principal: Callable
    require_admin: Callable
    require_permission: Callable
    scope_pipeline: Callable
    audit: Callable
    now: Callable
    enqueue_screening: Callable


def build_router(deps):
    router = APIRouter()

    def guard(conn, principal, pipeline_id, permission='auto_post.self_configure'):
        deps.scope_pipeline(principal,pipeline_id)
        deps.require_permission(principal,permission)
        if not repo.available(conn):
            raise HTTPException(503,{'code':'CYCLE_SCHEMA_REQUIRED','message':'Chức năng đang chờ cập nhật hệ thống.'})
        if not conn.execute('SELECT 1 FROM pipelines WHERE id=?',(pipeline_id,)).fetchone():
            raise HTTPException(404,'Not found')

    def resource(conn, table, resource_id, pipeline_id):
        try:
            return repo.owned(conn,table,resource_id,pipeline_id)
        except repo.CycleError as error:
            raise HTTPException(404,{'code':error.code,'message':error.message}) from error

    @router.get('/api/pipelines/{pipeline_id}/auto-post/cycle-config')
    def get_config(pipeline_id:int, principal=Depends(deps.require_principal)):
        with deps.db() as conn:
            guard(conn,principal,pipeline_id)
            config = repo.config(conn,pipeline_id)
            return {'pipeline_id':pipeline_id,'configured':config is not None,'settings':json.loads(config['settings_json']) if config else CycleSettings().model_dump(),
                    'overview':reports.overview(conn,pipeline_id,deps.now())}

    @router.put('/api/pipelines/{pipeline_id}/auto-post/cycle-config')
    def put_config(pipeline_id:int, data:CycleSettings, principal=Depends(deps.require_principal)):
        with deps.db() as conn:
            guard(conn,principal,pipeline_id)
            conn.execute('BEGIN IMMEDIATE')
            try:
                repo.save_config(conn,pipeline_id,data.model_dump(),principal.user_id,deps.now().isoformat())
            except repo.CycleError as error:
                raise HTTPException(404 if error.code=='RESOURCE_NOT_FOUND' else 409,{'code':error.code,'message':error.message}) from error
        deps.audit('cycle_config',None,'Updated shared Workspace cycle configuration',pipeline_id)
        return {'ok':True,'settings':data.model_dump()}

    @router.post('/api/pipelines/{pipeline_id}/auto-post/cycles',status_code=202)
    def create_cycle(pipeline_id:int,data:CycleRequest,principal=Depends(deps.require_principal)):
        with deps.db() as conn:
            guard(conn,principal,pipeline_id)
            conn.execute('BEGIN IMMEDIATE')
            try:
                cycle_id = engine.request_cycle(conn,pipeline_id,'manual-'+data.idempotency_key,deps.now().isoformat())
            except repo.CycleError as error:
                raise HTTPException(409,{'code':error.code,'message':error.message}) from error
        return {'ok':True,'cycle_id':cycle_id,'published':False}

    @router.get('/api/pipelines/{pipeline_id}/auto-post/cycles')
    def cycles(pipeline_id:int,principal=Depends(deps.require_principal)):
        with deps.db() as conn:
            guard(conn,principal,pipeline_id,'history.view')
            ids = [r[0] for r in conn.execute('SELECT id FROM auto_cycles WHERE pipeline_id=? ORDER BY id DESC LIMIT 100',(pipeline_id,))]
            return {'cycles':[reports.report(conn,pipeline_id,cid) for cid in ids]}

    @router.get('/api/pipelines/{pipeline_id}/auto-post/cycles/{cycle_id}')
    def cycle_report(pipeline_id:int,cycle_id:int,principal=Depends(deps.require_principal)):
        with deps.db() as conn:
            guard(conn,principal,pipeline_id,'history.view')
            resource(conn,'auto_cycles',cycle_id,pipeline_id)
            return reports.report(conn,pipeline_id,cycle_id)

    @router.get('/api/pipelines/{pipeline_id}/auto-post/library')
    def library(pipeline_id:int,principal=Depends(deps.require_principal)):
        with deps.db() as conn:
            guard(conn,principal,pipeline_id)
            albums = [dict(r) for r in conn.execute('SELECT id,name FROM workspace_albums WHERE pipeline_id=? ORDER BY id',(pipeline_id,))]
            for album in albums:
                album['media_ids'] = [r[0] for r in conn.execute('SELECT media_id FROM workspace_album_media WHERE pipeline_id=? AND album_id=?',(pipeline_id,album['id']))]
            templates = [dict(r) for r in conn.execute('SELECT id,name,body,approved,revision FROM caption_templates WHERE pipeline_id=? ORDER BY id',(pipeline_id,))]
            media = [{'id':r['id'],'name':r['original_name'],'eligible':content.media_ready(dict(r))} for r in conn.execute('SELECT * FROM media_assets WHERE pipeline_id=? ORDER BY id',(pipeline_id,))]
            return {'albums':albums,'templates':templates,'media':media}

    @router.post('/api/pipelines/{pipeline_id}/auto-post/albums',status_code=201)
    def create_album(pipeline_id:int,data:AlbumPayload,principal=Depends(deps.require_principal)):
        with deps.db() as conn:
            guard(conn,principal,pipeline_id,'media.upload')
            conn.execute('BEGIN IMMEDIATE')
            for mid in data.media_ids:
                resource(conn,'media_assets',mid,pipeline_id)
            if len(data.media_ids)!=len(set(data.media_ids)):
                raise HTTPException(422,'Ảnh trong Album không được trùng.')
            if conn.execute('SELECT 1 FROM workspace_albums WHERE pipeline_id=? AND name=?',(pipeline_id,data.name)).fetchone():
                raise HTTPException(409,'Tên Album đã tồn tại.')
            aid = conn.execute('INSERT INTO workspace_albums(pipeline_id,name,created_at) VALUES(?,?,?)',(pipeline_id,data.name,repo.now_iso())).lastrowid
            conn.executemany('INSERT INTO workspace_album_media VALUES(?,?,?)',[(pipeline_id,aid,mid) for mid in data.media_ids])
        deps.audit('album',aid,'Created a Workspace album without changing media eligibility',pipeline_id)
        return {'id':aid}

    @router.post('/api/pipelines/{pipeline_id}/auto-post/templates',status_code=201)
    def create_template(pipeline_id:int,data:TemplatePayload,principal=Depends(deps.require_principal)):
        with deps.db() as conn:
            guard(conn,principal,pipeline_id)
            if data.approved:
                try:
                    content.render_template({'body':data.body,'approved':True},{k:'Nội dung' for k in ('group_name','pipeline_name','image_description','cycle_date')})
                except (ValueError,KeyError) as error:
                    raise HTTPException(422,'Biến trong caption mẫu không hợp lệ.') from error
            tid = conn.execute('''INSERT INTO caption_templates(pipeline_id,name,body,approved,created_by,created_at)
                VALUES(?,?,?,?,?,?)''',(pipeline_id,data.name,data.body,int(data.approved),principal.user_id,repo.now_iso())).lastrowid
        deps.audit('caption_template',tid,'Saved caption template; approved='+str(data.approved),pipeline_id)
        return {'id':tid}

    @router.put('/api/pipelines/{pipeline_id}/auto-post/groups/{group_id}/global-status')
    def global_status(pipeline_id:int,group_id:int,data:GlobalStatusPayload,principal=Depends(deps.require_admin)):
        with deps.db() as conn:
            guard(conn,principal,pipeline_id,'groups.manage')
            resource(conn,'groups',group_id,pipeline_id)
            try:
                repo.set_global_state(conn,pipeline_id,group_id,data.state,data.evidence,principal.user_id)
            except repo.CycleError as error:
                raise HTTPException(409,{'code':error.code,'message':error.message}) from error
        deps.audit('group_global_status',group_id,'Admin evidence confirmed '+data.state,pipeline_id)
        return {'ok':True,'state':data.state}

    @router.post('/api/pipelines/{pipeline_id}/auto-post/groups/{group_id}/check',status_code=202)
    def check_pair(pipeline_id:int,group_id:int,data:PairCheckRequest,principal=Depends(deps.require_principal)):
        with deps.db() as conn:
            guard(conn,principal,pipeline_id,'groups.manage')
            group = resource(conn,'groups',group_id,pipeline_id)
            ids = data.account_ids
            queued = []
            for aid in sorted(set(ids)):
                resource(conn,'accounts',aid,pipeline_id)
                conn.execute('''INSERT OR IGNORE INTO group_accounts
                    (pipeline_id,account_id,group_id,posting_mode,nickname,status,created_at,updated_at)
                    VALUES(?,?,?,?,'','active',?,?)''',(pipeline_id,aid,group_id,group['posting_mode'],repo.now_iso(),repo.now_iso()))
                if deps.enqueue_screening(conn,pipeline_id,aid,group_id,True):
                    queued.append(aid)
        deps.audit('screening',group_id,'Queued per-account Group permission checks; no Join or publication',pipeline_id)
        return {'queued_account_ids':queued,'published':False}

    return router
