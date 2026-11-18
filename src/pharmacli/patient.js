// The patient's page: their own medication list, the counseling text from each
// label, recall notices, and questions answered with label quotes. Loaded
// after app.js. No pharmacist code is here, and nothing on this page is advice.
'use strict';

if (session.role === 'pharmacist') home('pharmacist');

const failed = (id, e) => fill($(id), el('p', {class: 'error', text: e.message}));
const quote = q => el('blockquote', {}, q.quote, el('cite', {text: q.cite}));

api('/patients/me').then(me => {
  $('hello').textContent = `${me.name}: your medicines`;
  fill($('meds'), ...me.drugs.map(d => el('li', {}, d.name, d.label ? null
    : el('span', {class: 'muted', text: ' · no label on file: ask your pharmacist'}))));
}).catch(e => failed('meds', e));

// section 17 of each label, word for word, exactly as `pharmacli docs handout` prints it
api('/patients/me/counseling').then(d => {
  fill($('counsel'), ...d.counseling.map(c => el('details', {},
    el('summary', {text: c.drug}),
    c.text ? el('pre', {class: 'label-text', text: c.text})
           : el('p', {class: 'muted', text: c.note}))));
}).catch(e => failed('counsel', e));

api('/patients/me/recalls').then(d => {
  fill($('recalls'),
    d.note ? el('p', {class: 'flag', text: d.note}) : null,
    ...d.recalls.map(r => el('article', {class: 'item'},
      el('h3', {text: r.title}), quote(r), el('p', {class: 'muted', text: r.note}))),
    d.recalls.length || d.note ? null : el('p', {text: 'No FDA recall reported in the last 90 '
      + 'days matches the packages on file for your medicines. That describes the recall list, '
      + 'not your own supply: your pharmacist can check your lot.'}));
}).catch(e => failed('recalls', e));

$('ask').addEventListener('submit', async e => {
  e.preventDefault();
  const question = $('q').value.trim();
  if (!question) return;
  const box = $('answer');
  box.setAttribute('aria-busy', 'true');
  fill(box, el('p', {class: 'muted', text: 'Looking in the label…'}));
  try {
    const a = await api('/patients/me/ask', {method: 'POST', json: {question}});
    // a refusal offers to pass the question on; only the patient sends it
    const pass = el('button', {type: 'button', onclick: async () => {
      try {
        await api('/patients/me/forward', {method: 'POST', json: {question}});
        pass.replaceWith(el('p', {role: 'status', text: 'Sent to your pharmacist.'}));
      } catch (err) {
        pass.replaceWith(el('p', {class: 'error', text: err.message}));
      }
    }}, 'Send this question to my pharmacist');
    fill(box, el('p', {text: a.message}), ...a.quotes.map(quote), a.refused ? pass : null);
  } catch (err) {
    failed('answer', err);
  }
  box.setAttribute('aria-busy', 'false');
});
