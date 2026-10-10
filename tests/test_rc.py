import sqlite3
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient
from app import models, isir, service, mail_digest
from app.main import app

@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / 'legacy.db'
    monkeypatch.setenv('ISIR_DB_PATH',str(path))
    return path

def test_legacy_migration_preserves_everything(database):
    with sqlite3.connect(database) as con:
        con.executescript("""
        CREATE TABLE subjects(id INTEGER PRIMARY KEY,ic TEXT,nazev TEXT,jmeno TEXT,datum_narozeni TEXT,created_at TEXT NOT NULL);
        CREATE TABLE results(id INTEGER PRIMARY KEY,subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,checked_at TEXT NOT NULL,count INTEGER NOT NULL,payload_json TEXT,error TEXT);
        CREATE TABLE emails(id INTEGER PRIMARY KEY,address TEXT NOT NULL UNIQUE,enabled INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
        CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT INTO subjects VALUES(17,'','Test','Person','1980-01-01','2026-01-01');
        INSERT INTO results VALUES(4,17,'2026-01-02',0,'{"data":[]}',NULL);
        INSERT INTO emails VALUES(2,'test@example.com',1,'2026-01-01');
        INSERT INTO settings VALUES('scheduler_last_slot','old-slot');
        """)
        original = {table: con.execute('SELECT * FROM ' + table).fetchall() for table in ('subjects','results','emails','settings')}
    models.initialize()
    models.initialize()
    with sqlite3.connect(database) as con:
        assert con.execute('SELECT id,ic,nazev,jmeno,datum_narozeni,created_at FROM subjects').fetchall() == original['subjects']
        for table in ('results','emails','settings'):
            assert con.execute('SELECT * FROM ' + table).fetchall() == original[table]
        assert con.execute('SELECT rc FROM subjects').fetchone()[0] == ''
    backup = Path(str(database) + '.pre-rc.db')
    with sqlite3.connect(backup) as con:
        assert con.execute('SELECT * FROM subjects').fetchall() == original['subjects']
        assert 'rc' not in [r[1] for r in con.execute('PRAGMA table_info(subjects)')]
    assert models.update_rc(17,'8001011234') == 1
    assert models.latest(17)['id'] == 4
    assert models.subjects()[0]['id'] == 17

@pytest.mark.parametrize('value,expected',[('800101/1234','8001011234'),(' 010101/001 ','010101001'),('8001011234','8001011234')])
def test_rc_format(value,expected):
    assert isir.validate_rc(value) == expected

@pytest.mark.parametrize('value',['abc','123','800101/12','800/1011234','800101 1234'])
def test_rc_invalid(value):
    with pytest.raises(ValueError):
        isir.validate_rc(value)

def test_rc_filter():
    subject = {'rc':'8001011234','nazev':'Optional label','jmeno':'Optional name','datum_narozeni':'1980-01-01'}
    hits = [{'rc':'800101/1234'},{'rodneCislo':'8001011234'},{'rc':'8001011235'},{}]
    assert isir.filter_hits_strict(hits,subject) == hits[:2]

def test_soap_rc_payload(monkeypatch):
    client = MagicMock()
    client.service.getIsirWsCuzkData.return_value = {'data':[]}
    monkeypatch.setattr(isir,'Client',lambda *a,**k:client)
    isir.query({'rc':'010101/001','nazev':'Label','jmeno':'Name','datum_narozeni':'1901-01-01'})
    payload = client.service.getIsirWsCuzkData.call_args.kwargs
    assert payload['rc'] == '010101001'
    assert not any(k in payload for k in ('ic','nazevOsoby','jmeno','datumNarozeni'))
    assert payload['vyhledatPresnouShoduJmen'] == 'F'

def test_rc_web_and_existing_subject(database):
    models.initialize()
    sid = models.add_subject(nazev='Person',jmeno='Test',datum_narozeni='1980-01-01')
    models.save_result(sid,{'data':[{'rc':'800101/1234'},{'rc':'8001011235'}]},2)
    with TestClient(app) as client:
        assert client.post(f'/subjects/{sid}/rc',data={'rc':'800101/1234'}).status_code == 200
        assert models.subjects()[0]['rc'] == '8001011234'
        assert models.latest(sid)['count'] == 2
        assert service.views()[0]['count'] == 1
        assert '8001011234' in client.get('/').text
        digest = mail_digest.templates.get_template('digest.html').render(rows=service.views())
        assert 'Rodné číslo: 8001011234' in digest
        assert 'Nálezy: 1' in digest
        assert client.post('/subjects',data={'rc':'010101/001'}).status_code == 200
        assert models.subjects()[1]['rc'] == '010101001'
        assert client.post('/subjects',data={'rc':'010101001'}).status_code == 200
        assert len(models.subjects()) == 2
        assert client.post('/subjects',data={'rc':'abc'}).status_code == 400
        assert client.post('/subjects',data={'rc':'800101/1234','ic':'00123456'}).status_code == 400
        assert client.post(f'/subjects/{sid}/rc',data={'rc':''}).status_code == 200
        assert models.subjects()[0]['rc'] == ''
        assert models.latest(sid) is not None
