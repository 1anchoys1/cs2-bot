#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CS2 Skin Price Tracker Bot — v2.0
Облачная версия: SQLite + Steam Market с fallback на CSFloat API.
Валюта: UAH (гривны), конвертация через exchangerate-api.
"""

import os
import time
import logging
import sqlite3
import threading
import statistics
import requests
from datetime import datetime, timedelta, timezone
from telebot import TeleBot, types

# ─────────────────────────────────────────────────────────────
#  НАСТРОЙКИ — меняйте только здесь или через переменные среды
# ─────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")
USER_ID   = int(os.getenv("USER_ID", "0"))  # Ваш Telegram ID

# Скины: название → порог уведомления (% изменения за 24ч)
SKINS_TO_TRACK: dict[str, float] = {
    "M4A4 | Evil Daimyo (Minimal Wear)":          5.0,
    "M4A4 | Zubastick (Minimal Wear)":           5.0,
    "M4A4 | Magnesium (Minimal Wear)":             5.0,
    "AK-47 | Rat Rod (Well-Worn)":                 5.0,
    "Desert Eagle | Mulberry (Field-Tested)":   5.0,
    "M4A1-S | Flashback (Field-Tested)":           5.0,
}

CHECK_INTERVAL_HOURS = 6       # Как часто проверять цены
DB_FILE = "cs2_prices.db"      # SQLite база; в облаке монтируйте volume или используйте /tmp
HISTORY_DAYS = 30              # Сколько дней хранить историю
USD_UAH_FALLBACK = 41.5        # Курс на случай если API конвертации недоступен

# ─────────────────────────────────────────────────────────────
#  ЛОГИРОВАНИЕ
# ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("cs2bot")

# ─────────────────────────────────────────────────────────────
#  БАЗА ДАННЫХ
# ─────────────────────────────────────────────────────────────

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def db_init() -> None:
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS prices (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                skin      TEXT    NOT NULL,
                price_usd REAL    NOT NULL,
                source    TEXT    NOT NULL DEFAULT 'steam',
                ts        TEXT    NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_skin_ts ON prices(skin, ts)")
    log.info("База данных инициализирована.")

def db_insert(skin: str, price_usd: float, source: str = "steam") -> None:
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO prices (skin, price_usd, source, ts) VALUES (?,?,?,?)",
            (skin, price_usd, source, datetime.now(timezone.utc).isoformat()),
        )

def db_history(skin: str, days: int) -> list[float]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT price_usd FROM prices WHERE skin=? AND ts>=? ORDER BY ts",
            (skin, cutoff),
        ).fetchall()
    return [r["price_usd"] for r in rows]

def db_last_price(skin: str) -> float | None:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT price_usd FROM prices WHERE skin=? ORDER BY ts DESC LIMIT 1",
            (skin,),
        ).fetchone()
    return row["price_usd"] if row else None

def db_purge_old() -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)).isoformat()
    with db_connect() as conn:
        conn.execute("DELETE FROM prices WHERE ts<?", (cutoff,))
    log.info("Старые записи удалены.")

def db_count(skin: str) -> int:
    with db_connect() as conn:
        row = conn.execute("SELECT COUNT(*) as c FROM prices WHERE skin=?", (skin,)).fetchone()
    return row["c"]

# ─────────────────────────────────────────────────────────────
#  КОНВЕРТАЦИЯ ВАЛЮТ
# ─────────────────────────────────────────────────────────────

_uah_rate_cache: dict = {"rate": USD_UAH_FALLBACK, "ts": 0.0}

def get_usd_to_uah() -> float:
    """Кешируем курс на 1 час."""
    if time.time() - _uah_rate_cache["ts"] < 3600:
        return _uah_rate_cache["rate"]
    try:
        r = requests.get(
            "https://api.exchangerate-api.com/v4/latest/USD",
            timeout=5,
        )
        rate = r.json()["rates"]["UAH"]
        _uah_rate_cache.update({"rate": rate, "ts": time.time()})
        log.info(f"Курс USD/UAH обновлён: {rate:.2f}")
        return rate
    except Exception as e:
        log.warning(f"Не удалось получить курс UAH: {e}. Используем {USD_UAH_FALLBACK}")
        return USD_UAH_FALLBACK

def usd_to_uah(usd: float) -> float:
    return round(usd * get_usd_to_uah(), 2)

# ─────────────────────────────────────────────────────────────
#  ПОЛУЧЕНИЕ ЦЕН: Steam → CSFloat (fallback)
# ─────────────────────────────────────────────────────────────

STEAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

def _fetch_steam_price(skin: str) -> float | None:
    """Запрашивает цену с Steam Market. Возвращает USD float или None."""
    import urllib.parse
    url = (
        "https://steamcommunity.com/market/priceoverview/"
        f"?currency=1&appid=730&market_hash_name={urllib.parse.quote(skin)}"
    )
    try:
        r = requests.get(url, headers=STEAM_HEADERS, timeout=12)
        if r.status_code == 429:
            log.warning(f"Steam rate-limit для '{skin}', ждём 30 сек...")
            time.sleep(30)
            r = requests.get(url, headers=STEAM_HEADERS, timeout=12)
        if r.status_code != 200:
            log.warning(f"Steam HTTP {r.status_code} для '{skin}'")
            return None
        data = r.json()
        if not data.get("success"):
            return None
        raw = data.get("lowest_price") or data.get("median_price") or ""
        # Убираем символ валюты и пробелы, нормализуем дробный разделитель
        clean = raw.replace("$", "").replace("€", "").replace(",", ".").strip().split()[0]
        return float(clean)
    except Exception as e:
        log.warning(f"Steam ошибка для '{skin}': {e}")
        return None


def _fetch_csfloat_price(skin: str) -> float | None:
    """
    Fallback: CSFloat public listings API.
    Берём минимальную цену из первых 5 активных листингов.
    """
    import urllib.parse
    url = (
        "https://csfloat.com/api/v1/listings"
        f"?market_hash_name={urllib.parse.quote(skin)}&limit=5&sort_by=price&order=asc"
    )
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        listings = data.get("data") or data if isinstance(data, list) else []
        prices = [
            item["price"] / 100  # CSFloat возвращает центы
            for item in listings
            if isinstance(item, dict) and item.get("price")
        ]
        if prices:
            p = min(prices)
            log.info(f"CSFloat цена для '{skin}': ${p:.4f}")
            return p
        return None
    except Exception as e:
        log.warning(f"CSFloat ошибка для '{skin}': {e}")
        return None


def fetch_price(skin: str) -> tuple[float | None, str]:
    """
    Возвращает (цена_USD, источник).
    Сначала пробует Steam, при ошибке — CSFloat.
    """
    price = _fetch_steam_price(skin)
    if price is not None and price > 0:
        return price, "steam"

    log.info(f"Steam не дал цену для '{skin}', пробуем CSFloat...")
    price = _fetch_csfloat_price(skin)
    if price is not None and price > 0:
        return price, "csfloat"

    return None, "none"


def fetch_all_prices() -> dict[str, tuple[float, str]]:
    """
    Получает цены для всех скинов.
    Возвращает {skin: (price_usd, source)}.
    Пауза 3 сек между запросами к Steam, чтобы не словить бан.
    """
    results: dict[str, tuple[float, str]] = {}
    for i, skin in enumerate(SKINS_TO_TRACK):
        price, source = fetch_price(skin)
        if price:
            results[skin] = (price, source)
        if i < len(SKINS_TO_TRACK) - 1:
            time.sleep(3)  # вежливая пауза между запросами
    return results

# ─────────────────────────────────────────────────────────────
#  СТАТИСТИКА И АНАЛИЗ
# ─────────────────────────────────────────────────────────────

def calc_stats(skin: str, current_usd: float) -> dict:
    h24  = db_history(skin, 1)
    h7d  = db_history(skin, 7)
    h30d = db_history(skin, 30)

    def safe_mean(lst): return statistics.mean(lst) if lst else None
    def pct_change(new, old):
        if not old: return 0.0
        return round((new - old) / old * 100, 2)

    avg24  = safe_mean(h24)
    avg7d  = safe_mean(h7d)
    avg30d = safe_mean(h30d)

    rate = get_usd_to_uah()
    return {
        "usd":       round(current_usd, 4),
        "uah":       round(current_usd * rate, 2),
        "avg24":     avg24,
        "avg7d":     avg7d,
        "avg30d":    avg30d,
        "pct24":     pct_change(current_usd, avg24)  if avg24  else None,
        "pct7d":     pct_change(current_usd, avg7d)  if avg7d  else None,
        "pct30d":    pct_change(current_usd, avg30d) if avg30d else None,
        "uah_rate":  round(rate, 2),
        "points":    len(h30d),
    }

# ─────────────────────────────────────────────────────────────
#  ФОРМАТИРОВАНИЕ СООБЩЕНИЙ
# ─────────────────────────────────────────────────────────────

def _trend(pct: float | None) -> str:
    if pct is None: return "➡️ нет данных"
    icon = "📈" if pct > 0 else ("📉" if pct < 0 else "➡️")
    sign = "+" if pct > 0 else ""
    return f"{icon} {sign}{pct:.2f}%"

def build_card(skin: str, stats: dict, source: str = "steam") -> str:
    src_tag = "🟩 Steam" if source == "steam" else "🟦 CSFloat"
    parts = [
        f"🔫 <b>{skin}</b>",
        f"",
        f"💰 Цена:  <code>₴{stats['uah']:.2f}</code>  <i>(${stats['usd']:.4f})</i>",
        f"💱 Курс USD/UAH: <code>{stats['uah_rate']:.2f}</code>",
        f"📡 Источник: {src_tag}",
        f"",
    ]
    if stats["avg24"] is not None:
        parts.append(f"24ч:  {_trend(stats['pct24'])}  (ср. ₴{stats['avg24']*stats['uah_rate']:.2f})")
    if stats["avg7d"] is not None:
        parts.append(f" 7д:  {_trend(stats['pct7d'])}  (ср. ₴{stats['avg7d']*stats['uah_rate']:.2f})")
    if stats["avg30d"] is not None:
        parts.append(f"30д:  {_trend(stats['pct30d'])}  (ср. ₴{stats['avg30d']*stats['uah_rate']:.2f})")
    parts += [
        f"",
        f"📈 Точек в базе: {stats['points']}",
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC",
    ]
    return "\n".join(parts)

def build_alert(skin: str, stats: dict, threshold: float, source: str) -> str:
    direction = "🚀 РОСТ" if (stats["pct24"] or 0) > 0 else "🔻 ПАДЕНИЕ"
    return (
        f"🚨 <b>ЦЕНОВОЕ УВЕДОМЛЕНИЕ — {direction}</b>\n\n"
        f"🔫 <b>{skin}</b>\n\n"
        f"💰 Сейчас:  <code>₴{stats['uah']:.2f}</code>  (${stats['usd']:.4f})\n"
        f"📊 Изменение 24ч: {_trend(stats['pct24'])}\n"
        f"⚡ Порог: ±{threshold}%\n"
        f"📡 {'Steam' if source == 'steam' else 'CSFloat'}"
    )

# ─────────────────────────────────────────────────────────────
#  ОСНОВНАЯ ПРОВЕРКА ЦЕН
# ─────────────────────────────────────────────────────────────

def run_price_check(bot: TeleBot, notify: bool = True) -> str:
    """
    Запрашивает цены, сохраняет в БД, отправляет уведомления.
    Возвращает краткий текстовый отчёт.
    """
    log.info("⏱ Запуск проверки цен...")
    prices = fetch_all_prices()

    if not prices:
        msg = "❌ Не удалось получить цены ни с одного источника."
        log.error(msg)
        return msg

    report_lines = [f"🔄 Проверка завершена — {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n"]

    for skin, threshold in SKINS_TO_TRACK.items():
        if skin not in prices:
            report_lines.append(f"⚠️ {skin[:35]}… — нет данных")
            continue

        price_usd, source = prices[skin]
        db_insert(skin, price_usd, source)

        stats = calc_stats(skin, price_usd)
        uah = stats["uah"]
        pct = stats.get("pct24")

        line = f"• {skin[:35]}… — ₴{uah:.2f}"
        if pct is not None:
            sign = "+" if pct > 0 else ""
            line += f"  ({sign}{pct:.1f}%)"
        report_lines.append(line)

        # Уведомление если порог превышен
        if notify and pct is not None and abs(pct) >= threshold and USER_ID:
            try:
                bot.send_message(USER_ID, build_alert(skin, stats, threshold, source), parse_mode="HTML")
            except Exception as e:
                log.error(f"Не удалось отправить уведомление: {e}")

    db_purge_old()
    log.info("✅ Проверка цен завершена.")
    return "\n".join(report_lines)

# ─────────────────────────────────────────────────────────────
#  ПЛАНИРОВЩИК
# ─────────────────────────────────────────────────────────────

def start_scheduler(bot: TeleBot) -> None:
    interval = CHECK_INTERVAL_HOURS * 3600

    def loop():
        # Первый запуск через минуту после старта
        time.sleep(60)
        while True:
            try:
                run_price_check(bot)
            except Exception as e:
                log.error(f"Ошибка планировщика: {e}", exc_info=True)
            log.info(f"Следующая проверка через {CHECK_INTERVAL_HOURS}ч.")
            time.sleep(interval)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    log.info(f"Планировщик запущен (интервал: {CHECK_INTERVAL_HOURS}ч).")

# ─────────────────────────────────────────────────────────────
#  TELEGRAM HANDLERS
# ─────────────────────────────────────────────────────────────

def register_handlers(bot: TeleBot) -> None:
    skin_list = list(SKINS_TO_TRACK.keys())

    # ── /start, /help ─────────────────────────────────────────
    @bot.message_handler(commands=["start", "help"])
    def cmd_start(msg: types.Message):
        text = (
            "👋 <b>CS2 Price Tracker</b>\n\n"
            "Слежу за ценами на скины и уведомляю о значимых движениях.\n"
            "Цены в <b>гривнах (UAH)</b>, курс обновляется каждый час.\n\n"
            "<b>Команды:</b>\n"
            "/prices — текущие цены всех скинов\n"
            "/skin — выбрать скин и смотреть детально\n"
            "/check — запустить проверку прямо сейчас\n"
            "/status — состояние бота и статистика БД\n"
        )
        bot.send_message(msg.chat.id, text, parse_mode="HTML")

    # ── /prices — быстрый обзор всех скинов ──────────────────
    @bot.message_handler(commands=["prices"])
    def cmd_prices(msg: types.Message):
        wait = bot.send_message(msg.chat.id, "⏳ Запрашиваю цены, подождите...")
        prices = fetch_all_prices()
        rate = get_usd_to_uah()

        if not prices:
            bot.edit_message_text("❌ Не удалось получить цены.", msg.chat.id, wait.message_id)
            return

        lines = [f"💹 <b>Текущие цены</b>  (1$ = ₴{rate:.2f})\n"]
        for skin in skin_list:
            if skin not in prices:
                lines.append(f"⚠️ {skin} — нет данных")
                continue
            p_usd, src = prices[skin]
            uah = usd_to_uah(p_usd)
            icon = "🟩" if src == "steam" else "🟦"
            lines.append(f"{icon} {skin}\n   <code>₴{uah:.2f}</code>  (${p_usd:.4f})")

        lines.append(f"\n🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
        bot.edit_message_text("\n".join(lines), msg.chat.id, wait.message_id, parse_mode="HTML")

    # ── /skin — детальная карточка ────────────────────────────
    @bot.message_handler(commands=["skin"])
    def cmd_skin(msg: types.Message):
        markup = types.InlineKeyboardMarkup(row_width=1)
        for i, skin in enumerate(skin_list):
            label = skin if len(skin) <= 42 else skin[:39] + "…"
            markup.add(types.InlineKeyboardButton(f"🔫 {label}", callback_data=f"s:{i}"))
        bot.send_message(
            msg.chat.id,
            "📋 Выберите скин:",
            reply_markup=markup,
        )

    @bot.callback_query_handler(func=lambda c: c.data.startswith("s:"))
    def cb_skin(call: types.CallbackQuery):
        bot.answer_callback_query(call.id, "⏳ Загружаю...")
        try:
            idx = int(call.data.split(":")[1])
            skin = skin_list[idx]
        except Exception:
            bot.send_message(call.message.chat.id, "❌ Неверный запрос.")
            return

        price_usd, source = fetch_price(skin)
        if price_usd is None:
            bot.send_message(call.message.chat.id, f"❌ Не удалось получить цену для <b>{skin}</b>.", parse_mode="HTML")
            return

        stats = calc_stats(skin, price_usd)
        card = build_card(skin, stats, source)

        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("🔄 Обновить", callback_data=f"s:{idx}"),
            types.InlineKeyboardButton("◀ Назад", callback_data="menu"),
        )
        try:
            bot.edit_message_text(card, call.message.chat.id, call.message.message_id,
                                  parse_mode="HTML", reply_markup=markup)
        except Exception:
            bot.send_message(call.message.chat.id, card, parse_mode="HTML", reply_markup=markup)

    @bot.callback_query_handler(func=lambda c: c.data == "menu")
    def cb_menu(call: types.CallbackQuery):
        bot.answer_callback_query(call.id)
        markup = types.InlineKeyboardMarkup(row_width=1)
        for i, skin in enumerate(skin_list):
            label = skin if len(skin) <= 42 else skin[:39] + "…"
            markup.add(types.InlineKeyboardButton(f"🔫 {label}", callback_data=f"s:{i}"))
        try:
            bot.edit_message_text("📋 Выберите скин:", call.message.chat.id,
                                  call.message.message_id, reply_markup=markup)
        except Exception:
            bot.send_message(call.message.chat.id, "📋 Выберите скин:", reply_markup=markup)

    # ── /check — ручной запуск ────────────────────────────────
    @bot.message_handler(commands=["check"])
    def cmd_check(msg: types.Message):
        bot.send_message(msg.chat.id, "🔄 Запускаю проверку цен...")
        def do_check():
            report = run_price_check(bot, notify=False)
            bot.send_message(msg.chat.id, report, parse_mode="HTML")
        threading.Thread(target=do_check, daemon=True).start()

    # ── /status ───────────────────────────────────────────────
    @bot.message_handler(commands=["status"])
    def cmd_status(msg: types.Message):
        rate = get_usd_to_uah()
        lines = [
            f"📊 <b>Статус бота</b>\n",
            f"💱 Курс USD/UAH: <code>{rate:.2f}</code>",
            f"⏱ Интервал проверки: каждые {CHECK_INTERVAL_HOURS}ч",
            f"",
            "<b>Записей в базе:</b>",
        ]
        for skin in skin_list:
            cnt = db_count(skin)
            last = db_last_price(skin)
            last_str = f"₴{usd_to_uah(last):.2f}" if last else "нет"
            short = skin[:38] + "…" if len(skin) > 38 else skin
            lines.append(f"• {short}: {cnt} зап., последняя {last_str}")
        bot.send_message(msg.chat.id, "\n".join(lines), parse_mode="HTML")


# ─────────────────────────────────────────────────────────────
#  ЗАПУСК
# ─────────────────────────────────────────────────────────────

def main():
    if BOT_TOKEN == "PASTE_YOUR_TOKEN_HERE" or not BOT_TOKEN:
        log.error("❌ Укажите BOT_TOKEN в переменной среды или прямо в коде!")
        return
    if not USER_ID:
        log.warning("⚠️ USER_ID не задан — уведомления не будут отправляться.")

    db_init()
    bot = TeleBot(BOT_TOKEN, parse_mode=None)
    register_handlers(bot)
    start_scheduler(bot)

    log.info("🤖 Бот запущен. Ожидаем команды...")
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=10)
        except Exception as e:
            log.error(f"Polling ошибка: {e}. Перезапуск через 10 сек...")
            time.sleep(10)


if __name__ == "__main__":
    main()
