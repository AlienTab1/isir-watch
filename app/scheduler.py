"""One in-process scheduler, started only by the standalone launcher."""
import logging
import threading
from datetime import timedelta
from . import models
from .config import now
from .service import check_all
from .mail_digest import send_digest

log = logging.getLogger(__name__)

def due_slot(current):
    monday = (current - timedelta(days=current.weekday())).replace(hour=6,minute=0,second=0,microsecond=0)
    if current < monday:
        monday -= timedelta(days=7)
    return monday.isoformat()

def tick():
    slot = due_slot(now())
    last = models.setting('scheduler_last_slot')
    if last is None:
        # Fresh installations do not send historical notifications.
        models.setting('scheduler_last_slot',slot)
        return
    if last == slot:
        return
    # Mark before execution: restart cannot silently duplicate an email.
    models.setting('scheduler_last_slot',slot)
    if check_all() == 0:
        send_digest()
    else:
        log.error('Scheduled digest skipped: check failed')

def run(stop):
    while not stop.is_set():
        try:
            tick()
        except Exception:
            log.exception('Scheduled job failed; use CLI to retry')
        stop.wait(20)

def start():
    stop = threading.Event()
    thread = threading.Thread(target=run,args=(stop,),name='isir-scheduler',daemon=True)
    thread.start()
    return stop, thread
