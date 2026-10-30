import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('o2switch', ROOT / 'o2switch.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


def zone():
    return [dict(type='record', line_index=0, dname='example.com.', ttl=3600, record_type='SOA', data=['ns1.example.com.', 'hostmaster.example.com.', '100', '3600', '600', '86400', '300']),
            dict(type='record', line_index=1, dname='_dmarc', ttl=3600, record_type='TXT', data=['v=DMARC1; p=none;']),
            dict(type='record', line_index=2, dname='example.com.', ttl=3600, record_type='MX', data=['0', 'mail.example.com.']),
            dict(type='record', line_index=3, dname='example.com.', ttl=3600, record_type='TXT', data=['v=spf1 +a +mx ~all']),
            dict(type='record', line_index=4, dname='example.com.', ttl=3600, record_type='TXT', data=['brevo-code:abc'])]


class FakeClient:
    """Simule parse_zone / mass_edit_zone et enregistre les autres appels."""
    def __init__(self):
        self.zone = zone()
        self.calls = []

    def call(self, module, function, params=None):
        params = params or {}
        self.calls.append((module, function, params))
        if (module, function) == ('DNS', 'parse_zone'):
            return copy.deepcopy(self.zone)
        if (module, function) == ('DNS', 'mass_edit_zone'):
            soa = self.zone[0]
            if int(params['serial']) != int(soa['data'][2]):
                raise m.Error('serial')
            for key, value in params.items():
                if key.startswith('add-'):
                    r = dict(json.loads(value), type='record', line_index=max(x['line_index'] for x in self.zone) + 1)
                    self.zone.append(r)
                elif key.startswith('edit-'):
                    r = dict(json.loads(value), type='record')
                    i = next(i for i, x in enumerate(self.zone) if x['line_index'] == r['line_index'])
                    self.zone[i] = r
                elif key.startswith('remove-'):
                    self.zone = [x for x in self.zone if x['line_index'] != int(value)]
            soa['data'][2] = str(int(soa['data'][2]) + 1)
            return {'new_serial': soa['data'][2]}
        return {'ok': True}


def quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()) as buf, contextlib.redirect_stderr(io.StringIO()):
        result = fn(*a, **k)
    return result, buf.getvalue()


class DnsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.c = FakeClient()
        m.dig = lambda name, kind: []

    def change(self, *a, **k):
        k.setdefault('backup_dir', self.tmp.name)
        return quiet(m.dns_change, self.c, *a, **k)

    def writes(self):
        return [c for c in self.c.calls if c[1] == 'mass_edit_zone']

    def test_dry_run_writes_nothing(self):
        _, text = self.change('example.com', '@', 'MX', '10 mx1.example.net', add=True)
        self.assertEqual(self.writes(), [])
        self.assertIn('--apply', text)

    def test_add_mx_keeps_other_records(self):
        self.change('example.com', '@', 'MX', '10 mx1.example.net', add=True, apply=True)
        mx = [r for r in self.c.zone if r['record_type'] == 'MX']
        self.assertEqual(len(mx), 2)
        self.assertIn(['10', 'mx1.example.net.'], [r['data'] for r in mx])
        self.assertEqual(len(list(Path(self.tmp.name).iterdir())), 1)

    def test_replace_single_mx(self):
        self.change('example.com', '@', 'MX', '10 mx1.example.net', apply=True)
        self.assertEqual([r['data'] for r in self.c.zone if r['record_type'] == 'MX'], [['10', 'mx1.example.net.']])

    def test_ambiguous_root_txt_refused(self):
        with self.assertRaisesRegex(m.Error, '--match'):
            self.change('example.com', '@', 'TXT', 'v=spf1 include:x ~all', apply=True)
        self.assertEqual(self.writes(), [])

    def test_match_replaces_spf_only(self):
        self.change('example.com', '@', 'TXT', 'v=spf1 +a +mx include:simplelogin.co ~all', match='v=spf1', apply=True)
        txt = sorted(''.join(r['data']) for r in self.c.zone if r['record_type'] == 'TXT' and r['dname'] == 'example.com.')
        self.assertEqual(txt, ['brevo-code:abc', 'v=spf1 +a +mx include:simplelogin.co ~all'])

    def test_relative_name_normalized(self):
        self.change('example.com', '_dmarc', 'TXT', 'v=DMARC1; p=quarantine;', apply=True)
        self.assertEqual([r['data'] for r in self.c.zone if r['dname'] == '_dmarc.example.com.'], [['v=DMARC1; p=quarantine;']])

    def test_cname_conflict(self):
        with self.assertRaisesRegex(m.Error, 'CNAME'):
            self.change('example.com', '@', 'CNAME', 'other.example.net')

    def test_delete_needs_destructive_flag(self):
        with self.assertRaisesRegex(m.Error, 'yes-destructive'):
            self.change('example.com', '_dmarc', 'TXT', delete=True, apply=True)
        self.change('example.com', '_dmarc', 'TXT', delete=True, apply=True, destructive_ok=True)
        self.assertFalse(any(r['dname'] == '_dmarc' for r in self.c.zone))
        self.assertEqual(len(self.c.zone), 4)

    def test_soa_ns_refused(self):
        with self.assertRaisesRegex(m.Error, 'Type autorisé'):
            self.change('example.com', '@', 'NS', 'ns3.example.net')

    def test_identical_refused(self):
        with self.assertRaisesRegex(m.Error, 'identique'):
            self.change('example.com', '@', 'MX', '0 mail.example.com', add=True)

    def test_validation(self):
        for rtype, value in (('A', '1.2.3'), ('MX', 'mx1.example.net'), ('AAAA', '1.2.3.4'), ('TXT', 'é')):
            with self.assertRaises(m.Error):
                m.record_data(rtype, value)
        self.assertEqual(m.record_data('TXT', 'a' * 300), ['a' * 255, 'a' * 45])
        self.assertEqual(m.record_data('CNAME', 'dkim._domainkey.example.net'), ['dkim._domainkey.example.net.'])
        with self.assertRaises(m.Error):
            m.record_data('CNAME', 'bad..host')
        self.assertEqual(m.record_data('CAA', '0 issue "letsencrypt.org"'), ['0', 'issue', 'letsencrypt.org'])


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.c = FakeClient()

    def test_readonly_runs(self):
        self.assertEqual(m.guarded_call(self.c, 'Email', 'list_forwarders', {'domain': 'x.fr'}), {'ok': True})

    def test_write_needs_apply(self):
        result, text = quiet(m.guarded_call, self.c, 'Email', 'add_forwarder', {'domain': 'x.fr'})
        self.assertIsNone(result)
        self.assertEqual(self.c.calls, [])
        self.assertIn('--apply', text)

    def test_destructive_needs_flag(self):
        with self.assertRaisesRegex(m.Error, 'yes-destructive'):
            m.guarded_call(self.c, 'Email', 'delete_pop', {'email': 'a'}, apply=True)
        m.guarded_call(self.c, 'Email', 'delete_pop', {'email': 'a'}, apply=True, destructive_ok=True)
        self.assertEqual(len(self.c.calls), 1)

    def test_unknown_function_is_write(self):
        result, _ = quiet(m.guarded_call, self.c, 'Nope', 'list_things', {})
        self.assertIsNone(result)
        self.assertEqual(self.c.calls, [])

    def test_reserved_refused(self):
        for module, function in (('DNS', 'mass_edit_zone'), ('Tokens', 'create_full_access')):
            with self.assertRaises(m.Error):
                m.guarded_call(self.c, module, function, {}, apply=True, destructive_ok=True)

    def test_secrets_masked(self):
        _, text = quiet(m.guarded_call, self.c, 'Email', 'add_pop', {'email': 'a', 'password': 'hunter2'})
        self.assertNotIn('hunter2', text)

    def test_cli_catchall_dry_run(self):
        _, text = quiet(m.main, ['mail', 'catchall', 'x.fr', '--set', 'moi@x.fr'], client=self.c)
        self.assertEqual(self.c.calls, [])
        self.assertIn('set_default_address', text)


if __name__ == '__main__':
    unittest.main()
