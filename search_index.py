"""Offline SQLite FTS5 catalogue for fast security search.

The index is disposable: callers build a temporary database and publish it with
``os.replace`` only after all rows have been written and checked.
"""
from __future__ import annotations

import os
import json
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path

_connections = threading.local()


def _manifest_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".manifest.json")


def _resolve(path: Path) -> Path | None:
    """Resolve the published index version without following user-controlled paths."""
    manifest = _manifest_path(path)
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        name = Path(str(raw["file"])).name
        target = path.parent / name
        if target.exists() and target.parent == path.parent:
            return target
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return path if path.exists() else None  # compatibility with pre-versioned indexes


def index_timestamp(path: str | Path) -> float | None:
    target = _resolve(Path(path))
    return target.stat().st_mtime if target else None


def _cleanup_old_versions(path: Path, keep: int = 3):
    """Bound disk growth; active Windows connections are retained for a later pass."""
    versions = sorted(path.parent.glob(f"{path.stem}.*.sqlite3"),
                      key=lambda candidate: candidate.stat().st_mtime_ns,
                      reverse=True)
    for old in versions[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def close_connection(path: str | Path | None = None):
    """Close the current thread's cached read-only connection (mainly for tests)."""
    ent = getattr(_connections, "entry", None)
    if ent and (path is None or ent[0] == str(Path(path))):
        ent[1].close()
        _connections.entry = None


def _connection(path: Path, target: Path):
    key = str(path)
    stamp = (str(target), target.stat().st_mtime_ns)
    ent = getattr(_connections, "entry", None)
    if ent and ent[0] == key and ent[2] == stamp:
        return ent[1]
    if ent:
        ent[1].close()
    con = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA cache_size=-4096")
    _connections.entry = (key, con, stamp)
    return con


def _tokens(value: str) -> str:
    value = "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", str(value or "").lower()))
    # Character tokens work for both Chinese substrings and Latin/code prefixes.
    return " ".join(dict.fromkeys(value))


def build_index(rows, path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.stem + ".", suffix=".tmp", dir=path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        con = sqlite3.connect(tmp)
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("CREATE TABLE securities (symbol TEXT PRIMARY KEY, name TEXT NOT NULL, code TEXT NOT NULL, type TEXT NOT NULL)")
        con.execute("CREATE VIRTUAL TABLE securities_fts USING fts5(symbol, name, code, tokens, content='securities', content_rowid='rowid')")
        clean = []
        seen = set()
        for row in rows or []:
            symbol = str(row.get("symbol") or "").strip().upper()
            name = str(row.get("name") or "").strip()
            code = str(row.get("code") or symbol.split(".")[0]).strip().upper()
            if not symbol or not name or symbol in seen:
                continue
            seen.add(symbol)
            clean.append((symbol, name, code, str(row.get("type") or "stock"),
                          _tokens(f"{symbol} {name} {code}")))
        con.executemany("INSERT INTO securities(symbol,name,code,type) VALUES (?,?,?,?)", [r[:4] for r in clean])
        # contentless rebuild is deterministic and avoids relying on rowid order.
        for rowid, row in enumerate(clean, 1):
            con.execute("INSERT INTO securities_fts(rowid,symbol,name,code,tokens) VALUES (?,?,?,?,?)",
                        (rowid, row[0], row[1], row[2], row[4]))
        con.execute("INSERT INTO securities_fts(securities_fts) VALUES ('optimize')")
        con.commit()
        con.close()
        version = path.with_name(f"{path.stem}.{int(time.time() * 1000)}.{uuid.uuid4().hex}.sqlite3")
        os.replace(tmp, version)
        manifest = _manifest_path(path)
        manifest_tmp = manifest.with_suffix(manifest.suffix + ".tmp")
        manifest_tmp.write_text(json.dumps({"file": version.name}, ensure_ascii=False), encoding="utf-8")
        os.replace(manifest_tmp, manifest)
        _cleanup_old_versions(path)
        return len(clean)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def search(path: str | Path, query: str, limit: int | None = None):
    q = _tokens(query)
    path = Path(path)
    target = _resolve(path)
    if not q or not target:
        return []
    con = _connection(path, target)
    raw_query = str(query).strip().lower()
    suffix = " LIMIT ?" if limit else ""
    exact = con.execute("SELECT symbol,name,code,type FROM securities WHERE lower(symbol)=? OR lower(name)=? OR lower(code)=?" + suffix,
                        (raw_query, raw_query, raw_query, limit) if limit else (raw_query, raw_query, raw_query)).fetchall()
    # Exact symbols, names and codes are unambiguous and must not pay for a broad FTS query.
    if exact:
        return [{"symbol": r[0], "name": r[1], "code": r[2], "type": r[3]} for r in exact]
    prefix = con.execute("SELECT symbol,name,code,type FROM securities WHERE lower(name) LIKE ? OR lower(code) LIKE ? OR lower(symbol) LIKE ?" + suffix,
                         (raw_query + "%", raw_query + "%", raw_query + "%", limit) if limit else (raw_query + "%", raw_query + "%", raw_query + "%")).fetchall()
    # AND on character tokens narrows candidates while Python keeps ranking exact.
    match = " AND ".join(q.split())
    rows = con.execute("SELECT s.symbol,s.name,s.code,s.type FROM securities_fts f JOIN securities s ON s.rowid=f.rowid WHERE securities_fts MATCH ?" + suffix,
                       (match, limit) if limit else (match,)).fetchall()
    merged = []
    seen = set()
    for row in exact + prefix + rows:
        if row[0] not in seen:
            seen.add(row[0])
            merged.append(row)
    return [{"symbol": r[0], "name": r[1], "code": r[2], "type": r[3]} for r in merged]
