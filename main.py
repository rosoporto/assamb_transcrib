#!/usr/bin/env python3
"""CLI-транскрибатор на AssemblyAI (Typer + Rich).

Команды:
  file     — транскрибировать один файл или все файлы папки
  youtube  — транскрибировать одно YouTube-видео (аудио качается во временную папку и удаляется)
  key      — проверить API-ключ
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.markup import escape
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

BASE = Path(__file__).resolve().parent
DEFAULT_OUT = BASE / "ГОТОВЫЕ_ТЕКСТЫ"
LOG_FILE = BASE / "transcription.log"
LOG_MAX_BYTES = 2 * 1024 * 1024  # при превышении лог переезжает в transcription.log.1

SUPPORTED = {
    ".mp3", ".mp4", ".m4a", ".mov", ".mkv", ".avi", ".webm", ".wav",
    ".aac", ".flac", ".ogg", ".opus", ".mpeg", ".mpg",
}
NETWORK_TIMEOUT = 90.0  # таймаут одной сетевой операции; зависшая загрузка уходит в ретрай за ~1,5 мин
# Тариф за час аудио, USD. API стоимость не возвращает, поэтому это ОЦЕНКА.
# Сверьте с https://www.assemblyai.com/pricing и при необходимости задайте
# ASSEMBLYAI_PRICE_PER_HOUR в .env.
DEFAULT_PRICE_PER_HOUR = 0.15
UPLOAD_ATTEMPTS = 3
POLL_ATTEMPTS = 5
PAUSE_MS = 1800  # пауза между словами, после которой начинается новый абзац

app = typer.Typer(add_completion=False, help="Транскрибатор на AssemblyAI.")
console = Console()


# ---------- утилиты ----------

def scrub(value: Any) -> str:
    """Убрать API-ключ из любого текста (ошибки, логи)."""
    text = str(value)
    key = os.environ.get("ASSEMBLYAI_API_KEY", "").strip()
    return text.replace(key, "[ключ скрыт]") if key else text


def err(value: Any) -> str:
    """То же для консоли + экранирование rich-разметки."""
    return escape(scrub(value))


def log(message: str) -> None:
    message = scrub(message)
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        if LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            LOG_FILE.replace(LOG_FILE.with_name(LOG_FILE.name + ".1"))
    except OSError:
        pass
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(f"{stamp}\t{message}\n")


def price_per_hour() -> float:
    try:
        return float(os.environ.get("ASSEMBLYAI_PRICE_PER_HOUR", DEFAULT_PRICE_PER_HOUR))
    except ValueError:
        return DEFAULT_PRICE_PER_HOUR


def cost_of(seconds: float) -> float:
    return seconds / 3600 * price_per_hour()


def fmt_cost(seconds: float) -> str:
    return f"{seconds / 60:.1f} мин ≈ ${cost_of(seconds):.4f} (оценка, ${price_per_hour():g}/ч)"


def get_api_key() -> str:
    load_dotenv(BASE / ".env")
    key = os.environ.get("ASSEMBLYAI_API_KEY", "").strip()
    if not key or key == "PASTE_YOUR_KEY_HERE":
        console.print("[red]Ключ не найден.[/] Положите ASSEMBLYAI_API_KEY в .env рядом с main.py.")
        raise typer.Exit(2)
    return key


def make_transcriber(key: str, speakers: bool, language: str, terms: Optional[str] = None):
    import assemblyai as aai

    aai.settings.api_key = key
    aai.settings.http_timeout = NETWORK_TIMEOUT
    params: dict[str, Any] = {"speaker_labels": speakers}
    if language == "auto":
        params["language_detection"] = True
    else:
        params["language_code"] = language
    keyterms = [t.strip() for t in (terms or "").split(",") if t.strip()]
    if keyterms:
        params["keyterms_prompt"] = keyterms
    return aai.Transcriber(), aai.TranscriptionConfig(**params)


def timecode(ms: Any) -> str:
    s = max(0, int(ms or 0)) // 1000
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_transcript(transcript: Any, speakers: bool) -> str:
    """Абзацы с таймкодами [HH:MM:SS], по паузам и границам предложений."""
    words = [w for w in (getattr(transcript, "words", None) or []) if (w.text or "").strip()]
    if not words:
        blocks = []
        for u in getattr(transcript, "utterances", None) or []:
            text = (u.text or "").strip()
            if text:
                label = f"Спикер {u.speaker}: " if speakers and u.speaker is not None else ""
                blocks.append(f"[{timecode(u.start)}] {label}{text}")
        return "\n\n".join(blocks) or (getattr(transcript, "text", None) or "").strip()

    blocks: List[str] = []
    para: List[Any] = []

    def flush() -> None:
        if not para:
            return
        first = para[0]
        spk = getattr(first, "speaker", None)
        label = f"Спикер {spk}: " if speakers and spk is not None else ""
        blocks.append(f"[{timecode(first.start)}] {label}" + " ".join(w.text.strip() for w in para))
        para.clear()

    for w in words:
        if para:
            prev = para[-1]
            new_speaker = speakers and getattr(w, "speaker", None) != getattr(prev, "speaker", None)
            if w.start - prev.end >= PAUSE_MS or new_speaker:
                flush()
        para.append(w)
        sentence_end = w.text.rstrip('"»”’)]').endswith((".", "!", "?", "…"))
        long_enough = len(para) >= 40 or (w.end - para[0].start) >= 30000
        if (sentence_end and long_enough) or len(para) >= 100:
            flush()
    flush()
    return "\n\n".join(blocks)


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(text.rstrip() + "\n", encoding="utf-8")
    tmp.replace(path)


def safe_name(value: str, fallback: str = "youtube") -> str:
    value = re.sub(r"[\U00010000-\U0010ffff\u2600-\u27bf\ufe0f\u200d]", "", value)  # эмодзи
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value or fallback)[:180]


def is_auth_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(s in text for s in ("invalid api key", "unauthorized", "authentication"))


def upload_file(path: Path) -> str:
    """Загрузка напрямую через httpx с Content-Length.

    Встроенный upload_file SDK шлёт тело потоком без длины и на этой сети зависает
    по 'write operation timed out' (curl те же 15 МБ грузит за ~20 с).
    """
    import assemblyai as aai
    import httpx

    def chunks():
        with path.open("rb") as fh:
            while block := fh.read(1 << 20):
                yield block

    headers = {"authorization": aai.settings.api_key, "content-length": str(path.stat().st_size)}
    timeout = httpx.Timeout(NETWORK_TIMEOUT, connect=30.0)
    resp = httpx.post(f"{aai.settings.base_url}/v2/upload", headers=headers, content=chunks(), timeout=timeout)
    if resp.status_code in (401, 403):
        raise RuntimeError("invalid api key")
    resp.raise_for_status()
    return resp.json()["upload_url"]


def upload_with_retries(transcriber: Any, path: Path) -> str:
    """Повторяем только загрузку: так ретрай не создаёт дубли платных заданий."""
    last: Optional[Exception] = None
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        try:
            return upload_file(path)
        except Exception as exc:  # noqa: BLE001
            if is_auth_error(exc):
                console.print("[red]AssemblyAI отклонил ключ.[/] Проверьте .env (команда `key`).")
                raise typer.Exit(2)
            last = exc
            log(f"UPLOAD_RETRY\tfile={path.name}\tattempt={attempt}\terror={scrub(exc)}")
            if attempt < UPLOAD_ATTEMPTS:
                delay = 5 * attempt
                console.print(f"[yellow]Связь прервалась: {err(exc)}. Повтор через {delay} с.[/]")
                time.sleep(delay)
    assert last is not None
    raise last


def wait_with_retries(transcript: Any, name: str) -> Any:
    """Продолжаем ждать то же задание после временных сбоев сети."""
    last: Optional[Exception] = None
    for attempt in range(1, POLL_ATTEMPTS + 1):
        try:
            return transcript.wait_for_completion()
        except Exception as exc:  # noqa: BLE001
            last = exc
            log(f"POLL_RETRY\tfile={name}\tid={transcript.id}\tattempt={attempt}\terror={scrub(exc)}")
            if attempt < POLL_ATTEMPTS:
                delay = 5 * attempt
                console.print(f"[yellow]Сбой связи, жду то же задание ещё {delay} с.[/]")
                time.sleep(delay)
    assert last is not None
    raise last


def output_names(files: List[Path]) -> dict[Path, str]:
    """Имя .txt для каждого файла: `a.txt`, а при совпадении основ (`a.mp3` и `a.mp4`) — `a_mp3.txt`, `a_mp4.txt`."""
    counts: dict[str, int] = {}
    for f in files:
        counts[f.stem.casefold()] = counts.get(f.stem.casefold(), 0) + 1
    return {
        f: f"{f.stem}_{f.suffix.lstrip('.').lower()}.txt" if counts[f.stem.casefold()] > 1 else f"{f.stem}.txt"
        for f in files
    }


def transcribe_one(transcriber: Any, config: Any, media: Path, out_path: Path, speakers: bool) -> float:
    """Загрузка → задание → ожидание → запись .txt. Возвращает секунды аудио (для расчёта стоимости)."""
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), TimeElapsedColumn(),
                  console=console, transient=True) as progress:
        task = progress.add_task("Загружаю файл…", total=None)
        audio_url = upload_with_retries(transcriber, media)
        progress.update(task, description="Создаю задание…")
        transcript = transcriber.submit(audio_url, config=config)
        log(f"SUBMITTED\tfile={media.name}\tid={transcript.id}")
        progress.update(task, description=f"Распознаю (задание {transcript.id})…")
        transcript = wait_with_retries(transcript, media.name)

    status = str(getattr(transcript.status, "value", transcript.status)).lower()
    if status == "error":
        raise RuntimeError(getattr(transcript, "error", None) or "Неизвестная ошибка AssemblyAI")
    text = format_transcript(transcript, speakers)
    if not text:
        raise RuntimeError("AssemblyAI вернул пустую транскрибацию")
    write_atomic(out_path, text)
    seconds = float(getattr(transcript, "audio_duration", None) or 0)
    log(f"OK\tfile={media.name}\tseconds={seconds:.0f}\tcost_est=${cost_of(seconds):.4f}\toutput={out_path.name}\tid={transcript.id}"
        f"\tlanguage={getattr(transcript, 'language_code', None) or 'unknown'}")
    return seconds


# ---------- команды ----------

@app.command()
def file(
    path: Path = typer.Argument(..., exists=True, help="Файл или папка с видео/аудио"),
    output_dir: Path = typer.Option(DEFAULT_OUT, "--output-dir", "-o", help="Куда класть .txt"),
    language: str = typer.Option("auto", "--language", "-l", help="auto или код языка (ru, en, …)"),
    speakers: bool = typer.Option(False, "--speakers", help="Метки спикеров"),
    terms: Optional[str] = typer.Option(None, "--terms", "-t", help="Термины/имена через запятую для лучшего распознавания"),
    force: bool = typer.Option(False, "--force", help="Перезаписать уже готовые .txt"),
) -> None:
    """Транскрибировать файл или все поддерживаемые файлы папки."""
    if path.is_dir():
        files = sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED)
    elif path.suffix.lower() in SUPPORTED:
        files = [path]
    else:
        console.print(f"[red]Формат {path.suffix or '?'} не поддерживается.[/]")
        raise typer.Exit(2)
    if not files:
        console.print("В папке нет подходящих видео/аудио.")
        raise typer.Exit(0)

    output_dir.mkdir(parents=True, exist_ok=True)
    names = output_names(files)
    pending = [f for f in files if force or not (output_dir / names[f]).exists()]
    console.print(f"Найдено: {len(files)}, к обработке: {len(pending)}, уже готово: {len(files) - len(pending)}.")
    if not pending:
        console.print("Для повторной обработки используйте --force.")
        raise typer.Exit(0)

    transcriber, config = make_transcriber(get_api_key(), speakers, language, terms)
    failures = 0
    total_seconds = 0.0
    for i, media in enumerate(pending, 1):
        out_path = output_dir / names[media]
        console.print(f"\n[bold][{i}/{len(pending)}][/] {media.name}")
        try:
            seconds = transcribe_one(transcriber, config, media, out_path, speakers)
            total_seconds += seconds
            console.print(f"[green]Готово:[/] {out_path}\n  Потрачено: {fmt_cost(seconds)}")
        except KeyboardInterrupt:
            log(f"STOPPED\tfile={media.name}")
            console.print("\n[yellow]Остановлено. Готовые тексты сохранены.[/]")
            raise typer.Exit(130)
        except typer.Exit:
            raise
        except Exception as exc:  # noqa: BLE001
            failures += 1
            log(f"ERROR\tfile={media.name}\terror={scrub(exc)}")
            console.print(f"[red]Ошибка:[/] {err(exc)}")

    console.print(f"\nЗавершено. Успешно: {len(pending) - failures}, ошибок: {failures}.")
    console.print(f"Итого потрачено: {fmt_cost(total_seconds)}")
    raise typer.Exit(1 if failures else 0)


@app.command()
def youtube(
    url: str = typer.Argument(..., help="Ссылка на YouTube-видео (одно, не плейлист)"),
    output_dir: Path = typer.Option(DEFAULT_OUT, "--output-dir", "-o"),
    language: str = typer.Option("auto", "--language", "-l"),
    speakers: bool = typer.Option(False, "--speakers"),
    terms: Optional[str] = typer.Option(None, "--terms", "-t", help="Термины/имена через запятую для лучшего распознавания"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Не спрашивать подтверждение платного запуска"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Скачать звук YouTube-видео во временную папку и транскрибировать."""
    import yt_dlp

    key = get_api_key()
    opts: dict[str, Any] = {
        "format": "139/bestaudio[abr<=64]/bestaudio/best",
        "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
        "js_runtimes": {n: {} for n in ("deno", "node") if shutil.which(n)},
    }
    with console.status("Получаю информацию о видео…"):
        with yt_dlp.YoutubeDL({**opts, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    if info.get("is_live"):
        console.print("[red]Идущие сейчас эфиры не поддерживаются.[/]")
        raise typer.Exit(2)

    title = info.get("title") or "youtube"
    minutes = int(info.get("duration") or 0) // 60
    console.print(f"[bold]{title}[/] — {minutes} мин.")
    out_path = output_dir / f"{safe_name(title)}.txt"
    output_dir.mkdir(parents=True, exist_ok=True)
    if out_path.exists() and not force:
        console.print(f"Уже готово: {out_path}. Для повторной обработки --force.")
        raise typer.Exit(0)
    if not yes and not typer.confirm("Платное распознавание AssemblyAI. Продолжить?"):
        raise typer.Exit(0)

    transcriber, config = make_transcriber(key, speakers, language, terms)
    with tempfile.TemporaryDirectory(prefix="aai_yt_") as tmp:
        with console.status("Скачиваю звук…"):
            with yt_dlp.YoutubeDL({**opts, "outtmpl": str(Path(tmp) / "audio.%(ext)s")}) as ydl:
                ydl.download([info.get("webpage_url") or url])
        audio = next(Path(tmp).iterdir())
        try:
            seconds = transcribe_one(transcriber, config, audio, out_path, speakers)
        except typer.Exit:
            raise
        except Exception as exc:  # noqa: BLE001
            log(f"ERROR\tyoutube={url}\terror={scrub(exc)}")
            console.print(f"[red]Ошибка:[/] {err(exc)}")
            raise typer.Exit(1)
    console.print(f"[green]Готово:[/] {out_path}\n  Потрачено: {fmt_cost(seconds)}")


@app.command()
def key() -> None:
    """Проверить API-ключ (бесплатный запрос списка транскрипций)."""
    import assemblyai as aai

    aai.settings.api_key = get_api_key()
    try:
        aai.Transcriber().list_transcripts(aai.ListTranscriptParameters(limit=1))
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Ключ не принят:[/] {err(exc)}")
        raise typer.Exit(1)
    console.print("[green]API-ключ принят.[/]")


if __name__ == "__main__":
    sys.exit(app())
