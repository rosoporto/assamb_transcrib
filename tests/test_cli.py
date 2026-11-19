"""Тесты нового поведения: единый вход, тарифы, ретраи, имена, журнал."""
import sys
from pathlib import Path, PurePosixPath

import pytest
import typer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main  # noqa: E402


# ---------- тарифы ----------

def test_price_includes_addons(monkeypatch):
    monkeypatch.delenv("ASSEMBLYAI_PRICE_PER_HOUR", raising=False)
    assert abs(main.price_per_hour() - 0.21) < 1e-9
    assert abs(main.price_per_hour(speakers=True) - 0.23) < 1e-9
    assert abs(main.price_per_hour(speakers=True, terms=True) - 0.28) < 1e-9
    assert abs(main.cost_of(3600, speakers=True) - 0.23) < 1e-9


def test_price_rejects_bad_env(monkeypatch):
    for value in ("-5", "1000", "0", "мусор"):
        monkeypatch.setenv("ASSEMBLYAI_PRICE_PER_HOUR", value)
        assert main.price_per_hour() == main.DEFAULT_PRICE_PER_HOUR


def test_fmt_cost_shows_breakdown(monkeypatch):
    monkeypatch.delenv("ASSEMBLYAI_PRICE_PER_HOUR", raising=False)
    text = main.fmt_cost(3600, speakers=True, terms=True)
    assert "$0.28" in text and "спикеры" in text and "термины" in text


# ---------- имена файлов ----------

def test_output_names_never_collide():
    names = main.output_names([Path("a.mp3"), Path("a.mp4"), Path("a_mp3.mp3")])
    assert sorted(names.values()) == ["a_mp3.txt", "a_mp3_2.txt", "a_mp4.txt"]
    assert len({n.casefold() for n in names.values()}) == 3


def test_output_names_case_insensitive_collision():
    # PurePosixPath: регистрозависимая ФС, где такие файлы могут лежать рядом
    names = main.output_names([PurePosixPath("a.mp3"), PurePosixPath("a.MP3")])
    assert len({n.casefold() for n in names.values()}) == 2


def test_safe_name_windows_reserved():
    assert main.safe_name("CON") == "_CON"
    assert main.safe_name("nul.txt") == "_nul.txt"
    assert main.safe_name("обычное имя") == "обычное имя"


# ---------- журнал ----------

def test_log_does_not_break_main_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "LOG_FILE", tmp_path)  # каталог вместо файла: запись невозможна
    main.log("это не должно бросить исключение")


def test_log_is_single_line(tmp_path, monkeypatch):
    log_file = tmp_path / "t.log"
    monkeypatch.setattr(main, "LOG_FILE", log_file)
    main.log("первая\nвторая\tтретья")
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and "\t" in lines[0]


def test_use_log_dir_moves_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "LOG_FILE", tmp_path / "old.log")
    main.use_log_dir(tmp_path)
    assert main.LOG_FILE == tmp_path / "transcription.log"


# ---------- ретраи ----------

def test_is_retryable_classifies_errors():
    import httpx

    req = httpx.Request("POST", "https://api.assemblyai.com/v2/upload")
    assert main.is_retryable(httpx.ConnectTimeout("timeout"))
    assert main.is_retryable(httpx.HTTPStatusError("503", request=req, response=httpx.Response(503)))
    assert not main.is_retryable(httpx.HTTPStatusError("413", request=req, response=httpx.Response(413)))
    assert not main.is_retryable(httpx.HTTPStatusError("401", request=req, response=httpx.Response(401)))

    class SDKError(Exception):
        status_code = 404

    assert not main.is_retryable(SDKError("failed to retrieve transcript"))
    assert main.is_retryable(RuntimeError("неизвестная ошибка"))


def test_wait_stops_immediately_on_auth_error(monkeypatch):
    calls = {"n": 0}

    class AuthError(Exception):
        status_code = 401

    class FakeTranscript:
        id = "t_1"

        def wait_for_completion(self, poll_timeout=None):
            calls["n"] += 1
            raise AuthError("unauthorized")

    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(main, "log", lambda m: None)
    with pytest.raises(AuthError):
        main.wait_with_retries(FakeTranscript(), "a.mp3")
    assert calls["n"] == 1


def test_wait_retries_on_transient_error(monkeypatch):
    calls = {"n": 0}

    class FakeTranscript:
        id = "t_2"

        def wait_for_completion(self, poll_timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("slow")
            return "done"

    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(main, "log", lambda m: None)
    assert main.wait_with_retries(FakeTranscript(), "a.mp3") == "done"
    assert calls["n"] == 3


def test_wait_passes_poll_timeout(monkeypatch):
    seen = {}

    class FakeTranscript:
        id = "t_3"

        def wait_for_completion(self, poll_timeout=None):
            seen["timeout"] = poll_timeout
            return "done"

    monkeypatch.setattr(main, "log", lambda m: None)
    main.wait_with_retries(FakeTranscript(), "a.mp3")
    assert seen["timeout"] == main.POLL_TIMEOUT


# ---------- конфигурация и разбор входа ----------

def test_make_transcriber_pins_model_and_language():
    _, cfg = main.make_transcriber("test-key", False, "ru")
    assert cfg.language_code == "ru"
    assert cfg.speech_models == ["universal-3-5-pro", "universal-2"]
    _, auto = main.make_transcriber("test-key", False, "auto")
    assert auto.language_detection is True
    _, terms = main.make_transcriber("test-key", True, "auto", "ChatGPT, Claude Code")
    assert terms.keyterms_prompt == ["ChatGPT", "Claude Code"]


def test_looks_like_url():
    assert main.looks_like_url("https://youtu.be/x")
    assert main.looks_like_url("www.youtube.com/watch?v=1")
    assert not main.looks_like_url("video.mp4")
    assert not main.looks_like_url(r"C:\видео\урок.mp4")


def test_prepare_argv_short_form():
    assert main.prepare_argv(["transcrib", "https://youtu.be/x"]) == ["transcrib", "run", "https://youtu.be/x"]
    assert main.prepare_argv(["transcrib", "-o", "out", "link"]) == ["transcrib", "run", "-o", "out", "link"]
    assert main.prepare_argv(["transcrib", "file", "x.mp4"]) == ["transcrib", "file", "x.mp4"]
    assert main.prepare_argv(["transcrib", "--help"]) == ["transcrib", "--help"]
    assert main.prepare_argv(["transcrib"]) == ["transcrib"]
    assert main.prepare_argv(["transcrib", "key"]) == ["transcrib", "key"]


def test_key_found_in_cwd_env(tmp_path, monkeypatch):
    monkeypatch.delenv("ASSEMBLYAI_API_KEY", raising=False)
    (tmp_path / ".env").write_text("ASSEMBLYAI_API_KEY=ключ_из_текущей_папки\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main, "BASE", tmp_path / "нет_такого_каталога")
    assert main.find_api_key() == "ключ_из_текущей_папки"


def test_placeholder_key_is_not_a_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ASSEMBLYAI_API_KEY", raising=False)
    (tmp_path / ".env").write_text("ASSEMBLYAI_API_KEY=PASTE_YOUR_KEY_HERE\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main, "BASE", tmp_path / "нет_такого_каталога")
    assert main.find_api_key() == ""


# ---------- --dry-run ----------

def test_dry_run_file_writes_nothing(tmp_path, monkeypatch):
    src = tmp_path / "a.mp3"
    src.write_bytes(b"0")
    out = tmp_path / "out"

    def boom():
        raise AssertionError("в dry-run ключ не нужен")

    monkeypatch.setattr(main, "get_api_key", boom)
    with pytest.raises(typer.Exit) as exc:
        main.run_file(src, out, "auto", False, None, False, False, dry_run=True)
    assert exc.value.exit_code == 0
    assert not out.exists()
    assert not list(tmp_path.glob("*.txt"))


def test_dry_run_marks_existing_and_new(tmp_path, capsys):
    (tmp_path / "new.mp3").write_bytes(b"0")
    (tmp_path / "done.mp3").write_bytes(b"0")
    out = tmp_path / "out"
    out.mkdir()
    (out / "done.txt").write_text("x", encoding="utf-8")

    with pytest.raises(typer.Exit):
        main.run_file(tmp_path, out, "auto", False, None, False, False, dry_run=True)
    printed = capsys.readouterr().out
    assert "готово, пропущу" in printed
    assert "будет обработано" in printed


def test_resolve_terms_priority(monkeypatch):
    monkeypatch.delenv("ASSEMBLYAI_TERMS", raising=False)
    monkeypatch.setattr(main, "_env_files", lambda: [])
    assert main.resolve_terms(None, False) == main.DEFAULT_TERMS
    assert main.resolve_terms("А,Б", False) == "А,Б"
    assert main.resolve_terms("А,Б", True) is None
    assert main.resolve_terms("  ", False) is None
    monkeypatch.setenv("ASSEMBLYAI_TERMS", "Х,У")
    assert main.resolve_terms(None, False) == "Х,У"
    assert main.resolve_terms("А", False) == "А"
    monkeypatch.setenv("ASSEMBLYAI_TERMS", "")
    assert main.resolve_terms(None, False) is None
