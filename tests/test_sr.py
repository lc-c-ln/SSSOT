import json
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

from ssot.classify import parse_classification, process_sr, set_manual  # noqa: E402
from ssot.llm import LLM  # noqa: E402
from ssot.ollama import OllamaError, OllamaLLM  # noqa: E402
from ssot.sr_web import App, make_handler  # noqa: E402
from ssot.store import Store, mask_secrets  # noqa: E402


class FakeLLM(LLM):
    model = "fake"

    def __init__(self, reply=None, exc=None):
        self.reply, self.exc = reply, exc

    def complete(self, system, user, max_tokens=2000, json_mode=False):
        if self.exc:
            raise self.exc
        return self.reply


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def new_sr(store):
    return store.create_sr("API 필터 추가", "GET /api/members 에 from/to 추가", "이요청", "acme/member")


def test_classified_high_confidence(store):
    sr = new_sr(store)
    st = process_sr(store, FakeLLM('```json\n{"type":"code","confidence":0.93,"reason":"API 변경"}\n```'), sr)
    row = store.get_sr(sr)
    assert st == "classified" and row["sr_type"] == "code" and row["confidence"] == 0.93
    ev = [a["event"] for a in store.audit_rows(sr)]
    assert ev == ["sr.created", "sr.auto_classified"]
    assert "GET /api/members" in store.audit_rows(sr)[1]["detail"]["user"]   # 프롬프트 전문 기록


def test_low_confidence_and_unknown_go_to_review(store):
    sr = new_sr(store)
    assert process_sr(store, FakeLLM('{"type":"data","confidence":0.4,"reason":"?"}'), sr) == "needs_review"
    sr2 = new_sr(store)
    assert process_sr(store, FakeLLM('{"type":"unknown","confidence":0.95,"reason":"?"}'), sr2) == "needs_review"


def test_garbage_response_needs_review_and_logged(store):
    sr = new_sr(store)
    assert process_sr(store, FakeLLM("모르겠어요"), sr) == "needs_review"
    assert store.audit_rows(sr)[-1]["event"] == "llm.invalid_response"
    sr2 = new_sr(store)
    assert process_sr(store, FakeLLM('{"type":"hack","confidence":1}'), sr2) == "needs_review"


def test_llm_down_keeps_sr_pending(store):
    sr = new_sr(store)
    assert process_sr(store, FakeLLM(exc=OllamaError("연결 실패")), sr) == "pending_llm"
    assert store.get_sr(sr)["status"] == "pending_llm"
    assert store.audit_rows(sr)[-1]["event"] == "llm.error"


def test_manual_classification_audited(store):
    sr = new_sr(store)
    process_sr(store, FakeLLM('{"type":"code","confidence":0.9,"reason":"x"}'), sr)
    set_manual(store, sr, "김담당", "data", "실은 데이터 건")
    assert store.get_sr(sr)["sr_type"] == "data"
    last = store.audit_rows(sr)[-1]
    assert last["event"] == "sr.manual_classified" and last["actor"] == "김담당" and last["detail"]["from"] == "code"
    with pytest.raises(ValueError):
        set_manual(store, sr, "", "data")
    with pytest.raises(ValueError):
        set_manual(store, sr, "김담당", "unknown")


def test_audit_append_only_and_chain(store, tmp_path):
    sr = new_sr(store)
    store.audit(sr, "a", "x", {"k": 1})
    assert store.verify_audit()["ok"]
    db = sqlite3.connect(tmp_path / "t.db")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE audit_log SET actor='z'")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM audit_log")
    # 트리거를 우회해 변조하면 체인 검증이 잡아낸다
    db.execute("DROP TRIGGER audit_no_update")
    db.execute("UPDATE audit_log SET detail='{\"k\": 2}' WHERE id=2")
    db.commit()
    r = Store(tmp_path / "t.db").verify_audit()
    assert not r["ok"] and r["broken_at"] == 2


def test_mask_secrets():
    t = mask_secrets('db password=hunter2 token: abc123 Bearer abcdefghijklmnop glpat-AAAAAAAAAAAAAAAA AKIAABCDEFGHIJKLMNOP')
    for leak in ("hunter2", "abc123", "abcdefghijklmnop", "glpat-AAAA", "AKIAABCD"):
        assert leak not in t
    assert "password=***" in t


def test_audit_masks_prompt(store):
    sr = store.create_sr("접속", "DB password=hunter2 로 접속 안 됨", "이요청")
    process_sr(store, FakeLLM('{"type":"code","confidence":0.9,"reason":"x"}'), sr)
    assert "hunter2" not in json.dumps(store.audit_rows(sr), ensure_ascii=False)


def test_create_validation(store):
    with pytest.raises(ValueError):
        store.create_sr("", "x", "y")
    with pytest.raises(ValueError):
        store.create_sr("t", "x" * 20001, "y")


def test_parse_clamps_confidence():
    assert parse_classification('{"type":"code","confidence":7}').confidence == 1.0


# ---- Ollama 클라이언트 (가짜 서버) ----
class _Fake(BaseHTTPRequestHandler):
    calls: list = []
    fail_first = 0

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(n))
        _Fake.calls.append((self.path, body))
        if _Fake.fail_first > 0:
            _Fake.fail_first -= 1
            self.send_response(503)
            self.end_headers()
            return
        if body.get("model") == "missing":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"model not found"}')
            return
        out = {"message": {"content": '{"type":"code","confidence":0.9,"reason":"r"}'}} if self.path == "/api/chat" \
            else {"embeddings": [[0.1, 0.2]]}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        data = json.dumps({"models": [{"name": "qwen2.5-coder:7b"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def fake_ollama():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Fake.calls, _Fake.fail_first = [], 0
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_ollama_chat_options_and_retry(fake_ollama):
    _Fake.fail_first = 2
    sleeps = []
    llm = OllamaLLM(host=fake_ollama, model="qwen2.5-coder:7b", num_ctx=4096, sleep=sleeps.append)
    assert "code" in llm.complete("sys", "user", json_mode=True)
    path, body = _Fake.calls[-1]
    assert path == "/api/chat" and body["format"] == "json" and body["stream"] is False
    assert body["options"]["temperature"] == 0 and body["options"]["num_ctx"] == 4096
    assert sleeps == [2, 4] and llm.last_call["model"] == "qwen2.5-coder:7b"
    assert llm.embed(["a"]) == [[0.1, 0.2]] and _Fake.calls[-1][0] == "/api/embed"
    assert llm.status()["model_ready"]


def test_ollama_404_not_retried_and_unreachable_retryable(fake_ollama):
    llm = OllamaLLM(host=fake_ollama, model="missing", sleep=lambda s: None)
    n = len(_Fake.calls)
    with pytest.raises(OllamaError) as e:
        llm.complete("s", "u")
    assert not e.value.retryable and len(_Fake.calls) == n + 1 and "ollama pull" in str(e.value)
    dead = OllamaLLM(host="http://127.0.0.1:1", retries=1, sleep=lambda s: None, timeout=1)
    with pytest.raises(OllamaError) as e2:
        dead.complete("s", "u")
    assert e2.value.retryable


def test_ollama_rejects_remote_host():
    with pytest.raises(OllamaError):
        OllamaLLM(host="http://8.8.8.8:11434")
    OllamaLLM(host="http://10.1.2.3:11434")   # 사설망 허용


# ---- 웹 API ----
@pytest.fixture
def server(tmp_path):
    app = App(Store(tmp_path / "w.db"), FakeLLM('{"type":"data","confidence":0.88,"reason":"데이터 정정"}'), sync_mode=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", app
    srv.shutdown()


def call(url, data=None, headers=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"content-type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_web_flow(server):
    base, app = server
    code, raw = call(base + "/api/sr", {"title": "상태 정정", "body": "탈퇴 회원 상태 정정", "requester": "이요청"})
    assert code == 200
    sid = json.loads(raw)["id"]
    d = json.loads(call(f"{base}/api/sr/{sid}")[1])
    assert d["sr"]["status"] == "classified" and d["sr"]["sr_type"] == "data"
    d = json.loads(call(f"{base}/api/sr/{sid}/classify", {"actor": "김담당", "type": "code"})[1])
    assert d["sr"]["sr_type"] == "code" and d["audit"][-1]["actor"] == "김담당"
    code, raw = call(f"{base}/api/sr/{sid}/audit.csv")
    assert code == 200 and "sr.manual_classified" in raw.decode("utf-8-sig")
    assert json.loads(call(base + "/api/sr")[1])["items"][0]["id"] == sid
    assert call(base + "/api/sr/999")[0] == 404
    assert call(base + "/api/sr", {"title": "", "body": "x", "requester": "y"})[0] == 400
    assert call(base + "/api/sr", {"title": "t", "body": "b", "requester": "r"},
                headers={"Origin": "http://evil.example"})[0] == 403
    assert call(f"{base}/api/sr/{sid}/retry", {"actor": "x"})[0] == 400   # classified 상태는 재시도 불가
    assert b"SR" in call(base + "/")[1]


def test_client_abort_is_silent(server):
    import socket
    base, _ = server
    host, port = base.replace("http://", "").split(":")
    for _ in range(3):   # 요청만 보내고 응답을 받기 전에 끊는다
        c = socket.create_connection((host, int(port)))
        c.sendall(b"GET /api/status HTTP/1.1\r\nHost: x\r\n\r\n")
        c.close()
    assert call(base + "/api/sr")[0] == 200   # 서버는 계속 정상


def test_web_projects_and_impact(server, tmp_path):
    base, app = server
    spring = str(Path(__file__).parent / "fixtures" / "spring")
    code, raw = call(base + "/api/projects", {"name": "acme/user", "local_path": spring, "token_env": "NOPE_TOKEN"})
    assert code == 200
    chk = json.loads(raw)["check"]
    assert chk["path_exists"] and chk["java_files"] > 0 and not chk["token_set"]
    assert call(base + "/api/projects", {"name": "x", "local_path": spring, "token_env": "glpat-abc.def"})[0] == 400
    assert json.loads(call(base + "/api/projects")[1])["items"][0]["name"] == "acme/user"
    assert json.loads(call(base + "/api/projects/check?name=acme%2Fuser")[1])["path_exists"]
    # 분류(code) -> 프로젝트 지정되어 있으면 영향 분석까지 자동 진행
    app.llm = FakeLLM('{"type":"code","confidence":0.95,"reason":"x","keywords":["User"]}')
    sid = json.loads(call(base + "/api/sr", {"title": "사용자 목록", "body": "list users", "requester": "이", "project": "acme/user"})[1])["id"]
    d = json.loads(call(f"{base}/api/sr/{sid}")[1])
    assert d["sr"]["status"] == "impact_ready" and d["impact"]["candidates"]
    f = d["impact"]["candidates"][0]["file"]
    d = json.loads(call(f"{base}/api/sr/{sid}/impact", {"actor": "김담당", "files": [f]})[1])
    assert d["sr"]["status"] == "patch_failed"          # 확정 즉시 수정안 생성 시도(이 픽스처는 git 저장소가 아님)
    assert call(f"{base}/api/sr/{sid}/impact", {"actor": "김담당", "files": ["../x"]})[0] == 400
    assert call(f"{base}/api/sr/{sid}/project", {"actor": "", "project": "acme/user"})[0] == 400
    assert call(base + "/api/projects/delete", {"name": "acme/user", "actor": "a"})[0] == 200
