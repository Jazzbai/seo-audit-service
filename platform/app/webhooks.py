"""Signed WordPress notifications: bounded, replay-safe and non-authorizing."""
import hashlib
import hmac
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from app.db import get_db
from app.models import Publication, Site
from app.operations import credentials, enqueue, now, record

router=APIRouter(prefix='/api/v1/webhooks')


@router.post('/wordpress/{site_id}',status_code=202)
async def wordpress_event(site_id:str,request:Request,db=Depends(get_db)):
    try:
        secret,_=credentials(db,site_id,'wordpress')
        key=secret.get('webhook_secret','')
        if not isinstance(key,str) or len(key)<32:
            raise ValueError('Webhook not configured')
    except Exception:
        raise HTTPException(401,'Notification authentication failed')
    body=bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body)>65536:
            raise HTTPException(413,'Notification is too large')
    supplied=request.headers.get('x-forgeseo-signature','')
    expected=hmac.new(key.encode(),body,hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied,expected):
        raise HTTPException(401,'Notification authentication failed')
    # Do not disclose whether a site exists until the notification has been
    # authenticated.  In particular, an unknown site_id and a known site
    # with a forged signature must take the same authentication path.
    site=db.get(Site,site_id)
    if site is None:
        raise HTTPException(401,'Notification authentication failed')
    try:
        payload=json.loads(body)
        timestamp=datetime.fromisoformat(payload['occurred_at'].replace('Z','+00:00'))
        if timestamp.tzinfo is None or abs((datetime.now(timezone.utc)-timestamp).total_seconds())>300:
            raise ValueError('Expired notification')
        data=payload['data']
        resource_id=int(data['id'])
        resource_type=data.get('resource_type','posts')
        if resource_type=='post':
            resource_type='posts'
        if resource_type=='page':
            resource_type='pages'
        if resource_id<=0 or resource_type not in ('posts','pages','products','product_categories'):
            raise ValueError('Unsupported notification target')
    except (ValueError,TypeError,KeyError,AttributeError):
        raise HTTPException(422,'Malformed or expired notification')
    operation=data.get('operation_key')
    if operation and db.scalar(select(Publication).where(Publication.site_id==site_id,Publication.operation_key==operation)):
        return {'status':'ignored','reason':'platform_write_already_has_verification'}
    resource_key=f'{resource_type}:{resource_id}'
    # Timestamp buckets collapse bursts. Event identity is not authority to write.
    bucket=int(timestamp.timestamp())//30
    job=enqueue(db,site,'targeted_audit',{'resource_key':resource_key},f'change:{resource_key}:{bucket}')
    return {'status':'queued','job_id':job.id}
