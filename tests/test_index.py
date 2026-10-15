import json
import shutil
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

from ssot.impact import analyze_sr, load_index, refresh_index, search, summarize_classes  # noqa: E402
from ssot.llm import LLM  # noqa: E402
from ssot.sr_web import App, make_handler  # noqa: E402
from ssot.store import Store  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "spring"

WITHDRAW = """package com.acme.member;
import org.springframework.stereotype.Service;
@Service
public class LeaveHandler {
    public void process(Long id) { repo.updateFlag(id, "W"); }
    public void restore(Long id) { repo.updateFlag(id, "A"); }
    public void purge(Long id) { repo.remove(id); }
}
"""


class SumLLM(LLM):
    model = "fake"

    def __init__(self):
        self.calls = []

    def complete(self, system, user, max_tokens=2000, json_mode=False):
        if "요약" in system:
            self.calls.append(user)
            return json.dumps({"summary": "회원 탈퇴 처리와 복구를 담당하는 서비스"}) if "LeaveHandler" in user else json.dumps({"summary": "사용자 서비스"})
        return json.dumps({"keywords": []})


@pytest.fixture
def proj(tmp_path):
    src = tmp_path / "src"
    shutil.copytree(FIXTURE, src)
    (src / "src/main/java/com/acme").mkdir(parents=True, exist_ok=True)
    (src / "src/main/java/com/acme/LeaveHandler.java").write_text(WITHDRAW, encoding="utf-8")
    s = Store(tmp_path / "x.db")
    s.upsert_project("acme/member", str(src))
    return s, src


def test_refresh_is_incremental(proj):
    s, src = proj
    r1 = refresh_index(s, src, "acme/member")
    assert r1["changed"] == r1["files"] > 0 and s.index_meta("acme/member")["chunks"] == r1["chunks"]
    v1 = s.index_meta("acme/member")["version"]
    r2 = refresh_index(s, src, "acme/member")
    assert r2["changed"] == 0 and s.index_meta("acme/member")["version"] == v1                 # 변경 없으면 그대로
    f = src / "src/main/java/com/acme/LeaveHandler.java"
    time.sleep(0.01)
    f.write_text(WITHDRAW.replace("purge", "erase"), encoding="utf-8")
    r3 = refresh_index(s, src, "acme/member")
    assert r3["changed"] == 1 and s.index_meta("acme/member")["version"] == v1 + 1
    assert any(c.method == "erase" for c in load_index(s, "acme/member")[0])
    f.unlink()
    r4 = refresh_index(s, src, "acme/member")
    assert r4["removed"] == 1 and not any(c.cls == "LeaveHandler" for c in load_index(s, "acme/member")[0])


def test_touch_without_content_change_does_not_reparse(proj):
    s, src = proj
    refresh_index(s, src, "acme/member")
    v = s.index_meta("acme/member")["version"]
    f = src / "src/main/java/com/acme/LeaveHandler.java"
    f.write_text(WITHDRAW, encoding="utf-8")        # 내용 동일, 수정 시각만 변경
    r = refresh_index(s, src, "acme/member")
    assert r["changed"] == 0 and s.index_meta("acme/member")["version"] == v


def test_summaries_generated_once_and_improve_korean_search(proj):
    s, src = proj
    llm = SumLLM()
    refresh_index(s, src, "acme/member")
    sr = s.create_sr("탈퇴 회원 처리", "탈퇴 회원 처리 로직을 고쳐 주세요", "이", "acme/member")
    before, _, _ = search(s, llm, src, "탈퇴 회원 처리", "탈퇴 회원 처리 로직", [], "acme/member")
    assert not any(c["file"].endswith("LeaveHandler.java") for c in before[:1])                 # 영문 코드만으로는 못 찾음
    n = summarize_classes(s, llm, "acme/member")
    assert n >= 1 and any("LeaveHandler" in u for u in llm.calls)
    after, _, _ = search(s, llm, src, "탈퇴 회원 처리", "탈퇴 회원 처리 로직", [], "acme/member")
    assert after[0]["file"].endswith("LeaveHandler.java")                                         # 요약 덕분에 1위
    calls = len(llm.calls)
    assert summarize_classes(s, llm, "acme/member") == 0 and len(llm.calls) == calls            # 재실행해도 다시 요약하지 않음
    (src / "src/main/java/com/acme/LeaveHandler.java").write_text(WITHDRAW + "// changed\n", encoding="utf-8")
    refresh_index(s, src, "acme/member")
    assert summarize_classes(s, llm, "acme/member") == 1                                          # 바뀐 파일만 다시


def test_projects_are_isolated(proj, tmp_path):
    s, src = proj
    other = tmp_path / "other"
    other.mkdir()
    (other / "Z.java").write_text("package z; @Service public class ZOnly { public void a(){} public void b(){} public void c(){} }", encoding="utf-8")
    s.upsert_project("other", str(other))
    refresh_index(s, src, "acme/member")
    refresh_index(s, other, "other")
    assert not any(c.cls == "ZOnly" for c in load_index(s, "acme/member")[0])
    assert any(c.cls == "ZOnly" for c in load_index(s, "other")[0])


def test_analyze_sr_uses_prebuilt_index(proj):
    s, src = proj
    refresh_index(s, src, "acme/member")
    sr = s.create_sr("사용자 목록", "list users", "이", "acme/member")
    assert analyze_sr(s, SumLLM(), sr) == "impact_ready"
    assert s.get_impact(sr)["candidates"]


def test_web_index_job(proj):
    s, src = proj
    app = App(s, SumLLM(), sync_mode=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path, data=None):
        req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None,
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    try:
        code, out = call("/api/projects/index", {"name": "acme/member", "actor": "김", "summarize": True, "embed": False})
        assert code == 200 and out["job"]["state"] == "done" and out["index"]["chunks"] > 0 and out["index"]["summaries"] >= 1
        assert call("/api/projects/index?name=acme%2Fmember")[1]["index"]["files"] > 0
        assert call("/api/projects/index", {"name": "nope"})[0] == 400
        assert "index.completed" in [a["event"] for a in s.audit_rows()]
    finally:
        srv.shutdown()
