"""SR 접수 웹 서버 (표준 라이브러리). 기본 127.0.0.1 바인딩.

POST /api/sr -> 접수 즉시 id 반환, 분류는 백그라운드 스레드에서 수행(화면은 폴링).
"""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import time
import io
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import os
import subprocess

from .classify import process_sr, set_manual
from .dataflow import analyze_data_sr, report_executed
from .docsync import apply_doc_patch, dismiss_doc_patch, handle_push, scan_commit, verify_secret
from . import gitops
from .oracle_meta import configured as oracle_configured
from .gitlab import PublishError, publish
from .impact import analyze_sr, confirm_selection
from .patch import decide, generate_patch
from .llm import LLM
from .store import Store

STATIC = Path(__file__).parent / "web_static"
MAX_BODY = 2 * 1024 * 1024


class App:
    def __init__(self, store: Store, llm: LLM, sync_mode: bool = False, allow_self_approve: bool = False):
        self.store, self.llm, self.sync_mode = store, llm, sync_mode
        self.allow_self_approve = allow_self_approve
        self._classify_lock = threading.Lock()   # 로컬 GPU 하나이므로 분류 호출은 한 번에 하나씩

    def run_classify(self, sr_id: int) -> None:
        with self._classify_lock:
            try:
                if process_sr(self.store, self.llm, sr_id) == "classified":
                    self.after_classified(sr_id)
            except Exception as e:  # noqa: BLE001 - 백그라운드 스레드 보호
                self.store.audit(sr_id, "system", "classify.crashed", {"error": f"{type(e).__name__}: {e}"})
                self.store.update_sr(sr_id, status="pending_llm", reason=f"처리 오류: {e}"[:300])

    def after_classified(self, sr_id: int) -> None:
        """소스 수정 건이고 등록된 프로젝트가 있으면 이어서 영향 분석을 수행한다."""
        sr = self.store.get_sr(sr_id)
        if not (sr and sr["project"] and self.store.get_project(sr["project"])):
            return
        if sr["sr_type"] == "code":
            analyze_sr(self.store, self.llm, sr_id)
        elif sr["sr_type"] == "data":
            analyze_data_sr(self.store, self.llm, sr_id)

    def run_analyze(self, sr_id: int) -> None:
        with self._classify_lock:
            try:
                analyze_sr(self.store, self.llm, sr_id)
            except Exception as e:  # noqa: BLE001
                self.store.audit(sr_id, "system", "impact.crashed", {"error": f"{type(e).__name__}: {e}"})
                self.store.update_sr(sr_id, status="impact_error", reason=f"처리 오류: {e}"[:300])

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
        return {"sr": sr, "audit": self.store.audit_rows(sr_id), "impact": self.store.get_impact(sr_id),
                "patches": self.store.list_patches(sr_id), "data": self.store.get_data(sr_id), "self_approve": self.allow_self_approve}

    # ---- 프로젝트 / 영향 분석 ----
    def projects(self) -> dict:
        return {"items": self.store.list_projects()}

    def save_project(self, p: dict) -> dict:
        docs = p.get("doc_paths", [])
        if isinstance(docs, str):
            docs = [x for x in docs.splitlines()]
        proj = self.store.upsert_project(str(p.get("name", "")), str(p.get("local_path", "")), str(p.get("gitlab_url", "")),
                                         str(p.get("default_branch", "main")), str(p.get("schema_doc", "")),
                                         str(p.get("token_env", "")), docs, str(p.get("db_env_prefix", "")))
        self.store.audit(None, str(p.get("actor", "")).strip() or "unknown", "project.saved",
                         {k: v for k, v in proj.items() if k != "created_at"})
        return {"project": proj, "check": self.check_project(proj["name"])}

    def delete_project(self, p: dict) -> dict:
        self.store.delete_project(str(p.get("name", "")))
        self.store.audit(None, str(p.get("actor", "")).strip() or "unknown", "project.deleted", {"name": p.get("name")})
        return {"ok": True}

    def check_project(self, name: str) -> dict:
        proj = self.store.get_project(name)
        if not proj:
            raise LookupError("프로젝트를 찾을 수 없습니다.")
        root = Path(proj["local_path"])
        res = {"path_exists": root.is_dir(), "java_files": 0, "git_repo": False, "branch_exists": False,
               "token_set": bool(proj["token_env"] and os.environ.get(proj["token_env"])),
               "schema_doc_exists": bool(proj["schema_doc"]) and Path(proj["schema_doc"]).is_file(),
               "db_configured": bool(proj["db_env_prefix"]) and oracle_configured(proj["db_env_prefix"])}
        if res["path_exists"]:
            res["java_files"] = sum(1 for _ in root.rglob("*.java"))
            def git(*a):
                return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, timeout=20)
            try:
                res["git_repo"] = git("rev-parse", "--is-inside-work-tree").returncode == 0
                if res["git_repo"]:
                    res["branch_exists"] = git("rev-parse", "--verify", proj["default_branch"]).returncode == 0
            except (OSError, subprocess.SubprocessError):
                pass
        return res

    def analyze(self, sr_id: int, p: dict) -> dict:
        sr = self.store.get_sr(sr_id)
        if not sr:
            raise LookupError("SR 을 찾을 수 없습니다.")
        if sr["status"] in ("classifying", "analyzing", "received"):
            raise ValueError("처리 중입니다. 잠시 후 다시 시도하세요.")
        self.store.audit(sr_id, str(p.get("actor", "")).strip() or "unknown", "impact.requested", {})
        if self.sync_mode:
            self.run_analyze(sr_id)
        else:
            threading.Thread(target=self.run_analyze, args=(sr_id,), daemon=True).start()
        return {"ok": True}

    def set_project(self, sr_id: int, p: dict) -> dict:
        actor, name = str(p.get("actor", "")).strip(), str(p.get("project", "")).strip()
        if not actor:
            raise ValueError("담당자 이름이 필요합니다.")
        sr = self.store.get_sr(sr_id)
        if not sr:
            raise LookupError("SR 을 찾을 수 없습니다.")
        if name and not self.store.get_project(name):
            raise ValueError(f"등록되지 않은 프로젝트입니다: {name}")
        self.store.update_sr(sr_id, project=name)
        self.store.audit(sr_id, actor, "sr.project_changed", {"from": sr["project"], "to": name})
        return self.detail(sr_id)

    def confirm_impact(self, sr_id: int, p: dict) -> dict:
        confirm_selection(self.store, sr_id, str(p.get("actor", "")), [str(f) for f in p.get("files", [])])
        self.start_patch(sr_id)
        return self.detail(sr_id)

    # ---- 데이터 수정 (M5) ----
    def run_data(self, sr_id: int) -> None:
        with self._classify_lock:
            try:
                analyze_data_sr(self.store, self.llm, sr_id)
            except Exception as e:  # noqa: BLE001
                self.store.audit(sr_id, "system", "data.crashed", {"error": f"{type(e).__name__}: {e}"})
                self.store.update_sr(sr_id, status="data_error", reason=f"처리 오류: {e}"[:300])

    def data_analyze(self, sr_id: int, p: dict) -> dict:
        sr = self.store.get_sr(sr_id)
        if not sr:
            raise LookupError("SR 을 찾을 수 없습니다.")
        if sr["sr_type"] != "data" or sr["status"] in ("classifying", "received", "data_analyzing"):
            raise ValueError("데이터 수정 건으로 분류가 끝난 SR 만 분석할 수 있습니다.")
        self.store.audit(sr_id, str(p.get("actor", "")).strip() or "unknown", "data.requested", {})
        if self.sync_mode:
            self.run_data(sr_id)
        else:
            threading.Thread(target=self.run_data, args=(sr_id,), daemon=True).start()
        return {"ok": True}

    def data_report(self, sr_id: int, p: dict) -> dict:
        report_executed(self.store, sr_id, str(p.get("actor", "")), str(p.get("affected", "")), str(p.get("note", "")))
        return self.detail(sr_id)

    # ---- 문서 갱신 (M4) ----
    def run_hook(self, payload: dict) -> None:
        with self._classify_lock:
            try:
                res = handle_push(self.store, self.llm, payload)
                self.store.audit(None, "gitlab", "hook.processed", {"result": res})
            except Exception as e:  # noqa: BLE001
                self.store.audit(None, "system", "hook.failed", {"error": f"{type(e).__name__}: {e}"})

    def hook_gitlab(self, payload: dict) -> dict:
        if self.sync_mode:
            self.run_hook(payload)
        else:
            threading.Thread(target=self.run_hook, args=(payload,), daemon=True).start()
        return {"accepted": True}

    def docs_list(self) -> dict:
        items = self.store.list_doc_patches()
        for it in items:   # 목록에는 편집 명령(op)을 싣지 않는다
            it["changes"] = [{k: v for k, v in c.items() if k != "op"} | {"applicable": bool(c.get("op"))} for c in it["changes"]]
        return {"items": items, "webhook_ready": bool(os.environ.get("SSOT_WEBHOOK_SECRET"))}

    def docs_scan(self, p: dict) -> dict:
        proj = self.store.get_project(str(p.get("project", "")))
        if not proj:
            raise ValueError("등록되지 않은 프로젝트입니다.")
        root = Path(proj["local_path"])
        sha = str(p.get("sha", "")).strip() or gitops.run(root, "rev-parse", proj["default_branch"]).stdout.decode().strip()
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
            raise ValueError("커밋 해시 형식이 올바르지 않습니다.")
        sha = gitops.run(root, "rev-parse", "--verify", f"{sha}^{{commit}}").stdout.decode().strip()
        msg = gitops.run(root, "log", "-1", "--format=%B", sha).stdout.decode("utf-8", "replace")
        self.store.audit(None, str(p.get("actor", "")).strip() or "unknown", "doc.scan_requested",
                         {"project": proj["name"], "commit": sha})
        with self._classify_lock:
            try:
                return scan_commit(self.store, self.llm, proj, sha, [msg], use_llm=bool(p.get("use_llm", True)))
            except gitops.GitError as e:
                raise ValueError(str(e)) from e

    def docs_apply(self, pid: int, p: dict) -> dict:
        out = apply_doc_patch(self.store, pid, str(p.get("actor", "")), [int(i) for i in p.get("accepted", [])])
        return {"output": str(out)}

    def docs_dismiss(self, pid: int, p: dict) -> dict:
        dismiss_doc_patch(self.store, pid, str(p.get("actor", "")), str(p.get("reason", "")))
        return {"ok": True}

    # ---- 수정안 ----
    def run_patch(self, sr_id: int, feedback: str = "") -> None:
        with self._classify_lock:
            try:
                generate_patch(self.store, self.llm, sr_id, feedback)
            except Exception as e:  # noqa: BLE001
                self.store.audit(sr_id, "system", "patch.crashed", {"error": f"{type(e).__name__}: {e}"})
                self.store.update_sr(sr_id, status="patch_failed", reason=f"처리 오류: {e}"[:300])

    def start_patch(self, sr_id: int, feedback: str = "") -> None:
        self.store.update_sr(sr_id, status="patch_generating")
        if self.sync_mode:
            self.run_patch(sr_id, feedback)
        else:
            threading.Thread(target=self.run_patch, args=(sr_id, feedback), daemon=True).start()

    def patch_generate(self, sr_id: int, p: dict) -> dict:
        sr = self.store.get_sr(sr_id)
        if not sr or sr["status"] not in ("impact_confirmed", "patch_failed"):
            raise ValueError("대상 파일이 확정된 SR 만 수정안을 만들 수 있습니다.")
        self.store.audit(sr_id, str(p.get("actor", "")).strip() or "unknown", "patch.requested",
                         {"feedback": str(p.get("feedback", ""))})
        self.start_patch(sr_id, str(p.get("feedback", "")).strip())
        return {"ok": True}

    def patch_decide(self, sr_id: int, p: dict) -> dict:
        result = decide(self.store, sr_id, int(p.get("patch_id", 0)), str(p.get("actor", "")),
                        str(p.get("decision", "")), str(p.get("note", "")), self.allow_self_approve)
        if result == "revise":
            self.start_patch(sr_id, str(p.get("note", "")).strip())
        elif result == "approved":
            try:
                publish(self.store, sr_id, str(p.get("actor", "")).strip())
            except PublishError as e:   # 로컬 커밋은 성공. 토큰/네트워크 문제는 나중에 재시도
                self.store.update_sr(sr_id, reason=f"브랜치 커밋됨, GitLab 반영 대기: {e}"[:300])
        return self.detail(sr_id)

    def patch_publish(self, sr_id: int, p: dict) -> dict:
        try:
            publish(self.store, sr_id, str(p.get("actor", "")).strip())
        except PublishError as e:
            raise ValueError(str(e)) from e
        return self.detail(sr_id)

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


def load_users() -> dict[str, str]:
    """SSOT_USERS="kim:비밀번호,lee:sha256:<hex>" 형식. 설정하면 모든 화면/API 가 로그인(Basic 인증)을 요구한다."""
    out: dict[str, str] = {}
    for item in os.environ.get("SSOT_USERS", "").split(","):
        if ":" in item:
            u, _, pw = item.strip().partition(":")
            if u and pw:
                out[u] = pw
    return out


def _pw_ok(stored: str, given: str) -> bool:
    if stored.startswith("sha256:"):
        return hmac.compare_digest(stored[7:].lower(), hashlib.sha256(given.encode("utf-8")).hexdigest())
    return hmac.compare_digest(stored.encode("utf-8"), given.encode("utf-8"))


class Throttle:
    """로그인 실패가 반복되는 클라이언트를 잠시 막는다 (무차별 대입 완화)."""

    def __init__(self, limit: int = 5, window: int = 300):
        self.limit, self.window, self.fails = limit, window, {}

    def blocked(self, key: str) -> bool:
        now = time.time()
        self.fails[key] = [t for t in self.fails.get(key, []) if now - t < self.window]
        return len(self.fails[key]) >= self.limit

    def fail(self, key: str) -> None:
        self.fails.setdefault(key, []).append(time.time())


def make_handler(app: App, users: dict[str, str] | None = None):
    users = users or {}
    throttle = Throttle()
    class Handler(BaseHTTPRequestHandler):
        server_version = "ssot-sr"

        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
            except (ConnectionError, TimeoutError):
                pass   # 브라우저가 새로고침/이동으로 연결을 먼저 끊은 경우: 무시

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

        def _auth(self) -> str | None:
            """인증이 켜져 있으면 사용자 이름, 꺼져 있으면 ''. 실패하면 None(응답까지 보냄)."""
            if not users:
                return ""
            ip = self.client_address[0]
            if throttle.blocked(ip):
                self._json(429, {"error": "로그인 실패가 반복되어 잠시 차단되었습니다. 5분 뒤 다시 시도하세요."})
                return None
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    u, _, pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")
                except Exception:  # noqa: BLE001
                    u, pw = "", ""
                if u in users and _pw_ok(users[u], pw):
                    return u
                throttle.fail(ip)
            self._send(401, b'{"error":"login required"}', "application/json; charset=utf-8",
                       {"WWW-Authenticate": 'Basic realm="SSSOT", charset="UTF-8"'})
            return None

        def _same_origin(self) -> bool:
            """브라우저의 교차 출처 POST(CSRF) 차단: Origin 이 있으면 Host 와 같아야 한다."""
            origin = self.headers.get("Origin")
            return not origin or urlparse(origin).netloc == self.headers.get("Host")

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            user = self._auth()
            if user is None:
                return
            if path == "/api/whoami":
                return self._json(200, {"user": user})
            if path in ("/", "/index.html"):
                return self._send(200, (STATIC / "sr.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/status":
                return self._guard(app.status)
            if path == "/api/sr":
                return self._guard(lambda: {"items": app.store.list_sr()})
            if path == "/api/projects":
                return self._guard(app.projects)
            if path == "/api/docs":
                return self._guard(app.docs_list)
            m = re.fullmatch(r"/api/projects/check", path)
            if m:
                name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
                return self._guard(lambda: app.check_project(name))
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
            user = ""
            if path != "/api/hooks/gitlab":     # 웹훅은 자체 비밀 토큰으로 인증
                user = self._auth()
                if user is None:
                    return

            def body() -> dict:
                d = json.loads(raw or "{}")
                if not isinstance(d, dict):
                    raise ValueError("JSON 객체가 필요합니다.")
                if user:     # 로그인한 사용자가 있으면 입력한 이름이 아니라 인증된 이름을 쓴다(사칭 방지)
                    d["actor"] = user
                    if path == "/api/sr":
                        d["requester"] = user
                return d

            if path == "/api/sr":
                return self._guard(lambda: app.create(body()))
            if path == "/api/hooks/gitlab":
                if not os.environ.get("SSOT_WEBHOOK_SECRET"):
                    return self._json(503, {"error": "SSOT_WEBHOOK_SECRET 환경변수가 설정되지 않아 웹훅을 받지 않습니다."})
                if not verify_secret(self.headers.get("X-Gitlab-Token")):
                    return self._json(401, {"error": "토큰이 올바르지 않습니다."})
                return self._guard(lambda: app.hook_gitlab(body()))
            if path == "/api/docs/scan":
                return self._guard(lambda: app.docs_scan(body()))
            m = re.fullmatch(r"/api/docs/(\d+)/(apply|dismiss)", path)
            if m:
                fn = app.docs_apply if m.group(2) == "apply" else app.docs_dismiss
                return self._guard(lambda: fn(int(m.group(1)), body()))
            if path == "/api/projects":
                return self._guard(lambda: app.save_project(body()))
            if path == "/api/projects/delete":
                return self._guard(lambda: app.delete_project(body()))
            m = re.fullmatch(r"/api/sr/(\d+)/(classify|retry|analyze|impact|project|data/analyze|data/report|patch/generate|patch/decide|patch/publish)", path)
            if m:
                fn = {"classify": app.classify_manual, "retry": app.retry, "analyze": app.analyze,
                      "impact": app.confirm_impact, "project": app.set_project,
                      "data/analyze": app.data_analyze, "data/report": app.data_report,
                      "patch/generate": app.patch_generate, "patch/decide": app.patch_decide,
                      "patch/publish": app.patch_publish}[m.group(2)]
                return self._guard(lambda: fn(int(m.group(1)), body()))
            self._json(404, {"error": "not found"})

    return Handler


def serve(db: str, host: str = "127.0.0.1", port: int = 8766, ollama_host: str | None = None,
          model: str | None = None, mask: bool = True, allow_self_approve: bool = False,
          insecure_no_auth: bool = False) -> None:
    from .ollama import OllamaLLM
    users = load_users()
    if host not in ("127.0.0.1", "localhost", "::1") and not users and not insecure_no_auth:
        raise SystemExit("외부에서 접속 가능한 주소(--host)로 열려면 로그인 설정이 필요합니다. 서버 실행 전에 환경변수 "
                         "SSOT_USERS=\"kim:비밀번호,lee:비밀번호\" 를 설정하세요. (사내망 테스트용: --insecure-no-auth)")
    app = App(Store(db, mask=mask), OllamaLLM(host=ollama_host, model=model), allow_self_approve=allow_self_approve)
    srv = ThreadingHTTPServer((host, port), make_handler(app, users))
    print(f"SR 접수 서버: http://{host}:{port}  (DB: {db}, 모델: {app.llm.model}, 종료: Ctrl+C)")
    print(f"로그인: {'사용자 ' + str(len(users)) + '명 (Basic 인증)' if users else '없음 (이 PC 에서만 접속)'}")
    if host not in ("127.0.0.1", "localhost", "::1") and not users:
        print("경고: 인증 없이 외부에 열려 있습니다. 사내망 안에서만 사용하세요.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
