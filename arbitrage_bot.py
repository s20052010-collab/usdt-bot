#!/usr/bin/env python3
"""
БОТ 2: P2P АРБИТРАЖ USDT
Мониторит курсы USDT на Binance/Bybit P2P
Ищет выгодные связки BUY/SELL
Отправляет сигналы в Telegram
ТОЛЬКО НАБЛЮДЕНИЕ — без автоматических сделок
"""

import asyncio
import aiohttp
import logging
import os
import json
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════
# КОНФИГУРАЦИЯ
# ═══════════════════════════════════════════════
CONFIG = {
    "min_margin_pct": 1.0,        # Минимальная маржа для сигнала (%)
    "check_interval": 300,         # Интервал проверки (секунды) = 5 минут
    "currencies": ["KZT", "AED", "TRY", "INR", "UZS"],
    "test_amounts": [500, 1000, 5000],  # Объёмы в USDT для расчёта
    "alert_chat_id": None,         # Заполнить после запуска
}

# ═══════════════════════════════════════════════
# ПОЛУЧЕНИЕ ДАННЫХ С BINANCE P2P
# ═══════════════════════════════════════════════
async def get_binance_p2p(trade_type: str, fiat: str, amount: int = 1000) -> list:
    """
    trade_type: BUY = мы покупаем USDT (ищем продавца)
                SELL = мы продаём USDT (ищем покупателя)
    """
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    payload = {
        "asset": "USDT",
        "fiat": fiat,
        "merchantCheck": False,
        "page": 1,
        "publisherType": None,
        "rows": 10,
        "tradeType": trade_type,
        "transAmount": amount
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    ads = data.get("data", [])
                    result = []
                    for ad in ads[:5]:
                        info = ad.get("adv", {})
                        advertiser = ad.get("advertiser", {})
                        result.append({
                            "price": float(info.get("price", 0)),
                            "min_amount": float(info.get("minSingleTransAmount", 0)),
                            "max_amount": float(info.get("dynamicMaxSingleTransAmount", 0)),
                            "nick": advertiser.get("nickName", "Unknown"),
                            "orders": advertiser.get("monthOrderCount", 0),
                            "completion": advertiser.get("monthFinishRate", 0),
                            "payment": [m.get("identifier", "") for m in info.get("tradeMethods", [])],
                        })
                    return result
    except Exception as e:
        logger.error(f"Binance P2P error {fiat}: {e}")
    return []

async def get_bybit_p2p(trade_type: str, fiat: str) -> list:
    """Получить данные с Bybit P2P"""
    url = "https://api2.bybit.com/fiat/otc/item/online"
    side = "1" if trade_type == "BUY" else "0"
    payload = {
        "tokenId": "USDT",
        "currencyId": fiat,
        "payment": [],
        "side": side,
        "size": "10",
        "page": "1",
        "amount": ""
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    items = data.get("result", {}).get("items", [])
                    result = []
                    for item in items[:5]:
                        result.append({
                            "price": float(item.get("price", 0)),
                            "min_amount": float(item.get("minAmount", 0)),
                            "max_amount": float(item.get("maxAmount", 0)),
                            "nick": item.get("nickName", "Unknown"),
                            "orders": item.get("recentOrderNum", 0),
                            "completion": float(item.get("recentExecuteRate", 0)),
                            "payment": item.get("payments", []),
                        })
                    return result
    except Exception as e:
        logger.error(f"Bybit P2P error {fiat}: {e}")
    return []

# ═══════════════════════════════════════════════
# РАСЧЁТ АРБИТРАЖА
# ═══════════════════════════════════════════════
def calculate_arbitrage(buy_price: float, sell_price: float, amount_usdt: int, fiat: str) -> dict:
    """
    buy_price = цена по которой мы ПОКУПАЕМ USDT (платим фиат)
    sell_price = цена по которой мы ПРОДАЁМ USDT (получаем фиат)
    Прибыль = sell - buy (в единицах фиата на 1 USDT)
    """
    if buy_price <= 0 or sell_price <= 0:
        return {}

    gross_diff = sell_price - buy_price
    gross_margin_pct = (gross_diff / buy_price) * 100

    # Расходы
    exchange_fee_pct = 0.1       # Биржевая комиссия ~0.1%
    bank_fee_pct = 0.3           # Банковский перевод ~0.3%
    spread_risk_pct = 0.2        # Риск изменения курса
    total_costs_pct = exchange_fee_pct + bank_fee_pct + spread_risk_pct

    net_margin_pct = gross_margin_pct - total_costs_pct
    net_profit_per_usdt = gross_diff * (1 - total_costs_pct/100)
    net_profit_total = net_profit_per_usdt * amount_usdt

    return {
        "buy_price": buy_price,
        "sell_price": sell_price,
        "fiat": fiat,
        "amount_usdt": amount_usdt,
        "gross_margin_pct": round(gross_margin_pct, 2),
        "net_margin_pct": round(net_margin_pct, 2),
        "net_profit_fiat": round(net_profit_total, 2),
        "profitable": net_margin_pct >= CONFIG["min_margin_pct"]
    }

# ═══════════════════════════════════════════════
# ФОРМАТИРОВАНИЕ СИГНАЛА
# ═══════════════════════════════════════════════
def format_signal(arb: dict, buy_ad: dict, sell_ad: dict, exchange_buy: str, exchange_sell: str) -> str:
    profit_icon = "🟢" if arb["profitable"] else "🔴"
    return (
        f"{profit_icon} *СИГНАЛ АРБИТРАЖА — {arb['fiat']}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Объём: *{arb['amount_usdt']} USDT*\n\n"
        f"📥 *КУПИТЬ USDT* ({exchange_buy})\n"
        f"   Цена: `{arb['buy_price']} {arb['fiat']}`\n"
        f"   Продавец: {buy_ad.get('nick', '?')}\n"
        f"   Сделок: {buy_ad.get('orders', '?')}\n\n"
        f"📤 *ПРОДАТЬ USDT* ({exchange_sell})\n"
        f"   Цена: `{arb['sell_price']} {arb['fiat']}`\n"
        f"   Покупатель: {sell_ad.get('nick', '?')}\n"
        f"   Сделок: {sell_ad.get('orders', '?')}\n\n"
        f"💰 *РАСЧЁТ:*\n"
        f"   Валовая маржа: {arb['gross_margin_pct']}%\n"
        f"   Чистая маржа: *{arb['net_margin_pct']}%*\n"
        f"   Прибыль: *{arb['net_profit_fiat']} {arb['fiat']}*\n\n"
        f"⚠️ *ПРАВИЛА:*\n"
        f"   • Проверить имя плательщика\n"
        f"   • Дождаться фактического зачисления\n"
        f"   • Сделку НЕ автоматизировать\n\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S %d.%m.%Y')}"
    )

# ═══════════════════════════════════════════════
# МОНИТОРИНГ
# ═══════════════════════════════════════════════
async def scan_opportunities(app) -> list:
    """Сканировать все валюты и найти возможности"""
    signals = []
    for fiat in CONFIG["currencies"]:
        try:
            # Получаем данные с обеих бирж
            binance_buy = await get_binance_p2p("BUY", fiat)   # Лучшие продавцы USDT
            binance_sell = await get_binance_p2p("SELL", fiat)  # Лучшие покупатели USDT
            bybit_buy = await get_bybit_p2p("BUY", fiat)
            bybit_sell = await get_bybit_p2p("SELL", fiat)

            all_buys = [(a, "Binance") for a in binance_buy] + [(a, "Bybit") for a in bybit_buy]
            all_sells = [(a, "Binance") for a in binance_sell] + [(a, "Bybit") for a in bybit_sell]

            if not all_buys or not all_sells:
                continue

            # Лучшая цена покупки (минимальная)
            best_buy = min(all_buys, key=lambda x: x[0]["price"])
            # Лучшая цена продажи (максимальная)
            best_sell = max(all_sells, key=lambda x: x[0]["price"])

            for amount in CONFIG["test_amounts"]:
                arb = calculate_arbitrage(
                    best_buy[0]["price"],
                    best_sell[0]["price"],
                    amount,
                    fiat
                )
                if arb and arb["profitable"]:
                    signal_text = format_signal(
                        arb, best_buy[0], best_sell[0],
                        best_buy[1], best_sell[1]
                    )
                    signals.append({"text": signal_text, "arb": arb})

            await asyncio.sleep(1)  # Пауза между запросами
        except Exception as e:
            logger.error(f"Error scanning {fiat}: {e}")

    return signals

async def monitor_job(context: ContextTypes.DEFAULT_TYPE):
    """Периодический мониторинг"""
    if not CONFIG["alert_chat_id"]:
        return
    try:
        signals = await scan_opportunities(context.application)
        if signals:
            for signal in signals[:3]:  # Максимум 3 сигнала за раз
                await context.bot.send_message(
                    chat_id=CONFIG["alert_chat_id"],
                    text=signal["text"],
                    parse_mode="Markdown"
                )
        else:
            # Тихий режим — нет прибыльных связок
            logger.info(f"Сканирование завершено. Прибыльных связок нет. {datetime.now()}")
    except Exception as e:
        logger.error(f"Monitor job error: {e}")

# ═══════════════════════════════════════════════
# КОМАНДЫ БОТА
# ═══════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CONFIG["alert_chat_id"] = update.effective_chat.id
    text = (
        "₿ *P2P АРБИТРАЖ USDT — МОНИТОР*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔍 Мониторю курсы USDT на Binance и Bybit P2P\n"
        "📊 Ищу связки с маржой ≥1% после всех расходов\n"
        "⚠️ Только наблюдение — сделки вручную\n\n"
        f"🌍 *Валюты:* {', '.join(CONFIG['currencies'])}\n"
        f"💰 *Объёмы:* {', '.join(map(str, CONFIG['test_amounts']))} USDT\n"
        f"⏱ *Интервал:* каждые {CONFIG['check_interval']//60} минут\n\n"
        "Используй команды:"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Сканировать сейчас", callback_data="scan_now")],
        [InlineKeyboardButton("📊 Текущие курсы", callback_data="show_rates")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
        [InlineKeyboardButton("📚 Правила безопасности", callback_data="safety")],
    ])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def scan_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Сканирую... Подожди 30-60 секунд")
    signals = await scan_opportunities(context.application)
    if signals:
        await update.message.reply_text(f"✅ Найдено {len(signals)} прибыльных связок:")
        for signal in signals[:5]:
            await update.message.reply_text(signal["text"], parse_mode="Markdown")
    else:
        text = (
            "😔 *Прибыльных связок не найдено*\n\n"
            "Текущие условия не дают чистую маржу ≥1%\n\n"
            "Это нормально — рынок P2P конкурентный.\n"
            "Бот продолжает мониторинг каждые 5 минут."
        )
        await update.message.reply_text(text, parse_mode="Markdown")

async def show_rates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Получаю текущие курсы...")
    text = "📊 *ТЕКУЩИЕ КУРСЫ P2P (BINANCE)*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for fiat in CONFIG["currencies"][:3]:  # Первые 3 валюты
        try:
            buy_ads = await get_binance_p2p("BUY", fiat)
            sell_ads = await get_binance_p2p("SELL", fiat)
            if buy_ads and sell_ads:
                best_buy = min(buy_ads, key=lambda x: x["price"])
                best_sell = max(sell_ads, key=lambda x: x["price"])
                spread = ((best_sell["price"] - best_buy["price"]) / best_buy["price"]) * 100
                text += (
                    f"*{fiat}*\n"
                    f"  📥 Купить USDT: `{best_buy['price']}`\n"
                    f"  📤 Продать USDT: `{best_sell['price']}`\n"
                    f"  📈 Спред: `{spread:.2f}%`\n\n"
                )
            await asyncio.sleep(1)
        except Exception as e:
            text += f"*{fiat}*: Ошибка получения данных\n\n"
    text += f"🕐 {datetime.now().strftime('%H:%M:%S')}"
    await update.message.reply_text(text, parse_mode="Markdown")

async def safety_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🛡 *ПРАВИЛА БЕЗОПАСНОСТИ — ОБЯЗАТЕЛЬНО*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "✅ *ВСЕГДА:*\n"
        "• Дождаться фактического зачисления денег в банке\n"
        "• Проверить что имя отправителя = имя на бирже\n"
        "• Работать только через escrow биржи\n"
        "• Хранить историю всех операций\n\n"
        "❌ *НИКОГДА:*\n"
        "• Не отпускать USDT по скрину перевода\n"
        "• Не принимать деньги от третьих лиц\n"
        "• Не работать через Telegram-обменники\n"
        "• Не делать сделки без верификации контрагента\n"
        "• Не вкладывать последние деньги\n\n"
        "⚠️ *ЛИМИТЫ НА СТАРТЕ:*\n"
        "• Неделя 1: 1 сделка/день, до 200 USDT\n"
        "• Неделя 2: до 2 сделок/день, до 500 USDT\n"
        "• После 20+ чистых сделок: масштабировать\n\n"
        "💀 *Главный риск:*\n"
        "Фальшивый платёж или платёж от третьего лица\n"
        "= потеря ВСЕЙ суммы сделки"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"⚙️ *ТЕКУЩИЕ НАСТРОЙКИ*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Мин. маржа: `{CONFIG['min_margin_pct']}%`\n"
        f"Интервал: `{CONFIG['check_interval']} сек`\n"
        f"Валюты: `{', '.join(CONFIG['currencies'])}`\n"
        f"Объёмы: `{', '.join(map(str, CONFIG['test_amounts']))} USDT`\n\n"
        f"Для изменения используй:\n"
        f"`/setmargin 1.5` — изменить мин. маржу\n"
        f"`/setinterval 600` — интервал в секундах"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def set_margin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(context.args[0])
        CONFIG["min_margin_pct"] = val
        await update.message.reply_text(f"✅ Минимальная маржа установлена: *{val}%*", parse_mode="Markdown")
    except:
        await update.message.reply_text("❌ Используй: `/setmargin 1.5`", parse_mode="Markdown")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "scan_now":
        await query.edit_message_text("🔍 Сканирую... Подожди 30-60 секунд")
        signals = await scan_opportunities(context.application)
        if signals:
            await query.edit_message_text(f"✅ Найдено {len(signals)} прибыльных связок! Смотри ниже 👇")
            for signal in signals[:3]:
                await query.message.reply_text(signal["text"], parse_mode="Markdown")
        else:
            await query.edit_message_text(
                "😔 Прибыльных связок сейчас нет.\nБот продолжает мониторинг каждые 5 минут.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔍 Повторить", callback_data="scan_now")]])
            )
    elif query.data == "show_rates":
        await query.edit_message_text("📊 Получаю курсы...")
        await show_rates(query, context)
    elif query.data == "safety":
        await safety_rules(query, context)
    elif query.data == "settings":
        await settings_cmd(query, context)

def main():
    TOKEN = os.environ.get("ARBITRAGE_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
    app = Application.builder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("scan", scan_now))
    app.add_handler(CommandHandler("rates", show_rates))
    app.add_handler(CommandHandler("safety", safety_rules))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("setmargin", set_margin))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Периодический мониторинг
    job_queue = app.job_queue
    job_queue.run_repeating(monitor_job, interval=CONFIG["check_interval"], first=60)

    logger.info("P2P Арбитраж бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
