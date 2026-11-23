# ICTRecht AI API – speeltuin

Python-scripts om te experimenteren met https://proxy.ai.ictrecht.nl/docs.

## Setup

Alles kan via Docker; dan hoef je lokaal geen Python te installeren. Alleen Docker (Desktop) is nodig.

```bash
cp .env.example .env            # en vul ICTRECHT_API_TOKEN in
docker compose build            # image bouwen (opnieuw na elke git pull)
```

Zonder Docker:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt       # of requirements-dev.txt voor de tests (pytest)
cp .env.example .env   # en vul ICTRECHT_API_TOKEN in
```

## Gebruik

Via Docker (`play` is `python play.py` in de container):

```bash
docker compose run --rm play models                  # GET  /api/models (+ werkt models.json bij)
docker compose run --rm play prompts                 # GET  /api/v1/prompts
docker compose run --rm play functions               # GET  /api/v1/functions
docker compose run --rm play chat "Wat is de AVG?"   # POST /api/chat/completions
docker compose run --rm play chat "..." -m <model> --stream
docker compose run --rm play chat "..." -m 3         # model op nummer uit models.json
docker compose run --rm play chat -i -s "Je bent een jurist"   # interactief gesprek
docker compose run --rm play raw GET models          # willekeurig endpoint
docker compose run --rm play login jij@ictrecht.nl   # token ophalen met e-mail/wachtwoord
```

Zonder Docker is het hetzelfde met `python play.py …` in plaats van `docker compose run --rm play …`.
`data/` (met `data/models.json`) en `logs/` worden in de container gekoppeld, dus wijzigingen komen gewoon in je projectmap.

## In je eigen scripts

```python
from ictrecht_api import Client

client = Client()  # leest token en base-URL uit .env
print(client.models())
resp = client.chat([{"role": "user", "content": "Hoi"}], model="...")
print(resp["choices"][0]["message"]["content"])
```

## Modellen-overzicht (`data/models.json`)

Iedere `models`-call werkt `data/models.json` bij: een lijst van alle modellen met een vast
nummer (`nr`, vanaf 0), het `id` en de naam. Dat nummer kun je overal gebruiken waar een
model gevraagd wordt (`-m 3`, of `client.chat(..., model=3)`).

- Nummers blijven stabiel: nieuwe modellen krijgen het volgende vrije nummer.
- Modellen die uit de API-lijst verdwijnen blijven staan met `"available": false`.
- Het bestand is lokaal en staat niet in git; de nummers gelden dus per computer. Ontbreekt het, dan wordt
  het bij de eerstvolgende `models`-call (of `-m <nummer>`) opnieuw opgebouwd.
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
docker compose build
docker compose up -d            # database + beheerscherm op http://localhost:8501
```

`docker compose up -d` start de database en het beheerscherm, en maakt of werkt het databaseschema
automatisch bij. De command line draai je daarnaast met `docker compose run --rm cli <commando>`: elke
keer een tijdelijke container die na afloop verdwijnt. Vragen om bevestiging (`[j/N]`) werken gewoon.

```bash
docker compose run --rm cli db init        # alleen nodig als het beheerscherm niet draait
docker compose run --rm cli import "data/Testregister_AI-modellen_ICTRecht_20261116 IE.xlsx"
docker compose run --rm cli info --model IE
docker compose run --rm cli run --model IE --dry-run
docker compose run --rm cli run --model IE
docker compose run --rm cli beoordeel --model IE
docker compose run --rm cli kalibratie --model IE
docker compose run --rm cli export --model IE
```

Tip: met `alias tr='docker compose run --rm cli'` typ je `tr run --model IE`.

Welke mappen de container ziet:

| Op je computer | In de container | Waarvoor |
|---|---|---|
| `./data` | `/app/data` | Excel-bestanden voor import, exports, back-ups, `models.json` (modellenlijst) |
| `./output` | `/app/output` | CSV van kalibratie |
| `./logs` | `/app/logs` | API-log |

Paden in commando's zijn relatief aan `/app`. Een register importeren doe je dus door het eerst in `./data`
te zetten en dan `data/<bestand>.xlsx` mee te geven. Bestanden buiten deze mappen ziet de container niet.
`.env` wordt bij het starten ingelezen; na een wijziging in `.env` is `docker compose up -d` genoeg (een
nieuwe build is niet nodig). Na een `git pull` wel: `docker compose build && docker compose up -d`.

De data staan in het Docker-volume `pgdata`. `docker compose down` stopt alles en laat de data staan.
**Let op:** `docker compose down -v` verwijdert dat volume en dus alle gegevens.

Overige handige commando's:

```bash
docker compose ps                          # wat draait er
docker compose logs -f gui                 # log van het beheerscherm
docker compose exec db psql -U testregister testregister   # SQL-prompt op de database
```

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

Hieronder staat steeds `testregister.py`. In Docker vervang je `python testregister.py` door
`docker compose run --rm cli`.

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
python testregister.py <commando> --help                  # alle opties
```

`--domein` is een alias van `--model`.

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
`--uitvoerder`, `--dry-run` en `--ja`. Al gedraaide cases overslaan: `--hervat` (vandaag al gedraaid voor
die versie, om na afbreken verder te gaan) of `--alleen-nieuw` (ooit al gedraaid voor die versie). Elke
prompt gaat in een nieuw gesprek, zonder eigen systeemprompt of temperatuur. Elke geslaagde run staat direct in de database, met het volledige antwoord,
de payload, de ruwe respons (met bronnen) en de duur. Ctrl-C verliest dus niets.

**beoordeel** scoort volgens [beoordeling/rubric.md](beoordeling/rubric.md) met `--beoordelaar-model` of
`ICTRECHT_JUDGE_MODEL`, en slaat het oordeel op als beoordeling van het type LLM, los van de jurist.
`--opnieuw` (met `--run`) maakt een nieuw oordeel. Het vorige blijft bewaard als verwijderde versie.

**kalibratie** vergelijkt per run de beoordeling van de jurist met die van de LLM: % gelijk eindoordeel
(doel ≥ 80%), gemiste K.O.'s (doel 0), verschil per dimensie en de afwijkende runs. De uitkomst komt ook
als CSV in `output/`. Alleen definitieve beoordelingen van de jurist tellen mee, en geen beoordelingen die
vanuit de LLM-scores zijn gestart (werkwijze *overgenomen*): die zijn niet onafhankelijk.

### Beheerscherm

- **Beoordelen**: per run staan de vraag, het verwachte antwoord (kernpunten, gedrag, bronnen) en het
  antwoord van het model naast elkaar. Daaronder staat per dimensie de score van de LLM naast die van de
  jurist, met het verschil en het eindoordeel dat meerekent. Daarnaast de kernpunten, de te verifiëren
  punten (af te vinken) en de toelichting van de LLM. Je naam vul je links in.
  - *Review* (standaard): je beoordeling start als klad met de scores van de LLM; pas aan en kies
    **Opslaan als klad** of **Definitief maken**. Telt niet mee voor de kalibratie.
  - *Blind beoordelen* (schakelaar links): het LLM-oordeel blijft verborgen tot je definitief maakt. Zet de
    schakelaar aan vóórdat je runs opent; een run waarvan je het LLM-oordeel al zag, telt niet als blind.
  Standaard zie je alleen runs die nog niet definitief zijn; na definitief maken ga je door naar de volgende.
  Een klad telt nog niet mee in Overzicht, kalibratie en export.
- **Testcases**, **Testruns**, **Modellen**: tabellen die je bewerkt zoals in Excel. *Verwijderd* aanvinken is
  een soft delete; terughalen kan via *Toon verwijderde*. Een nieuwe testcase krijgt automatisch het volgende
  TC-ID. Een handmatige run (een antwoord uit de OpenWebUI-interface) kun je bij Testruns toevoegen.
  Testruns toont per run de beoordelingsstatus, de scores van jurist en LLM (`J2 V1 B3 U2 G–`), de eindoordelen
  en K.O.'s, met filters op status en eindoordeel; bij *Details* open je een run direct in Beoordelen.
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

Tests (zonder netwerk, op SQLite): `pip install -r requirements-dev.txt && python -m pytest`. De tests
draaien lokaal; pytest zit niet in de Docker-image.

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
- `Dockerfile`, `docker-compose.yml`: database (`db`), beheerscherm (`gui`), testregister-CLI (`cli`) en speeltuin (`play`)
- `tests/`: pytest; `tests/fixtures/` bevat een kopie van het IE-voorbeeldregister
- `.env`: je token en instellingen (staat in `.gitignore`)
- `data/`, `logs/`, `output/`: lokale database en import/export, API-logs en CSV's (staan in `.gitignore`)
