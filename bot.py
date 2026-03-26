# ═══════════════════════════════════════════════════════════════
#  Reddit Karma Bot — Telegram Бот (Python)
#  Авторизация по коду → Покупка подписки
# ═══════════════════════════════════════════════════════════════

import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)

# ═══ Загрузка конфига из .env ═══
load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

AUTH_CODE_TTL_SEC = 600
AUTH_MAX_ATTEMPTS = 5
BROADCAST_DELAY_SEC = 0.05

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

authorized_users: set[int] = set()
auth_attempts: dict[int, list[float]] = defaultdict(list)
http_client: httpx.AsyncClient | None = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def on_startup(application):
    global http_client
    http_client = httpx.AsyncClient(timeout=15)

async def on_shutdown(application):
    global http_client
    if http_client:
        await http_client.aclose()


# ═══ Supabase (переиспользует http_client) ═══
async def supabase_get(ep):
    r = await http_client.get(f"{SUPABASE_URL}/rest/v1/{ep}", headers=HEADERS)
    r.raise_for_status()
    return r.json()

async def supabase_patch(ep, data):
    r = await http_client.patch(f"{SUPABASE_URL}/rest/v1/{ep}", headers=HEADERS, json=data)
    r.raise_for_status()
    return r.json()

async def supabase_upsert(ep, data):
    h = {**HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"}
    r = await http_client.post(f"{SUPABASE_URL}/rest/v1/{ep}", headers=h, json=data)
    r.raise_for_status()
    return r.json()


# ═══ Rate Limiting ═══
def check_auth_rate_limit(uid: int) -> bool:
    now = time.time()
    auth_attempts[uid] = [t for t in auth_attempts[uid] if now - t < AUTH_CODE_TTL_SEC]
    if len(auth_attempts[uid]) >= AUTH_MAX_ATTEMPTS:
        return False
    auth_attempts[uid].append(now)
    return True


# ═══ Авторизация ═══
async def is_authorized(tg_id: int) -> bool:
    if tg_id in authorized_users or tg_id == ADMIN_ID:
        return True
    try:
        rows = await supabase_get(f"auth_codes?telegram_id=eq.{tg_id}&confirmed=eq.true&select=code")
        if rows:
            authorized_users.add(tg_id)
            return True
    except Exception:
        pass
    return False

async def require_auth(update: Update) -> bool:
    if await is_authorized(update.effective_user.id):
        return True
    await update.message.reply_text(
        "⛔ Сначала авторизуйтесь!\n\n"
        "Откройте приложение, нажмите «Войти через Telegram» и отправьте мне 6-значный код."
    )
    return False


# ═══ /start ═══
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    payload = ctx.args[0] if ctx.args else None
    if payload and re.match(r"^\d{6}$", payload):
        await handle_auth_code(update, payload)
        return
    if await is_authorized(tg_id):
        await show_main_menu(update)
        return
    name = update.effective_user.first_name or "друг"
    await update.message.reply_text(
        f"👋 Добро пожаловать, {name}!\n\n"
        "📱 Откройте приложение на ПК\n🔑 Нажмите «Войти через Telegram»\n"
        "📩 Отправьте мне 6-значный код\n\nПример: `482917`",
        parse_mode="Markdown", reply_markup=ReplyKeyboardRemove(),
    )

async def show_main_menu(update: Update):
    tg_id = update.effective_user.id
    sub_info = "\n\n❌ У вас нет активной подписки"
    try:
        subs = await supabase_get(f"subscriptions?telegram_id=eq.{tg_id}&select=*")
        now = datetime.now(timezone.utc)
        if subs:
            s = subs[0]
            exp = s.get("expires_at")
            active = s.get("active") and (not exp or datetime.fromisoformat(exp.replace("Z", "+00:00")) > now)
            if active:
                exp_str = datetime.fromisoformat(exp.replace("Z", "+00:00")).strftime("%d.%m.%Y") if exp else "Бессрочно"
                sub_info = f"\n\n✅ Подписка: *{s['plan']}*\n⏰ До: {exp_str}"
    except Exception:
        pass
    kb = ReplyKeyboardMarkup([["🛒 Купить подписку", "📊 Моя подписка"], ["❓ Помощь"]], resize_keyboard=True)
    await update.message.reply_text(f"✅ Вы авторизованы!{sub_info}\n\nИспользуйте меню.", parse_mode="Markdown", reply_markup=kb)


# ═══ Обработка кода авторизации (rate limit + TTL) ═══
async def handle_auth_code(update: Update, code: str):
    tg_id = update.effective_user.id
    if not check_auth_rate_limit(tg_id):
        await update.message.reply_text("⚠️ Слишком много попыток! Подождите 10 минут.")
        return
    try:
        rows = await supabase_get(f"auth_codes?code=eq.{code}&select=*")
        if not rows:
            await update.message.reply_text("❌ Код не найден. Запросите новый.")
            return
        auth_row = rows[0]
        if auth_row.get("confirmed"):
            await update.message.reply_text("⚠️ Код уже использован.")
            return
        # TTL
        created_at = auth_row.get("created_at")
        if created_at:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(created_at.replace("Z", "+00:00"))).total_seconds()
            if age > AUTH_CODE_TTL_SEC:
                await update.message.reply_text("⏰ Код истёк (10 мин). Запросите новый.")
                return
        avatar_url = ""
        try:
            photos = await update.effective_user.get_profile_photos(limit=1)
            if photos.total_count > 0:
                f = await photos.photos[0][-1].get_file()
                avatar_url = f.file_path
        except Exception:
            pass
        await supabase_patch(f"auth_codes?code=eq.{code}", {
            "telegram_id": tg_id, "confirmed": True,
            "first_name": update.effective_user.first_name or "",
            "username": update.effective_user.username or "",
            "avatar_url": avatar_url,
        })
        authorized_users.add(tg_id)
        logger.info(f"User {tg_id} authorized")
        subs = await supabase_get(f"subscriptions?telegram_id=eq.{tg_id}&select=*")
        now = datetime.now(timezone.utc)
        has_sub = subs and subs[0].get("active") and (not subs[0].get("expires_at") or datetime.fromisoformat(subs[0]["expires_at"].replace("Z", "+00:00")) > now)
        kb = ReplyKeyboardMarkup([["🛒 Купить подписку", "📊 Моя подписка"], ["❓ Помощь"]], resize_keyboard=True)
        if has_sub:
            sub = subs[0]
            exp = sub.get("expires_at")
            exp_str = datetime.fromisoformat(exp.replace("Z", "+00:00")).strftime("%d.%m.%Y") if exp else "Бессрочно"
            await update.message.reply_text(f"✅ Авторизация подтверждена!\n\n📋 Подписка: *{sub['plan']}*\n⏰ До: {exp_str}\n\nВернитесь в приложение.", parse_mode="Markdown", reply_markup=kb)
        else:
            await update.message.reply_text("✅ Авторизация подтверждена!\n\nНет подписки. Нажмите «🛒 Купить подписку».", reply_markup=kb)
    except Exception as e:
        logger.error(f"Auth error: {e}")
        await update.message.reply_text("❌ Ошибка. Попробуйте позже.")


# ═══ /buy ═══
async def cmd_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update): return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Starter — $15", callback_data="buy_starter")],
        [InlineKeyboardButton("⭐ Pro — $39", callback_data="buy_pro")],
        [InlineKeyboardButton("Unlimited — $99", callback_data="buy_unlimited")],
    ])
    await update.message.reply_text(
        "🛒 *Тарифы*\n\n┌ *Starter* — 7 дней\n│ До 3 аккаунтов · ИИ-комментарии · прогрев\n└ *$15*\n\n"
        "┌ *Pro* — 30 дней ⭐\n│ До 10 аккаунтов · приоритетная поддержка\n└ *$39*\n\n"
        "┌ *Unlimited* — Навсегда\n│ Без лимита · все обновления\n└ *$99*",
        parse_mode="Markdown", reply_markup=kb)

async def cb_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    plan = q.data.replace("buy_", "")
    prices = {"starter": 15, "pro": 39, "unlimited": 99}
    names = {"starter": "Starter (7 дн)", "pro": "Pro (30 дн)", "unlimited": "Unlimited"}
    if plan not in prices: return
    user = q.from_user
    await q.message.reply_text(f"Вы выбрали: *{names[plan]}* — ${prices[plan]}\n\nДля оплаты: @yanaidyteba\nВаш ID: `{user.id}`", parse_mode="Markdown")
    if ADMIN_ID:
        try:
            await ctx.bot.send_message(ADMIN_ID, f"🔔 Заказ!\n{user.first_name} (@{user.username or 'нет'})\nID: `{user.id}`\n{names[plan]} — ${prices[plan]}", parse_mode="Markdown")
        except Exception: pass


# ═══ /status ═══
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update): return
    try:
        tg_id = update.effective_user.id
        rows = await supabase_get(f"subscriptions?telegram_id=eq.{tg_id}&select=*")
        if not rows:
            await update.message.reply_text("❌ Подписка не найдена. /buy")
            return
        sub = rows[0]
        now = datetime.now(timezone.utc)
        exp = sub.get("expires_at")
        active = sub.get("active") and (not exp or datetime.fromisoformat(exp.replace("Z", "+00:00")) > now)
        e = "✅" if active else "❌"
        st = "Активна" if active else "Неактивна"
        cr = datetime.fromisoformat(sub["created_at"].replace("Z", "+00:00")).strftime("%d.%m.%Y")
        ex = datetime.fromisoformat(exp.replace("Z", "+00:00")).strftime("%d.%m.%Y") if exp else "Бессрочно"
        await update.message.reply_text(f"{e} *{st}*\n\n📋 {sub['plan']}\n📅 Создана: {cr}\n⏰ До: {ex}\n🆔 `{tg_id}`", parse_mode="Markdown")
    except Exception:
        await update.message.reply_text("Ошибка. Попробуйте позже.")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❓ *Помощь*\n\n1. Откройте приложение\n2. «Войти через Telegram»\n3. Отправьте код\n4. /buy — подписка\n\n/status — статус\nПоддержка: @yanaidyteba", parse_mode="Markdown")


# ═══ АДМИН ═══
async def cmd_grant(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    args = ctx.args
    if not args or len(args) < 2:
        await update.message.reply_text("Формат: /grant <id> <plan> [days]\nПланы: Starter, Pro, Unlimited")
        return
    try:
        tg_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом"); return
    plan = args[1]
    if plan.lower() not in ("starter", "pro", "unlimited"):
        await update.message.reply_text("❌ План: Starter/Pro/Unlimited"); return
    days = None
    if len(args) > 2:
        try:
            days = max(1, min(int(args[2]), 3650))
        except ValueError:
            await update.message.reply_text("❌ Дни — число"); return
    exp = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat() if days else None
    try:
        await supabase_upsert("subscriptions", {"telegram_id": tg_id, "plan": plan.capitalize(), "active": True, "expires_at": exp})
        await update.message.reply_text(f"✅ Выдано: {plan} ({days or '∞'} дн) → {tg_id}")
        try:
            await ctx.bot.send_message(tg_id, f"🎉 Подписка *{plan}* активирована!\nСрок: {f'{days} дн' if days else 'Бессрочно'}", parse_mode="Markdown")
        except Exception: pass
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not ctx.args:
        await update.message.reply_text("Формат: /revoke <id>"); return
    try:
        tg_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID — число"); return
    try:
        await supabase_patch(f"subscriptions?telegram_id=eq.{tg_id}", {"active": False})
        await update.message.reply_text(f"✅ Отозвана: {tg_id}")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("Формат: /broadcast <текст>"); return
    msg = parts[1]
    try:
        subs = await supabase_get("subscriptions?active=eq.true&select=telegram_id")
        sent = 0
        for sub in subs:
            try:
                await ctx.bot.send_message(sub["telegram_id"], msg, parse_mode="Markdown")
                sent += 1
            except Exception: pass
            await asyncio.sleep(BROADCAST_DELAY_SEC)
        await update.message.reply_text(f"✅ Рассылка: {sent}/{len(subs)}")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        all_s = await supabase_get("subscriptions?select=*")
        now = datetime.now(timezone.utc)
        act = [s for s in all_s if s.get("active") and (not s.get("expires_at") or datetime.fromisoformat(s["expires_at"].replace("Z", "+00:00")) > now)]
        plans = {}
        for s in act:
            p = s.get("plan", "?")
            plans[p] = plans.get(p, 0) + 1
        ps = "\n".join(f"  {k}: {v}" for k, v in plans.items()) or "  нет"
        await update.message.reply_text(f"📊 *Статистика*\n\nВсего: {len(all_s)}\nАктивных: {len(act)}\n\n{ps}", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


# ═══ Текстовые сообщения ═══
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    tg_id = update.effective_user.id
    if re.match(r"^\d{6}$", text):
        await handle_auth_code(update, text); return
    if text == "🛒 Купить подписку":
        if await require_auth(update): await cmd_buy(update, ctx)
    elif text == "📊 Моя подписка":
        if await require_auth(update): await cmd_status(update, ctx)
    elif text == "❓ Помощь":
        await cmd_help(update, ctx)
    else:
        if await is_authorized(tg_id):
            await update.message.reply_text("Используйте меню или команды.")
        else:
            await update.message.reply_text("Отправьте 6-значный код.\nПример: `482917`", parse_mode="Markdown")

async def handle_webapp_data(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        req = json.loads(update.effective_message.web_app_data.data)
        if req.get("action") == "buy":
            user = update.effective_user
            await update.message.reply_text(f"Заказ: *{req.get('name')}* — ${req.get('price')}\n\nID: `{user.id}`\n@yanaidyteba", parse_mode="Markdown")
            if ADMIN_ID:
                try:
                    await ctx.bot.send_message(ADMIN_ID, f"🔔 Mini App заказ!\n{user.first_name} (@{user.username or 'нет'})\nID: `{user.id}`\n{req.get('name')} — ${req.get('price')}", parse_mode="Markdown")
                except Exception: pass
    except Exception as e:
        logger.error(f"WebApp: {e}")


# ═══ ЗАПУСК ═══
def main():
    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).post_shutdown(on_shutdown).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("buy", cmd_buy))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("grant", cmd_grant))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CallbackQueryHandler(cb_buy, pattern=r"^buy_"))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info(f"🤖 Бот запущен | Admin: {ADMIN_ID}")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
