import json
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

from ssot import gitops  # noqa: E402
from ssot.gitlab import PublishError, create_mr, publish  # noqa: E402
from ssot.impact import analyze_sr, confirm_selection  # noqa: E402
from ssot.llm import LLM  # noqa: E402
from ssot.patch import apply_edits, decide, generate_patch, make_diff  # noqa: E402
from ssot.store import Store  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "spring"
CTRL = "src/main/java/com/acme/user/UserController.java"

OLD = '''    public List<UserDto> list(@RequestParam(defaultValue = "ACTIVE") String status, HttpServletRequest req) {
        return userService.list(status);
    }'''
NEW = '''    public List<UserDto> list(@RequestParam(defaultValue = "ACTIVE") String status,
                              @RequestParam(required = false) String from, HttpServletRequest req) {
        return userService.list(status);
    }'''


class EditLLM(LLM):
    model = "fake"

    def __init__(self, edits, explanation="from 파라미터 추가"):
        self.edits, self.explanation, self.calls = edits, explanation, []

    def complete(self, system, user, max_tokens=2000, json_mode=False):
        self.calls.append(user)
        if "[요청 제목]" not in user:
            return json.dumps({"keywords": ["User", "list"]})
        return json.dumps({"explanation": self.explanation, "edits": self.edits})


def git(root, *a):
    return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    shutil.copytree(FIXTURE, r)
    git(r, "init", "-q", "-b", "main")
    git(r, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return r


@pytest.fixture
def ready(tmp_path, repo):
    """영향 분석까지 끝나고 대상 파일이 확정된 SR."""
    s = Store(tmp_path / "p.db")
    s.upsert_project("acme/user", str(repo), "http://127.0.0.1:1/acme/user", "main", token_env="TEST_GL_TOKEN")
    sr = s.create_sr("사용자 목록에 from 필터 추가", "GET /api/users 에 from 파라미터", "이요청", "acme/user")
    llm = EditLLM([{"old": OLD, "new": NEW}])
    analyze_sr(s, llm, sr)
    confirm_selection(s, sr, "김담당", [CTRL])
    return s, sr, repo, llm


def test_apply_edits_exact_whitespace_tolerant_and_problems():
    src = "a\n    foo();\n    bar();\nz\n"
    out, pr = apply_edits(src, [{"old": "foo();\nbar();", "new": "foo();\nbaz();"}])   # 들여쓰기 달라도 찾아 보정
    assert out == "a\n    foo();\n    baz();\nz\n" and not pr
    _, pr = apply_edits(src, [{"old": "nothing here", "new": "x"}, {"old": "", "new": "x"}])
    assert len(pr) == 2
    _, pr = apply_edits("x\nx\n", [{"old": "x", "new": "y"}])
    assert "2곳" in pr[0]                                  # 모호하면 적용하지 않는다
    crlf, _ = apply_edits("a\r\nb\r\n", [{"old": "b", "new": "c"}])
    assert crlf == "a\r\nc\r\n"                            # CRLF 유지


def test_make_diff_marks_missing_newline():
    d = make_diff("f.txt", "a\nb", "a\nc")
    assert "\\ No newline at end of file" in d and d.startswith("--- a/f.txt")


def test_generate_validates_with_git_apply(ready):
    s, sr, repo, llm = ready
    pid = generate_patch(s, llm, sr)
    p = s.get_patch(pid)
    assert p["status"] == "proposed" and p["stats"]["files"] == [CTRL] and p["stats"]["additions"] == 2
    assert "from 파라미터" in p["explanation"] and s.get_sr(sr)["status"] == "patch_ready"
    assert "@RequestParam(required = false) String from" in p["diff"]
    assert git(repo, "status", "--porcelain") == ""        # 사용자 작업 폴더는 그대로
    assert "patch.generated" in [a["event"] for a in s.audit_rows(sr)]


def test_generate_without_valid_edit_fails_cleanly(ready):
    s, sr, repo, _ = ready
    pid = generate_patch(s, EditLLM([{"old": "존재하지 않는 코드", "new": "x"}]), sr)
    p = s.get_patch(pid)
    assert p["status"] == "failed" and p["warnings"] and s.get_sr(sr)["status"] == "patch_failed"


def test_approve_commits_on_new_branch_without_touching_workdir(ready):
    s, sr, repo, llm = ready
    pid = generate_patch(s, llm, sr)
    with pytest.raises(ValueError, match="본인"):
        decide(s, sr, pid, "이요청", "approve")
    assert decide(s, sr, pid, "김담당", "approve", "확인") == "approved"
    p = s.get_patch(pid)
    assert p["status"] == "committed" and p["branch"].startswith("sr/") and len(p["commit_sha"]) == 40
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main" and git(repo, "status", "--porcelain") == ""
    assert "String from" in git(repo, "show", f"{p['branch']}:{CTRL}")
    assert "String from" not in git(repo, "show", f"main:{CTRL}")
    log = git(repo, "log", "-1", "--format=%an|%cn|%B", p["branch"])
    assert log.startswith("김담당|SSSOT|[SR-") and "Approved-by: 김담당" in log
    ev = s.audit_rows(sr)[-1]
    assert ev["event"] == "patch.approved" and ev["actor"] == "김담당" and ev["detail"]["commit"] == p["commit_sha"]
    assert s.get_sr(sr)["status"] == "committed"
    with pytest.raises(ValueError):
        decide(s, sr, pid, "김담당", "approve")            # 두 번 승인 불가


def test_self_approve_allowed_by_flag(ready):
    s, sr, repo, llm = ready
    pid = generate_patch(s, llm, sr)
    assert decide(s, sr, pid, "이요청", "approve", allow_self_approve=True) == "approved"


def test_revise_creates_new_version_with_feedback_and_reject_needs_reason(ready):
    s, sr, repo, llm = ready
    pid = generate_patch(s, llm, sr)
    with pytest.raises(ValueError):
        decide(s, sr, pid, "김담당", "revise", "")
    assert decide(s, sr, pid, "김담당", "revise", "to 파라미터도 추가") == "revise"
    assert s.get_patch(pid)["status"] == "superseded"
    pid2 = generate_patch(s, llm, sr, "to 파라미터도 추가")
    assert s.get_patch(pid2)["version"] == 2 and "to 파라미터도 추가" in llm.calls[-1] and "[이전 수정안]" in llm.calls[-1]
    with pytest.raises(ValueError):
        decide(s, sr, pid, "김담당", "reject", "x")        # 옛 버전은 처리 불가
    with pytest.raises(ValueError):
        decide(s, sr, pid2, "김담당", "reject", "")
    assert decide(s, sr, pid2, "김담당", "reject", "범위 밖") == "rejected"
    assert s.get_sr(sr)["status"] == "rejected" and git(repo, "branch", "--list", "sr/*") == ""


# ---- GitLab ----
class _GL(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _GL.seen.append((self.path, self.headers.get("PRIVATE-TOKEN"), body))
        code, out = (200, {"iid": 87, "web_url": "http://gl/acme/user/-/merge_requests/87"}) \
            if self.headers.get("PRIVATE-TOKEN") == "good" else (401, {"message": "401 Unauthorized good-not-leaked"})
        data = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def gitlab(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _GL)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _GL.seen = []
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_create_mr_and_errors(gitlab):
    mr = create_mr(gitlab + "/acme/user.git", "good", "sr/1-x", "main", "t", "d")
    assert mr["iid"] == 87
    path, token, body = _GL.seen[-1]
    assert path == "/api/v4/projects/acme%2Fuser/merge_requests" and body["source_branch"] == "sr/1-x"
    with pytest.raises(PublishError, match="토큰이 올바르지"):
        create_mr(gitlab + "/acme/user", "bad", "a", "main", "t", "d")
    with pytest.raises(PublishError):
        create_mr("https://host", "x", "a", "b", "t", "d")


def test_publish_pushes_and_creates_mr(ready, gitlab, tmp_path, monkeypatch):
    s, sr, repo, llm = ready
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    git(repo, "remote", "add", "origin", str(bare))
    s.upsert_project("acme/user", str(repo), gitlab + "/acme/user", "main", token_env="TEST_GL_TOKEN")
    pid = generate_patch(s, llm, sr)
    decide(s, sr, pid, "김담당", "approve")
    with pytest.raises(PublishError, match="환경변수"):      # 토큰 없으면 로컬 커밋 상태로 남는다
        publish(s, sr, "김담당")
    assert s.get_sr(sr)["status"] == "committed"
    monkeypatch.setenv("TEST_GL_TOKEN", "good")
    url = publish(s, sr, "김담당")
    assert url.endswith("/merge_requests/87") and s.get_sr(sr)["status"] == "mr_created"
    assert git(bare, "branch", "--list", "sr/*")                 # 실제로 push 됨
    assert publish(s, sr, "김담당") == url                       # 재호출은 중복 생성하지 않음
    assert len([x for x in _GL.seen]) == 1
    assert s.audit_rows(sr)[-1]["event"] == "gitlab.mr_created"


def test_publish_failure_does_not_leak_token(ready, gitlab, tmp_path, monkeypatch):
    s, sr, repo, llm = ready
    git(repo, "remote", "add", "origin", str(tmp_path / "nowhere.git"))
    pid = generate_patch(s, llm, sr)
    decide(s, sr, pid, "김담당", "approve")
    monkeypatch.setenv("TEST_GL_TOKEN", "super-secret-token")
    with pytest.raises(PublishError) as e:
        publish(s, sr, "김담당")
    assert "super-secret-token" not in str(e.value)
    assert "super-secret-token" not in json.dumps(s.audit_rows(sr), ensure_ascii=False)
    assert gitops.auth_env("") == {}
