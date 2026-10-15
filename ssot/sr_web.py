"""SR 접수 웹 서버 (표준 라이브러리). 기본 127.0.0.1 바인딩.

POST /api/sr -> 접수 즉시 id 반환, 분류는 백그라운드 스레드에서 수행(화면은 폴링).
"""
from __future__ import annotations

import csv
import io
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .classify import process_sr, set_manual
from .llm import LLM
from .store import Store

STATIC = Path(__file__).parent / "web_static"
MAX_BODY = 2 * 1024 * 1024


class App:
    def __init__(self, store: Store, llm: LLM, sync_mode: bool = False):
        self.store, self.llm, self.sync_mode = store, llm, sync_mode
        self._classify_lock = threading.Lock()   # 로컬 GPU 하나이므로 분류 호출은 한 번에 하나씩

    def run_classify(self, sr_id: int) -> None:
        with self._classify_lock:
            try:
                process_sr(self.store, self.llm, sr_id)
            except Exception as e:  # noqa: BLE001 - 백그라운드 스레드 보호
                self.store.audit(sr_id, "system", "classify.crashed", {"error": f"{type(e).__name__}: {e}"})
                self.store.update_sr(sr_id, status="pending_llm", reason=f"처리 오류: {e}"[:300])

    def start_classify(self, sr_id: int) -> None:
        if self.sync_mode:
            self.run_classify(sr_id)
        else:
            threading.Thread(target=self.run_classify, args=(sr_id,), daemon=True).start()

    # ---- API ----
    def create(self, p: dict) -> dict:
        sr_id = self.store.create_sr(str(p.get("title", "")), str(p.get("body", "")),
                                     str(p.get("requester", "")), str(p.get("project", "")))
        self.start_classify(sr_id)
        return {"id": sr_id}

    def detail(self, sr_id: int) -> dict:
        sr = self.store.get_sr(sr_id)
        if not sr:
            raise LookupError("SR 을 찾을 수 없습니다.")
        return {"sr": sr, "audit": self.store.audit_rows(sr_id)}

    def classify_manual(self, sr_id: int, p: dict) -> dict:
        set_manual(self.store, sr_id, str(p.get("actor", "")), str(p.get("type", "")), str(p.get("note", "")))
        return self.detail(sr_id)

    def retry(self, sr_id: int, p: dict) -> dict:
        sr = self.store.get_sr(sr_id)
        if not sr:
            raise LookupError("SR 을 찾을 수 없습니다.")
        if sr["status"] not in ("pending_llm", "needs_review"):
            raise ValueError("재시도할 수 없는 상태입니다.")
        self.store.audit(sr_id, str(p.get("actor", "")).strip() or "unknown", "sr.retry", {})
        self.start_classify(sr_id)
        return {"ok": True}

    def status(self) -> dict:
        st = self.llm.status() if hasattr(self.llm, "status") else {"ok": True}
        return {"ollama": st, "audit": self.store.verify_audit()}

    def audit_csv(self, sr_id: int) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "ts", "sr_id", "actor", "event", "detail", "hash"])
        for r in self.store.audit_rows(sr_id):
            w.writerow([r["id"], r["ts"], r["sr_id"], r["actor"], r["event"],
                        json.dumps(r["detail"], ensure_ascii=False), r["hash"]])
        return "﻿" + buf.getvalue()   # Excel 에서 한글이 깨지지 않도록 BOM


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ssot-sr"

        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _guard(self, fn):
            try:
                self._json(200, fn())
            except LookupError as e:
                self._json(404, {"error": str(e)})
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                self._json(400, {"error": str(e) or type(e).__name__})
            except Exception as e:  # noqa: BLE001
                self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def _same_origin(self) -> bool:
            """브라우저의 교차 출처 POST(CSRF) 차단: Origin 이 있으면 Host 와 같아야 한다."""
            origin = self.headers.get("Origin")
            return not origin or urlparse(origin).netloc == self.headers.get("Host")

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                return self._send(200, (STATIC / "sr.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/status":
                return self._guard(app.status)
            if path == "/api/sr":
                return self._guard(lambda: {"items": app.store.list_sr()})
            m = re.fullmatch(r"/api/sr/(\d+)", path)
            if m:
                return self._guard(lambda: app.detail(int(m.group(1))))
            m = re.fullmatch(r"/api/sr/(\d+)/audit\.csv", path)
            if m:
                return self._send(200, app.audit_csv(int(m.group(1))).encode("utf-8"), "text/csv; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="SR-{m.group(1)}-audit.csv"'})
            self._json(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if not self._same_origin():
                return self._json(403, {"error": "허용되지 않은 출처입니다."})
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n > MAX_BODY:
                return self._json(413, {"error": "요청이 너무 큽니다."})
            raw = self.rfile.read(n).decode("utf-8") if n else "{}"
            path = urlparse(self.path).path

            def body() -> dict:
                d = json.loads(raw or "{}")
                if not isinstance(d, dict):
                    raise ValueError("JSON 객체가 필요합니다.")
                return d

            if path == "/api/sr":
                return self._guard(lambda: app.create(body()))
            m = re.fullmatch(r"/api/sr/(\d+)/(classify|retry)", path)
            if m:
                fn = app.classify_manual if m.group(2) == "classify" else app.retry
                return self._guard(lambda: fn(int(m.group(1)), body()))
            self._json(404, {"error": "not found"})

    return Handler


def serve(db: str, host: str = "127.0.0.1", port: int = 8766, ollama_host: str | None = None,
          model: str | None = None, mask: bool = True) -> None:
    from .ollama import OllamaLLM
    app = App(Store(db, mask=mask), OllamaLLM(host=ollama_host, model=model))
    srv = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"SR 접수 서버: http://{host}:{port}  (DB: {db}, 모델: {app.llm.model}, 종료: Ctrl+C)")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("경고: 인증이 없습니다. 사내망 안에서만 사용하세요.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
