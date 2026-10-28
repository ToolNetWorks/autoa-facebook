"""Pipeline Album rotation and explicit, audit-ready caption generation."""
import hashlib
import json
import re
import string
import unicodedata

from services import ai_provider, media_status
from services.caption_validator import analyze_caption
from services.auto_posting_cycles.repository import CycleError, owned


def caption_key(text):
    normalized = re.sub(r'\s+',' ',unicodedata.normalize('NFKC',text)).strip().casefold()
    return hashlib.sha256(normalized.encode()).hexdigest()


def media_ready(media):
    return bool(media['usable'] and not media['locked'] and
        media_status.state_of(media['analysis_status'],media['safety_status']) in
        (media_status.READY,media_status.NEEDS_REVIEW))


def select_image(conn, pipeline_id, settings):
    rows = conn.execute('SELECT * FROM media_assets WHERE pipeline_id=? ORDER BY id',(pipeline_id,)).fetchall()
    candidates = []
    album = None
    if settings['album_id']:
        album = {r[0] for r in conn.execute('SELECT media_id FROM workspace_album_media WHERE pipeline_id=? AND album_id=?',(pipeline_id,settings['album_id']))}
    for row in rows:
        media = dict(row)
        if not media_ready(media) or (album is not None and media['id'] not in album):
            continue
        if settings['media_ids'] and media['id'] not in settings['media_ids']:
            continue
        uses = conn.execute("""SELECT COUNT(*) FROM auto_cycle_targets WHERE pipeline_id=? AND media_id=?
            AND status NOT IN ('failed','skipped','no_eligible_account')""",(pipeline_id,media['id'])).fetchone()[0]
        if not settings['repeat_media'] and uses:
            continue
        candidates.append((uses,media['id'],media))
    return min(candidates,key=lambda x:(x[0],x[1]))[2] if candidates else None


def render_template(template, context):
    if not template['approved']:
        raise CycleError('TEMPLATE_NOT_APPROVED')
    allowed = {'group_name','pipeline_name','image_description','cycle_date'}
    for _,name,spec,conversion in string.Formatter().parse(template['body']):
        if name is not None and (name not in allowed or spec or conversion):
            raise CycleError('TEMPLATE_INVALID')
    return template['body'].format_map(context)


def generate(conn, pipeline_id, target, settings, provider_config):
    group = owned(conn,'groups',target['group_id'],pipeline_id)
    media = owned(conn,'media_assets',target['media_id'],pipeline_id)
    pipeline = conn.execute('SELECT name FROM pipelines WHERE id=?',(pipeline_id,)).fetchone()
    context = {'group_name':group['group_name'] or group['label'], 'pipeline_name':pipeline[0],
               'image_description':media['description'], 'cycle_date':target['created_at'][:10]}
    source = 'template'
    template_id = None
    if settings['caption_mode']=='ai':
        try:
            if not provider_config or not provider_config.configured:
                raise CycleError('AI_UNAVAILABLE')
            profile = conn.execute('SELECT tone,language FROM ai_caption_settings WHERE pipeline_id=?',(pipeline_id,)).fetchone()
            answer = ai_provider.chat_json(
                'Viết caption đúng bối cảnh được cung cấp. Không bịa dữ kiện; không né kiểm duyệt. Trả JSON {"caption":"..."}.',
                json.dumps({'pipeline':context['pipeline_name'],'group':context['group_name'],
                            'image':context['image_description'],'topic':settings['topic'],
                            'profile':dict(profile) if profile else {},'identity':settings['posting_mode']},ensure_ascii=False),provider_config)
            if not isinstance(answer,dict) or not isinstance(answer.get('caption'),str):
                raise CycleError('AI_INVALID_CAPTION')
            caption = answer['caption'].strip()
            if not caption:
                raise CycleError('AI_EMPTY_CAPTION')
            source = 'ai'
        except Exception as error:
            if not settings['fallback_template_id']:
                raise CycleError('AI_CAPTION_FAILED') from error
            template_id = settings['fallback_template_id']
            source = 'approved_fallback'
    if source != 'ai':
        ids = settings['template_ids']
        template_id = template_id or (ids[(target['id']-1) % len(ids)] if ids else None)
        if template_id is None:
            raise CycleError('NO_CAPTION_TEMPLATE')
        template = owned(conn,'caption_templates',template_id,pipeline_id)
        caption = render_template(template,context)
    if not caption.strip() or len(caption)>20000 or analyze_caption(caption).issues:
        raise CycleError('CAPTION_NEEDS_REVIEW')
    snapshot = {'template_id':template_id,'context':context,'mode':settings['caption_mode']}
    return caption,source,snapshot
