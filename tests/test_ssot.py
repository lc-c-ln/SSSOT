import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers import build_docx, build_xlsx  # noqa: E402
from ssot.cli import main  # noqa: E402
from ssot.docs import open_document  # noqa: E402
from ssot.facts import collect_facts  # noqa: E402
from ssot.llm import LLM  # noqa: E402
from ssot.sync import sync_narrative, sync_tables  # noqa: E402

SPRING = Path(__file__).parent / "fixtures" / "spring"

CLASS_ROWS = [
    ["번호", "클래스명", "유형", "설명", "주요 메서드"],
    ["1", "UserController", "컨트롤러", "사용자 API", "get, list"],           # 메서드 일부 누락 -> 수정
    ["2", "UserService", "Service", "", "find, list, register, update"],      # 설명 비어 있음 -> 채움
    ["3", "OldBatchJob", "Component", "폐기됨", ""],                            # 소스에 없음 -> missing
]
API_ROWS = [
    ["메서드", "경로", "설명"],
    ["GET", "/api/users/{userId}", "사용자 조회"],    # 변수명 차이 -> path 갱신
    ["GET", "/api/users", "목록"],
]
CONFIG_ROWS = [
    ["설정키", "기본값", "설명"],
    ["server.port", "9090", "서버 포트"],              # 값 불일치
    ["user.cache.ttlSeconds", "600", "캐시 TTL"],     # relaxed binding 일치
]


@pytest.fixture(scope="module")
def facts():
    return collect_facts(SPRING)


def test_analyzer_finds_components_endpoints_config(facts):
    keys = {(f.kind, f.match) for f in facts}
    assert ("class", "UserController") in keys
    assert ("class", "User") in keys and ("class", "UserRepository") in keys
    assert ("endpoint", "GET /api/users/{}") in keys
    assert ("endpoint", "PATCH /api/users/{}") in keys   # RequestMapping method={PUT,PATCH}
    assert ("endpoint", "PUT /api/users/{}") in keys
    assert ("config", "server.port") in keys
    pw = next(f for f in facts if f.match == "spring.datasource.password")
    assert pw.get("value") == "****"                     # 비밀값 마스킹
    ctrl = next(f for f in facts if f.match == "UserController")
    assert ctrl.get("summary").startswith("사용자 관리 API")
    assert "register" in ctrl.get("methods")
    get = next(f for f in facts if f.match == "GET /api/users/{}")
    assert get.get("returns") == "UserDto"               # ResponseEntity 언랩
    assert "HttpServletRequest" not in next(f for f in facts if f.match == "GET /api/users").get("params")


def test_markdown_sync(facts, tmp_path):
    md = tmp_path / "def.md"
    rows = lambda r: "\n".join("| " + " | ".join(x) + " |" + ("\n|---|---|---|---|---|" if i == 0 else "") for i, x in enumerate(r))
    md.write_text("# 컴포넌트 정의서\n\n" + rows(CLASS_ROWS) + "\n\n## API\n\n" + rows(API_ROWS) +
                  "\n\n## 설정\n\n" + rows(CONFIG_ROWS) + "\n", encoding="utf-8")
    doc = open_document(md)
    ch = sync_tables(doc, facts)
    acts = {(c.action, c.key, c.field) for c in ch}
    assert ("update", "UserController", "stereotype") not in acts           # 컨트롤러 == RestController
    assert ("update", "UserController", "methods") in acts
    assert ("fill", "UserService", "설명") in acts
    assert any(c.action == "missing" and c.key == "OldBatchJob" for c in ch)
    assert any(c.action == "add" and c.key == "UserRepository" for c in ch)
    assert any(c.action == "add" and c.key == "POST /api/users/register" for c in ch)
    assert ("update", "GET /api/users/{id}", "path") in acts
    assert ("update", "server.port", "value") in acts
    assert not any(c.key == "user.cache.ttl-seconds" for c in ch)           # relaxed binding 일치
    out = tmp_path / "out.md"
    doc.save(out)
    text = out.read_text(encoding="utf-8")
    assert "| UserRepository |" in text
    # 재실행하면 변경 없음 (멱등)
    again = sync_tables(open_document(out), facts)
    assert [c for c in again if c.action in ("update", "fill", "add")] == []


def test_docx_sync_preserves_package(facts, tmp_path):
    p = tmp_path / "def.docx"
    build_docx(p, [("h1", "컴포넌트 정의서"), ("p", "개요"), ("table", CLASS_ROWS), ("h1", "API"), ("table", API_ROWS)])
    doc = open_document(p)
    ch = sync_tables(doc, facts)
    assert any(c.action == "add" for c in ch)
    out = tmp_path / "def.updated.docx"
    doc.save(out)
    with zipfile.ZipFile(out) as z:
        xml = z.read("word/document.xml").decode("utf-8")
        assert z.testzip() is None
    assert 'mc:Ignorable="w14"' in xml and "xmlns:w14=" in xml            # 네임스페이스 보존
    assert "UserRepository" in xml and "<w:b" in xml                    # 새 행/서식 유지
    reopened = open_document(out)
    names = [r[1] for r in reopened.tables()[0].rows()]
    assert names[-1] in {"User", "UserRepository", "POST /api/users/register"} and "UserRepository" in names
    assert "tblHeader" not in xml.split("UserRepository")[1].split("</w:tr>")[0]  # 헤더 속성 복제 안 됨


def test_xlsx_sync(facts, tmp_path):
    p = tmp_path / "def.xlsx"
    build_xlsx(p, "컴포넌트", CLASS_ROWS)
    doc = open_document(p)
    ch = sync_tables(doc, facts)
    assert any(c.action == "add" and c.key == "User" for c in ch)
    out = tmp_path / "out.xlsx"
    doc.save(out)
    re_doc = open_document(out)
    rows = re_doc.tables()[0].rows()
    assert rows[1][3] != ""                                  # UserService 설명 채움 (공유 문자열 + inlineStr 혼용)
    assert any(r[1] == "UserRepository" for r in rows)
    with zipfile.ZipFile(out) as z:
        assert 'mc:Ignorable="x14ac"' in z.read("xl/worksheets/sheet1.xml").decode()
        assert "A1:E" in z.read("xl/worksheets/sheet1.xml").decode()


class FakeLLM(LLM):
    def __init__(self):
        self.calls = 0

    def complete(self, system, user, max_tokens=2000):
        if "한 줄 설명" in system:
            return "사용자 DTO 설명"
        self.calls += 1
        assert "server.port" in user and "8080" in user       # 소스 사실이 프롬프트에 포함됨
        if "9090" in user:
            return '```json\n{"ok": false, "fixes": [{"wrong": "9090", "right": "8080"}], "reason": "application.yml server.port"}\n```'
        return '{"ok": true}'


def test_narrative_updates_only_conflicting_paragraph(facts, tmp_path):
    md = tmp_path / "manual.md"
    md.write_text("# 운영자 매뉴얼\n\n## 기동\n\n서버 포트는 9090 이다. `server.port` 로 변경한다.\n\n## 기타\n\n문의는 담당자에게.\n",
                  encoding="utf-8")
    doc = open_document(md)
    llm = FakeLLM()
    ch = sync_narrative(doc, facts, llm)
    assert len(ch) == 1 and ch[0].new.startswith("서버 포트는 8080 이다.")
    doc.save(md)
    assert "문의는 담당자에게." in md.read_text(encoding="utf-8")


def test_cli_preview_does_not_write(tmp_path, capsys):
    md = tmp_path / "d.md"
    md.write_text("| 클래스명 | 설명 |\n|---|---|\n| UserService | |\n", encoding="utf-8")
    before = md.read_text(encoding="utf-8")
    rc = main(["sync", "--src", str(SPRING), "--doc", str(md), "--check"])
    assert rc == 1 and md.read_text(encoding="utf-8") == before
    rc = main(["sync", "--src", str(SPRING), "--doc", str(md), "--apply", "--out-dir", str(tmp_path / "o")])
    assert (tmp_path / "o" / "d.updated.md").exists() and md.read_text(encoding="utf-8") == before


def test_openai_compat_client_against_fake_ollama(tmp_path):
    import json, threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from ssot.llm import make_llm

    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen["path"], seen["model"] = self.path, body["model"]
            out = json.dumps({"choices": [{"message": {"content": "사용자 조회 서비스"}}]}).encode()
            self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a): pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    llm = make_llm("ollama", "qwen2.5:7b", f"http://127.0.0.1:{srv.server_port}/v1")
    assert llm.complete("sys", "user") == "사용자 조회 서비스"
    assert seen == {"path": "/v1/chat/completions", "model": "qwen2.5:7b"}
    srv.shutdown()


def test_llm_connection_error_is_actionable():
    from ssot.llm import make_llm
    with pytest.raises(RuntimeError, match="ollama serve"):
        make_llm("ollama", "x", "http://127.0.0.1:9/v1").complete("s", "u")


def test_narrative_checks_each_paragraph_separately_and_survives_bad_reply(facts, tmp_path):
    md = tmp_path / "m.md"
    paras = ["# 매뉴얼", "## 기동", "서버 포트는 8080 이다. `server.port`",
             "캐시는 `user.cache.ttl-seconds` 600초.", "포트는 9090 이며 `server.port` 이다."]
    md.write_text(chr(10).join(p + chr(10) for p in paras), encoding="utf-8")

    class Flaky(FakeLLM):
        def complete(self, system, user, max_tokens=2000):
            if "600초" in user:
                return "죄송합니다, JSON이 아닙니다"
            return super().complete(system, user, max_tokens)

    llm = Flaky()
    ch = sync_narrative(open_document(md), [f for f in facts if f.kind == "config"], llm)
    assert llm.calls == 2                                  # 8080 문단, 9090 문단 (ttl 문단은 깨진 응답)
    assert [c.action for c in ch].count("narrative") == 1  # 9090 문단만 수정
    assert [c.action for c in ch].count("review") == 1     # 깨진 응답은 review 로 남기고 계속 진행


def _narr(facts, tmp_path, para, reply):
    md = tmp_path / "n.md"
    md.write_text("## 기동" + chr(10) * 2 + para + chr(10), encoding="utf-8")

    class L(LLM):
        def complete(self, system, user, max_tokens=2000):
            return reply

    doc = open_document(md)
    return sync_narrative(doc, [f for f in facts if f.kind == "config"], L()), doc


def test_narrative_only_replaces_the_wrong_value_and_keeps_rest(facts, tmp_path):
    reply = '{"ok": false, "fixes": [{"wrong": "30초", "right": "600초"}], "reason": "ttl"}'
    ch, doc = _narr(facts, tmp_path, "캐시 TTL은 `user.cache.ttl-seconds` 이며 기본값은 30초이다. 변경 시 재기동한다.", reply)
    assert ch[0].action == "narrative"
    assert ch[0].new == "캐시 TTL은 `user.cache.ttl-seconds` 이며 기본값은 600초이다. 변경 시 재기동한다."


def test_narrative_rejects_invented_values_and_missing_wrong(facts, tmp_path):
    # 소스에 없는 값(7777)을 지어낸 경우
    ch, _ = _narr(facts, tmp_path, "포트는 9090 이며 `server.port` 이다.",
                  '{"ok": false, "fixes": [{"wrong": "9090", "right": "7777"}]}')
    assert [c.action for c in ch] == ["review"] and "소스 사실에 없" in ch[0].note
    # 문단에 없는 문구를 wrong 으로 준 경우
    ch, _ = _narr(facts, tmp_path, "포트는 9090 이며 `server.port` 이다.",
                  '{"ok": false, "fixes": [{"wrong": "1234", "right": "8080"}]}')
    assert [c.action for c in ch] == ["review"] and "문단에 없" in ch[0].note
    # 검증 불가능한 한글-only 교체
    ch, _ = _narr(facts, tmp_path, "포트는 9090 이며 `server.port` 이다.",
                  '{"ok": false, "fixes": [{"wrong": "이며", "right": "입니다"}]}')
    assert [c.action for c in ch] == ["review"]


def _web_server():
    import threading
    from http.server import ThreadingHTTPServer
    from ssot.web import Handler
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def _call(base, path, body=None):
    import json, urllib.request, urllib.error
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_web_preview_selective_apply_and_download(tmp_path):
    import base64, json
    rows = ["| 번호 | 클래스명 | 유형 | 설명 | 주요 메서드 |", "|---|---|---|---|---|",
            "| 1 | UserController | 컨트롤러 | 사용자 API | get |", "| 2 | UserService | 서비스 | | find |"]
    md = (chr(10).join(["# 정의서", ""] + rows) + chr(10)).encode()
    srv, base = _web_server()
    try:
        code, raw = _call(base, "/api/preview", {"src_path": str(SPRING), "docs": [{"name": "정의서.md", "data": base64.b64encode(md).decode()}]})
        res = json.loads(raw)
        assert code == 200 and res["facts"]["class"] == 4
        changes = res["docs"][0]["changes"]
        assert all("op" not in c for c in changes)
        chosen = [c["id"] for c in changes if c["action"] == "fill"]            # 설명 채움만 선택
        assert chosen and any(c["action"] == "add" for c in changes)
        code, raw = _call(base, "/api/apply", {"session": res["session"], "accepted": {"정의서.md": chosen}})
        out = json.loads(raw)["files"][0]
        assert out["applied"] == len(chosen)
        code, body = _call(base, out["url"])
        text = body.decode("utf-8")
        assert "사용자 비즈니스 로직" in text                                   # 선택한 항목은 반영
        assert "UserRepository" not in text and "get |" in text                # 선택 안 한 추가/수정은 미반영
    finally:
        srv.shutdown()


def test_web_errors_and_zip_slip(tmp_path):
    import base64, io, json, zipfile
    srv, base = _web_server()
    try:
        code, raw = _call(base, "/api/preview", {"src_path": str(tmp_path / "nope"), "docs": []})
        assert code == 400 and "소스 폴더" in json.loads(raw)["error"]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("../evil.txt", "x")
        code, raw = _call(base, "/api/preview", {"src_zip": base64.b64encode(buf.getvalue()).decode(),
                                                 "docs": [{"name": "a.md", "data": ""}]})
        assert code == 400 and "허용되지 않는" in json.loads(raw)["error"]
        assert _call(base, "/download/zzz/a.md")[0] == 404
        assert _call(base, "/")[0] == 200
    finally:
        srv.shutdown()
