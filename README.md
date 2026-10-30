# o2switch-cli

CLI `o2switch` pour piloter le compte cPanel o2switch via l'API UAPI. Python 3.9+, `curl` et `dig`, aucune dépendance. Le transport passe par `curl` car le Python système (LibreSSL 2.8) ne gère pas TLS 1.3, exigé par o2switch. Remplace et étend o2dns, mon ancien script qui ne gérait que les TXT et CNAME.

Lien installé : `~/.local/bin/o2switch` vers `o2switch.py`.

## Garde-fous

- La lecture s'exécute directement.
- Toute écriture affiche ce qui va changer et ne fait rien sans `--apply`.
- Les suppressions (delete, remove, revoke, disable...) exigent en plus `--yes-destructive`.
- Lecture ou écriture est décidée par le champ `x-readonly` de la spec officielle cPanel. Une fonction inconnue est traitée comme une écriture.
- Le DNS et les tokens passent par des sous-commandes dédiées, jamais par `call`.
- Chaque écriture DNS sauvegarde la zone avant, envoie le serial SOA (refus si la zone a bougé), puis relit la zone et vérifie que seul le record visé a changé.
- Le token vit dans le Trousseau macOS (service `o2switch-cli.cpanel`). Il n'apparaît jamais dans les arguments, les fichiers ou la sortie. Les requêtes partent en POST et sans suivre de redirection.
- Les paramètres dont le nom évoque un secret sont masqués dans les aperçus. Pour un mot de passe, utiliser `password=@prompt` (saisie masquée).

## Mise en route

1. Dans cPanel : Sécurité, puis Manage API Tokens. Crée un token avec une expiration.
2. Lance `o2switch configure` et saisis hostname (`<serveur>.o2switch.net`, visible dans cPanel), identifiant cPanel, nom du token, puis le token en saisie masquée.
3. `o2switch whoami` vérifie l'accès.

Ensuite, `o2switch token renew --apply` crée un nouveau token (90 jours par défaut), le vérifie, le range dans le Trousseau et révoque l'ancien. Un avertissement s'affiche quand l'expiration approche (7 jours).

## Exemples

```sh
o2switch dns list example.fr
o2switch dns list example.fr --type MX
o2switch dns set example.fr @ MX "10 mx1.example.net" --add            # aperçu
o2switch dns set example.fr @ TXT "v=spf1 ... ~all" --match v=spf1 --apply
o2switch dns set example.fr dkim._domainkey CNAME dkim._domainkey.example.net --apply
o2switch dns delete example.fr test TXT --apply --yes-destructive

o2switch mail list
o2switch mail forwarders example.fr
o2switch mail forward alias@example.fr moi@example.fr --apply
o2switch mail catchall example.fr
o2switch mail catchall example.fr --set moi@example.fr --apply

o2switch domains | ssl | db | quota

o2switch api search ssl autossl
o2switch api show Email add_pop
o2switch call SSL start_autossl_check --apply
o2switch call Email add_pop email=contact domain=example.fr password=@prompt --apply
```

Types DNS gérés : A, AAAA, CNAME, TXT, MX, SRV, CAA. SOA et NS sont refusés. Quand plusieurs records partagent le même nom et le même type (TXT à la racine), il faut `--match` pour en remplacer un ou `--add` pour en ajouter un.

## Index API

`api-index.json` résume les 693 fonctions UAPI (résumé, paramètres, lecture seule). Pour le régénérer depuis la spec officielle :

```sh
python3 tools/build_index.py
```

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Les tests n'utilisent ni réseau ni token.

## Limites

- Les tâches cron n'existent qu'en API2 côté cPanel, pas encore gérées.
- Les écritures réelles n'ont pas encore été testées sur le serveur avec cette CLI (token expiré au 30/10/2026). Les formats viennent de la spec officielle et d'o2dns, validé en réel en octobre 2026.
