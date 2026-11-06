"""Incremental public ISIR action feed. Existing application tables are untouched."""
import json
import os
import re
import threading
from collections import defaultdict
from contextlib import closing
from datetime import timedelta
from pathlib import Path
import sqlite3
from urllib.parse import urlparse
from lxml import etree
import requests
from zeep import Client, helpers
from zeep.transports import Transport
from . import models, isir
from .config import now, db_path
from .history import parse_time

WSDL='https://isir.justice.cz:8443/isir_public_ws/IsirWsPublicService?wsdl'
lock=threading.RLock()

def initialize():
    with models.connection() as con:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='proceeding_actions'").fetchone():
            backup=Path(str(db_path())+'.pre-proceedings.db')
            if not backup.exists():
                with closing(sqlite3.connect(backup)) as dest:
                    con.backup(dest)
        con.executescript('''
        CREATE TABLE IF NOT EXISTS proceeding_links(
          subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
          case_key TEXT NOT NULL, case_label TEXT NOT NULL, detail_url TEXT NOT NULL,
          since_id INTEGER NOT NULL, PRIMARY KEY(subject_id,case_key));
        CREATE TABLE IF NOT EXISTS proceeding_actions(
          action_id INTEGER PRIMARY KEY, case_key TEXT NOT NULL, section TEXT NOT NULL,
          number TEXT NOT NULL, published_at TEXT NOT NULL, detected_at TEXT NOT NULL,
          description TEXT NOT NULL, document_url TEXT NOT NULL, kind TEXT NOT NULL,
          raw_json TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS proceeding_actions_case ON proceeding_actions(case_key,action_id);
        CREATE TABLE IF NOT EXISTS proceeding_hidden(case_key TEXT PRIMARY KEY);
        ''')

def case_key(value):
    match=re.search(r'\b(INS|ICM)\s+(\d+)\s*/\s*(\d{4})\b',str(value),re.I)
    return f'{match[1].upper()} {int(match[2])}/{match[3]}' if match else ''

def safe_url(value):
    value=str(value or '')
    url=urlparse(value)
    return value if url.scheme in ('http','https') and url.hostname=='isir.justice.cz' else ''

def register(subject_id,hits):
    with models.connection() as con:
        row=con.execute("SELECT value FROM settings WHERE key='proceedings_cursor'").fetchone()
        cursor=int(row[0]) if row else 0
        for raw in hits:
            hit=isir.normalize_hit(raw)
            key=case_key(hit['spis'])
            if key:
                con.execute('INSERT OR IGNORE INTO proceeding_links VALUES(?,?,?,?,?)',
                            (subject_id,key,hit['spis'],safe_url(hit['detail_url']),cursor))

def register_existing():
    for subject in models.subjects():
        with models.connection() as con:
            result=con.execute('SELECT payload_json FROM results WHERE subject_id=? AND error IS NULL ORDER BY id DESC LIMIT 1',(subject['id'],)).fetchone()
        if result:
            register(subject['id'],isir.filter_hits_strict(isir.get_hits(json.loads(result[0])),subject))

class Feed:
    def __enter__(self):
        self.session=requests.Session()
        try:
            self.client=Client(os.getenv('ISIR_PUBLIC_WSDL',WSDL),transport=Transport(session=self.session,timeout=25,operation_timeout=25))
        except Exception:
            self.session.close()
            raise
        return self

    def __exit__(self,*args):
        self.session.close()

    def call(self,operation,**kwargs):
        data=helpers.serialize_object(getattr(self.client.service,operation)(**kwargs),target_cls=dict)
        if not isinstance(data,dict) or (data.get('status') or {}).get('stav')!='OK':
            raise RuntimeError('ISIR WS_1 nevrátil úspěšnou odpověď')
        return data

    def head(self):
        values=self.call('getIsirWsPublicPodnetPosledniId')['cisloPosledniId']
        return int(values[0] if isinstance(values,list) else values)

    def batch(self,cursor):
        data=self.call('getIsirWsPublicPodnetId',idPodnetu=cursor)
        records=data.get('data') or []
        if not isinstance(records,list):
            raise ValueError('Neplatný formát dávky WS_1')
        return records

def note_fields(raw):
    note=raw.get('poznamka') or ''
    if not note:
        return {}
    if '<!DOCTYPE' in note.upper() or '<!ENTITY' in note.upper():
        raise ValueError('Nepovolená XML deklarace')
    node=etree.fromstring(note.encode('utf-8'),parser=etree.XMLParser(resolve_entities=False,no_network=True))
    return {etree.QName(e).localname:(e.text or '').strip() for e in node.iter() if isinstance(e.tag,str)}

def apply_batch(cursor,records):
    ids=[int(r['id']) for r in records]
    if ids!=sorted(set(ids)) or any(i<=cursor for i in ids):
        raise ValueError('WS_1 vrátil neplatné pořadí ID')
    with models.connection() as con:
        con.execute('BEGIN IMMEDIATE')
        actual=con.execute("SELECT value FROM settings WHERE key='proceedings_cursor'").fetchone()
        if not actual or int(actual[0])!=cursor:
            return False
        tracked={r[0] for r in con.execute('SELECT DISTINCT l.case_key FROM proceeding_links l JOIN subjects s ON s.id=l.subject_id WHERE s.deleted_at IS NULL')}
        for raw in records:
            key=case_key(raw.get('spisovaZnacka'))
            if key not in tracked:
                continue
            fields=note_fields(raw)
            if fields.get('datumVyskrtnuti') or fields.get('datumVecZrusena'):
                con.execute('INSERT OR IGNORE INTO proceeding_hidden VALUES(?)',(key,))
            section=str(raw.get('oddil') or '').strip().upper()
            number=raw.get('cisloVOddilu')
            if not section or number is None:
                continue
            kind='Záznam ve spise'
            if fields.get('datumUdalostZrusena'):
                kind='Zrušení / mylný zápis'
            elif fields.get('priznakAnVedlejsiDokument')=='T':
                kind='Vedlejší dokument'
            elif fields.get('datumPravniMoci'):
                kind='Právní moc'
            elif fields.get('priznakAnVedlejsiUdalost')=='T':
                kind='Vedlejší událost'
            elif fields.get('datumZverejneniOpraveneUdalosti') or con.execute('SELECT 1 FROM proceeding_actions WHERE case_key=? AND section=? AND number=?',(key,section,str(number))).fetchone():
                kind='Aktualizace záznamu'
            con.execute('INSERT OR IGNORE INTO proceeding_actions VALUES(?,?,?,?,?,?,?,?,?,?)',
                (int(raw['id']),key,section,str(number),str(raw.get('datumZverejneniUdalosti') or ''),now().isoformat(),
                 str(raw.get('popisUdalosti') or ''),safe_url(raw.get('dokumentUrl')),kind,json.dumps(raw,ensure_ascii=False,default=str)))
        if ids:
            con.execute("UPDATE settings SET value=? WHERE key='proceedings_cursor'",(str(ids[-1]),))
    return True

def sync():
    import logging
    with lock:
        try:
            register_existing()
            if not models.subjects():
                return True
            with Feed() as feed:
                head=feed.head()
                if models.setting('proceedings_cursor') is None:
                    with models.connection() as con:
                        con.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',('proceedings_cursor',str(head)))
                        con.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',('proceedings_since',now().isoformat()))
                    models.setting('proceedings_status','Sledování aktivováno; starší dokumenty nejsou importovány')
                    return True
                for _ in range(int(os.getenv('ISIR_PUBLIC_MAX_BATCHES','200'))):
                    cursor=int(models.setting('proceedings_cursor'))
                    if cursor>=head:
                        models.setting('proceedings_status','Aktuální')
                        models.setting('proceedings_checked_at',now().isoformat())
                        return True
                    records=feed.batch(cursor)
                    if not records:
                        # IDs can have gaps. Keep the cursor: delayed publication must not be skipped.
                        models.setting('proceedings_status','Aktuální dle dostupných dat WS_1')
                        models.setting('proceedings_checked_at',now().isoformat())
                        return True
                    apply_batch(cursor,records)
                models.setting('proceedings_status','Stahování pokračuje; další kontrola naváže na uložené ID')
                return False
        except Exception:
            logging.getLogger(__name__).exception('Proceeding event synchronization failed')
            models.setting('proceedings_status','Chyba synchronizace událostí; uložená historie je zachována')
            return False

def rows(subject_id=None):
    with models.connection() as con:
        sql='''SELECT DISTINCT a.* FROM proceeding_actions a JOIN proceeding_links l ON l.case_key=a.case_key
        JOIN subjects s ON s.id=l.subject_id WHERE s.deleted_at IS NULL AND a.action_id>l.since_id
        AND NOT EXISTS(SELECT 1 FROM proceeding_hidden h WHERE h.case_key=a.case_key)'''
        return [dict(r) for r in con.execute(sql+(' AND s.id=?' if subject_id else '')+' ORDER BY a.action_id DESC',(subject_id,) if subject_id else ())]

def overview(subject_id,days=7):
    data=rows(subject_id)
    current=now()
    windows={d:[r for r in data if timedelta(0)<=current-parse_time(r['detected_at'])<=timedelta(days=d)] for d in (7,30,60)}
    groups=defaultdict(lambda:defaultdict(list))
    for event in windows[days]:
        groups[event['case_key']][event['section']].append(event)
    natural=lambda value:[int(part) if part.isdigit() else part for part in re.split(r'(\d+)',value)]
    ordered={case:dict(sorted(sections.items(),key=lambda item:natural(item[0]))) for case,sections in groups.items()}
    return dict(counts={d:len(v) for d,v in windows.items()},groups=ordered,days=days)

def watermark():
    return max((r['action_id'] for r in rows()),default=0)

def has_new_changes():
    return watermark()>int(models.setting('digest_last_action_id') or 0)
