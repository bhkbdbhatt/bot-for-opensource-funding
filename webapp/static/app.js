'use strict';

/* ------------------------------------------------------------------ setup */

const tokenMeta = document.querySelector('meta[name="web-token"]');
const TOKEN = tokenMeta ? tokenMeta.content : 'dummy-web-token';

const STEPS = [
  { id: 1, title: 'Project', sub: 'What are we promoting?' },
  { id: 2, title: 'GitHub', sub: 'Connect and choose search terms' },
  { id: 3, title: 'Discover', sub: 'Find related projects' },
  { id: 4, title: 'Review', sub: 'Approve who to contact' },
  { id: 5, title: 'Outreach', sub: 'Preview and send' },
  { id: 6, title: 'Dashboard', sub: 'Pipeline status' },
];

const state = {
  version: '',
  presets: [],
  profiles: [],
  channels: ['email', 'forum'],
  statuses: ['new', 'contacted', 'replied', 'sponsored', 'failed'],
  profileId: null,
  profile: null,
  draft: null,
  step: 1,
  dirty: false,
  statusInfo: null,
  sponsors: null,
  github: { connected: false, token_set: false, token_env: 'GITHUB_TOKEN', api_base: 'https://api.github.com' },
  candidates: [],
  candidateQuery: '',
  selected: new Set(),
  approveChannel: 'email',
  sendSelected: new Set(),
  sendDryRun: true,
  sendUseLlm: false,
  job: null,
  jobKind: '',
  jobLogs: [],
  jobLogCount: 0,
  sendResult: null,
  auth: {
    authenticated: false,
    accountsExist: false,
    user: null,
    passwordMinLength: 12,
    totpDigits: 6,
    recoveryCodeCount: 10,
    screen: 'login',
    email: '',
    challenge: '',
    pendingSecret: '',
    pendingUri: '',
    recoveryCodes: [],
  },
};

/* ---------------------------------------------------------------- helpers */

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

function esc(value) {
  return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function deepClone(value) {
  return JSON.parse(JSON.stringify(value === undefined ? null : value));
}

function getPath(obj, path, fallback) {
  if (!obj) return fallback;
  let node = obj;
  for (const part of path.split('.')) {
    if (node === null || typeof node !== 'object' || !(part in node)) return fallback;
    node = node[part];
  }
  return node === undefined ? fallback : node;
}

function setPath(obj, path, value) {
  const parts = path.split('.');
  let node = obj;
  for (let i = 0; i < parts.length - 1; i += 1) {
    if (node[parts[i]] === null || typeof node[parts[i]] !== 'object') node[parts[i]] = {};
    node = node[parts[i]];
  }
  node[parts[parts.length - 1]] = value;
}

function fmtDate(value) {
  if (!value) return '';
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  if (isNaN(date.getTime())) return String(value);
  return date.toLocaleString();
}

function truncate(value, n) {
  const text = String(value || '');
  return text.length > n ? text.slice(0, n - 1) + '\u2026' : text;
}

async function api(method, path, body) {
  const options = {
    method,
    credentials: 'same-origin',
    headers: { 'X-Web-Token': TOKEN },
  };
  if (body !== undefined && body !== null) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data = null;
  try { data = await response.json(); } catch (err) { data = null; }
  if (!response.ok) {
    if (response.status === 401 && !path.startsWith('/api/auth/')) {
      state.auth.authenticated = false;
      state.auth.user = null;
      state.auth.screen = state.auth.accountsExist ? 'login' : 'setup';
      renderAuth();
    }
    const message = (data && data.error) || (response.status + ' ' + response.statusText);
    throw new Error(message);
  }
  return data;
}

function toast(message, type) {
  const root = $('#toasts');
  const node = document.createElement('div');
  node.className = 'toast ' + (type || '');
  node.textContent = message;
  root.appendChild(node);
  setTimeout(() => {
    node.style.opacity = '0';
    node.style.transition = 'opacity .3s';
    setTimeout(() => node.remove(), 320);
  }, type === 'error' ? 7000 : 4000);
}

/* ------------------------------------------------------------------ modal */

function openModal(html) {
  const root = $('#modal-root');
  if (!root) return;
  root.innerHTML = '<div class="modal">' + html + '</div>';
  root.classList.remove('hidden');
}

function closeModal() {
  const root = $('#modal-root');
  if (!root) return;
  root.classList.add('hidden');
  root.innerHTML = '';
}

const modalRoot = $('#modal-root');
if (modalRoot && modalRoot.addEventListener) {
  modalRoot.addEventListener('click', (event) => {
    if (event.target === modalRoot || (event.target && event.target.dataset && event.target.dataset.action === 'close-modal')) {
      closeModal();
    }
  });
}
if (typeof document !== 'undefined' && document.addEventListener) {
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      closeModal();
    }
  });
}

/* -------------------------------------------------------------------- auth */

function authError(message) {
  const box = $('#auth-error');
  if (box) {
    box.textContent = message;
    box.classList.remove('hidden');
  }
}

function clearAuthError() {
  const box = $('#auth-error');
  if (box) {
    box.textContent = '';
    box.classList.add('hidden');
  }
}

function authCard(inner) {
  return '<div class="auth-card">' +
    '<div class="auth-brand"><span class="brand-mark">OW</span>' +
    '<div><div class="brand-name">Outreach Wizard</div>' +
    '<div class="brand-sub">Email, password and two-factor</div></div></div>' +
    inner +
    '<div class="auth-error hidden" id="auth-error" role="alert"></div>' +
    '</div>';
}

function authScreenLogin() {
  return authCard(
    '<h2>Sign in</h2>' +
    '<p class="hint">This wizard can send outreach on your behalf, so every request needs an account.</p>' +
    '<label class="field"><span>Email</span>' +
    '<input type="email" id="auth-email" autocomplete="username" spellcheck="false" value="' + esc(state.auth.email) + '"></label>' +
    '<label class="field"><span>Password</span>' +
    '<input type="password" id="auth-password" autocomplete="current-password"></label>' +
    '<div class="actions"><button class="primary wide" data-action="auth-login">Sign in</button></div>');
}

function authScreenSetup() {
  const min = state.auth.passwordMinLength;
  return authCard(
    '<h2>Create the first account</h2>' +
    '<p class="hint">Stored in <code>users.json</code> beside the config. Passwords are kept as salted PBKDF2-SHA256 hashes and peppered with a key from <code>OUTREACH_AUTH_SECRET</code> - the plaintext is never written down.</p>' +
    '<label class="field"><span>Email</span>' +
    '<input type="email" id="auth-email" autocomplete="username" spellcheck="false"></label>' +
    '<label class="field"><span>Password</span>' +
    '<input type="password" id="auth-password" autocomplete="new-password">' +
    '<span class="small muted">At least ' + min + ' characters, mixing three of: lowercase, uppercase, digits, symbols.</span></label>' +
    '<label class="field"><span>Confirm password</span>' +
    '<input type="password" id="auth-confirm" autocomplete="new-password"></label>' +
    '<div class="actions"><button class="primary wide" data-action="auth-setup">Create account</button></div>' +
    '<p class="hint mt">Once signed in, switch on two-factor from Security &amp; 2FA.</p>');
}

function authScreenTotp() {
  const digits = state.auth.totpDigits;
  return authCard(
    '<h2>Two-factor check</h2>' +
    '<p class="hint">Enter the ' + digits + '-digit code from your authenticator app' +
    (state.auth.recoveryCodeCount
      ? ', or one of your single-use recovery codes.'
      : '. You have not enrolled two-factor on this account yet.') + '</p>' +
    '<label class="field"><span>Code</span>' +
    '<input type="text" id="auth-code" inputmode="numeric" autocomplete="one-time-code" spellcheck="false"></label>' +
    '<div class="actions"><button class="primary wide" data-action="auth-totp">Verify</button>' +
    '<button class="ghost wide" data-action="auth-back">Back</button></div>');
}

function authScreenEnrol() {
  return authCard(
    '<h2>Turn on two-factor</h2>' +
    '<ol class="numbered">' +
    '<li>Open Google Authenticator, 1Password, Authy or any TOTP app and add a new entry.</li>' +
    '<li>Enter this secret by hand, or import the setup URI:</li>' +
    '</ol>' +
    '<div class="secret-box" id="auth-secret">' + esc(state.auth.pendingSecret || '') + '</div>' +
    '<p class="hint"><button class="small ghost" data-action="auth-copy">Copy setup URI</button></p>' +
    '<pre class="uri-box" id="auth-uri">' + esc(state.auth.pendingUri || '') + '</pre>' +
    '<label class="field"><span>Code shown by your app</span>' +
    '<input type="text" id="auth-code" inputmode="numeric" autocomplete="one-time-code" spellcheck="false"></label>' +
    '<div class="actions"><button class="primary wide" data-action="auth-totp-confirm">Enable two-factor</button>' +
    '<button class="ghost wide" data-action="auth-totp-cancel">Cancel</button></div>');
}

function authScreenRecovery() {
  const codes = state.auth.recoveryCodes || [];
  return authCard(
    '<h2>Save your recovery codes</h2>' +
    '<p class="hint">Each code works once, in place of your authenticator. This is the only time they are shown - only their hashes are stored.</p>' +
    '<div class="codes">' + codes.map((code) => '<code>' + esc(code) + '</code>').join('') + '</div>' +
    '<div class="actions"><button class="ghost wide" data-action="auth-copy-codes">Copy codes</button>' +
    '<button class="primary wide" data-action="auth-recovery-done">I have saved them</button></div>');
}

const AUTH_SCREENS = {
  setup: authScreenSetup,
  login: authScreenLogin,
  totp: authScreenTotp,
  enrol: authScreenEnrol,
  recovery: authScreenRecovery,
};

// Screens shown before there is a session. See renderAuth().
const PREAUTH_SCREENS = ['setup', 'login', 'totp'];

function renderAuth() {
  const root = $('#auth-root');
  const shell = $('#app-root');
  const screen = state.auth.screen;
  // 'setup' | 'login' | 'totp' only make sense before sign-in. Once a session
  // exists they instead mean "go back to the wizard", which is how cancelling
  // enrolment returns here. The post-auth screens ('enrol', 'recovery') must
  // still take over the page, otherwise a half-finished enrolment would leave
  // the user in the app with a secret nobody has confirmed.
  const showAuth = state.auth.authenticated
    ? PREAUTH_SCREENS.indexOf(screen) === -1
    : true;
  if (showAuth) {
    shell.classList.add('hidden');
    root.classList.remove('hidden');
    root.innerHTML = (AUTH_SCREENS[screen] || authScreenLogin)();
    const first = root.querySelector('input');
    if (first) first.focus();
    return;
  }
  root.classList.add('hidden');
  root.innerHTML = '';
  shell.classList.remove('hidden');
  updateWho();
}

function updateWho() {
  const user = state.auth.user || {};
  const email = user.email || '-';
  const who = $('#who-email');
  if (who) {
    who.textContent = email;
    who.title = user.totp_enrolled ? email + ' - two-factor on' : email + ' - two-factor off';
  }
}

async function loadAuthState() {
  try {
    const data = await api('GET', '/api/auth/state');
    state.auth.accountsExist = !!data.accounts_exist;
    state.auth.passwordMinLength = data.password_min_length || 12;
    state.auth.totpDigits = data.totp_digits || 6;
    state.auth.recoveryCodeCount = data.recovery_code_count || 0;
    state.auth.user = data.user || null;
    state.auth.authenticated = !!data.authenticated;
  } catch (err) {
    state.auth.authenticated = false;
  }
}

function authValues() {
  return {
    email: ((($('#auth-email') || {}).value) || '').trim(),
    password: (($('#auth-password') || {}).value) || '',
    confirm: (($('#auth-confirm') || {}).value) || '',
    code: ((($('#auth-code') || {}).value) || '').trim(),
  };
}

async function enterApp() {
  state.auth.challenge = '';
  state.auth.pendingSecret = '';
  state.auth.pendingUri = '';
  await loadAuthState();
  if (!state.auth.authenticated) {
    state.auth.screen = state.auth.accountsExist ? 'login' : 'setup';
    renderAuth();
    return;
  }
  renderAuth();
  await initApp();
}

async function actionAuthSetup() {
  clearAuthError();
  const values = authValues();
  if (!values.email || !values.password) { authError('Enter an email address and a password.'); return; }
  try {
    await api('POST', '/api/auth/setup', {
      email: values.email,
      password: values.password,
      confirm: values.confirm,
    });
  } catch (err) {
    authError(err.message);
    return;
  }
  toast('Account created', 'success');
  await enterApp();
}

async function actionAuthLogin() {
  clearAuthError();
  const values = authValues();
  if (!values.email || !values.password) { authError('Enter your email address and password.'); return; }
  state.auth.email = values.email;
  const button = document.querySelector('[data-action="auth-login"]');
  if (button) { button.disabled = true; button.textContent = 'Checking...'; }
  try {
    const result = await api('POST', '/api/auth/login', { email: values.email, password: values.password });
    if (result.totp_required) {
      state.auth.challenge = result.challenge;
      state.auth.screen = 'totp';
      renderAuth();
      return;
    }
    await enterApp();
  } catch (err) {
    authError(err.message);
    const again = document.querySelector('[data-action="auth-login"]');
    if (again) { again.disabled = false; again.textContent = 'Sign in'; }
  }
}

async function actionAuthTotp() {
  clearAuthError();
  const code = authValues().code;
  if (!code) { authError('Enter the code from your authenticator app.'); return; }
  const button = document.querySelector('[data-action="auth-totp"]');
  if (button) { button.disabled = true; button.textContent = 'Verifying...'; }
  try {
    const result = await api('POST', '/api/auth/totp', {
      email: state.auth.email,
      code: code,
      challenge: state.auth.challenge,
    });
    if (result.recovery_codes_left !== undefined) {
      toast('Recovery code accepted - ' + result.recovery_codes_left + ' left', 'warn');
    }
    await enterApp();
  } catch (err) {
    state.auth.challenge = '';
    state.auth.screen = 'login';
    renderAuth();
    authError(err.message);
  }
}

function actionAuthBack() {
  state.auth.challenge = '';
  state.auth.screen = 'login';
  clearAuthError();
  renderAuth();
}

async function actionAuthLogout() {
  try {
    await api('POST', '/api/auth/logout', {});
  } catch (err) {
    /* the cookie is cleared server-side regardless of what the browser reports */
  }
  state.auth.authenticated = false;
  state.auth.user = null;
  state.auth.screen = state.auth.accountsExist ? 'login' : 'setup';
  closeModal();
  renderAuth();
  toast('Signed out', 'success');
}

async function actionTotpEnrol() {
  clearAuthError();
  try {
    const result = await api('POST', '/api/auth/totp/enrol', {});
    state.auth.pendingSecret = result.secret;
    state.auth.pendingUri = result.uri;
    closeModal();
    state.auth.screen = 'enrol';
    renderAuth();
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function actionTotpConfirm() {
  clearAuthError();
  const code = authValues().code;
  if (!code) { authError('Enter the code your app is showing.'); return; }
  try {
    const result = await api('POST', '/api/auth/totp/confirm', { code: code });
    state.auth.user = result.user;
    state.auth.recoveryCodes = result.recovery_codes || [];
    state.auth.screen = 'recovery';
    renderAuth();
  } catch (err) {
    authError(err.message);
  }
}

function actionTotpCancel() {
  state.auth.pendingSecret = '';
  state.auth.pendingUri = '';
  state.auth.screen = 'login';
  closeModal();
  renderAuth();
  openSecurity();
}

function actionRecoveryDone() {
  state.auth.recoveryCodes = [];
  state.auth.screen = 'login';
  renderAuth();
  openSecurity();
}

function actionCopySecret() {
  copyText((state.auth.pendingUri || '').trim(), 'Setup URI copied');
}

function actionCopyCodes() {
  copyText((state.auth.recoveryCodes || []).join('\n'), 'Recovery codes copied');
}

function copyText(text, message) {
  if (!text) return;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text)
      .then(() => toast(message, 'success'))
      .catch(() => toast('Copy failed - select the text manually', 'warn'));
  } else {
    toast('Select the text and copy it manually', 'warn');
  }
}

/* ------------------------------------------------------------- security */

function openSecurity() {
  const user = state.auth.user || {};
  const enrolled = !!user.totp_enrolled;
  openModal('<h2>Security</h2>' +
    '<p class="hint">Signed in as <strong>' + esc(user.email || '-') + '</strong></p>' +
    '<div class="card flat"><div class="flex between wrap mb">' +
    '<h3>Two-factor authentication</h3>' +
    (enrolled ? pill('enabled', 'good') : pill('not enabled', 'warn')) + '</div>' +
    '<p class="hint">' + (enrolled
      ? 'A ' + state.auth.totpDigits + '-digit code is required at every sign-in. ' +
        (user.recovery_codes_left || 0) + ' recovery code(s) left.'
      : 'Add a second factor so a stolen password is not enough to reach the wizard.') + '</p>' +
    '<div class="actions">' +
    (enrolled
      ? '<button class="danger" data-action="auth-totp-disable">Disable two-factor</button>'
      : '<button class="primary" data-action="auth-totp-enrol">Enable two-factor</button>') +
    '</div>' +
    (enrolled
      ? '<div class="grid-2 mt">' +
        '<label class="field"><span>Current password</span><input type="password" id="sec-totp-current" autocomplete="current-password"></label>' +
        '<label class="field"><span>Authenticator code</span><input type="text" id="sec-code" inputmode="numeric" autocomplete="one-time-code"></label>' +
        '</div><p class="small muted">A recovery code works in place of the authenticator code.</p>'
      : '') +
    '</div>' +
    '<div class="card flat"><h3>Change password</h3>' +
    '<label class="field"><span>Current password</span><input type="password" id="sec-current" autocomplete="current-password"></label>' +
    '<label class="field"><span>New password</span><input type="password" id="sec-next" autocomplete="new-password">' +
    '<span class="small muted">At least ' + state.auth.passwordMinLength + ' characters, mixing three of: lowercase, uppercase, digits, symbols.</span></label>' +
    '<label class="field"><span>Confirm new password</span><input type="password" id="sec-confirm" autocomplete="new-password"></label>' +
    '<div class="actions"><button class="primary" data-action="auth-change-password">Update password</button></div></div>' +
    '<div class="actions"><button class="ghost" data-action="close-modal">Close</button>' +
    '<div class="spacer"></div><button class="link-btn" data-action="logout">Sign out</button></div>');
}

async function actionTotpDisable() {
  // #sec-totp-current, not #sec-current: the change-password card below also
  // has a "current password" box and querySelector would return that one.
  const password = (($('#sec-totp-current') || {}).value) || '';
  const code = ((($('#sec-code') || {}).value) || '').trim();
  if (!password) { toast('Enter your current password to confirm', 'warn'); return; }
  if (state.auth.user && state.auth.user.totp_enrolled && !code) {
    toast('Enter the code from your authenticator', 'warn');
    return;
  }
  try {
    const result = await api('POST', '/api/auth/totp/disable', { password: password, code: code });
    state.auth.user = result.user;
    closeModal();
    openSecurity();
    toast('Two-factor disabled', 'warn');
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function actionChangePassword() {
  const current = (($('#sec-current') || {}).value) || '';
  const next = (($('#sec-next') || {}).value) || '';
  const confirm = (($('#sec-confirm') || {}).value) || '';
  if (!current || !next) { toast('Fill in every password field', 'warn'); return; }
  try {
    await api('POST', '/api/auth/password', { current: current, next: next, confirm: confirm });
  } catch (err) {
    toast(err.message, 'error');
    return;
  }
  toast('Password updated', 'success');
  closeModal();
  openSecurity();
}

/* ----------------------------------------------------------------- render */

function render() {
  renderProfileBar();
  renderSteps();
  $('#brand-profile').textContent = state.profile ? state.profile.name : 'No profile';
  $('#version').textContent = state.version ? 'v' + state.version : '';

  const main = $('#main');
  if (!state.profileId) {
    main.innerHTML = renderNoProfile();
    return;
  }
  switch (state.step) {
    case 1: main.innerHTML = renderProject(); break;
    case 2: main.innerHTML = renderGithub(); break;
    case 3: main.innerHTML = renderDiscover(); break;
    case 4: main.innerHTML = renderReview(); break;
    case 5: main.innerHTML = renderOutreach(); break;
    case 6: main.innerHTML = renderDashboard(); break;
    default: main.innerHTML = renderProject();
  }
}

function renderProfileBar() {
  const select = $('#profile-select');
  const options = state.profiles.map((p) =>
    '<option value="' + esc(p.id) + '"' + (p.id === state.profileId ? ' selected' : '') + '>' +
    esc(p.name || p.id) + '</option>').join('');
  select.innerHTML = options || '<option value="">No campaigns</option>';
  select.disabled = state.profiles.length === 0;
}

function renderSteps() {
  $('#steps').innerHTML = STEPS.map((s) => {
    const disabled = s.id > 1 && !state.profileId;
    const cls = ['step'];
    if (s.id === state.step) cls.push('active');
    if (s.id < state.step) cls.push('done');
    if (disabled) cls.push('disabled');
    const num = s.id < state.step ? '\u2713' : s.id;
    return '<button class="' + cls.join(' ') + '" data-action="go-step" data-step="' + s.id + '"' +
      (disabled ? ' disabled' : '') + '>' +
      '<span class="step-num">' + num + '</span>' +
      '<span class="step-labels"><span class="step-title">' + esc(s.title) + '</span>' +
      '<span class="step-sub">' + esc(s.sub) + '</span></span></button>';
  }).join('');
}

function pageHead(kicker, title, sub) {
  return '<header class="page-head"><div class="page-kicker">' + esc(kicker) + '</div>' +
    '<h1 class="page-title">' + esc(title) + '</h1>' +
    '<p class="page-sub">' + esc(sub) + '</p></header>';
}

function renderNoProfile() {
  return pageHead('Welcome', 'Create your first campaign',
    'A campaign bundles a project, its search terms, contacts and delivery settings.') +
    '<div class="card"><h3>No campaign yet</h3>' +
    '<p class="hint">Create one from a preset, then work through the steps.</p>' +
    '<button class="primary" data-action="new-profile">Create campaign</button></div>';
}

/* ---------------------------------------------------------- form helpers */

function field(label, bind, opts) {
  opts = opts || {};
  const value = getPath(state.draft, bind, opts.def === undefined ? '' : opts.def);
  const ph = opts.placeholder ? ' placeholder="' + esc(opts.placeholder) + '"' : '';
  const hint = opts.hint ? '<span class="small muted">' + esc(opts.hint) + '</span>' : '';
  if (opts.textarea) {
    const val = Array.isArray(value) ? value.join('\n') : value;
    return '<label class="field"><span>' + esc(label) + '</span>' +
      '<textarea data-bind="' + bind + '"' + (opts.list ? ' data-list="true"' : '') +
      ' rows="' + (opts.rows || 3) + '"' + ph + '>' + esc(val) + '</textarea>' + hint + '</label>';
  }
  return '<label class="field"><span>' + esc(label) + '</span>' +
    '<input type="' + (opts.type || 'text') + '" data-bind="' + bind + '" value="' + esc(value) + '"' + ph + '>' +
    hint + '</label>';
}

function checkField(label, bind) {
  const on = !!getPath(state.draft, bind, false);
  return '<label class="check"><input type="checkbox" data-bind="' + bind + '"' + (on ? ' checked' : '') + '> ' +
    esc(label) + '</label>';
}

function flagCheck(label, flag, on) {
  return '<label class="check"><input type="checkbox" data-flag="' + flag + '"' + (on ? ' checked' : '') + '> ' +
    esc(label) + '</label>';
}

function pill(text, kind) {
  return '<span class="pill ' + (kind || '') + '">' + esc(text) + '</span>';
}

/* ------------------------------------------------------------ step: project */

function renderProject() {
  const features = getPath(state.draft, 'plugin.features', []);
  return pageHead('Step 1', 'Project', 'Describe what you are promoting. Everything here is generic - the bot inserts it into every message.') +
    '<div class="card">' +
    '<h3>Campaign</h3>' +
    '<p class="hint">A campaign is an isolated profile with its own config, contacts, log and outbox.</p>' +
    '<div class="row">' +
    '<div class="actions">' +
    '<button class="primary" data-action="save-profile">' + (state.dirty ? 'Save changes' : 'Saved') + '</button>' +
    '<button class="ghost" data-action="delete-profile">Delete campaign</button>' +
    '</div></div></div>' +

    '<div class="card"><h3>Plugin</h3><p class="hint">The product or project you want funded, sponsored or co-marketed.</p>' +
    '<div class="grid-2">' +
    field('Name', 'plugin.name', { placeholder: 'Acme Cloud Toolkit' }) +
    field('Maintainer name', 'plugin.maintainer_name') +
    field('Repository URL', 'plugin.repo_url', { placeholder: 'https://github.com/org/repo' }) +
    field('Maintainer email', 'plugin.maintainer_email', { type: 'email' }) +
    field('Listing / marketplace URL', 'plugin.appfoundry_url', { placeholder: 'https://...' }) +
    field('Demo URL', 'plugin.demo_url', { placeholder: 'https://... (optional)' }) +
    '</div>' +
    field('Description', 'plugin.description', { textarea: true, rows: 3 }) +
    field('Value proposition (problem-first, one or two lines)', 'plugin.value_prop', { textarea: true, rows: 2 }) +
    field('Features (one per line)', 'plugin.features', { textarea: true, list: true, rows: 5, hint: features.length + ' feature(s)' }) +
    field('Call to action', 'plugin.cta', { textarea: true, rows: 2 }) +
    field('Sponsorship ask', 'plugin.ask', { textarea: true, rows: 2 }) +
    field('Unsubscribe / opt-out note', 'plugin.unsubscribe_note', { textarea: true, rows: 2 }) +
    '<div class="actions"><button class="primary" data-action="save-profile">Save changes</button>' +
    '<span class="small muted">' + (state.dirty ? 'Unsaved changes' : 'All changes saved') + '</span></div>' +
    '</div>';
}

/* ------------------------------------------------------------- step: github */

function renderGithub() {
  const tokenEnv = getPath(state.draft, 'github.token_env', 'GITHUB_TOKEN');
  const connected = state.github && state.github.connected;
  const statusPill = connected
    ? pill('connected' + (state.github.login ? ' as ' + state.github.login : ''), 'good')
    : pill('anonymous (limited quota)', 'warn');
  const rate = state.github && state.github.rate_limit;
  const rateText = rate && rate.limit
    ? 'Search quota ' + (rate.remaining !== undefined ? rate.remaining : '?') + '/' + rate.limit
    : 'Quota unknown';

  return pageHead('Step 2', 'Connect to GitHub', 'A personal access token raises the search quota and lets the bot read public profiles. It is kept in memory only - never written to disk.') +
    '<div class="card"><div class="flex between wrap"><h3>Connection</h3>' + statusPill + '</div>' +
    '<p class="hint">' + esc(rateText) + '</p>' +
    '<div class="grid-2">' +
    '<label class="field"><span>Personal access token (classic or fine-grained)</span>' +
    '<input type="password" id="gh-token" placeholder="ghp_... (blank = anonymous)"></label>' +
    field('Environment variable name', 'github.token_env', { placeholder: 'GITHUB_TOKEN', hint: 'The token is stored under this variable for this process only.' }) +
    '</div>' +
    '<div class="actions">' +
    '<button class="primary" data-action="connect-github">Connect</button>' +
    '<button class="ghost" data-action="disconnect-github">Disconnect</button>' +
    '<span class="small muted">Current variable: <code>' + esc(tokenEnv) + '</code></span>' +
    '</div></div>' +

    '<div class="card"><h3>What to search for</h3>' +
    '<p class="hint">Topics are matched as GitHub subjects. Comma or newline separated. For a Genesys campaign use <code>genesys</code>, <code>genesys-cloud</code>.</p>' +
    field('Topics', 'github.topics', { textarea: true, list: true, rows: 3, placeholder: 'genesys, genesys-cloud' }) +
    '<div class="grid-2">' +
    field('Results per page (max 100)', 'github.per_page', { type: 'number' }) +
    field('Max pages per topic', 'github.max_pages', { type: 'number' }) +
    field('Minimum stars', 'github.filters.min_stars', { type: 'number' }) +
    field('Min repos for an organisation', 'github.filters.min_org_repos', { type: 'number' }) +
    field('Min topic repos for an individual', 'github.filters.min_individual_genesys_repos', { type: 'number' }) +
    field('Max owner profile lookups', 'github.filters.max_owner_lookups', { type: 'number', hint: 'Budget: each lookup is one API call.' }) +
    '</div>' +
    checkField('Exclude forks', 'github.filters.exclude_forks') +
    checkField('Scrape public email addresses from profiles / websites', 'github.scrape_public_emails') +
    '<div class="actions"><button class="primary" data-action="save-profile">Save settings</button>' +
    '<div class="spacer"></div><button data-action="go-step" data-step="3">Continue to discover</button></div>' +
    '</div>';
}

/* ----------------------------------------------------------- step: discover */

function renderDiscover() {
  const topics = getPath(state.draft, 'github.topics', []);
  const topicList = topics.length ? topics.join(', ') : '(none set)';
  return pageHead('Step 3', 'Discover', 'Sweep GitHub for owners building in your space. This can take a minute; it runs in the background.') +
    '<div class="card"><h3>Search</h3>' +
    '<p class="hint">Topics: <code>' + esc(topicList) + '</code></p>' +
    '<div class="actions"><button class="primary" data-action="run-discovery">Run discovery</button>' +
    '<button class="ghost" data-action="go-step" data-step="4">Skip to review</button></div></div>' +
    '<div id="job-panel">' + (state.job && state.jobKind === 'discovery' ? jobInner() : '') + '</div>';
}

/* ------------------------------------------------------------- step: review */

function filteredCandidates() {
  const query = state.candidateQuery.trim().toLowerCase();
  if (!query) return state.candidates;
  return state.candidates.filter((c) => {
    const hay = [c.owner_name, c.top_repo, c.description, c.language, (c.discovered_from || []).join(' '), c.email_if_public]
      .join(' ').toLowerCase();
    return hay.indexOf(query) !== -1;
  });
}

function candidateRows() {
  const rows = filteredCandidates();
  if (!rows.length) {
    return '<tr><td colspan="7" class="empty">No matching candidates.</td></tr>';
  }
  return rows.map((c) => {
    const key = c.owner_name;
    const email = c.email_if_public ? '<span class="mono small">' + esc(c.email_if_public) + '</span>' : '<span class="muted small">no public email</span>';
    return '<tr>' +
      '<td><input type="checkbox" data-candidate="' + esc(key) + '"' + (state.selected.has(key) ? ' checked' : '') + '></td>' +
      '<td><strong>' + esc(c.owner_name) + '</strong>' + (c.github_url ? '<div class="small"><a href="' + esc(c.github_url) + '" target="_blank" rel="noreferrer">profile</a></div>' : '') + '</td>' +
      '<td>' + esc(c.owner_type || '') + '</td>' +
      '<td class="mono">' + (c.repos_count || 0) + '</td>' +
      '<td class="mono">' + (c.genesys_repos_count || 0) + '</td>' +
      '<td class="mono">' + (c.stars || 0) + '</td>' +
      '<td>' + email + (c.top_repo ? '<div class="small muted">' + esc(truncate(c.top_repo, 34)) + '</div>' : '') + '</td>' +
      '</tr>';
  }).join('');
}

function renderReview() {
  if (!state.candidates.length) {
    return pageHead('Step 4', 'Review', 'Approve the owners you want to contact.') +
      '<div class="card"><p class="hint">No candidates yet.</p>' +
      '<div class="actions"><button class="primary" data-action="go-step" data-step="3">Run discovery</button>' +
      '<button class="ghost" data-action="refresh-candidates">Load last results</button></div></div>';
  }
  const withEmail = state.candidates.filter((c) => c.email_if_public).length;
  return pageHead('Step 4', 'Review', 'Approve the owners you want to contact. Only approved owners enter the tracker.') +
    '<div class="card">' +
    '<div class="flex between wrap mb">' +
    '<div class="flex wrap">' + pill(state.candidates.length + ' candidates', 'info') + pill(withEmail + ' with public email', '') + pill(state.selected.size + ' selected', state.selected.size ? 'good' : '') + '</div>' +
    '<div class="actions">' +
    '<button class="small ghost" data-action="select-all-email">Select all with email</button>' +
    '<button class="small ghost" data-action="clear-selection">Clear</button>' +
    '<button class="small ghost" data-action="refresh-candidates">Reload</button>' +
    '</div></div>' +
    '<div class="actions mb"><input type="text" data-filter="candidate" placeholder="Filter by owner, repo or language..." value="' + esc(state.candidateQuery) + '">' +
    '<select data-flag="approveChannel"><option value="email"' + (state.approveChannel === 'email' ? ' selected' : '') + '>email channel</option>' +
    '<option value="forum"' + (state.approveChannel === 'forum' ? ' selected' : '') + '>forum channel</option></select>' +
    '<button class="primary" data-action="approve">Approve ' + state.selected.size + ' selected</button>' +
    '</div>' +
    '<div class="table-wrap"><table><thead><tr>' +
    '<th style="width:34px"></th><th>Owner</th><th>Type</th><th class="mono">Repos</th><th class="mono">Topic</th><th class="mono">Stars</th><th>Contact</th>' +
    '</tr></thead><tbody id="candidate-rows">' + candidateRows() + '</tbody></table></div>' +
    '</div>';
}

function renderCandidateRows() {
  const tbody = $('#candidate-rows');
  if (tbody) tbody.innerHTML = candidateRows();
}

/* ------------------------------------------------------------ step: outreach */

function newSponsors() {
  if (!state.sponsors) return [];
  return state.sponsors.sponsors.filter((s) => s.status === 'new');
}

function renderOutreach() {
  const info = state.statusInfo && state.statusInfo.config ? state.statusInfo.config : {};
  const smtpSet = !!info.smtp_password_set;
  const ghSet = !!(state.github && state.github.connected);
  const sponsors = newSponsors();
  const selectedCount = sponsors.filter((s) => state.sendSelected.has(s.name)).length;

  const sponsorRows = sponsors.length ? sponsors.map((s) => {
    const deliverable = s.email || s.contact || s.website;
    return '<tr>' +
      '<td><input type="checkbox" data-sponsor-pick="' + esc(s.name) + '"' + (state.sendSelected.has(s.name) ? ' checked' : '') + '></td>' +
      '<td><strong>' + esc(s.name) + '</strong><div class="small muted">' + esc(s.channel) + '</div></td>' +
      '<td class="mono small">' + esc(s.email || s.website || s.contact || '-') + '</td>' +
      '<td class="mono">' + (s.stars || 0) + '</td>' +
      '<td>' + (deliverable ? pill('ready', 'good') : pill('no contact', 'warn')) + '</td>' +
      '<td><button class="small ghost" data-action="preview-sponsor" data-name="' + esc(s.name) + '">Preview</button></td>' +
      '</tr>';
  }).join('') : '<tr><td colspan="6" class="empty">No sponsors in the <code>new</code> queue. Approve candidates in step 4 or add a contact.</td></tr>';

  let resultHtml = '';
  if (state.sendResult && state.sendResult.result) {
    const r = state.sendResult.result;
    const deliveries = (r.deliveries || []).map((d) =>
      '<tr><td>' + esc(d.sponsor) + '</td><td>' + esc(d.channel) + '</td>' +
      '<td>' + (d.ok ? pill('ok', 'good') : pill('failed', 'bad')) + '</td>' +
      '<td class="mono small">' + esc(d.detail || '') + '</td></tr>').join('');
    resultHtml = '<div class="card"><h3>Last delivery</h3>' +
      '<div class="flex wrap mb">' + pill('attempted ' + r.attempted, '') + pill('sent ' + r.sent, 'good') +
      pill('failed ' + r.failed, r.failed ? 'bad' : '') + pill('skipped ' + r.skipped, 'warn') + '</div>' +
      (deliveries ? '<div class="table-wrap"><table><thead><tr><th>Sponsor</th><th>Channel</th><th>Status</th><th>Detail</th></tr></thead><tbody>' + deliveries + '</tbody></table></div>' : '') +
      ((r.notes || []).length ? '<ul class="list mt">' + r.notes.map((n) => '<li class="small muted">' + esc(n) + '</li>').join('') + '</ul>' : '') +
      '</div>';
  }

  return pageHead('Step 5', 'Outreach', 'Preview each message, then send the approved contacts. Rate limits and the per-contact cooldown still apply.') +
    '<div class="card"><h3>Secrets &amp; credentials</h3>' +
    '<p class="hint">Kept in memory for this process only - never written to the config file. Set the matching environment variable for CLI runs.</p>' +
    '<div class="grid-2">' +
    '<label class="field"><span>SMTP password' + (smtpSet ? ' (set)' : '') + '</span><input type="password" id="smtp-password" placeholder="app password"></label>' +
    '<label class="field"><span>Forum API key</span><input type="password" id="forum-key" placeholder="optional"></label>' +
    '</div>' +
    '<div class="actions"><button data-action="save-secrets">Save secrets</button>' +
    (ghSet ? pill('GitHub connected', 'good') : pill('GitHub not connected', 'warn')) +
    (smtpSet ? pill('SMTP ready', 'good') : pill('SMTP password missing', 'warn')) + '</div></div>' +

    '<div class="card"><div class="flex between wrap"><h3>Queue</h3><div class="flex wrap">' +
    pill(sponsors.length + ' in queue', '') + pill(selectedCount + ' selected', selectedCount ? 'good' : '') + '</div></div>' +
    flagCheck('Dry run (generate and log, never transmit)', 'sendDryRun', state.sendDryRun) +
    flagCheck('Render with the configured LLM command', 'sendUseLlm', state.sendUseLlm) +
    '<div class="actions mb"><button class="primary" data-action="send">Send ' + selectedCount + ' selected</button>' +
    (state.job && state.jobKind === 'send' && (state.job.state === 'running' || state.job.state === 'pending') ? '<button class="danger" data-action="cancel-job">Cancel</button>' : '') +
    '<button class="ghost" data-action="refresh-sponsors">Refresh</button></div>' +
    '<div class="table-wrap"><table><thead><tr><th style="width:34px"></th><th>Name</th><th>Address</th><th class="mono">Stars</th><th>Status</th><th></th></tr></thead><tbody>' + sponsorRows + '</tbody></table></div>' +
    '</div>' +
    '<div id="job-panel">' + (state.job && state.jobKind === 'send' ? jobInner() : '') + '</div>' +
    resultHtml;
}

/* ----------------------------------------------------------- step: dashboard */

function renderDashboard() {
  const summary = state.sponsors ? state.sponsors.summary : null;
  if (!summary) return pageHead('Step 6', 'Dashboard', 'Loading...');

  const byStatus = summary.by_status || {};
  const stats = '<div class="stats">' +
    stat(summary.total, 'Total contacts') +
    stat(byStatus.new || 0, 'New') +
    stat(byStatus.contacted || 0, 'Contacted') +
    stat(byStatus.replied || 0, 'Replied') +
    stat(byStatus.sponsored || 0, 'Sponsored') +
    stat(summary.failures || 0, 'Failures') +
    '</div>';

  const usage = summary.daily_usage || {};
  const usageHtml = Object.keys(usage).map((channel) => {
    const u = usage[channel];
    const pct = u.limit ? Math.min(100, Math.round((u.used / u.limit) * 100)) : 0;
    return '<div class="usage-item"><span class="name">' + esc(channel) + '</span>' +
      '<span class="bar"><i data-pct="' + pct + '"></i></span>' +
      '<span class="small muted">' + u.used + '/' + u.limit + '</span></div>';
  }).join('');

  const rows = state.sponsors.sponsors.length ? state.sponsors.sponsors.map((s) => {
    const statusOptions = state.statuses.map((st) =>
      '<option value="' + esc(st) + '"' + (st === s.status ? ' selected' : '') + '>' + esc(st) + '</option>').join('');
    return '<tr>' +
      '<td><strong>' + esc(s.name) + '</strong>' + (s.company ? '<div class="small muted">' + esc(s.company) + '</div>' : '') + '</td>' +
      '<td>' + esc(s.channel) + '</td>' +
      '<td class="mono small">' + esc(s.email || s.website || s.contact || '-') + '</td>' +
      '<td class="mono">' + (s.attempts || 0) + '</td>' +
      '<td><select data-mark="' + esc(s.name) + '">' + statusOptions + '</select></td>' +
      '<td><button class="small ghost" data-action="preview-sponsor" data-name="' + esc(s.name) + '">Preview</button> ' +
      '<button class="small danger" data-action="remove-sponsor" data-name="' + esc(s.name) + '">Remove</button></td>' +
      '</tr>';
  }).join('') : '<tr><td colspan="6" class="empty">No contacts tracked yet.</td></tr>';

  const history = (summary.recent_history || []).slice().reverse().map((entry) => {
    const detail = Object.keys(entry).filter((k) => k !== 'at' && k !== 'event')
      .map((k) => k + '=' + entry[k]).join(' ');
    return '<li class="small"><span class="mono muted">' + esc(entry.at || '') + '</span> &middot; <strong>' +
      esc(entry.event || '') + '</strong> ' + esc(detail) + '</li>';
  }).join('');

  return pageHead('Step 6', 'Dashboard', 'The current pipeline, daily rate limits and recent activity.') +
    stats +
    '<div class="card"><h3>Rate limits today</h3><div class="usage">' + (usageHtml || '<p class="hint">No channels configured.</p>') + '</div></div>' +
    '<div class="card"><div class="flex between wrap"><h3>Contacts</h3>' +
    '<div class="actions"><button class="small" data-action="add-contact">+ Add contact</button>' +
    '<button class="small ghost" data-action="refresh-sponsors">Refresh</button></div></div>' +
    '<div class="table-wrap"><table><thead><tr><th>Name</th><th>Channel</th><th>Address</th><th class="mono">Attempts</th><th>Status</th><th></th></tr></thead><tbody>' + rows + '</tbody></table></div></div>' +
    '<div class="card"><h3>Recent activity</h3>' + (history ? '<ul class="list">' + history + '</ul>' : '<p class="hint">Nothing yet.</p>') + '</div>';
}

function stat(value, label) {
  return '<div class="stat"><div class="value">' + value + '</div><div class="label">' + esc(label) + '</div></div>';
}

/* -------------------------------------------------------------- job panel */

function jobInner() {
  if (!state.job) return '';
  const j = state.job;
  const running = j.state === 'running' || j.state === 'pending';
  const kind = j.state === 'done' ? 'good' : j.state === 'error' ? 'bad' : j.state === 'cancelled' ? 'warn' : 'info';
  return '<div class="job-panel"><div class="job-head">' +
    (running ? '<span class="spinner"></span>' : '') +
    '<strong>' + esc(j.label) + '</strong>' + pill(j.state, kind) +
    '<span class="spacer"></span>' +
    (running ? '<button class="small ghost" data-action="cancel-job">Cancel</button>' : '') +
    '</div><pre class="job-log" id="job-log">' + esc(state.jobLogs.join('\n')) + '</pre></div>';
}

function updateJobPanel() {
  const panel = $('#job-panel');
  if (!panel || !state.job) return;
  panel.innerHTML = jobInner();
  const log = $('#job-log');
  if (log) log.scrollTop = log.scrollHeight;
}

/* --------------------------------------------------------------- job flow */

async function runJob(kind, path, body) {
  if (state.job && (state.job.state === 'running' || state.job.state === 'pending')) {
    toast('A job is already running.', 'warn');
    return;
  }
  let response;
  try {
    response = await api('POST', path, body);
  } catch (err) {
    toast(err.message, 'error');
    return;
  }
  state.job = response.job;
  state.jobKind = kind;
  state.jobLogs = [];
  state.jobLogCount = 0;
  if (kind === 'send') state.sendResult = null;
  render();
  pollJob();
}

async function pollJob() {
  const job = state.job;
  if (!job) return;
  let data;
  try {
    data = await api('GET', '/api/jobs/' + job.id + '?since=' + state.jobLogCount);
  } catch (err) {
    toast(err.message, 'error');
    state.job = null;
    render();
    return;
  }
  const current = data.job;
  state.job = current;
  state.jobLogCount = current.log_count || 0;
  if (current.logs && current.logs.length) {
    current.logs.forEach((line) => state.jobLogs.push(line));
    if (state.jobLogs.length > 800) state.jobLogs.splice(0, state.jobLogs.length - 800);
  }
  updateJobPanel();
  const running = current.state === 'running' || current.state === 'pending';
  if (running) {
    setTimeout(pollJob, 900);
  } else {
    await onJobFinished(current);
  }
}

async function onJobFinished(job) {
  if (job.state === 'error') {
    toast(job.error || 'Job failed', 'error');
    render();
    return;
  }
  if (job.state === 'cancelled') {
    toast('Job cancelled', 'warn');
    render();
    return;
  }
  if (job.kind === 'discovery') {
    state.candidates = (job.result && job.result.candidates) || [];
    state.selected = new Set();
    state.step = 4;
    toast('Discovery finished: ' + state.candidates.length + ' candidate(s)', 'success');
    render();
  } else if (job.kind === 'send') {
    state.sendResult = job.result;
    await loadSponsors();
    await loadStatus();
    toast('Delivery finished', 'success');
    render();
  } else {
    render();
  }
}

/* ------------------------------------------------------------- data loads */

async function selectProfile(id) {
  try {
    const data = await api('GET', '/api/profiles/' + id);
    state.profileId = id;
    state.profile = data.profile;
    state.draft = deepClone(data.config);
    state.dirty = false;
    state.candidates = [];
    state.selected = new Set();
    state.sendSelected = new Set();
    state.sendResult = null;
    state.job = null;
    state.step = 1;
    try { state.github = await api('GET', '/api/profiles/' + id + '/github'); } catch (err) { /* keep defaults */ }
    await loadStatus();
    await loadSponsors();
    await loadCandidates();
    render();
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function loadStatus() {
  if (!state.profileId) return;
  try {
    state.statusInfo = await api('GET', '/api/profiles/' + state.profileId + '/status');
  } catch (err) {
    state.statusInfo = null;
  }
}

async function loadSponsors() {
  if (!state.profileId) return;
  try {
    state.sponsors = await api('GET', '/api/profiles/' + state.profileId + '/sponsors');
    const names = (state.sponsors.sponsors || []).filter((s) => s.status === 'new').map((s) => s.name);
    state.sendSelected = new Set(names);
  } catch (err) {
    state.sponsors = null;
  }
}

async function loadCandidates() {
  if (!state.profileId) return;
  try {
    const data = await api('GET', '/api/profiles/' + state.profileId + '/candidates');
    state.candidates = data.candidates || [];
  } catch (err) {
    state.candidates = [];
  }
}

async function saveProfile() {
  if (!state.draft) return false;
  let response;
  try {
    response = await api('PUT', '/api/profiles/' + state.profileId, { config: state.draft });
  } catch (err) {
    toast(err.message, 'error');
    return false;
  }
  if (!response.ok) {
    toast((response.errors || ['Validation failed']).join('; '), 'error');
    return false;
  }
  state.profile = response.profile || state.profile;
  state.dirty = false;
  if (response.warnings && response.warnings.length) toast(response.warnings.join(' | '), 'warn');
  await refreshProfiles();
  toast('Saved', 'success');
  return true;
}

async function refreshProfiles() {
  try {
    const data = await api('GET', '/api/profiles');
    state.profiles = data.profiles || [];
  } catch (err) { /* ignore */ }
}

/* --------------------------------------------------------------- actions */

async function actionGoStep(step) {
  if (step > 1 && !state.profileId) return;
  if (state.dirty && (state.step === 1 || state.step === 2)) {
    const ok = await saveProfile();
    if (!ok) return;
  }
  state.step = Number(step);
  render();
}

async function actionConnectGithub() {
  const token = ($('#gh-token') || {}).value || '';
  const tokenEnv = getPath(state.draft, 'github.token_env', 'GITHUB_TOKEN');
  try {
    const result = await api('POST', '/api/github/connect', { token: token, token_env: tokenEnv });
    state.github = result;
    toast(result.message || 'Connected', result.connected ? 'success' : 'warn');
    render();
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function actionDisconnectGithub() {
  const tokenEnv = getPath(state.draft, 'github.token_env', 'GITHUB_TOKEN');
  try {
    state.github = await api('POST', '/api/github/disconnect', { token_env: tokenEnv });
    toast('Disconnected', 'warn');
    render();
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function actionRunDiscovery() {
  if (state.dirty) {
    const ok = await saveProfile();
    if (!ok) return;
  }
  const body = {
    topics: getPath(state.draft, 'github.topics', []),
    min_org_repos: getPath(state.draft, 'github.filters.min_org_repos', 5),
    min_individual_genesys_repos: getPath(state.draft, 'github.filters.min_individual_genesys_repos', 3),
    min_stars: getPath(state.draft, 'github.filters.min_stars', 0),
    exclude_forks: getPath(state.draft, 'github.filters.exclude_forks', true),
    per_page: getPath(state.draft, 'github.per_page', 100),
    max_pages: getPath(state.draft, 'github.max_pages', 3),
    max_owner_lookups: getPath(state.draft, 'github.filters.max_owner_lookups', 40),
    scrape_public_emails: getPath(state.draft, 'github.scrape_public_emails', true),
    email_scrape_max_sites: getPath(state.draft, 'github.email_scrape_max_sites', 3),
  };
  await runJob('discovery', '/api/profiles/' + state.profileId + '/discover', body);
}

async function actionApprove() {
  if (!state.selected.size) {
    toast('Select at least one candidate', 'warn');
    return;
  }
  const chosen = state.candidates.filter((c) => state.selected.has(c.owner_name));
  try {
    const result = await api('POST', '/api/profiles/' + state.profileId + '/approve', {
      candidates: chosen,
      channel: state.approveChannel,
    });
    toast('Added ' + result.created + ' new, enriched ' + result.updated, 'success');
    if (result.errors && result.errors.length) toast(result.errors.join('; '), 'warn');
    state.step = 5;
    await loadSponsors();
    render();
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function actionSend() {
  const names = newSponsors().filter((s) => state.sendSelected.has(s.name)).map((s) => s.name);
  if (!names.length) {
    toast('Select at least one contact to send to', 'warn');
    return;
  }
  if (!state.sendDryRun) {
    if (!confirm('Live send: transmit to ' + names.length + ' contact(s)? This cannot be undone.')) return;
  }
  await runJob('send', '/api/profiles/' + state.profileId + '/send', {
    names: names,
    dry_run: state.sendDryRun,
    llm: state.sendUseLlm,
    batch: names.length,
  });
}

async function actionSaveSecrets() {
  const smtp = ($('#smtp-password') || {}).value || '';
  const forum = ($('#forum-key') || {}).value || '';
  if (!smtp && !forum) { toast('Nothing to save', 'warn'); return; }
  try {
    const result = await api('POST', '/api/profiles/' + state.profileId + '/secrets', {
      smtp_password: smtp,
      forum_api_key: forum,
    });
    toast('Saved: ' + (result.applied.join(', ') || 'none'), 'success');
    const fields = ['#smtp-password', '#forum-key'];
    fields.forEach((sel) => { const el = $(sel); if (el) el.value = ''; });
    await loadStatus();
    render();
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function actionPreview(name) {
  try {
    const sponsor = (state.sponsors ? state.sponsors.sponsors : []).find((s) => s.name === name);
    const channel = sponsor ? sponsor.channel : 'email';
    const result = await api('POST', '/api/profiles/' + state.profileId + '/preview', {
      sponsor: name,
      channel: channel,
      llm: state.sendUseLlm,
    });
    openModal('<h2>Preview &middot; ' + esc(result.sponsor) + '</h2>' +
      '<p class="hint">' + esc(result.channel) + ' &middot; ' + result.words + '/' + result.word_limit + ' words</p>' +
      '<pre class="message-preview">' + esc(result.message) + '</pre>' +
      '<div class="actions mt"><button class="primary" data-action="close-modal">Close</button></div>');
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function actionMark(name, status) {
  try {
    await api('POST', '/api/profiles/' + state.profileId + '/mark', { name: name, status: status });
    toast(name + ' -> ' + status, 'success');
    await loadSponsors();
    render();
  } catch (err) {
    toast(err.message, 'error');
    await loadSponsors();
    render();
  }
}

async function actionRemove(name) {
  if (!confirm('Remove ' + name + ' from this campaign?')) return;
  try {
    await api('POST', '/api/profiles/' + state.profileId + '/remove', { name: name });
    toast('Removed ' + name, 'success');
    await loadSponsors();
    await loadStatus();
    render();
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function actionCancelJob() {
  if (!state.job) return;
  try {
    await api('POST', '/api/jobs/' + state.job.id + '/cancel', {});
    toast('Cancelling...', 'warn');
  } catch (err) {
    toast(err.message, 'error');
  }
}

function actionNewProfile() {
  const options = state.presets.map((p) =>
    '<option value="' + esc(p.id) + '">' + esc(p.label) + ' - ' + esc(p.description) + '</option>').join('');
  openModal('<h2>New campaign</h2>' +
    '<p class="hint">Each campaign is fully isolated: its own config, contacts, log and outbox.</p>' +
    '<label class="field"><span>Name</span><input type="text" id="np-name" placeholder="Genesys Cloud plugin"></label>' +
    '<label class="field"><span>Preset</span><select id="np-preset">' + options + '</select></label>' +
    '<div class="actions"><button class="primary" data-action="create-profile">Create</button>' +
    '<button class="ghost" data-action="close-modal">Cancel</button></div>');
}

async function actionCreateProfile() {
  const name = ($('#np-name') || {}).value || '';
  const preset = ($('#np-preset') || {}).value || 'generic';
  if (!name.trim()) { toast('Enter a name', 'warn'); return; }
  try {
    const result = await api('POST', '/api/profiles', { name: name, preset: preset });
    closeModal();
    await refreshProfiles();
    await selectProfile(result.profile.id);
    toast('Created ' + result.profile.name, 'success');
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function actionDeleteProfile() {
  if (!state.profileId) return;
  if (!confirm('Delete campaign "' + state.profile.name + '" and all of its local state?')) return;
  try {
    await api('DELETE', '/api/profiles/' + state.profileId);
    toast('Deleted', 'success');
    state.profileId = null;
    state.profile = null;
    state.draft = null;
    await refreshProfiles();
    if (state.profiles.length) await selectProfile(state.profiles[0].id);
    else render();
  } catch (err) {
    toast(err.message, 'error');
  }
}

function actionAddContact() {
  const channels = state.channels.map((c) => '<option value="' + esc(c) + '">' + esc(c) + '</option>').join('');
  openModal('<h2>Add contact</h2>' +
    '<div class="grid-2">' +
    '<label class="field"><span>Name</span><input type="text" id="ac-name"></label>' +
    '<label class="field"><span>Channel</span><select id="ac-channel">' + channels + '</select></label>' +
    '<label class="field"><span>Email</span><input type="email" id="ac-email"></label>' +
    '<label class="field"><span>Company</span><input type="text" id="ac-company"></label>' +
    '<label class="field"><span>GitHub login</span><input type="text" id="ac-github"></label>' +
    '<label class="field"><span>Website</span><input type="text" id="ac-website"></label>' +
    '</div>' +
    '<label class="field"><span>Notes</span><textarea id="ac-notes" rows="2"></textarea></label>' +
    '<div class="actions"><button class="primary" data-action="create-contact">Add</button>' +
    '<button class="ghost" data-action="close-modal">Cancel</button></div>');
}

async function actionCreateContact() {
  const payload = {
    name: ($('#ac-name') || {}).value || '',
    channel: ($('#ac-channel') || {}).value || 'email',
    email: ($('#ac-email') || {}).value || '',
    company: ($('#ac-company') || {}).value || '',
    github: ($('#ac-github') || {}).value || '',
    website: ($('#ac-website') || {}).value || '',
    notes: ($('#ac-notes') || {}).value || '',
  };
  try {
    const result = await api('POST', '/api/profiles/' + state.profileId + '/sponsors', payload);
    closeModal();
    toast(result.created ? 'Added ' + payload.name : 'Enriched ' + payload.name, 'success');
    await loadSponsors();
    render();
  } catch (err) {
    toast(err.message, 'error');
  }
}

/* -------------------------------------------------------- event wiring */

document.addEventListener('click', (event) => {
  const el = event.target.closest('[data-action]');
  if (!el) return;
  const action = el.dataset.action;
  const handlers = {
    'go-step': () => actionGoStep(Number(el.dataset.step)),
    'new-profile': actionNewProfile,
    'create-profile': actionCreateProfile,
    'delete-profile': actionDeleteProfile,
    'save-profile': async () => { if (await saveProfile()) render(); },
    'connect-github': actionConnectGithub,
    'disconnect-github': actionDisconnectGithub,
    'run-discovery': actionRunDiscovery,
    'approve': actionApprove,
    'send': actionSend,
    'save-secrets': actionSaveSecrets,
    'cancel-job': actionCancelJob,
    'close-modal': closeModal,
    'add-contact': actionAddContact,
    'create-contact': actionCreateContact,
    'logout': actionAuthLogout,
    'open-security': openSecurity,
    'auth-setup': actionAuthSetup,
    'auth-login': actionAuthLogin,
    'auth-totp': actionAuthTotp,
    'auth-back': actionAuthBack,
    'auth-totp-enrol': actionTotpEnrol,
    'auth-totp-confirm': actionTotpConfirm,
    'auth-totp-cancel': actionTotpCancel,
    'auth-totp-disable': actionTotpDisable,
    'auth-change-password': actionChangePassword,
    'auth-recovery-done': actionRecoveryDone,
    'auth-copy': actionCopySecret,
    'auth-copy-codes': actionCopyCodes,
    'select-all-email': () => {
      state.selected = new Set(state.candidates.filter((c) => c.email_if_public).map((c) => c.owner_name));
      render();
    },
    'clear-selection': () => { state.selected = new Set(); render(); },
    'refresh-candidates': async () => { await loadCandidates(); toast('Candidates reloaded'); render(); },
    'refresh-sponsors': async () => { await loadSponsors(); await loadStatus(); render(); },
    'preview-sponsor': () => actionPreview(el.dataset.name),
    'remove-sponsor': () => actionRemove(el.dataset.name),
  };
  const handler = handlers[action];
  if (handler) {
    event.preventDefault();
    handler();
  }
});

document.addEventListener('input', (event) => {
  const bindEl = event.target.closest('[data-bind]');
  if (bindEl) applyBind(bindEl);
  const filterEl = event.target.closest('[data-filter="candidate"]');
  if (filterEl) {
    state.candidateQuery = filterEl.value || '';
    renderCandidateRows();
  }
});

document.addEventListener('change', (event) => {
  const target = event.target;
  const bindEl = target.closest('[data-bind]');
  if (bindEl) applyBind(bindEl);

  const flagEl = target.closest('[data-flag]');
  if (flagEl) {
    const flag = flagEl.dataset.flag;
    state[flag] = flagEl.type === 'checkbox' ? flagEl.checked : flagEl.value;
    if (flag === 'approveChannel') { state.approveChannel = flagEl.value; }
  }

  const candidateEl = target.closest('[data-candidate]');
  if (candidateEl) {
    const key = candidateEl.dataset.candidate;
    if (candidateEl.checked) state.selected.add(key); else state.selected.delete(key);
    const counter = document.querySelector('[data-action="approve"]');
    if (counter) counter.textContent = 'Approve ' + state.selected.size + ' selected';
  }

  const sponsorEl = target.closest('[data-sponsor-pick]');
  if (sponsorEl) {
    const name = sponsorEl.dataset.sponsorPick;
    if (sponsorEl.checked) state.sendSelected.add(name); else state.sendSelected.delete(name);
    const counter = document.querySelector('[data-action="send"]');
    if (counter) counter.textContent = 'Send ' + state.sendSelected.size + ' selected';
  }

  const markEl = target.closest('[data-mark]');
  if (markEl) actionMark(markEl.dataset.mark, markEl.value);
});

const profileSelect = $('#profile-select');
if (profileSelect && profileSelect.addEventListener) {
  profileSelect.addEventListener('change', () => {
    if (profileSelect.value) selectProfile(profileSelect.value);
  });
}

function applyBind(el) {
  if (!state.draft) return;
  const bind = el.dataset.bind;
  let value;
  if (el.type === 'checkbox') {
    value = el.checked;
  } else if (el.dataset.list === 'true') {
    value = el.value.split(/[\n,]+/).map((part) => part.trim()).filter(Boolean);
  } else if (el.type === 'number') {
    value = el.value === '' ? 0 : Number(el.value);
  } else {
    value = el.value;
  }
  setPath(state.draft, bind, value);
  state.dirty = true;
}

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeModal();
  if (event.key !== 'Enter') return;
  const inAuth = event.target.closest && event.target.closest('#auth-root');
  if (!inAuth || event.target.tagName === 'BUTTON') return;
  const submit = {
    setup: 'auth-setup',
    login: 'auth-login',
    totp: 'auth-totp',
    enrol: 'auth-totp-confirm',
  }[state.auth.screen];
  if (!submit) return;
  event.preventDefault();
  const button = document.querySelector('#auth-root [data-action="' + submit + '"]');
  if (button) button.click();
});

/* -------------------------------------------------------------- bootstrap */

async function initApp() {
  try {
    const data = await api('GET', '/api/bootstrap');
    state.version = data.version;
    state.presets = data.presets || [];
    state.profiles = data.profiles || [];
    state.channels = data.channels || ['email', 'forum'];
    state.statuses = data.statuses || state.statuses;
    state.github = data.github || state.github;
    if (state.profiles.length) {
      await selectProfile(state.profiles[0].id);
    } else {
      render();
    }
  } catch (err) {
    if (state.auth.authenticated) {
      $('#main').innerHTML = '<div class="card"><h3>Could not reach the server</h3><p class="hint">' + esc(err.message) + '</p></div>';
    }
  }
}

async function init() {
  await loadAuthState();
  if (state.auth.authenticated) {
    await enterApp();
    return;
  }
  state.auth.screen = state.auth.accountsExist ? 'login' : 'setup';
  renderAuth();
}

if (typeof document !== 'undefined' && document && document.addEventListener) {
  document.addEventListener('DOMContentLoaded', init);
} else {
  // In non-browser environments, run immediately
  init();
}
