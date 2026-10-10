import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from .config import db_path, now

@contextmanager
def connection():
    path = db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    try:
        with con:
            yield con
    except sqlite3.Error:
        logging.getLogger(__name__).exception('Database operation failed')
        raise
    finally:
        con.close()

def initialize():
    with connection() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS subjects (
          id INTEGER PRIMARY KEY, ic TEXT, nazev TEXT, jmeno TEXT,
          datum_narozeni TEXT, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS results (
          id INTEGER PRIMARY KEY, subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
          checked_at TEXT NOT NULL, count INTEGER NOT NULL, payload_json TEXT, error TEXT);
        CREATE INDEX IF NOT EXISTS results_subject ON results(subject_id,id);
        CREATE TABLE IF NOT EXISTS emails (
          id INTEGER PRIMARY KEY, address TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        columns = {row['name'] for row in con.execute('PRAGMA table_info(subjects)')}
        if 'rc' not in columns:
            backup_path = Path(db_path()).with_name(Path(db_path()).name + '.pre-rc.db')
            if not backup_path.exists():
                with sqlite3.connect(backup_path) as backup:
                    con.backup(backup)
                logging.getLogger(__name__).info('Database backup created before rc migration')
            con.execute("ALTER TABLE subjects ADD COLUMN rc TEXT NOT NULL DEFAULT ''")
            logging.getLogger(__name__).info('Database migrated: added subjects.rc')

def subjects():
    with connection() as con:
        return [dict(r) for r in con.execute('SELECT * FROM subjects ORDER BY id')]

def add_subject(ic='', nazev='', jmeno='', datum_narozeni='', rc=''):
    with connection() as con:
        return con.execute('INSERT INTO subjects(ic,nazev,jmeno,datum_narozeni,created_at,rc) VALUES(?,?,?,?,?,?)',
                           (ic,nazev,jmeno,datum_narozeni,now().isoformat(),rc)).lastrowid

def update_rc(subject_id, rc):
    with connection() as con:
        return con.execute("UPDATE subjects SET rc=? WHERE id=? AND (ic IS NULL OR ic='')", (rc, subject_id)).rowcount

def delete_subject(subject_id):
    with connection() as con:
        con.execute('DELETE FROM subjects WHERE id=?', (subject_id,))

def save_result(subject_id, payload, count, error=None):
    with connection() as con:
        con.execute('INSERT INTO results(subject_id,checked_at,count,payload_json,error) VALUES(?,?,?,?,?)',
                    (subject_id,now().isoformat(),count,json.dumps(payload,ensure_ascii=False,default=str),error))

def latest(subject_id):
    with connection() as con:
        r = con.execute('SELECT * FROM results WHERE subject_id=? ORDER BY id DESC LIMIT 1',(subject_id,)).fetchone()
        return dict(r) if r else None

def emails(enabled_only=False):
    with connection() as con:
        return [dict(r) for r in con.execute('SELECT * FROM emails' + (' WHERE enabled=1' if enabled_only else '') + ' ORDER BY id')]

def add_email(address):
    with connection() as con:
        con.execute('INSERT OR IGNORE INTO emails(address,created_at) VALUES(?,?)',(address,now().isoformat()))

def delete_email(email_id):
    with connection() as con:
        con.execute('DELETE FROM emails WHERE id=?',(email_id,))

def setting(key, value=None):
    with connection() as con:
        if value is not None:
            con.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,value))
        r = con.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        return r[0] if r else None
