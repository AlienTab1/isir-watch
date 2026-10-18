"""Immutable detected changes; failed/truncated checks never imply disappearance."""
import json
from datetime import datetime, timedelta, timezone
from . import isir, models
from .config import now

FIELDS = {'spis':'Spisová značka','stav':'Stav řízení','jmeno':'Jméno/název',
          'ico':'IČO','rc':'Rodné číslo','narozeni':'Datum narození','adresa':'Adresa'}
KINDS = {'added':'Nově nalezené řízení','removed':'Řízení již není v aktuálních výsledcích','updated':'Změna údajů'}

def identity(subject):
    if subject.get('ic'):
        key = ['ic',subject['ic']]
    elif subject.get('rc'):
        key = ['rc',subject['rc']]
    else:
        key = ['name',isir.norm(subject.get('nazev')),isir.norm(subject.get('jmeno')),subject.get('datum_narozeni')]
    return json.dumps(key + [subject.get('watch_epoch','')],ensure_ascii=False)

def snapshot(payload, subject):
    raw = isir.get_hits(payload)
    status = payload.get('stav') or {} if isinstance(payload,dict) else {}
    def has_list(value):
        return isinstance(value,(list,tuple)) or (isinstance(value,dict) and any(has_list(v) for v in value.values()))
    recognized = has_list(payload) or (isinstance(status,dict) and
        (status.get('kodChyby')=='WS2' or status.get('pocetVysledku')==0))
    complete = len(raw) < 50 and recognized
    if isinstance(status,dict):
        try:
            complete = complete and int(status.get('pocetVysledku') or 0) < 50
        except (ValueError,TypeError):
            complete = False
        if status.get('kodChyby') not in (None,'','WS2'):
            complete = False
    out = {}
    for raw_hit in isir.filter_hits_strict(raw,subject):
        hit = isir.normalize_hit(raw_hit)
        key = ''.join(isir.norm(hit['spis']).split()) or hit['detail_url']
        if not key or key in out:
            complete = False
        if key:
            out[key] = {k:str(hit.get(k) or '').strip() for k in FIELDS}
    return out, complete

def record(con, result_id):
    result = dict(con.execute('SELECT * FROM results WHERE id=?',(result_id,)).fetchone())
    if result['error'] or con.execute('SELECT 1 FROM snapshots WHERE result_id=?',(result_id,)).fetchone():
        return
    subject = dict(con.execute('SELECT * FROM subjects WHERE id=?',(result['subject_id'],)).fetchone())
    try:
        current, complete = snapshot(json.loads(result['payload_json']),subject)
    except (ValueError,TypeError,KeyError):
        current, complete = {}, False
    ident = identity(subject)
    previous = con.execute('SELECT payload_json FROM snapshots WHERE subject_id=? AND identity=? AND complete=1 AND result_id<? ORDER BY result_id DESC LIMIT 1',
                           (subject['id'],ident,result_id)).fetchone()
    if complete and previous:
        old = json.loads(previous['payload_json'])
        for key in sorted(set(old) | set(current)):
            before,after = old.get(key),current.get(key)
            if before is None:
                kind = 'added'
            elif after is None:
                kind = 'removed'
            else:
                kind = 'updated'
            fields = {k:{'before':(before or {}).get(k,''),'after':(after or {}).get(k,'')}
                      for k in FIELDS if isir.norm((before or {}).get(k)) != isir.norm((after or {}).get(k))}
            if kind != 'updated' or fields:
                con.execute('INSERT INTO changes(subject_id,result_id,detected_at,case_key,case_label,kind,fields_json) VALUES(?,?,?,?,?,?,?)',
                            (subject['id'],result_id,result['checked_at'],key,(after or before).get('spis') or key,kind,json.dumps(fields,ensure_ascii=False)))
    con.execute('INSERT INTO snapshots VALUES(?,?,?,?,?)',(result_id,subject['id'],ident,int(complete),json.dumps(current,ensure_ascii=False)))

def backfill():
    with models.connection() as con:
        ids = [r[0] for r in con.execute('SELECT r.id FROM results r LEFT JOIN snapshots s ON s.result_id=r.id WHERE s.result_id IS NULL AND r.error IS NULL ORDER BY r.id')]
        for result_id in ids:
            record(con,result_id)

def parse_time(value):
    dt = datetime.fromisoformat(value)
    # Older records were written in the configured local timezone.
    return (dt if dt.tzinfo else dt.replace(tzinfo=now().tzinfo)).astimezone(timezone.utc)

def windows(subject_id, current=None):
    current = (current or now()).astimezone(timezone.utc)
    groups = {days:[] for days in (7,30,60)}
    with models.connection() as con:
        rows = con.execute('SELECT * FROM changes WHERE subject_id=? ORDER BY id DESC',(subject_id,)).fetchall()
    for row in rows:
        event = dict(row)
        age = current - parse_time(event['detected_at'])
        event['kind_label'] = KINDS[event['kind']]
        event['fields'] = [{'label':FIELDS[k],**value} for k,value in json.loads(event['fields_json']).items()]
        for days in groups:
            if timedelta(0) <= age <= timedelta(days=days):
                groups[days].append(event)
    return groups

def watermark():
    with models.connection() as con:
        return con.execute('SELECT COALESCE(MAX(c.id),0) FROM changes c JOIN subjects s ON s.id=c.subject_id WHERE s.deleted_at IS NULL').fetchone()[0]

def has_new_changes():
    with models.connection() as con:
        return bool(con.execute('SELECT 1 FROM changes c JOIN subjects s ON s.id=c.subject_id WHERE s.deleted_at IS NULL AND c.id>? LIMIT 1',
                                (int(models.setting('digest_last_change_id') or 0),)).fetchone())
