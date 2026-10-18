import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient
from app import models, history, service, scheduler, mail_digest
from app.main import app

TZ = ZoneInfo('Europe/Prague')

@pytest.fixture(autouse=True)
def database(tmp_path,monkeypatch):
    monkeypatch.setenv('ISIR_DB_PATH',str(tmp_path/'watch.db'))
    models.initialize()

def subject():
    return models.add_subject(ic='00123456',nazev='Test company')

def payload(state='A',case='KSBR 1 INS 42 / 2026',**extra):
    return {'data':[dict(ic='00123456',spis=case,stav=state,**extra)]}

def events(sid):
    return history.windows(sid)[60]

def test_baseline_and_reversions():
    sid=subject()
    models.save_result(sid,payload(),1)
    assert not events(sid)
    models.save_result(sid,payload('B'),1)
    models.save_result(sid,payload('A'),1)
    models.save_result(sid,payload('A'),1)
    changes=events(sid)
    assert len(changes)==2
    assert changes[0]['fields']==[{'label':'Stav řízení','before':'B','after':'A'}]

def test_add_remove_and_failed_checks():
    sid=subject()
    models.save_result(sid,{'data':[]},0)
    models.save_result(sid,payload(),1)
    models.save_result(sid,None,0,'SOAP unavailable')
    assert [e['kind'] for e in events(sid)]==['added']
    models.save_result(sid,{'data':[]},0)
    assert [e['kind'] for e in events(sid)]==['removed','added']
    assert 'již není' in events(sid)[0]['kind_label']

def test_incomplete_not_disappearance():
    sid=subject()
    models.save_result(sid,payload(),1)
    models.save_result(sid,{'data':[dict(ic='99999999',spis=str(i)) for i in range(50)]},0)
    models.save_result(sid,{'data':[],'stav':{'pocetVysledku':50}},0)
    models.save_result(sid,None,0)
    models.save_result(sid,{'unexpected':'malformed'},0)
    models.save_result(sid,payload('B'),1)
    assert [e['kind'] for e in events(sid)]==['updated']

def test_windows_boundaries():
    sid=subject()
    models.save_result(sid,payload(),1)
    fixed=datetime(2026,10,18,12,tzinfo=TZ)
    for age,state in [(65,'B'),(45,'C'),(20,'D'),(6,'E')]:
        models.save_result(sid,payload(state),1)
        with models.connection() as con:
            con.execute('UPDATE changes SET detected_at=? WHERE id=(SELECT MAX(id) FROM changes)',((fixed-timedelta(days=age)).isoformat(),))
    groups=history.windows(sid,fixed)
    assert [len(groups[d]) for d in (7,30,60)]==[1,2,3]

def test_backfill_idempotent():
    sid=subject()
    with models.connection() as con:
        for state in ('A','B','A'):
            con.execute('INSERT INTO results(subject_id,checked_at,count,payload_json) VALUES(?,?,?,?)',
                        (sid,'2026-10-18T06:00:00+02:00',1,json.dumps(payload(state))))
    models.initialize();models.initialize()
    with models.connection() as con:
        assert con.execute('SELECT COUNT(*) FROM changes').fetchone()[0]==2
        assert con.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0]==3

def test_upgrade_rc_database_keeps_data(database=None):
    # Recreate the schema from the previous release, including birth numbers.
    with models.connection() as con:
        con.executescript('''DROP TABLE changes; DROP TABLE snapshots;
        DROP TABLE results; DROP TABLE subjects;
        CREATE TABLE subjects(id INTEGER PRIMARY KEY,ic TEXT,nazev TEXT,jmeno TEXT,datum_narozeni TEXT,created_at TEXT NOT NULL,rc TEXT NOT NULL DEFAULT '');
        CREATE TABLE results(id INTEGER PRIMARY KEY,subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,checked_at TEXT NOT NULL,count INTEGER NOT NULL,payload_json TEXT,error TEXT);
        INSERT INTO subjects VALUES(71,'','Test','Person','1980-01-01','2026-01-01','8001011234');
        INSERT INTO results VALUES(14,71,'2026-01-02',0,'{"data":[]}',NULL);
        INSERT INTO emails(address,created_at) VALUES('saved@example.com','2026-01-01');
        INSERT INTO settings VALUES('preserved_setting','preserved');''')
    models.initialize();models.initialize()
    assert models.subjects()[0]['id']==71
    assert models.subjects()[0]['rc']=='8001011234'
    assert models.latest(71)['id']==14
    assert models.emails()[0]['address']=='saved@example.com'
    assert models.setting('preserved_setting')=='preserved'

def test_trash_restore_purge(monkeypatch):
    sid=subject()
    models.save_result(sid,payload(),1)
    models.save_result(sid,payload('B'),1)
    mock=MagicMock();monkeypatch.setattr(service.isir,'query',mock)
    with TestClient(app) as client:
        assert client.post(f'/subjects/{sid}/delete').status_code==200
        assert not models.subjects()
        assert len(models.subjects(True))==1
        assert models.latest(sid) and len(events(sid))==1
        assert service.check_all()==0
        mock.assert_not_called()
        assert not service.views()
        assert not history.has_new_changes()
        assert client.post(f'/subjects/{sid}/check').status_code==404
        assert client.post(f'/subjects/{sid}/rc',data={'rc':'800101/1234'}).status_code==404
        assert 'Test company' in client.get('/trash').text
        assert client.post(f'/subjects/{sid}/restore').status_code==200
        assert models.subjects()[0]['id']==sid
        # Restoring starts a fresh comparison baseline; old events remain intact.
        models.save_result(sid,payload('C'),1)
        assert len(events(sid))==1
        assert client.post(f'/subjects/{sid}/purge',data={'confirmation':'SMAZAT'}).status_code==404
        client.post(f'/subjects/{sid}/delete')
        assert client.post(f'/subjects/{sid}/purge').status_code==400
        assert models.latest(sid)
        assert client.post(f'/subjects/{sid}/purge',data={'confirmation':'SMAZAT'}).status_code==200
        assert not models.latest(sid) and not events(sid)

def test_independent_schedules_and_order(monkeypatch):
    fixed=datetime(2026,10,19,5,tzinfo=TZ)
    monkeypatch.setattr(scheduler,'now',lambda:fixed)
    scheduler.initialize()
    calls=[]
    monkeypatch.setattr(scheduler,'check_all',lambda:calls.append('check') or 0)
    monkeypatch.setattr(scheduler,'send_digest',lambda **kwargs:calls.append('digest') or True)
    # Both jobs at 06:00 Monday.
    scheduler.save_schedule('digest',dict(scheduler.DEFAULTS['digest'],time='06:00'))
    fixed=fixed.replace(hour=6)
    scheduler.tick();scheduler.tick()
    assert calls==['check','digest']
    fixed=fixed+timedelta(days=1)
    scheduler.tick()
    assert calls==['check','digest','check']

def test_same_run_failure_skips_mail(monkeypatch):
    fixed=datetime(2026,10,19,5,tzinfo=TZ)
    monkeypatch.setattr(scheduler,'now',lambda:fixed)
    scheduler.initialize()
    scheduler.save_schedule('digest',dict(scheduler.DEFAULTS['digest'],time='06:00'))
    monkeypatch.setattr(scheduler,'check_all',lambda:1)
    send=MagicMock();monkeypatch.setattr(scheduler,'send_digest',send)
    fixed=fixed.replace(hour=6)
    scheduler.tick()
    send.assert_not_called()
    assert 'vynechán' in models.setting('schedule_status_digest')

def test_environment_seeds_but_web_controls(monkeypatch):
    monkeypatch.setenv('ISIR_SCHEDULER_ENABLED','false')
    with TestClient(app) as client:
        assert not scheduler.get_schedule('check')['enabled']
        assert not scheduler.get_schedule('digest')['enabled']
        assert client.post('/schedules/check',data=dict(enabled='true',frequency='daily',time='06:00',month_day='1')).status_code==200
        assert scheduler.get_schedule('check')['enabled']
        assert not scheduler.get_schedule('digest')['enabled']
        scheduler.initialize()
        assert scheduler.get_schedule('check')['enabled']
        for bad in [dict(frequency='weekly',time='06:00'),dict(frequency='daily',time='25:00'),dict(frequency='oops',time='06:00')]:
            assert client.post('/schedules/check',data=bad).status_code==400
        assert client.post('/schedules/unknown',data=dict(frequency='daily',time='06:00')).status_code==400
        assert client.post('/schedules/digest',data=dict(enabled='true',frequency='weekly',time='07:00',weekdays=['0','2'],only_changes='true')).status_code==200
        assert scheduler.get_schedule('digest')['weekdays']==[0,2]
        assert scheduler.get_schedule('digest')['only_changes']

def test_monthly_and_dst():
    config=dict(scheduler.DEFAULTS['check'],frequency='monthly',month_day=31)
    assert scheduler.due_slot(datetime(2026,2,28,6,tzinfo=TZ),config).startswith('2026-02-28T06:00')
    config=dict(scheduler.DEFAULTS['check'],time='02:30')
    first=scheduler.due_slot(datetime(2026,10,25,2,45,tzinfo=TZ,fold=0),config)
    second=scheduler.due_slot(datetime(2026,10,25,2,45,tzinfo=TZ,fold=1),config)
    assert first==second
    assert scheduler.due_slot(datetime(2026,10,25,2,15,tzinfo=TZ,fold=1),config)==first
    assert scheduler.due_slot(datetime(2026,3,29,3,30,tzinfo=TZ),config).startswith('2026-03-29T03:30')

def test_changed_only_digest_and_failed_send(monkeypatch):
    sid=subject();models.add_email('test@example.com')
    monkeypatch.setenv('ISIR_MAIL_FROM','sender@example.com')
    smtp=MagicMock();monkeypatch.setattr(mail_digest.smtplib,'SMTP',smtp)
    models.save_result(sid,payload(),1)
    assert not mail_digest.send_digest(only_changes=True)
    smtp.assert_not_called()
    models.save_result(sid,payload('B'),1)
    smtp.side_effect=OSError('offline')
    with pytest.raises(OSError):
        mail_digest.send_digest(only_changes=True)
    assert history.has_new_changes()
    smtp.side_effect=None
    assert mail_digest.send_digest(only_changes=True)
    assert not history.has_new_changes()
    assert not mail_digest.send_digest(only_changes=True)
    assert mail_digest.send_digest() # explicit manual send

def test_web_mail_show_same_history():
    sid=subject()
    models.save_result(sid,payload(),1)
    models.save_result(sid,payload('B'),1)
    with TestClient(app) as client:
        html=client.get('/').text
        assert '7 dní: 1 změn' in html
        assert 'Plánování' in html and 'Koš (0)' in html
    email=mail_digest.templates.get_template('digest.html').render(rows=service.views())
    assert '7 dní: 1 změn' in email and 'Stav řízení' in email
    assert '<details>' not in email
