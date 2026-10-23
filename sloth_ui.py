"""Local web UI for Downloads Sloth. Zero dependencies (standard library only).

    python sloth.py ui            # opens http://127.0.0.1:8765

It reads the same SQLite log and config as the command line, and it runs the
same Sloth code: nothing here sorts files differently from `sloth.py once`.
The server only listens on 127.0.0.1 and rejects requests from other sites.
"""
import json
import sqlite3
import sys
import threading
import time
import webbrowser
from collections import deque
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG = deque(maxlen=400)
LOCK = threading.Lock()
JOB = {"running": False, "label": "", "started": None, "ended": None, "failed": False}

PILE_IGNORE = {"desktop.ini", "thumbs.db", "sloth.py", "sloth_ui.py", "config.toml",
               "requirements.txt", "sloth.db", "readme.md"}
PILE_SKIP_SUFFIX = {".crdownload", ".part", ".tmp", ".download", ".partial"}


class Tee:
    """Copy everything the sloth prints into the on-page console, line by line."""
    def __init__(self, real):
        self.real, self.buf = real, ""

    def write(self, s):
        if self.real is not None:
            try:
                self.real.write(s)
            except Exception:
                pass
        with LOCK:
            self.buf += s
            while "\n" in self.buf:
                line, self.buf = self.buf.split("\n", 1)
                LOG.append(line)
        return len(s)

    def flush(self):
        if self.real is not None:
            try:
                self.real.flush()
            except Exception:
                pass

    def isatty(self):
        return False


# ------------------------------------------------------------------ state
def _connect(db_path):
    if not db_path.exists():
        return None
    try:
        return sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    except Exception:
        return None


def build_state(cfg):
    f, beh, llm = cfg["folders"], cfg["behavior"], cfg["llm"]
    watch, dest = f["watch"], f["dest"]
    state = {
        "config": {"watch": str(watch), "dest": str(dest), "model": llm["model"],
                   "cap": llm["max_requests_per_day"], "dry_default": beh["dry_run"],
                   "review": beh["review_category"]},
        "pile": [], "shelves": {}, "feed": [], "watch_missing": not watch.exists(),
        "stats": {"model": 0, "rule": 0, "private": 0, "dup": 0, "review": 0,
                  "calls": 0, "tin": 0, "tout": 0},
    }
    preds, rows = {}, []
    con = _connect(f["db"])
    if con is not None:
        try:
            for src, cat, conf in con.execute(
                    "SELECT src,category,confidence FROM moves WHERE status='dry' ORDER BY id"):
                preds[src] = (cat, conf)
            rows = con.execute(
                "SELECT src,dst,category,summary,confidence,status,ts FROM moves "
                "WHERE status!='undone' ORDER BY id DESC LIMIT 400").fetchall()
            today = date.today().isoformat()
            for st, summ in con.execute(
                    "SELECT status,summary FROM moves WHERE ts LIKE ? "
                    "AND status IN ('moved','duplicate','review')", (today + "%",)):
                s = state["stats"]
                if st == "duplicate":
                    s["dup"] += 1
                elif st == "review":
                    s["review"] += 1
                elif summ.startswith("Matched a private-name"):
                    s["private"] += 1
                elif summ.startswith("Sorted by extension"):
                    s["rule"] += 1
                else:
                    s["model"] += 1
            # API calls follow the UTC day, like OpenRouter's counter
            utc0 = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            local0 = utc0.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
            calls = con.execute(
                "SELECT COUNT(*) FROM moves WHERE ts>=? AND prompt_tokens>0", (local0,)).fetchone()[0]
            tin, tout = con.execute(
                "SELECT COALESCE(SUM(prompt_tokens),0),COALESCE(SUM(completion_tokens),0) "
                "FROM moves WHERE ts LIKE ?", (today + "%",)).fetchone()
            state["stats"].update(calls=calls, tin=tin, tout=tout)
        except Exception:
            pass
        finally:
            con.close()

    seen = set()
    for src, dst, cat, summ, conf, st, ts in rows:
        if src in seen:
            continue
        seen.add(src)
        if len(state["feed"]) < 80:
            state["feed"].append({
                "orig": Path(src).name, "new": Path(dst).name, "category": cat,
                "summary": summ or "", "conf": conf or 0, "status": st, "ts": ts})

    if watch.exists():
        try:
            for p in sorted(watch.iterdir(), key=lambda x: x.name.lower()):
                if (not p.is_file() or p.name.startswith((".", "~$"))
                        or p.name.lower() in PILE_IGNORE
                        or p.suffix.lower() in PILE_SKIP_SUFFIX):
                    continue
                pred = preds.get(str(p))
                state["pile"].append({
                    "name": p.name, "size": p.stat().st_size,
                    "pred": pred[0] if pred else None, "conf": pred[1] if pred else None})
        except OSError:
            pass
    if dest.exists():
        n = 0
        for p in dest.rglob("*"):
            n += 1
            if n > 3000:
                break
            if not p.is_file() or p.name == "sloth.db" or p.name.startswith("_digest_") \
                    or p.suffix == ".db-journal":
                continue
            parts = p.relative_to(dest).parts[:-1]
            state["shelves"].setdefault("/".join(parts) or "(top level)", []).append(p.name)
        state["shelves"] = {k: sorted(v, key=str.lower)
                            for k, v in sorted(state["shelves"].items())}
    with LOCK:
        state["job"] = dict(JOB)
        state["log"] = list(LOG)
    return state


# ------------------------------------------------------------------ jobs
def start_job(cfg, Sloth, client_factory, body):
    action = body.get("action")
    live = bool(body.get("live"))
    try:
        limit = int(body["limit"]) if body.get("limit") not in (None, "") else None
        last = int(body.get("last") or 1)
    except (TypeError, ValueError):
        return False, "limit and last must be whole numbers"
    if limit is not None and not 1 <= limit <= 5000:
        return False, "limit must be between 1 and 5000"
    if not 1 <= last <= 1000:
        return False, "last must be between 1 and 1000"
    labels = {"once": "Sorting for real" if live else "Preview (dry run)",
              "undo": "Undoing", "digest": "Writing digest", "check": "Checking key and limits"}
    if action not in labels:
        return False, "unknown action"
    review_only = bool(body.get("review"))
    with LOCK:
        if JOB["running"]:
            return False, "A job is already running."
        LOG.clear()
        JOB.update(running=True, label=labels[action], started=time.time(),
                   ended=None, failed=False)

    def work():
        s = None
        try:
            client = client_factory() if client_factory else None
            s = Sloth(cfg, dry_run=(not live) if action == "once" else True, client=client)
            if action == "once":
                s.once(limit)
            elif action == "undo":
                s.undo(last, review_only)
            elif action == "digest":
                s.digest()
            else:
                s.check()
        except SystemExit as e:        # sloth.py uses sys.exit("message") for setup problems
            print(f"STOPPED: {e}")
            JOB["failed"] = True
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}")
            JOB["failed"] = True
        finally:
            if s is not None:
                try:
                    s.db.close()
                except Exception:
                    pass
            with LOCK:
                JOB.update(running=False, ended=time.time())

    threading.Thread(target=work, daemon=True).start()
    return True, "started"


# ------------------------------------------------------------------ server
def make_handler(ctx):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _trusted(self):
            port = ctx["port"]
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            origin = self.headers.get("Origin")
            return self.headers.get("Host", "") in hosts and (
                origin is None or origin in {"http://" + h for h in hosts})

        def _send(self, code, payload, ctype="application/json; charset=utf-8"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._trusted():
                return self._send(403, {"error": "forbidden"})
            if self.path in ("/", "/index.html"):
                return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            if self.path == "/api/state":
                return self._send(200, build_state(ctx["cfg"]))
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._trusted():
                return self._send(403, {"error": "forbidden"})
            if self.path != "/api/run":
                return self._send(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(min(n, 10_000)) or b"{}")
            except Exception:
                return self._send(400, {"error": "bad request"})
            ok, msg = start_job(ctx["cfg"], ctx["Sloth"], ctx["client_factory"], body)
            self._send(200 if ok else 409, {"ok": ok, "message": msg})

    return Handler


def serve(cfg, Sloth, port=8765, open_browser=True, client_factory=None, block=True):
    ctx = {"cfg": cfg, "Sloth": Sloth, "client_factory": client_factory, "port": port}
    server = None
    for p in range(port, port + 10):
        ctx["port"] = p
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), make_handler(ctx))
            break
        except OSError:
            continue
    if server is None:
        sys.exit(f"Could not open a port between {port} and {port + 9}.")
    if not isinstance(sys.stdout, Tee):
        sys.stdout = Tee(sys.stdout)
    url = f"http://127.0.0.1:{ctx['port']}"
    print(f"Sloth UI running at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    if not block:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, url
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()


# ------------------------------------------------------------------ page
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Downloads Sloth</title>
<style>
:root{
  --fog:#E6EBE3; --paper:#F3F5EF; --bark:#33291F; --bark-2:#625A4F;
  --moss:#47663C; --moss-ink:#F3F5EF; --lichen:#CBD9AC; --rule:#C6CEBF; --hibiscus:#B03350;
  --serif:Georgia,"Palatino Linotype","Book Antiqua",serif;
  --sans:"Segoe UI",system-ui,-apple-system,Roboto,sans-serif;
  --mono:Consolas,"Cascadia Mono","SF Mono",Menlo,monospace;
}
@media (prefers-color-scheme:dark){
  :root{--fog:#1B2019; --paper:#232920; --bark:#E4E8DC; --bark-2:#9CA593;
        --moss:#8EB27B; --moss-ink:#1B2019; --lichen:#3F5132; --rule:#384133; --hibiscus:#E57D92;}
}
*{box-sizing:border-box}
html{background:var(--fog)}
body{margin:0;color:var(--bark);font:15px/1.5 var(--sans);background:var(--fog)}
.wrap{max-width:1180px;margin:0 auto;padding:28px 28px 64px}
h1{font:400 36px/1.1 var(--serif);letter-spacing:-.01em;margin:0}
h2{font:400 20px/1.2 var(--serif);margin:0 0 4px}
.tag{color:var(--bark-2);margin:6px 0 0;max-width:60ch}
.mono{font-family:var(--mono);font-size:13px}
.muted{color:var(--bark-2)}
header{display:flex;gap:24px;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;
  padding-bottom:18px;border-bottom:1px solid var(--rule)}
.paths{font:12.5px/1.6 var(--mono);color:var(--bark-2);text-align:right;max-width:46ch;overflow-wrap:anywhere}
.bar{display:flex;flex-wrap:wrap;gap:10px 22px;align-items:center;padding:14px 0;border-bottom:1px solid var(--rule)}
.grp{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.grp label{color:var(--bark-2);font-size:13px}
input[type=number]{width:64px;padding:6px 8px;border:1px solid var(--rule);border-radius:4px;
  background:var(--paper);color:var(--bark);font:inherit}
button{font:inherit;padding:7px 14px;border-radius:4px;border:1px solid var(--bark-2);
  background:transparent;color:var(--bark);cursor:pointer}
button:hover:not(:disabled){background:var(--paper)}
button.primary{background:var(--moss);color:var(--moss-ink);border-color:var(--moss)}
button.primary:hover:not(:disabled){background:var(--moss);filter:brightness(1.1)}
button.live{border-color:var(--hibiscus);color:var(--hibiscus)}
button:disabled{opacity:.45;cursor:default}
:focus-visible{outline:2px solid var(--moss);outline-offset:2px}
.sep{width:1px;align-self:stretch;background:var(--rule)}
#status{padding:12px 0;min-height:48px}
#status strong{font-weight:600}
#status .bad{color:var(--hibiscus)}
.summary{margin:0;max-width:78ch}
.cols{display:grid;grid-template-columns:minmax(0,5fr) minmax(0,6fr);border-top:1px solid var(--rule);
  border-bottom:1px solid var(--rule)}
.col{padding:18px 22px 22px 0;min-width:0}
.col+.col{padding:18px 0 22px 22px;border-left:1px solid var(--rule)}
.col .sub{color:var(--bark-2);font-size:13px;margin:0 0 12px}
ul.files{list-style:none;margin:0;padding:0}
ul.files li{display:flex;justify-content:space-between;gap:12px;padding:4px 6px;margin:0 -6px;border-radius:3px}
ul.files li .n{overflow-wrap:anywhere}
ul.files li .r{white-space:nowrap;color:var(--bark-2);font-size:12.5px}
.fresh{animation:fresh 3s ease-out}
@keyframes fresh{from{background:var(--lichen)}to{background:transparent}}
.shelf{margin:0 0 14px}
.shelf h3{font:600 14px/1.3 var(--sans);margin:0 0 2px;display:flex;justify-content:space-between;gap:12px}
.shelf h3 .c{color:var(--bark-2);font-weight:400}
.shelf.attn h3{color:var(--hibiscus)}
.more{color:var(--bark-2);font-size:12.5px;padding:2px 0}
.empty{color:var(--bark-2);font-style:italic;margin:6px 0}
section.feedwrap{padding-top:22px}
.row{display:grid;grid-template-columns:104px minmax(0,1fr) 150px;gap:6px 16px;padding:11px 0;border-bottom:1px solid var(--rule)}
.state{font-size:13px;display:flex;align-items:flex-start;gap:8px}
.mark{width:10px;height:10px;margin-top:6px;flex:none;border:2px solid var(--moss);background:var(--moss)}
.mark.dry{background:transparent}
.mark.review{border-color:var(--hibiscus);background:var(--hibiscus)}
.mark.duplicate{border-style:dashed;background:transparent}
.names{min-width:0}
.names .orig{color:var(--bark-2);text-decoration:line-through;text-decoration-color:var(--rule);overflow-wrap:anywhere}
.names .new{font-weight:600;overflow-wrap:anywhere}
.names .why{color:var(--bark-2);font-size:13.5px;margin-top:2px}
.names .cat{font-size:13px}
.how{font-size:13px;color:var(--bark-2)}
.conf{display:flex;align-items:center;gap:8px;margin-top:4px}
.conf i{display:block;height:4px;background:var(--rule);width:72px;border-radius:2px;overflow:hidden}
.conf b{display:block;height:100%;background:var(--moss)}
.conf.low b{background:var(--hibiscus)}
details{margin-top:26px}
summary{cursor:pointer;font:400 20px/1.2 var(--serif)}
pre#log{background:var(--paper);border:1px solid var(--rule);border-radius:4px;padding:12px 14px;margin:12px 0 0;
  max-height:300px;overflow:auto;font:12.5px/1.55 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere}
@media (max-width:820px){
  .cols{grid-template-columns:1fr}
  .col,.col+.col{padding:16px 0;border-left:0}
  .col+.col{border-top:1px solid var(--rule)}
  .row{grid-template-columns:1fr}
  .paths{text-align:left}
}
@media (prefers-reduced-motion:reduce){.fresh{animation:none}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Downloads Sloth</h1>
      <p class="tag">Slow and steady. It reads each file, renames it, sorts it, and never deletes anything. Every move can be undone.</p>
    </div>
    <div class="paths" id="paths"></div>
  </header>

  <div class="bar">
    <div class="grp">
      <label for="limit">Files to try</label>
      <input id="limit" type="number" min="1" placeholder="all">
      <button id="preview" class="primary">Preview (moves nothing)</button>
      <button id="sort" class="live">Sort for real</button>
    </div>
    <div class="sep" aria-hidden="true"></div>
    <div class="grp">
      <label for="last">Undo last</label>
      <input id="last" type="number" min="1" value="3">
      <button id="undo">Undo</button>
      <button id="undoreview">Put back "needs a human"</button>
    </div>
    <div class="sep" aria-hidden="true"></div>
    <div class="grp">
      <button id="digest">Write digest</button>
      <button id="check">Check key and limits</button>
    </div>
  </div>

  <div id="status" aria-live="polite"></div>
  <p class="summary" id="summary"></p>

  <div class="cols" style="margin-top:18px">
    <div class="col">
      <h2>The pile</h2>
      <p class="sub" id="pilesub"></p>
      <ul class="files" id="pile"></ul>
    </div>
    <div class="col">
      <h2>The shelves</h2>
      <p class="sub" id="shelfsub"></p>
      <div id="shelves"></div>
    </div>
  </div>

  <section class="feedwrap">
    <h2>What the sloth decided</h2>
    <p class="sub muted" style="margin:0 0 6px;font-size:13px">Newest first. A preview row is a prediction; nothing has moved.</p>
    <div id="feed"></div>
  </section>

  <details open>
    <summary>Console</summary>
    <pre id="log" aria-label="Console output"></pre>
  </details>
</div>

<script>
const $ = s => document.querySelector(s);
function h(tag, props, ...kids){
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === 'class') e.className = v; else e.setAttribute(k, v);
  }
  for (const c of kids.flat()) { if (c == null || c === false) continue; e.append(c); }
  return e;      // append() inserts strings as text, so file names can never inject markup
}
const fmtSize = n => n < 1024 ? n + ' B' : n < 1048576 ? (n/1024).toFixed(0) + ' KB' : (n/1048576).toFixed(1) + ' MB';
const plural = (n, w) => n + ' ' + w + (n === 1 ? '' : 's');

let last = null, prevPile = null, prevShelf = null, jobSeen = null, state = null;

async function post(body){
  const r = await fetch('/api/run', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const j = await r.json();
  if (!j.ok) alert(j.message);
  tick(true);
}
const limitVal = () => $('#limit').value.trim() || null;
$('#preview').onclick = () => post({action:'once', live:false, limit:limitVal()});
$('#sort').onclick = () => {
  const n = state ? state.pile.length : 0;
  const lim = limitVal();
  const count = lim ? Math.min(+lim, n) : n;
  if (confirm('Move up to ' + count + ' files from\n' + state.config.watch + '\ninto\n' + state.config.dest +
              '\n\nNothing is deleted and every move can be undone.')) post({action:'once', live:true, limit:lim});
};
$('#undo').onclick = () => post({action:'undo', last:$('#last').value});
$('#undoreview').onclick = () => post({action:'undo', review:true, last:$('#last').value});
$('#digest').onclick = () => post({action:'digest'});
$('#check').onclick = () => post({action:'check'});

function renderStatus(s){
  const j = s.job, el = $('#status');
  el.replaceChildren();
  if (s.watch_missing) {
    el.append(h('span', {class:'bad'}, 'The watch folder does not exist: ' + s.config.watch + '. Fix config.toml.'));
    return;
  }
  if (j.running) {
    const secs = Math.max(0, Math.round(Date.now()/1000 - j.started));
    el.append(h('strong', {}, j.label), ' is in progress (' + secs + 's). The model is slow on purpose: it waits between calls to respect rate limits.');
  } else if (j.ended) {
    const secs = Math.round(j.ended - j.started);
    el.append(h('span', {}, h('strong', {}, j.label),
      j.failed ? [' stopped after ' + secs + 's. ', h('span', {class:'bad'}, 'See the console for why.')]
               : ' finished in ' + secs + 's.'));
  } else {
    el.append('Idle. Start with a preview: it uses the model but moves nothing.');
  }
}

function renderSummary(s){
  const t = s.stats, handled = t.model + t.rule + t.private + t.dup + t.review;
  const cfgCap = s.config.cap;
  const bits = [];
  if (handled === 0) bits.push('Nothing has been moved today.');
  else {
    const free = t.rule + t.private + t.dup;
    bits.push('Today the sloth handled ' + plural(handled, 'file') + ': ' + t.model + ' sorted by the model, ' + free +
      ' without it (' + t.rule + ' by extension, ' + t.private + ' private by name, ' + t.dup + ' duplicates), and ' +
      t.review + ' for a human.');
  }
  bits.push('Model calls today: ' + t.calls + ' of the ' + cfgCap + ' allowed by config.toml (' + t.tin + ' tokens in, ' + t.tout + ' out).');
  $('#summary').textContent = bits.join(' ');
}

function renderPile(s){
  const ul = $('#pile'); ul.replaceChildren();
  const now = new Set(s.pile.map(f => f.name));
  const planned = s.pile.filter(f => f.pred).length;
  $('#pilesub').textContent = s.pile.length === 0 ? 'Nothing waiting.' :
    plural(s.pile.length, 'file') + ' waiting in the watch folder' + (planned ? ', ' + planned + ' with a previewed destination.' : '.');
  if (s.pile.length === 0) { ul.append(h('li', {class:'empty'}, 'The folder is tidy.')); }
  for (const f of s.pile) {
    const fresh = prevPile && !prevPile.has(f.name);
    ul.append(h('li', {class: fresh ? 'fresh' : ''},
      h('span', {class:'n mono'}, f.name),
      h('span', {class:'r'}, f.pred ? 'would go to ' + f.pred : fmtSize(f.size))));
  }
  prevPile = now;
}

function renderShelves(s){
  const box = $('#shelves'); box.replaceChildren();
  const cats = Object.keys(s.shelves);
  const total = cats.reduce((a, c) => a + s.shelves[c].length, 0);
  $('#shelfsub').textContent = total === 0 ? 'Nothing sorted yet.' : plural(total, 'file') + ' in ' + plural(cats.length, 'folder') + '.';
  if (!cats.length) box.append(h('p', {class:'empty'}, 'Preview first, then sort for real. Sorted files appear here.'));
  const now = new Set();
  for (const c of cats) {
    const files = s.shelves[c];
    const attn = c === s.config.review;
    const shelf = h('div', {class:'shelf' + (attn ? ' attn' : '')},
      h('h3', {}, h('span', {}, c), h('span', {class:'c'}, plural(files.length, 'file'))));
    const ul = h('ul', {class:'files'});
    files.slice(0, 8).forEach(n => {
      const key = c + '/' + n; now.add(key);
      ul.append(h('li', {class: prevShelf && !prevShelf.has(key) ? 'fresh' : ''}, h('span', {class:'n mono'}, n)));
    });
    files.slice(8).forEach(n => now.add(c + '/' + n));
    shelf.append(ul);
    if (files.length > 8) shelf.append(h('div', {class:'more'}, '+ ' + (files.length - 8) + ' more'));
    box.append(shelf);
  }
  prevShelf = now;
}

function how(r){
  if (r.status === 'duplicate') return 'same content hash';
  if (r.summary.startsWith('Matched a private-name')) return 'name rule, never sent';
  if (r.summary.startsWith('Sorted by extension')) return 'extension rule, no model';
  return 'read by model';
}
const STATE_LABEL = {moved:'moved', dry:'preview', review:'needs a human', duplicate:'duplicate'};

function renderFeed(s){
  const box = $('#feed'); box.replaceChildren();
  if (!s.feed.length) box.append(h('p', {class:'empty'}, 'No decisions yet.'));
  for (const r of s.feed) {
    const same = r.orig === r.new;
    const conf = Math.round(r.conf * 100);
    box.append(h('div', {class:'row'},
      h('div', {class:'state'}, h('span', {class:'mark ' + r.status}), STATE_LABEL[r.status] || r.status),
      h('div', {class:'names'},
        h('div', {class:'mono'}, same ? h('span', {class:'new'}, r.orig) : [h('span', {class:'orig'}, r.orig), ' \u2192 ', h('span', {class:'new'}, r.new)]),
        h('div', {class:'cat mono'}, r.category),
        r.summary && h('div', {class:'why'}, r.summary)),
      h('div', {},
        h('div', {class:'how'}, how(r)),
        h('div', {class:'conf' + (r.conf < 0.6 ? ' low' : ''), title:'model confidence'},
          h('i', {}, h('b', {style:'width:' + conf + '%'})), conf + '%'))));
  }
}

function renderLog(s){
  const pre = $('#log');
  const text = s.log.join('\n');
  if (pre.textContent === text) return;
  const stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 24;
  pre.textContent = text;
  if (stick) pre.scrollTop = pre.scrollHeight;
}

function render(s){
  state = s;
  renderStatus(s);
  document.querySelectorAll('button').forEach(b => b.disabled = s.job.running);
  const snap = JSON.stringify([s.pile, s.shelves, s.feed, s.stats, s.config, s.watch_missing]);
  if (snap !== last) {
    last = snap;
    renderSummary(s); renderPile(s); renderShelves(s); renderFeed(s);
    $('#paths').replaceChildren(
      h('div', {}, 'watching ' + s.config.watch),
      h('div', {}, 'sorting into ' + s.config.dest),
      h('div', {}, 'model ' + s.config.model));
  }
  renderLog(s);
}

let timer = null;
async function tick(now){
  clearTimeout(timer);
  let running = false;
  try {
    const r = await fetch('/api/state', {cache:'no-store'});
    const s = await r.json();
    render(s); running = s.job.running;
  } catch (e) {
    $('#status').textContent = 'Lost the connection. Is the sloth still running in your terminal?';
  }
  timer = setTimeout(tick, running ? 800 : 2500);
}
tick();
</script>
</body>
</html>
"""
