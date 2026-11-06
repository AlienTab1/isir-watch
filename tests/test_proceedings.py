import json
import sqlite3
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient
from app import models, proceedings as p, mail_digest, service
from app.main import app

@pytest.fixture(autouse=True)
def db(tmp_path,monkeypatch):
    monkeypatch.setenv('ISIR_DB_PATH',str(tmp_path/'test.db'))
    models.initialize()

def setup_case():
    sid=models.add_subject(ic='00123456',internal_name='Internal company',note='Note')
    p.register(sid,[{'spis':'KSBR 32 INS 11536 / 2025'}])
    models.setting('proceedings_cursor','100')
    return sid

def action(i,section='B',number=43,**extra):
    return dict(id=i,spisovaZnacka='INS 11536/2025',oddil=section,cisloVOddilu=number,
                popisUdalosti='Sdělení',datumZverejneniUdalosti='2026-11-03T12:35:00+01:00',**extra)

def test_sections_duplicates_updates_and_cursor():
    sid=setup_case()
    assert p.apply_batch(100,[action(101),action(102,'C20',11),action(103,'P1',1)])
    assert not p.apply_batch(100,[action(101)])
    p.apply_batch(103,[action(104,dokumentUrl='https://isir.justice.cz/doc/Document?idDokument=1')])
    view=p.overview(sid)
    assert view['counts'][7]==4
    assert set(view['groups']['INS 11536/2025'])=={'B','C20','P1'}
    assert p.rows()[0]['kind']=='Aktualizace záznamu'
    assert models.setting('proceedings_cursor')=='104'

def test_atomic_failure_and_retry():
    setup_case()
    with pytest.raises(Exception):
        p.apply_batch(100,[action(101),action(102,poznamka='<broken')])
    assert models.setting('proceedings_cursor')=='100' and not p.rows()
    assert p.apply_batch(100,[action(101),action(102)])
    assert len(p.rows())==2

def test_unrelated_actions_and_document_safety():
    sid=setup_case()
    other=action(101);other['spisovaZnacka']='INS 999/2025'
    p.apply_batch(100,[other,action(102,dokumentUrl='javascript:alert(1)')])
    assert len(p.rows())==1 and p.rows()[0]['document_url']==''
    models.delete_subject(sid)
    assert not p.rows()
    models.restore_subject(sid)
    assert len(p.rows())==1

def test_modifiers_and_withdrawal():
    setup_case()
    p.apply_batch(100,[action(101,poznamka='<udalost><priznakAnVedlejsiDokument>T</priznakAnVedlejsiDokument></udalost>')])
    assert p.rows()[0]['kind']=='Vedlejší dokument'
    p.apply_batch(101,[action(102,section='',poznamka='<udalost><datumVyskrtnuti>2026-11-03</datumVyskrtnuti></udalost>')])
    assert not p.rows()

def test_init_and_restart_no_loss(monkeypatch):
    sid=models.add_subject(ic='00123456')
    fake=MagicMock();fake.head.return_value=100
    monkeypatch.setattr(p,'Feed',lambda:MagicMock(__enter__=lambda s:fake,__exit__=lambda *a:None))
    assert p.sync()
    fake.batch.assert_not_called()
    assert models.setting('proceedings_since')
    p.register(sid,[{'spis':'KSBR 32 INS 11536/2025'}])
    fake.head.return_value=101;fake.batch.return_value=[action(101)]
    assert p.sync()
    assert len(p.rows())==1
    fake.batch.reset_mock()
    assert p.sync()
    fake.batch.assert_not_called()

def test_email_marker_success_only(monkeypatch):
    setup_case();models.add_email('test@example.com')
    monkeypatch.setenv('ISIR_MAIL_FROM','sender@example.com')
    smtp=MagicMock();monkeypatch.setattr(mail_digest.smtplib,'SMTP',smtp)
    assert not mail_digest.send_digest(only_changes=True)
    p.apply_batch(100,[action(101)])
    smtp.side_effect=OSError('offline')
    with pytest.raises(OSError):mail_digest.send_digest(only_changes=True)
    assert p.has_new_changes()
    smtp.side_effect=None
    assert mail_digest.send_digest(only_changes=True)
    assert not p.has_new_changes()
    assert not mail_digest.send_digest(only_changes=True)

def test_overview_email_and_time_filter():
    sid=setup_case()
    p.apply_batch(100,[action(101),action(102,'C20',11)])
    with TestClient(app) as client:
        html=client.get('/?days=30').text
        assert 'Přehled novinek ve spisech' in html and 'B-43' in html and 'C20-11' in html
        assert 'Internal company' in html and 'Note' in html
        assert client.get('/?days=31').status_code==422
    email=mail_digest.templates.get_template('digest.html').render(rows=service.views(60))
    assert 'B-43' in email and 'C20-11' in email and '<details>' not in email
    assert p.overview(sid)['counts']=={7:2,30:2,60:2}

def test_migration_idempotent_preserves_existing_tables():
    sid=setup_case();models.save_result(sid,{'data':[]},0)
    models.add_email('saved@example.com');models.setting('custom','keep')
    with models.connection() as con:
        tables=['subjects','results','emails','settings','snapshots','changes']
        before={t:[tuple(r) for r in con.execute('SELECT * FROM '+t)] for t in tables}
    models.initialize();models.initialize()
    with models.connection() as con:
        assert before=={t:[tuple(r) for r in con.execute('SELECT * FROM '+t)] for t in tables}
