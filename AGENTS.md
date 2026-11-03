# Instructions for AI agents

You are an AI coding agent (Claude Code, Codex, Cursor...) and your user asked you to install or use **o2switch-cli**. This file tells you how. Keep the safety rules at the end: they matter more than the rest.

## What it does

`o2switch` drives the user's o2switch cPanel account through cPanel's UAPI: DNS zones, mailboxes, forwarders, catch-all, SSL, databases, quota, API tokens, plus a generic `call` for any of the 693 UAPI functions. Reads run directly. Every write prints what it would change and does nothing without `--apply`; deletions also need `--yes-destructive`. DNS writes back the zone up first, then re-read it and check that only the targeted record changed.

Requirements: macOS (the API token lives in the Keychain, service `o2switch-cli.cpanel`), Python 3.9+, `curl`, optional `dig`.

## Install

Run, then read the output: every line starts with `✓`, `!` (optional piece missing) or `✗` (blocking).

```sh
curl -fsSL https://raw.githubusercontent.com/pyxl-dev/o2switch-cli/main/install.sh | sh
```

- `✗ python3 is missing` or `✗ curl is missing`: tell the user and give them the command printed. Do not install system tools without asking.
- `! dig not found`: optional, only the public-DNS check after a DNS write is skipped.
- Check: `o2switch --help` prints the usage.

First-time setup is for the **user** to do in their own terminal: `o2switch configure` asks for the cPanel hostname, user, token name and the token (hidden input). The token is created in cPanel under Security, then Manage API Tokens. Then `o2switch whoami` checks access. Never run `configure` with a token you were given in chat.

## Use

```sh
o2switch whoami
o2switch dns list example.fr [--type MX] [--name sub] [--json]
o2switch dns backup example.fr [--out file.json]
o2switch dns set example.fr <name|@> <TYPE> "<value>" [--ttl N] [--add | --match TEXT] [--apply]
o2switch dns delete example.fr <name> <TYPE> [--match TEXT] --apply --yes-destructive
o2switch mail list [--domain d] | forwarders <domain> | forward <alias@d> <dest> [--delete] [--apply] | catchall [<domain>] [--set addr|fail|blackhole --apply]
o2switch domains | ssl | db | quota
o2switch token list | token renew [--days N] [--keep-old] [--apply]
o2switch api search <terms...> | api show <Module> <function>
o2switch call <Module> <function> key=value ... [--apply] [--yes-destructive]
```

- DNS types: A, AAAA, CNAME, TXT, MX, SRV, CAA. SOA and NS are refused. Several records with the same name and type: pass `--match` to replace one or `--add` to add one.
- Find a function with `api search`, read its parameters with `api show`, then `call`. `L` in the search output means read-only, `É` means write or unknown.
- `call` parameters: `key=value`, `key=@prompt` (hidden input), `key=@file:path`. DNS and tokens (`DNS::mass_edit_zone`, `ZoneEdit::*`, `Tokens::*`) are refused in `call`: use `dns` and `token`.
- Zone backups are written to `~/Library/Application Support/o2switch-cli/backups`.
- Tests: `python3 -m unittest discover -s tests -v` (no network, no token).

## Safety rules

- Preview first. Run a write without `--apply`, show the user what it prints, and add `--apply` only once they agree. Add `--yes-destructive` only for a deletion the user explicitly asked for.
- Never ask for, print, store or pass a cPanel token or a password in chat, arguments, files or logs. For passwords use `key=@prompt` so the user types it; do not use a value from chat.
- Do not write to the account unless the user's task requires it, and only for the domains and records they named.
- Do not touch SOA or NS records, and do not work around the guardrails (no direct HTTP calls to cPanel, no editing the index to make a function look read-only).
- If a write fails after the backup was taken, report the backup path to the user and stop. Do not retry blindly.
