import base64
import hashlib
import json
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

from ssot.impact import get_index  # noqa: E402
from ssot.llm import LLM, make_llm  # noqa: E402
from ssot.sr_web import App, _pw_ok, load_users, make_handler  # noqa: E402
from ssot.store import Store  # noqa: E402

ROOT = Path(__file__).parent.parent


class L(LLM):
    model = "fake"

    def complete(self, system, user, max_tokens=2000, json_mode=False):
        return '{"type":"code","confidence":0.9,"reason":"x"}'


def basic(u, p):
    return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}


def req(url, data=None, headers=None):
    r = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                               headers={"content-type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@pytest.fixture
def secured(tmp_path, monkeypatch):
    monkeypatch.setenv("SSOT_USERS", f"kim:pw1,lee:sha256:{hashlib.sha256('pw2'.encode()).hexdigest()}")
    users = load_users()
    app = App(Store(tmp_path / "s.db"), L(), sync_mode=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app, users))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", app
    srv.shutdown()


def test_users_parsing_and_passwords(monkeypatch):
    monkeypatch.setenv("SSOT_USERS", "a:b:c, d:e ,bad")
    assert load_users() == {"a": "b:c", "d": "e"}
    assert _pw_ok("pw", "pw") and not _pw_ok("pw", "px")
    assert _pw_ok("sha256:" + hashlib.sha256(b"x").hexdigest(), "x")


def test_login_required_everywhere_but_webhook(secured, monkeypatch):
    base, app = secured
    assert req(base + "/")[0] == 401 and req(base + "/api/sr")[0] == 401
    assert req(base + "/api/sr", {"title": "t", "body": "b", "requester": "x"})[0] == 401
    assert req(base + "/api/sr", headers=basic("kim", "wrong"))[0] == 401
    assert req(base + "/api/sr", headers=basic("kim", "pw1"))[0] == 200
    assert req(base + "/api/sr", headers=basic("lee", "pw2"))[0] == 200            # sha256 비밀번호
    assert json.loads(req(base + "/api/whoami", headers=basic("kim", "pw1"))[1])["user"] == "kim"
    monkeypatch.setenv("SSOT_WEBHOOK_SECRET", "hook")
    assert req(base + "/api/hooks/gitlab", {}, {"X-Gitlab-Token": "hook"})[0] == 200   # 웹훅은 비밀 토큰으로 인증


def test_authenticated_identity_overrides_typed_names(secured):
    base, app = secured
    code, raw = req(base + "/api/sr", {"title": "t", "body": "b", "requester": "사칭"}, basic("kim", "pw1"))
    sid = json.loads(raw)["id"]
    assert app.store.get_sr(sid)["requester"] == "kim"
    req(f"{base}/api/sr/{sid}/classify", {"actor": "사칭", "type": "data"}, basic("lee", "pw2"))
    assert app.store.audit_rows(sid)[-1]["actor"] == "lee"                             # 로그에는 인증된 이름


def test_repeated_failures_are_throttled(secured):
    base, _ = secured
    codes = [req(base + "/api/sr", headers=basic("kim", f"bad{i}"))[0] for i in range(7)]
    assert 429 in codes
    assert req(base + "/api/sr", headers=basic("kim", "pw1"))[0] == 429                # 차단 중에는 올바른 비밀번호도 거부


def test_non_loopback_requires_login(monkeypatch, tmp_path):
    from ssot.sr_web import serve
    monkeypatch.delenv("SSOT_USERS", raising=False)
    with pytest.raises(SystemExit, match="SSOT_USERS"):
        serve(str(tmp_path / "x.db"), host="0.0.0.0")


def test_air_gapped_no_external_references():
    html = (ROOT / "ssot" / "web_static" / "sr.html").read_text(encoding="utf-8")
    assert not re.findall(r"""(?:src|href)\s*=\s*["']https?://""", html)           # CDN/외부 폰트 없음
    assert "@import" not in html and "fetch('http" not in html


def test_external_llm_blocked_in_local_only(monkeypatch):
    monkeypatch.setenv("SSOT_LOCAL_ONLY", "1")
    with pytest.raises(RuntimeError, match="SSOT_LOCAL_ONLY"):
        make_llm("anthropic")


def test_index_cache_reused_until_files_change(tmp_path):
    store = Store(tmp_path / "i.db")
    src = tmp_path / "src"
    src.mkdir()
    f = src / "A.java"
    f.write_text("package a; public class A { public void run() {} }", encoding="utf-8")
    c1, bm1 = get_index(store, src, "p")
    c2, bm2 = get_index(store, src, "p")
    assert bm1 is bm2                                                                    # 변경 없으면 재사용
    f.write_text("package a; public class A { public void run() {} public void stop() {} }", encoding="utf-8")
    c3, bm3 = get_index(store, src, "p")
    assert bm3 is not bm1 and any(c.method == "stop" for c in c3)
