// A tapped notification opens "/#/chat/room/…"; with the 15-minute session run out the page goes through
// /login and must come back to the same place, not Home (Béré, 23 Nov). The login page's own helper, read
// from the file (it loads Alpine from a CDN, which a check should not depend on).
import { readFileSync } from 'fs';
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const src = readFileSync(new URL('../../../gateway/html/login.html', import.meta.url), 'utf8');
const fn = src.match(/function keepHash\(\) \{[^\n]*\}/);
ok(!!fn, 'the login page has keepHash');
const keep = (hash) => new Function('location', `${fn[0]}; return keepHash();`)({ hash });
ok(keep('#/chat/room/!hTfw642zHuebo9BSK6_qwVbfQKTf41q6cy2jhntq71Y') === '#/chat/room/!hTfw642zHuebo9BSK6_qwVbfQKTf41q6cy2jhntq71Y', 'a notification\'s chat address is kept');
ok(keep('#/chat/greg/group/%21abc%3Aserver') === '#/chat/greg/group/%21abc%3Aserver', 'an encoded one too');
ok(keep('') === '', 'no address: nothing added');
for (const bad of ['#javascript:alert(1)', '#//evil.example/x', '#/a b', '#/x"onload=1', "#/x'y", '#/<script>']) ok(keep(bad) === '', `not carried over: ${bad}`);
ok((src.match(/'\/' \+ keepHash\(\)/g) || []).length === 3, 'all three ways past the login page keep it (already signed in, signed in, set up)');
