# ICTRecht AI API – speeltuin

Python-scripts om te experimenteren met https://proxy.ai.ictrecht.nl/docs.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
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

## Structuur

- `ictrecht_api/client.py` – `Client` met een methode per endpoint, plus `request()` voor de rest
- `play.py` – CLI om snel dingen te proberen
- `.env` – je token (staat in `.gitignore`)
- `logs/` – API-logs (staat in `.gitignore`)
