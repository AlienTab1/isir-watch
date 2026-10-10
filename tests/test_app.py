from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient
from app import models, isir, service, mail_digest, scheduler
from app.main import app

@pytest.fixture(autouse=True)
def database(tmp_path,monkeypatch):
    monkeypatch.setenv('ISIR_DB_PATH',str(tmp_path/'test.db'))
    models.initialize()

def person():
    return dict(ic='',nazev='Bosák',jmeno='Jan',datum_narozeni='1980-01-02')

def hit(**changes):
    result = dict(nazevOsoby='Bosák',jmeno='Jan',datumNarozeni='1980-01-02')
    result.update(changes)
    return result

@pytest.mark.parametrize('changes',[{'nazevOsoby':'Bosáková'},{'jmeno':'Petr'},{'datumNarozeni':'1980-01-03'}])
def test_strict(changes):
    assert isir.filter_hits_strict([hit(**changes)],person()) == []

def test_normalization_matching():
    assert len(isir.filter_hits_strict([hit(nazevOsoby=' BOSÁK ',datumNarozeni='02.01.1980')],person()))==1

def test_ico():
    subject = {'ic':'00123456','nazev':'Informativní název'}
    assert len(isir.filter_hits_strict([{'ic':'123456','nazevOsoby':'Jiný název'}],subject))==1
    assert not isir.filter_hits_strict([{'ic':'00123457'},{}],subject)

def test_normalize():
    result = isir.normalize_hit({'nested':hit(ic='00123456',rc='123',nazevOrganizace='Krajský soud v Brně',cisloSenatu=12,druhVec='INS',bcVec=42,rocnik=2026,ulice='Nová',cisloPopisne='1',mesto='Brno',psc='60000',druhStavKonkursu='Aktivní',urlDetailRizeni='https://isir.justice.cz/detail')})
    assert result == dict(spis='KSBR 12 INS 42 / 2026',stav='Aktivní',jmeno='Bosák Jan',ico='00123456',rc='123',narozeni='1980-01-02',adresa='Nová, 1, Brno, 60000',detail_url='https://isir.justice.cz/detail')
    assert not isir.normalize_hit({'url':'javascript:alert(1)'})['detail_url']

def test_db():
    sid = models.add_subject(**person())
    models.save_result(sid,{'data':[hit()]},1)
    assert models.latest(sid)['count']==1
    models.add_email('test@example.com')
    assert len(models.emails(True))==1
    models.delete_email(models.emails()[0]['id'])
    assert not models.emails()
    models.delete_subject(sid)
    assert not models.subjects() and models.latest(sid) is None

def test_count_ui_digest(monkeypatch):
    sid=models.add_subject(**person())
    models.save_result(sid,{'data':[hit()]+[hit(nazevOsoby='Bosáková')]*16},17)
    assert service.views()[0]['count']==1
    with TestClient(app) as client:
        body=client.get('/').text
        assert 'Nálezy: 1' in body and 'Nálezy: 17' not in body
    html=mail_digest.templates.get_template('digest.html').render(rows=service.views())
    assert 'Nálezy: 1' in html and 'Nálezy: 17' not in html

def test_smtp(monkeypatch):
    smtp=MagicMock()
    monkeypatch.setattr(mail_digest.smtplib,'SMTP',smtp)
    mail_digest.send_digest()
    smtp.assert_not_called()
    models.add_email('test@example.com')
    monkeypatch.setenv('ISIR_MAIL_FROM','sender@example.com')
    mail_digest.send_digest()
    msg=smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
    assert msg.get_body(preferencelist=('html',)).get_content_type()=='text/html'
    assert msg.get_body(preferencelist=('plain',)).get_content_type()=='text/plain'
    smtp.side_effect=OSError('unavailable')
    assert mail_digest.main()==1

def test_errors_continue(monkeypatch):
    models.add_subject(**person())
    models.add_subject(ic='00123456')
    mock=MagicMock(side_effect=[RuntimeError('offline'),{'data':[]}])
    monkeypatch.setattr(isir,'query',mock)
    assert service.check_all()==1 and mock.call_count==2
    assert models.latest(1)['error']

def test_soap_status(monkeypatch):
    sid=models.add_subject(**person())
    monkeypatch.setattr(isir,'query',lambda s: {'data':[],'stav':{'kodChyby':'SQL1'}})
    assert not service.check_subject(models.subjects()[0])
    assert 'SQL1' in models.latest(sid)['error']

def test_web_validation_and_escape():
    with TestClient(app) as client:
        assert client.post('/subjects',data={'ic':'00123456'},headers={'Origin':'https://other.example'}).status_code==403
        assert client.post('/subjects',data={'ic':'123'}).status_code==400
        assert client.post('/subjects',data={'ic':'00123456','nazev':'<script>'}).status_code==200
        assert '&lt;script&gt;' in client.get('/').text
        assert client.post('/subjects',data={'ic':'00123456','nazev':'<script>'}).status_code==200
        assert len(models.subjects())==1
        assert client.get('/health').status_code==200

def test_scheduler(monkeypatch):
    current=datetime(2026,10,12,6,0,tzinfo=ZoneInfo('Europe/Prague'))
    monkeypatch.setattr(scheduler,'now',lambda:current)
    models.setting('scheduler_last_slot','2026-10-05T06:00:00+02:00')
    check=MagicMock(return_value=0)
    send=MagicMock()
    monkeypatch.setattr(scheduler,'check_all',check)
    monkeypatch.setattr(scheduler,'send_digest',send)
    scheduler.tick();scheduler.tick()
    assert check.call_count==send.call_count==1

def test_scheduler_failure(monkeypatch):
    monkeypatch.setattr(scheduler,'now',lambda:datetime(2026,10,12,6,0,tzinfo=ZoneInfo('Europe/Prague')))
    models.setting('scheduler_last_slot','old')
    monkeypatch.setattr(scheduler,'check_all',lambda:1)
    send=MagicMock();monkeypatch.setattr(scheduler,'send_digest',send)
    scheduler.tick()
    send.assert_not_called()

def test_dst():
    tz=ZoneInfo('Europe/Prague')
    assert scheduler.due_slot(datetime(2026,10,26,6,0,tzinfo=tz)).endswith('+01:00')
    assert scheduler.due_slot(datetime(2026,10,19,6,0,tzinfo=tz)).endswith('+02:00')
