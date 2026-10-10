import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape
from . import models
from .service import views

templates = Environment(loader=FileSystemLoader(Path(__file__).parent / 'templates'),autoescape=select_autoescape(('html',)))
log = logging.getLogger(__name__)

def send_digest():
    recipients = [r['address'] for r in models.emails(True)]
    if not recipients:
        log.info('DIGEST: no recipients -> nothing to send')
        return
    message = EmailMessage()
    message['From'] = os.getenv('ISIR_MAIL_FROM') or os.getenv('ISIR_SMTP_USER','')
    if not message['From']:
        raise ValueError('ISIR_MAIL_FROM or ISIR_SMTP_USER must be configured')
    message['To'] = 'undisclosed-recipients:;'
    message['Subject'] = os.getenv('ISIR_MAIL_SUBJECT','ISIR Watch – týdenní přehled')
    message.set_content('Váš klient nepodporuje HTML. Otevřete zprávu v prohlížeči.')
    message.add_alternative(templates.get_template('digest.html').render(rows=views()),subtype='html')
    with smtplib.SMTP(os.getenv('ISIR_SMTP_HOST','smtp.gmail.com'),int(os.getenv('ISIR_SMTP_PORT','587')),timeout=25) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        if os.getenv('ISIR_SMTP_USER'):
            smtp.login(os.environ['ISIR_SMTP_USER'],os.environ['ISIR_SMTP_PASS'])
        smtp.send_message(message,to_addrs=recipients)
    log.info('DIGEST sent OK recipients=%s',len(recipients))

def main():
    logging.basicConfig(level=logging.INFO)
    try:
        models.initialize()
        send_digest()
        return 0
    except Exception:
        log.exception('DIGEST failed')
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
