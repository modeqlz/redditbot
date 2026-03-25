# ═══════════════════════════════════════════════════════════════
#  Reddit Karma Bot — Telegram Бот (Python)
#  Авторизация по коду → Покупка подписки
# ═══════════════════════════════════════════════════════════════

import logging
import re
from datetime import datetime, timedelta, timezone

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

# ═══ CONFIG ═══
BOT_TOKEN = "8568338479:AAHKwVR2yrP6CthvwFwUs4ks2w8hTXJ0kc8"
ADMIN_ID = 937453201

SUPABASE_URL = "https://xabcwmhmbxhcopoynbyw.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhhYmN3bWhtYnhoY29wb3luYnl3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzQzODcxOTUsImV4cCI6MjA4OTk2MzE5NX0.m9xCYtWBlObqLVADUhbNlw-DHnUGHc76vsjRmP6Xgr4"

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

# Хранилище авторизованных пользователей (telegram_id: True)
authorized_users: set[int] = set()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  Supabase
# ═══════════════════════════════════════════════════════════════
async def supabase_get(endpoint: str):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{SUPABASE_URL}/rest/v1/{endpoint}", headers=HEADERS)
        r.raise_for_status()
        return r.json()


async def supabase_post(endpoint: str, data: dict):
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{SUPABASE_URL}/rest/v1/{endpoint}", headers=HEADERS, json=data)
        r.raise_for_status()
        return r.json()


async def supabase_patch(endpoint: str, data: dict):
    async with httpx.AsyncClient() as c:
        r = await c.patch(f"{SUPABASE_URL}/rest/v1/{endpoint}", headers=HEADERS, json=data)
        r.raise_for_status()
        return r.json()


async def supabase_upsert(endpoint: str, data: dict):
    h = {**HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"}
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{SUPABASE_URL}/rest/v1/{endpoint}", headers=h, json=data)
        r.raise_for_status()
        return r.json()


# ═══════════════════════════════════════════════════════════════
#  Проверка авторизации
# ═══════════════════════════════════════════════════════════════
async def is_authorized(tg_id: int) -> bool:
    """Проверяет, авторизован ли пользователь (есть запись в auth_codes)."""
    if tg_id in authorized_users or tg_id == ADMIN_ID:
        return True
    # Проверяем в БД
    try:
        rows = await supabase_get(f"auth_codes?telegram_id=eq.{tg_id}&confirmed=eq.true&select=code")
        if rows:
            authorized_users.add(tg_id)
            return True
    except Exception:
        pass
    return False


async def require_auth(update: Update) -> bool:
    """Если не авторизован — отправляет сообщение и возвращает False."""
    if await is_authorized(update.effective_user.id):
        return True
    await update.message.reply_text(
        "⛔ Сначала авторизуйтесь!\n\n"
        "Откройте десктоп-приложение не даю упасть, "
        "нажмите «Войти через Telegram» и отправьте мне 6-значный код."
    )
    return False


# ═══════════════════════════════════════════════════════════════
#  /start — Приветствие
# ═══════════════════════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    payload = ctx.args[0] if ctx.args else None

    # Если пришёл код из десктопа через deep link
    if payload and re.match(r"^\d{6}$", payload):
        await handle_auth_code(update, payload)
        return

    # Если уже авторизован — показываем главное меню
    if await is_authorized(tg_id):
        await show_main_menu(update)
        return

    # Не авторизован — просим код
    name = update.effective_user.first_name or "друг"
    await update.message.reply_text(
        f"👋 Добро пожаловать, {name}!\n\n"
        f"Для начала работы необходимо авторизоваться.\n\n"
        f"📱 Откройте приложение не даю упасть на компьютере\n"
        f"🔑 Нажмите «Войти через Telegram»\n"
        f"📩 Отправьте мне 6-значный код из приложения\n\n"
        f"Пример: `482917`",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )


# ═══════════════════════════════════════════════════════════════
#  Главное меню (после авторизации)
# ═══════════════════════════════════════════════════════════════
async def show_main_menu(update: Update):
    # Проверяем подписку
    tg_id = update.effective_user.id
    has_sub = False
    sub_info = ""

    try:
        subs = await supabase_get(f"subscriptions?telegram_id=eq.{tg_id}&select=*")
        now = datetime.now(timezone.utc)
        if subs:
            s = subs[0]
            exp = s.get("expires_at")
            has_sub = s.get("active") and (
                not exp or datetime.fromisoformat(exp.replace("Z", "+00:00")) > now
            )
            if has_sub:
                exp_str = datetime.fromisoformat(exp.replace("Z", "+00:00")).strftime("%d.%m.%Y") if exp else "Бессрочно"
                sub_info = f"\n\n✅ Подписка: *{s['plan']}*\n⏰ Действует до: {exp_str}"
    except Exception:
        pass

    kb = ReplyKeyboardMarkup(
        [["🛒 Купить подписку", "📊 Моя подписка"], ["❓ Помощь"]],
        resize_keyboard=True,
    )

    status = sub_info if has_sub else "\n\n❌ У вас нет активной подписки"

    await update.message.reply_text(
        f"✅ Вы авторизованы!{status}\n\n"
        f"Используйте меню ниже для управления.",
        parse_mode="Markdown",
        reply_markup=kb,
    )


# ═══════════════════════════════════════════════════════════════
#  Обработка кода авторизации
# ═══════════════════════════════════════════════════════════════
async def handle_auth_code(update: Update, code: str):
    tg_id = update.effective_user.id
    username = update.effective_user.username or ""
    first_name = update.effective_user.first_name or ""

    try:
        rows = await supabase_get(f"auth_codes?code=eq.{code}&select=*")
        if not rows:
            await update.message.reply_text(
                "❌ Код не найден или истёк.\n"
                "Нажмите «Войти» в приложении заново."
            )
            return

        if rows[0].get("confirmed"):
            await update.message.reply_text("⚠️ Этот код уже использован. Запросите новый в приложении.")
            return

        # Получаем URL аватара
        avatar_url = ""
        try:
            photos = await update.effective_user.get_profile_photos(limit=1)
            if photos.total_count > 0:
                file = await photos.photos[0][-1].get_file()
                avatar_url = file.file_path
        except Exception:
            pass

        # Подтверждаем код + сохраняем профиль
        await supabase_patch(
            f"auth_codes?code=eq.{code}",
            {
                "telegram_id": tg_id,
                "confirmed": True,
                "first_name": first_name,
                "username": username,
                "avatar_url": avatar_url,
            },
        )

        # Добавляем в кэш
        authorized_users.add(tg_id)

        # Проверяем подписку
        subs = await supabase_get(f"subscriptions?telegram_id=eq.{tg_id}&select=*")
        now = datetime.now(timezone.utc)
        has_sub = (
            subs and subs[0].get("active") and (
                not subs[0].get("expires_at")
                or datetime.fromisoformat(subs[0]["expires_at"].replace("Z", "+00:00")) > now
            )
        )

        kb = ReplyKeyboardMarkup(
            [["🛒 Купить подписку", "📊 Моя подписка"], ["❓ Помощь"]],
            resize_keyboard=True,
        )

        if has_sub:
            sub = subs[0]
            exp = sub.get("expires_at")
            exp_str = datetime.fromisoformat(exp.replace("Z", "+00:00")).strftime("%d.%m.%Y") if exp else "Бессрочно"

            await update.message.reply_text(
                "✅ Авторизация подтверждена!\n\n"
                f"📋 Подписка: *{sub['plan']}*\n"
                f"⏰ Действует до: {exp_str}\n\n"
                "Вернитесь в приложение — оно автоматически загрузится.",
                parse_mode="Markdown",
                reply_markup=kb,
            )
        else:
            await update.message.reply_text(
                "✅ Авторизация подтверждена!\n\n"
                "У вас пока нет активной подписки.\n"
                "Нажмите «🛒 Купить подписку» чтобы оформить.",
                reply_markup=kb,
            )

    except Exception as e:
        logger.error(f"Auth error: {e}")
        await update.message.reply_text("❌ Произошла ошибка. Попробуйте позже.")


# ═══════════════════════════════════════════════════════════════
#  /buy — Покупка (только для авторизованных)
# ═══════════════════════════════════════════════════════════════
async def cmd_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return
    await show_pricing(update)


async def show_pricing(update: Update):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Starter — $15", callback_data="buy_starter")],
        [InlineKeyboardButton("⭐ Pro — $39", callback_data="buy_pro")],
        [InlineKeyboardButton("Unlimited — $99", callback_data="buy_unlimited")],
    ])
    await update.message.reply_text(
        "🛒 *Тарифы не даю упасть*\n\n"
        "┌ *Starter* — 7 дней\n"
        "│ До 3 аккаунтов\n"
        "│ ИИ-комментарии + прогрев\n"
        "└ Цена: *$15*\n\n"
        "┌ *Pro* — 30 дней ⭐\n"
        "│ До 10 аккаунтов\n"
        "│ Приоритетная поддержка\n"
        "└ Цена: *$39*\n\n"
        "┌ *Unlimited* — Навсегда\n"
        "│ Без лимита аккаунтов\n"
        "│ Все обновления бесплатно\n"
        "└ Цена: *$99*",
        parse_mode="Markdown",
        reply_markup=kb,
    )


async def cb_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    plan = query.data.replace("buy_", "")
    prices = {"starter": 15, "pro": 39, "unlimited": 99}
    names = {"starter": "Starter (7 дней)", "pro": "Pro (30 дней)", "unlimited": "Unlimited (навсегда)"}

    user = query.from_user
    await query.message.reply_text(
        f"Вы выбрали: *{names[plan]}* — ${prices[plan]}\n\n"
        f"Для оплаты свяжитесь с администратором:\n"
        f"@yanaidyteba\n\n"
        f"Укажите ваш ID: `{user.id}`",
        parse_mode="Markdown",
    )

    if ADMIN_ID:
        try:
            await ctx.bot.send_message(
                ADMIN_ID,
                f"🔔 Новый заказ!\n\n"
                f"Покупатель: {user.first_name} (@{user.username or 'нет'})\n"
                f"ID: `{user.id}`\n"
                f"Тариф: {names[plan]} — ${prices[plan]}",
                parse_mode="Markdown",
            )
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
#  /status — Статус подписки (только авторизованные)
# ═══════════════════════════════════════════════════════════════
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return

    try:
        tg_id = update.effective_user.id
        rows = await supabase_get(f"subscriptions?telegram_id=eq.{tg_id}&select=*")

        if not rows:
            await update.message.reply_text("❌ У вас нет подписки.\nИспользуйте /buy для покупки.")
            return

        sub = rows[0]
        now = datetime.now(timezone.utc)
        exp = sub.get("expires_at")
        is_active = sub.get("active") and (
            not exp or datetime.fromisoformat(exp.replace("Z", "+00:00")) > now
        )
        emoji = "✅" if is_active else "❌"
        status = "Активна" if is_active else "Неактивна"
        created = datetime.fromisoformat(sub["created_at"].replace("Z", "+00:00")).strftime("%d.%m.%Y")
        exp_str = datetime.fromisoformat(exp.replace("Z", "+00:00")).strftime("%d.%m.%Y") if exp else "Бессрочно"

        await update.message.reply_text(
            f"{emoji} *Подписка: {status}*\n\n"
            f"📋 Тариф: {sub['plan']}\n"
            f"📅 Оформлена: {created}\n"
            f"⏰ Истекает: {exp_str}\n"
            f"🆔 ID: `{tg_id}`",
            parse_mode="Markdown",
        )
    except Exception:
        await update.message.reply_text("Ошибка. Попробуйте позже.")


# ═══════════════════════════════════════════════════════════════
#  /help
# ═══════════════════════════════════════════════════════════════
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ *Помощь — не даю упасть*\n\n"
        "*Как начать:*\n"
        "1. Откройте приложение на ПК\n"
        "2. Нажмите «Войти через Telegram»\n"
        "3. Отправьте код в этот чат\n"
        "4. Купите подписку — /buy\n"
        "5. Запускайте фарм!\n\n"
        "*Команды:*\n"
        "/buy — Купить подписку\n"
        "/status — Моя подписка\n"
        "/help — Справка\n\n"
        "Поддержка: @yanaidyteba",
        parse_mode="Markdown",
    )


# ═══════════════════════════════════════════════════════════════
#  АДМИН-КОМАНДЫ
# ═══════════════════════════════════════════════════════════════
async def cmd_grant(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = ctx.args
    if not args or len(args) < 2:
        await update.message.reply_text("Формат: /grant <telegram_id> <plan> [days]\nПример: /grant 123456 pro 30")
        return

    tg_id = int(args[0])
    plan = args[1]
    days = int(args[2]) if len(args) > 2 else None
    expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat() if days else None

    try:
        await supabase_upsert("subscriptions", {
            "telegram_id": tg_id, "plan": plan, "active": True, "expires_at": expires_at,
        })
        await update.message.reply_text(f"✅ Подписка выдана!\nID: {tg_id}\nПлан: {plan}\nДней: {days or '∞'}")
        try:
            await ctx.bot.send_message(
                tg_id,
                f"🎉 Подписка *{plan}* активирована!\nСрок: {f'{days} дней' if days else 'Бессрочно'}\n\nТеперь авторизуйтесь в приложении.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Формат: /revoke <telegram_id>")
        return
    tg_id = ctx.args[0]
    try:
        await supabase_patch(f"subscriptions?telegram_id=eq.{tg_id}", {"active": False})
        await update.message.reply_text(f"✅ Подписка отозвана: {tg_id}")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    text = update.message.text.split(maxsplit=1)
    if len(text) < 2:
        await update.message.reply_text("Формат: /broadcast <текст>")
        return
    msg = text[1]
    try:
        subs = await supabase_get("subscriptions?active=eq.true&select=telegram_id")
        sent = 0
        for sub in subs:
            try:
                await ctx.bot.send_message(sub["telegram_id"], msg, parse_mode="Markdown")
                sent += 1
            except Exception:
                pass
        await update.message.reply_text(f"✅ Рассылка: {sent}/{len(subs)}")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        all_subs = await supabase_get("subscriptions?select=*")
        now = datetime.now(timezone.utc)
        active = [s for s in all_subs if s.get("active") and (
            not s.get("expires_at") or datetime.fromisoformat(s["expires_at"].replace("Z", "+00:00")) > now
        )]
        plans: dict[str, int] = {}
        for s in active:
            p = s.get("plan", "?")
            plans[p] = plans.get(p, 0) + 1
        plan_str = "\n".join(f"  {k}: {v}" for k, v in plans.items()) or "  нет"
        await update.message.reply_text(
            f"📊 *Статистика*\n\nВсего: {len(all_subs)}\nАктивных: {len(active)}\n\n{plan_str}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


# ═══════════════════════════════════════════════════════════════
#  Обработка текстовых сообщений
# ═══════════════════════════════════════════════════════════════
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    tg_id = update.effective_user.id

    # Ловим 6-значный код авторизации
    if re.match(r"^\d{6}$", text):
        await handle_auth_code(update, text)
        return

    # Кнопки меню (только если авторизован)
    if text == "🛒 Купить подписку":
        if await require_auth(update):
            await show_pricing(update)
    elif text == "📊 Моя подписка":
        if await require_auth(update):
            await cmd_status(update, ctx)
    elif text == "❓ Помощь":
        await cmd_help(update, ctx)
    else:
        # Неизвестное сообщение
        if await is_authorized(tg_id):
            await update.message.reply_text("Используйте кнопки меню или команды.")
        else:
            await update.message.reply_text(
                "Введите 6-значный код из приложения для авторизации.\n"
                "Пример: `482917`",
                parse_mode="Markdown",
            )


# ═══════════════════════════════════════════════════════════════
#  Обработка данных из Mini App (WebApp)
# ═══════════════════════════════════════════════════════════════
async def handle_webapp_data(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = update.effective_message.web_app_data.data
    import json
    try:
        req = json.loads(data)
        if req.get("action") == "buy":
            plan = req.get("plan")
            price = req.get("price")
            name = req.get("name")
            user = update.effective_user

            await update.message.reply_text(
                f"Вы выбрали в Mini-App: *{name}* — ${price}\n\n"
                f"Для оплаты переведите нужную сумму в USDT (или свяжитесь с поддержкой).\n"
                f"Ваш ID: `{user.id}`\n\n"
                f"Администратор: @yanaidyteba",
                parse_mode="Markdown",
            )

            if ADMIN_ID:
                try:
                    await ctx.bot.send_message(
                        ADMIN_ID,
                        f"🔔 Новый заказ из Mini App!\n\n"
                        f"Покупатель: {user.first_name} (@{user.username or 'нет'})\n"
                        f"ID: `{user.id}`\n"
                        f"Тариф: {name} — ${price}",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"WebApp Error: {e}")


# ═══════════════════════════════════════════════════════════════
#  ЗАПУСК
# ═══════════════════════════════════════════════════════════════
def main():
    app = Application.builder().token(BOT_TOKEN).build()

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

    print("🤖 Reddit Karma Bot Shop запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
