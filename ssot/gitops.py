"""로컬 Git 작업 헬퍼. 사용자의 작업 폴더를 건드리지 않도록 항상 임시 worktree 에서 작업한다."""
from __future__ import annotations

import base64
import contextlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


class GitError(RuntimeError):
    pass


def scrub(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def run(root: Path | str, *args: str, input: bytes | None = None, env: dict | None = None,
        secrets: list[str] | None = None, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    e = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
    try:
        r = subprocess.run(["git", "-C", str(root), *args], input=input, capture_output=True, env=e, timeout=timeout)
    except FileNotFoundError as ex:
        raise GitError("git 실행 파일을 찾을 수 없습니다.") from ex
    except subprocess.TimeoutExpired as ex:
        raise GitError(f"git {args[0]} 시간 초과") from ex
    if check and r.returncode != 0:
        msg = (r.stderr or r.stdout).decode("utf-8", "replace").strip()
        raise GitError(scrub(f"git {args[0]} 실패: {msg}", secrets or [])[:600])
    return r


def is_repo(root: Path) -> bool:
    return root.is_dir() and run(root, "rev-parse", "--is-inside-work-tree", check=False).returncode == 0


@contextlib.contextmanager
def worktree(root: Path, ref: str, new_branch: str | None = None):
    """root 저장소의 ref 를 임시 폴더에 체크아웃한다. new_branch 를 주면 그 이름의 새 브랜치로 만든다(블록 종료 후 브랜치는 남는다)."""
    if run(root, "rev-parse", "--verify", "--quiet", ref, check=False).returncode != 0:
        raise GitError(f"기본 브랜치를 찾을 수 없습니다: {ref}")
    tmp = Path(tempfile.mkdtemp(prefix="ssot_wt_"))
    wt = tmp / "wt"
    try:
        args = ["worktree", "add", "-q"]
        args += ["-b", new_branch, str(wt), ref] if new_branch else ["--detach", str(wt), ref]
        run(root, *args)
        yield wt
    finally:
        run(root, "worktree", "remove", "--force", str(wt), check=False)
        shutil.rmtree(tmp, ignore_errors=True)
        run(root, "worktree", "prune", check=False)


def slugify(title: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:limit].strip("-")
    return s or "change"


def unique_branch(root: Path, base: str) -> str:
    name, n = base, 1
    while run(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False).returncode == 0:
        n += 1
        name = f"{base}-{n}"
    return name


def auth_env(token: str) -> dict:
    """토큰을 명령행 인자가 아닌 환경변수(GIT_CONFIG_*)로 넘겨 프로세스 목록에 노출되지 않게 한다."""
    if not token:
        return {}
    b = base64.b64encode(f"oauth2:{token}".encode()).decode()
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {b}"}
