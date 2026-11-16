#!/usr/bin/env python3
"""Speeltuin voor de ICTRecht AI proxy API.

Voorbeelden:
    python play.py models
    python play.py prompts
    python play.py functions
    python play.py chat "Wat is een verwerkersovereenkomst?"
    python play.py chat "Leg de AI Act uit" --model <model-id> --stream
    python play.py chat "Leg de AI Act uit" -m 1      # nummer uit models.json
    python play.py chat --interactive
    python play.py raw GET models
    python play.py raw POST chat/completions '{"messages":[{"role":"user","content":"Hoi"}]}'
    python play.py login jij@ictrecht.nl
"""

import argparse
import getpass
import json
import sys

from ictrecht_api import ApiError, Client
from ictrecht_api.client import pretty


def print_table(items, fields, args):
    """Compacte weergave van een Open WebUI-lijst; --json voor de volledige response."""
    if args.json:
        print(pretty(items))
        return
    if isinstance(items, dict):
        items = items.get("data", items)
    if not isinstance(items, list):
        print(pretty(items))
        return
    for item in items:
        print("  ".join(str(item.get(f) or "") for f in fields))
    print(f"\n{len(items)} items", file=sys.stderr)


def cmd_models(client, args):
    data = client.models()
    if args.json or not client.models_file:
        print_table(data, ["id", "name"], args)
        return
    entries = [e for e in client._load_models_file() if e.get("available", True)]
    for e in entries:
        print(f"{e['nr']:>3}  {e['id']}  {e['name']}")
    print(f"\n{len(entries)} modellen, overzicht bijgewerkt in {client.models_file}", file=sys.stderr)


def cmd_prompts(client, args):
    print_table(client.prompts(), ["command", "title"], args)


def cmd_functions(client, args):
    print_table(client.functions(), ["id", "name", "type"], args)


def cmd_login(client, args):
    password = getpass.getpass("Wachtwoord: ")
    result = client.login(args.email, password)
    print(pretty(result))
    print("\nZet het token in .env als ICTRECHT_API_TOKEN=...", file=sys.stderr)


def ask(client, messages, args) -> str:
    if args.stream:
        parts = []
        for text in client.chat_stream(messages, model=args.model, temperature=args.temperature):
            print(text, end="", flush=True)
            parts.append(text)
        print()
        return "".join(parts)

    response = client.chat(messages, model=args.model, temperature=args.temperature)
    if args.json:
        print(pretty(response))
    try:
        answer = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        if not args.json:
            print(pretty(response))
        return ""
    if not args.json:
        print(answer)
    return answer


def cmd_chat(client, args):
    messages = []
    if args.system:
        messages.append({"role": "system", "content": args.system})

    if not args.interactive:
        if not args.message:
            sys.exit("Geef een bericht mee, of gebruik --interactive")
        messages.append({"role": "user", "content": args.message})
        ask(client, messages, args)
        return

    print("Interactieve chat. Leeg bericht of Ctrl-D om te stoppen.\n")
    if args.message:
        messages.append({"role": "user", "content": args.message})
        messages.append({"role": "assistant", "content": ask(client, messages, args)})
    while True:
        try:
            user = input("\n> ").strip()
        except EOFError:
            break
        if not user:
            break
        messages.append({"role": "user", "content": user})
        messages.append({"role": "assistant", "content": ask(client, messages, args)})


def cmd_raw(client, args):
    payload = json.loads(args.body) if args.body else None
    response = client.request(args.method.upper(), args.path, json=payload)
    try:
        print(pretty(response.json()))
    except ValueError:
        print(response.text)


def main():
    parser = argparse.ArgumentParser(description="Speeltuin voor de ICTRecht AI proxy API")
    parser.add_argument("--base-url", help="Overschrijft ICTRECHT_API_BASE_URL")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, func, help in [("models", cmd_models, "Beschikbare modellen"),
                             ("prompts", cmd_prompts, "Beschikbare prompts"),
                             ("functions", cmd_functions, "Beschikbare functions")]:
        p = sub.add_parser(name, help=help)
        p.add_argument("--json", action="store_true", help="Volledige JSON-response tonen")
        p.set_defaults(func=func)

    p = sub.add_parser("login", help="Inloggen met e-mail/wachtwoord (geeft token terug)")
    p.add_argument("email")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("chat", help="Chat completion")
    p.add_argument("message", nargs="?")
    p.add_argument("--model", "-m", help="Model-id of nummer uit models.json (zie: python play.py models)")
    p.add_argument("--system", "-s", help="Systeemprompt")
    p.add_argument("--temperature", "-t", type=float)
    p.add_argument("--stream", action="store_true", help="Antwoord streamen")
    p.add_argument("--json", action="store_true", help="Volledige JSON-response tonen")
    p.add_argument("--interactive", "-i", action="store_true", help="Doorlopend gesprek")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("raw", help="Willekeurige request, bijv. 'raw GET models'")
    p.add_argument("method")
    p.add_argument("path")
    p.add_argument("body", nargs="?", help="JSON-body")
    p.set_defaults(func=cmd_raw)

    args = parser.parse_args()
    client = Client(base_url=args.base_url)
    try:
        args.func(client, args)
    except ApiError as e:
        sys.exit(f"API-fout: {e}")
    except ValueError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
