/* One fetch wrapper. It returns data or throws ApiError, and the message is
   always the server's own words, never a generic line.

   Why this exists: the console's api() threw "path HTTP 500" and discarded the
   server's JSON body (console.html:1557). The server's error shape is
   {"ok": false, "error": "..."} (webui.py _send_json), and at least 22 sites
   send ok:false with HTTP 200, so a 2xx body whose ok is false is an error
   too. `stale` comes from the service worker's X-Dourmouse-Stale marker on a
   cached /api/state (sw.js). `offline` means the network call itself failed. */

export class ApiError extends Error {
  constructor(message, info = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = info.status || 0;
    this.body = info.body === undefined ? null : info.body;
    this.path = info.path || '';
    this.stale = Boolean(info.stale);
    this.offline = Boolean(info.offline);
    if (info.cause) this.cause = info.cause;
  }
}

export function isAbort(err) {
  return Boolean(err) && (err.name === 'AbortError' || err.code === 20);
}

function looksJson(contentType, text) {
  if (contentType && contentType.includes('json')) return true;
  const t = text.trimStart();
  return t.startsWith('{') || t.startsWith('[');
}

function parseBody(res, text) {
  const ct = res.headers && res.headers.get ? res.headers.get('content-type') || '' : '';
  if (!text) return null;
  if (!looksJson(ct, text)) return null;
  try {
    return JSON.parse(text);
  } catch (_err) {
    return null;
  }
}

function messageFrom(data, path, status, statusText) {
  if (data && typeof data === 'object') {
    if (typeof data.error === 'string' && data.error.trim()) return data.error.trim();
    if (data.error && typeof data.error === 'object' && typeof data.error.message === 'string') {
      return data.error.message;
    }
    if (typeof data.message === 'string' && data.message.trim()) return data.message.trim();
    if (typeof data.detail === 'string' && data.detail.trim()) return data.detail.trim();
  }
  if (status >= 400) return path + ' answered HTTP ' + status + (statusText ? ' ' + statusText : '');
  return path + ' was refused by the server (ok was false and no reason was given)';
}

/* A resumable SSE parser: feed it text as it arrives, it calls onEvent once
   per complete "data:" record. The server writes "data: <json>\n\n". */
export function createSseParser(onEvent) {
  let carry = '';
  const line = (l) => {
    const s = l.endsWith('\r') ? l.slice(0, -1) : l;
    if (!s.startsWith('data:')) return;
    let evt;
    try {
      evt = JSON.parse(s.slice(5).trim());
    } catch (_err) {
      return;
    }
    if (evt && typeof evt === 'object') onEvent(evt);
  };
  return {
    push(text) {
      carry += text;
      const parts = carry.split('\n');
      carry = parts.pop() || '';
      for (const p of parts) line(p);
    },
    flush() {
      if (carry) line(carry);
      carry = '';
    },
  };
}

export function createApi({ fetch: fetchImpl, signal: defaultSignal } = {}) {
  const doFetch = (...args) => (fetchImpl || globalThis.fetch)(...args);

  async function send(method, path, body, opts = {}) {
    const signal = opts.signal || defaultSignal;
    const init = { method, credentials: 'same-origin', signal };
    if (body !== undefined) {
      init.headers = { 'Content-Type': 'application/json' };
      init.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await doFetch(path, init);
    } catch (err) {
      if (isAbort(err)) throw err;
      throw new ApiError('Cannot reach the Dourmouse server: ' + (err && err.message ? err.message : String(err)), {
        path,
        offline: true,
        cause: err,
      });
    }
    const stale = Boolean(res.headers && res.headers.get && res.headers.get('X-Dourmouse-Stale') === '1');
    let text = '';
    try {
      text = await res.text();
    } catch (err) {
      if (isAbort(err)) throw err;
      throw new ApiError('The connection dropped while reading ' + path, { path, status: res.status, offline: true, cause: err });
    }
    const data = parseBody(res, text);
    if (!res.ok) {
      throw new ApiError(messageFrom(data, path, res.status, res.statusText), {
        status: res.status,
        body: data,
        path,
        stale,
      });
    }
    if (!opts.allowOkFalse && data && typeof data === 'object' && !Array.isArray(data) && data.ok === false) {
      throw new ApiError(messageFrom(data, path, res.status, res.statusText), {
        status: res.status,
        body: data,
        path,
        stale,
      });
    }
    return { data, stale, status: res.status };
  }

  function markStale(data, stale) {
    if (stale && data && typeof data === 'object') {
      Object.defineProperty(data, '__stale', { value: true, enumerable: false });
    }
    return data;
  }

  const api = {
    /* Parsed JSON, or throws ApiError. A stale answer carries data.__stale. */
    async get(path, opts) {
      const r = await send('GET', path, undefined, opts);
      return markStale(r.data, r.stale);
    },
    async post(path, body, opts) {
      const r = await send('POST', path, body === undefined ? {} : body, opts);
      return markStale(r.data, r.stale);
    },
    /* Same as get/post but also tells you stale and status. */
    getMeta(path, opts) {
      return send('GET', path, undefined, opts);
    },
    postMeta(path, body, opts) {
      return send('POST', path, body === undefined ? {} : body, opts);
    },
    isStale(data) {
      return Boolean(data && data.__stale);
    },
    /* POST that reads an SSE body and calls onEvent for every record. */
    async stream(path, body, onEvent, opts = {}) {
      const signal = opts.signal || defaultSignal;
      let res;
      try {
        res = await doFetch(path, {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body === undefined ? {} : body),
          signal,
        });
      } catch (err) {
        if (isAbort(err)) throw err;
        throw new ApiError('Cannot reach the Dourmouse server: ' + (err && err.message ? err.message : String(err)), {
          path,
          offline: true,
          cause: err,
        });
      }
      if (!res.ok) {
        let text = '';
        try {
          text = await res.text();
        } catch (_err) {
          text = '';
        }
        const data = parseBody(res, text);
        throw new ApiError(messageFrom(data, path, res.status, res.statusText), { status: res.status, body: data, path });
      }
      const parser = createSseParser(onEvent);
      if (!res.body || !res.body.getReader) {
        parser.push(await res.text());
        parser.flush();
        return;
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      try {
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          parser.push(dec.decode(value, { stream: true }));
        }
        parser.push(dec.decode());
        parser.flush();
      } catch (err) {
        if (isAbort(err)) throw err;
        throw new ApiError('The stream from ' + path + ' ended early: ' + (err && err.message ? err.message : String(err)), {
          path,
          offline: true,
          cause: err,
        });
      }
    },
    /* The same api, but every call aborts when `signal` does. */
    withSignal(signal) {
      return createApi({ fetch: fetchImpl, signal });
    },
  };
  return api;
}
