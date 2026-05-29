import asyncio
import aiohttp
import logging
import os
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("ARBITRAGE_BOT_TOKEN", "")
CHAT_ID = None
MIN_MARGIN = 1.0
CURRENCIES = ["KZT", "AED", "TRY", "INR"]

async def send_message(session, text):
    if not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        await session.post(url, json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "Markdown"
        })
    except Exception as e:
        logger.error(f"Send error: {e}")

async def get_updates(session, offset=0):
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    try:
        async with session.get(url, params={"offset": offset, "timeout": 30}) as r:
            data = await r.json()
            return data.get("result", [])
    except:
        return []

async def get_binance_buy(session, fiat):
    """Лучшая цена покупки USDT (минимальная - мы платим)"""
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    try:
        async with session.post(url, json={
            "asset": "USDT", "fiat": fiat,
            "tradeType": "BUY", "page": 1, "rows": 5,
            "merchantCheck": False
        }, headers={"Content-Type": "application/json"}, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                data = await r.json()
                ads = data.get("data", [])
                if ads:
                    prices = [float(a["adv"]["price"]) for a in ads if a.get("adv", {}).get("price")]
                    if prices:
                        return min(prices), ads[0]["advertiser"].get("nickName", "?")
    except Exception as e:
        logger.error(f"Buy {fiat}: {e}")
    return None, None

async def get_binance_sell(session, fiat):
    """Лучшая цена продажи USDT (максимальная - мы получаем)"""
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    try:
        async with session.post(url, json={
            "asset": "USDT", "fiat": fiat,
            "tradeType": "SELL", "page": 1, "rows": 5,
            "merchantCheck": False
        }, headers={"Content-Type": "application/json"}, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                data = await r.json()
                ads = data.get("data", [])
                if ads:
                    prices = [float(a["adv"]["price"]) for a in ads if a.get("adv", {}).get("price")]
                    if prices:
                        return max(prices), ads[0]["advertiser"].get("nickName", "?")
    except Exception as e:
        logger.error(f"Sell {fiat}: {e}")
    return None, None

async def scan(session):
    results = []
    for fiat in CURRENCIES:
        buy_price, buy_nick = await get_binance_buy(session, fiat)
        await asyncio.sleep(1)
        sell_price, sell_nick = await get_binance_sell(session, fiat)
        await asyncio.sleep(1)

        if not buy_price or not sell_price:
            continue

        gross = ((sell_price - buy_price) / buy_price) * 100
        net = gross - 0.6  # комиссии ~0.6%

        results.append({
            "fiat": fiat,
            "buy": buy_price,
            "sell": sell_price,
            "gross": round(gross, 2),
            "net": round(net, 2),
            "profitable": net >= MIN_MARGIN,
            "buy_nick": buy_nick,
            "sell_nick": sell_nick
        })

    return results

def format_rates(results):
    text = f"📊 *КУРСЫ USDT — {datetime.now().strftime('%H:%M')}*\n"
    text += "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for r in results:
        icon = "🟢" if r["profitable"] else "🔴"
        text += f"{icon} *{r['fiat']}*\n"
        text += f"  📥 Купить: `{r['buy']}`\n"
        text += f"  📤 Продать: `{r['sell']}`\n"
        text += f"  💰 Чистая маржа: *{r['net']}%*\n\n"
    return text

def format_signal(r):
    profit_1000 = round((r["sell"] - r["buy"]) * 1000 * 0.994, 2)
    return (
        f"🚨 *СИГНАЛ АРБИТРАЖА — {r['fiat']}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📥 *КУПИТЬ USDT*\n"
        f"   Цена: `{r['buy']} {r['fiat']}`\n"
        f"   У продавца: {r['buy_nick']}\n\n"
        f"📤 *ПРОДАТЬ USDT*\n"
        f"   Цена: `{r['sell']} {r['fiat']}`\n"
        f"   Покупателю: {r['sell_nick']}\n\n"
        f"💰 *Чистая маржа: {r['net']}%*\n"
        f"💵 Прибыль с 1000 USDT: ~{profit_1000} {r['fiat']}\n\n"
        f"⚠️ Проверь имя плательщика!\n"
        f"⚠️ Жди реального зачисления!\n\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S %d.%m.%Y')}"
    )

HELP_TEXT = """
🤖 *USDT АРБИТРАЖ МОНИТОР*
━━━━━━━━━━━━━━━━━━━━━━

Команды:
/start — запустить мониторинг
/scan — сканировать сейчас
/rates — текущие курсы
/safety — правила безопасности
/help — помощь

Бот мониторит Binance P2P каждые 5 минут.
Присылает сигнал когда маржа ≥1%.
"""

SAFETY_TEXT = """
🛡 *ПРАВИЛА БЕЗОПАСНОСТИ*
━━━━━━━━━━━━━━━━━━━━━━

✅ ВСЕГДА:
• Ждать реального зачисления в банке
• Проверять имя отправителя = имя на бирже
• Работать только через escrow биржи

❌ НИКОГДА:
• Не отпускать USDT по скрину
• Не принимать деньги от третьих лиц
• Не делать сделки в Telegram

⚠️ ЛИМИТЫ:
• Неделя 1: 1 сделка/день, до 200 USDT
• Неделя 2: до 2 сделок, до 500 USDT
• После 20+ сделок — масштабировать
"""

async def handle_command(session, text):
    global CHAT_ID
    cmd = text.strip().lower()

    if cmd == "/start":
        await send_message(session,
            "✅ *Мониторинг запущен!*\n\n"
            "Сканирую Binance P2P каждые 5 минут.\n"
            "Пришлю сигнал когда маржа ≥1%\n\n"
            + HELP_TEXT
        )

    elif cmd == "/scan":
        await send_message(session, "🔍 Сканирую... подожди 30 сек")
        results = await scan(session)
        if not results:
            await send_message(session, "❌ Не удалось получить данные. Попробуй позже.")
            return
        profitable = [r for r in results if r["profitable"]]
        if profitable:
            for r in profitable:
                await send_message(session, format_signal(r))
        else:
            await send_message(session,
                "😔 Прибыльных связок нет.\n"
                f"Лучшая маржа: {max(r['net'] for r in results):.2f}%\n"
                "Продолжаю мониторинг каждые 5 минут."
            )

    elif cmd == "/rates":
        await send_message(session, "📊 Получаю курсы...")
        results = await scan(session)
        if results:
            await send_message(session, format_rates(results))
        else:
            await send_message(session, "❌ Не удалось получить данные.")

    elif cmd == "/safety":
        await send_message(session, SAFETY_TEXT)

    elif cmd == "/help":
        await send_message(session, HELP_TEXT)

async def polling_loop(session):
    offset = 0
    while True:
        updates = await get_updates(session, offset)
        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            if msg:
                global CHAT_ID
                CHAT_ID = msg["chat"]["id"]
                text = msg.get("text", "")
                if text.startswith("/"):
                    await handle_command(session, text)
        await asyncio.sleep(1)

async def monitor_loop(session):
    await asyncio.sleep(30)  # Первый скан через 30 сек
    while True:
        if CHAT_ID:
            try:
                results = await scan(session)
                profitable = [r for r in results if r["profitable"]]
                if profitable:
                    for r in profitable:
                        await send_message(session, format_signal(r))
                    logger.info(f"Signals sent: {len(profitable)}")
                else:
                    logger.info(f"No signals. Best: {max((r['net'] for r in results), default=0):.2f}%")
            except Exception as e:
                logger.error(f"Monitor error: {e}")
        await asyncio.sleep(300)  # Каждые 5 минут

async def main():
    if not TOKEN:
        logger.error("TOKEN не установлен!")
        return

    logger.info("Бот запущен")
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        await asyncio.gather(
            polling_loop(session),
            monitor_loop(session)
        )

if __name__ == "__main__":
    asyncio.run(main())
