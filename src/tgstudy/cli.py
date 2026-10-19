from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import portalocker
from telethon import TelegramClient, utils

from . import __version__
from .config import Chat, Config, data_dir, default_timezone, status_write
from .control import instance_running, lock, maintenance
from .exporter import export_dirty
from .history import export_history
from .periods import day_window, keep_period, set_period
from .startup import install_autostart, remove_autostart
from .store import Store
from .transcribe import Transcriber
from .watcher import telegram_running, watch
from .worker import run_worker, sync_now, telegram_client


def prompt(label: str, default: str = "") -> str:
    value = input(label + (f" [{default}]" if default else "") + ": ").strip()
    return value or default


async def choose_chat(client):
    print("\nВведи часть названия чата или канала. Enter покажет все чаты.")
    while True:
        search = prompt("Поиск").casefold()
        matches = [
            dialog
            async for dialog in client.iter_dialogs()
            if search in (dialog.name or "").casefold()
        ]
        if not matches:
            print("Совпадений нет; попробуй другое имя.")
            continue
        for index, dialog in enumerate(matches, 1):
            print(f"{index}. {dialog.name} (id={dialog.id})")
        choice = prompt("Номер чата (Enter = новый поиск)")
        if choice.isdigit() and 1 <= int(choice) <= len(matches):
            selected = matches[int(choice) - 1]
            await client.get_input_entity(selected.entity)
            return selected


async def add_chat(root: Path, cfg: Config, *, days=None, own_posts=None):
    days = (
        days
        if days is not None
        else (1 if cfg.export_mode == "daily" else cfg.history_days)
    )
    _, _, start, _ = day_window(cfg.timezone, days)
    client = telegram_client(cfg, root)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("AuthorizationRequiredRunSetup")
        if (await client.get_me()).id != cfg.own_id:
            raise RuntimeError("SessionAccountMismatchRunSetup")
        selected = await choose_chat(client)
        chat_id = utils.get_peer_id(selected.entity)
        if chat_id in {c.chat_id for c in cfg.chat_configs()}:
            print("Этот чат уже подключён.")
            return
        broadcast = bool(getattr(selected.entity, "broadcast", False))
        if own_posts and not broadcast:
            raise ValueError("--own-posts предназначен только для личного канала")
        if own_posts is None:
            own_posts = broadcast and prompt(
                "В канале только твои записи? y/n (y = отмечать посты как твой output)",
                "n",
            ).lower() in {"y", "yes", "д", "да"}
        cfg.extra_chats.append(
            Chat(
                chat_id,
                selected.name or str(chat_id),
                start.isoformat() if start else None,
                bool(own_posts),
            )
        )
        cfg.save(root)
        store = Store(root / "journal.sqlite3")
        try:
            store.reset_cursor(chat_id)
            export_dirty(store, cfg.select_chat(chat_id))
        finally:
            store.close()
        print(
            f"Подключён: {selected.name}. Выгрузка: {Path(cfg.output_dir) / str(chat_id)}"
        )
        print(
            "Выполни sync для немедленной догрузки. При открытом Telegram она также начнётся автоматически."
        )
    finally:
        await client.disconnect()


def remove_chat(root: Path, cfg: Config, chat_id: int):
    cfg.select_chat(chat_id)  # Reject unknown IDs before changing anything.
    if chat_id == cfg.chat_id:
        if not cfg.extra_chats:
            raise ValueError("Нельзя отключить последний чат")
        promoted = cfg.extra_chats.pop(0)
        cfg.chat_id, cfg.chat_title = promoted.chat_id, promoted.chat_title
        cfg.history_since, cfg.own_posts = promoted.history_since, promoted.own_posts
    else:
        cfg.extra_chats = [chat for chat in cfg.extra_chats if chat.chat_id != chat_id]
    cfg.save(root)
    print("Чат отключён. Его архив сохранён.")


def choose_mode(cfg: Config):
    print("\n1 — единая выгрузка за последние N дней (включая сегодня)")
    print("2 — весь текущий день, завтра новый файл автоматически")
    choice = prompt("Режим 1/2", "2" if cfg.export_mode == "daily" else "1")
    if choice not in {"1", "2"}:
        raise ValueError("Выбери 1 или 2")
    mode = "daily" if choice == "2" else "history"
    days = (
        int(prompt("Количество дней; 0 = вся история", str(cfg.history_days)))
        if mode == "history"
        else None
    )
    keep_period(cfg, mode, days)


def save_mode(root: Path, cfg: Config):
    cfg.save(root)
    store = Store(root / "journal.sqlite3")
    try:
        for scoped in cfg.chat_configs():
            store.reset_cursor(scoped.chat_id)
            export_dirty(store, scoped)
    finally:
        store.close()


async def setup(root: Path):
    previous = None
    if (root / "config.json").exists():
        previous = Config.load(root)
    print("\nTelegram Study Logger — первоначальная настройка\n")
    print("Открой https://my.telegram.org → API development tools.")
    print("api_id и api_hash вводятся здесь на твоём компьютере.")
    print("Модуль создаст отдельную сессию Telegram; пароль и код не сохраняются.\n")
    api_id = int(prompt("api_id", str(previous.api_id) if previous else ""))
    if previous:
        api_hash = (
            getpass.getpass("api_hash (Enter = прежний): ").strip() or previous.api_hash
        )
    else:
        api_hash = getpass.getpass("api_hash: ").strip()
    if api_id <= 0 or len(api_hash) != 32:
        raise ValueError("Некорректные API credentials")
    client = TelegramClient(
        str(root / "account"),
        api_id,
        api_hash,
        device_model="Telegram Study Logger",
        app_version=__version__,
    )
    try:
        # This is the only place allowed to prompt for account authorization.
        await client.start(
            phone=lambda: prompt("Номер телефона с кодом страны"),
            code_callback=lambda: getpass.getpass("Код входа из Telegram: ").strip(),
            password=lambda: getpass.getpass("Пароль двухэтапной аутентификации: "),
        )
        me = await client.get_me()
        print("\nПоиск чата: введи часть имени. Пустая строка покажет все чаты.")
        while True:
            search = prompt("Поиск").casefold()
            matches = []
            async for dialog in client.iter_dialogs():
                if search in (dialog.name or "").casefold():
                    matches.append(dialog)
            if not matches:
                print("Совпадений нет; попробуй другое имя.")
                continue
            for index, dialog in enumerate(matches, 1):
                label = "личный" if dialog.is_user else "группа/канал"
                print(f"{index}. {dialog.name} ({label}, id={dialog.id})")
            choice = prompt("Номер нужного чата (Enter = новый поиск)")
            if choice.isdigit() and 1 <= int(choice) <= len(matches):
                selected = matches[int(choice) - 1]
                break
        # Cache the selected input entity so negative channel IDs work after restart.
        await client.get_input_entity(selected.entity)
        tz = prompt(
            "Часовой пояс IANA", previous.timezone if previous else default_timezone()
        )
        ZoneInfo(tz)
        print("\n1 — история за последние N календарных дней (единая выгрузка периода)")
        print("2 — ежедневно: весь сегодняшний день, завтра новый файл автоматически")
        choice = prompt(
            "Режим 1/2", "2" if not previous or previous.export_mode == "daily" else "1"
        )
        if choice not in {"1", "2"}:
            raise ValueError("Выбери 1 или 2")
        mode = "daily" if choice == "2" else "history"
        days = (
            int(
                prompt(
                    "Количество дней, включая сегодня; 0 = вся история",
                    str(previous.history_days) if previous else "7",
                )
            )
            if mode == "history"
            else None
        )
        default_output = (
            previous.output_dir
            if previous
            else str(Path.home() / "Documents/TelegramStudy")
        )
        output = str(
            Path(prompt("Папка выгрузки", default_output)).expanduser().resolve()
        )
        model = prompt(
            "Модель: tiny / base / small / medium (small = баланс качества и скорости)",
            previous.model if previous else "small",
        )
        lang = prompt(
            "Язык: auto (смесь языков) / en / ru / другой код (один язык)",
            ("auto" if previous.multilingual else previous.language or "auto")
            if previous
            else "auto",
        )
        cfg = Config(
            api_id=api_id,
            api_hash=api_hash,
            chat_id=utils.get_peer_id(selected.entity),
            chat_title=selected.name,
            own_id=me.id,
            output_dir=output,
            timezone=tz,
            model=model,
            language=None if lang == "auto" else lang,
            multilingual=lang == "auto",
        )
        if previous and previous.own_id == me.id:
            cfg.extra_chats = [
                c for c in previous.extra_chats if c.chat_id != cfg.chat_id
            ]
            for scoped in previous.chat_configs():
                if scoped.chat_id == cfg.chat_id:
                    cfg.own_posts = scoped.own_posts
        set_period(cfg, mode, days)
        store = Store(root / "journal.sqlite3")
        try:
            for scoped in cfg.chat_configs():
                store.reset_cursor(scoped.chat_id)
        finally:
            store.close()
        Path(output).mkdir(parents=True, exist_ok=True, mode=0o700)
        cfg.save(root)
        print(
            f"\nВыбран: {cfg.chat_title}. Выгрузка: {Path(output) / str(cfg.chat_id)}"
        )
        if prompt("Скачать модель распознавания сейчас? y/n", "y").lower() in {
            "y",
            "yes",
            "д",
            "да",
        }:
            print(
                "Загрузка модели; это требуется один раз. Интернет нужен для скачивания."
            )
            try:
                await asyncio.to_thread(Transcriber(cfg, root).load)
            except Exception as exc:
                print(
                    f"Загрузка не удалась ({type(exc).__name__}); модуль повторит её при обработке записи."
                )
        print("\nНастройка сохранена.")
    finally:
        await client.disconnect()


def configure_logging(root: Path):
    handler = RotatingFileHandler(
        root / "runtime.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger = logging.getLogger("tgstudy")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    # Avoid logging Telegram payloads or exception reprs from third-party code.
    logging.getLogger("telethon").addHandler(logging.NullHandler())
    logging.getLogger("telethon").propagate = False


def main():
    if os.name != "nt":
        os.umask(0o077)
    parser = argparse.ArgumentParser(
        description="Автоматический локальный журнал выбранных Telegram-чатов"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in (
        "setup",
        "settings",
        "install",
        "uninstall",
        "watch",
        "status",
        "retry",
        "paths",
    ):
        sub.add_parser(name)
    chats = sub.add_parser("chats", help="Подключить дополнительные чаты и каналы")
    chat_actions = chats.add_subparsers(dest="action", required=True)
    chat_actions.add_parser("list")
    chat_add = chat_actions.add_parser("add")
    chat_add.add_argument(
        "--days", type=int, help="Глубина первого импорта; 0 = вся история"
    )
    chat_add.add_argument(
        "--own-posts",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Личный канал: считать его посты своими (кроме пересылок)",
    )
    chat_remove = chat_actions.add_parser("remove")
    chat_remove.add_argument("chat_id", type=int)
    sync = sub.add_parser("sync")
    sync.add_argument("--chat-id", type=int, help="Догрузить только этот чат")
    period = sub.add_parser("mode", help="Выбрать режим без повторной авторизации")
    period.add_argument("mode", choices=["daily", "history"])
    period.add_argument(
        "--days", type=int, help="История за N календарных дней, 0 = вся"
    )
    export = sub.add_parser(
        "export", help="Пересоздать архив или выгрузить период из Telegram"
    )
    export.add_argument("--chat-id", type=int, help="Выгрузить только этот чат")
    period_args = export.add_mutually_exclusive_group()
    period_args.add_argument(
        "--days", type=int, help="Разовая выгрузка из Telegram за N дней"
    )
    period_args.add_argument(
        "--today", action="store_true", help="Разовая выгрузка всего текущего дня"
    )
    worker = sub.add_parser("worker")
    worker.add_argument("--stop-file", type=Path)
    speech = sub.add_parser("speech", help="Языки распознавания без повторного входа")
    speech.add_argument("language", help="auto для смеси языков; en/ru для одного")
    speech.add_argument("--model", help="small/medium/large-v3 или локальная модель")
    speech.add_argument("--languages", help="Разрешённые языки auto, например en,ru")
    videos = sub.add_parser(
        "videos", help="Распознавать обычные видео (ссылки всегда исключены)"
    )
    videos.add_argument("policy", choices=["off", "on"])
    retranscribe = sub.add_parser(
        "retranscribe", help="Заново распознать сохранённые записи"
    )
    retranscribe.add_argument(
        "--chat-id", type=int, help="Повторить ASR только в этом чате"
    )
    scope = retranscribe.add_mutually_exclusive_group(required=True)
    scope.add_argument("--days", type=int, help="N дней, включая сегодня; 0 = всё")
    scope.add_argument("--today", action="store_true")
    scope.add_argument(
        "--unexpected-languages",
        action="store_true",
        help="Только сохранённые записи с неожиданным языком в метаданных",
    )
    scope.add_argument("--message-id", type=int, help="Одна запись выбранного чата")
    args = parser.parse_args()
    root = data_dir()
    configure_logging(root)
    try:
        if args.cmd == "chats":
            cfg = Config.load(root)
            if args.action == "list":
                for scoped in cfg.chat_configs():
                    print(f"{scoped.chat_id}: {scoped.chat_title}")
                    print(f"  Экспорт: {Path(scoped.output_dir) / str(scoped.chat_id)}")
                    if scoped.own_posts:
                        print(
                            "  Посты канала отмечаются как свои; пересылки исключены."
                        )
            else:
                with maintenance(root):
                    cfg = Config.load(root)
                    if args.action == "add":
                        asyncio.run(
                            add_chat(
                                root, cfg, days=args.days, own_posts=args.own_posts
                            )
                        )
                    else:
                        remove_chat(root, cfg, args.chat_id)
        elif args.cmd == "settings":
            # Also used by the installer for upgrades; an old watcher must exit first.
            remove_autostart(root)
            with lock(root, "watcher", timeout=70), lock(root, "worker", timeout=70):
                if (root / "config.json").exists():
                    cfg = Config.load(root)
                    choose_mode(cfg)
                    save_mode(root, cfg)
                else:
                    asyncio.run(setup(root))
            install_autostart(root)
            print("Режим сохранён, автозапуск включён.")
        elif args.cmd == "setup":
            # Setup must never share an account.session file with a running worker.
            with lock(root, "watcher"), lock(root, "worker"):
                asyncio.run(setup(root))
            install_autostart(root)
            print(
                "Автозапуск установлен. Можно закрыть это окно и пользоваться Telegram."
            )
        elif args.cmd == "install":
            Config.load(root)
            print(f"Автозапуск установлен: {install_autostart(root)}")
        elif args.cmd == "uninstall":
            remove_autostart(root)
            print(
                "Автозапуск отключён. Фоновые процессы завершатся; архив и настройки сохранены."
            )
        elif args.cmd == "watch":
            with lock(root, "watcher"):
                watch(root)
        elif args.cmd == "worker":
            with lock(root, "worker"):
                asyncio.run(run_worker(root, args.stop_file or root / "worker-stop"))
        elif args.cmd == "paths":
            print(f"Данные: {root}")
            if (root / "config.json").exists():
                print(f"Экспорт: {Config.load(root).output_dir}")
        elif args.cmd == "status":
            print(f"Данные: {root}")
            status = root / "status.json"
            print(
                status.read_text(encoding="utf-8")
                if status.exists()
                else "Пока не запущено; выполни setup."
            )
            if (root / "config.json").exists():
                cfg = Config.load(root)
                print(
                    f"Подключено чатов: {len(cfg.chat_configs())}; список: tgstudy chats list"
                )
                print(f"Наблюдатель запущен: {instance_running(root, 'watcher')}")
                print(f"Обработчик запущен: {instance_running(root, 'worker')}")
                print(f"Telegram найден: {telegram_running(cfg.process_names)}")
                print(
                    f"Распознавание: {'auto/multilingual' if cfg.multilingual else cfg.language or 'auto/single'}; модель: {cfg.model}"
                )
                print(
                    f"Языки auto: {', '.join(cfg.speech_languages)}; фрагменты: {cfg.speech_chunk_seconds} сек."
                )
                print(
                    f"Обычные видео: {'on' if cfg.transcribe_videos else 'off'}; кэш: {cfg.media_cache_days} дней / {cfg.media_cache_max_mb} MB"
                )
        elif args.cmd == "sync":
            cfg = Config.load(root)
            if args.chat_id is not None:
                cfg = cfg.select_chat(args.chat_id)
            print("Догрузка сообщений из Telegram…", flush=True)
            with maintenance(root):
                count = asyncio.run(sync_now(root, cfg))
            print(
                f"История синхронизирована; сообщений после контрольной точки: {count}."
            )
        elif args.cmd == "speech":
            cfg = Config.load(root)
            cfg.multilingual = args.language == "auto"
            cfg.language = None if cfg.multilingual else args.language
            if args.model:
                cfg.model = args.model
            if args.languages:
                if not cfg.multilingual:
                    parser.error("--languages применяется только к speech auto")
                cfg.speech_languages = [s.strip() for s in args.languages.split(",")]
            if cfg.multilingual and cfg.model in {
                "tiny.en",
                "base.en",
                "small.en",
                "medium.en",
            }:
                cfg.model = cfg.model.removesuffix(".en")
            with maintenance(root):
                cfg.save(root)
            print(
                "Настройки распознавания сохранены. Для старых расшифровок используй retranscribe."
            )
        elif args.cmd == "videos":
            cfg = Config.load(root)
            cfg.transcribe_videos = args.policy == "on"
            with maintenance(root):
                cfg.save(root)
                store = Store(root / "journal.sqlite3")
                try:
                    count = 0
                    for scoped in cfg.chat_configs():
                        if not cfg.transcribe_videos:
                            count += store.exclude_videos(scoped.chat_id)
                        export_dirty(store, scoped)
                finally:
                    store.close()
            print(
                f"Обычные видео: {args.policy}. Старых ASR-результатов исключено: {count}."
            )
        elif args.cmd == "retranscribe":
            cfg = Config.load(root)
            if args.chat_id is not None:
                cfg = cfg.select_chat(args.chat_id)
            elif args.message_id is not None and cfg.extra_chats:
                parser.error(
                    "С --message-id укажи --chat-id: номера сообщений повторяются в разных чатах"
                )
            if args.message_id is not None and args.message_id <= 0:
                parser.error("--message-id должен быть положительным")
            days = 1 if args.today else args.days or 0
            _, _, start, end = day_window(cfg.timezone, days)
            with maintenance(root):
                store = Store(root / "journal.sqlite3")
                try:
                    count = 0
                    for scoped in cfg.chat_configs():
                        if not scoped.transcribe_videos:
                            store.exclude_videos(scoped.chat_id)
                        count += store.retranscribe(
                            scoped.chat_id,
                            start,
                            end,
                            unexpected_languages=cfg.speech_languages
                            if args.unexpected_languages
                            else None,
                            message_id=args.message_id,
                        )
                        export_dirty(store, scoped)
                finally:
                    store.close()
            print(
                f"Записей поставлено на повторную расшифровку: {count}. Оставь Telegram открытым."
            )
        elif args.cmd == "mode":
            cfg = Config.load(root)
            set_period(cfg, args.mode, args.days)
            print(
                "Применение режима; обработчик при необходимости будет перезапущен…",
                flush=True,
            )
            with maintenance(root):
                save_mode(root, cfg)
            print(
                f"Режим: {cfg.export_mode}. Количество дней: {1 if cfg.export_mode == 'daily' else cfg.history_days}."
            )
        elif args.cmd in {"export", "retry"}:
            cfg = Config.load(root)
            if getattr(args, "chat_id", None) is not None:
                cfg = cfg.select_chat(args.chat_id)
            if args.cmd == "export" and (args.days is not None or args.today):
                days = 1 if args.today else args.days
                if days < 0:
                    parser.error("--days должен быть >= 0")
                print(
                    "Выгрузка из Telegram и расшифровка; длительность зависит от объёма записей…",
                    flush=True,
                )
                with maintenance(root):
                    for scoped in cfg.chat_configs():
                        path, count, errors = asyncio.run(
                            export_history(root, scoped, days)
                        )
                        print(
                            f"Выгружено сообщений: {count}; ошибок расшифровки: {errors}. Файл: {path}"
                        )
                return
            with maintenance(root):
                store = Store(root / "journal.sqlite3")
                try:
                    for scoped in cfg.chat_configs():
                        if args.cmd == "retry":
                            store.retry_errors(scoped.chat_id)
                            print(f"{scoped.chat_title}: ошибки поставлены в очередь.")
                        else:
                            store.mark_all_dirty(scoped.chat_id)
                            print(
                                f"{scoped.chat_title}: пересоздано дней: {export_dirty(store, scoped)}"
                            )
                finally:
                    store.close()
        else:
            parser.error("Неизвестная команда")
    except portalocker.exceptions.LockException:
        if args.cmd in {"watch", "worker"}:
            return  # An existing instance owns the session/journal.
        print(
            "Модуль уже работает. Выполни tgstudy uninstall и дождись остановки (до минуты).",
            file=sys.stderr,
        )
        raise SystemExit(2)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        name = type(exc).__name__
        logging.getLogger("tgstudy").error("Command %s failed: %s", args.cmd, name)
        if args.cmd == "worker":
            status_write(root, state="worker_error", error=name)
        print(
            f"Не удалось выполнить {args.cmd}: {name}. Проверь README и настройки.",
            file=sys.stderr,
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
