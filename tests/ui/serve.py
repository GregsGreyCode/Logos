# Screen checks (tests/ui/run.sh): the full dashboard (gateway/html/app.html + gateway/ui) with fake API data like prod on 22 Nov.
import http.server, sys, os, json, mimetypes, datetime, re
REPO, HERE = sys.argv[1], os.path.dirname(os.path.abspath(__file__))
UI, ASSETS = os.path.join(REPO, 'gateway/ui'), os.path.join(REPO, 'assets')
today = datetime.date.today(); tom = today + datetime.timedelta(days=1)
d, t = today.isoformat(), tom.isoformat()
AG = lambda n, dn, mx, own=True: {'name': n, 'display_name': dn, 'matrix_id': mx, 'running': True, 'model_up': True, 'voice': True, 'shared': n == 'household'}
FAKE = {
  '/api/v2/me': {'username': 'greg', 'display_name': 'Greg', 'role': 'admin', 'matrix_id': '@greg:test', 'look': 'default', 'prefs': {'widgets': ['weather', 'next', 'messages', 'coming', 'lights', 'lists'], 'tips_seen': ['home', 'agents', 'chat', 'notifications', 'routines']}, 'agent_colours': {'greg': '#6d74ff', 'household': '#5fd49a', 'bere': '#a35bd6'}},
  '/api/v2/home': {'me': 'greg', 'agent': {'name': 'greg', 'display_name': "Greg's agent"}, 'model_up': True, 'model_off_since': None, 'brain_changes': [],
    'today': {'date': d, 'busy': [], 'calendars': {'bere': 'Béré', 'household': 'Household'}, 'events': [
      {'start': f'{d} 15:00', 'title': 'Call Maman', 'calendar': 'bere'},
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
EXTRA = json.load(open(os.path.join(HERE, 'extra.json'))) if os.path.exists(os.path.join(HERE, 'extra.json')) else {}
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, body, ctype):
        self.send_response(code); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        p = self.path.split('?')[0]
        if p == '/':
            html = open(os.path.join(REPO, 'gateway/html/app.html')).read()
            html = html.replace('<head>', '<head><script type="importmap">{"imports": {"/ui/mx.js": "/fake/mx.js", "@matrix-org/matrix-sdk-crypto-wasm": "/ui/vendor/matrix/crypto/index.mjs"}}</script>', 1)
            html = re.sub(r'<link[^>]*fonts\.googleapis[^>]*>', '', html)
            return self.send(200, html.encode(), 'text/html')
        if p == '/sw.js': return self.send(200, open(os.path.join(UI, 'sw.js'), 'rb').read(), 'text/javascript')
        if p == '/fake/mx.js': return self.send(200, open(os.path.join(HERE, 'fake-mx.js'), 'rb').read(), 'text/javascript')
        # A check slows the API by leaving a file named slow here or in $OUT (run.sh's screenshot folder).
        if p.startswith('/api/') and any(os.path.exists(os.path.join(d, 'slow')) for d in (HERE, os.environ.get('OUT', HERE))): import time as _t; _t.sleep(1.2)
        if p in EXTRA: return self.send(200, json.dumps(EXTRA[p]).encode(), 'application/json')
        if p in FAKE: return self.send(200, json.dumps(FAKE[p]).encode(), 'application/json')
        if p.startswith('/api/'): return self.send(200, b'{}', 'application/json')
        base, rest = (UI, p[4:]) if p.startswith('/ui/') else (ASSETS, p[8:]) if p.startswith('/static/') else (None, None)
        f = os.path.join(base, rest) if base else None
        if f and os.path.isfile(f):
            ct = 'text/javascript' if f.endswith(('.js', '.mjs')) else (mimetypes.guess_type(f)[0] or 'application/octet-stream')
            return self.send(200, open(f, 'rb').read(), ct)
        self.send(404, b'', 'text/plain')
    def do_POST(self): self.send(200, b'{}', 'application/json')
    def do_PUT(self): self.send(200, b'{"routines": {}}', 'application/json')
http.server.ThreadingHTTPServer(('127.0.0.1', int(sys.argv[2])), H).serve_forever()
