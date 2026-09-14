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
  API needed. This replaced the aborted Flask idea; still zero-dependency. Also serves
  the **kanban board** of all projects (columns persisted in a `kanban.org` file in the
  same folder; per-card column/order/note in each project's own file).
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
- `GET /api/projects` → `[{id, name, mtime, recent, size, column, order, note}]`, sorted
  most-recent-first (`recent` = max(mtime, last-opened); last-opened tracked in
  `<dir>/.org-gantt-state.json`). `column`/`order`/`note` are the project's kanban card
  fields, parsed from its `#+KANBAN_*` header keywords. `kanban.org` is excluded (it's
  the board, not a project).
- `GET /api/projects/{id}` → `{id, name, text, mtime}` (also bumps last-opened).
- `POST /api/projects {name}` → creates `<slug>.org` from a starter template.
- `PUT /api/projects/{id} {text}` → atomic write (`.tmp` + `os.replace`).
- `PATCH /api/projects/{id} {title?, column?, order?, note?}` → in-place edit of just
  those header keywords (`apply_card_patch` / `set_header_keyword`), preserving the rest
  of the file. Used by the kanban board (move/rename/note a card). Does **not** bump
  last-opened, so dragging a card never reshuffles the switcher's recency order.
- `DELETE /api/projects/{id}` → removes the file. Project `id` is the `.org` basename;
  `SAFE_ID` + a root-containment check block path traversal (matters once hosted).
- `GET /api/board` → `{columns: [...]}` — the ordered kanban columns, read from the
  top-level headings of `kanban.org` (lazily created with `DEFAULT_COLUMNS` on first read).
- `PUT /api/board {columns}` → rewrites `kanban.org` (one `* heading` per column).

Run it: `python3 server.py --dir ~/Dropbox/gantt` (defaults to `./projects`, port 8730).
Mutating requests log a timestamped line to stdout (`created`/`updated`/`deleted`/`carded
<id>`, `board columns: …`, with byte count on updates); reads are silent, and `--demo`
logs nothing (`log()` helper). `--demo` keeps board + card edits in memory only.

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
- **Kanban card fields** (file-level header keywords, all optional): `#+KANBAN_COLUMN:`
  (which board column this project sits in — absent/unknown ⇒ the first column),
  `#+KANBAN_ORDER:` (integer sort key within the column), `#+KANBAN_NOTE:` (a one-line
  card note). The card's *title* is just the project's `#+TITLE`. `parseOrg`/`serialize`
  round-trip these verbatim, so editing a project in the gantt view never drops them.
- **The board file** `kanban.org` (a real, non-hidden `.org` in the project dir) holds
  only the ordered column list — one top-level heading per column. It is *not* a project
  (excluded from the listing). Column membership/order/notes live on the individual
  projects (above); `kanban.org` is just the column skeleton, so empty columns and column
  order survive. Reorder its headings in Emacs and the board reorders.

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
  per day (two alternating shades via `.odd`; weekends are NOT distinguished); zoomed out (`dayW < 12`) the
  grid goes `.dense` (per-day borders off, Monday `.wkline`s on). The axis prints every
  day's number + weekday initial when `dayW >= 18`, else Mondays only (`.daynum.wk`).
  Today line (solid orange), target line (dashed orange).
- Layout is two panes in a flex `.chart-grid`: a fixed-width `.label-col` (task-name
  cells, built alongside the bars in `renderChart` and kept row-aligned — `.label-head`
  matches the 52px axis, `.label-cell` matches the 44px rows) and the scrolling
  `.chart-scroll` holding the day grid + bars. Labels are their own column, never
  overlaying the grid. A `.col-resizer` splitter between them (`initColResizer`) drags
  the label column width (clamped 120–640px, persisted in localStorage `org-gantt-labelw`).
- `chartRange()` spans the file's earliest task start → latest end/target, padded
  **±1 week** (`addDays`), start snapped to Monday. The `.chart-scroll` pane is wider
  than its container at normal zoom, so only the timeline scrolls/swipes horizontally
  while the label column stays put; `scrollToToday()` (rAF-deferred for layout) sets the
  initial scroll so today sits ~30% from the left.
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
  app's personality. The buffer is **collapsed by default** (`.buffer.collapsed` hides the
  textarea/applybar/modeline/Revert; the `#bufBar` tabbar toggles it, caret rotates,
  state in `localStorage["org-gantt-buffer-collapsed"]`, absent ⇒ collapsed).
- **Theming** (`initTheme`, `#themeToggle` in the top `.appbar` — a full-width bar at the
  very top of the page, above the board, sized to the toggle's height with an empty
  `.appname` slot on the left reserved for a future app name; present in every mode).
  Light is the default `:root` palette; a dark palette redefines the same tokens
  under both `:root[data-theme="dark"]` and `@media (prefers-color-scheme: dark)
  :root:not([data-theme="light"])`, so with no explicit choice the OS wins and an explicit
  choice overrides. Every rule is written against the CSS variables (a handful of
  formerly-hardcoded colors became tokens: `--sel`, `--ink-soft`, `--accent-bg`,
  `--accent-border`; `color-scheme` is set per theme so native date pickers/scrollbars
  follow). The toggle stores `light`/`dark` in `localStorage["org-gantt-theme"]` (absent =
  follow OS) and swaps only the tokens — no re-render needed. The milestone diamond gets
  its color from `.milestone polygon { fill: var(--accent) }`, overriding the inline SVG
  fallback fill so it re-themes too.
- **Kanban board** (`#kanban` at the top of the page, server/demo only — standalone has
  one linked file and no board). Each card is a project; the board is built in
  `renderBoard()` from `board.columns` (loaded via `loadBoard()`) + the enriched
  `projects` list. `colOf(p)` resolves a card's visible column (its `column`, else the
  first) so a card is never lost on a rename/delete. Drag-and-drop is native HTML5 DnD
  (`draggable` + a module-level `drag = {kind:'card'|'col', …}`); cards drop into
  `.kcol-list`, columns reorder by dragging `.kcol-head`. `moveCard` renumbers the
  destination column (step 10) and persists only changed cards via `applyCardEdit`, which
  routes the **open** project through `state.kanban*` + the normal full-file autosave
  (so no PATCH races the debounced PUT) and every **other** card through
  `PATCH /api/projects/{id}`. Column ops (`addColumn`/`renameColumn`/`deleteColumn`/
  `reorderColumns`) mutate `board.columns` and `PUT /api/board`; rename/delete also
  re-`applyCardEdit` the affected cards. The card editor modal edits title (`#+TITLE`) +
  note. Collapse state is in `localStorage["org-gantt-kanban-collapsed"]`.

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
