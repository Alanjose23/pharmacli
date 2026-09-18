import http.server
import json
import os
import pathlib
import re
import socketserver
import urllib.parse
import webbrowser

from pharmacli import ask

# The page itself lives next to this module as chat.html, so it stays HTML in
# an editor instead of a 147-line Python string
PAGE = (pathlib.Path(__file__).with_name("chat.html")
        .read_text(encoding="utf-8"))


def _args_from(q: str, label: str) -> dict:
    kw = {}
    m = ask.NDC.search(q)
    if m:
        kw["ndc"] = re.sub(r"\D", "", m.group())
    lab = {w.lower() for w in label.split()}
    rest = " ".join(w for w in q.split() if w.lower() not in lab).strip()
    kw["phrase"] = rest or q
    return kw


class Handler(http.server.BaseHTTPRequestHandler):
    db = None
    # ponytail: one upload slot, because `chat` binds to 127.0.0.1 for one local
    # user. Add a session id the day it serves more than one person.
    upload = None
    MAX_UPLOAD = 5 * 1024 * 1024          # a 5,000-line inventory CSV is ~200KB

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, body) -> None:
        self._send(json.dumps(body).encode(), "application/json")

    def do_POST(self) -> None:
        url = urllib.parse.urlparse(self.path)
        if url.path == "/upload":
            return self._upload()
        if url.path == "/save":
            return self._save()
        if url.path == "/clear":
            type(self).upload = None
            return self._json({"ok": True})
        self.send_error(404)

    def _upload(self) -> None:
        import tempfile

        from pharmacli.cli import read_inventory

        size = int(self.headers.get("Content-Length") or 0)
        name = self.headers.get("X-Filename") or "inventory.csv"
        if size > self.MAX_UPLOAD:
            return self._json({"name": name, "lines": 0, "skipped": 0, "reasons": [],
                               "suggest": [],
                               "error": f"file is larger than {self.MAX_UPLOAD // 2**20} MB"})
        with tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False) as fh:
            fh.write(self.rfile.read(size))
            path = fh.name
        try:
            rows, skipped = read_inventory(path)
        except OSError as exc:
            return self._json({"name": name, "lines": 0, "skipped": 0, "reasons": [],
                               "suggest": [], "error": f"cannot read the file: {exc}"})
        finally:
            os.remove(path)

        reasons = [f"line {line}: {why}" for line, why in skipped[:5]]
        if not rows:
            type(self).upload = None
            return self._json({"name": name, "lines": 0, "skipped": len(skipped),
                               "reasons": reasons, "suggest": [],
                               "error": "no usable ndc,cost rows"})
        shelf = ({n: c for n, c, _, _ in rows}, {n: (oh, mo) for n, _, oh, mo in rows})
        type(self).upload = {"name": name, "rows": rows, "shelf": shelf}
        return self._json({"name": name, "lines": len(rows), "skipped": len(skipped),
                           "reasons": reasons,
                           "suggest": ask.suggestions(self.db, shelf)})

    def _save(self) -> None:
        from pharmacli.config import load
        from pharmacli.store import connect, watch_cost

        held = type(self).upload
        if not held:
            return self._json({"error": "nothing attached"})
        conn = connect(self.db or load().db)
        try:
            changed = watch_cost(conn, held["rows"])
        finally:
            conn.close()
        type(self).upload = None          # the shelf holds it now; answer from there
        return self._json({"saved": len(held["rows"]), "changed": changed})

    def do_GET(self) -> None:
        url = urllib.parse.urlparse(self.path)
        if url.path == "/":
            return self._send(PAGE.encode(), "text/html; charset=utf-8")
        if url.path == "/prompts":
            return self._json([lab for lab, _, _ in ask.QUESTIONS])
        if url.path == "/ask":
            q = urllib.parse.parse_qs(url.query).get("q", [""])[0]
            # match() once, here, only to strip the label's own words out of the
            # phrase arg; answer() hands the label back so it is not re-derived
            ranked = ask.match(q)
            guess = ranked[0][1] if ranked and ranked[0][0] >= ask.CUTOFF else ""
            held = type(self).upload
            label, lines = ask.answer(q, db=self.db, shelf=held and held["shelf"],
                                      **_args_from(q, guess))
            text = "\n".join(lines)
            if held and label:
                text = f"from {held['name']} (not saved)\n\n{text}"
            return self._json({"label": label, "text": text})
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
