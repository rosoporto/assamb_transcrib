"""Тесты платного тракта, команд и скачивания — без сети (всё на подменах)."""
import sys
import types
from types import SimpleNamespace as N
from typing import ClassVar

import httpx
import pytest
import typer

import main


# ---------- загрузка ----------

def _fake_response(status: int, payload: dict | None = None):
    response = httpx.Response(status, json=payload or {})
    return response


def test_upload_file_sends_content_length(tmp_path, monkeypatch):
    import assemblyai as aai

    aai.settings.api_key = "test-key"
    media = tmp_path / "a.mp3"
    media.write_bytes(b"0123456789")
    seen = {}

    def fake_post(url, headers=None, content=None, timeout=None):
        seen["url"] = url
        seen["headers"] = dict(headers)
        seen["body"] = b"".join(content)
        request = httpx.Request("POST", url)
        return httpx.Response(200, json={"upload_url": "https://cdn/upload/1"}, request=request)

    monkeypatch.setattr(httpx, "post", fake_post)
    assert main.upload_file(media) == "https://cdn/upload/1"
    assert seen["url"].endswith("/v2/upload")
    assert seen["headers"]["content-length"] == "10"
    assert seen["body"] == b"0123456789"


def test_upload_file_reports_bad_key(tmp_path, monkeypatch):
    import assemblyai as aai

    aai.settings.api_key = "test-key"
    media = tmp_path / "a.mp3"
    media.write_bytes(b"0")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(401, json={}))
    with pytest.raises(RuntimeError, match="invalid api key"):
        main.upload_file(media)


def test_upload_retries_transient_error(tmp_path, monkeypatch):
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("timeout")
        return "https://cdn/upload/2"

    monkeypatch.setattr(main, "upload_file", flaky)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(main, "log", lambda m: None)
    assert main.upload_with_retries(tmp_path / "a.mp3") == "https://cdn/upload/2"
    assert calls["n"] == 2


def test_upload_does_not_retry_413(tmp_path, monkeypatch):
    calls = {"n": 0}
    request = httpx.Request("POST", "https://api.assemblyai.com/v2/upload")

    def too_big(path):
        calls["n"] += 1
        raise httpx.HTTPStatusError("413", request=request, response=httpx.Response(413))

    monkeypatch.setattr(main, "upload_file", too_big)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(main, "log", lambda m: None)
    with pytest.raises(httpx.HTTPStatusError):
        main.upload_with_retries(tmp_path / "a.mp3")
    assert calls["n"] == 1


def test_upload_exits_on_auth_error(tmp_path, monkeypatch):
    def bad_key(path):
        raise RuntimeError("invalid api key")

    monkeypatch.setattr(main, "upload_file", bad_key)
    monkeypatch.setattr(main, "log", lambda m: None)
    with pytest.raises(typer.Exit) as exc:
        main.upload_with_retries(tmp_path / "a.mp3")
    assert exc.value.exit_code == 2


# ---------- распознавание ----------

def _fake_transcript(**overrides):
    data = {
        "id": "t_1",
        "status": N(value="completed"),
        "error": None,
        "words": [N(text="Привет", start=0, end=400, speaker=None),
                  N(text="мир.", start=400, end=900, speaker=None)],
        "utterances": [],
        "text": "Привет мир.",
        "audio_duration": 120,
        "language_code": "ru",
        "speech_model_used": "universal-3-5-pro",
    }
    data.update(overrides)
    transcript = N(**data)
    transcript.wait_for_completion = lambda poll_timeout=None: transcript
    return transcript


def _fake_transcriber(transcript):
    return N(submit=lambda url, config=None: transcript)


def test_transcribe_one_writes_text(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "upload_with_retries", lambda media: "https://cdn/upload/3")
    monkeypatch.setattr(main, "log", lambda m: None)
    out = tmp_path / "a.txt"
    seconds = main.transcribe_one(_fake_transcriber(_fake_transcript()), None,
                                  tmp_path / "a.mp3", out, False, 0.21)
    assert seconds == 120.0
    assert out.read_text(encoding="utf-8") == "[00:00:00] Привет мир.\n"


def test_transcribe_one_reports_api_error(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "upload_with_retries", lambda media: "url")
    monkeypatch.setattr(main, "log", lambda m: None)
    transcript = _fake_transcript(status=N(value="error"), error="bad audio")
    with pytest.raises(RuntimeError, match="bad audio"):
        main.transcribe_one(_fake_transcriber(transcript), None, tmp_path / "a.mp3",
                            tmp_path / "a.txt", False, 0.21)


def test_transcribe_one_reports_empty_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "upload_with_retries", lambda media: "url")
    monkeypatch.setattr(main, "log", lambda m: None)
    transcript = _fake_transcript(words=[], utterances=[], text="")
    with pytest.raises(RuntimeError, match="пустую"):
        main.transcribe_one(_fake_transcriber(transcript), None, tmp_path / "a.mp3",
                            tmp_path / "a.txt", False, 0.21)


def test_transcribe_one_write_failure_mentions_id(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "upload_with_retries", lambda media: "url")
    monkeypatch.setattr(main, "log", lambda m: None)
    blocked = tmp_path / "blocked.txt"
    blocked.mkdir()  # на месте файла — каталог, замена не пройдёт
    with pytest.raises(RuntimeError, match="t_1"):
        main.transcribe_one(_fake_transcriber(_fake_transcript()), None, tmp_path / "a.mp3",
                            blocked, False, 0.21)


# ---------- команда file ----------

def test_run_file_processes_and_summarizes(tmp_path, monkeypatch, capsys):
    media = tmp_path / "a.mp3"
    media.write_bytes(b"0")
    out = tmp_path / "out"
    monkeypatch.setattr(main, "get_api_key", lambda: "k")
    monkeypatch.setattr(main, "make_transcriber", lambda *a, **k: (object(), object()))
    monkeypatch.setattr(main, "transcribe_one", lambda *a, **k: 60.0)

    with pytest.raises(typer.Exit) as exc:
        main.run_file(media, out, "auto", False, None, False, False)
    printed = capsys.readouterr().out
    assert exc.value.exit_code == 0
    assert "Успешно: 1, ошибок: 0" in printed
    assert "1.0 мин" in printed


def test_run_file_reports_failures(tmp_path, monkeypatch, capsys):
    media = tmp_path / "a.mp3"
    media.write_bytes(b"0")
    monkeypatch.setattr(main, "get_api_key", lambda: "k")
    monkeypatch.setattr(main, "make_transcriber", lambda *a, **k: (object(), object()))

    def boom(*args, **kwargs):
        raise RuntimeError("сеть легла")

    monkeypatch.setattr(main, "transcribe_one", boom)
    with pytest.raises(typer.Exit) as exc:
        main.run_file(media, tmp_path / "out", "auto", False, None, False, False)
    assert exc.value.exit_code == 1
    assert "ошибок: 1" in capsys.readouterr().out


# ---------- YouTube ----------

class _FakeYDL:
    info: ClassVar[dict] = {"title": "Ролик [тест]", "duration": 785, "is_live": False,
                            "webpage_url": "https://www.youtube.com/watch?v=x"}
    downloaded: ClassVar[list] = []

    def __init__(self, params=None):
        self.params = params or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        return dict(self.info)

    def download(self, urls):
        if getattr(self, "fail", False):
            raise RuntimeError("network reset")
        _FakeYDL.downloaded.append(urls)


def _install_fake_ytdlp(monkeypatch, fail=False):
    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = type("YoutubeDL", (_FakeYDL,), {"fail": fail})
    monkeypatch.setitem(sys.modules, "yt_dlp", module)


def test_run_youtube_dry_run(tmp_path, monkeypatch, capsys):
    _install_fake_ytdlp(monkeypatch)
    monkeypatch.setattr(main, "get_api_key", lambda: pytest.fail("ключ в dry-run не нужен"))
    with pytest.raises(typer.Exit) as exc:
        main.run_youtube("https://youtu.be/x", tmp_path / "out", "auto", False, None, False, False, dry_run=True)
    printed = capsys.readouterr().out
    assert exc.value.exit_code == 0
    assert "Проверка без оплаты" in printed
    assert "$0.0458" in printed
    assert not (tmp_path / "out").exists()


def test_run_youtube_download_error_is_friendly(tmp_path, monkeypatch, capsys):
    _install_fake_ytdlp(monkeypatch, fail=True)
    monkeypatch.setattr(main, "get_api_key", lambda: "k")
    monkeypatch.setattr(main, "make_transcriber", lambda *a, **k: (object(), object()))

    with pytest.raises(typer.Exit) as exc:
        main.run_youtube("https://youtu.be/x", tmp_path / "out", "auto", False, None, False, False)
    printed = capsys.readouterr().out
    assert exc.value.exit_code == 1
    assert "Не удалось скачать звук" in printed
    assert "ERROR" in (tmp_path / "out" / "transcription.log").read_text(encoding="utf-8")


def test_download_audio_ignores_part_files(tmp_path, monkeypatch):
    _install_fake_ytdlp(monkeypatch)
    (tmp_path / "audio.m4a.part").write_bytes(b"x" * 100)
    (tmp_path / "audio.m4a").write_bytes(b"y" * 10)
    audio = main.download_audio("https://youtu.be/x", {}, tmp_path)
    assert audio.name == "audio.m4a"
