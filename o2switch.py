#!/usr/bin/env python3
"""o2switch : pilotage du compte cPanel o2switch via l'API UAPI.

Lecture libre. Toute écriture affiche d'abord ce qui va changer et n'est
exécutée qu'avec --apply. Les suppressions exigent en plus --yes-destructive.
"""
import argparse
import base64
import getpass
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import urllib.parse

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / 'api-index.json'
APP_DIR = Path.home() / 'Library/Application Support/o2switch-cli'
CONFIG = APP_DIR / 'config.json'
BACKUPS = APP_DIR / 'backups'
LEGACY_CONFIG = Path.home() / 'Library/Application Support/o2dns/config.json'
SERVICE = 'o2switch-cli.cpanel'

DESTRUCTIVE = re.compile(r'(delete|remove|revoke|terminate|kill|uninstall|purge|disable|suspend|reset|restore|unset|drop|destroy|wipe|trash|empty|clear)', re.I)
SECRET_PARAM = re.compile(r'(pass|token|secret|key|hash)', re.I)
# Gérés par des sous-commandes dédiées avec sauvegarde et vérification.
RESERVED = {('Tokens', 'create_full_access'), ('Tokens', 'revoke'), ('Tokens', 'rename'),
            ('DNS', 'mass_edit_zone'), ('ZoneEdit', 'add_zone_record'),
            ('ZoneEdit', 'edit_zone_record'), ('ZoneEdit', 'remove_zone_record')}
DNS_TYPES = ('A', 'AAAA', 'CNAME', 'TXT', 'MX', 'SRV', 'CAA')


class Error(Exception):
    pass


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f, indent=2, ensure_ascii=True)
        f.write('\n')


def out(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


def stamp():
    return time.strftime('%Y%m%d-%H%M%S')


# ---------------------------------------------------------------- API

class Client:
    def __init__(self, host, user, token):
        self.base = 'https://' + host + ':2083/execute/'
        self.authorization = 'cpanel ' + user + ':' + token
        self.user = user

    def call(self, module, function, params=None):
        # POST : les paramètres (mots de passe compris) ne finissent pas dans les logs d'URL.
        # curl plutôt qu'urllib : le Python système (LibreSSL 2.8) ne parle pas TLS 1.3, exigé par o2switch.
        # Token et corps passent par stdin (-K -) pour rester hors des arguments du processus.
        # urlencode échappe guillemets et antislashs : le corps est sûr dans une chaîne de config curl.
        body = urllib.parse.urlencode(params or {})
        config = 'header = "Authorization: ' + self.authorization.replace('\\', '\\\\').replace('"', '\\"') + '"\n' + \
                 'data = "' + body + '"\n'
        try:
            p = subprocess.run(['curl', '-sS', '--proto', '=https', '--max-redirs', '0', '-m', '30', '-K', '-',
                                '-w', '\n%{http_code}', self.base + module + '/' + function],
                               input=config, capture_output=True, text=True, timeout=40)
        except (OSError, subprocess.SubprocessError):
            raise Error('curl introuvable ou bloqué.') from None
        if p.returncode:
            raise Error('Connexion cPanel impossible : ' + (p.stderr.strip().splitlines() or ['curl ' + str(p.returncode)])[-1])
        text, _, code = p.stdout.rpartition('\n')
        if code != '200':
            hint = ' (token expiré ou révoqué : relance configure)' if code in ('401', '403') else ''
            raise Error('Erreur HTTP cPanel ' + code + hint)
        try:
            payload = json.loads(text)
        except ValueError:
            raise Error('Réponse cPanel non JSON.') from None
        result = payload.get('result', payload)
        if result.get('status') != 1:
            raise Error('cPanel a refusé ' + module + '::' + function + ' : ' + '; '.join(result.get('errors') or ['raison inconnue']))
        for w in result.get('warnings') or []:
            print('Avertissement cPanel : ' + str(w), file=sys.stderr)
        return result.get('data')


def load_config():
    if not CONFIG.exists():
        raise Error('Configuration absente : lance « o2switch configure ».')
    config = json.loads(CONFIG.read_text())
    if not re.fullmatch(r'[a-z0-9-]+\.o2switch\.net', config.get('host', '')) or not re.fullmatch(r'[a-zA-Z0-9_]+', config.get('user', '')):
        raise Error('Configuration cPanel invalide.')
    return config


def keychain_account(config):
    return config['user'] + '@' + config['host']


def keychain_store(account, token):
    # Interactive security prompt keeps the secret out of process arguments.
    def quote(s):
        return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'
    command = 'add-generic-password -U -s ' + quote(SERVICE) + ' -a ' + quote(account) + ' -w ' + quote(token) + '\n'
    p = subprocess.run(['security', '-i'], input=command, capture_output=True, text=True)
    if p.returncode:
        raise Error('Enregistrement du token dans le Trousseau impossible.')


def get_client():
    config = load_config()
    p = subprocess.run(['security', 'find-generic-password', '-s', SERVICE, '-a', keychain_account(config), '-w'],
                       capture_output=True, text=True)
    if p.returncode:
        raise Error('Token absent du Trousseau : lance « o2switch configure ».')
    expires = int(config['token_expires_at']) if config.get('token_expires_at') else None
    if expires and expires - time.time() < 7 * 86400:
        days = int((expires - time.time()) // 86400)
        print('Attention : le token expire ' + ('dans ' + str(days) + ' jour(s)' if days >= 0 else 'déjà') +
              '. Lance « o2switch token renew --apply ».', file=sys.stderr)
    return Client(config['host'], config['user'], p.stdout.rstrip('\n'))


def update_config(**changes):
    config = load_config()
    config.update(changes)
    temp = CONFIG.with_name('config.' + str(os.getpid()) + '.json')
    save(temp, config)
    temp.replace(CONFIG)


def configure():
    if sys.platform != 'darwin':
        raise Error('Le stockage Trousseau nécessite macOS.')
    default = json.loads(LEGACY_CONFIG.read_text()) if LEGACY_CONFIG.exists() else {}
    def ask(label, fallback):
        value = input(label + (' [' + fallback + ']' if fallback else '') + ' : ').strip()
        return value or fallback
    host = ask('Hostname cPanel (ex. serveur.o2switch.net)', default.get('host', '')).lower()
    if not re.fullmatch(r'[a-z0-9-]+\.o2switch\.net', host):
        raise Error('Hostname o2switch.net attendu, sans URL ni port.')
    user = ask('Identifiant cPanel', default.get('user', ''))
    if not re.fullmatch(r'[a-zA-Z0-9_]+', user):
        raise Error('Identifiant invalide.')
    name = ask('Nom du token dans cPanel (pour suivre son expiration)', '')
    token = getpass.getpass('Token API (saisie masquée) : ')
    if not token or any(c.isspace() for c in token):
        raise Error('Token invalide.')
    client = Client(host, user, token)
    tokens = client.call('Tokens', 'list')
    match = [t for t in tokens if t.get('name') == name]
    keychain_store(user + '@' + host, token)
    APP_DIR.mkdir(parents=True, exist_ok=True)
    temp = CONFIG.with_name('config.' + str(os.getpid()) + '.json')
    save(temp, {'host': host, 'user': user, 'token_name': name or None,
                'token_expires_at': int(match[0]['expires_at']) if match and match[0].get('expires_at') else None})
    temp.replace(CONFIG)
    print('Token valide et enregistré dans le Trousseau (service ' + SERVICE + ').')
    if name and not match:
        print('Nom de token introuvable dans cPanel : expiration non suivie.')


# ---------------------------------------------------------------- Index API

def load_index():
    return json.loads(INDEX.read_text())


def lookup(module, function):
    return load_index().get(module, {}).get(function)


def parse_params(pairs):
    params = {}
    for pair in pairs:
        if '=' not in pair:
            raise Error('Paramètre attendu sous la forme clé=valeur : ' + pair)
        key, value = pair.split('=', 1)
        if value == '@prompt':
            value = getpass.getpass(key + ' (saisie masquée) : ')
        elif value.startswith('@file:'):
            value = Path(value[6:]).expanduser().read_text()
        params[key] = value
    return params


def masked(params):
    return {k: ('***' if SECRET_PARAM.search(k) else v) for k, v in params.items()}


def guarded_call(client, module, function, params, apply=False, destructive_ok=False, reserved_ok=False):
    """Appel UAPI avec garde-fous : lecture directe, écriture seulement avec apply."""
    if (module, function) in RESERVED and not reserved_ok:
        raise Error(module + '::' + function + ' passe par une sous-commande dédiée (dns, token).')
    meta = lookup(module, function)
    readonly = bool(meta and meta.get('readonly') is True)
    if readonly:
        return client.call(module, function, params)
    label = module + '::' + function
    if meta is None:
        print('Fonction absente de l\'index UAPI : traitée comme une écriture.', file=sys.stderr)
    destructive = bool(DESTRUCTIVE.search(function))
    if not apply:
        print('Écriture non exécutée (ajoute --apply) : ' + label + ' ' + json.dumps(masked(params), ensure_ascii=False))
        return None
    if destructive and not destructive_ok:
        raise Error(label + ' est destructif : ajoute --yes-destructive en plus de --apply.')
    return client.call(module, function, params)


# ---------------------------------------------------------------- DNS

def zone_name(value):
    value = value.rstrip('.').lower()
    if not re.fullmatch(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', value):
        raise Error('Nom de domaine invalide : ' + value)
    return value


def owner(zone, name):
    zone = zone_name(zone)
    name = name.rstrip('.').lower()
    if name in ('@', zone):
        return zone + '.'
    if not name.endswith('.' + zone):
        name += '.' + zone
    if len(name) > 253 or any(not re.fullmatch(r'\*|[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?', label) for label in name.split('.')):
        raise Error('Nom DNS invalide : ' + name)
    return name + '.'


def hostname(value):
    # Cible de CNAME/MX/SRV : les labels de service (_domainkey, _dmarc) sont permis.
    value = value.rstrip('.').lower()
    if len(value) > 253 or '.' not in value or any(not re.fullmatch(r'[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9])?', label) for label in value.split('.')):
        raise Error('Nom d\'hôte invalide : ' + value)
    return value + '.'


def decode(value):
    try:
        return base64.b64decode(value, validate=True).decode('utf-8')
    except (ValueError, UnicodeError):
        raise Error('Champ DNS base64 non décodable.')


def records(raw, zone):
    if not isinstance(raw, list):
        raise Error('Format parse_zone inattendu : liste attendue.')
    result = []
    for item in raw:
        if item.get('type') != 'record' and 'record_type' not in item:
            continue
        dname = decode(item['dname_b64']) if 'dname_b64' in item else item.get('dname')
        data = [decode(v) for v in item['data_b64']] if 'data_b64' in item else item.get('data')
        if not isinstance(dname, str) or not isinstance(data, list):
            raise Error('Structure du record DNS non reconnue.')
        if not dname.endswith('.'):
            dname = owner(zone, dname)
        result.append(dict(line_index=item['line_index'], dname=dname.lower(), data=data,
                           ttl=item['ttl'], record_type=item['record_type']))
    return result


def serial(recs):
    soa = [r for r in recs if r['record_type'] == 'SOA']
    if len(soa) != 1 or len(soa[0]['data']) != 7:
        raise Error('SOA non reconnu : écriture refusée.')
    return int(soa[0]['data'][2])


def record_data(rtype, value):
    """Convertit la valeur saisie en champ data cPanel, avec validation."""
    parts = value.split()
    try:
        if rtype == 'A':
            return [str(ipaddress.IPv4Address(value))]
        if rtype == 'AAAA':
            return [str(ipaddress.IPv6Address(value))]
    except ValueError:
        raise Error('Adresse IP invalide : ' + value) from None
    if rtype == 'CNAME':
        return [hostname(value)]
    if rtype == 'TXT':
        if not value or len(value.encode()) > 4096 or not value.isascii() or any(ord(c) < 32 for c in value):
            raise Error('TXT : ASCII imprimable, 4096 octets maximum.')
        return [value[i:i + 255] for i in range(0, len(value), 255)]
    if rtype == 'MX':
        if len(parts) != 2 or not parts[0].isdigit():
            raise Error('MX attendu : "<priorité> <serveur>", ex. "10 mx1.example.net".')
        return [str(int(parts[0])), hostname(parts[1])]
    if rtype == 'SRV':
        if len(parts) != 4 or not all(p.isdigit() for p in parts[:3]):
            raise Error('SRV attendu : "<priorité> <poids> <port> <cible>".')
        return [str(int(p)) for p in parts[:3]] + [hostname(parts[3])]
    if rtype == 'CAA':
        if len(parts) < 3 or not parts[0].isdigit() or parts[1] not in ('issue', 'issuewild', 'iodef'):
            raise Error('CAA attendu : "<flag> issue|issuewild|iodef <valeur>".')
        return [parts[0], parts[1], ' '.join(parts[2:]).strip('"')]
    raise Error('Type non pris en charge : ' + rtype)


def show_value(r):
    return ''.join(r['data']) if r['record_type'] == 'TXT' else ' '.join(r['data'])


def find(recs, fqdn, rtype, match):
    found = [r for r in recs if r['dname'] == fqdn and r['record_type'] == rtype]
    if match:
        found = [r for r in found if match in show_value(r)]
    return found


def dns_plan(recs, zone, name, rtype, value, ttl, add=False, match=None, delete=False):
    """Retourne (opération, record avant, record après) sans rien écrire."""
    fqdn = owner(zone, name)
    rtype = rtype.upper()
    if rtype not in DNS_TYPES:
        raise Error('Type autorisé : ' + ', '.join(DNS_TYPES) + '. SOA et NS restent intouchables.')
    current = find(recs, fqdn, rtype, match)
    if delete:
        if not current:
            raise Error('Aucun record ' + rtype + ' à ' + fqdn + (' contenant « ' + match + ' »' if match else '') + '.')
        if len(current) > 1:
            raise Error('Plusieurs records correspondent : précise --match.')
        return 'remove', current[0], None
    data = record_data(rtype, value)
    if not 60 <= ttl <= 604800:
        raise Error('TTL hors limites (60 à 604800 secondes).')
    others = [r for r in recs if r['dname'] == fqdn and r['record_type'] != rtype]
    if rtype == 'CNAME' and others:
        raise Error('Un autre record existe au nom du CNAME.')
    if any(r['record_type'] == 'CNAME' for r in others):
        raise Error('Un CNAME existe déjà à ce nom.')
    if any(r['data'] == data for r in find(recs, fqdn, rtype, None)):
        raise Error('Record identique déjà présent.')
    new = dict(dname=fqdn, ttl=ttl, record_type=rtype, data=data)
    if add:
        if rtype == 'CNAME' and current:
            raise Error('Un CNAME existe déjà à ce nom.')
        return 'add', None, new
    if len(current) > 1:
        raise Error(str(len(current)) + ' records ' + rtype + ' à ce nom : précise --match pour en remplacer un, ou --add.')
    if current:
        new['line_index'] = current[0]['line_index']
        return 'edit', current[0], new
    return 'add', None, new


def canonical(items):
    return sorted(json.dumps({k: v for k, v in x.items() if k != 'line_index'}, sort_keys=True)
                  for x in items if x['record_type'] != 'SOA')


def dns_change(client, zone, name, rtype, value=None, ttl=3600, add=False, match=None, delete=False,
               apply=False, destructive_ok=False, backup_dir=None):
    zone = zone_name(zone)
    raw = client.call('DNS', 'parse_zone', {'zone': zone})
    recs = records(raw, zone)
    op, before, after = dns_plan(recs, zone, name, rtype, value, ttl, add, match, delete)
    print('Zone ' + zone + ' : ' + op)
    if before:
        print('  - ' + before['dname'] + ' ' + str(before['ttl']) + ' ' + before['record_type'] + ' ' + show_value(before))
    if after:
        print('  + ' + after['dname'] + ' ' + str(after['ttl']) + ' ' + after['record_type'] + ' ' + show_value(after))
    if not apply:
        print('Rien écrit. Ajoute --apply pour appliquer.')
        return None
    if op == 'remove' and not destructive_ok:
        raise Error('Suppression : ajoute --yes-destructive en plus de --apply.')
    backup = Path(backup_dir or BACKUPS) / (zone + '-' + stamp() + '.json')
    save(backup, {'zone': zone, 'records': raw})
    params = {'zone': zone, 'serial': serial(recs)}
    if op == 'remove':
        params['remove-0'] = before['line_index']
    else:
        params[op + '-0'] = json.dumps(after, separators=(',', ':'))
    client.call('DNS', 'mass_edit_zone', params)
    new_recs = records(client.call('DNS', 'parse_zone', {'zone': zone}), zone)
    old_rest = [r for r in recs if r is not before]
    new_rest = list(new_recs)
    if after:
        hit = [r for r in new_recs if r['dname'] == after['dname'] and r['record_type'] == after['record_type']
               and r['data'] == after['data'] and r['ttl'] == after['ttl']]
        if len(hit) != 1:
            raise Error('Écriture faite mais relecture non conforme. Sauvegarde : ' + str(backup))
        new_rest.remove(hit[0])
    if canonical(old_rest) != canonical(new_rest):
        raise Error('Écriture faite mais un autre record a changé. Sauvegarde : ' + str(backup))
    print('Appliqué et vérifié via l\'API. Sauvegarde : ' + str(backup))
    if after:
        try:
            public = dig(after['dname'], after['record_type'])
        except Error:
            public = []
        expected = show_value(after).rstrip('.')
        if any(expected in p.replace('"', '').rstrip('.') for p in public):
            print('Déjà visible dans le DNS public.')
        else:
            print('Pas encore visible dans le DNS public (propagation, TTL ' + str(after['ttl']) + ' s).')
    return after or before


def dig(name, kind):
    try:
        p = subprocess.run(['dig', '+time=3', '+tries=1', '+short', name, kind], capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        raise Error('Requête DNS impossible.')
    lines = [line for line in p.stdout.strip().splitlines() if line and not line.startswith(';;')]
    return [''.join(shlex.split(line)) for line in lines] if kind == 'TXT' else lines


def dns_list(client, zone, name=None, rtype=None):
    zone = zone_name(zone)
    recs = records(client.call('DNS', 'parse_zone', {'zone': zone}), zone)
    if name:
        recs = [r for r in recs if r['dname'] == owner(zone, name)]
    if rtype:
        recs = [r for r in recs if r['record_type'] == rtype.upper()]
    return recs


# ---------------------------------------------------------------- Commandes

def cmd_whoami(client, args):
    info = client.call('Variables', 'get_user_information', {})
    keep = ('user', 'domain', 'contact_email', 'plan', 'disk_block_limit', 'created', 'last_modified')
    out({k: info.get(k) for k in keep if k in info} if isinstance(info, dict) else info)
    config = load_config()
    expires = int(config['token_expires_at']) if config.get('token_expires_at') else None
    print('Token : ' + (config.get('token_name') or 'nom inconnu') + ', expire ' +
          (time.strftime('%Y-%m-%d %H:%M', time.localtime(expires)) if expires else 'jamais ou inconnu'))


def cmd_token(client, args):
    if args.action == 'list':
        rows = client.call('Tokens', 'list')
        for t in rows:
            # cPanel renvoie parfois les timestamps en chaîne.
            exp = int(t['expires_at']) if t.get('expires_at') else None
            print(t.get('name') + '  expire ' + (time.strftime('%Y-%m-%d', time.localtime(exp)) if exp else 'jamais') +
                  ('  lecture seule' if t.get('readonly') else ''))
        return
    # renew
    config = load_config()
    name = 'o2switch-cli-' + stamp()
    expires_at = int(time.time()) + args.days * 86400
    old = config.get('token_name')
    print('Créer ' + name + ' (expire ' + time.strftime('%Y-%m-%d', time.localtime(expires_at)) + ')' +
          (', puis révoquer ' + old if old and not args.keep_old else ''))
    if not args.apply:
        print('Rien écrit. Ajoute --apply pour appliquer.')
        return
    data = client.call('Tokens', 'create_full_access', {'name': name, 'expires_at': expires_at})
    token = data['token']
    Client(config['host'], config['user'], token).call('Tokens', 'list')
    keychain_store(keychain_account(config), token)
    update_config(token_name=name, token_expires_at=expires_at)
    print('Nouveau token vérifié et enregistré dans le Trousseau.')
    if old and not args.keep_old:
        Client(config['host'], config['user'], token).call('Tokens', 'revoke', {'name': old})
        print('Ancien token révoqué : ' + old)


def cmd_api(client, args):
    index = load_index()
    if args.action == 'search':
        terms = [t.lower() for t in args.terms]
        for module in sorted(index):
            for function, meta in sorted(index[module].items()):
                text = (module + ' ' + function + ' ' + meta['summary']).lower()
                if all(t in text for t in terms):
                    flag = 'L' if meta['readonly'] is True else 'É'
                    print(flag + ' ' + module + '::' + function + '  ' + meta['summary'])
        print('(L = lecture, É = écriture ou inconnu)', file=sys.stderr)
        return
    meta = index.get(args.module, {}).get(args.function)
    if not meta:
        raise Error('Fonction inconnue dans l\'index : ' + args.module + '::' + args.function)
    out(meta)


def cmd_call(client, args):
    params = parse_params(args.params)
    data = guarded_call(client, args.module, args.function, params, args.apply, args.yes_destructive)
    if data is not None:
        out(data)


def cmd_dns(client, args):
    if args.action == 'list':
        recs = dns_list(client, args.zone, args.name, args.type)
        if args.json:
            out(recs)
        else:
            for r in recs:
                print(r['dname'] + '  ' + str(r['ttl']) + '  ' + r['record_type'] + '  ' + show_value(r))
    elif args.action == 'backup':
        z = zone_name(args.zone)
        path = Path(args.out) if args.out else BACKUPS / (z + '-' + stamp() + '.json')
        save(path, {'zone': z, 'records': client.call('DNS', 'parse_zone', {'zone': z})})
        print('Sauvegarde : ' + str(path))
    elif args.action == 'set':
        dns_change(client, args.zone, args.name, args.type, ' '.join(args.value), args.ttl, args.add, args.match,
                   apply=args.apply)
    elif args.action == 'delete':
        dns_change(client, args.zone, args.name, args.type, match=args.match, delete=True,
                   apply=args.apply, destructive_ok=args.yes_destructive)


def cmd_mail(client, args):
    if args.action == 'list':
        params = {'domain': args.domain} if args.domain else {}
        for m in client.call('Email', 'list_pops_with_disk', params):
            print(m.get('email') + '  ' + str(m.get('humandiskused', '')) + ' / ' + str(m.get('humandiskquota', '')))
    elif args.action == 'forwarders':
        for f in client.call('Email', 'list_forwarders', {'domain': args.domain}):
            print(str(f.get('dest')) + '  ->  ' + str(f.get('forward')))
    elif args.action == 'forward':
        email = args.address
        if '@' not in email:
            raise Error('Adresse complète attendue : alias@domaine.')
        local, domain = email.split('@', 1)
        if args.delete:
            data = guarded_call(client, 'Email', 'delete_forwarder', {'address': email, 'forwarder': args.dest},
                                args.apply, args.yes_destructive)
        else:
            data = guarded_call(client, 'Email', 'add_forwarder',
                                {'domain': domain, 'email': local, 'fwdopt': 'fwd', 'fwdemail': args.dest}, args.apply)
        if data is not None:
            print('Fait.')
    elif args.action == 'catchall':
        if not args.set:
            params = {'user': load_config()['user']}
            if args.domain:
                params['domain'] = args.domain
            out(client.call('Email', 'list_default_address', params))
            return
        params = {'domain': args.domain}
        if args.set in ('fail', 'blackhole'):
            params['fwdopt'] = args.set
            if args.set == 'fail':
                params['failmsgs'] = 'No such person at this address'
        elif '@' in args.set:
            params.update(fwdopt='fwd', fwdemail=args.set)
        else:
            raise Error('--set attend une adresse mail, fail ou blackhole.')
        if guarded_call(client, 'Email', 'set_default_address', params, args.apply) is not None:
            print('Catch-all de ' + args.domain + ' : ' + args.set)


def cmd_simple(client, args):
    module, function = {
        'domains': ('DomainInfo', 'list_domains'),
        'ssl': ('SSL', 'installed_hosts'),
        'db': ('Mysql', 'list_databases'),
        'quota': ('Quota', 'get_quota_info'),
    }[args.command]
    out(client.call(module, function, {}))


def build_parser():
    p = argparse.ArgumentParser(prog='o2switch', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='command', required=True)

    sub.add_parser('configure', help='Enregistrer hostname, identifiant et token (Trousseau)')
    sub.add_parser('whoami', help='Compte et expiration du token')

    t = sub.add_parser('token', help='Tokens API')
    t.add_argument('action', choices=['list', 'renew'])
    t.add_argument('--days', type=int, default=90)
    t.add_argument('--keep-old', action='store_true', help='Ne pas révoquer l\'ancien token')
    t.add_argument('--apply', action='store_true')

    a = sub.add_parser('api', help='Explorer les 693 fonctions UAPI')
    asub = a.add_subparsers(dest='action', required=True)
    s = asub.add_parser('search'); s.add_argument('terms', nargs='+')
    s = asub.add_parser('show'); s.add_argument('module'); s.add_argument('function')

    c = sub.add_parser('call', help='Appel UAPI générique : Module fonction clé=valeur')
    c.add_argument('module'); c.add_argument('function'); c.add_argument('params', nargs='*',
                   help='clé=valeur, clé=@prompt (saisie masquée), clé=@file:chemin')
    c.add_argument('--apply', action='store_true'); c.add_argument('--yes-destructive', action='store_true')

    d = sub.add_parser('dns', help='Zone DNS')
    dsub = d.add_subparsers(dest='action', required=True)
    s = dsub.add_parser('list'); s.add_argument('zone'); s.add_argument('--name'); s.add_argument('--type'); s.add_argument('--json', action='store_true')
    s = dsub.add_parser('backup'); s.add_argument('zone'); s.add_argument('--out')
    s = dsub.add_parser('set', help='Créer ou remplacer un record')
    s.add_argument('zone'); s.add_argument('name'); s.add_argument('type', type=str.upper); s.add_argument('value', nargs='+')
    s.add_argument('--ttl', type=int, default=3600)
    s.add_argument('--add', action='store_true', help='Ajouter sans remplacer les records existants')
    s.add_argument('--match', help='Remplacer le record dont la valeur contient ce texte')
    s.add_argument('--apply', action='store_true')
    s = dsub.add_parser('delete'); s.add_argument('zone'); s.add_argument('name'); s.add_argument('type', type=str.upper)
    s.add_argument('--match'); s.add_argument('--apply', action='store_true'); s.add_argument('--yes-destructive', action='store_true')

    m = sub.add_parser('mail', help='Boîtes, redirections, catch-all')
    msub = m.add_subparsers(dest='action', required=True)
    s = msub.add_parser('list'); s.add_argument('--domain')
    s = msub.add_parser('forwarders'); s.add_argument('domain')
    s = msub.add_parser('forward'); s.add_argument('address'); s.add_argument('dest')
    s.add_argument('--delete', action='store_true'); s.add_argument('--apply', action='store_true'); s.add_argument('--yes-destructive', action='store_true')
    s = msub.add_parser('catchall'); s.add_argument('domain', nargs='?')
    s.add_argument('--set', help='adresse@domaine, fail ou blackhole'); s.add_argument('--apply', action='store_true')

    for name, text in (('domains', 'Domaines du compte'), ('ssl', 'Certificats installés'),
                       ('db', 'Bases MySQL'), ('quota', 'Quota disque')):
        sub.add_parser(name, help=text)
    return p


def main(argv=None, client=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == 'configure':
            configure()
            return 0
        if args.command == 'api':
            cmd_api(None, args)
            return 0
        if args.command == 'mail' and args.action == 'catchall' and args.set and not args.domain:
            raise Error('Précise le domaine pour --set.')
        client = client or get_client()
        handler = {'whoami': cmd_whoami, 'token': cmd_token, 'call': cmd_call, 'dns': cmd_dns,
                   'mail': cmd_mail}.get(args.command, cmd_simple)
        handler(client, args)
    except Error as e:
        print('Erreur : ' + str(e), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError) as e:
        print('Erreur : ' + type(e).__name__ + ' : ' + str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
