"""Independent database-backed schedules, evaluated by a single worker."""
import calendar
import json
import logging
import os
import threading
from datetime import timedelta, timezone
from . import models
from .config import now
from .service import check_all
from .mail_digest import send_digest

log = logging.getLogger(__name__)
schedule_lock = threading.RLock()
DEFAULTS = {
    'check':dict(enabled=True,frequency='daily',time='06:00',weekdays=[0],month_day=1,only_changes=False),
    'digest':dict(enabled=True,frequency='weekly',time='07:00',weekdays=[0],month_day=1,only_changes=False),
}

def validate(config):
    if config['frequency'] not in ('daily','weekly','monthly'):
        raise ValueError('Neplatná frekvence')
    parts = config['time'].split(':')
    if len(parts)!=2 or any(len(p)!=2 or not p.isdigit() for p in parts):
        raise ValueError('Čas musí být HH:MM')
    hour,minute = map(int,parts)
    if not 0<=hour<=23 or not 0<=minute<=59:
        raise ValueError('Neplatný čas')
    if not 1<=config['month_day']<=31:
        raise ValueError('Den měsíce musí být 1 až 31')
    if any(day not in range(7) for day in config['weekdays']) or (config['frequency']=='weekly' and not config['weekdays']):
        raise ValueError('Vyberte alespoň jeden den v týdnu')
    return config

def get_schedule(job):
    if job not in DEFAULTS:
        raise ValueError('Neplatná úloha')
    raw = models.setting('schedule_'+job)
    if raw:
        return json.loads(raw)
    config = DEFAULTS[job].copy()
    config['enabled'] = os.getenv('ISIR_SCHEDULER_ENABLED','true').lower() == 'true'
    return config

def due_slot(current, config=None):
    if config is None:
        config = dict(DEFAULTS['digest'],time='06:00')
    hour,minute = map(int,config['time'].split(':'))
    for offset in range(370):
        candidate = (current-timedelta(days=offset)).replace(hour=hour,minute=minute,second=0,microsecond=0,fold=0)
        # Resolve nonexistent spring times forward; execute repeated autumn times once.
        candidate = candidate.astimezone(timezone.utc).astimezone(current.tzinfo)
        if candidate.astimezone(timezone.utc) > current.astimezone(timezone.utc):
            continue
        if config['frequency']=='weekly' and candidate.weekday() not in config['weekdays']:
            continue
        if config['frequency']=='monthly' and candidate.day != min(config['month_day'],calendar.monthrange(candidate.year,candidate.month)[1]):
            continue
        return candidate.isoformat()
    raise ValueError('Nelze určit termín')

def initialize():
    with schedule_lock:
        for job in DEFAULTS:
            if models.setting('schedule_'+job) is None:
                config = get_schedule(job)
                models.setting('schedule_'+job,json.dumps(config))
                models.setting('schedule_last_'+job,due_slot(now(),config))

def save_schedule(job, config):
    if job not in DEFAULTS:
        raise ValueError('Neplatná úloha')
    validate(config)
    with schedule_lock, models.connection() as con:
        for key,value in [('schedule_'+job,json.dumps(config)),('schedule_last_'+job,due_slot(now(),config))]:
            con.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,value))

def tick():
    initialize()
    check_failed = False
    for job in DEFAULTS:
        with schedule_lock:
            config = get_schedule(job)
            if not config['enabled']:
                continue
            slot = due_slot(now(),config)
            if models.setting('schedule_last_'+job) == slot:
                continue
            # At-most-once attempts; failed sends require manual retry.
            models.setting('schedule_last_'+job,slot)
        if job=='digest' and check_failed:
            models.setting('schedule_status_'+job,'Přehled vynechán: souběžná kontrola selhala')
            continue
        try:
            if job=='check':
                check_failed = check_all()!=0
                if check_failed:
                    raise RuntimeError('Některé kontroly selhaly')
                status = 'Kontrola dokončena'
            else:
                sent = send_digest(only_changes=config['only_changes'])
                status = 'Přehled odeslán' if sent else 'Bez odeslání (žádní příjemci nebo žádné nové změny)'
            models.setting('schedule_status_'+job,now().isoformat()+' · '+status)
        except Exception:
            if job=='check':
                check_failed = True
            log.exception('Scheduled %s failed; retry manually',job)
            models.setting('schedule_status_'+job,now().isoformat()+' · Úloha selhala; podrobnosti v logu')

def run(stop):
    while not stop.is_set():
        try:
            tick()
        except Exception:
            log.exception('Scheduled job failed; use CLI to retry')
        stop.wait(20)

def start():
    initialize()
    stop = threading.Event()
    thread = threading.Thread(target=run,args=(stop,),name='isir-scheduler',daemon=True)
    thread.start()
    return stop, thread
