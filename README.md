# ICTRecht AI API – speeltuin

Python-scripts om te experimenteren met https://proxy.ai.ictrecht.nl/docs.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt       # of requirements-dev.txt voor de tests (pytest)
cp .env.example .env   # en vul ICTRECHT_API_TOKEN in
```

## Gebruik

```bash
python play.py models                       # GET  /api/models (+ werkt models.json bij)
python play.py prompts                      # GET  /api/v1/prompts
python play.py functions                    # GET  /api/v1/functions
python play.py chat "Wat is de AVG?"        # POST /api/chat/completions
python play.py chat "..." -m <model> --stream
python play.py chat "..." -m 3              # model op nummer uit models.json
python play.py chat -i -s "Je bent een jurist"   # interactief gesprek
python play.py raw GET models               # willekeurig endpoint
python play.py login jij@ictrecht.nl        # token ophalen met e-mail/wachtwoord
```

## In je eigen scripts

```python
from ictrecht_api import Client

client = Client()  # leest token en base-URL uit .env
print(client.models())
resp = client.chat([{"role": "user", "content": "Hoi"}], model="...")
print(resp["choices"][0]["message"]["content"])
```

## Modellen-overzicht (`models.json`)

Iedere `models`-call werkt `models.json` bij: een lijst van alle modellen met een vast
nummer (`nr`, vanaf 0), het `id` en de naam. Dat nummer kun je overal gebruiken waar een
model gevraagd wordt (`-m 3`, of `client.chat(..., model=3)`).

- Nummers blijven stabiel: nieuwe modellen krijgen het volgende vrije nummer.
- Modellen die uit de API-lijst verdwijnen blijven staan met `"available": false`.
- Ander pad: `ICTRECHT_API_MODELS_FILE=pad/naar/models.json`; uitzetten met een lege waarde.

## Logging

Elke API-call wordt gelogd in `logs/api-YYYY-MM-DD.jsonl` (één JSON-regel per call) met
tijdstip, methode, URL, status, duur, request-payload en de volledige response-payload.
Bij `--stream` worden alle ontvangen stream-events opgeslagen. Wachtwoorden worden gemaskeerd.

- Andere map: `ICTRECHT_API_LOG_DIR=pad/naar/map` in `.env`, of `Client(log_dir="...")`
- Uitzetten: `ICTRECHT_API_LOG_DIR=` (leeg), of `Client(log_dir="")`
- Bekijken: `tail -n 1 logs/api-*.jsonl | python -m json.tool`

## Testregister (database, CLI en beheerscherm)

Testcases, modelversies, testruns en beoordelingen staan in een **database** (PostgreSQL in Docker,
of lokaal een SQLite-bestand). Excel is alleen nog import- en exportformaat, in dezelfde opbouw als het
huidige register. Runs en de LLM-beoordelaar draai je via de command line. In het **beheerscherm**
bekijk en wijzig je gegevens en scoren juristen de antwoorden.

### Starten met Docker

```bash
cp .env.example .env            # token invullen; centraal: ook POSTGRES_PASSWORD en GUI_WACHTWOORD
docker compose up -d            # database + beheerscherm op http://localhost:8501
docker compose run --rm cli import "data/Testregister_AI-modellen_ICTRecht_20261116 IE.xlsx"
docker compose run --rm cli run --model IE --dry-run
```

Bestanden voor import en export zet je in `./data` (in de container: `/app/data`). Bij `docker compose up` maakt
of werkt het beheerscherm het databaseschema automatisch bij. De data staan in het Docker-volume `pgdata`.
**Let op:** `docker compose down -v` verwijdert dat volume en dus alle gegevens.

Back-up maken en terugzetten:

```bash
docker compose exec db pg_dump -U testregister -Fc testregister > data/backup_$(date +%Y%m%d).dump
docker compose exec -T db pg_restore -U testregister -d testregister --clean < data/backup_….dump
```

### Zonder Docker

```bash
pip install -r requirements.txt
python testregister.py db init   # SQLite in data/testregister.db, of de database uit DATABASE_URL
streamlit run gui/app.py         # beheerscherm
```

Met `DATABASE_URL` in `.env` gebruikt de CLI buiten Docker de Postgres-container of een centrale server
(zie `.env.example`).

### Commando's

```bash
python testregister.py db init                            # schema aanmaken/bijwerken (Alembic); veilig om te herhalen
python testregister.py import  register.xlsx [--dry-run] [--ja]
python testregister.py export  [--model IE …] [--uit pad.xlsx]
python testregister.py info    [--model IE]
python testregister.py koppel  [--model IE] [--ververs]   # Model-ID ↔ OpenWebUI-id
python testregister.py run     --alle | --model IE [--model AVG …] | --tc IE-KERN-001 [--tc …]   [opties]
python testregister.py beoordeel --alle | --model IE | --run RUN-…   [--versie v1.0] [--opnieuw] [--dry-run]
python testregister.py kalibratie [--model IE] [--versie v1.0]
python testregister.py instelling alleen-goedgekeurd [J|N]
```

`--domein` is een alias van `--model`. In Docker: `docker compose run --rm cli <commando> …`.

**import** maakt eerst een plan, toont het en importeert pas na bevestiging. Er wordt **nooit iets
overschreven**:

- Een testcase met een TC-ID dat al bestaat, of zonder TC-ID, krijgt het volgende vrije nummer
  (`IE-KERN-001` → `IE-KERN-007`). Het oorspronkelijke ID wordt bewaard.
- Staat dezelfde case er al (zelfde model, type, prompt en context), dan wordt hij overgeslagen. Twee keer
  hetzelfde bestand importeren levert dus niets dubbel op.
- Bestaande modelversies en runs worden overgeslagen. Runs uit Excel met scores worden een beoordeling
  (Type beoordelaar, standaard mens).

**export** vult het lege sjabloon `sjabloon/Testregister_sjabloon.xlsx`. Formules, keuzelijsten,
Overzicht en Regressie blijven intact. Het sjabloon heeft ruimte voor 50 modelregels, 300 testcases en
1.000 runs. Past het niet, exporteer dan per model (`--model`). Per run gaat de beoordeling van de jurist
mee, of anders die van de LLM. Antwoorden langer dan 32.000 tekens worden in Excel afgekapt; het volledige
antwoord blijft in de database. Een nieuw sjabloon (bijvoorbeeld uit een nieuwe versie van het
generatorscript) maak je met `python scripts/maak_sjabloon.py <register.xlsx>`.

**run** kiest zonder `--versie` per model de nieuwste versie met status *in test*, en anders de nieuwste
versie die niet is uitgefaseerd. Modellen zonder versie worden bij `--alle` overgeslagen; bij `--model`
stopt het commando. Verder: `--type`, `--alleen-regressie`, `--ook-concept` / `--alleen-goedgekeurd`
(standaard volgt run de instelling *alleen-goedgekeurd*), `--herhalingen`, `--parallel` (max 4),
`--uitvoerder`, `--hervat`, `--dry-run` en `--ja`. Elke prompt gaat in een nieuw gesprek, zonder eigen
systeemprompt of temperatuur. Elke geslaagde run staat direct in de database, met het volledige antwoord,
de payload, de ruwe respons (met bronnen) en de duur. Ctrl-C verliest dus niets.

**beoordeel** scoort volgens [beoordeling/rubric.md](beoordeling/rubric.md) met `--beoordelaar-model` of
`ICTRECHT_JUDGE_MODEL`, en slaat het oordeel op als beoordeling van het type LLM, los van de jurist.
`--opnieuw` (met `--run`) maakt een nieuw oordeel. Het vorige blijft bewaard als verwijderde versie.

**kalibratie** vergelijkt per run de beoordeling van de jurist met die van de LLM: % gelijk eindoordeel
(doel ≥ 80%), gemiste K.O.'s (doel 0), verschil per dimensie en de afwijkende runs. De uitkomst komt ook
als CSV in `output/`.

### Beheerscherm

- **Beoordelen**: per run staan de vraag, het verwachte antwoord (kernpunten, gedrag, bronnen) en het
  antwoord van het model naast elkaar. Daaronder vul je de scores, K.O. en toelichting in. Standaard zie je
  alleen runs die nog niet door een jurist zijn beoordeeld, en na opslaan ga je door naar de volgende.
  Het oordeel van de LLM zit dichtgeklapt, zodat je eerst zelf scoort. Je naam vul je links in.
- **Testcases**, **Testruns**, **Modellen**: tabellen die je bewerkt zoals in Excel. *Verwijderd* aanvinken is
  een soft delete; terughalen kan via *Toon verwijderde*. Een nieuwe testcase krijgt automatisch het volgende
  TC-ID. Een handmatige run (een antwoord uit de OpenWebUI-interface) kun je bij Testruns toevoegen.
- **Overzicht**: per modelversie het aantal runs, het aantal beoordelingen, de eindoordelen en de
  kalibratie.

Bij een centrale installatie zet je `GUI_WACHTWOORD` (eenvoudig gedeeld wachtwoord), `GUI_ADRES=0.0.0.0` en
liefst een reverse proxy met TLS ervoor. Er zijn nog geen persoonlijke accounts: de naam bij een beoordeling
is wat de gebruiker invult.

### Instellingen in `.env`

`ICTRECHT_TESTER` (Uitgevoerd door, en standaardnaam in het beheerscherm), `ICTRECHT_JUDGE_MODEL`,
`ICTRECHT_API_TIMEOUT` (standaard 300 s), `DATABASE_URL`, `POSTGRES_PASSWORD` en `GUI_WACHTWOORD`.
API-calls worden bij time-outs, 429 en 5xx tot 3× herhaald.

**Waarom niet gewoon openpyxl voor het schrijven?** openpyxl verwijdert bij opslaan de keuzelijsten van het
register (Excel bewaart keuzelijsten die naar een ander tabblad verwijzen als extensie die openpyxl niet
kent). Daarom leest het script met openpyxl, maar schrijft het door alleen de XML van de betreffende cellen
aan te passen (`register/xlsxpatch.py`). Excel herberekent de formules bij het openen (`fullCalcOnLoad`).

Tests (zonder netwerk, op SQLite): `python -m pytest`

## Structuur

- `ictrecht_api/client.py`: `Client` met een methode per endpoint, plus `request()` voor de rest
- `play.py`: CLI om snel dingen te proberen
- `testregister.py`: CLI voor het testregister
- `db/`: datamodel (SQLAlchemy), sessie, gedeelde functies (ID's, versies, koppeling) en de migraties (Alembic)
- `register/`: Excel lezen (op kopnaam), import, export via XML-patch, eindoordeel
- `testrunner/`: modelkoppeling, uitvoeren, beoordelen, kalibratie
- `gui/`: beheerscherm (Streamlit)
- `sjabloon/Testregister_sjabloon.xlsx`: leeg register voor de export
- `modelkoppeling.json`: startwaarden voor de koppelingen; worden bij `db init` in een lege database gezet
- `beoordeling/rubric.md`: het scoresysteem; bron voor de beoordelaarsprompt
- `Dockerfile`, `docker-compose.yml`: database, beheerscherm en CLI
- `tests/`: pytest; `tests/fixtures/` bevat een kopie van het IE-voorbeeldregister
- `.env`: je token en instellingen (staat in `.gitignore`)
- `data/`, `logs/`, `output/`: lokale database en import/export, API-logs en CSV's (staan in `.gitignore`)
