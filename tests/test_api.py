"""Web API 测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from audiobook_forge.config import load_settings
from audiobook_forge.web.app import create_app


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ABF_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABF_TTS_PROVIDER", "mock")
    monkeypatch.setenv("ABF_LLM_ENABLED", "false")
    settings = load_settings()
    settings.ensure_dirs()
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_status(client: TestClient) -> None:
    res = client.get("/api/status")
    assert res.status_code == 200
    data = res.json()
    assert data["provider"] == "mock"
    assert data["providers"]["mock"]["ready"] is True


def test_index_served(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "Audiobook Forge" in res.text


def test_upload_and_detail(client: TestClient, sample_txt: Path) -> None:
    with sample_txt.open("rb") as fh:
        res = client.post("/api/books", files={"file": ("sample.txt", fh, "text/plain")})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["chapters"] == 2

    book_id = data["book_id"]
    detail = client.get(f"/api/books/{book_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["book"]["title"] == "测试小说"
    assert len(body["chapters"]) == 2
    assert body["cast"] is None


def test_upload_rejects_bad_extension(client: TestClient) -> None:
    res = client.post(
        "/api/books", files={"file": ("x.pdf", b"%PDF-1.4", "application/pdf")}
    )
    assert res.status_code == 400


def test_voices(client: TestClient) -> None:
    res = client.get("/api/voices?provider=mock")
    assert res.status_code == 200
    assert res.json()["voices"]


def test_preview_returns_audio(client: TestClient) -> None:
    res = client.post(
        "/api/preview",
        json={"text": "今天天气不错。", "voice": "mock-female-a"},
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("audio/")
    assert len(res.content) > 1000


def test_full_flow_via_api(client: TestClient, sample_txt: Path) -> None:
    with sample_txt.open("rb") as fh:
        book_id = client.post(
            "/api/books", files={"file": ("sample.txt", fh, "text/plain")}
        ).json()["book_id"]

    res = client.post(f"/api/books/{book_id}/run", json={"skip_llm": True, "make_m4b": False})
    assert res.status_code == 200
    job_id = res.json()["job_id"]

    # 轮询直到结束
    import time

    for _ in range(120):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"done", "failed", "cancelled"}:
            break
        time.sleep(0.1)

    assert job["status"] == "done", job.get("message")

    download = client.get(f"/api/books/{book_id}/download?fmt=mp3")
    assert download.status_code == 200
    assert len(download.content) > 1000


def _wait(client: TestClient, job_id: str, timeout: float = 30.0) -> dict:
    import time

    deadline = time.monotonic() + timeout
    job: dict = {}
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"done", "failed", "cancelled"}:
            return job
        time.sleep(0.1)
    return job


def test_selective_chapter_synthesis_via_api(
    client: TestClient, sample_txt: Path
) -> None:
    with sample_txt.open("rb") as fh:
        book_id = client.post(
            "/api/books", files={"file": ("sample.txt", fh, "text/plain")}
        ).json()["book_id"]

    analyze = client.post(f"/api/books/{book_id}/analyze", json={"skip_llm": True})
    assert analyze.status_code == 200
    assert _wait(client, analyze.json()["job_id"])["status"] == "done"

    # 只合成第 2 章（index=1），此时还没有分段，接口应自动先规划
    res = client.post(f"/api/books/{book_id}/synthesize", json={"chapters": [1]})
    assert res.status_code == 200, res.text
    job = _wait(client, res.json()["job_id"])
    assert job["status"] == "done", job.get("message")

    detail = client.get(f"/api/books/{book_id}").json()
    chapters = {c["index"]: c for c in detail["chapters"]}
    assert chapters[1]["seg_total"] > 0
    assert chapters[1]["seg_done"] == chapters[1]["seg_total"]
    # 第 1 章一点都没合成
    assert chapters[0]["seg_done"] == 0


def test_synthesize_auto_analyzes_when_no_cast(client: TestClient, sample_txt: Path) -> None:
    """新书直接点「合成」时，应自动分析+分段，而不是报错让用户再点一次。"""
    with sample_txt.open("rb") as fh:
        book_id = client.post(
            "/api/books", files={"file": ("sample.txt", fh, "text/plain")}
        ).json()["book_id"]

    res = client.post(f"/api/books/{book_id}/synthesize", json={"chapters": [0]})
    assert res.status_code == 200
    job = _wait(client, res.json()["job_id"])
    assert job["status"] == "done", job.get("message")

    detail = client.get(f"/api/books/{book_id}").json()
    assert detail["cast"] is not None
    assert detail["chapters"][0]["seg_done"] == detail["chapters"][0]["seg_total"] > 0


def test_settings_roundtrip(client: TestClient, tmp_path: Path) -> None:
    res = client.get("/api/settings")
    assert res.status_code == 200
    body = res.json()
    assert body["tts"]["provider"] == "mock"
    assert {p["name"] for p in body["tts"]["providers"]} == {
        "mock",
        "mimo",
        "xfyun",
        "openai",
    }
    assert "api_key" not in body["llm"], "接口不能回传密钥"
    assert "api_key" not in body["tts"]["openai"], "接口不能回传密钥"

    upd = client.post(
        "/api/settings",
        json={
            "tts_provider": "mimo",
            "llm_enabled": True,
            "llm_base_url": "https://api.siliconflow.cn/v1",
            "llm_model": "deepseek-ai/DeepSeek-V3.2",
            "llm_api_key": "sk-test-secret",
        },
    )
    assert upd.status_code == 200, upd.text
    data = upd.json()
    assert data["tts"]["provider"] == "mimo"
    assert data["llm"]["model"] == "deepseek-ai/DeepSeek-V3.2"
    assert data["llm"]["has_key"] is True
    assert "sk-test-secret" not in upd.text

    runtime = tmp_path / "data" / "settings.json"
    assert runtime.is_file()
    import json as _json

    saved = _json.loads(runtime.read_text(encoding="utf-8"))
    assert saved["tts"]["provider"] == "mimo"
    assert saved["llm"]["api_key"] == "sk-test-secret"

    # 留空 key 不会清掉已保存的密钥
    again = client.post("/api/settings", json={"llm_api_key": ""})
    assert again.json()["llm"]["has_key"] is True


def test_settings_rejects_unknown_provider(client: TestClient) -> None:
    res = client.post("/api/settings", json={"tts_provider": "openai"})
    assert res.status_code == 200
    assert res.json()["tts"]["provider"] == "openai"

    bad = client.post("/api/settings", json={"tts_provider": "nope"})
    assert bad.status_code == 400


def test_upload_filename_becomes_title(client: TestClient, tmp_path: Path) -> None:
    """以前会拿 UUID 临时名当书名，现在必须用原文件名。"""
    path = tmp_path / "剑来.txt"
    path.write_text(
        "第一章 初入江湖\n\n少年背着剑走下山。\n\n第二章 风雪夜\n\n客栈里灯火通明。",
        encoding="utf-8",
    )
    with path.open("rb") as fh:
        res = client.post(
            "/api/books", files={"file": ("剑来.txt", fh, "text/plain")}
        )
    assert res.status_code == 200, res.text
    assert res.json()["title"] == "剑来"


def test_safe_stem_repairs_paths_and_escapes() -> None:
    from audiobook_forge.web.app import _safe_stem

    assert _safe_stem(r"C:\fake\dir\剑来.txt") == "剑来"
    assert _safe_stem("%E5%89%91%E6%9D%A5.txt") == "剑来"
    assert _safe_stem("../../etc/passwd.txt") == "passwd"
    assert _safe_stem("") == "book"


def test_discover_reports_error_instead_of_500(client: TestClient) -> None:
    res = client.get(
        "/api/discover?kind=llm&base_url=http://127.0.0.1:9/v1&api_key=x"
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False
    assert body["error"]


def test_runtime_overrides_beat_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from audiobook_forge.config import load_settings, save_runtime_overrides

    monkeypatch.setenv("ABF_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABF_TTS_PROVIDER", "mock")
    monkeypatch.setenv("ABF_LLM_ENABLED", "false")

    settings = load_settings()
    assert settings.tts.provider == "mock"

    settings.tts.provider = "mimo"
    settings.llm.enabled = True
    settings.llm.api_key = "sk-from-ui"
    settings.llm.model = "deepseek-ai/DeepSeek-V3.2"
    save_runtime_overrides(settings)

    reloaded = load_settings()
    assert reloaded.tts.provider == "mimo"
    assert reloaded.llm.enabled is True
    assert reloaded.llm.api_key == "sk-from-ui"
    assert reloaded.llm.model == "deepseek-ai/DeepSeek-V3.2"
