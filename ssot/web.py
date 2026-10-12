"""웹 UI 서버 (표준 라이브러리 http.server). 로컬 PC 에서 실행하는 도구이므로 127.0.0.1 에만 바인딩한다.

흐름: /api/preview (소스+문서 업로드, 변경 목록 생성) -> 화면에서 항목 선택 -> /api/apply (선택 적용) -> /download/...
"""
from __future__ import annotations

import base64
import json
import re
import secrets
import shutil
import tempfile
import threading
import urllib.request
import zipfile
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from .docs import open_document
from .facts import collect_facts
from .llm import make_llm
from .sync import Change, apply_ops, sync_narrative, sync_tables
from .tablemap import load_aliases

STATIC = Path(__file__).parent / "web_static"
MAX_BODY = 300 * 1024 * 1024
MAX_SESSIONS = 20
DOC_EXT = {".docx", ".md", ".markdown", ".xlsx"}
ACTION_ORDER = ["update", "fill", "add", "narrative", "review", "missing"]


class Session:
    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.docs: dict[str, Path] = {}                 # 원본 파일명 -> 저장 경로
        self.changes: dict[str, list[Change]] = {}      # 원본 파일명 -> 변경 목록
        self.outputs: dict[str, Path] = {}              # 원본 파일명 -> 적용 결과 파일


SESSIONS: dict[str, Session] = {}
LOCK = threading.Lock()


def _safe_name(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    return re.sub(r'[<>:"|?*\x00-\x1f]', "_", name) or "document"


def _extract_zip(data: bytes, dest: Path) -> Path:
    z_path = dest / "src.zip"
    z_path.write_bytes(data)
    root = (dest / "src").resolve()
    with zipfile.ZipFile(z_path) as z:
        for info in z.infolist():
            target = (root / info.filename).resolve()
            if root != target and root not in target.parents:  # zip-slip 방지
                raise ValueError(f"허용되지 않는 경로가 zip 에 있습니다: {info.filename}")
        z.extractall(root)
    return root


def _new_session() -> tuple[str, Session]:
    sid = secrets.token_urlsafe(12)
    s = Session(Path(tempfile.mkdtemp(prefix="ssot_")))
    with LOCK:
        SESSIONS[sid] = s
        while len(SESSIONS) > MAX_SESSIONS:
            old = SESSIONS.pop(next(iter(SESSIONS)))
            shutil.rmtree(old.workdir, ignore_errors=True)
    return sid, s


def ollama_status(base_url: str) -> dict:
    root = re.sub(r"/v1/?$", "", base_url.rstrip("/"))
    try:
        with urllib.request.urlopen(f"{root}/api/tags", timeout=2) as r:
            models = [m["name"] for m in json.loads(r.read().decode()).get("models", [])]
        return {"ok": True, "models": models}
    except Exception as e:  # noqa: BLE001 - 상태 조회용이라 이유만 전달
        return {"ok": False, "models": [], "error": str(e)}


def preview(payload: dict) -> dict:
    if payload.get("src_zip"):
        sid, sess = _new_session()
        src = _extract_zip(base64.b64decode(payload["src_zip"]), sess.workdir)
    else:
        src = Path(str(payload.get("src_path", "")).strip().strip('"'))
        if not src.is_dir():
            raise ValueError(f"소스 폴더를 찾을 수 없습니다: {src}")
        sid, sess = _new_session()
    files = payload.get("docs") or []
    if not files:
        raise ValueError("문서를 하나 이상 올려 주세요.")

    facts = collect_facts(src)
    cfg = src / "ssot.toml"
    aliases = load_aliases(cfg if cfg.exists() else None)
    llm = None
    if payload.get("llm"):
        llm = make_llm(payload.get("provider") or "ollama", payload.get("model") or None, payload.get("base_url") or None)

    result_docs = []
    for f in files:
        name = _safe_name(f["name"])
        if Path(name).suffix.lower() not in DOC_EXT:
            raise ValueError(f"지원하지 않는 문서 형식입니다: {name}")
        path = sess.workdir / name
        path.write_bytes(base64.b64decode(f["data"]))
        sess.docs[name] = path
        doc = open_document(path)
        changes = sync_tables(doc, facts, aliases, llm, strict=bool(payload.get("strict")))
        if llm:
            changes += sync_narrative(doc, facts, llm)
        changes.sort(key=lambda c: ACTION_ORDER.index(c.action))
        sess.changes[name] = changes
        result_docs.append({
            "name": name,
            "changes": [{"id": i, **{k: v for k, v in asdict(c).items() if k != "op"}, "applicable": c.op is not None}
                        for i, c in enumerate(changes)],
        })
    kinds: dict[str, int] = {}
    for f in facts:
        kinds[f.kind] = kinds.get(f.kind, 0) + 1
    return {"session": sid, "facts": kinds, "docs": result_docs}


def apply(payload: dict) -> dict:
    sess = SESSIONS.get(payload.get("session", ""))
    if not sess:
        raise ValueError("세션이 만료되었습니다. 미리보기를 다시 실행하세요.")
    out = []
    for name, ids in (payload.get("accepted") or {}).items():
        if name not in sess.docs:
            continue
        ops = [sess.changes[name][i].op for i in ids
               if 0 <= i < len(sess.changes[name]) and sess.changes[name][i].op]
        if not ops:
            continue
        doc = open_document(sess.docs[name])
        apply_ops(doc, ops)
        p = Path(name)
        target = sess.workdir / f"{p.stem}.updated{p.suffix}"
        doc.save(target)
        sess.outputs[name] = target
        out.append({"name": name, "applied": len(ops), "file": target.name,
                    "url": f"/download/{payload['session']}/{quote(target.name)}"})
    return {"files": out}


class Handler(BaseHTTPRequestHandler):
    server_version = "ssot-web"

    def log_message(self, *a):  # 콘솔 소음 줄이기
        pass

    def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/status":
            base = "http://localhost:11434/v1"
            return self._json(200, {"ollama": ollama_status(base), "base_url": base})
        m = re.match(r"^/download/([\w\-]+)/(.+)$", url.path)
        if m:
            sess = SESSIONS.get(m.group(1))
            fname = unquote(m.group(2))
            f = next((p for p in (sess.outputs.values() if sess else []) if p.name == fname), None)
            if not f:
                return self._json(404, {"error": "파일이 없습니다."})
            return self._send(200, f.read_bytes(), "application/octet-stream",
                              {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(f.name)}"})
        self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        route = {"/api/preview": preview, "/api/apply": apply}.get(urlparse(self.path).path)
        if not route:
            return self._json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > MAX_BODY:
                return self._json(413, {"error": "업로드 크기 제한(300MB)을 넘었습니다."})
            self._json(200, route(json.loads(self.rfile.read(n).decode("utf-8"))))
        except (ValueError, KeyError, zipfile.BadZipFile, RuntimeError, OSError) as e:
            self._json(400, {"error": str(e) or type(e).__name__})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": f"{type(e).__name__}: {e}"})


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"SSSOT 웹 UI: http://{host}:{port}  (종료: Ctrl+C)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for s in SESSIONS.values():
            shutil.rmtree(s.workdir, ignore_errors=True)
