"""SR 저장소 + 감사 로그 (SQLite).

감사 로그는 append-only: UPDATE/DELETE 를 트리거로 막고, 각 행이 이전 행의 해시를 포함하는 해시 체인이라
사후 변조를 verify_audit() 으로 탐지할 수 있다.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sr (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL, body TEXT NOT NULL, requester TEXT NOT NULL, project TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL, sr_type TEXT NOT NULL DEFAULT '', confidence REAL, reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, sr_id INTEGER, actor TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL,
  prev_hash TEXT NOT NULL, hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE, gitlab_url TEXT NOT NULL DEFAULT '', default_branch TEXT NOT NULL DEFAULT 'main',
  local_path TEXT NOT NULL, schema_doc TEXT NOT NULL DEFAULT '', token_env TEXT NOT NULL DEFAULT '',
  doc_paths TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sr_impact (
  sr_id INTEGER PRIMARY KEY, keywords TEXT NOT NULL, candidates TEXT NOT NULL, selected TEXT NOT NULL DEFAULT '[]',
  vector_used INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sr_patch (
  id INTEGER PRIMARY KEY AUTOINCREMENT, sr_id INTEGER NOT NULL, version INTEGER NOT NULL,
  status TEXT NOT NULL, explanation TEXT NOT NULL DEFAULT '', diff TEXT NOT NULL DEFAULT '',
  stats TEXT NOT NULL DEFAULT '{}', warnings TEXT NOT NULL DEFAULT '[]', feedback TEXT NOT NULL DEFAULT '',
  decided_by TEXT NOT NULL DEFAULT '', decision_note TEXT NOT NULL DEFAULT '', branch TEXT NOT NULL DEFAULT '',
  commit_sha TEXT NOT NULL DEFAULT '', mr_url TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS doc_patch (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, commit_sha TEXT NOT NULL, doc_path TEXT NOT NULL,
  doc_sha TEXT NOT NULL, status TEXT NOT NULL, changes TEXT NOT NULL, sr_ids TEXT NOT NULL DEFAULT '[]',
  summary TEXT NOT NULL DEFAULT '', output TEXT NOT NULL DEFAULT '', decided_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(project, commit_sha, doc_path)
);
CREATE TABLE IF NOT EXISTS sr_data (
  sr_id INTEGER PRIMARY KEY, schema_source TEXT NOT NULL, tables TEXT NOT NULL, related TEXT NOT NULL,
  statements TEXT NOT NULL, notes TEXT NOT NULL, diffs TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS code_file (
  project TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, sha TEXT NOT NULL,
  PRIMARY KEY (project, path)
);
CREATE TABLE IF NOT EXISTS code_chunk (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, path TEXT NOT NULL, cls TEXT NOT NULL,
  method TEXT NOT NULL, line INTEGER NOT NULL, text TEXT NOT NULL, snippet TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS code_chunk_pf ON code_chunk(project, path);
CREATE TABLE IF NOT EXISTS code_summary (
  project TEXT NOT NULL, path TEXT NOT NULL, cls TEXT NOT NULL, sha TEXT NOT NULL, summary TEXT NOT NULL,
  PRIMARY KEY (project, path, cls)
);
CREATE TABLE IF NOT EXISTS code_index (
  project TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 0, built_at TEXT NOT NULL DEFAULT '',
  files INTEGER NOT NULL DEFAULT 0, chunks INTEGER NOT NULL DEFAULT 0, summaries INTEGER NOT NULL DEFAULT 0,
  embedded INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS embed_cache (sha TEXT NOT NULL, model TEXT NOT NULL, vec BLOB NOT NULL, PRIMARY KEY (sha, model));
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""

STATUSES = {"received", "classifying", "classified", "needs_review", "pending_llm",
            "analyzing", "impact_ready", "impact_error", "impact_confirmed",
            "data_analyzing", "data_ready", "data_error", "data_done",
            "patch_generating", "patch_ready", "patch_failed", "rejected", "committed", "mr_created"}
TYPES = {"code", "data", "unknown"}

_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?key|token)\b(\s*[=:]\s*)(\S+)"),
    re.compile(r"(?i)\b(bearer)(\s+)([A-Za-z0-9._\-]{12,})"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]


def mask_secrets(text: str) -> str:
    """프롬프트/응답을 감사 로그에 남기기 전 비밀값 패턴을 가린다."""
    out = text
    for pat in _SECRET_PATTERNS:
        if pat.groups >= 3:
            out = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}***", out)
        else:
            out = pat.sub("***", out)
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path, mask: bool = True):
        self.path = str(path)
        self.mask = mask
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            cols = {r["name"] for r in self._db.execute("PRAGMA table_info(project)")}
            if "db_env_prefix" not in cols:   # 이전 버전 DB 호환
                self._db.execute("ALTER TABLE project ADD COLUMN db_env_prefix TEXT NOT NULL DEFAULT ''")
            self._db.commit()

    # ---- 감사 로그 ----
    @staticmethod
    def _hash(prev: str, ts: str, sr_id, actor: str, event: str, detail: str) -> str:
        raw = "\x1f".join([prev, ts, "" if sr_id is None else str(sr_id), actor, event, detail])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def audit(self, sr_id: int | None, actor: str, event: str, detail: dict | None = None) -> int:
        d = json.dumps(detail or {}, ensure_ascii=False, sort_keys=True)
        if self.mask:
            d = mask_secrets(d)
        with self._lock:
            row = self._db.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
            prev = row["hash"] if row else "0" * 64
            ts = _now()
            h = self._hash(prev, ts, sr_id, actor, event, d)
            cur = self._db.execute(
                "INSERT INTO audit_log(ts, sr_id, actor, event, detail, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
                (ts, sr_id, actor, event, d, prev, h))
            self._db.commit()
            return cur.lastrowid

    def audit_rows(self, sr_id: int | None = None) -> list[dict]:
        with self._lock:
            if sr_id is None:
                rows = self._db.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
            else:
                rows = self._db.execute("SELECT * FROM audit_log WHERE sr_id=? ORDER BY id", (sr_id,)).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]

    def verify_audit(self) -> dict:
        """체인 전체를 다시 계산한다. 깨진 첫 행 id 를 알려 준다."""
        prev = "0" * 64
        with self._lock:
            rows = self._db.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        for r in rows:
            if r["prev_hash"] != prev or r["hash"] != self._hash(prev, r["ts"], r["sr_id"], r["actor"], r["event"], r["detail"]):
                return {"ok": False, "broken_at": r["id"], "count": len(rows)}
            prev = r["hash"]
        return {"ok": True, "count": len(rows)}

    # ---- SR ----
    def create_sr(self, title: str, body: str, requester: str, project: str = "") -> int:
        title, body, requester, project = title.strip(), body.strip(), requester.strip(), project.strip()
        if not title or not body or not requester:
            raise ValueError("제목, 요청 내용, 요청자는 필수입니다.")
        if len(title) > 200 or len(project) > 200 or len(requester) > 100:
            raise ValueError("제목/프로젝트/요청자 길이가 너무 깁니다.")
        if len(body) > 20000:
            raise ValueError("요청 내용은 20,000자 이하로 입력하세요.")
        now = _now()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO sr(title, body, requester, project, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (title, body, requester, project, "received", now, now))
            self._db.commit()
            sr_id = cur.lastrowid
        self.audit(sr_id, requester, "sr.created", {"title": title, "project": project})
        return sr_id

    def get_sr(self, sr_id: int) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM sr WHERE id=?", (sr_id,)).fetchone()
        return dict(r) if r else None

    def list_sr(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM sr ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def update_sr(self, sr_id: int, **fields) -> None:
        allowed = {"status", "sr_type", "confidence", "reason", "project"}
        if set(fields) - allowed:
            raise ValueError(f"수정할 수 없는 필드: {set(fields) - allowed}")
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError(f"알 수 없는 상태: {fields['status']}")
        if "sr_type" in fields and fields["sr_type"] not in TYPES | {""}:
            raise ValueError(f"알 수 없는 유형: {fields['sr_type']}")
        sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
        with self._lock:
            self._db.execute(f"UPDATE sr SET {sets} WHERE id=?", (*fields.values(), _now(), sr_id))
            self._db.commit()

    # ---- 프로젝트 ----
    def upsert_project(self, name: str, local_path: str, gitlab_url: str = "", default_branch: str = "main",
                       schema_doc: str = "", token_env: str = "", doc_paths: list[str] | None = None,
                       db_env_prefix: str = "") -> dict:
        name, local_path = name.strip(), local_path.strip().strip('"')
        if not name or not local_path:
            raise ValueError("프로젝트 이름과 로컬 소스 경로는 필수입니다.")
        if len(name) > 100 or not re.fullmatch(r"[\w\-./ 가-힣]+", name):
            raise ValueError("프로젝트 이름에는 글자, 숫자, - _ . / 공백만 쓸 수 있습니다.")
        token_env = token_env.strip()
        if token_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
            raise ValueError("토큰 환경변수 이름 형식이 올바르지 않습니다. (토큰 값이 아니라 변수 이름을 넣으세요)")
        db_env_prefix = db_env_prefix.strip()
        if db_env_prefix and not re.fullmatch(r"[A-Za-z0-9_]{1,40}", db_env_prefix):
            raise ValueError("DB 환경변수 접두어는 영문/숫자/_ 만 쓸 수 있습니다. (SSOT_DB_<접두어>_USER 형태로 읽습니다)")
        gitlab_url = gitlab_url.strip().rstrip("/")
        if gitlab_url and not re.match(r"https?://", gitlab_url):
            raise ValueError("GitLab 주소는 http(s):// 로 시작해야 합니다.")
        docs = json.dumps([d.strip().strip('"') for d in (doc_paths or []) if d.strip()], ensure_ascii=False)
        with self._lock:
            self._db.execute(
                "INSERT INTO project(name, gitlab_url, default_branch, local_path, schema_doc, token_env, doc_paths, created_at, db_env_prefix)"
                " VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET gitlab_url=excluded.gitlab_url,"
                " default_branch=excluded.default_branch, local_path=excluded.local_path, schema_doc=excluded.schema_doc,"
                " token_env=excluded.token_env, doc_paths=excluded.doc_paths, db_env_prefix=excluded.db_env_prefix",
                (name, gitlab_url, default_branch.strip() or "main", local_path, schema_doc.strip().strip('"'),
                 token_env, docs, _now(), db_env_prefix))
            self._db.commit()
        return self.get_project(name)

    def get_project(self, name: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM project WHERE name=?", (name,)).fetchone()
        return self._proj(r) if r else None

    def list_projects(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM project ORDER BY name").fetchall()
        return [self._proj(r) for r in rows]

    def delete_project(self, name: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM project WHERE name=?", (name,))
            self._db.commit()

    @staticmethod
    def _proj(r) -> dict:
        d = dict(r)
        d["doc_paths"] = json.loads(d["doc_paths"])
        return d

    # ---- 영향 분석 ----
    def save_impact(self, sr_id: int, keywords: list[str], candidates: list[dict], vector_used: bool) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO sr_impact(sr_id, keywords, candidates, selected, vector_used, updated_at) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(sr_id) DO UPDATE SET keywords=excluded.keywords, candidates=excluded.candidates,"
                " selected=excluded.selected, vector_used=excluded.vector_used, updated_at=excluded.updated_at",
                (sr_id, json.dumps(keywords, ensure_ascii=False), json.dumps(candidates, ensure_ascii=False),
                 json.dumps([c["file"] for c in candidates if c.get("preselected")], ensure_ascii=False),
                 int(vector_used), _now()))
            self._db.commit()

    def get_impact(self, sr_id: int) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM sr_impact WHERE sr_id=?", (sr_id,)).fetchone()
        if not r:
            return None
        return {"keywords": json.loads(r["keywords"]), "candidates": json.loads(r["candidates"]),
                "selected": json.loads(r["selected"]), "vector_used": bool(r["vector_used"]), "updated_at": r["updated_at"]}

    def set_selected(self, sr_id: int, files: list[str]) -> None:
        with self._lock:
            self._db.execute("UPDATE sr_impact SET selected=?, updated_at=? WHERE sr_id=?",
                             (json.dumps(files, ensure_ascii=False), _now(), sr_id))
            self._db.commit()

    # ---- 임베딩 캐시 ----
    def cache_get(self, shas: list[str], model: str) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        with self._lock:
            for i in range(0, len(shas), 500):
                part = shas[i:i + 500]
                q = ",".join("?" * len(part))
                for r in self._db.execute(f"SELECT sha, vec FROM embed_cache WHERE model=? AND sha IN ({q})", (model, *part)):
                    out[r["sha"]] = r["vec"]
        return out

    def cache_put(self, items: dict[str, bytes], model: str) -> None:
        with self._lock:
            self._db.executemany("INSERT OR REPLACE INTO embed_cache(sha, model, vec) VALUES (?,?,?)",
                                 [(k, model, v) for k, v in items.items()])
            self._db.commit()

    # ---- 수정안(Diff) ----
    def create_patch(self, sr_id: int, feedback: str = "") -> int:
        with self._lock:
            v = (self._db.execute("SELECT COALESCE(MAX(version),0) FROM sr_patch WHERE sr_id=?", (sr_id,)).fetchone()[0]) + 1
            now = _now()
            cur = self._db.execute(
                "INSERT INTO sr_patch(sr_id, version, status, feedback, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (sr_id, v, "generating", feedback, now, now))
            self._db.commit()
            return cur.lastrowid

    _PATCH_FIELDS = {"status", "explanation", "diff", "stats", "warnings", "decided_by", "decision_note",
                     "branch", "commit_sha", "mr_url", "error"}

    def update_patch(self, patch_id: int, **fields) -> None:
        if set(fields) - self._PATCH_FIELDS:
            raise ValueError(f"수정할 수 없는 필드: {set(fields) - self._PATCH_FIELDS}")
        for k in ("stats", "warnings"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
        with self._lock:
            self._db.execute(f"UPDATE sr_patch SET {sets} WHERE id=?", (*fields.values(), _now(), patch_id))
            self._db.commit()

    @staticmethod
    def _patch(r) -> dict:
        d = dict(r)
        d["stats"], d["warnings"] = json.loads(d["stats"]), json.loads(d["warnings"])
        return d

    def get_patch(self, patch_id: int) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM sr_patch WHERE id=?", (patch_id,)).fetchone()
        return self._patch(r) if r else None

    def list_patches(self, sr_id: int) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM sr_patch WHERE sr_id=? ORDER BY id", (sr_id,)).fetchall()
        return [self._patch(r) for r in rows]

    # ---- 문서 갱신 제안 ----
    def create_doc_patch(self, project: str, commit_sha: str, doc_path: str, doc_sha: str, changes: list[dict],
                         sr_ids: list[int], summary: str) -> int | None:
        now = _now()
        with self._lock:
            try:
                cur = self._db.execute(
                    "INSERT INTO doc_patch(project, commit_sha, doc_path, doc_sha, status, changes, sr_ids, summary, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (project, commit_sha, doc_path, doc_sha, "pending", json.dumps(changes, ensure_ascii=False),
                     json.dumps(sr_ids), summary, now, now))
            except sqlite3.IntegrityError:    # 같은 커밋·문서는 한 번만 (웹훅 재전송 대비)
                return None
            self._db.commit()
            return cur.lastrowid

    @staticmethod
    def _dp(r) -> dict:
        d = dict(r)
        d["changes"], d["sr_ids"] = json.loads(d["changes"]), json.loads(d["sr_ids"])
        return d

    def get_doc_patch(self, pid: int) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM doc_patch WHERE id=?", (pid,)).fetchone()
        return self._dp(r) if r else None

    def list_doc_patches(self, status: str | None = None) -> list[dict]:
        with self._lock:
            rows = (self._db.execute("SELECT * FROM doc_patch WHERE status=? ORDER BY id DESC LIMIT 200", (status,))
                    if status else self._db.execute("SELECT * FROM doc_patch ORDER BY id DESC LIMIT 200")).fetchall()
        return [self._dp(r) for r in rows]

    def update_doc_patch(self, pid: int, **fields) -> None:
        allowed = {"status", "output", "decided_by", "changes"}
        if set(fields) - allowed:
            raise ValueError(f"수정할 수 없는 필드: {set(fields) - allowed}")
        if "changes" in fields and not isinstance(fields["changes"], str):
            fields["changes"] = json.dumps(fields["changes"], ensure_ascii=False)
        sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
        with self._lock:
            self._db.execute(f"UPDATE doc_patch SET {sets} WHERE id=?", (*fields.values(), _now(), pid))
            self._db.commit()

    # ---- 데이터 수정 분석 ----
    def save_data(self, sr_id: int, schema_source: str, tables: list, related: list, statements: list, notes: list,
                  diffs: list) -> None:
        j = lambda x: json.dumps(x, ensure_ascii=False)  # noqa: E731
        with self._lock:
            self._db.execute(
                "INSERT INTO sr_data(sr_id, schema_source, tables, related, statements, notes, diffs, updated_at) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(sr_id) DO UPDATE SET schema_source=excluded.schema_source, tables=excluded.tables,"
                " related=excluded.related, statements=excluded.statements, notes=excluded.notes, diffs=excluded.diffs,"
                " updated_at=excluded.updated_at",
                (sr_id, schema_source, j(tables), j(related), j(statements), j(notes), j(diffs), _now()))
            self._db.commit()

    def get_data(self, sr_id: int) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM sr_data WHERE sr_id=?", (sr_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        for k in ("tables", "related", "statements", "notes", "diffs"):
            d[k] = json.loads(d[k])
        return d

    # ---- 소스 색인(미리 분석) ----
    def code_files(self, project: str) -> dict[str, tuple[int, int, str]]:
        with self._lock:
            rows = self._db.execute("SELECT path, size, mtime_ns, sha FROM code_file WHERE project=?", (project,)).fetchall()
        return {r["path"]: (r["size"], r["mtime_ns"], r["sha"]) for r in rows}

    def code_replace_file(self, project: str, path: str, size: int, mtime_ns: int, sha: str, chunks: list[tuple]) -> None:
        """chunks: (cls, method, line, text, snippet)"""
        with self._lock:
            self._db.execute("DELETE FROM code_chunk WHERE project=? AND path=?", (project, path))
            self._db.executemany(
                "INSERT INTO code_chunk(project, path, cls, method, line, text, snippet) VALUES (?,?,?,?,?,?,?)",
                [(project, path, *c) for c in chunks])
            self._db.execute("INSERT OR REPLACE INTO code_file(project, path, size, mtime_ns, sha) VALUES (?,?,?,?,?)",
                             (project, path, size, mtime_ns, sha))
            self._db.commit()

    def code_touch_file(self, project: str, path: str, size: int, mtime_ns: int) -> None:
        with self._lock:
            self._db.execute("UPDATE code_file SET size=?, mtime_ns=? WHERE project=? AND path=?", (size, mtime_ns, project, path))
            self._db.commit()

    def code_delete_files(self, project: str, paths: list[str]) -> None:
        with self._lock:
            for p in paths:
                for t in ("code_file", "code_chunk", "code_summary"):
                    self._db.execute(f"DELETE FROM {t} WHERE project=? AND path=?", (project, p))
            self._db.commit()

    def code_chunks(self, project: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute("SELECT path, cls, method, line, text, snippet FROM code_chunk WHERE project=? ORDER BY id",
                                    (project,)).fetchall()

    def summaries(self, project: str) -> dict[tuple[str, str], tuple[str, str]]:
        with self._lock:
            rows = self._db.execute("SELECT path, cls, sha, summary FROM code_summary WHERE project=?", (project,)).fetchall()
        return {(r["path"], r["cls"]): (r["sha"], r["summary"]) for r in rows}

    def put_summary(self, project: str, path: str, cls: str, sha: str, summary: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO code_summary(project, path, cls, sha, summary) VALUES (?,?,?,?,?)",
                             (project, path, cls, sha, summary))
            self._db.commit()

    def index_meta(self, project: str) -> dict:
        with self._lock:
            r = self._db.execute("SELECT * FROM code_index WHERE project=?", (project,)).fetchone()
        return dict(r) if r else {"project": project, "version": 0, "built_at": "", "files": 0, "chunks": 0,
                                  "summaries": 0, "embedded": 0, "last_error": ""}

    def set_index_meta(self, project: str, bump: bool = False, **fields) -> None:
        cur = self.index_meta(project)
        cur.update(fields)
        if bump:
            cur["version"] += 1
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO code_index(project, version, built_at, files, chunks, summaries, embedded, last_error)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (project, cur["version"], cur["built_at"], cur["files"], cur["chunks"], cur["summaries"], cur["embedded"], cur["last_error"]))
            self._db.commit()
