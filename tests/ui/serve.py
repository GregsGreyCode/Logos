# Screen checks (tests/ui/run.sh): the full dashboard (gateway/html/app.html + gateway/ui) with fake API data like prod on 22 Nov.
import http.server, sys, os, json, mimetypes, datetime, re
REPO, HERE = sys.argv[1], os.path.dirname(os.path.abspath(__file__))
UI, ASSETS = os.path.join(REPO, 'gateway/ui'), os.path.join(REPO, 'assets')
today = datetime.date.today(); tom = today + datetime.timedelta(days=1)
d, t = today.isoformat(), tom.isoformat()
AG = lambda n, dn, mx, own=True: {'name': n, 'display_name': dn, 'matrix_id': mx, 'running': True, 'model_up': True, 'voice': True, 'shared': n == 'household'}
FAKE = {
  '/api/v2/me': {'username': 'greg', 'display_name': 'Greg', 'role': 'admin', 'matrix_id': '@greg:test', 'look': 'default', 'prefs': {'widgets': ['weather', 'next', 'messages', 'coming', 'lights', 'lists'], 'tips_seen': ['home', 'agents', 'chat', 'notifications', 'routines']}, 'agent_colours': {'greg': '#6d74ff', 'household': '#5fd49a', 'bere': '#a35bd6'}, 'version': {'commit': 'abc1234', 'date': 1795392000}},
  '/api/v2/home': {'me': 'greg', 'agent': {'name': 'greg', 'display_name': "Greg's agent"}, 'model_up': True, 'model_off_since': None, 'brain_changes': [],
    'today': {'date': d, 'busy': [], 'calendars': {'bere': 'Béré', 'household': 'Household'}, 'events': [
      {'id': 'bere/call-maman.ics', 'start': f'{d} 15:00', 'end': f'{d} 15:30', 'title': 'Call Maman', 'calendar': 'bere', 'read_only': True},
      {'start': (datetime.datetime.now() + datetime.timedelta(minutes=40)).strftime('%Y-%m-%d %H:%M'), 'title': 'Dinner with Alex', 'calendar': 'household'},
      {'start': f'{t} 21:00', 'title': 'Alex Bday', 'calendar': 'bere', 'location': 'https://maps.app.goo.gl/abc'},
      {'start': f'{t} 21:00', 'title': 'Alex bday party', 'calendar': 'bere'},
      {'start': f'{t}', 'all_day': True, 'title': "Deborah's birthday", 'calendar': 'bere'},
      {'start': f'{t}', 'all_day': True, 'title': 'Rubbish bins', 'calendar': 'household'}]}},
  '/api/v2/noticeboard': {'waiting': [], 'log': []},
  '/api/v2/agents': {'agents': [AG('household', 'Household agent', '@household-agent:test'), AG('greg', "Greg's agent", '@greg-agent:test')], 'others': [AG('bere', "Béré's agent", '@bere-agent:test')]},
  '/api/v2/activity': {'working': [], 'model_up': True},
  '/api/v2/models/local': {'queue': {'waiting': 0, 'running': 0}, 'speed': {'alone': {'median_tokens_per_s': 16}}},
  '/api/v2/notifications': {'items': [], 'seen_until': 0},
  '/api/v2/agents/household/brain/page': {'text': '## Shopping list\n- [ ] pasta\n- [ ] lemon\n- [ ] something sweet\n## To-do\n- [ ] Drop off giveaway bag at charity shop\n- [ ] a\n- [ ] b\n- [ ] c\n- [ ] d\n## Wedding\n- [ ] Select violin songs & reply to email\n- [ ] x\n- [ ] y\n- [ ] z\n- [ ] w\n- [ ] v\n'},
}
# Settings › House speaker: the real list of voices (gateway/tts.py is stdlib only), and a silent three-second sample.
import importlib.util, io, wave
_spec = importlib.util.spec_from_file_location('tts', os.path.join(REPO, 'gateway/tts.py')); TTS = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(TTS)
FAKE['/api/v2/speaker'] = {'on': True, 'problem': '', 'voices': [{'id': i, 'name': n, 'accent': a, 'who': w, 'lang': TTS.language(i)} for i, (n, a, w) in TTS.VOICES.items()],
  'agents': [{'name': 'household', 'display_name': 'Household agent', 'shared': True, 'speaks': True, 'voice': TTS.DEFAULT_VOICE, 'default': TTS.DEFAULT_VOICE,
              'voice_fr': 'fr_FR-tom-medium', 'default_fr': 'fr_FR-tom-medium', 'set_by': None},
             {'name': 'greg', 'display_name': "Greg's agent", 'shared': False, 'speaks': False, 'voice': TTS.DEFAULT_VOICE, 'default': TTS.DEFAULT_VOICE,
              'voice_fr': 'fr_FR-tom-medium', 'default_fr': 'fr_FR-tom-medium', 'set_by': None}]}
_b = io.BytesIO(); _w = wave.open(_b, 'wb'); _w.setframerate(22050); _w.setsampwidth(2); _w.setnchannels(1); _w.writeframes(b'\0\0' * 66150); _w.close(); SAMPLE = _b.getvalue()
# People (F38): an admin's view with someone invited and someone removed, one person's page, the admin actions.
import time as _time
_NOW = int(_time.time())
PEOPLE = {
  '/api/v2/people/audit': {'actions': [{'at': _NOW - 60, 'action': 'invite_accepted', 'by': 'Ann', 'about': 'ann'},
                                       {'at': _NOW - 3600, 'action': 'person_invited', 'by': 'Greg', 'about': 'Ann'},
                                       {'at': _NOW - 7200, 'action': 'person_removed', 'by': 'Greg', 'about': 'Dan'}]},
  '/api/v2/people/ann': {'username': 'ann', 'display_name': 'Ann', 'is_admin': False, 'is_you': False, 'created_at': (_NOW - 3600) * 1000,
    'last_login': (_NOW - 60) * 1000, 'state': 'active', 'busy': None, 'email': 'ann@example.org', 'removable': True, 'not_removable_because': None,
    'steps': [{'id': 'account', 'label': 'Account', 'state': 'ok', 'detail': 'Signed in for the first time'},
              {'id': 'chat', 'label': 'Chat account (Element X)', 'state': 'ok', 'detail': '@ann:test, with the same password'},
              {'id': 'calendar', 'label': 'Calendar', 'state': 'ok', 'detail': 'Their own calendar and Household, with the same password'},
              {'id': 'agent', 'label': 'Their agent', 'state': 'ok', 'detail': 'Running'},
              {'id': 'household', 'label': 'Household agent', 'state': 'failed', 'detail': 'It did not come back; see Agents'}],
    'visibility': {'own_agent': 'ann', 'uses': [{'name': 'ann', 'display_name': "Ann's agent", 'shared_with': [], 'household': False},
                                                {'name': 'household', 'display_name': 'Household agent', 'shared_with': ['Greg'], 'household': True}],
                   'reach': ['Chats in Element X, only with the people it is set to answer', 'Calendars: their own and Household (read and change), Greg (read only)'],
                   'seen_by': [{'what': "Ann's agent: its chats and its brain", 'who': ['Ann'], 'note': 'Admins get no exception.'},
                               {'what': 'Their own calendar', 'who': ["Ann's agent (read and change)", "Greg's agent (read only)", 'Household agent (busy times only)'], 'note': ''}]}},
  '/api/v2/people/cleo': {'username': 'cleo', 'display_name': 'Cléo', 'is_admin': False, 'is_you': False, 'state': 'invited', 'busy': None, 'steps': [],
    'visibility': {'own_agent': None, 'uses': [], 'reach': [], 'seen_by': []}, 'invite': {'expires_at': _NOW + 6 * 86400, 'options': {}},
    'removable': False, 'not_removable_because': 'Cancel the invitation instead.'},
  '/api/v2/people/dan': {'username': 'dan', 'display_name': 'Dan', 'is_admin': False, 'is_you': False, 'state': 'removed', 'busy': None, 'steps': [],
    'visibility': None, 'removed': {'removed_at': _NOW - 7200, 'delete_after': _NOW + 29 * 86400, 'notes': [], 'agent': 'dan', 'by': 'Greg'}},
}
# Added to the people list (extra.json has Greg and Béré): what only an admin sees.
PEOPLE_ADMIN = {'is_admin': True,
    'invited': [{'username': 'cleo', 'display_name': 'Cléo', 'state': 'invited', 'invite_expires': _NOW + 6 * 86400}],
    'removed': [{'username': 'dan', 'display_name': 'Dan', 'state': 'removed', 'delete_after': _NOW + 29 * 86400}],
    'offers': {'chat': True, 'calendar': True, 'household': True, 'email': True, 'matrix_server': 'test'}}
PEOPLE_POST = {'/api/v2/people': (201, {'username': 'ella', 'token': 'TESTTOKEN-not-a-secret', 'expires_at': _NOW + 7 * 86400}),
               '/auth/invite/check': (200, {'display_name': 'Ella', 'username': 'ella', 'invited_by': 'Greg', 'expires_at': _NOW + 7 * 86400,
                                            'min_password': 10, 'chat_id': '@ella:test', 'calendar': True}),
               '/auth/invite/accept': (200, {'ok': True, 'username': 'ella', 'steps': [{'id': 'account', 'label': 'Account', 'state': 'ok', 'detail': 'Signed in for the first time'},
                                                                                     {'id': 'chat', 'label': 'Chat account (Element X)', 'state': 'ok', 'detail': '@ella:test'}]})}
# The Calendar screen (27 Nov): events around today, so every range asked for has something in it.
def _cal_events():
    D = lambda n: (today + datetime.timedelta(days=n)).isoformat()
    E = lambda i, cal, title, start, end, **kw: {'id': f'{cal}/{i}.ics', 'calendar': cal, 'title': title, 'start': start, 'end': end,
                                               'all_day': len(start) == 10, 'location': '', 'notes': '', 'repeats': False, **kw}
    return [
        E('call-maman', 'bere', 'Call Maman', f'{d} 15:00', f'{d} 15:30', read_only=True),
        E('bins', 'household', 'Rubbish bins', D(0), D(1), repeats=True, notes='Green bin this week.'),
        E('dentist', 'personal', 'Dentist', f'{D(1)} 10:30', f'{D(1)} 11:15', location='12 High Street https://maps.app.goo.gl/abc', notes='Bring the form.'),
        E('standup', 'personal', 'Standup', f'{D(1)} 09:00', f'{D(1)} 09:15'),
        E('lunch', 'household', 'Lunch with Alex', f'{D(1)} 12:30', f'{D(1)} 14:00'),
        E('violin', 'bere', 'Violin lesson', f'{D(1)} 17:00', f'{D(1)} 18:00', read_only=True),
        E('deborah', 'bere', "Deborah's birthday", D(3), D(4), repeats=True, read_only=True),
        E('bath', 'household', 'Weekend in Bath', D(5), D(7), location='Bath'),
        E('yoga', 'personal', 'Yoga', f'{D(-2)} 18:00', f'{D(-2)} 19:00'),
        E('dinner', 'personal', 'Team dinner', f'{D(9)} 19:30', f'{D(9)} 22:00'),
        E('flight', 'bere', 'Flight to Lyon', f'{D(12)} 07:10', f'{D(12)} 09:55', read_only=True),
    ]
def calendar_range(q):
    a, b = q.get('start', [d])[0], q.get('end', [d])[0]
    ev = [e for e in _cal_events() if e['start'][:10] <= b and (e['end'] or e['start'])[:10] >= a]
    return {'start': a, 'end': b, 'today': d, 'agent': 'greg', 'can_change': ['personal', 'household'], 'busy': [], 'events': ev,
            'calendars': [{'key': 'personal', 'name': 'Your own calendar', 'busy_only': False}, {'key': 'household', 'name': 'Household', 'busy_only': False},
                          {'key': 'bere', 'name': 'Béré', 'busy_only': False}]}
EXTRA = json.load(open(os.path.join(HERE, 'extra.json'))) if os.path.exists(os.path.join(HERE, 'extra.json')) else {}
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, body, ctype):
        self.send_response(code); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        p = self.path.split('?')[0]
        if p == '/':
            html = open(os.path.join(REPO, 'gateway/html/app.html')).read()
            html = html.replace('<head>', '<head><script type="importmap">{"imports": {"/ui/mx.js": "/ui/__fake-mx.js", "@matrix-org/matrix-sdk-crypto-wasm": "/ui/vendor/matrix/crypto/index.mjs"}}</script>', 1)
            html = re.sub(r'<link[^>]*fonts\.googleapis[^>]*>', '', html)
            return self.send(200, html.encode(), 'text/html')
        if p == '/sw.js': return self.send(200, open(os.path.join(UI, 'sw.js'), 'rb').read(), 'text/javascript')
        if p == "/ui/__fake-mx.js": return self.send(200, open(os.path.join(HERE, 'fake-mx.js'), 'rb').read(), 'text/javascript')
        # A check slows the API by leaving a file named slow here or in $OUT (run.sh's screenshot folder).
        if p.startswith('/api/') and any(os.path.exists(os.path.join(d, 'slow')) for d in (HERE, os.environ.get('OUT', HERE))): import time as _t; _t.sleep(1.2)
        if p == '/api/v2/speaker/sample': return self.send(200, SAMPLE, 'audio/wav')
        if p == '/invite': return self.send(200, open(os.path.join(REPO, 'gateway/html/invite.html'), 'rb').read(), 'text/html')
        if p == '/api/v2/people': return self.send(200, json.dumps({**EXTRA.get(p, {'people': [], 'agents': []}), **PEOPLE_ADMIN}).encode(), 'application/json')
        if p in PEOPLE: return self.send(200, json.dumps(PEOPLE[p]).encode(), 'application/json')
        if p == '/api/v2/calendar':
            import urllib.parse as _u
            return self.send(200, json.dumps(calendar_range(_u.parse_qs(self.path.partition('?')[2]))).encode(), 'application/json')
        if p in EXTRA: return self.send(200, json.dumps(EXTRA[p]).encode(), 'application/json')
        if p in FAKE: return self.send(200, json.dumps(FAKE[p]).encode(), 'application/json')
        if p.startswith('/api/'): return self.send(200, b'{}', 'application/json')
        base, rest = (UI, p[4:]) if p.startswith('/ui/') else (ASSETS, p[8:]) if p.startswith('/static/') else (None, None)
        f = os.path.join(base, rest) if base else None
        if f and os.path.isfile(f):
            ct = 'text/javascript' if f.endswith(('.js', '.mjs')) else (mimetypes.guess_type(f)[0] or 'application/octet-stream')
            return self.send(200, open(f, 'rb').read(), ct)
        self.send(404, b'', 'text/plain')
    def do_POST(self):
        p = self.path.split('?')[0]
        n = int(self.headers.get('Content-Length') or 0); body = json.loads(self.rfile.read(n) or b'{}') if n else {}
        if p == '/auth/invite/check' and body.get('token') != 'GOODTOKEN': return self.send(410, json.dumps({'error': 'invite_unusable', 'message': 'This invitation link cannot be used.'}).encode(), 'application/json')
        if p in PEOPLE_POST: code, out = PEOPLE_POST[p]; return self.send(code, json.dumps(out).encode(), 'application/json')
        self.send(200, b'{}', 'application/json')
    def do_PUT(self): self.send(200, b'{"routines": {}}', 'application/json')
http.server.ThreadingHTTPServer(('127.0.0.1', int(sys.argv[2])), H).serve_forever()
