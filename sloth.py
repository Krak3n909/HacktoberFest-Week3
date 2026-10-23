#!/usr/bin/env python3
"""Downloads Sloth: an open-weight-model file tidier.

Watches a folder, asks an open-weight model (Gemma 4 31B via OpenRouter by
default) what each new file is, then renames and sorts it. Never deletes.
Every move is logged to SQLite and can be undone.

Commands:
  python sloth.py check             test your key, show real limits
  python sloth.py once [--limit 10] sort files currently in the watch folder
  python sloth.py run               watch continuously
  python sloth.py undo --last 5     put the last 5 moved files back
  python sloth.py digest            write today's summary (markdown)
  python sloth.py ui                open the local web dashboard
"""
import argparse
import base64
import hashlib
import io
import json
import logging
import os
import queue
import re
import shutil
import sqlite3
import sys
import time
import tomllib
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path


logging.getLogger("pypdf").setLevel(logging.ERROR)

IGNORE_SUFFIXES = {".crdownload", ".part", ".tmp", ".download", ".partial"}
TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".log", ".html", ".xml",
            ".yaml", ".yml", ".py", ".js", ".ts", ".java", ".c", ".cpp"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


class _Cached(Exception):
    pass


class _Transient(Exception):
    pass


class Stop(Exception):
    """Abort the batch WITHOUT touching the file (rate limit, bad key, no credits)."""
    def __init__(self, kind, msg):
        super().__init__(msg)
        self.kind, self.msg = kind, msg


IGNORE_NAMES = {"desktop.ini", "thumbs.db", "sloth.py", "sloth_ui.py", "config.toml",
                "requirements.txt", "sloth.db"}


def _is_hidden_or_system(path):
    """Windows hidden (0x2) / system (0x4) attribute check."""
    try:
        return bool(getattr(path.stat(), "st_file_attributes", 0) & 0x6)
    except OSError:
        return False


RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10))}


# ---------------------------------------------------------------- config
def load_config(path):
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    for k in ("watch", "dest", "db"):
        cfg["folders"][k] = Path(os.path.expanduser(cfg["folders"][k]))
    return cfg


# ---------------------------------------------------------------- helpers
def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(name, ext):
    if name.lower().endswith(ext.lower()):
        name = name[: -len(ext)]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip(" .")
    name = re.sub(r"\s+", "_", name)[:80]
    if not name or name.upper() in RESERVED:
        name = "file"
    return name + ext


def unique_path(p):
    if not p.exists():
        return p
    i = 1
    while True:
        cand = p.with_name(f"{p.stem}_{i}{p.suffix}")
        if not cand.exists():
            return cand
        i += 1


def wait_until_settled(path, settle, timeout=180):
    """Wait for the browser to finish writing and release the file (Windows locks)."""
    start, last_size, stable_since = time.time(), -1, None
    while time.time() - start < timeout:
        try:
            size = path.stat().st_size
            with open(path, "rb"):
                pass
        except (FileNotFoundError, PermissionError):
            time.sleep(1)
            continue
        if size == last_size:
            stable_since = stable_since or time.time()
            if time.time() - stable_since >= settle:
                return True
        else:
            last_size, stable_since = size, None
        time.sleep(1)
    return False


def extract(path, max_chars):
    """Return (text_excerpt, image_data_url). Either may be None."""
    ext = path.suffix.lower()
    try:
        if ext in TEXT_EXT:
            return path.read_text(encoding="utf-8", errors="ignore")[:max_chars], None
        if ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            text = ""
            for page in reader.pages[:3]:
                text += (page.extract_text() or "") + "\n"
                if len(text) >= max_chars:
                    break
            return (text.strip()[:max_chars] or None), None
        if ext == ".docx":
            import docx
            d = docx.Document(str(path))
            return "\n".join(p.text for p in d.paragraphs)[:max_chars], None
        if ext in IMG_EXT:
            from PIL import Image
            img = Image.open(path).convert("RGB")
            img.thumbnail((1024, 1024))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=80)
            b64 = base64.b64encode(buf.getvalue()).decode()
            return None, f"data:image/jpeg;base64,{b64}"
    except Exception as e:  # unreadable file: fall back to filename-only
        print(f"  (could not read content: {e})")
    return None, None


def parse_json(text):
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b == -1:
        raise ValueError("no JSON object in reply")
    return json.loads(text[a: b + 1])


# ---------------------------------------------------------------- the sloth
class Sloth:
    def __init__(self, cfg, dry_run=None, client=None):
        self.cfg = cfg
        self.dry = cfg["behavior"]["dry_run"] if dry_run is None else dry_run
        cfg["folders"]["dest"].mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(cfg["folders"]["db"], check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS moves(
            id INTEGER PRIMARY KEY, ts TEXT, src TEXT, dst TEXT, category TEXT,
            summary TEXT, confidence REAL, sha256 TEXT,
            prompt_tokens INT DEFAULT 0, completion_tokens INT DEFAULT 0,
            status TEXT)""")
        self.last_call = 0.0
        self.client = client
        # Whole-word matching: "id" must not fire inside "Hybrid" or "Identification",
        # and "tin" must not fire inside "Meeting". Separators (_ - . space) split words.
        pats = "|".join(cfg["privacy"]["local_only_name_patterns"])
        self.re_private = re.compile(
            r"(?<![a-z])(?:" + pats + r")s?(?![a-z])", re.I)

    # ---- model
    def _client(self):
        if self.client is None:
            from openai import OpenAI
            llm = self.cfg["llm"]
            key = os.environ.get(llm["api_key_env"])
            if not key:
                sys.exit(f"Set {llm['api_key_env']} first. In PowerShell:\n"
                         f'  setx {llm["api_key_env"]} "your-key"  (then reopen the terminal)')
            self.client = OpenAI(
                base_url=llm["base_url"], api_key=key,
                default_headers={"X-Title": "Downloads Sloth"})
        return self.client

    def _calls_today(self):
        # OpenRouter's daily window is the UTC day; stored timestamps are local time
        utc_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        local_start = utc_start.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
        return self.db.execute(
            "SELECT COUNT(*) FROM moves WHERE ts >= ? AND prompt_tokens > 0",
            (local_start,)).fetchone()[0]

    def similar_examples(self, path, limit=3):
        """Earlier decisions for files with the same name pattern (digits -> #)."""
        sig = re.sub(r"\d+", "#", path.stem.lower())
        if "#" not in sig:
            return []
        skip = (self.cfg["behavior"]["review_category"],
                self.cfg["behavior"]["duplicates_category"],
                self.cfg["privacy"]["local_only_category"],
                "Other")  # never let a catch-all answer become the series' "example"
        rows = self.db.execute(
            "SELECT src,dst,category FROM moves WHERE status IN ('moved','dry') "
            "AND category NOT IN (?,?,?,?) ORDER BY id DESC LIMIT 400", skip).fetchall()
        out, seen = [], set()
        for src, dst, cat in rows:
            name = Path(src).name
            if name == path.name or name in seen:
                continue
            if re.sub(r"\d+", "#", Path(src).stem.lower()) == sig:
                seen.add(name)
                out.append((name, cat, Path(dst).name))
                if len(out) >= limit:
                    break
        return out

    def classify(self, path, text, img_url):
        llm, cfg = self.cfg["llm"], self.cfg
        cats = ", ".join(cfg["categories"])
        system = (
            "You are a careful file librarian. Reply with ONE JSON object and nothing else:\n"
            '{"category": <exactly one of the allowed categories>, '
            '"filename": <short descriptive name, no extension, max 6 words, '
            'format like 2026-03_Electricity_Bill if a date is evident>, '
            '"summary": <one sentence>, "confidence": <0.0 to 1.0>}\n'
            f"Allowed categories: {cats}\n"
            "If you are unsure, use a low confidence. Never invent details.")
        ex = self.similar_examples(path)
        ex_txt = ""
        if ex:
            ex_txt = ("Similar-named files already sorted (stay consistent with them "
                      "unless this file's content clearly differs):\n"
                      + "\n".join(f"- {a} -> {b}/{c}" for a, b, c in ex) + "\n")
        parts = [{"type": "text", "text": (
            f"Original filename: {path.name}\nExtension: {path.suffix}\n"
            f"Size: {path.stat().st_size} bytes\n{ex_txt}"
            f"Content excerpt:\n{text or '(no text available)'}")}]
        if img_url:
            parts.append({"type": "image_url", "image_url": {"url": img_url}})

        # throttle + retry. Real errors are shown, and quota problems stop the batch.
        last_msg = ""
        waits, i, json_retried, transient = (0, 15, 45, 90), 0, False, 0
        while i < len(waits):
            time.sleep(waits[i])
            raw, finish = "", None
            gap = llm["min_seconds_between_calls"] - (time.time() - self.last_call)
            if gap > 0:
                time.sleep(gap)
            try:
                self.last_call = time.time()
                r = self._client().chat.completions.create(
                    model=llm["model"], temperature=0.1, max_tokens=llm.get("max_output_tokens", 600),
                    extra_body=llm.get("extra_body") or None,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": parts}])
                if not getattr(r, "choices", None):
                    raise _Transient(str(getattr(r, "error", None) or "empty response")[:200])
                raw = r.choices[0].message.content or ""
                finish = getattr(r.choices[0], "finish_reason", None)
                data = parse_json(raw)
                u = r.usage
                return data, (u.prompt_tokens if u else 0), (u.completion_tokens if u else 0)
            except _Transient as e:
                transient += 1
                if transient > 2:
                    raise RuntimeError(f"provider returned no usable answer: {e}")
                print(f"  provider returned no usable answer ({e}); retry {transient}/2")
                time.sleep(5 * transient)
                continue
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                snippet = raw[:150].replace("\n", " ")
                if not json_retried:      # models occasionally ramble: retry once
                    json_retried = True
                    print(f"  (invalid JSON; finish_reason={finish}; got {snippet!r}; retrying once)")
                    continue
                raise RuntimeError(f"model reply was not valid JSON "
                                   f"(finish_reason={finish}): {snippet!r}")
            except Exception as e:
                code = getattr(e, "status_code", None)
                last_msg = str(e)[:400]
                low = last_msg.lower()
                if code == 401:
                    raise Stop("fatal", f"API key rejected (401). Check {llm['api_key_env']}.")
                if code == 402:
                    raise Stop("fatal", "402 Payment Required: your OpenRouter balance is "
                                        "negative. Add credits, then retry.")
                if code == 429 or "rate" in low:
                    if "per-day" in low or "per day" in low or "daily" in low:
                        raise Stop("daily", f"Daily free-model limit reached: {last_msg}")
                    print(f"  429 from API: {last_msg[:200]}")
                    if "upstream" in low or "shared" in low:
                        print("  -> The shared FREE pool is saturated (not your quota). Options: "
                              "retry later, add your own Google AI Studio key in OpenRouter "
                              "settings (BYOK), or use the paid slug. See README.")
                    print("  waiting before retry...")
                    i += 1
                    continue
                raise
        raise Stop("rate", f"Still rate limited after retries: {last_msg}")

    # ---- one file
    def process(self, path):
        cfg, beh = self.cfg, self.cfg["behavior"]
        path = Path(path)
        dest_root = cfg["folders"]["dest"]
        if (not path.is_file() or path.suffix.lower() in IGNORE_SUFFIXES
                or path.name.startswith(("~$", "."))
                or path.name.lower() in IGNORE_NAMES
                or _is_hidden_or_system(path)
                or path.parent != cfg["folders"]["watch"]):
            return
        if not wait_until_settled(path, beh["settle_seconds"]):
            print(f"skip (still busy): {path.name}")
            return

        sha = sha256_of(path)
        ext = path.suffix.lower()
        prompt_tok = comp_tok = 0
        status = "moved"

        dup = self.db.execute(
            "SELECT dst FROM moves WHERE sha256=? AND status='moved'", (sha,)).fetchone()
        if not dup and self.dry:
            # dry-run: predict duplicates among files not yet moved
            dup = self.db.execute(
                "SELECT dst FROM moves WHERE sha256=? AND src!=? AND status='dry' "
                "AND category!=? ORDER BY id LIMIT 1",
                (sha, str(path), beh["duplicates_category"])).fetchone()
            dup_exists = bool(dup)
        else:
            dup_exists = bool(dup and Path(dup[0]).exists())
        if dup_exists:
            category, new_name = beh["duplicates_category"], path.name
            summary, conf, status = f"Duplicate of {Path(dup[0]).name}", 1.0, "duplicate"
        elif self.re_private.search(re.sub(r"[^A-Za-z0-9]+", " ", path.stem)):
            category, new_name = cfg["privacy"]["local_only_category"], path.name
            summary, conf = "Matched a private-name rule. Content NOT sent to the model.", 1.0
        elif ext in cfg["extension_rules"]:
            category, new_name = cfg["extension_rules"][ext], path.name
            summary, conf = f"Sorted by extension rule ({ext}), no model call.", 1.0
        else:
            if self._calls_today() >= cfg["llm"]["max_requests_per_day"]:
                raise Stop("daily", f"Reached your max_requests_per_day cap "
                                    f"({cfg['llm']['max_requests_per_day']}). Free quota resets "
                                    f"at 00:00 UTC (6 AM Dhaka). Raise the cap in config.toml "
                                    f"if you have more quota.")
            cached = self.db.execute(
                "SELECT category,dst,summary,confidence FROM moves WHERE sha256=? "
                "AND status='dry' AND prompt_tokens>0 AND category!=? ORDER BY id DESC LIMIT 1",
                (sha, beh["review_category"])).fetchone()
            if cached:
                # reuse an earlier answer for identical content: no API call
                category, new_name = cached[0], Path(cached[1]).name
                summary, conf = cached[2], float(cached[3])
                print("  (reused cached dry-run answer, no API call)")
                text = img = None
                cached_hit = True
            else:
                cached_hit = False
                text, img = extract(path, beh["max_chars_sent"])
            try:
                if cached_hit:
                    raise _Cached()
                data, prompt_tok, comp_tok = self.classify(path, text, img)
                category = data.get("category", "")
                conf = float(data.get("confidence", 0))
                summary = str(data.get("summary", ""))[:300]
                new_name = safe_name(str(data.get("filename", path.stem)), path.suffix)
                if category not in cfg["categories"] or conf < beh["min_confidence"]:
                    category, new_name, status = beh["review_category"], path.name, "review"
            except _Cached:
                pass
            except Stop:
                raise
            except Exception as e:
                print(f"  model failed for {path.name}: {e}")
                category, new_name, status = beh["review_category"], path.name, "review"
                summary, conf = f"Model error: {str(e)[:120]}", 0.0

        target = unique_path(dest_root / category / new_name)
        arrow = f"{path.name}  ->  {target.relative_to(dest_root)}"
        if self.dry:
            print(f"[dry-run] {arrow}  ({conf:.2f})")
            status = "dry"
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            for _ in range(5):  # Windows may still hold a lock
                try:
                    shutil.move(str(path), str(target))
                    break
                except PermissionError:
                    time.sleep(2)
            else:
                print(f"could not move {path.name} (locked)")
                return
            print(f"[moved]   {arrow}  ({conf:.2f})")
        self.db.execute(
            "INSERT INTO moves(ts,src,dst,category,summary,confidence,sha256,"
            "prompt_tokens,completion_tokens,status) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), str(path), str(target),
             category, summary, conf, sha, prompt_tok, comp_tok, status))
        self.db.commit()

    # ---- commands
    def once(self, limit=None):
        done = 0
        # Process originals before browser copies like "report (1).pdf", so the
        # cleanly named file is kept and the "(1)" copy becomes the duplicate.
        def order(p):
            return (bool(re.search(r"\s\(\d+\)$", p.stem)), len(p.name), p.name.lower())
        for p in sorted(self.cfg["folders"]["watch"].iterdir(), key=order):
            if limit is not None and done >= limit:
                print(f"--limit {limit} reached, stopping (files left untouched).")
                break
            try:
                before = self.db.total_changes
                self.process(p)
                done += 1 if self.db.total_changes != before else 0
            except Stop as e:
                print(f"\nSTOPPED: {e.msg}\nRemaining files were left untouched. "
                      f"Nothing was mis-filed.")
                break

    def run(self, backlog=False):
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
        q = queue.Queue()

        class H(FileSystemEventHandler):
            def on_created(self, e):
                if not e.is_directory:
                    q.put(e.src_path)

            def on_moved(self, e):  # browsers rename .crdownload -> final name
                if not e.is_directory:
                    q.put(e.dest_path)

        if backlog:
            self.once()
        obs = Observer()
        obs.schedule(H(), str(self.cfg["folders"]["watch"]), recursive=False)
        obs.start()
        print(f"Sloth is watching {self.cfg['folders']['watch']}  "
              f"(dry_run={self.dry}). Ctrl+C to stop.")
        try:
            while True:
                try:
                    item = q.get(timeout=1)
                except queue.Empty:
                    continue
                try:
                    self.process(item)
                except Stop as e:
                    print(f"STOPPED: {e.msg}")
                    if e.kind == "rate":
                        print("Pausing 5 minutes, then retrying this file.")
                        time.sleep(300)
                        q.put(item)
                    else:
                        print("Exiting. Fix the issue (or wait for the daily reset, "
                              "00:00 UTC) and start me again.")
                        break
        except KeyboardInterrupt:
            obs.stop()
        obs.join()

    def check(self):
        """Show the real account limits and make one tiny test call."""
        llm = self.cfg["llm"]
        key = os.environ.get(llm["api_key_env"], "")
        print(f"model: {llm['model']}\nkey set: {bool(key)}")
        if "openrouter.ai" in llm["base_url"] and key:
            try:
                req = urllib.request.Request(
                    "https://openrouter.ai/api/v1/key",
                    headers={"Authorization": f"Bearer {key}"})
                info = json.load(urllib.request.urlopen(req, timeout=20)).get("data", {})
                for k in ("label", "is_free_tier", "usage_daily", "usage", "limit_remaining"):
                    print(f"  {k}: {info.get(k)}")
                print("  (is_free_tier=True means you have never bought credits -> 50 free "
                      "requests/day; buying $10 once raises it to 1000/day)")
            except Exception as e:
                print(f"  could not read /key: {e}")
        try:
            r = self._client().chat.completions.create(
                model=llm["model"], max_tokens=10,
                extra_body=llm.get("extra_body") or None,
                messages=[{"role": "user", "content": "Say OK"}])
            print("test call OK:", r.choices[0].message.content)
        except Exception as e:
            print("test call FAILED:")
            print("  status:", getattr(e, "status_code", None))
            print("  message:", str(e)[:600])

    def undo(self, last=1, review_only=False):
        statuses = "('review')" if review_only else "('moved','review','duplicate')"
        rows = self.db.execute(
            f"SELECT id,src,dst FROM moves WHERE status IN {statuses} "
            "ORDER BY id DESC LIMIT ?", (last,)).fetchall()
        for rid, src, dst in rows:
            d, s = Path(dst), unique_path(Path(src))
            if d.exists():
                s.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(d), str(s))
                print(f"restored {s.name}")
            self.db.execute("UPDATE moves SET status='undone' WHERE id=?", (rid,))
        self.db.commit()
        if not rows:
            print("nothing to undo")

    def digest(self, day=None):
        day = day or date.today().isoformat()
        rows = self.db.execute(
            "SELECT ts,src,dst,category,summary,confidence,status,prompt_tokens,"
            "completion_tokens FROM moves WHERE ts LIKE ? AND status!='dry' ORDER BY id",
            (day + "%",)).fetchall()
        dest_root = self.cfg["folders"]["dest"]
        moved = [r for r in rows if r[6] == "moved"]
        review = [r for r in rows if r[6] == "review"]
        dups = [r for r in rows if r[6] == "duplicate"]
        # model usage counts ALL of the day's real API calls, including dry runs
        calls, pt, ct = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0) "
            "FROM moves WHERE ts LIKE ? AND prompt_tokens>0", (day + "%",)).fetchone()
        handled = len(moved) + len(review) + len(dups)
        no_model = len(dups) + len([r for r in moved if r[4].startswith(
            ("Sorted by extension rule", "Matched a private-name rule"))])
        free = self.cfg["llm"]["model"].endswith(":free")
        cost = "$0 (free open-weight model)" if free else "see your provider's billing page"
        out = [f"# Sloth digest: {day}", "",
               f"Handled **{handled}** files: **{len(moved)}** sorted, **{len(dups)}** duplicates, "
               f"**{len(review)}** need your eyes.", "",
               f"Model calls today (including dry runs and retries): **{calls}**. "
               f"Files that never needed the model: **{no_model}** "
               f"(duplicates, extension rules, private-name rules).",
               f"Tokens: {pt} in / {ct} out. Cost: {cost}.", ""]
        if review:
            out += ["## Needs a human", ""]
            out += [f"- `{Path(r[1]).name}`: {r[4]}" for r in review]
            out.append("")
        if moved:
            out += ["## Moved", ""]
            for r in moved:
                try:
                    rel = Path(r[2]).relative_to(dest_root)
                except ValueError:
                    rel = Path(r[2])
                out.append(f"- `{Path(r[1]).name}` -> `{rel}`: {r[4]}")
        text = "\n".join(out) + "\n"
        outfile = dest_root / f"_digest_{day}.md"
        outfile.write_text(text, encoding="utf-8")
        print(text)
        print(f"saved: {outfile}")


def main():
    ap = argparse.ArgumentParser(description="Downloads Sloth")
    ap.add_argument("command", choices=["once", "run", "undo", "digest", "check", "ui"])
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.toml")))
    ap.add_argument("--live", action="store_true", help="override dry_run: actually move files")
    ap.add_argument("--dry", action="store_true", help="force dry run")
    ap.add_argument("--backlog", action="store_true", help="with run: sort existing files first")
    ap.add_argument("--limit", type=int, default=None, help="with once: max files to process")
    ap.add_argument("--review", action="store_true",
                    help="with undo: only restore files that went to 'Needs a human'")
    ap.add_argument("--last", type=int, default=1, help="with undo: how many moves")
    ap.add_argument("--port", type=int, default=8765, help="with ui: port (default 8765)")
    ap.add_argument("--no-browser", action="store_true", help="with ui: don't open a browser tab")
    a = ap.parse_args()
    cfg = load_config(a.config)
    if a.command == "ui":
        import sloth_ui
        cfg["folders"]["dest"].mkdir(parents=True, exist_ok=True)
        sloth_ui.serve(cfg, Sloth, a.port, open_browser=not a.no_browser)
        return
    dry = True if a.dry else (False if a.live else None)
    s = Sloth(cfg, dry_run=dry)
    {"once": lambda: s.once(a.limit), "check": s.check, "run": lambda: s.run(a.backlog),
     "undo": lambda: s.undo(a.last, a.review), "digest": s.digest}[a.command]()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted. Nothing is half-moved; every move is logged and undoable.")