/* Tab identity and the active project. Every /api/chat and /api/confirm must
   carry tab_id: the server gives each tab its own ChatSession, gate and lock
   (webui.py _session_gate_lock_for_tab), and a confirmation can only be
   resolved through the same tab's resolver. An active project overrides the
   plain tab id, which is how a project gets its own scoped session.

   Same storage keys as the console (console.html:1949-2020), so moving
   between /console and /shell in one tab keeps one identity. */

export const TAB_ID_KEY = 'dm_tab_id_v1';
export const ACTIVE_PROJECT_KEY = 'dm_active_project_v1';

function fallbackStorage() {
  try {
    return globalThis.sessionStorage;
  } catch (_err) {
    return null;
  }
}

function randomId() {
  try {
    if (globalThis.crypto && globalThis.crypto.randomUUID) return globalThis.crypto.randomUUID();
  } catch (_err) {
    /* fall through */
  }
  return String(Date.now()) + Math.random().toString(36).slice(2);
}

export function createScope({ storage } = {}) {
  const store = storage === undefined ? fallbackStorage() : storage;
  const listeners = new Set();
  let memoryId = '';
  let memoryProject = null;

  const read = (key) => {
    try {
      return store ? store.getItem(key) : null;
    } catch (_err) {
      return null;
    }
  };
  const write = (key, value) => {
    try {
      if (store) store.setItem(key, value);
    } catch (_err) {
      /* private mode: identity is per page load then */
    }
  };
  const remove = (key) => {
    try {
      if (store) store.removeItem(key);
    } catch (_err) {
      /* nothing to do */
    }
  };
  const emit = () => listeners.forEach((fn) => {
    try {
      fn(scope.project());
    } catch (err) {
      console.error(err);
    }
  });

  const scope = {
    project() {
      const raw = read(ACTIVE_PROJECT_KEY);
      if (!raw) return memoryProject;
      try {
        const p = JSON.parse(raw);
        return p && typeof p === 'object' ? p : null;
      } catch (_err) {
        return null;
      }
    },
    setProject(project) {
      memoryProject = project;
      write(ACTIVE_PROJECT_KEY, JSON.stringify(project));
      emit();
    },
    leaveProject() {
      memoryProject = null;
      remove(ACTIVE_PROJECT_KEY);
      emit();
    },
    tabId() {
      const p = scope.project();
      if (p && p.tab_id) return String(p.tab_id);
      let id = read(TAB_ID_KEY);
      if (!id) {
        id = memoryId || randomId();
        memoryId = id;
        write(TAB_ID_KEY, id);
      }
      return id;
    },
    onChange(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    count: () => listeners.size,
    /* /shell?project=<tab_id>&name=<name>: how a native project window opens
       this page (console.html:8195). tab_id is a sanitised token, not a path. */
    applyQuery(search) {
      const q = new URLSearchParams(search || '');
      const id = (q.get('project') || '').trim();
      if (!id || !/^[A-Za-z0-9_-]{1,80}$/.test(id)) return false;
      const name = (q.get('name') || '').trim().slice(0, 120);
      scope.setProject({ tab_id: id, name: name || id, path: '' });
      return true;
    },
  };
  return scope;
}
