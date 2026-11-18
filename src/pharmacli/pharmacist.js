// The pharmacist's page. Loaded after app.js, which holds the token, api() and
// el(). Nothing here is sent to a patient's browser.
'use strict';

if (session.role === 'patient') home('patient');

const LABELS = {systolic: 'Systolic BP', diastolic: 'Diastolic BP', egfr: 'eGFR',
                crcl: 'Creatinine clearance', alt: 'ALT', ast: 'AST', potassium: 'Serum potassium',
                weight_kg: 'Weight', pregnant: 'Pregnant', allergies: 'Allergies'};
// severity is carried by the words and the order; the colour only repeats it
const SEVERE = {'Needs manual review': 'sev-high', 'Do not use together': 'sev-high',
                'Avoid or adjust the dose': 'sev-mid', 'Monitor': 'sev-mid', 'Recall': 'sev-mid'};
let bounds = {};        // unit and limits for each number, as the server checks them
let words = {};         // the words a drug name is matched on, as the server matches them
let suggest = [];       // every name that has a label
let current = null;     // the patient on screen

const table = (heads, rows) => el('table', {},
  el('thead', {}, el('tr', {}, heads.map(h => el('th', {scope: 'col', text: h})))),
  el('tbody', {}, rows.map(r => el('tr', {}, r.map(c => el('td', {}, c))))));
const shown = (k, v) => v + (bounds[k] ? ' ' + bounds[k].unit : '');

// One labelled field with room for its error. The error is tied to the input
// for a screen reader; fail() fills it in and marks the input invalid.
function field(id, label, input) {
  input.id = id;
  input.setAttribute('aria-describedby', id + '-e');
  return el('div', {class: 'field'}, el('label', {for: id, text: label}), input,
            el('p', {id: id + '-e', class: 'error', role: 'alert'}));
}

// The server's {field: reason}, each next to its field, focus on the first.
// The server is the authority; the min/max on the inputs only mirror it.
// Returns whatever had no field of its own, for the caller to show.
function fail(prefix, errors) {
  let first = null;
  const rest = [];
  for (const [k, why] of Object.entries(errors)) {
    const input = $(prefix + k);
    if (!input) { rest.push(why); continue; }
    input.setAttribute('aria-invalid', 'true');
    $(prefix + k + '-e').textContent = why;
    first = first || input;
  }
  if (first) first.focus();
  return rest.join('; ');
}

function clearErrors(form) {
  form.querySelectorAll('[aria-invalid]').forEach(i => i.removeAttribute('aria-invalid'));
  form.querySelectorAll('.error').forEach(p => { p.textContent = ''; });
}

// the indicator inputs, used wherever readings are entered
function indicatorFields(prefix) {
  const today = new Date().toISOString().slice(0, 10);
  return [
    ...Object.keys(bounds).map(k => field(prefix + k, `${LABELS[k]} (${bounds[k].unit})`,
      el('input', {type: 'number', step: 'any', min: bounds[k].low, max: bounds[k].high}))),
    field(prefix + 'pregnant', 'Pregnant', el('select', {},
      ['', 'yes', 'no'].map(v => el('option', {value: v, text: v || 'not recorded'})))),
    field(prefix + 'allergies', 'Allergies', el('input', {maxlength: 500})),
    field(prefix + 'taken', 'Reading date', el('input', {type: 'date', max: today})),
  ];
}
const filled = prefix => Object.fromEntries([...Object.keys(LABELS), 'taken']
  .map(k => [k, $(prefix + k) ? $(prefix + k).value.trim() : '']).filter(([, v]) => v));

// ── the list ────────────────────────────────────────────────────────────────
async function listPatients() {
  const d = await api('/patients');
  ({bounds, words, suggest} = d);
  const {accepted, overridden} = d.decisions;
  $('rate').textContent = accepted + overridden
    ? `Decisions: ${accepted} accepted, ${overridden} overridden `
      + `(${Math.round(100 * overridden / (accepted + overridden))}% overridden)`
    : 'No decisions recorded yet';
  fill($('names'), ...suggest.map(n => el('option', {value: n})));
  if (!$('n-indicators').children.length) $('n-indicators').append(...indicatorFields('n-'));
  fill($('patients'), ...d.patients.map(p => el('li', {},
    el('button', {class: 'pick', type: 'button', 'data-id': p.id,
                  'aria-current': p.id === current ? 'true' : null, onclick: () => show(p.id)},
       el('strong', {text: p.name}),
       ` · born ${p.dob} · ${p.drugs} drug${p.drugs === 1 ? '' : 's'}`))));
  if (!d.patients.length) {
    $('patients').append(el('li', {class: 'muted',
      text: 'No patients yet. Add one above, upload a CSV, or load the mock patients.'}));
  }
}

async function listQuestions() {
  const d = await api('/patients/questions');
  fill($('questions'), ...d.questions.map(q => el('li', {},
    el('button', {class: 'pick', type: 'button', onclick: () => show(q.patient)},
       el('strong', {text: q.name}), ` · ${q.at.slice(0, 10)}`),
    el('p', {text: q.text}))));
  if (!d.questions.length) $('questions').append(el('li', {class: 'muted', text: 'None waiting.'}));
}

function uploaded(d) {
  fill($('uploaded'), 
    el('p', {text: `${d.created} created, ${d.updated} updated, ${d.rejected.length} rejected. `
                 + 'Interaction checks for their drugs are queued.'}),
    d.rejected.length ? el('ul', {}, d.rejected.map(r =>
      el('li', {text: `line ${r.line}: ${r.reason}`}))) : null);
  return listPatients();
}
const uploadFailed = e => fill($('uploaded'), el('p', {class: 'error', text: e.message}));

$('file').addEventListener('change', async () => {
  const file = $('file').files[0];
  if (!file) return;
  try {
    await uploaded(await api('/patients/upload', {method: 'POST', body: await file.text()}));
  } catch (e) { uploadFailed(e); }
  $('file').value = '';
});
$('mock').addEventListener('click', () =>
  api('/patients/mock', {method: 'POST'}).then(uploaded).catch(uploadFailed));

// ── one profile ─────────────────────────────────────────────────────────────
async function show(pid) {
  current = pid;
  fill($('report'));
  draw(await api('/patients/' + pid));
  document.querySelectorAll('#patients .pick').forEach(b =>
    b.dataset.id === pid ? b.setAttribute('aria-current', 'true') : b.removeAttribute('aria-current'));
  $('profile-h').focus();
  loadReport();
}

// an edit to the open profile: redraw it, or put the server's reasons by the fields
async function edit(prefix, method, path, json) {
  clearErrors($('profile'));
  try {
    draw(await api(`/patients/${current}${path}`, {method, json}));
    listPatients();
    loadReport();
  } catch (e) {
    const errors = e.body && e.body.detail && e.body.detail.errors;
    $('profile-error').textContent = errors ? fail(prefix, errors) : e.message;
  }
}

function draw(p) {
  $('profile').hidden = false;
  $('profile-h').textContent = p.name;

  const drug = el('form', {class: 'row'},
    field('d-drug', 'Add a drug', el('input', {list: 'names', maxlength: 80, required: true})),
    el('button', {type: 'submit'}, 'Add'));
  drug.addEventListener('submit', e => {
    e.preventDefault();
    edit('d-', 'POST', '/drugs', {name: $('d-drug').value});
  });

  const reading = el('form', {},
    el('div', {class: 'grid'}, indicatorFields('r-')),
    el('button', {type: 'submit'}, 'Add reading'));
  reading.addEventListener('submit', e => {
    e.preventDefault();
    edit('r-', 'POST', '/readings', filled('r-'));
  });

  // a pharmacist creates each patient's login; its password works once
  const login = el('form', {class: 'row'},
    field('l-username', p.login ? `Reset the login of ${p.login}` : 'Username for a patient login',
          el('input', {value: p.login || '', maxlength: 32, autocomplete: 'off', required: true})),
    el('button', {type: 'submit'}, p.login ? 'Reset login' : 'Create login'));
  login.addEventListener('submit', async e => {
    e.preventDefault();
    clearErrors($('profile'));
    try {
      const d = await api(`/patients/${current}/login`,
                          {method: 'POST', json: {username: $('l-username').value}});
      $('l-once').textContent = `${d.username} can sign in once with: ${d.one_time_password} `
        + '-- hand it over now. It is not shown again, and the first sign-in must replace it.';
    } catch (err) {
      const errors = err.body && err.body.detail && err.body.detail.errors;
      $('profile-error').textContent = errors ? fail('l-', errors) : err.message;
    }
  });

  const now = Object.keys(LABELS).filter(k => p.current[k]).map(k => [k, p.current[k]]);
  fill($('profile-body'), 
    el('p', {class: 'muted', text: `Born ${p.dob} · `
      + (p.login ? `signs in as ${p.login}` : 'no patient login')}),
    el('h3', {text: 'Drugs'}),
    el('ul', {class: 'plain'}, p.drugs.map(d => el('li', {class: 'row'},
      el('span', {}, d.name, ' · ', d.label ? `label: ${d.label}`
        : el('strong', {class: 'flag', text: 'no label, not checked'})),
      el('button', {type: 'button', 'aria-label': `Stop ${d.name}`,
                    onclick: () => edit('d-', 'DELETE', `/drugs/${d.id}`)}, 'Stop')))),
    drug,
    el('h3', {text: 'Indicators now'}),
    now.length ? table(['Indicator', 'Value', 'Taken', 'Entered by'], now.map(([k, r]) =>
      [LABELS[k] || k, shown(k, r.value), r.taken, r.entered_by]))
      : el('p', {class: 'muted', text: 'No readings yet.'}),
    el('details', {},
      el('summary', {text: 'Add a reading, see the history, or give the patient a login'}),
      el('p', {class: 'muted', text: 'A correction is a new reading: nothing is overwritten.'}),
      reading,
      el('h3', {text: 'History'}),
      table(['Taken', 'Indicator', 'Value', 'Entered', 'By'], p.history.map(r =>
        [r.taken, LABELS[r.indicator] || r.indicator, shown(r.indicator, r.value),
         r.entered.slice(0, 10), r.entered_by])),
      el('h3', {text: 'Patient login'}),
      login,
      el('p', {id: 'l-once', role: 'status'})));
}

// ── the report: every item cited, each one accepted or overridden ──────────
async function loadReport() {
  const pid = current;
  try {
    const r = await api(`/patients/${pid}/report`);
    if (pid === current) drawReport(r);            // not if another patient was opened meanwhile
  } catch (e) {
    if (pid === current) fill($('report'), el('p', {class: 'error', text: e.message}));
  }
}

function drawReport(r) {
  fill($('report'), 
    el('h3', {text: 'Report'}),
    el('p', {}, el('strong', {text: r.summary})),
    el('p', {class: 'muted', text: 'Each item quotes a label, a NADAC file or an FDA recall. '
      + 'None is a clinical decision: accept or override each one.'}),
    r.notes.length ? el('ul', {class: 'flag'}, r.notes.map(n => el('li', {text: n}))) : null,
    r.items.map(reportItem));
}

function reportItem(i) {
  const box = el('article', {class: 'item'},
    el('h4', {}, el('span', {class: SEVERE[i.status] || '', text: i.status}), ' · ', i.title),
    i.note ? el('p', {text: i.note}) : null,
    i.quotes.map(q => el('blockquote', {},
      q.category ? el('strong', {text: q.category + ': '}) : null, q.quote,
      el('cite', {text: q.cite}))),
    !i.quotes.length && i.searched
      ? el('p', {class: 'muted', text: 'Searched: ' + i.searched.join('; ')}) : null);
  if (!i.key) return box;                          // pending or not checked: nothing to decide

  const made = i.decision;
  const why = el('input', {id: 'why-' + i.key, maxlength: 500});
  const decide = verdict => api(`/patients/${current}/decisions`,
      {method: 'POST', json: {item: i.key, verdict, reason: why.value}})
    .then(r => {
      drawReport(r);
      listPatients();
      const again = $('why-' + i.key);             // keep the keyboard where it was
      if (again) again.focus();
    })
    .catch(e => { $('profile-error').textContent = e.message; });
  box.append(
    el('p', {class: 'muted', text: made
      ? `${made.verdict} by ${made.by} on ${made.at.slice(0, 10)}`
        + (made.reason ? `: ${made.reason}` : '')
      : 'Not decided yet'}),
    el('div', {class: 'row'},
      el('div', {class: 'field'}, el('label', {for: 'why-' + i.key, text: 'Reason (optional)'}), why),
      el('button', {type: 'button', onclick: () => decide('accepted')}, 'Accept'),
      el('button', {type: 'button', onclick: () => decide('overridden')}, 'Override')));
  return box;
}

// ── the new-patient bar ─────────────────────────────────────────────────────
const typed = [];                  // the drugs entered so far
let firstKey = 0;                  // when typing began, and how many times Save was pressed:
let attempts = 0;                  // what the form reports about itself, nothing about the patient

// The server's matching, mirrored, so a name with no label is flagged as it is
// typed. The server decides when the patient is saved.
function hasLabel(name) {
  const flat = name.toLowerCase().trim().replace(/\s+/g, ' ');
  if (suggest.includes(flat)) return true;
  const found = new Set((flat.match(/[a-z0-9]+/g) || []).map(w => words[w]).filter(Boolean));
  return found.size === 1;
}

function drawTyped() {
  fill($('n-list'), ...typed.map((name, i) => el('li', {class: 'row'},
    el('span', {}, name, hasLabel(name) ? null
      : el('strong', {class: 'flag', text: ' · no label: saved as not checked'})),
    el('button', {type: 'button', 'aria-label': `Remove ${name}`, onclick: () => {
      typed.splice(i, 1);
      drawTyped();
      $('n-drugs').focus();
    }}, 'Remove'))));
}

function addTyped() {
  const name = $('n-drugs').value.trim().replace(/\s+/g, ' ');
  if (!name) return;
  if (!typed.some(t => t.toLowerCase() === name.toLowerCase())) typed.push(name);
  $('n-flag').textContent = hasLabel(name) ? ''
    : `"${name}" has no label on file. It will be saved, and reported as not checked.`;
  $('n-drugs').value = '';
  drawTyped();
}

$('n-drugs').addEventListener('keydown', e => {
  if (e.key !== 'Enter') return;
  e.preventDefault();                              // Enter adds the drug; it does not save yet
  addTyped();
});
$('new').addEventListener('input', () => { firstKey = firstKey || performance.now(); });

async function savePatient(confirm) {
  addTyped();                                      // a drug still in the box counts
  clearErrors($('new'));
  $('n-confirm').hidden = true;
  if (!confirm) attempts += 1;
  const entry = {seconds: firstKey ? (performance.now() - firstKey) / 1000 : 0,
                 attempt: confirm ? 0 : attempts};
  try {
    const p = await api('/patients', {method: 'POST', json: {
      name: $('n-name').value, dob: $('n-dob').value, drugs: typed, ...filled('n-'), confirm,
      entry}});
    $('new').reset();
    typed.length = 0;
    firstKey = attempts = 0;
    $('n-flag').textContent = '';
    drawTyped();
    await listPatients();
    show(p.id);
  } catch (e) {
    const detail = e.body && e.body.detail;
    if (e.status === 409) {
      // the same name and date of birth is on file: only a pharmacist can say it is someone else
      $('n-error').textContent = detail.message;
      $('n-confirm').hidden = false;
      $('n-confirm').focus();
    } else {
      $('n-error').textContent = detail && detail.errors ? fail('n-', detail.errors) : e.message;
    }
  }
}
$('new').addEventListener('submit', e => { e.preventDefault(); savePatient(false); });
$('n-confirm').addEventListener('click', () => savePatient(true));

listPatients().catch(e => fill($('patients'), el('li', {class: 'error', text: e.message})));
listQuestions().catch(e => fill($('questions'), el('li', {class: 'error', text: e.message})));
