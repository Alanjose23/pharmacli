import http.server
import json
import re
import socketserver
import urllib.parse
import webbrowser

from pharmacli import ask

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>pharmacli</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     color:#1a1a1a;background:#fafafa;display:flex;justify-content:center}
#app{width:100%;max-width:920px;background:#fff;border:1px solid #e6e6e6;
     min-height:100vh;display:flex;flex-direction:column}
#log{flex:1;overflow-y:auto;padding:24px}
/* the empty state: centred, and the only thing on screen until you ask */
#empty{height:100%;display:flex;flex-direction:column;align-items:center;
       justify-content:center;text-align:center;padding:24px}
h1{font-size:19px;font-weight:600;margin:0 0 8px}
.sub{color:#8a8a8a;max-width:400px;margin:0 0 28px}
.try{color:#8a8a8a;font-size:13px;margin-bottom:12px}
#pills{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;max-width:620px}
.pill{background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:9px 14px;
      cursor:pointer;font:inherit;color:#1a1a1a}
.pill:hover{border-color:#b8b8b8;background:#fafafa}
/* answers are column-aligned ASCII -- monospace or the tables break */
.msg{margin-bottom:18px}
.you{text-align:right}
.you span{background:#f0f0f0;border-radius:12px;padding:8px 14px;display:inline-block}
.bot pre{font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;
         white-space:pre-wrap;margin:0;padding:12px 14px;background:#fafafa;
         border:1px solid #eee;border-radius:8px;overflow-x:auto}
.label{color:#8a8a8a;font-size:12px;margin-bottom:6px}
/* the composer */
form{padding:16px 24px 22px;display:flex;gap:8px}
#q{flex:1;background:#f5f5f5;border:1px solid #ececec;border-radius:10px;
   padding:13px 16px;font:inherit;outline:none}
#q:focus{border-color:#c8c8c8;background:#fff}
button.send{background:none;border:none;cursor:pointer;color:#9a9a9a;padding:0 8px}
button.send:hover{color:#1a1a1a}
</style></head><body><div id="app">
<div id="log"><div id="empty">
  <h1>PharmaCLI chat</h1>
  <p class="sub">Here are some suggestions:</p>
  <div class="try">Try these prompts:</div>
  <div id="pills"></div>
</div></div>
<form id="f"><input id="q" placeholder="Ask Something" autocomplete="off" autofocus>
<button class="send" type="submit" aria-label="Send">
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
       stroke-width="1.6"><path d="M4 12l16-8-6 8 6 8z"/></svg>
</button></form></div>
<script>
const log = document.getElementById('log'), qbox = document.getElementById('q');

// the pills ARE the registry -- fetched, never hardcoded, so a new question in
// QUESTIONS shows up here with no edit to this page
fetch('/prompts').then(r => r.json()).then(ls => {
  document.getElementById('pills').innerHTML = ls.slice(0, 6)
    .map(l => `<button class="pill">${l}</button>`).join('');
  document.querySelectorAll('.pill').forEach(
    b => b.onclick = () => send(b.textContent));
});

function add(cls, html) {
  document.getElementById('empty')?.remove();
  const d = document.createElement('div');
  d.className = 'msg ' + cls; d.innerHTML = html;
  log.appendChild(d); log.scrollTop = log.scrollHeight;
  return d;
}
const esc = s => s.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

async function send(q) {
  if (!q.trim()) return;
  qbox.value = '';
  add('you', `<span>${esc(q)}</span>`);
  const pending = add('bot', '<pre>...</pre>');
  try {
    const r = await fetch('/ask?q=' + encodeURIComponent(q));
    const d = await r.json();
    pending.innerHTML = (d.label ? `<div class="label">· ${esc(d.label)}</div>` : '')
                      + `<pre>${esc(d.text)}</pre>`;
  } catch (e) {
    pending.innerHTML = `<pre>the server went away: ${esc(String(e))}</pre>`;
  }
  log.scrollTop = log.scrollHeight;
}
document.getElementById('f').onsubmit = e => { e.preventDefault(); send(qbox.value); };
</script></body></html>"""


def _args_from(q: str, label: str) -> dict:
    kw = {}
    m = re.search(r"\d[\d\s-]{9,}\d", q)
    if m:
        kw["ndc"] = re.sub(r"\D", "", m.group())
    lab = {w.lower() for w in label.split()}
    rest = " ".join(w for w in q.split() if w.lower() not in lab).strip()
    kw["phrase"] = rest or q
    # kw["topic"] = q                     # explain wants the whole question  # off: model/why questions -- uncomment to restore
    return kw


class Handler(http.server.BaseHTTPRequestHandler):
    db = None

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urllib.parse.urlparse(self.path)
        if url.path == "/":
            return self._send(PAGE.encode(), "text/html; charset=utf-8")
        if url.path == "/prompts":
            labels = [lab for lab, _, _ in ask.QUESTIONS]
            return self._send(json.dumps(labels).encode(), "application/json")
        if url.path == "/ask":
            q = urllib.parse.parse_qs(url.query).get("q", [""])[0]
            # match() once, here, only to strip the label's own words out of the
            # phrase arg; answer() hands the label back so it is not re-derived
            ranked = ask.match(q)
            guess = ranked[0][1] if ranked and ranked[0][0] >= ask.CUTOFF else ""
            label, lines = ask.answer(q, db=self.db, **_args_from(q, guess))
            body = {"label": label, "text": "\n".join(lines)}
            return self._send(json.dumps(body).encode(), "application/json")
        self.send_error(404)

    def log_message(self, *a):            # one line per keystroke is noise
        pass


def serve(db=None, port: int = 8765, open_browser: bool = True) -> None:
    Handler.db = db
    # 127.0.0.1, never 0.0.0.0: no auth, and it answers with your acquisition costs
    with socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler) as srv:
        url = f"http://127.0.0.1:{port}"
        print(f"pharmacli chat on {url}   (ctrl-c to stop)")
        if open_browser:
            webbrowser.open(url)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print()
