# org-gantt-web

Repo: `org-gantt-web` (open source).
GitHub topics to set: `org-mode`, `emacs`, `gantt`, `plain-text`, `project-management`.
Tagline / repo description: "A web-based Gantt chart whose database is a plain org-mode file."

A single-file Gantt chart web app whose native storage format is org-mode plain text.
Built for solo project scheduling with a hard requirement of data sovereignty: the 
`.org` file is the database, not an export. It must always round-trip cleanly with 
Emacs org-mode.

## Current state

- `org-gantt-web.html` — the whole frontend: vanilla JS, no build step, no deps.
  Styling uses IBM Plex Sans/Mono with system fallbacks. Palette: paper/ink
  engineering aesthetic with safety-orange accents (CSS variables in `:root`).
- `server.py` — optional local backend (Python 3 stdlib only, no deps). Serves the
  HTML and exposes a small REST API over real `.org` files on disk, so the browser
  and Emacs edit the same files. This is the primary/recommended way to run.

### Three runtime modes (the frontend auto-detects at boot via `GET /api/config`)

- **server** — served by `server.py` without `--demo`. `.org` files live in the
  `--dir` folder (point it at Dropbox). Frontend lists them, opens/creates/deletes
  them, and **autosaves** every edit back to the file (debounced ~500ms PUT, atomic
  write server-side). Works in every browser incl. Brave/Safari — no browser file
  API needed. This replaced the aborted Flask idea; still zero-dependency.
- **demo** — `server.py --demo`. In-memory sample projects, all writes are no-ops;
  the frontend keeps edits in localStorage only. Public-safe (no host disk access,
  no cross-visitor state), intended for `gantt.orgtxt.com` as a preview-before-download.
- **standalone** — the single HTML opened with no backend (the GitHub-download story).
  Falls back to `localStorage` + the **File System Access API**: "Link file…" /
  "Sync ↑" (⌘S) / "Load ↓", handle remembered in IndexedDB (`org-gantt`/`kv`/`handle`),
  drift-guarded by `lastSyncText`. `FS_OK` gates it; where `showSaveFilePicker` is
  absent (Firefox/Safari, **and Brave by default** — it disables the API), the sync
  buttons show disabled and Download .org is the fallback. `localStorage` works
  everywhere.

Persistence entry points in the frontend: `saveStorage()` writes the localStorage
working copy (per-project key `org-gantt-doc[:<id>]` via `lsKey()`) and then branches
by `MODE` — server → `scheduleServerSave()`, standalone → File System Access sync bar,
demo → nothing. `MODE`, `currentProjectId`, and `projects` hold the mode state;
`openProject`/`newProject`/`deleteProject`/`loadProjectList` drive the switcher.

### Backend API (`server.py`)

- `GET /api/config` → `{demo, dir, version}` (frontend uses this to pick its mode).
- `GET /api/projects` → `[{id, name, mtime, recent, size}]`, sorted most-recent-first
  (`recent` = max(mtime, last-opened); last-opened tracked in `<dir>/.org-gantt-state.json`).
- `GET /api/projects/{id}` → `{id, name, text, mtime}` (also bumps last-opened).
- `POST /api/projects {name}` → creates `<slug>.org` from a starter template.
- `PUT /api/projects/{id} {text}` → atomic write (`.tmp` + `os.replace`).
- `DELETE /api/projects/{id}` → removes the file. Project `id` is the `.org` basename;
  `SAFE_ID` + a root-containment check block path traversal (matters once hosted).

Run it: `python3 server.py --dir ~/Dropbox/gantt` (defaults to `./projects`, port 8730).

## Org format contract (do not break)

This mapping is the core of the project. Any change must keep files readable by
stock Emacs org-mode and re-importable by the app's parser.

```org
#+TITLE: Bathroom Renovation
#+TARGET_DATE: <2026-09-20 Sun>
#+TODO: TODO | DONE

* TODO Standalone task
SCHEDULED: <2026-08-10 Mon> DEADLINE: <2026-08-13 Thu>
:PROPERTIES:
:PROGRESS: 25
:END:

* TODO Tile shower & floor [1/6]        ← phase (parent): NO timestamps, cookie derived
** DONE Install waterproofing membrane  ← child: own dates, own bar
SCHEDULED: <2026-08-22 Sat> DEADLINE: <2026-08-23 Sun>
:PROPERTIES:
:PROGRESS: 100
:END:

* TODO Rough-in inspection :milestone:  ← milestone: DEADLINE only, diamond marker
DEADLINE: <2026-08-19 Wed>
```

Rules:
- Task: heading with `SCHEDULED:` (bar start) + `DEADLINE:` (bar end).
- Milestone: heading tagged `:milestone:` with `DEADLINE:` only. Works at either level.
- Phase (group): any top-level heading that has child headings. Its span
  (min child start → max child end), progress (duration-weighted child average), and
  `[n/m]` cookie (DONE children / total) are ALL derived — never stored, never editable.
  Phase headings carry no timestamps so file and chart can't disagree.
- Progress: `:PROGRESS:` property (0–100) on leaf tasks. Legacy alternative: org
  checkboxes (`- [ ]` / `- [X]`) under a task drive progress instead; parser and
  serializer still support these, but the editing UI for them was removed in favor
  of child headings.
- Keyword flips TODO → DONE at 100%.
- Nesting is two levels by design (phases → children). The parser flattens deeper
  levels into children of their top heading. Rationale: if a child needs dated
  sub-steps, it should probably be its own phase. (n-level nesting is a possible
  future feature, not a current one.)

## Architecture notes

- Single IIFE; state shape:
  `{ title, targetDate, items: [{ id, type: 'task'|'milestone', name, start, end, progress, subs: [], children: [] }] }`
  A "phase" is just a task with non-empty `children` (see `isGroup()`).
- Dates are ISO strings; all math via noon-UTC Date objects to avoid TZ/DST edges
  (`d()`, `addDays()`, `diffDays()` helpers).
- `serialize(state)` / `parseOrg(text)` are the only translation layer. The visible
  textarea ("buffer") is editable; Apply runs `parseOrg`, chart edits re-run
  `serialize`. `bufferDirty` guards against clobbering user text edits.
- `syncGroups()` recomputes derived phase fields; called at the top of `render()`
  and during drags. Derived fields are cached on the item but treated as read-only.
- Rendering: absolutely-positioned bars on a day grid. `dayW` px/day is the **zoom
  level** (default 26, clamped `ZOOM_MIN`..`ZOOM_MAX` = 6..64). `− / Fit / +` in the
  toolbar call `zoomOut`/`zoomFit`/`zoomIn`; `setZoom` keeps the centered day fixed.
  `fitMode` (set by Fit) recomputes `dayW` each render to fit the whole span. Rows
  flattened via `visibleRows()` honoring the in-memory `collapsed` set. One `.daycol`
  per day (alternating `.odd` stripe, `.weekend` shaded); zoomed out (`dayW < 12`) the
  grid goes `.dense` (per-day borders off, Monday `.wkline`s on). The axis prints every
  day's number + weekday initial when `dayW >= 18`, else Mondays only (`.daynum.wk`).
  Today line (solid orange), target line (dashed orange).
- `chartRange()` spans the file's earliest task start → latest end/target, padded
  **±1 calendar month** (`addMonths`), start snapped to Monday. The chart is wider than
  its `.chart-scroll` container at normal zoom, so it scrolls/swipes horizontally with
  the row labels pinned (`position: sticky`); `scrollToToday()` (rAF-deferred for
  layout) sets the initial scroll so today sits ~30% from the left.
- Drag (`attachDrag`, one handler, **axis-locked**): a drag on the bar commits to one
  axis from the first ~4px of movement. Horizontal → reschedule (day-quantized; a
  phase's summary bar shifts all children by the same delta; the right-edge `.handle`
  resizes a leaf task's duration). Vertical → reorder: moves the item among its
  siblings only (top-level within `state.items`, a phase's children within that phase),
  showing a `.dropline` at the target gap, and rewrites the `.org` heading order on
  drop. Move/up listeners are bound to `window` (not the bar) because a reschedule
  re-renders the chart mid-drag and destroys the grabbed bar element — element-bound
  listeners would sever the drag after the first pixel. Cross-level moves (child ↔
  top-level) are not supported yet.
- The faux Emacs modeline under the buffer shows `**` when dirty. Keep it — it's the
  app's personality.

## Roadmap (discussed, not built)

1. ~~**Local file backend**~~ + ~~**multiple projects / recents switcher**~~ — DONE
   via `server.py` (see Current state). Follow-ups not yet built:
   - **Live reload**: server mode has no external-edit detection yet (last-write-wins
     PUT). Add mtime tracking + a poll or SSE so an Emacs save reflects in the browser,
     and warn instead of clobbering if the file changed under an open editor.
   - **Remote hosting at `gantt.orgtxt.com`**: `server.py` is deploy-ready, but a
     public deployment needs auth (reverse proxy / basic auth — none built) and HTTPS
     (Caddy/Let's Encrypt), and decides where the `.org` lives (server disk + Syncthing
     back to the laptop, or Emacs over TRAMP). `--demo` mode already covers the public
     preview-before-download use case safely.
   - Reflect save errors more visibly; a "New file…" affordance exists via the switcher.
2. Task dependencies (org could encode via `:BLOCKER:` / `:TRIGGER:` properties à la
   org-edna, or a custom `:AFTER:` property) + critical-path highlighting.
3. n-level nesting if two levels ever proves limiting.
4. Weekend-aware durations (working days vs calendar days).

## Conventions

- No frameworks, no build step — keep it a single file as long as practical.
- Any new metadata must live in org-native constructs (properties, tags, cookies)
  that degrade gracefully in Emacs.
- Preserve round-tripping: `parseOrg(serialize(state))` must be lossless for all
  supported constructs; unknown org content may be dropped on Apply (documented
  limitation), but never corrupted into invalid org.
