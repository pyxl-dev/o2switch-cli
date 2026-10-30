#!/usr/bin/env python3
"""Génère api-index.json à partir de la spec OpenAPI UAPI officielle de cPanel.

Usage : python3 tools/build_index.py [chemin/cpanel.openapi.json]
Sans argument, télécharge https://api.docs.cpanel.net/_spec/specifications/cpanel.openapi.json
"""
import json
from pathlib import Path
import sys
import urllib.request

SPEC_URL = 'https://api.docs.cpanel.net/_spec/specifications/cpanel.openapi.json'
OUT = Path(__file__).resolve().parents[1] / 'api-index.json'


def short(text, limit=200):
    text = ' '.join((text or '').split())
    return text if len(text) <= limit else text[:limit - 3] + '...'


def main():
    if len(sys.argv) > 1:
        spec = json.loads(Path(sys.argv[1]).read_text())
    else:
        with urllib.request.urlopen(SPEC_URL, timeout=60) as response:
            spec = json.load(response)
    index = {}
    for path, methods in spec['paths'].items():
        parts = path.strip('/').split('/')
        if len(parts) != 2:
            continue
        module, function = parts
        op = next(iter(methods.values()))
        params = []
        for p in op.get('parameters', []):
            if 'name' not in p:
                continue
            schema = p.get('schema', {})
            entry = {'name': p['name'], 'required': bool(p.get('required')), 'type': schema.get('type', '')}
            if schema.get('enum'):
                entry['enum'] = schema['enum']
            entry['description'] = short(p.get('description'))
            params.append(entry)
        index.setdefault(module, {})[function] = {
            'summary': short(op.get('summary'), 120),
            # x-readonly absent : inconnu, traité comme une écriture par la CLI.
            'readonly': op.get('x-readonly'),
            'params': params,
        }
    OUT.write_text(json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n')
    print(str(sum(len(v) for v in index.values())) + ' fonctions, ' + str(len(index)) + ' modules -> ' + str(OUT))


if __name__ == '__main__':
    main()
