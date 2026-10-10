import os
from datetime import datetime
from zoneinfo import ZoneInfo

def now():
    return datetime.now(ZoneInfo(os.getenv('TZ', 'Europe/Prague')))

def db_path():
    return os.getenv('ISIR_DB_PATH', 'data/isir-watch.db')
