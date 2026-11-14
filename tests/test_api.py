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
