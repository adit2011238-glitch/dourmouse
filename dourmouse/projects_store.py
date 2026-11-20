"""Projects store (plan 1.4, finding 199): projects that work like Claude projects.

A project has a name, a goal line, standing instructions, files, its own memory, work items, assigned agents, an
outputs shelf, a timeline and threads. One SQLite database holds the structure
(``<workspace>/projects/projects.db``). Each project also gets its own passage store
(``<workspace>/library/projects/<id>.db``: SQLite with FTS5 keyword search, passage-level citations). The user wiki is
a separate shared store and is not touched here. No project ever reads another project's passage store: every search
opens exactly one id.

Quick asks are not stored: nothing here is called for a turn that has no project. A thread's messages are stored only
when ``record_turn`` is called with a thread of a real project. Embeddings (Gemini, per folder) are plan 2.3; this
module keeps the keyword half and the passage layout so vectors can be added to the same db later.

Everything raises ``ValueError`` (bad input) or ``LookupError`` (no such thing); the routes turn those into 400 and 404.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import re
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from dourmouse.config import workspace_dir
from dourmouse.library.store import Store as LibraryStore

NAME_MAX = 120
GOAL_MAX = 300
INSTRUCTIONS_MAX = 8_000  # the same cap the workspace DOURMOUSE.md has on every turn's system prompt
TITLE_MAX = 200
NOTE_MAX = 4_000
FILE_MAX_BYTES = 5_000_000
FILES_PER_PROJECT = 200
PASSAGE_CHARS = 1_200
SEARCH_LIMIT_MAX = 50
WORK_STATUSES = ("todo", "doing", "done")
TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".py", ".js", ".ts", ".html", ".css", ".yaml",
                 ".yml", ".toml", ".rst", ".tex", ".sql", ".sh", ".swift", ".c", ".h", ".cpp", ".java", ".log"}
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,60}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Starter kits. Instructions are plain and short on purpose: the owner edits them.
TEMPLATES: dict[str, dict[str, Any]] = {
    "blank": {
        "label": "Blank", "goal": "", "instructions": "", "agents": [], "work_items": [],
    },
    "school": {
        "label": "School",
        "goal": "Keep coursework, deadlines and revision on track.",
        "instructions": (
            "This project is for school work. Cite sources in MLA. Keep deadlines in the work items. When asked for "
            "a summary, give the key points first and the detail after. Never write an assignment for submission as "
            "if it were the student's own without saying so."
        ),
        "agents": ["researcher", "docs", "planner"],
        "work_items": ["List this term's subjects and deadlines", "Add the course notes and past papers as files",
                       "Plan a weekly revision timetable"],
    },
    "research": {
        "label": "Research",
        "goal": "Find, read and weigh sources on one question.",
        "instructions": (
            "This project answers a research question. Prefer primary and open-access sources. Give every claim a "
            "source and say how strong the evidence is. Say plainly when the sources disagree or are thin."
        ),
        "agents": ["researcher", "librarian", "docs"],
        "work_items": ["Write the research question in one sentence", "Collect the first ten sources",
                       "Draft the findings and the limits"],
    },
    "personal_admin": {
        "label": "Personal admin",
        "goal": "Handle mail, forms, bookings and household paperwork.",
        "instructions": (
            "This project is for personal admin. Draft first, never send or pay without asking. Keep a short list of "
            "what is waiting on someone else and who. Do not store ID numbers, card numbers or passwords."
        ),
        "agents": ["mail_calendar", "planner", "docs"],
        "work_items": ["List what needs doing this month", "Add the forms and letters that need a reply"],
    },
    "coding": {
        "label": "Coding",
        "goal": "Build and maintain one codebase.",
        "instructions": (
            "This project is about one codebase. Read the code before changing it. Run the tests before saying "
            "something works. Keep changes small and say which files changed."
        ),
        "agents": ["coder", "security", "planner"],
        "work_items": ["Describe the codebase and how to run it", "List the first three changes wanted"],
    },
}


def _now() -> float:
    return round(time.time(), 3)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _clean_line(raw: Any, field: str, limit: int, *, required: bool = False) -> str:
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise ValueError(f"{field} must be text")
    text = " ".join(raw.split())
    if _CONTROL.search(raw):
        raise ValueError(f"{field} must not contain control characters")
    if required and not text:
        raise ValueError(f"{field} is required")
    if len(text) > limit:
        raise ValueError(f"{field} is too long ({len(text)} characters, the limit is {limit})")
    return text


def _clean_block(raw: Any, field: str, limit: int) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError(f"{field} must be text")
    if _CONTROL.search(raw):
        raise ValueError(f"{field} must not contain control characters")
    text = raw.strip()
    if len(text) > limit:
        raise ValueError(f"{field} is too long ({len(text)} characters, the limit is {limit})")
    return text


def chunks(text: str, size: int = PASSAGE_CHARS) -> list[str]:
    """Split on paragraph breaks into passages of at most ``size`` characters (a long paragraph is cut)."""
    out: list[str] = []
    current = ""
    for para in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        para = para.strip()
        if not para:
            continue
        while len(para) > size:
            if current:
                out.append(current)
                current = ""
            cut = para.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            out.append(para[:cut].strip())
            para = para[cut:].strip()
        if current and len(current) + len(para) + 2 > size:
            out.append(current)
            current = ""
        current = f"{current}\n\n{para}" if current else para
    if current:
        out.append(current)
    return [c for c in out if c]


def fts_query(raw: str) -> str:
    """User words as a safe FTS5 query: every word quoted, all required. No operators reach the engine."""
    words = re.findall(r"[\w'-]+", raw or "", flags=re.UNICODE)[:12]
    return " ".join('"' + w.replace('"', "") + '"' for w in words if w.strip("'-"))


#: A project's passage store is a library store (plan 2.1): the same file layout as the user wiki, with the
#: documents, vectors and embedding queue next to the FTS5 passages. The old name stays for callers and tests.
PassageStore = LibraryStore


_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, goal TEXT NOT NULL DEFAULT '', instructions TEXT NOT NULL DEFAULT '',
  template TEXT NOT NULL DEFAULT 'blank', cover TEXT NOT NULL DEFAULT '', agents TEXT NOT NULL DEFAULT '[]',
  pinned INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, last_active REAL NOT NULL);
CREATE TABLE IF NOT EXISTS files (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
  rel_path TEXT NOT NULL, indexed INTEGER NOT NULL DEFAULT 0, added_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS files_project ON files(project_id);
CREATE TABLE IF NOT EXISTS threads (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
  archived INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS threads_project ON threads(project_id);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread_id);
CREATE TABLE IF NOT EXISTS work_items (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'todo',
  assignee TEXT NOT NULL DEFAULT '', job_id TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS work_project ON work_items(project_id);
CREATE TABLE IF NOT EXISTS outputs (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'note',
  body TEXT NOT NULL DEFAULT '', file_id TEXT NOT NULL DEFAULT '', job_id TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS outputs_project ON outputs(project_id);
CREATE TABLE IF NOT EXISTS memory_notes (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS memory_project ON memory_notes(project_id);
CREATE TABLE IF NOT EXISTS timeline (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL, summary TEXT NOT NULL, ref TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS timeline_project ON timeline(project_id, at);
"""


class ProjectStore:
    def __init__(self, root: Path | None = None) -> None:
        base = Path(root) if root is not None else workspace_dir()
        self.dir = base / "projects"
        self.library = base / "library" / "projects"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.library.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "projects.db"
        self._lock = threading.RLock()
        with self._connect() as db:
            db.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        return db

    # ---- ids and stores -------------------------------------------------------------------------------------------

    def _need(self, db: sqlite3.Connection, project_id: str) -> sqlite3.Row:
        if not isinstance(project_id, str) or not _ID.match(project_id):
            raise LookupError("no such project")
        row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise LookupError("no such project")
        return row

    def passages(self, project_id: str) -> PassageStore:
        """This project's own passage store. The id is checked, so no path outside the folder can be built."""
        if not isinstance(project_id, str) or not _ID.match(project_id):
            raise LookupError("no such project")
        return LibraryStore(self.library / f"{project_id}.db", kind="project")

    def _log(self, db: sqlite3.Connection, project_id: str, kind: str, summary: str, ref: str = "") -> None:
        now = _now()
        db.execute("INSERT INTO timeline(project_id, at, kind, summary, ref) VALUES (?,?,?,?,?)",
                   (project_id, now, kind, summary[:300], ref))
        db.execute("UPDATE projects SET last_active=? WHERE id=?", (now, project_id))

    # ---- projects -------------------------------------------------------------------------------------------------

    @staticmethod
    def _project_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["agents"] = json.loads(d["agents"] or "[]")
        d["pinned"] = bool(d["pinned"])
        d["archived"] = bool(d["archived"])
        return d

    def create(self, name: str, *, template: str = "blank", goal: str | None = None,
               instructions: str | None = None, agents: list[str] | None = None) -> dict[str, Any]:
        name = _clean_line(name, "name", NAME_MAX, required=True)
        template = str(template or "blank").strip().lower().replace(" ", "_")
        kit = TEMPLATES.get(template)
        if kit is None:
            raise ValueError(f"no template called {template!r} (choose from: {', '.join(TEMPLATES)})")
        goal_v = _clean_line(kit["goal"] if goal is None else goal, "goal", GOAL_MAX)
        instr = _clean_block(kit["instructions"] if instructions is None else instructions, "instructions", INSTRUCTIONS_MAX)
        agent_ids = self._clean_agents(kit["agents"] if agents is None else agents)
        pid = f"{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:24] or 'project'}-{_new_id()[:6]}"
        if not _ID.match(pid):
            pid = f"project-{_new_id()}"
        now = _now()
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO projects(id, name, goal, instructions, template, agents, created_at, updated_at, last_active)"
                " VALUES (?,?,?,?,?,?,?,?,?)", (pid, name, goal_v, instr, template, json.dumps(agent_ids), now, now, now))
            for title in kit["work_items"]:
                db.execute("INSERT INTO work_items(id, project_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
                           (_new_id(), pid, title, now, now))
            self._log(db, pid, "created", f"Project created from the {kit['label']} template")
        if instr:
            self.passages(pid).replace("instructions", "instructions", "Project instructions", instr)
        return self.get(pid)

    @staticmethod
    def _clean_agents(raw: Any) -> list[str]:
        from dourmouse.agent_profiles import resolve

        if raw is None:
            return []
        if not isinstance(raw, (list, tuple)):
            raise ValueError("agents must be a list of agent ids")
        out: list[str] = []
        for item in raw:
            resolved = resolve(str(item))
            if resolved is None:
                raise ValueError(f"no agent called {item!r}")
            if resolved.agent_id not in out:
                out.append(resolved.agent_id)
        if len(out) > 14:
            raise ValueError("a project has at most 14 agents")
        return out

    def list(self, *, include_archived: bool = False, query: str = "") -> list[dict[str, Any]]:
        """Pinned first, then by recent activity. ``query`` matches name, goal and instructions (case-insensitive)."""
        with self._connect() as db:
            rows = db.execute("SELECT * FROM projects" + ("" if include_archived else " WHERE archived=0")  # noqa: S608 - only fixed column names and ? placeholders are joined into the text
                              + " ORDER BY pinned DESC, last_active DESC").fetchall()
            out = []
            needle = (query or "").strip().lower()
            for row in rows:
                d = self._project_dict(row)
                if needle and needle not in f"{d['name']} {d['goal']} {d['instructions']}".lower():
                    continue
                d["open_work"] = db.execute("SELECT COUNT(*) FROM work_items WHERE project_id=? AND status!='done'", (d["id"],)).fetchone()[0]
                d["threads"] = db.execute("SELECT COUNT(*) FROM threads WHERE project_id=? AND archived=0", (d["id"],)).fetchone()[0]
                d["instructions"] = d["instructions"][:200]  # the list shows a taste, the page shows all
                out.append(d)
        return out

    def get(self, project_id: str) -> dict[str, Any]:
        with self._connect() as db:
            d = self._project_dict(self._need(db, project_id))
            d["files"] = [dict(r) for r in db.execute("SELECT id, name, size, indexed, added_at FROM files WHERE project_id=? ORDER BY added_at", (project_id,))]
            d["work_items"] = [dict(r) for r in db.execute("SELECT * FROM work_items WHERE project_id=? ORDER BY created_at", (project_id,))]
            d["threads"] = [dict(r) for r in db.execute(
                "SELECT id, title, summary, archived, created_at, updated_at FROM threads WHERE project_id=? AND archived=0 ORDER BY updated_at DESC", (project_id,))]
            d["outputs"] = [dict(r) for r in db.execute(
                "SELECT id, title, kind, file_id, job_id, created_at FROM outputs WHERE project_id=? ORDER BY created_at DESC", (project_id,))]
            d["memory"] = [dict(r) for r in db.execute("SELECT id, text, created_at FROM memory_notes WHERE project_id=? ORDER BY created_at DESC", (project_id,))]
        d["passages"] = self.passages(project_id).count()
        return d

    def update(self, project_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {"name", "goal", "instructions", "cover", "agents", "pinned", "archived"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"cannot change {', '.join(sorted(unknown))}")
        sets: dict[str, Any] = {}
        if "name" in fields:
            sets["name"] = _clean_line(fields["name"], "name", NAME_MAX, required=True)
        if "goal" in fields:
            sets["goal"] = _clean_line(fields["goal"], "goal", GOAL_MAX)
        if "instructions" in fields:
            sets["instructions"] = _clean_block(fields["instructions"], "instructions", INSTRUCTIONS_MAX)
        if "cover" in fields:
            sets["cover"] = _clean_line(fields["cover"], "cover", 60)
        if "agents" in fields:
            sets["agents"] = json.dumps(self._clean_agents(fields["agents"]))
        for flag in ("pinned", "archived"):
            if flag in fields:
                if not isinstance(fields[flag], bool):
                    raise ValueError(f"{flag} must be true or false")
                sets[flag] = int(fields[flag])
        if not sets:
            raise ValueError("nothing to change")
        sets["updated_at"] = _now()
        with self._lock, self._connect() as db:
            self._need(db, project_id)
            db.execute(f"UPDATE projects SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), project_id))  # noqa: S608 - only fixed column names and ? placeholders are joined into the text
            if "instructions" in fields:
                self._log(db, project_id, "instructions", "Instructions changed")
            if "archived" in fields:
                self._log(db, project_id, "archived" if fields["archived"] else "restored",
                          "Project archived" if fields["archived"] else "Project restored")
            if "agents" in fields:
                self._log(db, project_id, "agents", "Assigned agents changed")
        if "instructions" in fields:
            p = self.passages(project_id)
            p.replace("instructions", "instructions", "Project instructions", sets["instructions"])
        return self.get(project_id)

    def delete(self, project_id: str) -> None:
        """Remove the project, its files, its passage store and its threads. The owner confirms this in the screen."""
        with self._lock, self._connect() as db:
            self._need(db, project_id)
            for table in ("files", "threads", "work_items", "outputs", "memory_notes", "timeline"):
                if table == "threads":
                    db.execute("DELETE FROM messages WHERE thread_id IN (SELECT id FROM threads WHERE project_id=?)", (project_id,))
                db.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))  # noqa: S608 - fixed names
            db.execute("DELETE FROM projects WHERE id=?", (project_id,))
        shutil.rmtree(self.dir / project_id, ignore_errors=True)
        for suffix in ("", "-wal", "-shm"):
            (self.library / f"{project_id}.db{suffix}").unlink(missing_ok=True)

    # ---- files ----------------------------------------------------------------------------------------------------

    def add_file(self, project_id: str, name: str, *, text: str | None = None, data: bytes | None = None) -> dict[str, Any]:
        """Store a copy of a file in the project's own folder and, if it is text, index its passages."""
        name = _clean_line(name, "name", 200, required=True)
        if "/" in name or "\\" in name or name in {".", ".."}:
            raise ValueError("name must be a file name, not a path")
        if (text is None) == (data is None):
            raise ValueError("give either text or data")
        raw = text.encode("utf-8") if text is not None else data
        assert raw is not None
        if len(raw) > FILE_MAX_BYTES:
            raise ValueError(f"the file is {len(raw)} bytes; the limit is {FILE_MAX_BYTES}")
        fid = _new_id()
        rel = f"files/{fid}-{name}"
        with self._lock, self._connect() as db:
            self._need(db, project_id)
            if db.execute("SELECT COUNT(*) FROM files WHERE project_id=?", (project_id,)).fetchone()[0] >= FILES_PER_PROJECT:
                raise ValueError(f"a project holds at most {FILES_PER_PROJECT} files")
            target = self.dir / project_id / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            indexable = Path(name).suffix.lower() in TEXT_SUFFIXES
            body = ""
            if indexable:
                try:
                    body = raw.decode("utf-8")
                except UnicodeDecodeError:
                    indexable = False
            db.execute("INSERT INTO files(id, project_id, name, size, sha256, rel_path, indexed, added_at) VALUES (?,?,?,?,?,?,?,?)",
                       (fid, project_id, name, len(raw), hashlib.sha256(raw).hexdigest(), rel, int(indexable), _now()))
            self._log(db, project_id, "file", f"File added: {name}", fid)
        if indexable:
            self.passages(project_id).replace("file", fid, name, body)
        return {"id": fid, "name": name, "size": len(raw), "indexed": indexable}

    def read_file(self, project_id: str, file_id: str) -> tuple[str, bytes]:
        with self._connect() as db:
            self._need(db, project_id)
            row = db.execute("SELECT name, rel_path FROM files WHERE id=? AND project_id=?", (file_id, project_id)).fetchone()
        if row is None:
            raise LookupError("no such file")
        path = (self.dir / project_id / row["rel_path"]).resolve()
        if (self.dir / project_id).resolve() not in path.parents:
            raise LookupError("no such file")
        return row["name"], path.read_bytes()

    def remove_file(self, project_id: str, file_id: str) -> None:
        with self._lock, self._connect() as db:
            self._need(db, project_id)
            row = db.execute("SELECT name, rel_path FROM files WHERE id=? AND project_id=?", (file_id, project_id)).fetchone()
            if row is None:
                raise LookupError("no such file")
            db.execute("DELETE FROM files WHERE id=?", (file_id,))
            (self.dir / project_id / row["rel_path"]).unlink(missing_ok=True)
            self._log(db, project_id, "file", f"File removed: {row['name']}")
        self.passages(project_id).remove("file", file_id)

    # ---- project memory (its own notes, searched with the files) ---------------------------------------------------

    def remember(self, project_id: str, text: str) -> dict[str, Any]:
        text = _clean_block(text, "text", NOTE_MAX)
        if not text:
            raise ValueError("text is required")
        nid = _new_id()
        with self._lock, self._connect() as db:
            self._need(db, project_id)
            db.execute("INSERT INTO memory_notes(id, project_id, text, created_at) VALUES (?,?,?,?)", (nid, project_id, text, _now()))
            self._log(db, project_id, "memory", "Remembered: " + text[:120], nid)
        self.passages(project_id).replace("memory", nid, "Project memory", text)
        return {"id": nid, "text": text}

    def forget(self, project_id: str, note_id: str) -> None:
        with self._lock, self._connect() as db:
            self._need(db, project_id)
            if not db.execute("DELETE FROM memory_notes WHERE id=? AND project_id=?", (note_id, project_id)).rowcount:
                raise LookupError("no such memory")
            self._log(db, project_id, "memory", "A memory was forgotten", note_id)
        self.passages(project_id).remove("memory", note_id)

    def search(self, project_id: str, query: str, limit: int = 8) -> builtins.list[dict[str, Any]]:
        with self._connect() as db:
            self._need(db, project_id)
        return self.passages(project_id).search(query, limit)

    # ---- work items -----------------------------------------------------------------------------------------------

    def add_work(self, project_id: str, title: str, *, assignee: str = "", notes: str = "") -> dict[str, Any]:
        title = _clean_line(title, "title", TITLE_MAX, required=True)
        notes = _clean_block(notes, "notes", NOTE_MAX)
        who = self._clean_agents([assignee])[0] if assignee else ""
        wid, now = _new_id(), _now()
        with self._lock, self._connect() as db:
            self._need(db, project_id)
            db.execute("INSERT INTO work_items(id, project_id, title, assignee, notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                       (wid, project_id, title, who, notes, now, now))
            self._log(db, project_id, "work", f"Work item added: {title}", wid)
        return self.work_item(project_id, wid)

    def work_item(self, project_id: str, work_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM work_items WHERE id=? AND project_id=?", (work_id, project_id)).fetchone()
        if row is None:
            raise LookupError("no such work item")
        return dict(row)

    def update_work(self, project_id: str, work_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {"title", "status", "assignee", "notes", "job_id"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"cannot change {', '.join(sorted(unknown))}")
        sets: dict[str, Any] = {}
        if "title" in fields:
            sets["title"] = _clean_line(fields["title"], "title", TITLE_MAX, required=True)
        if "status" in fields:
            if fields["status"] not in WORK_STATUSES:
                raise ValueError(f"status must be one of: {', '.join(WORK_STATUSES)}")
            sets["status"] = fields["status"]
        if "assignee" in fields:
            sets["assignee"] = self._clean_agents([fields["assignee"]])[0] if fields["assignee"] else ""
        if "notes" in fields:
            sets["notes"] = _clean_block(fields["notes"], "notes", NOTE_MAX)
        if "job_id" in fields:
            sets["job_id"] = _clean_line(fields["job_id"], "job_id", 80)
        if not sets:
            raise ValueError("nothing to change")
        sets["updated_at"] = _now()
        with self._lock, self._connect() as db:
            self._need(db, project_id)
            self.work_item(project_id, work_id)
            db.execute(f"UPDATE work_items SET {', '.join(k + '=?' for k in sets)} WHERE id=? AND project_id=?",  # noqa: S608 - only fixed column names and ? placeholders are joined into the text
                       (*sets.values(), work_id, project_id))
            if "status" in sets:
                item = db.execute("SELECT title FROM work_items WHERE id=?", (work_id,)).fetchone()
                self._log(db, project_id, "work", f"{item['title']}: {sets['status']}", work_id)
        return self.work_item(project_id, work_id)

    def remove_work(self, project_id: str, work_id: str) -> None:
        with self._lock, self._connect() as db:
            self._need(db, project_id)
            if not db.execute("DELETE FROM work_items WHERE id=? AND project_id=?", (work_id, project_id)).rowcount:
                raise LookupError("no such work item")

    # ---- outputs shelf --------------------------------------------------------------------------------------------

    def add_output(self, project_id: str, title: str, *, kind: str = "note", body: str = "", file_id: str = "",
                   job_id: str = "") -> dict[str, Any]:
        title = _clean_line(title, "title", TITLE_MAX, required=True)
        kind = _clean_line(kind or "note", "kind", 30)
        body = _clean_block(body, "body", 50_000)
        oid = _new_id()
        with self._lock, self._connect() as db:
            self._need(db, project_id)
            if file_id and not db.execute("SELECT 1 FROM files WHERE id=? AND project_id=?", (file_id, project_id)).fetchone():
                raise ValueError("that file is not in this project")
            db.execute("INSERT INTO outputs(id, project_id, title, kind, body, file_id, job_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
                       (oid, project_id, title, kind, body, file_id, _clean_line(job_id, "job_id", 80), _now()))
            self._log(db, project_id, "output", f"Output added: {title}", oid)
        return {"id": oid, "title": title, "kind": kind}

    def get_output(self, project_id: str, output_id: str) -> dict[str, Any]:
        with self._connect() as db:
            self._need(db, project_id)
            row = db.execute("SELECT * FROM outputs WHERE id=? AND project_id=?", (output_id, project_id)).fetchone()
        if row is None:
            raise LookupError("no such output")
        return dict(row)

    # ---- threads --------------------------------------------------------------------------------------------------

    def new_thread(self, project_id: str, title: str = "New thread") -> dict[str, Any]:
        title = _clean_line(title or "New thread", "title", TITLE_MAX, required=True)
        tid, now = _new_id(), _now()
        with self._lock, self._connect() as db:
            self._need(db, project_id)
            db.execute("INSERT INTO threads(id, project_id, title, created_at, updated_at) VALUES (?,?,?,?,?)", (tid, project_id, title, now, now))
            self._log(db, project_id, "thread", f"Thread started: {title}", tid)
        return {"id": tid, "title": title}

    def _thread(self, db: sqlite3.Connection, project_id: str, thread_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM threads WHERE id=? AND project_id=?", (thread_id, project_id)).fetchone()
        if row is None:
            raise LookupError("no such thread")
        return row

    def record_turn(self, project_id: str, thread_id: str, user: str, assistant: str) -> None:
        """Save one exchange. Only ever called with a real project thread: a quick ask has none, so it is never stored."""
        user = _clean_block(user, "user", 100_000)
        assistant = _clean_block(assistant, "assistant", 100_000)
        now = _now()
        with self._lock, self._connect() as db:
            self._need(db, project_id)
            row = self._thread(db, project_id, thread_id)
            db.executemany("INSERT INTO messages(thread_id, role, content, at) VALUES (?,?,?,?)",
                           [(thread_id, "user", user, now), (thread_id, "assistant", assistant, now)])
            title = row["title"]
            if title == "New thread" and user:
                title = " ".join(user.split())[:60]
            db.execute("UPDATE threads SET updated_at=?, title=? WHERE id=?", (now, title, thread_id))
            db.execute("UPDATE projects SET last_active=? WHERE id=?", (now, project_id))

    def thread(self, project_id: str, thread_id: str, limit: int = 200) -> dict[str, Any]:
        with self._connect() as db:
            self._need(db, project_id)
            row = self._thread(db, project_id, thread_id)
            msgs = db.execute("SELECT role, content, at FROM messages WHERE thread_id=? ORDER BY id DESC LIMIT ?", (thread_id, max(1, min(limit, 1000)))).fetchall()
        return {**dict(row), "messages": [dict(m) for m in reversed(msgs)]}

    def summarise_thread(self, project_id: str, thread_id: str, summary: str) -> None:
        """Keep a short summary of a thread and make it searchable as project memory (never the whole transcript)."""
        summary = _clean_block(summary, "summary", NOTE_MAX)
        with self._lock, self._connect() as db:
            self._need(db, project_id)
            row = self._thread(db, project_id, thread_id)
            db.execute("UPDATE threads SET summary=? WHERE id=?", (summary, thread_id))
            title = row["title"]
        self.passages(project_id).replace("thread", thread_id, f"Thread: {title}", summary)

    def archive_thread(self, project_id: str, thread_id: str) -> None:
        with self._lock, self._connect() as db:
            self._need(db, project_id)
            self._thread(db, project_id, thread_id)
            db.execute("UPDATE threads SET archived=1 WHERE id=?", (thread_id,))

    # ---- timeline and context ---------------------------------------------------------------------------------------

    def timeline(self, project_id: str, limit: int = 100) -> builtins.list[dict[str, Any]]:
        with self._connect() as db:
            self._need(db, project_id)
            rows = db.execute("SELECT at, kind, summary, ref FROM timeline WHERE project_id=? ORDER BY id DESC LIMIT ?",
                              (project_id, max(1, min(limit, 500)))).fetchall()
        return [dict(r) for r in rows]

    def note_event(self, project_id: str, kind: str, summary: str, ref: str = "") -> None:
        """For other modules (jobs, agents) to put an entry on a project's timeline."""
        with self._lock, self._connect() as db:
            self._need(db, project_id)
            self._log(db, project_id, _clean_line(kind, "kind", 30, required=True), _clean_line(summary, "summary", 300, required=True), ref)

    def context_for(self, project_id: str, question: str = "", passages: int = 4) -> str:
        """The text a turn in this project is seeded with: instructions, assigned agents, file names and the best
        passages for the question, with citations. Only this project's store is searched."""
        proj = self.get(project_id)
        lines = [f'You are working inside the project "{proj["name"]}".']
        if proj["goal"]:
            lines.append(f"Goal: {proj['goal']}")
        if proj["instructions"]:
            lines += ["Standing instructions for this project (follow them):", proj["instructions"]]
        if proj["agents"]:
            lines.append("Agents assigned to this project: " + ", ".join(proj["agents"]) + ".")
        if proj["files"]:
            lines.append("Files in this project: " + ", ".join(f["name"] for f in proj["files"][:30]) + ".")
        hits = [h for h in self.passages(project_id).search(question, passages) if h["source"] != "instructions"] if question.strip() else []
        if hits:
            lines.append("Relevant passages from this project (cite them as given):")
            lines += [f"- ({h['citation']}) {h['text'][:600]}" for h in hits]
        return "\n".join(lines)


_store: ProjectStore | None = None
_store_lock = threading.Lock()


def get_project_store() -> ProjectStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = ProjectStore()
        return _store


def set_project_store(store: ProjectStore | None) -> None:
    global _store
    with _store_lock:
        _store = store
