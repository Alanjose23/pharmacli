// Shared by the signed-in pages, and the whole of the sign-in page's logic.
// The token lives in sessionStorage -- one tab, gone when it closes -- and
// travels only in the Authorization header, never in a URL the server sees.
'use strict';

const session = {
  get token() { return sessionStorage.getItem('token') || ''; },
  get role() { return sessionStorage.getItem('role') || ''; },
  start(d) { sessionStorage.setItem('token', d.token); sessionStorage.setItem('role', d.role); },
  end() { sessionStorage.clear(); location.replace('/login'); },
};
const home = role => location.replace(role === 'patient' ? '/patient' : '/pharmacist');
const $ = id => document.getElementById(id);

// One call to the API; `json` is sent as a JSON body. An answer that is not ok
// is thrown as an Error carrying the server's own words, its status and body.
async function api(path, {json, ...opts} = {}) {
  const headers = {...(opts.headers || {}), Authorization: 'Bearer ' + session.token};
  if (json !== undefined) {
    headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(json);
  }
  const r = await fetch(path, {...opts, headers});
  const body = await r.json().catch(() => ({}));
  if (r.ok) return body;
  if (r.status === 401 && !path.startsWith('/auth/')) session.end();
  const e = new Error(typeof body.detail === 'string' ? body.detail : `the server said ${r.status}`);
  e.status = r.status;
  e.body = body;
  throw e;
}

// Build an element. Everything dynamic goes in as text -- a patient's name, a
// drug someone typed, a label quote, an upload error -- so none of it can be
// read as HTML.
function el(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, '');
    else if (v !== false && v != null) node.setAttribute(k, v);
  }
  node.append(...kids.flat().filter(k => k != null && k !== false));
  return node;
}

// Replace an element's children. Like el(), it takes arrays and skips what is
// absent: replaceChildren() on its own would print "null" and "[object ...]".
const fill = (node, ...kids) =>
  node.replaceChildren(...kids.flat().filter(k => k != null && k !== false));

if ($('signin')) {
  let once = '';        // the password just typed, kept only to replace a one-time one
  $('signin').addEventListener('submit', async e => {
    e.preventDefault();
    $('signin-error').textContent = '';
    once = $('password').value;
    try {
      const d = await api('/auth/login', {method: 'POST',
                                          json: {username: $('username').value, password: once}});
      session.start(d);
      if (!d.must_change) return home(d.role);
      $('signin').hidden = true;
      $('who').value = $('username').value;     // so a password manager files the new one right
      $('change').hidden = false;
      $('new').focus();
    } catch (err) {
      $('signin-error').textContent = err.message;
      $('password').select();
    }
  });
  $('change').addEventListener('submit', async e => {
    e.preventDefault();
    if ($('new').value !== $('again').value) {
      $('change-error').textContent = 'the two passwords differ';
      return $('again').select();
    }
    try {
      await api('/auth/password', {method: 'POST', json: {current: once, new: $('new').value}});
      home(session.role);
    } catch (err) {
      $('change-error').textContent = err.message;
      $('new').select();
    }
  });
} else if (!session.token) {
  location.replace('/login');
}

$('signout')?.addEventListener('click', async () => {
  try { await api('/auth/logout', {method: 'POST'}); } catch { /* signed out either way */ }
  session.end();
});
