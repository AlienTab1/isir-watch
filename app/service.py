import json
import logging
import threading
from . import models, isir
from . import history

log = logging.getLogger(__name__)
check_lock = threading.Lock()

def check_subject(subject):
    try:
        payload = isir.query(subject)
        status = payload.get('stav', {}) if isinstance(payload, dict) else {}
        code = status.get('kodChyby') if isinstance(status, dict) else None
        if code and code != 'WS2':
            models.save_result(subject['id'],payload,0,f"{code}: {status.get('textChyby', '')} {status.get('popisChyby', '')}")
            log.error('SOAP subject=%s status=%s',subject['id'],code)
            return False
        hits = isir.get_hits(payload)
        filtered = isir.filter_hits_strict(hits, subject)
        models.save_result(subject['id'],payload,len(filtered))
        log.info('subject=%s raw_hits=%s filtered_hits=%s',subject['id'],len(hits),len(filtered))
        return True
    except Exception as exc:
        log.exception('SOAP subject=%s failed',subject['id'])
        models.save_result(subject['id'],None,0,f'{type(exc).__name__}: {exc}')
        return False

def views():
    out = []
    for subject in models.subjects():
        result = models.latest(subject['id'])
        hits = []
        raw_count = 0
        if result and not result['error']:
            raw = isir.get_hits(json.loads(result['payload_json']))
            raw_count = len(raw)
            hits = [isir.normalize_hit(h) for h in isir.filter_hits_strict(raw,subject)]
        out.append(dict(subject=subject,result=result,hits=hits,count=len(hits),truncated=raw_count>=50,changes=history.windows(subject['id'])))
    return out

def check_all():
    ok = errors = 0
    with check_lock:
        for subject in models.subjects():
            if check_subject(subject):
                ok += 1
            else:
                errors += 1
    log.info('checked %s subjects successes=%s errors=%s',ok+errors,ok,errors)
    return 1 if errors else 0
