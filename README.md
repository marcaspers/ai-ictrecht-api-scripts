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

## Testregister-runner (`testregister.py`)

Draait de testcases uit het testregister (Excel) via de API, schrijft de antwoorden als testruns
in een **nieuwe versie** van het register en laat ze scoren door een LLM-beoordelaar.

```bash
python testregister.py info       register.xlsx [--model IE]                # overzicht + validatie
python testregister.py koppel     register.xlsx [--ververs]                 # Model-ID ↔ OpenWebUI-id
python testregister.py run        register.xlsx --model IE --versie v1.0 --dry-run
python testregister.py run        register.xlsx --model IE --versie v1.0 --alleen-regressie --herhalingen 3
python testregister.py beoordeel  register.xlsx --model IE --versie v1.0 [--schrijf-in-register]
python testregister.py kalibratie register.xlsx --model IE --versie v1.0
```

Elk schrijvend commando maakt `Testregister_AI-modellen_ICTRecht_<jjjjmmdd>.xlsx` naast het
invoerbestand (`_2`, `_3`, … als dat al bestaat), of het pad uit `--uit`. Het invoerbestand
wordt nooit aangepast.

**run** – selecteert de cases van één model (`--tc`, `--type`, `--alleen-regressie`), volgt
Overzicht!B7 voor goedgekeurd/concept (overschrijven met `--ook-concept` / `--alleen-goedgekeurd`),
slaat *vervallen* en bijlagen over, controleert vooraf de vrije regels in Testruns en vraagt om
bevestiging (`--ja` om dat over te slaan). Elke prompt gaat in een nieuw gesprek zonder eigen
systeemprompt of temperatuur. Opties: `--herhalingen`, `--parallel` (max 4), `--uitvoerder`,
`--hervat` (cases overslaan die vandaag al voor die versie zijn gedraaid).

**beoordeel** – scoort runs zonder scores volgens [beoordeling/rubric.md](beoordeling/rubric.md)
met het model uit `--beoordelaar-model` of `ICTRECHT_JUDGE_MODEL`. Standaard alleen naar
`output/beoordelingen/<Run-ID>.json`; het register blijft voor de jurist. Met `--run RUN-…` kan
ook een run die de jurist al scoorde worden beoordeeld (voor kalibratie); `--opnieuw` beoordeelt
opnieuw en bewaart het vorige oordeel.

**kalibratie** – vergelijkt LLM-oordelen met de scores van de jurist (Type beoordelaar = mens):
% gelijk eindoordeel (doel ≥ 80%), gemiste K.O.'s (doel 0), verschil per dimensie en de runs met
een afwijkend eindoordeel. Ook als CSV in `output/`.

Bestanden:

- `modelkoppeling.json` (in git) – bevestigde koppelingen Model-ID → OpenWebUI-id. Wordt bij elke
  run tegen de actuele modellenlijst gecontroleerd; bij twijfel vraagt het script (of stopt met
  `--niet-interactief`).
- `output/runs/<Run-ID>.json` – auditspoor per run: verzonden payload, volledige respons (incl.
  bronnen), starttijd en duur. Lange antwoorden worden in Excel afgekapt op 32.000 tekens; het
  volledige antwoord staat hier.
- `output/beoordelingen/<Run-ID>.json` – oordeel van de beoordelaar plus de ruwe uitvoer.

Instellingen in `.env`: `ICTRECHT_TESTER` (Uitgevoerd door), `ICTRECHT_JUDGE_MODEL`,
`ICTRECHT_API_TIMEOUT` (standaard 300 s). API-calls worden bij time-outs, 429 en 5xx tot 3× herhaald.

**Waarom niet gewoon openpyxl voor het schrijven?** openpyxl verwijdert bij opslaan de
keuzelijsten van het register (Excel bewaart keuzelijsten die naar een ander tabblad verwijzen
als extensie die openpyxl niet kent). Het script leest daarom met openpyxl, maar schrijft door
alleen de betreffende cellen in de XML van het tabblad Testruns aan te passen
(`register/xlsxpatch.py`). Alle andere onderdelen blijven byte voor byte gelijk. Excel
herberekent de formules bij het openen (`fullCalcOnLoad`).

Tests (zonder netwerk): `python -m pytest`

## Structuur

- `ictrecht_api/client.py` – `Client` met een methode per endpoint, plus `request()` voor de rest
- `play.py` – CLI om snel dingen te proberen
- `testregister.py` – CLI voor de testregister-runner
- `register/` – register lezen (op kopnaam), validatie, Run-ID's, eindoordeel, schrijven via XML-patch
- `testrunner/` – modelkoppeling, uitvoeren, beoordelen, kalibratie
- `beoordeling/rubric.md` – scoresysteem; bron voor de beoordelaarsprompt
- `tests/` – pytest; `tests/fixtures/` bevat een kopie van het IE-voorbeeldregister
- `.env` – je token (staat in `.gitignore`)
- `logs/`, `output/` – API-logs en testrunner-uitvoer (staan in `.gitignore`)
