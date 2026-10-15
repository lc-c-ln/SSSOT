import json
import shutil
import subprocess
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

from ssot.docsync import apply_doc_patch, dismiss_doc_patch, handle_push, match_project, scan_commit, verify_secret  # noqa: E402
from ssot.sr_web import App, make_handler  # noqa: E402
from ssot.store import Store  # noqa: E402

ROOT = Path(__file__).parent
DOC = """# 컴포넌트 정의서

| 클래스명 | 유형 | 설명 | 주요 메서드 |
|---|---|---|---|
| UserController | 컨트롤러 | 사용자 API | get |
| OldBatchJob | Component | 폐기됨 | |
"""


def git(root, *a):
    return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def env(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "fixtures" / "spring", repo)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init [SR-0001]")
    doc = tmp_path / "정의서.md"
    doc.write_text(DOC, encoding="utf-8")
    s = Store(tmp_path / "d.db")
    s.upsert_project("acme/user", str(repo), "https://gitlab.company.com/acme/user", "main", doc_paths=[str(doc)])
    s.create_sr("t", "b", "이요청", "acme/user")
    return s, repo, doc, git(repo, "rev-parse", "HEAD")


def push_payload(sha, ref="refs/heads/main", url="https://gitlab.company.com/acme/user"):
    return {"object_kind": "push", "ref": ref, "after": sha, "checkout_sha": sha,
            "project": {"web_url": url + "/", "git_http_url": url + ".git"},
            "commits": [{"message": "SR-0001 반영\n\nSR: SR-0001"}]}


def test_match_and_secret(env, monkeypatch):
    s, *_ = env
    assert match_project(s, push_payload("a" * 40))["name"] == "acme/user"
    assert match_project(s, push_payload("a" * 40, url="https://gitlab.company.com/other/x")) is None
    monkeypatch.delenv("SSOT_WEBHOOK_SECRET", raising=False)
    assert not verify_secret("anything")                  # 비밀이 없으면 전부 거부
    monkeypatch.setenv("SSOT_WEBHOOK_SECRET", "s3cret")
    assert verify_secret("s3cret") and not verify_secret("nope") and not verify_secret(None)


def test_push_creates_patch_and_links_sr(env):
    s, repo, doc, sha = env
    res = handle_push(s, None, push_payload(sha))
    assert res["created"] == 1
    dp = s.list_doc_patches("pending")[0]
    acts = {(c["action"], c["key"]) for c in dp["changes"]}
    assert any(a == "update" for a, _ in acts) and any(a == "missing" for a, _ in acts)   # 메서드 갱신 + 소스에 없는 행 알림
    assert dp["sr_ids"] == [1] and "수정 가능" in dp["summary"]
    assert "doc.patch_proposed" in [a["event"] for a in s.audit_rows(1)]
    assert handle_push(s, None, push_payload(sha))["created"] == 0                        # 재전송은 중복 생성 안 함
    assert doc.read_text(encoding="utf-8") == DOC                                           # 원본 불변


def test_push_ignored_cases(env):
    s, repo, doc, sha = env
    assert "기본 브랜치" in handle_push(s, None, push_payload(sha, ref="refs/heads/feature"))["ignored"]
    assert "삭제" in handle_push(s, None, {**push_payload(sha), "after": "0" * 40, "checkout_sha": None})["ignored"]
    assert "등록되지" in handle_push(s, None, push_payload(sha, url="https://x/y/z"))["ignored"]


def test_unknown_commit_gives_pull_hint(env):
    s, repo, doc, sha = env
    proj = s.get_project("acme/user")
    with pytest.raises(Exception, match="git pull"):
        scan_commit(s, None, proj, "b" * 40)


def test_apply_writes_updated_copy_only_selected(env):
    s, repo, doc, sha = env
    handle_push(s, None, push_payload(sha))
    dp = s.list_doc_patches("pending")[0]
    upd = [c["id"] for c in dp["changes"] if c["action"] == "update"]
    with pytest.raises(ValueError):
        apply_doc_patch(s, dp["id"], "김담당", [])
    with pytest.raises(ValueError):
        apply_doc_patch(s, dp["id"], "", upd)
    out = apply_doc_patch(s, dp["id"], "김담당", upd)
    assert out.name == "정의서.updated.md" and "OldBatchJob" in out.read_text(encoding="utf-8")   # 삭제 안 함
    assert "list" in out.read_text(encoding="utf-8")
    assert doc.read_text(encoding="utf-8") == DOC
    assert s.get_doc_patch(dp["id"])["status"] == "applied"
    ev = s.audit_rows(1)[-1]
    assert ev["event"] == "doc.patch_applied" and ev["actor"] == "김담당" and ev["detail"]["changes"]
    with pytest.raises(ValueError):
        apply_doc_patch(s, dp["id"], "김담당", upd)                                              # 재적용 불가


def test_changed_doc_blocks_apply_and_dismiss(env):
    s, repo, doc, sha = env
    handle_push(s, None, push_payload(sha))
    dp = s.list_doc_patches("pending")[0]
    doc.write_text(DOC + "\n추가 문단\n", encoding="utf-8")
    with pytest.raises(ValueError, match="바뀌었습니다"):
        apply_doc_patch(s, dp["id"], "김담당", [c["id"] for c in dp["changes"] if c["action"] == "update"])
    with pytest.raises(ValueError):
        dismiss_doc_patch(s, dp["id"], "김담당", "")
    dismiss_doc_patch(s, dp["id"], "김담당", "수동 관리 문서")
    assert s.get_doc_patch(dp["id"])["status"] == "dismissed"


@pytest.fixture
def server(env):
    s, *_ = env
    app = App(s, None, sync_mode=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", env
    srv.shutdown()


def post(url, data, headers=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers={"content-type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_webhook_endpoint_auth_and_flow(server, monkeypatch):
    base, (s, repo, doc, sha) = server
    monkeypatch.delenv("SSOT_WEBHOOK_SECRET", raising=False)
    assert post(base + "/api/hooks/gitlab", push_payload(sha))[0] == 503
    monkeypatch.setenv("SSOT_WEBHOOK_SECRET", "s3cret")
    assert post(base + "/api/hooks/gitlab", push_payload(sha))[0] == 401
    assert post(base + "/api/hooks/gitlab", push_payload(sha), {"X-Gitlab-Token": "wrong"})[0] == 401
    assert post(base + "/api/hooks/gitlab", push_payload(sha), {"X-Gitlab-Token": "s3cret"})[0] == 200
    with urllib.request.urlopen(base + "/api/docs") as r:
        items = json.loads(r.read())["items"]
    assert items and all("op" not in c for c in items[0]["changes"])
    ids = [c["id"] for c in items[0]["changes"] if c["action"] == "update"]
    code, out = post(f"{base}/api/docs/{items[0]['id']}/apply", {"actor": "김담당", "accepted": ids})
    assert code == 200 and out["output"].endswith("정의서.updated.md")


def test_manual_scan_endpoint(server):
    base, (s, repo, doc, sha) = server
    code, out = post(base + "/api/docs/scan", {"project": "acme/user", "actor": "김담당", "use_llm": False})
    assert code == 200 and out["created"] == 1
    assert post(base + "/api/docs/scan", {"project": "acme/user", "sha": "zz"})[0] == 400
