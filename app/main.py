import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from . import models
from .isir import validate_rc
from .service import views, check_subject, check_lock
from . import scheduler

log = logging.getLogger(__name__)
root = Path(__file__).parent
templates = Jinja2Templates(directory=str(root / 'templates'))

@asynccontextmanager
async def lifespan(app):
    models.initialize()
    scheduler.initialize()
    log.info('ISIR Watch started')
    yield

app = FastAPI(lifespan=lifespan)
app.mount('/static',StaticFiles(directory=root / 'static'),name='static')

@app.middleware('http')
async def protect_forms(request: Request, call_next):
    if request.method == 'POST':
        origin = request.headers.get('origin')
        referer = request.headers.get('referer')
        source = origin or referer
        if source and urlparse(source).netloc != request.headers.get('host'):
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse('Cross-origin form submission rejected',status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse('Cross-site form submission rejected',status_code=403)
    return await call_next(request)

def redirect():
    return RedirectResponse('/',status_code=303)

@app.get('/')
def index(request: Request):
    return templates.TemplateResponse(request=request,name='index.html',context=dict(rows=views(),emails=models.emails(),
        schedules={job:scheduler.get_schedule(job) for job in scheduler.DEFAULTS},
        statuses={job:models.setting('schedule_status_'+job) for job in scheduler.DEFAULTS},
        timezone=os.getenv('TZ','Europe/Prague'),trash_count=len(models.subjects(trashed=True))))

@app.get('/health')
def health():
    with models.connection() as con:
        con.execute('SELECT 1 FROM subjects LIMIT 1')
    return {'status':'ok'}

@app.post('/subjects')
def add_subject(ic: str=Form(''),nazev: str=Form(''),jmeno: str=Form(''),datum_narozeni: str=Form(''),rc: str=Form('')):
    ic,nazev,jmeno,datum_narozeni = (v.strip() for v in (ic,nazev,jmeno,datum_narozeni))
    rc = rc.strip()
    if ic and rc:
        raise HTTPException(400,'Vyplňte buď IČO, nebo rodné číslo')
    if rc:
        try:
            rc = validate_rc(rc)
        except ValueError as exc:
            raise HTTPException(400,str(exc))
    if max(map(len,(nazev,jmeno))) > 200:
        raise HTTPException(400,'Jméno je příliš dlouhé')
    if ic:
        if not re.fullmatch(r'[0-9]{8}',ic):
            raise HTTPException(400,'IČO musí mít 8 číslic')
        jmeno = datum_narozeni = ''
    elif not rc:
        if not all((nazev,jmeno,datum_narozeni)):
            raise HTTPException(400,'Vyplňte IČO, rodné číslo nebo příjmení, jméno a datum narození')
        try:
            parsed = date.fromisoformat(datum_narozeni)
            if parsed > date.today():
                raise ValueError()
            datum_narozeni = parsed.isoformat()
        except ValueError:
            raise HTTPException(400,'Neplatné datum narození')
    if rc and datum_narozeni:
        try:
            parsed = date.fromisoformat(datum_narozeni)
            if parsed > date.today():
                raise ValueError()
            datum_narozeni = parsed.isoformat()
        except ValueError:
            raise HTTPException(400,'Neplatné datum narození')
    with check_lock:
        if not any(all(s[k] == v for k,v in (('ic',ic),('nazev',nazev),('jmeno',jmeno),('datum_narozeni',datum_narozeni),('rc',rc))) for s in models.subjects()):
            models.add_subject(ic,nazev,jmeno,datum_narozeni,rc)
    return redirect()

@app.post('/subjects/{subject_id}/rc')
def update_rc(subject_id: int, rc: str=Form('')):
    try:
        rc = validate_rc(rc) if rc.strip() else ''
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    with check_lock:
        if not models.update_rc(subject_id,rc):
            raise HTTPException(404,'Fyzická osoba neexistuje')
    return redirect()

@app.post('/subjects/{subject_id}/check')
def check(subject_id: int):
    with check_lock:
        subject = next((s for s in models.subjects() if s['id']==subject_id),None)
        if not subject:
            raise HTTPException(404,'Subjekt neexistuje')
        check_subject(subject)
    return redirect()

@app.post('/subjects/{subject_id}/delete')
def delete(subject_id: int):
    with check_lock:
        models.delete_subject(subject_id)
    return redirect()

@app.get('/trash')
def trash(request: Request):
    return templates.TemplateResponse(request=request,name='trash.html',context=dict(subjects=models.subjects(trashed=True)))

@app.post('/subjects/{subject_id}/restore')
def restore(subject_id: int):
    with check_lock:
        if not models.restore_subject(subject_id):
            raise HTTPException(404,'Subjekt v koši neexistuje')
    return RedirectResponse('/trash',status_code=303)

@app.post('/subjects/{subject_id}/purge')
def purge(subject_id: int, confirmation: str=Form('')):
    if confirmation != 'SMAZAT':
        raise HTTPException(400,'Trvalé smazání potvrďte zadáním SMAZAT')
    with check_lock:
        if not models.purge_subject(subject_id):
            raise HTTPException(404,'Subjekt v koši neexistuje')
    return RedirectResponse('/trash',status_code=303)

@app.post('/schedules/{job}')
def save_schedule(job: str, enabled: bool=Form(False), frequency: str=Form(...), time: str=Form(...),
                  weekdays: list[int]=Form([]), month_day: int=Form(1), only_changes: bool=Form(False)):
    try:
        scheduler.save_schedule(job,dict(enabled=enabled,frequency=frequency,time=time,
            weekdays=sorted(set(weekdays)),month_day=month_day,only_changes=only_changes if job=='digest' else False))
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    return redirect()

@app.post('/check-all')
def manual_check_all():
    from .service import check_all
    check_all()
    return redirect()

@app.post('/digest')
def manual_digest():
    from .mail_digest import send_digest
    try:
        send_digest()
    except Exception:
        log.exception('Manual digest failed')
        raise HTTPException(502,'Odeslání selhalo; ověřte SMTP konfiguraci a log')
    return redirect()

@app.post('/emails')
def add_email(address: str=Form(...)):
    address = address.strip()
    if len(address)>254 or not re.fullmatch(r'[^\s@<>;,]+@[^\s@<>;,]+\.[^\s@<>;,]+',address):
        raise HTTPException(400,'Neplatný e-mail')
    models.add_email(address)
    return redirect()

@app.post('/emails/{email_id}/delete')
def delete_email(email_id: int):
    models.delete_email(email_id)
    return redirect()
