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
python play.py models                       # GET  /api/models
python play.py prompts                      # GET  /api/v1/prompts
python play.py functions                    # GET  /api/v1/functions
python play.py chat "Wat is de AVG?"        # POST /api/chat/completions
python play.py chat "..." -m <model> --stream
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

## Structuur

- `ictrecht_api/client.py` – `Client` met een methode per endpoint, plus `request()` voor de rest
- `play.py` – CLI om snel dingen te proberen
- `.env` – je token (staat in `.gitignore`)
