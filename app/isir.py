import os
import unicodedata
from datetime import date, datetime
from urllib.parse import urlparse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from zeep import Client, helpers
from zeep.transports import Transport

WSDL = 'https://isir.justice.cz:8443/isir_cuzk_ws/IsirWsCuzkService?wsdl'

def norm(value):
    return ' '.join(unicodedata.normalize('NFC', str(value or '')).casefold().split())

def dob(value):
    if isinstance(value, (date, datetime)):
        return value.strftime('%Y-%m-%d')
    text = str(value or '').strip()
    for pattern in ('%Y-%m-%d', '%d.%m.%Y', '%d. %m. %Y'):
        try:
            return datetime.strptime(text[:10] if pattern == '%Y-%m-%d' else text, pattern).date().isoformat()
        except ValueError:
            pass
    return text

def flatten(hit):
    out = {}
    if isinstance(hit, dict):
        for key, value in hit.items():
            if isinstance(value, dict):
                for k, v in flatten(value).items():
                    out.setdefault(k, v)
            elif not isinstance(value, (list, tuple)):
                out[key] = value
    return out

def pick(hit, *keys):
    return next((hit[k] for k in keys if hit.get(k) is not None and hit[k] != ''), '')

def get_hits(payload):
    if isinstance(payload, (list, tuple)):
        return [h for h in payload if isinstance(h, dict)]
    if isinstance(payload, dict):
        for key in ('vysledky','seznam','dluznici','items','data','osoba'):
            if key in payload:
                value = payload[key]
                if isinstance(value, (list, tuple)):
                    return get_hits(value)
                if isinstance(value, dict):
                    result = get_hits(value)
                    if result:
                        return result
        for value in payload.values():
            if isinstance(value, (dict, list, tuple)):
                result = get_hits(value)
                if result:
                    return result
    return []

def filter_hits_strict(hits, subject):
    out = []
    for raw in hits:
        h = flatten(raw)
        if subject.get('ic'):
            # Missing IČO cannot establish identity: reject conservatively.
            if str(pick(h,'ic','ico','IC')).strip().zfill(8) != subject['ic']:
                continue
        else:
            if subject.get('nazev') and norm(pick(h,'nazevOsoby','prijmeni','nazev')) != norm(subject['nazev']):
                continue
            if subject.get('jmeno') and norm(pick(h,'jmeno')) != norm(subject['jmeno']):
                continue
            if subject.get('datum_narozeni') and dob(pick(h,'datumNarozeni','narozeni')) != dob(subject['datum_narozeni']):
                continue
        out.append(raw)
    return out

COURTS = dict(zip(('Brně','Českých Budějovicích','Hradci Králové','Ostravě','Plzni','Praze','Ústí nad Labem','Olomouci','Pardubicích'),
                  ('KSBR','KSCB','KSHK','KSOS','KSPL','KSPH','KSUL','KSOL','KSPA')))

def normalize_hit(raw):
    h = flatten(raw)
    spis = pick(h,'spisovaZnacka','spisZnacka','spis','spisovaZnackaText','spisZnackaPlna')
    if not spis:
        court = str(pick(h,'nazevOrganizace'))
        court = next((code for city, code in COURTS.items() if court == 'Krajský soud v ' + city), court)
        if h.get('bcVec') and h.get('rocnik'):
            spis = f"{court} {pick(h,'cisloSenatu')} {pick(h,'druhVec')} {h['bcVec']} / {h['rocnik']}".strip()
    name = ' '.join(str(v) for v in (pick(h,'nazevOsoby','prijmeni'),pick(h,'jmeno')) if v)
    address = pick(h,'adresa','adresaText','sidloText','sídloText','sidlo')
    if not address:
        address = ', '.join(str(h[k]) for k in ('ulice','cisloPopisne','mesto','psc') if h.get(k))
    url = str(pick(h,'urlDetailRizeni','detail_url','detailUrl','url'))
    parsed = urlparse(url)
    if parsed.scheme not in ('https','http') or parsed.hostname not in ('isir.justice.cz','or.justice.cz'):
        url = ''
    return dict(spis=spis, stav=pick(h,'stavRizeni','stav','stadium','stavRizeniText','stavText','druhStavKonkursu'),
                jmeno=name or pick(h,'jmenoNeboNazev','nazev'), ico=pick(h,'ic','ico'), rc=pick(h,'rc','rodneCislo'),
                narozeni=dob(pick(h,'datumNarozeni','narozeni')), adresa=address, detail_url=url)

def query(subject):
    session = requests.Session()
    session.mount('https://', HTTPAdapter(max_retries=Retry(total=3,backoff_factor=.5,
                  status_forcelist=(500,502,503,504),allowed_methods=frozenset(('GET','POST')))))
    try:
        client = Client(os.getenv('ISIR_WSDL',WSDL),transport=Transport(session=session,timeout=25,operation_timeout=25))
        payload = dict(filtrAktualniRizeni='T',vyhledatBezDiakritiky='T',vyhledatPresnouShoduJmen='F',maxPocetVysledku=50,maxRelevanceVysledku=7)
        for key, remote in (('ic','ic'),('nazev','nazevOsoby'),('jmeno','jmeno'),('datum_narozeni','datumNarozeni')):
            if subject.get('ic') and key != 'ic':
                continue
            if subject.get(key):
                payload[remote] = subject[key]
        return helpers.serialize_object(client.service.getIsirWsCuzkData(**payload),target_cls=dict)
    finally:
        session.close()
