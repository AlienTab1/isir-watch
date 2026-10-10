import logging
import os
import uvicorn
from . import models, scheduler

def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
    models.initialize()
    stop = thread = None
    if os.getenv('ISIR_SCHEDULER_ENABLED','true').lower() == 'true':
        stop,thread = scheduler.start()
    try:
        uvicorn.run('app.main:app',host='0.0.0.0',port=int(os.getenv('PORT','80')),workers=1)
    finally:
        if stop:
            stop.set()
            thread.join(timeout=5)

if __name__ == '__main__':
    main()
