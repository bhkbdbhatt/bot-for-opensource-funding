
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync("D:\\plugin-genesys\\bot-for-genesys-funding\\webapp\\static\\app.js", 'utf8');

function makeEl(tag) {
  return {
    tagName: tag, id: '', className: '', innerHTML: '', textContent: '', title: '',
    value: '', hidden: false,
    classes: new Set(),
    classList: {
      add: (c) => el.classes.add(c),
      remove: (c) => el.classes.delete(c),
      contains: (c) => el.classes.has(c),
    },
    children: [],
    querySelector: () => null,
    focus() {},
    setAttribute() {},
    removeAttribute() {},
  };
}
const store = {};
const el = (id) => { if (!store[id]) { store[id] = makeEl('div'); store[id].id = id; } return store[id]; };
global.document = { getElementById: (id) => store[id] || null, querySelector: () => null, querySelectorAll: () => [], createElement: makeEl, addEventListener() {} }; global.document.addEventListener = global.document.addEventListener || function(){}; global.document.querySelector = global.document.querySelector || function(){return null;};
global.window = { location: { href: '', origin: 'http://127.0.0.1:8911' } };
global.navigator = { clipboard: null };
global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
global.fetch = async () => { throw new Error('no network in this test'); };
global.setInterval = () => 0;
global.clearInterval = () => {};
global.alert = () => {};
global.FormData = function () {};

// Evaluate app.js as a real global script so top-level const/function
// declarations land where the exported probe can reach them.
const EXPORTS = [
  'state', 'renderAuth', 'openSecurity', 'AUTH_SCREENS',
  'authScreenEnrol', 'authScreenRecovery', 'authScreenLogin', 'authScreenSetup',
].join(', ');
try {
  vm.runInThisContext(src + '\n;globalThis.__T = { ' + EXPORTS + ' };');
} catch (e) {
  // init() runs on load and will fail without a server; the bindings above are
  // still bound by that point.
  if (!globalThis.__T) { console.log('FAIL could not evaluate app.js: ' + e.message); process.exit(1); }
}
const G = globalThis.__T;

// Render the screens under test through the real renderAuth().
function render(authenticated, screen) {
  G.state.auth.authenticated = authenticated;
  G.state.auth.screen = screen;
  G.state.auth.email = 'ops@example.com';
  G.state.auth.accountsExist = true;
  G.state.auth.passwordMinLength = 12;
  G.state.auth.totpDigits = 6;
  G.state.auth.recoveryCodeCount = 10;
  G.state.auth.pendingSecret = 'JBSWY3DPEHPK3PXP';
  G.state.auth.pendingUri = 'otpauth://totp/x?secret=JBSWY3DPEHPK3PXP';
  G.state.auth.recoveryCodes = ['aaaa-bbbb', 'cccc-dddd'];
  const root = el('auth-root'), shell = el('app-root');
  G.renderAuth();
  return {
    authVisible: !root.classes.has('hidden'),
    shellVisible: !shell.classes.has('hidden'),
    html: root.innerHTML,
  };
}

const cases = [
  ['not signed in, login',  false, 'login',  true,  false],
  ['not signed in, setup',  false, 'setup',  true,  false],
  ['not signed in, totp',   false, 'totp',   true,  false],
  ['signed in, wizard',     true,  'login',  false, true],
  ['signed in, enrol',      true,  'enrol',  true,  false],
  ['signed in, recovery',   true,  'recovery', true, false],
];

let bad = 0;
for (const [name, authed, screen, wantAuth, wantShell] of cases) {
  const r = render(authed, screen);
  const ok = r.authVisible === wantAuth && r.shellVisible === wantShell;
  console.log((ok ? 'ok   ' : 'FAIL ') + 'renderAuth ' + name +
              ' (auth=' + r.authVisible + ' shell=' + r.shellVisible + ')');
  if (!ok) bad++;
  if (screen === 'enrol' && r.html && !/JBSWY3DPEHPK3PXP/.test(r.html)) {
    console.log('FAIL enrolment screen omits the secret');
    bad++;
  }
  if (screen === 'recovery' && r.html && !/cccc-dddd/.test(r.html)) {
    console.log('FAIL recovery screen omits the codes');
    bad++;
  }
}
process.exit(bad ? 1 : 0);
