"""Turso (libSQL) over HTTP, shaped like the bits of sqlite3 this app uses. Standard library only.

Set TURSO_DATABASE_URL (libsql://<db>-<org>.turso.io) and TURSO_AUTH_TOKEN and the whole app uses Turso instead of gk.db.
Speaks Turso's "Hrana over HTTP" v2 protocol (POST /v2/pipeline). Each statement auto-commits, so commit() is a no-op.
HTTPS connections are pooled and kept alive, so a query costs one network round trip: put the Turso database in the
same region as the backend (e.g. both in Singapore or both in Oregon)."""
import base64, http.client, json, queue, sqlite3, threading
from urllib.parse import urlparse

VERSION = [0]                                 # bumped on every write; lets readers cache results (see site.cached_site_data)
_vlock = threading.Lock()

def _bump():
    with _vlock: VERSION[0] += 1

def configured(env=None):
    import os
    env = os.environ if env is None else env
    return bool(env.get("TURSO_DATABASE_URL"))

class Row:
    """Works like sqlite3.Row: row[0], row["col"], dict(row), tuple unpacking."""
    __slots__ = ("_cols", "_vals", "_idx")
    def __init__(self, cols, vals, idx): self._cols, self._vals, self._idx = cols, vals, idx
    def keys(self): return list(self._cols)
    def __getitem__(self, k):
        if isinstance(k, str): return self._vals[self._idx[k]]
        return self._vals[k]
    def __iter__(self): return iter(self._vals)
    def __len__(self): return len(self._vals)
    def __eq__(self, o): return tuple(self) == tuple(o)
    def __repr__(self): return f"Row({dict(zip(self._cols, self._vals))})"

class Cursor:
    def __init__(self, rows=(), lastrowid=None, rowcount=-1):
        self._rows, self._i, self.lastrowid, self.rowcount = list(rows), 0, lastrowid, rowcount
    def fetchone(self):
        if self._i >= len(self._rows): return None
        self._i += 1; return self._rows[self._i - 1]
    def fetchall(self):
        r = self._rows[self._i:]; self._i = len(self._rows); return r
    def __iter__(self):
        while (r := self.fetchone()) is not None: yield r

def _arg(v):
    if v is None: return {"type": "null"}
    if isinstance(v, bool): return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int): return {"type": "integer", "value": str(v)}
    if isinstance(v, float): return {"type": "float", "value": v}
    if isinstance(v, (bytes, bytearray, memoryview)): return {"type": "blob", "base64": base64.b64encode(bytes(v)).decode().rstrip("=")}
    return {"type": "text", "value": str(v)}

def _val(c):
    t = c.get("type")
    if t == "integer": return int(c["value"])
    if t == "float": return float(c["value"])
    if t == "text": return c["value"]
    if t == "blob":
        b = c.get("base64", ""); return base64.b64decode(b + "=" * (-len(b) % 4))
    return None

def _error(e):
    msg = (e or {}).get("message", "Turso error")
    if "constraint" in msg.lower(): return sqlite3.IntegrityError(msg)
    return sqlite3.OperationalError(msg)

_READ = ("SELECT", "PRAGMA TABLE_INFO", "WITH", "EXPLAIN")

class _Pool:
    def __init__(self, host, tls=True, size=8): self.host, self.tls, self.q = host, tls, queue.LifoQueue(size)
    def get(self):
        try: return self.q.get_nowait()
        except queue.Empty:
            return (http.client.HTTPSConnection if self.tls else http.client.HTTPConnection)(self.host, timeout=60)
    def put(self, c):
        try: self.q.put_nowait(c)
        except queue.Full: c.close()

_pools, _plock = {}, threading.Lock()

class Connection:
    def __init__(self, url, token, transport=None):
        u = urlparse(url.replace("libsql://", "https://", 1))      # http://127.0.0.1:8080 works too (`turso dev` local server)
        self.host, self.token = u.netloc, token
        self.transport = transport                    # tests may pass a function(body_dict) -> response_dict
        if not transport:
            with _plock: self.pool = _pools.setdefault(u.scheme + "://" + self.host, _Pool(self.host, u.scheme != "http"))

    def _post(self, body):
        if self.transport: return self.transport(body)
        raw, last = json.dumps(body).encode(), None
        for attempt in (1, 2):                        # a pooled keep-alive socket may have been closed by the server: retry once
            c = self.pool.get()
            try:
                c.request("POST", "/v2/pipeline", raw, {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
                r = c.getresponse(); data = r.read()
                if r.status >= 400:
                    c.close()
                    raise sqlite3.OperationalError(f"Turso HTTP {r.status}: {data[:300].decode('utf-8', 'replace')}")
                self.pool.put(c); return json.loads(data)
            except (http.client.HTTPException, ConnectionError, OSError) as e:
                c.close(); last = e
        raise sqlite3.OperationalError(f"Cannot reach Turso: {last}")

    def _run(self, reqs):
        res = self._post({"requests": reqs + [{"type": "close"}]})["results"]
        return res[:-1]

    @staticmethod
    def _stmt(sql, params):
        st = {"sql": sql, "want_rows": True}
        if isinstance(params, dict): st["named_args"] = [{"name": (k if k[:1] in ":@$" else ":" + k), "value": _arg(v)} for k, v in params.items()]
        elif params: st["args"] = [_arg(v) for v in params]
        return st

    @staticmethod
    def _cursor(r):
        if r["type"] != "ok": raise _error(r.get("error"))
        res = r["response"]["result"]
        cols = [c.get("name") or "" for c in res.get("cols", [])]
        idx = {c: i for i, c in enumerate(cols)}
        rows = [Row(cols, [_val(v) for v in row], idx) for row in res.get("rows", [])]
        lid = res.get("last_insert_rowid")
        return Cursor(rows, int(lid) if lid not in (None, "") else None, res.get("affected_row_count", -1))

    def execute(self, sql, params=()):
        if not sql.lstrip().upper().startswith(_READ): _bump()
        return self._cursor(self._run([{"type": "execute", "stmt": self._stmt(sql, params)}])[0])

    def execute_many(self, items):
        """Several statements in ONE round trip (used by the copy tool). items = [(sql, params), ...]"""
        _bump()
        for r in self._run([{"type": "execute", "stmt": self._stmt(s, p)} for s, p in items]):
            if r["type"] != "ok": raise _error(r.get("error"))

    def executescript(self, script):
        _bump()
        r = self._run([{"type": "sequence", "sql": script}])[0]
        if r["type"] != "ok": raise _error(r.get("error"))

    def commit(self): pass
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass

def connect(url=None, token=None, transport=None):
    import os
    return Connection(url or os.environ["TURSO_DATABASE_URL"], token if token is not None else os.getenv("TURSO_AUTH_TOKEN", ""), transport)
