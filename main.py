#!/usr/bin/env python3
"""CLI-транскрибатор на AssemblyAI (Typer + Rich).

Один вход: `transcrib <ссылка|путь>` — ссылка уходит на YouTube, путь читается с диска.
Явные команды тоже остались: run, file, youtube, key.
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
from typing import Any

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.markup import escape
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

BASE = Path(__file__).resolve().parent
OUT_DIR_NAME = "ГОТОВЫЕ_ТЕКСТЫ"
LOG_FILE = BASE / "transcription.log"
LOG_MAX_BYTES = 2 * 1024 * 1024  # при превышении лог переезжает в transcription.log.1
KEY_PLACEHOLDER = "PASTE_YOUR_KEY_HERE"

SUPPORTED = {
    ".mp3", ".mp4", ".m4a", ".mov", ".mkv", ".avi", ".webm", ".wav",
    ".aac", ".flac", ".ogg", ".opus", ".mpeg", ".mpg",
}
NETWORK_TIMEOUT = 90.0  # таймаут одной сетевой операции; зависшая загрузка уходит в ретрай
POLL_TIMEOUT = 3 * 3600  # потолок ожидания задания: 3 часа, чтобы не висеть вечно

# Тарифы за час аудио, USD. API стоимость не возвращает, поэтому это ОЦЕНКА.
# Значения — по https://www.assemblyai.com/pricing на 19.11.2026:
# Universal-3.5 Pro (модель по умолчанию) $0.21/ч, спикеры +$0.02/ч, термины +$0.05/ч.
DEFAULT_PRICE_PER_HOUR = 0.21
PRICE_SPEAKERS_PER_HOUR = 0.02
PRICE_TERMS_PER_HOUR = 0.05

# Термины, вшитые по умолчанию (улучшают распознавание названий и имён). Платная надбавка $0.05/ч.
# Заменить список: --terms "А,Б" или ASSEMBLYAI_TERMS в .env. Отключить: --no-terms или ASSEMBLYAI_TERMS= (пусто).
DEFAULT_TERMS = (
    "ChatGPT,OpenAI,Anthropic,Claude,Claude Code,Codex,OpenCode,Gemini,Cursor,Copilot,"
    "GitHub,GitLab,Python,TypeScript,JavaScript,Docker,Telegram,YouTube,API,GPT,LLM,MCP,AssemblyAI"
)

UPLOAD_ATTEMPTS = 3
POLL_ATTEMPTS = 5
PAUSE_MS = 1800  # пауза между словами, после которой начинается новый абзац
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
_COMMANDS = {"run", "file", "youtube", "key"}
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

app = typer.Typer(add_completion=False, help="Транскрибатор на AssemblyAI: ссылка или файл на входе.")
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
    """Журнал не должен ломать основную работу: любые сбои записи глотаем."""
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    clean = scrub(message).replace("\t", " ").replace("\n", " ").replace("\r", " ")
    line = f"{stamp}\t{clean}\n"
    try:
        if LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            LOG_FILE.replace(LOG_FILE.with_name(LOG_FILE.name + ".1"))
    except OSError:
        pass
    try:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def use_log_dir(directory: Path) -> None:
    """Журнал живёт рядом с результатами, а не в каталоге пакета."""
    global LOG_FILE
    LOG_FILE = directory / "transcription.log"


def price_base() -> float:
    """Базовый тариф с проверкой значения из окружения."""
    raw = os.environ.get("ASSEMBLYAI_PRICE_PER_HOUR")
    try:
        value = float(raw) if raw else DEFAULT_PRICE_PER_HOUR
    except ValueError:
        return DEFAULT_PRICE_PER_HOUR
    return value if 0 < value <= 100 else DEFAULT_PRICE_PER_HOUR


def price_per_hour(speakers: bool = False, terms: bool = False) -> float:
    extra = (PRICE_SPEAKERS_PER_HOUR if speakers else 0) + (PRICE_TERMS_PER_HOUR if terms else 0)
    return price_base() + extra


def cost_of(seconds: float, speakers: bool = False, terms: bool = False) -> float:
    return seconds / 3600 * price_per_hour(speakers, terms)


def fmt_cost(seconds: float, speakers: bool = False, terms: bool = False) -> str:
    parts = [f"${price_base():g}/ч"]
    if speakers:
        parts.append(f"+${PRICE_SPEAKERS_PER_HOUR:g} спикеры")
    if terms:
        parts.append(f"+${PRICE_TERMS_PER_HOUR:g} термины")
    return f"{seconds / 60:.1f} мин ≈ ${cost_of(seconds, speakers, terms):.4f} (оценка: {' '.join(parts)})"


def _env_files() -> list[Path]:
    return [Path.cwd() / ".env", BASE / ".env"]


def find_api_key() -> str:
    """Ключ ищем в окружении, затем в .env текущей папки, затем рядом со скриптом."""
    key = os.environ.get("ASSEMBLYAI_API_KEY", "").strip()
    if key and key != KEY_PLACEHOLDER:
        return key
    for candidate in _env_files():
        if not candidate.is_file():
            continue
        load_dotenv(candidate, override=False)
        key = os.environ.get("ASSEMBLYAI_API_KEY", "").strip()
        if key and key != KEY_PLACEHOLDER:
            return key
    return ""


def get_api_key() -> str:
    key = find_api_key()
    if key:
        return key
    places = " или ".join(str(p) for p in _env_files())
    console.print(f"[red]Ключ не найден.[/] Положите ASSEMBLYAI_API_KEY в .env ({places}) "
                  f"или задайте переменную окружения.")
    raise typer.Exit(2)


def looks_like_url(value: str) -> bool:
    return bool(re.match(r"^(https?://|www\.)", value.strip(), re.IGNORECASE))


def resolve_terms(terms: str | None, no_terms: bool) -> str | None:
    """Итоговый список терминов: --no-terms > --terms > ASSEMBLYAI_TERMS (пусто = выключено) > DEFAULT_TERMS."""
    if no_terms:
        return None
    for candidate in _env_files():
        if candidate.is_file():
            load_dotenv(candidate, override=False)
    if terms is not None:
        return terms.strip() or None
    env = os.environ.get("ASSEMBLYAI_TERMS")
    if env is not None:
        return env.strip() or None
    return DEFAULT_TERMS


def resolve_out(output_dir: Path | None) -> Path:
    return (output_dir or (Path.cwd() / OUT_DIR_NAME)).expanduser()


def make_transcriber(key: str, speakers: bool, language: str, terms: str | None = None):
    import assemblyai as aai

    aai.settings.api_key = key
    aai.settings.http_timeout = NETWORK_TIMEOUT
    params: dict[str, Any] = {
        "speaker_labels": speakers,
        # Модель задаём явно: иначе цена и качество зависят от серверного значения по умолчанию.
        "speech_models": ["universal-3-5-pro", "universal-2"],
    }
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

    blocks: list[str] = []
    para: list[Any] = []

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
    value = value or fallback
    if value.split(".")[0].upper() in _WIN_RESERVED:  # CON.txt на Windows создать нельзя
        value = f"_{value}"
    return value[:180]


def is_auth_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(s in text for s in ("invalid api key", "unauthorized", "authentication"))


def is_retryable(exc: Exception) -> bool:
    """Повторяем только временные сбои: сеть, таймауты, 429 и 5xx."""
    import httpx

    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is None:
        return True  # неизвестная ошибка — считаем временной, как раньше
    return int(status) in RETRYABLE_STATUS


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


def upload_with_retries(path: Path) -> str:
    """Повторяем только загрузку: так ретрай не создаёт дубли платных заданий."""
    last: Exception | None = None
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        try:
            return upload_file(path)
        except Exception as exc:
            if is_auth_error(exc):
                console.print("[red]AssemblyAI отклонил ключ.[/] Проверьте .env (команда `key`).")
                raise typer.Exit(2) from exc
            last = exc
            log(f"UPLOAD_RETRY\tfile={path.name}\tattempt={attempt}\terror={scrub(exc)}")
            if not is_retryable(exc) or attempt == UPLOAD_ATTEMPTS:
                break
            delay = 5 * attempt
            console.print(f"[yellow]Связь прервалась: {err(exc)}. Повтор через {delay} с.[/]")
            time.sleep(delay)
    if last is None:
        raise RuntimeError("загрузка не выполнена")
    raise last


def wait_with_retries(transcript: Any, name: str) -> Any:
    """Продолжаем ждать то же задание после временных сбоев сети."""
    last: Exception | None = None
    for attempt in range(1, POLL_ATTEMPTS + 1):
        try:
            return transcript.wait_for_completion(poll_timeout=POLL_TIMEOUT)
        except Exception as exc:
            last = exc
            log(f"POLL_RETRY\tfile={name}\tid={transcript.id}\tattempt={attempt}\terror={scrub(exc)}")
            if not is_retryable(exc) or attempt == POLL_ATTEMPTS:
                break
            delay = 5 * attempt
            console.print(f"[yellow]Сбой связи, жду то же задание ещё {delay} с.[/]")
            time.sleep(delay)
    if last is None:
        raise RuntimeError("задание не дождалось ответа")
    raise last


def output_names(files: list[Path]) -> dict[Path, str]:
    """Имя .txt для каждого файла, без коллизий даже при похожих именах.

    `a.txt`, а при совпадении основ (`a.mp3` и `a.mp4`) — `a_mp3.txt`, `a_mp4.txt`;
    если и это занято (`a_mp3.mp3`), добавляется `_2`, `_3`, …
    """
    counts: dict[str, int] = {}
    for f in files:
        stem = f.stem.casefold()
        counts[stem] = counts.get(stem, 0) + 1

    used: set[str] = set()
    names: dict[Path, str] = {}
    for f in files:
        stem = f.stem if counts[f.stem.casefold()] == 1 else f"{f.stem}_{f.suffix.lstrip('.').lower()}"
        candidate = f"{stem}.txt"
        index = 2
        while candidate.casefold() in used:
            candidate = f"{stem}_{index}.txt"
            index += 1
        used.add(candidate.casefold())
        names[f] = candidate
    return names


def transcribe_one(transcriber: Any, config: Any, media: Path, out_path: Path,
                   speakers: bool, rate: float) -> float:
    """Загрузка → задание → ожидание → запись .txt. Возвращает секунды аудио."""
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), TimeElapsedColumn(),
                  console=console, transient=True) as progress:
        task = progress.add_task("Загружаю файл…", total=None)
        audio_url = upload_with_retries(media)
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
    seconds = float(getattr(transcript, "audio_duration", None) or 0)
    try:
        write_atomic(out_path, text)
    except OSError as exc:
        log(f"WRITE_ERROR\tfile={media.name}\tid={transcript.id}\terror={scrub(exc)}")
        raise RuntimeError(f"не удалось записать {out_path.name}: {exc}. "
                           f"Текст оплачен, id задания {transcript.id}") from exc
    log(f"OK\tfile={media.name}\tseconds={seconds:.0f}\tcost_est=${seconds / 3600 * rate:.4f}"
        f"\toutput={out_path.name}\tid={transcript.id}"
        f"\tlanguage={getattr(transcript, 'language_code', None) or 'unknown'}"
        f"\tmodel={getattr(transcript, 'speech_model_used', None) or 'unknown'}")
    return seconds


# ---------- команды ----------

def _collect_files(path: Path) -> list[Path]:
    if not path.exists():
        console.print(f"[red]Не найдено:[/] {escape(str(path))}")
        raise typer.Exit(2)
    if path.is_dir():
        return sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED)
    if path.suffix.lower() in SUPPORTED:
        return [path]
    console.print(f"[red]Формат {path.suffix or '?'} не поддерживается.[/]")
    raise typer.Exit(2)


def print_plan(files: list[Path], names: dict[Path, str], out_dir: Path,
               force: bool, speakers: bool, terms: bool) -> None:
    """Показать, что будет сделано, ничего не скачивая и не оплачивая."""
    rate = price_per_hour(speakers, terms)
    console.print("[bold]Проверка без оплаты[/]: ничего не скачивается и не отправляется.")
    for f in files:
        target = out_dir / names[f]
        state = "готово, пропущу" if target.exists() and not force else "будет обработано"
        console.print(f"  {escape(f.name)} → {escape(names[f])} [{state}]")
    pending = sum(1 for f in files if force or not (out_dir / names[f]).exists())
    console.print(f"К обработке: {pending} из {len(files)}. Тариф: ${rate:g}/ч; "
                  "итог зависит от длительности.")


def run_file(path: Path, output_dir: Path | None, language: str, speakers: bool,
             terms: str | None, force: bool, yes: bool, dry_run: bool = False) -> None:
    files = _collect_files(path)
    if not files:
        console.print("В папке нет подходящих видео/аудио.")
        raise typer.Exit(0)

    out_dir = resolve_out(output_dir)
    if out_dir.exists() and not out_dir.is_dir():
        console.print(f"[red]{escape(str(out_dir))} — это файл, а нужен каталог для результатов.[/]")
        raise typer.Exit(2)
    names = output_names(files)
    pending = [f for f in files if force or not (out_dir / names[f]).exists()]
    console.print(f"Найдено: {len(files)}, к обработке: {len(pending)}, уже готово: {len(files) - len(pending)}.")
    if dry_run:
        print_plan(files, names, out_dir, force, speakers, bool(terms))
        raise typer.Exit(0)
    if not pending:
        console.print("Для повторной обработки используйте --force.")
        raise typer.Exit(0)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        console.print(f"[red]Не удалось создать папку результатов {escape(str(out_dir))}:[/] {err(exc)}")
        raise typer.Exit(2) from exc
    use_log_dir(out_dir)
    key = get_api_key()
    if len(pending) > 1 and not yes:
        console.print(f"Платно: {len(pending)} файлов, тариф ${price_base():g}/ч"
                      + (f" (+ спикеры ${PRICE_SPEAKERS_PER_HOUR:g}/ч)" if speakers else "")
                      + (f" (+ термины ${PRICE_TERMS_PER_HOUR:g}/ч)" if terms else "") + ".")
        if not typer.confirm("Продолжить?", default=True):
            raise typer.Exit(0)

    transcriber, config = make_transcriber(key, speakers, language, terms)
    rate = price_per_hour(speakers, terms)
    failures = 0
    total_seconds = 0.0
    for i, media in enumerate(pending, 1):
        out_path = out_dir / names[media]
        console.print(f"\n[bold][{i}/{len(pending)}][/] {escape(media.name)}")
        try:
            seconds = transcribe_one(transcriber, config, media, out_path, speakers, rate)
            total_seconds += seconds
            console.print(f"[green]Готово:[/] {escape(str(out_path))}\n"
                          f"  Потрачено: {fmt_cost(seconds, speakers, terms)}")
        except KeyboardInterrupt:
            log(f"STOPPED\tfile={media.name}")
            console.print("\n[yellow]Остановлено. Готовые тексты сохранены.[/]")
            raise typer.Exit(130)
        except typer.Exit:
            raise
        except Exception as exc:
            failures += 1
            log(f"ERROR\tfile={media.name}\terror={scrub(exc)}")
            console.print(f"[red]Ошибка:[/] {err(exc)}")

    console.print(f"\nЗавершено. Успешно: {len(pending) - failures}, ошибок: {failures}.")
    console.print(f"Итого потрачено: {fmt_cost(total_seconds, speakers, terms)}")
    raise typer.Exit(1 if failures else 0)


def download_audio(url: str, opts: dict[str, Any], tmp: Path) -> Path:
    """Скачать звук во временную папку и вернуть единственный готовый файл."""
    import yt_dlp

    with yt_dlp.YoutubeDL({**opts, "outtmpl": str(tmp / "audio.%(ext)s")}) as ydl:
        ydl.download([url])
    files = [p for p in tmp.iterdir() if p.is_file() and p.suffix not in (".part", ".ytdl", ".temp")]
    if not files:
        raise RuntimeError("звук не скачался: временная папка пуста")
    return max(files, key=lambda p: p.stat().st_size)


class _QuietLogger:
    """Гасим служебный вывод yt-dlp: об ошибке сообщаем сами."""

    def debug(self, msg: str) -> None: ...

    def info(self, msg: str) -> None: ...

    def warning(self, msg: str) -> None: ...

    def error(self, msg: str) -> None: ...


def run_youtube(url: str, output_dir: Path | None, language: str, speakers: bool,
                terms: str | None, force: bool, yes: bool, dry_run: bool = False) -> None:
    import yt_dlp

    opts: dict[str, Any] = {
        "format": "139/bestaudio[abr<=64]/bestaudio/best",
        "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
        "socket_timeout": 30, "retries": 3, "fragment_retries": 3, "extractor_retries": 3,
        "logger": _QuietLogger(),
        "js_runtimes": {n: {} for n in ("deno", "node") if shutil.which(n)},
    }
    with console.status("Получаю информацию о видео…"):
        try:
            with yt_dlp.YoutubeDL({**opts, "skip_download": True}) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            log(f"ERROR\turl={url}\terror={scrub(exc)}")
            console.print(f"[red]Не удалось прочитать видео:[/] {err(exc)}")
            console.print("  Проверьте ссылку и доступность ролика (приватные и платные не подойдут).")
            raise typer.Exit(1) from exc
    if info.get("is_live"):
        console.print("[red]Идущие сейчас эфиры не поддерживаются.[/]")
        raise typer.Exit(2)

    title = info.get("title") or "youtube"
    seconds = float(info.get("duration") or 0)
    console.print(f"[bold]{escape(title)}[/] — {int(seconds) // 60} мин.")

    out_dir = resolve_out(output_dir)
    if out_dir.exists() and not out_dir.is_dir():
        console.print(f"[red]{escape(str(out_dir))} — это файл, а нужен каталог для результатов.[/]")
        raise typer.Exit(2)
    out_path = out_dir / f"{safe_name(title)}.txt"
    if dry_run:
        state = "готово, пропущу" if out_path.exists() and not force else "будет создано"
        console.print("[bold]Проверка без оплаты[/]: ничего не скачивается и не отправляется.")
        console.print(f"  {escape(title)} → {escape(out_path.name)} [{state}]")
        if seconds:
            console.print(f"Цена: {fmt_cost(seconds, speakers, terms)}")
        raise typer.Exit(0)

    key = get_api_key()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        console.print(f"[red]Не удалось создать папку результатов {escape(str(out_dir))}:[/] {err(exc)}")
        raise typer.Exit(2) from exc
    use_log_dir(out_dir)

    if out_path.exists() and not force:
        console.print(f"Уже готово: {escape(str(out_path))}. Для повторной обработки --force.")
        raise typer.Exit(0)
    if seconds:
        console.print(f"Цена: {fmt_cost(seconds, speakers, terms)}")

    transcriber, config = make_transcriber(key, speakers, language, terms)
    rate = price_per_hour(speakers, terms)
    with tempfile.TemporaryDirectory(prefix="aai_yt_") as tmp_name:
        tmp = Path(tmp_name)
        with console.status("Скачиваю звук…"):
            try:
                audio = download_audio(info.get("webpage_url") or url, opts, tmp)
            except Exception as exc:
                log(f"ERROR\tyoutube={url}\terror={scrub(exc)}")
                console.print(f"[red]Не удалось скачать звук:[/] {err(exc)}")
                console.print("  Это сетевая ошибка или ограничение YouTube. Повторите позже или скачайте файл "
                              "вручную и передайте его путь.")
                raise typer.Exit(1) from exc
        try:
            total = transcribe_one(transcriber, config, audio, out_path, speakers, rate)
        except typer.Exit:
            raise
        except Exception as exc:
            log(f"ERROR\tyoutube={url}\terror={scrub(exc)}")
            console.print(f"[red]Ошибка:[/] {err(exc)}")
            raise typer.Exit(1) from exc
    console.print(f"[green]Готово:[/] {escape(str(out_path))}\n  Потрачено: {fmt_cost(total, speakers, terms)}")


# ---------- общие опции ----------

OutputOpt = typer.Option(None, "--output-dir", "-o", help=f"Куда класть .txt (по умолчанию ./{OUT_DIR_NAME})")
LanguageOpt = typer.Option("auto", "--language", "-l", help="auto или код языка (ru, en, …)")
SpeakersOpt = typer.Option(False, "--speakers", help="Метки спикеров")
TermsOpt = typer.Option(None, "--terms", "-t", help="Свои термины через запятую (вместо вшитых по умолчанию)")
NoTermsOpt = typer.Option(False, "--no-terms", help="Отключить термины (дешевле на $0.05/ч)")
ForceOpt = typer.Option(False, "--force", help="Перезаписать уже готовые .txt")
YesOpt = typer.Option(False, "--yes", "-y", help="Не спрашивать подтверждение пакета")
DryRunOpt = typer.Option(False, "--dry-run", help="Показать план и цену, ничего не оплачивать")


@app.command()
def run(
    source: str = typer.Argument(..., help="Ссылка на видео или путь к файлу/папке"),
    output_dir: Path | None = OutputOpt,
    language: str = LanguageOpt,
    speakers: bool = SpeakersOpt,
    terms: str | None = TermsOpt,
    no_terms: bool = NoTermsOpt,
    force: bool = ForceOpt,
    yes: bool = YesOpt,
    dry_run: bool = DryRunOpt,
) -> None:
    """Ссылка или файл — разобраться и обработать без лишних вопросов."""
    terms = resolve_terms(terms, no_terms)
    if looks_like_url(source):
        run_youtube(source, output_dir, language, speakers, terms, force, yes, dry_run)
    else:
        run_file(Path(source), output_dir, language, speakers, terms, force, yes, dry_run)


@app.command()
def file(
    path: Path = typer.Argument(..., exists=True, help="Файл или папка с видео/аудио"),
    output_dir: Path | None = OutputOpt,
    language: str = LanguageOpt,
    speakers: bool = SpeakersOpt,
    terms: str | None = TermsOpt,
    no_terms: bool = NoTermsOpt,
    force: bool = ForceOpt,
    yes: bool = YesOpt,
    dry_run: bool = DryRunOpt,
) -> None:
    """Транскрибировать файл или все поддерживаемые файлы папки."""
    terms = resolve_terms(terms, no_terms)
    run_file(path, output_dir, language, speakers, terms, force, yes, dry_run)


@app.command()
def youtube(
    url: str = typer.Argument(..., help="Ссылка на YouTube-видео (одно, не плейлист)"),
    output_dir: Path | None = OutputOpt,
    language: str = LanguageOpt,
    speakers: bool = SpeakersOpt,
    terms: str | None = TermsOpt,
    no_terms: bool = NoTermsOpt,
    force: bool = ForceOpt,
    yes: bool = YesOpt,
    dry_run: bool = DryRunOpt,
) -> None:
    """Скачать звук YouTube-видео во временную папку и транскрибировать."""
    terms = resolve_terms(terms, no_terms)
    run_youtube(url, output_dir, language, speakers, terms, force, yes, dry_run)


@app.command()
def key() -> None:
    """Проверить API-ключ (бесплатный запрос списка транскрипций)."""
    import assemblyai as aai

    aai.settings.api_key = get_api_key()
    aai.settings.http_timeout = NETWORK_TIMEOUT
    try:
        aai.Transcriber().list_transcripts(aai.ListTranscriptParameters(limit=1))
    except Exception as exc:
        console.print(f"[red]Ключ не принят:[/] {err(exc)}")
        raise typer.Exit(1) from exc
    console.print("[green]API-ключ принят.[/]")


def prepare_argv(argv: list[str]) -> list[str]:
    """`transcrib <ссылка|путь>` без подкоманды: подставляем run."""
    if len(argv) < 2:
        return argv
    if any(arg in _COMMANDS for arg in argv[1:]):
        return argv
    if argv[1] in ("--help", "-h"):
        return argv
    return [argv[0], "run", *argv[1:]]


def main() -> None:
    sys.argv = prepare_argv(sys.argv)
    app()


if __name__ == "__main__":
    sys.exit(main())
