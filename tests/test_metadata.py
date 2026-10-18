import sqlite3
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient
from app import models, service, history, isir, mail_digest
from app.main import app

@pytest.fixture(autouse=True)
def database(tmp_path,monkeypatch):
    path=tmp_path/'metadata.db'
    monkeypatch.setenv('ISIR_DB_PATH',str(path))
    models.initialize()
    return path

def test_metadata_create_edit_clear_and_escape():
    note='První řádek\n<script>alert(1)</script>'
    with TestClient(app) as client:
        assert client.post('/subjects',data=dict(ic='00123456',internal_name='Interní klient',note=note)).status_code==200
        subject=models.subjects()[0]
        assert subject['internal_name']=='Interní klient' and subject['note']==note
        body=client.get('/').text
        email=mail_digest.templates.get_template('digest.html').render(rows=service.views())
        for rendered in (body,email):
            assert 'Interní klient' in rendered
            assert 'První řádek' in rendered
            assert '&lt;script&gt;' in rendered and '<script>' not in rendered
        sid=subject['id']
        assert client.post(f'/subjects/{sid}/metadata',data={'internal_name':'Nový název','note':'Nová poznámka'}).status_code==200
        assert models.subjects()[0]['note']=='Nová poznámka'
        client.post(f'/subjects/{sid}/delete')
        assert 'Nový název' in client.get('/trash').text
        assert client.post(f'/subjects/{sid}/metadata',data={'note':'Cannot edit trash'}).status_code==404
        client.post(f'/subjects/{sid}/restore')
        assert models.subjects()[0]['note']=='Nová poznámka'
        assert client.post(f'/subjects/{sid}/metadata',data={}).status_code==200
        assert models.subjects()[0]['note']==models.subjects()[0]['internal_name']==''
        assert client.post('/subjects/9999/metadata',data={}).status_code==404

def test_metadata_validation():
    sid=models.add_subject(ic='00123456')
    with TestClient(app) as client:
        for values in ({'internal_name':'x'*201},{'note':'x'*5001}):
            assert client.post(f'/subjects/{sid}/metadata',data=values).status_code==400
            assert client.post('/subjects',data={'ic':'00123457',**values}).status_code==400
        assert len(models.subjects())==1

def test_metadata_does_not_change_search_or_history(monkeypatch):
    sid=models.add_subject(ic='00123456',nazev='Company')
    payload={'data':[{'ic':'00123456','spis':'KSBR 1 INS 42/2026','stav':'A'}]}
    models.save_result(sid,payload,1)
    before=history.identity(models.subjects()[0])
    client=MagicMock()
    client.service.getIsirWsCuzkData.return_value=payload
    monkeypatch.setattr(isir,'Client',lambda *a,**kw:client)
    isir.query(models.subjects()[0])
    first=client.service.getIsirWsCuzkData.call_args.kwargs
    models.update_metadata(sid,'Internal label','Private annotation')
    isir.query(models.subjects()[0])
    assert client.service.getIsirWsCuzkData.call_args.kwargs==first
    assert history.identity(models.subjects()[0])==before
    models.save_result(sid,payload,1)
    assert not history.windows(sid)[60]

def test_metadata_migration_preserves_all_data(database):
    sid=models.add_subject(ic='00123456')
    models.save_result(sid,{'data':[]},0)
    models.save_result(sid,{'data':[{'ic':'00123456','spis':'KSBR 1 INS 42/2026','stav':'A'}]},1)
    models.add_email('saved@example.com')
    models.setting('saved_setting','keep')
    models.delete_subject(sid)
    with models.connection() as con:
        con.execute('ALTER TABLE subjects DROP COLUMN internal_name')
        con.execute('ALTER TABLE subjects DROP COLUMN note')
        tables=('subjects','results','changes','snapshots','settings','emails')
        originals={table:[tuple(r) for r in con.execute('SELECT * FROM '+table)] for table in tables}
    # Remove only this fixture's earlier empty-database backup to simulate an upgrade.
    backup=Path(str(database)+'.pre-metadata.db')
    backup.unlink()
    models.initialize();models.initialize()
    with sqlite3.connect(database) as con:
        for table in tables:
            if table=='subjects':
                columns=[r[1] for r in con.execute('PRAGMA table_info(subjects)') if r[1] not in ('internal_name','note')]
                rows=con.execute('SELECT '+','.join(columns)+' FROM subjects').fetchall()
            else:
                rows=con.execute('SELECT * FROM '+table).fetchall()
            assert rows==originals[table]
    with sqlite3.connect(backup) as con:
        assert con.execute('SELECT * FROM subjects').fetchall()==originals['subjects']
    subject=models.subjects(True)[0]
    assert subject['internal_name']==subject['note']==''
