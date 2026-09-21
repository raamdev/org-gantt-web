#!/usr/bin/env python3
"""org-gantt-web — local server.

Serves the single-file app and reads/writes real .org project files on disk,
so the browser and Emacs edit the same files. No third-party dependencies.

Usage:
    python3 server.py                       # serve ./projects on http://localhost:8730
    python3 server.py --dir ~/Dropbox/gantt # point at your Dropbox folder
    python3 server.py --demo                # public-safe demo: samples only, no disk writes

Modes are chosen here; the frontend auto-detects which one it's talking to via
GET /api/config. See CLAUDE.md ("Backend / server modes") for the full contract.
"""
import argparse
import json
import os
import re
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(HERE, "org-gantt-web.html")
STATE_NAME = ".org-gantt-state.json"          # per-dir "recently opened" tracking

# A project id is just its filename. Keep it strictly a safe .org basename so it
# can never escape the projects directory (matters once this is public).
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*\.org$")
TITLE_RE = re.compile(r"^#\+TITLE:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# The kanban board. Its ordered column list lives in a plain .org file (one heading
# per column) alongside the projects, so it round-trips with Emacs like everything
# else. It is NOT itself a project — excluded from the project list. Each project's
# card state (which column, sort order, note) lives in that project's own .org file
# as header keywords, so a project's board status travels with the project file.
BOARD_NAME = "kanban.org"
DEFAULT_COLUMNS = ["Next up", "In progress", "Done"]
KAN_COLUMN_RE = re.compile(r"^#\+KANBAN_COLUMN:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
KAN_ORDER_RE = re.compile(r"^#\+KANBAN_ORDER:\s*(-?\d+)", re.IGNORECASE | re.MULTILINE)
KAN_NOTE_RE = re.compile(r"^#\+KANBAN_NOTE:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
HEADING_RE = re.compile(r"^\*+\s")


def kanban_of(text):
    """Pull a project's card fields (column, order, note) out of its org header."""
    text = text or ""
    mc = KAN_COLUMN_RE.search(text)
    mo = KAN_ORDER_RE.search(text)
    mn = KAN_NOTE_RE.search(text)
    return {
        "column": mc.group(1).strip() if mc else None,
        "order": int(mo.group(1)) if mo else None,
        "note": mn.group(1).strip() if mn else "",
    }


def set_header_keyword(text, key, value):
    """Insert/replace/remove a `#+KEY: value` line in the header block (everything
    before the first `*` heading). value=None removes the line. Returns new text.
    Keeps unrelated keywords and body untouched so the file round-trips cleanly."""
    lines = (text or "").split("\n")
    hstart = len(lines)
    for i, ln in enumerate(lines):
        if HEADING_RE.match(ln):
            hstart = i
            break
    key_re = re.compile(r"^#\+" + re.escape(key) + r":", re.IGNORECASE)
    idx, last_kw = None, -1
    for i in range(hstart):
        if lines[i].startswith("#+"):
            last_kw = i
            if key_re.match(lines[i]):
                idx = i
    if value is None:
        if idx is not None:
            del lines[idx]
        return "\n".join(lines)
    newline = "#+%s: %s" % (key, value)
    if idx is not None:
        lines[idx] = newline
    else:
        lines.insert(last_kw + 1 if last_kw >= 0 else 0, newline)
    return "\n".join(lines)


def apply_card_patch(text, fields):
    """Apply a partial card edit (title / note / column / order) to org text by
    editing the corresponding header keywords in place. Shared by both stores."""
    def clean(v):
        return re.sub(r"\s+", " ", str(v)).strip()

    if "title" in fields and clean(fields["title"]):
        text = set_header_keyword(text, "TITLE", clean(fields["title"]))
    if "column" in fields:
        v = fields["column"]
        text = set_header_keyword(text, "KANBAN_COLUMN", clean(v) if v and clean(v) else None)
    if "order" in fields:
        v = fields["order"]
        text = set_header_keyword(text, "KANBAN_ORDER", None if v is None else str(int(v)))
    if "note" in fields:
        v = fields["note"]
        text = set_header_keyword(text, "KANBAN_NOTE", clean(v) if v and clean(v) else None)
    return text


HEX_RE = re.compile(r"^#[0-9A-Fa-f]{3,8}$")
# Per-column styling in each kanban.org heading's property drawer. bg/fg color the cards;
# border colors the column box. Keep these in sync with the frontend + COLOR_PROPS below.
COLOR_PROPS = [("bg", "CARD_BG"), ("fg", "CARD_FG"), ("border", "BORDER")]
COLOR_RE = {key: re.compile(r"^:%s:\s*(\S+)" % prop, re.IGNORECASE) for key, prop in COLOR_PROPS}


def _hex_or_none(v):
    v = (v or "").strip()
    return v if HEX_RE.match(v) else None


def clean_colors(names, colors):
    """Keep only valid per-column colors (#hex) for columns that exist."""
    out = {}
    for name in names:
        c = (colors or {}).get(name) or {}
        entry = {}
        for key, _prop in COLOR_PROPS:
            v = _hex_or_none(c.get(key))
            if v:
                entry[key] = v
        if entry:
            out[name] = entry
    return out


def parse_board(text):
    """Return ([names], {name: {bg?, fg?, border?}}). Columns are the top-level headings
    of kanban.org; a column's optional colors live in that heading's property drawer
    (:CARD_BG: / :CARD_FG: / :BORDER:), so board styling round-trips with Emacs too."""
    names, colors = [], {}
    cur = None
    for ln in (text or "").split("\n"):
        m = re.match(r"^\*+\s+(.+?)\s*$", ln)
        if m and m.group(1).strip():
            cur = m.group(1).strip()
            names.append(cur)
            continue
        if cur is None:
            continue
        stripped = ln.strip()
        for key, _prop in COLOR_PROPS:
            mm = COLOR_RE[key].match(stripped)
            if mm:
                v = _hex_or_none(mm.group(1))
                if v:
                    colors.setdefault(cur, {})[key] = v
                break
    return names, colors


def board_text(columns, colors=None):
    colors = colors or {}
    lines = ["#+TITLE: Project board", ""]
    for name in columns:
        lines.append("* " + name)
        c = colors.get(name) or {}
        props = [(prop, _hex_or_none(c.get(key))) for key, prop in COLOR_PROPS]
        props = [(prop, v) for prop, v in props if v]
        if props:
            lines.append(":PROPERTIES:")
            for prop, v in props:
                lines.append(":%s: %s" % (prop, v))
            lines.append(":END:")
    return "\n".join(lines) + "\n"


def log(msg):
    """Timestamped line to stdout (flushed so it shows up live under the server)."""
    print("[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def org_stamp(d):
    return "<%s %s>" % (d.isoformat(), DOW[d.weekday()])


def starter_org(title):
    today = date.today()
    return (
        "#+TITLE: %s\n"
        "#+TODO: TODO | DONE\n\n"
        "* TODO First task\n"
        "SCHEDULED: %s DEADLINE: %s\n"
        ":PROPERTIES:\n"
        ":PROGRESS: 0\n"
        ":END:\n"
    ) % (title, org_stamp(today), org_stamp(today + timedelta(days=3)))


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "project").lower()).strip("-")
    return s or "project"


def title_of(text, fallback):
    m = TITLE_RE.search(text or "")
    return m.group(1).strip() if m else fallback


# --------------------------------------------------------------------------- #
#  Stores
# --------------------------------------------------------------------------- #
class FileStore:
    """Projects are .org files inside a single root directory."""

    demo = False

    def __init__(self, root):
        self.root = os.path.abspath(os.path.expanduser(root))
        os.makedirs(self.root, exist_ok=True)
        self.lock = threading.Lock()

    # -- path safety: an id must resolve to a .org file directly inside root --
    def _resolve(self, pid):
        if not SAFE_ID.match(pid or ""):
            raise ValueError("bad project id")
        p = os.path.abspath(os.path.join(self.root, pid))
        if os.path.dirname(p) != self.root:
            raise ValueError("project id escapes root")
        return p

    def _state_path(self):
        return os.path.join(self.root, STATE_NAME)

    def _load_state(self):
        try:
            with open(self._state_path(), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"opened_at": {}}

    def _save_state(self, st):
        try:
            tmp = self._state_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f)
            os.replace(tmp, self._state_path())
        except OSError:
            pass

    def _bump(self, pid):
        import time
        with self.lock:
            st = self._load_state()
            st.setdefault("opened_at", {})[pid] = time.time()
            self._save_state(st)

    def list(self):
        opened = self._load_state().get("opened_at", {})
        out = []
        for name in os.listdir(self.root):
            if name.startswith(".") or not name.endswith(".org") or name == BOARD_NAME:
                continue
            p = os.path.join(self.root, name)
            if not os.path.isfile(p):
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    head = f.read(2048)
            except OSError:
                continue
            mtime = os.path.getmtime(p)
            card = kanban_of(head)
            out.append({
                "id": name,
                "name": title_of(head, name[:-4]),
                "mtime": mtime,
                "recent": max(mtime, opened.get(name, 0)),
                "size": os.path.getsize(p),
                "column": card["column"],
                "order": card["order"],
                "note": card["note"],
            })
        out.sort(key=lambda x: x["recent"], reverse=True)
        return out

    # -- kanban board (column list persisted as headings in kanban.org) --
    def _board_path(self):
        return os.path.join(self.root, BOARD_NAME)

    def board(self):
        p = self._board_path()
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    names, colors = parse_board(f.read())
                if names:
                    return {"columns": names, "colors": colors}
            except OSError:
                pass
        self.write_board(list(DEFAULT_COLUMNS), {})     # lazy-init on first run
        log("created %s (default columns)" % BOARD_NAME)
        return {"columns": list(DEFAULT_COLUMNS), "colors": {}}

    def write_board(self, columns, colors=None):
        names = [str(c).strip() for c in columns if str(c).strip()]
        clean = clean_colors(names, colors)
        p = self._board_path()
        with self.lock:
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(board_text(names, clean))
            os.replace(tmp, p)
        return {"columns": names, "colors": clean}

    def patch(self, pid, fields):
        """In-place edit of a project's card keywords. Does NOT bump 'recently
        opened' — dragging a card around shouldn't reshuffle the project switcher."""
        p = self._resolve(pid)
        with self.lock:
            with open(p, encoding="utf-8") as f:
                text = f.read()
            text = apply_card_patch(text, fields)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, p)
        card = kanban_of(text)
        return {"id": pid, "name": title_of(text, pid[:-4]), **card}

    def read(self, pid):
        p = self._resolve(pid)
        with open(p, encoding="utf-8") as f:
            text = f.read()
        self._bump(pid)
        return {"id": pid, "name": title_of(text, pid[:-4]),
                "text": text, "mtime": os.path.getmtime(p)}

    def write(self, pid, text):
        p = self._resolve(pid)
        with self.lock:
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, p)          # atomic; Dropbox/Emacs never see a half-written file
        self._bump(pid)
        return {"id": pid, "mtime": os.path.getmtime(p)}

    def create(self, name):
        base = slugify(name)
        pid = base + ".org"
        n = 2
        while os.path.exists(os.path.join(self.root, pid)):
            pid = "%s-%d.org" % (base, n)
            n += 1
        self.write(pid, starter_org(name or "New project"))
        return self.read(pid)

    def delete(self, pid):
        p = self._resolve(pid)
        with self.lock:
            if os.path.exists(p):
                os.remove(p)
            st = self._load_state()
            st.get("opened_at", {}).pop(pid, None)
            self._save_state(st)
        return {"ok": True}


class DemoStore:
    """In-memory sample projects for a public demo. All writes are no-ops, so a
    visitor can never touch the host disk or see anyone else's files. The frontend
    also keeps demo edits in localStorage only, so this is doubly safe."""

    demo = True

    def __init__(self):
        self.samples = _demo_samples()
        self.columns = list(DEFAULT_COLUMNS)
        self.colors = {}

    def list(self):
        out = []
        for i, (pid, text) in enumerate(self.samples.items()):
            card = kanban_of(text)
            out.append({"id": pid, "name": title_of(text, pid[:-4]),
                        "mtime": 0, "recent": len(self.samples) - i, "size": len(text),
                        "column": card["column"], "order": card["order"], "note": card["note"]})
        return out

    def read(self, pid):
        if pid not in self.samples:
            raise KeyError(pid)
        return {"id": pid, "name": title_of(self.samples[pid], pid[:-4]),
                "text": self.samples[pid], "mtime": 0}

    def write(self, pid, text):
        if pid in self.samples:
            self.samples[pid] = text            # in memory only; never touches disk
        return {"id": pid, "mtime": 0}

    def create(self, name):
        base = slugify(name)
        pid, n = base + ".org", 2
        while pid in self.samples:
            pid = "%s-%d.org" % (base, n); n += 1
        text = starter_org(name or "New project")
        self.samples[pid] = text                # in-session only, so demo feels real
        return {"id": pid, "name": name, "text": text, "mtime": 0}

    def delete(self, pid):
        self.samples.pop(pid, None)
        return {"ok": True}

    # -- board / card edits: in-memory so a demo visitor can play, never persisted --
    def board(self):
        return {"columns": list(self.columns), "colors": dict(self.colors)}

    def write_board(self, columns, colors=None):
        self.columns = [str(c).strip() for c in columns if str(c).strip()]
        self.colors = clean_colors(self.columns, colors)
        return {"columns": list(self.columns), "colors": dict(self.colors)}

    def patch(self, pid, fields):
        if pid not in self.samples:
            raise KeyError(pid)
        self.samples[pid] = apply_card_patch(self.samples[pid], fields)
        card = kanban_of(self.samples[pid])
        return {"id": pid, "name": title_of(self.samples[pid], pid[:-4]), **card}


# A rich, realistic demo: 5 phases (each with child tasks), 3 milestones, a target
# date, and a mix of done / in-progress / not-started work spanning ~4 months so the
# zoom + horizontal scroll have something to show. Phases carry no timestamps — their
# span, progress, and [n/m] cookie are derived from children (the app recomputes them).
_ORBIT_ORG = """\
#+TITLE: Orbit — Product Launch
#+TARGET_DATE: <2026-10-30 Fri>
#+TODO: TODO | DONE
#+KANBAN_COLUMN: In progress
#+KANBAN_ORDER: 0
#+KANBAN_NOTE: Beta rollout targeted for late October

* DONE Discovery & research [3/3]
** DONE Competitive analysis
SCHEDULED: <2026-07-06 Mon> DEADLINE: <2026-07-10 Fri>
:PROPERTIES:
:PROGRESS: 100
:END:
** DONE User interviews
SCHEDULED: <2026-07-13 Mon> DEADLINE: <2026-07-20 Mon>
:PROPERTIES:
:PROGRESS: 100
:END:
** DONE Define MVP scope
SCHEDULED: <2026-07-21 Tue> DEADLINE: <2026-07-24 Fri>
:PROPERTIES:
:PROGRESS: 100
:END:

* DONE Kickoff approved :milestone:
DEADLINE: <2026-07-27 Mon>

* TODO Design [1/3]
** DONE Wireframes
SCHEDULED: <2026-07-27 Mon> DEADLINE: <2026-08-03 Mon>
:PROPERTIES:
:PROGRESS: 100
:END:
** TODO Visual design system
SCHEDULED: <2026-08-04 Tue> DEADLINE: <2026-08-14 Fri>
:PROPERTIES:
:PROGRESS: 70
:END:
** TODO Prototype & usability test
SCHEDULED: <2026-08-12 Wed> DEADLINE: <2026-08-21 Fri>
:PROPERTIES:
:PROGRESS: 20
:END:

* TODO Build [0/4]
** TODO Backend API
SCHEDULED: <2026-08-10 Mon> DEADLINE: <2026-09-04 Fri>
:PROPERTIES:
:PROGRESS: 55
:END:
** TODO Web client
SCHEDULED: <2026-08-17 Mon> DEADLINE: <2026-09-18 Fri>
:PROPERTIES:
:PROGRESS: 30
:END:
** TODO Auth & billing
SCHEDULED: <2026-09-07 Mon> DEADLINE: <2026-09-18 Fri>
:PROPERTIES:
:PROGRESS: 0
:END:
** TODO Admin dashboard
SCHEDULED: <2026-09-14 Mon> DEADLINE: <2026-09-25 Fri>
:PROPERTIES:
:PROGRESS: 0
:END:

* TODO Feature freeze :milestone:
DEADLINE: <2026-09-28 Mon>

* TODO QA & polish [0/3]
** TODO Test plan & automation
SCHEDULED: <2026-09-21 Mon> DEADLINE: <2026-10-02 Fri>
:PROPERTIES:
:PROGRESS: 0
:END:
** TODO Bug bash
SCHEDULED: <2026-10-05 Mon> DEADLINE: <2026-10-09 Fri>
:PROPERTIES:
:PROGRESS: 0
:END:
** TODO Performance pass
SCHEDULED: <2026-10-12 Mon> DEADLINE: <2026-10-16 Fri>
:PROPERTIES:
:PROGRESS: 0
:END:

* TODO Go-to-market [0/3]
** TODO Marketing site
SCHEDULED: <2026-10-05 Mon> DEADLINE: <2026-10-16 Fri>
:PROPERTIES:
:PROGRESS: 0
:END:
** TODO Docs & onboarding
SCHEDULED: <2026-10-12 Mon> DEADLINE: <2026-10-23 Fri>
:PROPERTIES:
:PROGRESS: 0
:END:
** TODO Beta rollout
SCHEDULED: <2026-10-19 Mon> DEADLINE: <2026-10-27 Tue>
:PROPERTIES:
:PROGRESS: 0
:END:

* TODO Public launch :milestone:
DEADLINE: <2026-10-30 Fri>
"""

_LOFT_ORG = """\
#+TITLE: Loft Renovation
#+TARGET_DATE: <2026-10-16 Fri>
#+TODO: TODO | DONE
#+KANBAN_COLUMN: Next up
#+KANBAN_ORDER: 0
#+KANBAN_NOTE: Waiting on the permit to clear

* DONE Demolition & prep [2/2]
** DONE Clear & protect the space
SCHEDULED: <2026-08-17 Mon> DEADLINE: <2026-08-19 Wed>
:PROPERTIES:
:PROGRESS: 100
:END:
** DONE Demo non-structural walls
SCHEDULED: <2026-08-20 Thu> DEADLINE: <2026-08-25 Tue>
:PROPERTIES:
:PROGRESS: 100
:END:

* TODO Systems rough-in [0/3]
** TODO Electrical rough-in
SCHEDULED: <2026-08-26 Wed> DEADLINE: <2026-09-01 Tue>
:PROPERTIES:
:PROGRESS: 40
:END:
** TODO Plumbing rough-in
SCHEDULED: <2026-08-31 Mon> DEADLINE: <2026-09-04 Fri>
:PROPERTIES:
:PROGRESS: 10
:END:
** TODO HVAC ducting
SCHEDULED: <2026-09-02 Wed> DEADLINE: <2026-09-08 Tue>
:PROPERTIES:
:PROGRESS: 0
:END:

* TODO Rough-in inspection :milestone:
DEADLINE: <2026-09-09 Wed>

* TODO Finishes [0/3]
** TODO Drywall & paint
SCHEDULED: <2026-09-10 Thu> DEADLINE: <2026-09-22 Tue>
:PROPERTIES:
:PROGRESS: 0
:END:
** TODO Flooring
SCHEDULED: <2026-09-23 Wed> DEADLINE: <2026-10-02 Fri>
:PROPERTIES:
:PROGRESS: 0
:END:
** TODO Fixtures & finish carpentry
SCHEDULED: <2026-10-05 Mon> DEADLINE: <2026-10-14 Wed>
:PROPERTIES:
:PROGRESS: 0
:END:

* TODO Final walkthrough :milestone:
DEADLINE: <2026-10-16 Fri>
"""


def _demo_samples():
    return {
        "orbit-product-launch.org": _ORBIT_ORG,
        "loft-renovation.org": _LOFT_ORG,
    }


# --------------------------------------------------------------------------- #
#  HTTP handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    store = None          # set in main()
    server_version = "org-gantt/1.0"

    def log_message(self, fmt, *args):
        pass              # quiet; flip to super().log_message for debugging

    # -- helpers --
    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self):
        try:
            with open(HTML_PATH, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(500, "org-gantt-web.html not found next to server.py")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def _path(self):
        return urlparse(self.path).path

    # -- routing --
    def do_GET(self):
        path = self._path()
        if path in ("/", "/index.html", "/org-gantt-web.html"):
            return self._send_html()
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path == "/api/config":
            return self._send_json({
                "demo": self.store.demo,
                "dir": None if self.store.demo else os.path.basename(self.store.root),
                "version": "1.0",
            })
        if path == "/api/projects":
            return self._send_json(self.store.list())
        if path == "/api/board":
            return self._send_json(self.store.board())
        m = re.match(r"^/api/projects/([^/]+)$", path)
        if m:
            pid = unquote(m.group(1))
            try:
                return self._send_json(self.store.read(pid))
            except KeyError:
                return self._send_json({"error": "not found"}, 404)
            except ValueError as e:
                return self._send_json({"error": str(e)}, 400)
        return self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self._path() == "/api/projects":
            name = (self._body_json().get("name") or "New project").strip()
            try:
                result = self.store.create(name)
                if not self.store.demo:
                    log("created %s" % result.get("id"))
                return self._send_json(result, 201)
            except (OSError, ValueError) as e:
                return self._send_json({"error": str(e)}, 400)
        return self._send_json({"error": "not found"}, 404)

    def do_PUT(self):
        if self._path() == "/api/board":
            body = self._body_json()
            cols = body.get("columns")
            if not isinstance(cols, list):
                return self._send_json({"error": "missing columns"}, 400)
            colors = body.get("colors") if isinstance(body.get("colors"), dict) else {}
            try:
                result = self.store.write_board(cols, colors)
                if not self.store.demo:
                    log("board columns: %s (%d colored)" % (" | ".join(result["columns"]), len(result["colors"])))
                return self._send_json(result)
            except (OSError, ValueError) as e:
                return self._send_json({"error": str(e)}, 400)
        m = re.match(r"^/api/projects/([^/]+)$", self._path())
        if m:
            pid = unquote(m.group(1))
            text = self._body_json().get("text")
            if text is None:
                return self._send_json({"error": "missing text"}, 400)
            try:
                result = self.store.write(pid, text)
                if not self.store.demo:
                    log("updated %s (%d bytes)" % (pid, len(text.encode("utf-8"))))
                return self._send_json(result)
            except (OSError, ValueError) as e:
                return self._send_json({"error": str(e)}, 400)
        return self._send_json({"error": "not found"}, 404)

    def do_PATCH(self):
        m = re.match(r"^/api/projects/([^/]+)$", self._path())
        if m:
            pid = unquote(m.group(1))
            fields = self._body_json()
            try:
                result = self.store.patch(pid, fields)
                if not self.store.demo:
                    log("carded %s (%s)" % (pid, ", ".join(sorted(fields.keys())) or "—"))
                return self._send_json(result)
            except KeyError:
                return self._send_json({"error": "not found"}, 404)
            except (OSError, ValueError) as e:
                return self._send_json({"error": str(e)}, 400)
        return self._send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        m = re.match(r"^/api/projects/([^/]+)$", self._path())
        if m:
            pid = unquote(m.group(1))
            try:
                result = self.store.delete(pid)
                if not self.store.demo:
                    log("deleted %s" % pid)
                return self._send_json(result)
            except (OSError, ValueError) as e:
                return self._send_json({"error": str(e)}, 400)
        return self._send_json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser(description="org-gantt-web local server")
    ap.add_argument("--dir", default=os.path.join(HERE, "projects"),
                    help="directory holding your .org project files (default: ./projects)")
    ap.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8730, help="bind port (default: 8730)")
    ap.add_argument("--demo", action="store_true",
                    help="public-safe demo: in-memory samples only, no disk writes")
    args = ap.parse_args()

    Handler.store = DemoStore() if args.demo else FileStore(args.dir)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    where = "DEMO (samples, no disk writes)" if args.demo else Handler.store.root
    print("org-gantt-web serving %s" % where)
    print("open  http://%s:%d/" % ("localhost" if args.host == "127.0.0.1" else args.host, args.port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
