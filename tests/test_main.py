import sys
from pathlib import Path
from types import SimpleNamespace as N

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main  # noqa: E402


def w(text, start, end, speaker=None):
    return N(text=text, start=start, end=end, speaker=speaker)


def test_timecode():
    assert main.timecode(0) == "00:00:00"
    assert main.timecode(3_723_000) == "01:02:03"
    assert main.timecode(None) == "00:00:00"


def test_paragraph_split_on_pause():
    t = N(words=[w("Привет", 0, 500), w("мир.", 500, 900), w("Новая", 3000, 3400)], text="")
    assert main.format_transcript(t, False) == "[00:00:00] Привет мир.\n\n[00:00:03] Новая"


def test_speaker_labels_split_paragraph():
    t = N(words=[w("Да.", 0, 300, "A"), w("Нет.", 400, 700, "B")], text="")
    out = main.format_transcript(t, True)
    assert "[00:00:00] Спикер A: Да." in out and "[00:00:00] Спикер B: Нет." in out


def test_fallback_to_plain_text_when_no_words():
    assert main.format_transcript(N(words=[], utterances=[], text=" текст "), False) == "текст"


def test_cost_estimate(monkeypatch):
    monkeypatch.setenv("ASSEMBLYAI_PRICE_PER_HOUR", "0.36")
    assert abs(main.cost_of(3600) - 0.36) < 1e-9
    monkeypatch.setenv("ASSEMBLYAI_PRICE_PER_HOUR", "мусор")
    assert main.price_per_hour() == main.DEFAULT_PRICE_PER_HOUR


def test_safe_name_strips_emoji_and_forbidden_chars():
    assert main.safe_name('🔥 a/b:c?"d') == "a b c d"
    assert main.safe_name("???", "fallback") == "fallback"


def test_scrub_hides_key(monkeypatch):
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "SECRET123")
    assert "SECRET123" not in main.scrub("fail with SECRET123 inside")
    assert "[" not in main.err("a [b] c").replace("\\[", "")


def test_write_atomic(tmp_path):
    p = tmp_path / "x.txt"
    main.write_atomic(p, "abc  \n\n")
    assert p.read_text(encoding="utf-8") == "abc\n"
    assert not list(tmp_path.glob("*.part"))


def test_output_names_disambiguate_same_stem():
    names = main.output_names([Path("a.mp3"), Path("A.MP4"), Path("b.wav")])
    assert names == {Path("a.mp3"): "a_mp3.txt", Path("A.MP4"): "A_mp4.txt", Path("b.wav"): "b.txt"}


def test_log_rotates_when_too_big(tmp_path, monkeypatch):
    log_file = tmp_path / "transcription.log"
    log_file.write_text("x" * 50, encoding="utf-8")
    monkeypatch.setattr(main, "LOG_FILE", log_file)
    monkeypatch.setattr(main, "LOG_MAX_BYTES", 10)
    main.log("новая запись")
    assert (tmp_path / "transcription.log.1").read_text(encoding="utf-8") == "x" * 50
    assert "новая запись" in log_file.read_text(encoding="utf-8")
