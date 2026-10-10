import logging
from . import models
from .service import check_all

def main():
    logging.basicConfig(level=logging.INFO)
    try:
        models.initialize()
        return check_all()
    except Exception:
        logging.exception('Fatal check error')
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
