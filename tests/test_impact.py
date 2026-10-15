import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

from ssot.impact import analyze_sr, build_chunks, confirm_selection, tokens  # noqa: E402
from ssot.llm import LLM  # noqa: E402
from ssot.store import Store  # noqa: E402

SPRING = Path(__file__).parent / "fixtures" / "spring"


class KwLLM(LLM):
    model = "fake"
    embed_model = "fake-embed"

    def __init__(self, kws, embed=False, embed_fail=False):
        self.kws, self.embed_calls = kws, 0
        self._embed, self._fail = embed, embed_fail
        if not embed:
            self.embed = None  # type: ignore[assignment]
            del self.embed

    def complete(self, system, user, max_tokens=2000, json_mode=False):
        return json.dumps({"keywords": self.kws})


class EmbLLM(KwLLM):
    def __init__(self, kws, fail=False):
        super().__init__(kws, embed=True)
        self.fail, self.texts = fail, 0

    def embed(self, texts):
        if self.fail:
            raise RuntimeError("model not found")
        self.texts += len(texts)
        out = []
        for t in texts:   # 단어 포함 여부로 만든 가짜 벡터 (user/list/register/config 축)
            tl = t.lower()
            out.append([float("user" in tl), float("list" in tl), float("register" in tl), 0.1])
        return out


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "i.db")
    s.upsert_project("acme/user", str(SPRING), "https://gitlab.example.com/acme/user", "main")
    return s


def mk(store, title, body, project="acme/user"):
    return store.create_sr(title, body, "이요청", project)


def test_tokens_split_camel_and_korean():
    t = tokens("UserController listUsers 회원목록")
    assert {"user", "controller", "list", "users", "회원목록", "회원", "원목", "목록"} <= set(t)


def test_chunks_cover_methods():
    ch = build_chunks(SPRING)
    names = {(c.cls, c.method) for c in ch}
    assert ("UserController", "list") in names or any(c.cls == "UserController" and c.method for c in ch)
    assert not any("test" in c.file.split("/") for c in ch)


def test_keyword_search_ranks_relevant_file_first(store):
    sr = mk(store, "사용자 목록 조회", "GET /api/users 목록 조회 API를 수정해 주세요")
    assert analyze_sr(store, KwLLM(["User", "list", "users"]), sr) == "impact_ready"
    imp = store.get_impact(sr)
    top = imp["candidates"][0]["file"]
    assert "User" in top and not imp["vector_used"]
    assert imp["candidates"][0]["chunks"][0]["terms"]          # 근거 용어 제공
    assert imp["selected"]                                      # 상위 후보는 미리 선택
    ev = [a["event"] for a in store.audit_rows(sr)]
    assert "impact.keywords" in ev and "impact.analyzed" in ev


def test_vector_search_used_and_cached(store):
    sr = mk(store, "사용자 등록", "register user")
    llm = EmbLLM(["register"])
    analyze_sr(store, llm, sr)
    assert store.get_impact(sr)["vector_used"]
    first = llm.texts
    sr2 = mk(store, "사용자 목록", "list users")
    analyze_sr(store, llm, sr2)
    assert llm.texts - first == 1          # 청크는 캐시, 질의 임베딩 1건만 추가


def test_embedding_failure_falls_back_to_keywords(store):
    sr = mk(store, "사용자 목록", "list users")
    assert analyze_sr(store, EmbLLM(["User"], fail=True), sr) == "impact_ready"
    imp = store.get_impact(sr)
    assert not imp["vector_used"] and imp["candidates"]
    assert "벡터 검색 생략" in store.get_sr(sr)["reason"]


def test_unregistered_or_missing_path_is_error(store):
    sr = mk(store, "x", "y", project="nope")
    assert analyze_sr(store, KwLLM([]), sr) == "impact_error"
    sr2 = mk(store, "x", "y", project="")
    assert analyze_sr(store, KwLLM([]), sr2) == "impact_error"
    store.upsert_project("gone", str(SPRING / "nope"))
    sr3 = mk(store, "x", "y", project="gone")
    assert analyze_sr(store, KwLLM([]), sr3) == "impact_error"
    assert "찾을 수 없습니다" in store.get_sr(sr3)["reason"]


def test_confirm_selection_validates_paths(store):
    sr = mk(store, "사용자 목록", "list users")
    analyze_sr(store, KwLLM(["User"]), sr)
    f = store.get_impact(sr)["candidates"][0]["file"]
    confirm_selection(store, sr, "김담당", [f])
    assert store.get_sr(sr)["status"] == "impact_confirmed" and store.get_impact(sr)["selected"] == [f]
    assert store.audit_rows(sr)[-1]["actor"] == "김담당"
    for bad in (["../../etc/passwd"], ["no/such/File.java"], []):
        with pytest.raises(ValueError):
            confirm_selection(store, sr, "김담당", bad)
    with pytest.raises(ValueError):
        confirm_selection(store, sr, "", [f])


def test_project_validation(store):
    with pytest.raises(ValueError):
        store.upsert_project("p", "/x", token_env="glpat-secret-value")   # 변수 이름이 아닌 토큰 값 방지
    with pytest.raises(ValueError):
        store.upsert_project("p", "/x", gitlab_url="ftp://x")
    with pytest.raises(ValueError):
        store.upsert_project("", "/x")
    p = store.upsert_project("p", '"/x y"', doc_paths=[" a.md ", ""])
    assert p["local_path"] == "/x y" and p["doc_paths"] == ["a.md"]
