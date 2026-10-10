# ISIR Watch

ISIR Watch je samostatná webová aplikace pro sledování fyzických a právnických
osob v českém insolvenčním rejstříku (ISIR). Umožňuje spravovat sledované
subjekty, kontrolovat jejich aktuální řízení a zasílat pravidelný e-mailový přehled.
Údaje získává z veřejné SOAP služby Justice.cz.

## Funkce

- Evidence právnických osob podle IČO a fyzických osob podle jména a data narození.
- Ruční kontrola jednotlivých subjektů i hromadná kontrola z příkazové řádky.
- Přesné lokální filtrování výsledků pro omezení záměny podobných jmen.
- Webový přehled posledních kontrol, nalezených řízení a případných chyb.
- Správa příjemců a HTML e-mailový digest.
- Automatická pondělní kontrola s navazujícím odesláním přehledu.
- Samostatný Docker kontejner a připravený deployment YAML pro TrueNAS.

## Architektura a ukládání dat

Python, FastAPI a Jinja2 zajišťují webové rozhraní, zeep komunikaci se SOAP
službou a SQLite ukládání subjektů, odpovědí a příjemců. Jeden kontejner obsahuje
web, databázi a jednoduchý plánovač; externí databáze ani scheduler nejsou potřeba.

Ve výchozí konfiguraci je SQLite **uvnitř kontejneru** v `/data/isir-watch.db`.
Restart stejného kontejneru zachová data, jeho odstranění/recreate/update je ztratí.
Dockerfile záměrně nedeklaruje VOLUME. Před aktualizací proveďte backup.

## Spuštění

```sh
cp .env.example .env
# Nastavte SMTP a odesílatele v .env.
docker compose build
docker compose up -d
docker compose logs -f
```

Web: http://localhost:8081. Bez Compose:

```sh
docker build -t isir-watch .
docker run -d --name isir-watch --restart unless-stopped --env-file .env -p 8081:80 isir-watch
```

Na první spuštění není potřeba SMTP; to je nutné až pro digest s příjemci.
Zadejte subjekty a příjemce, proveďte ruční kontrolu a testovací digest.
Pro vypnutí plánování při nastavování použijte `ISIR_SCHEDULER_ENABLED=false`.
`.env` obsahuje hesla, nepatří do Gitu. Gmail vyžaduje app password.
Web nemá přihlašování a je určen pouze pro důvěryhodnou interní síť.

## Ruční operace

```sh
docker compose exec app python -m app.check_all
docker compose exec app python -m app.mail_digest
```

Ruční kontrola neposílá digest. Automatická pondělní kontrola v 06:00 Europe/Prague
odešle digest až po úspěchu všech subjektů. Žádné systemd/cron/APScheduler nejsou potřeba.
Scheduler běží pouze přes `python -m app.run`, v jediném procesu a jednom kontejneru.
Nepouštějte více replik nad stejnou databází. Přímý Uvicorn scheduler nespouští.

Při prvním spuštění scheduler označí poslední termín bez zpětného odeslání.
Při dalších restartech dožene poslední zmeškané pondělí. Termín zaznamená před
spuštěním: při pádu/SMTP chybě se automaticky neopakuje, aby se nezdvojovaly zprávy.
Chybu řešte ručním příkazem. CET/CEST zajišťuje Python zoneinfo + tzdata.
Při chybě jednoho subjektu kontrola pokračuje a vrací nenulový exit code; automatický digest se vynechá.
Ruční digest zobrazuje uložené chyby výslovně, nikoli jako nulové nálezy.

SOAP používá široké hledání a lokální přesné porovnání jmen (Unicode NFC, trim,
velikost písmen; diakritika zůstává významná), normalizovaného data a IČO.
Při dotazu IČO je nález bez IČO konzervativně vyřazen; název je pouze popisek.
Limit 50 výsledků je v přehledu označen jako potenciálně neúplný výsledek.
Raw odpovědi jsou uchovány a web i digest přepočítávají nálezy společnou funkcí.

## Záloha a obnova

Pro konzistentní kopii zastavte kontejner (upravte název podle `docker compose ps`):

```sh
docker stop isir-watch
docker cp isir-watch:/data/isir-watch.db ./isir-watch.db
docker start isir-watch
```

Zálohujte také `.env` a deployment YAML. Pro obnovu do existujícího zastaveného
kontejneru zkopírujte DB zpět; vlastník souboru musí být UID 10001.
Pro zachování dat při výměně kontejneru lze připojit volume nebo bind mount
do `/data`, aniž by se měnil kód. Připojený adresář musí být zapisovatelný pro UID 10001.

## GitHub image a TrueNAS

Workflow `.github/workflows/image.yml` spouští testy a sestavuje image. Push na
`main` publikuje `ghcr.io/<vlastník>/<repo>:latest`; tag `v*` vytvoří verzovaný tag.
Pull request image sestaví bez publikování.
Pro anonymní stahování nastavte publikovaný GHCR package na public.

`deploy/truenas.yaml` je Compose YAML pro TrueNAS s Docker Apps (24.10+).
Nahraďte image skutečným GHCR názvem, nastavte SMTP a vložte YAML do
Apps → Install via YAML. Nevyžaduje lokální build ani `.env` soubor.
Podrobnosti: [TrueNAS – Installing Custom Apps](https://apps.truenas.com/managing-apps/installing-custom-apps/).
Databáze se při aktualizaci image bez mountu ztratí, proto nejdřív exportujte zálohu.

## Lokální vývoj a testy

```sh
python -m venv .venv
# Aktivujte virtualenv.
pip install -r requirements.txt
python -m pytest -q
# Lokálně nastavte ISIR_DB_PATH=data/isir-watch.db a PORT=8081.
python -m app.run
```

Unit testy nevyužívají Justice.cz ani SMTP server. Testují přesnou filtraci,
normalizaci, DB, web/digest count, SMTP chyby a pondělní plánování včetně DST.

Čtecí integrační test SOAP (testovací IČO 00000000, bez ukládání a e-mailu):
`python -m scripts.soap_smoke`. Vyžaduje dostupnost Justice.cz na portu 8443.
