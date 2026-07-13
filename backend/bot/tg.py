import asyncio
import logging
import os
import json
import time
import secrets
import string
import aiohttp
import aiosqlite
import re
from dotenv import load_dotenv
from cryptography.fernet import Fernet

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    FSInputFile,
    CopyTextButton,
)

# --- Configuration & Pricing ---

load_dotenv()
BOT_TOKEN      = os.getenv("BOT_TOKEN")
ADMIN_ID       = os.getenv("ADMIN_ID")
BOT_DB_KEY     = os.getenv("BOT_DB_KEY")
BOT_API_SECRET = os.getenv("BOT_API_SECRET")
API_URL        = "https://maakolo.sbs:8445"
APK_PATH       = "/var/www/maakolo/downloads/maakolo.apk"
SUPPORT_URL    = "https://t.me/Maakolo_help"

PRICE_BASE_RUB    = 150
PRICE_BASE_XTR    = 145
PRICE_STEALTH_RUB = 200
PRICE_STEALTH_XTR = 225

if not all([BOT_TOKEN, ADMIN_ID, BOT_DB_KEY, BOT_API_SECRET]):
    raise RuntimeError("Не заданы обязательные переменные окружения (.env)")

ADMIN_ID = int(ADMIN_ID)

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("maakolo")
log.setLevel(logging.INFO)
logging.getLogger("aiogram").setLevel(logging.WARNING)

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

_fernet = Fernet(BOT_DB_KEY.encode())
def _enc(v: str) -> str: return _fernet.encrypt(v.encode()).decode()
def _dec(v: str) -> str: return _fernet.decrypt(v.encode()).decode()

# Глобальная сессия aiohttp и кэш бота
_http_session: aiohttp.ClientSession | None = None
bot_username: str = ""

def get_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    return _http_session

# --- Database ---

DB_PATH  = "maakolo_bot.db"
_db_lock = asyncio.Lock()
_db_conn: aiosqlite.Connection | None = None

async def _get_db() -> aiosqlite.Connection:
    global _db_conn
    try:
        if _db_conn is None:
            _db_conn = await aiosqlite.connect(DB_PATH)
            await _db_conn.execute("PRAGMA journal_mode=WAL")
            await _db_conn.execute(
                "CREATE TABLE IF NOT EXISTS users "
                "(uid INTEGER PRIMARY KEY, data TEXT NOT NULL DEFAULT '{}')"
            )
            await _db_conn.execute(
                "CREATE TABLE IF NOT EXISTS tickets "
                "(msg_id INTEGER PRIMARY KEY, uid INTEGER NOT NULL, ts INTEGER NOT NULL)"
            )
            await _db_conn.commit()
        return _db_conn
    except Exception as e:
        log.error(f"DB Error: {e}")
        _db_conn = None
        raise

async def get_user_data(uid: int) -> dict:
    async with _db_lock:
        conn = await _get_db()
        async with conn.execute("SELECT data FROM users WHERE uid=?", (uid,)) as c:
            row = await c.fetchone()
        return json.loads(row[0]) if row else {}

async def update_user_data(uid: int, updater_func) -> dict:
    """Транзакционное обновление"""
    async with _db_lock:
        conn = await _get_db()
        async with conn.execute("SELECT data FROM users WHERE uid=?", (uid,)) as c:
            row = await c.fetchone()
        d = json.loads(row[0]) if row else {}
        d = updater_func(d)
        await conn.execute(
            "INSERT INTO users(uid,data) VALUES(?,?) "
            "ON CONFLICT(uid) DO UPDATE SET data=excluded.data",
            (uid, json.dumps(d, ensure_ascii=False))
        )
        await conn.commit()
        return d

async def get_all_user_ids() -> list[int]:
    async with _db_lock:
        conn = await _get_db()
        async with conn.execute("SELECT uid FROM users") as c:
            return [r[0] for r in await c.fetchall()]

async def get_lang(uid: int) -> str:
    return (await get_user_data(uid)).get("lang", "EN")

async def set_lang(uid: int, lang: str):
    def _upd(d): d["lang"] = lang; return d
    await update_user_data(uid, _upd)

async def get_os(uid: int) -> str | None:
    return (await get_user_data(uid)).get("os")

async def set_os(uid: int, val: str):
    def _upd(d): d["os"] = val; return d
    await update_user_data(uid, _upd)

async def set_crypto_pending(uid: int, txn_id: str):
    def _upd(d): d["pending_txn"] = txn_id; return d
    await update_user_data(uid, _upd)

async def get_crypto_pending(uid: int) -> str | None:
    return (await get_user_data(uid)).get("pending_txn")

async def clear_crypto_pending(uid: int):
    def _upd(d): d.pop("pending_txn", None); return d
    await update_user_data(uid, _upd)

async def _ticket_set(msg_id: int, uid: int):
    async with _db_lock:
        conn = await _get_db()
        ts = int(time.time())
        await conn.execute(
            "INSERT OR REPLACE INTO tickets(msg_id,uid,ts) VALUES(?,?,?)", (msg_id, uid, ts)
        )
        await conn.execute("DELETE FROM tickets WHERE ts<?", (ts - 7 * 86400,))
        await conn.commit()

async def _ticket_get(msg_id: int) -> int | None:
    async with _db_lock:
        conn = await _get_db()
        async with conn.execute("SELECT uid FROM tickets WHERE msg_id=?", (msg_id,)) as c:
            row = await c.fetchone()
            return row[0] if row else None

async def _ticket_count() -> int:
    async with _db_lock:
        conn = await _get_db()
        async with conn.execute("SELECT COUNT(*) FROM tickets") as c:
            row = await c.fetchone(); return row[0] if row else 0

async def clear_account_state(uid: int):
    """Сбрасывает весь стейт устройства при выходе/удалении"""
    def _upd(d):
        return {"lang": d.get("lang", "EN")}
    await update_user_data(uid, _upd)

# --- Account Management ---

def _gen_password() -> str:
    chars = string.ascii_uppercase + string.ascii_lowercase + string.digits + "!@#$%^&*"
    pwd = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*"),
    ]
    pwd += [secrets.choice(chars) for _ in range(12)]
    secrets.SystemRandom().shuffle(pwd)
    return "".join(pwd)

async def get_account(uid: int) -> tuple[str | None, str | None]:
    """Только чтение аккаунта, без автосоздания"""
    d = await get_user_data(uid)
    if "api_id" in d and "api_pwd" in d:
        try:
            pwd = _dec(d["api_pwd"])
        except Exception:
            pwd = d["api_pwd"]
            def _upd(data): data["api_pwd"] = _enc(pwd); return data
            await update_user_data(uid, _upd)
        return d["api_id"], pwd
    return None, None

async def create_account(uid: int) -> tuple[str | None, str | None]:
    try:
        s = get_session()
        async with s.get(f"{API_URL}/generate_id") as r:
            if r.status != 200: return None, None
            new_id = (await r.json())["account_id"]
        pwd = _gen_password()
        async with s.post(f"{API_URL}/register", json={"account_id": new_id, "password": pwd}) as r:
            if r.status not in [200, 201]: return None, None

        def _upd(d):
            d["api_id"] = new_id
            d["api_pwd"] = _enc(pwd)
            return d
        await update_user_data(uid, _upd)
        return new_id, pwd
    except Exception as e:
        log.error(f"create_account uid={uid}: {e}")
        return None, None

# --- Profile ---

async def load_profile(uid: int, lang: str) -> tuple[str, dict[str, str], bool]:
    """Возвращает (текст, {tariff: vless_key}, has_sub)"""
    acc_id, acc_pwd = await get_account(uid)
    if not acc_id:
        return T(lang, "server_error"), {}, False

    active: list[tuple[str, int]] = []
    keys:   dict[str, str]        = {}

    try:
        s = get_session()
        async with s.post(f"{API_URL}/login", json={"account_id": acc_id, "password": acc_pwd}) as r:
            ld = await r.json() if r.status == 200 else {}

        now_ms = int(time.time() * 1000)
        for tariff, exp_key in [("stealth", "expiry_stealth"), ("base", "expiry_base")]:
            exp = ld.get(exp_key, 0)
            if exp > now_ms:
                days = max(1, (exp - now_ms) // (86400 * 1000))
                active.append((tariff, int(days)))
                async with s.post(f"{API_URL}/get_key",
                                  json={"account_id": acc_id, "password": acc_pwd, "tariff": tariff}) as r2:
                    if r2.status == 200:
                        kd = await r2.json()
                        if kd.get("status") == "success":
                            keys[tariff] = kd["vless_link"]
    except Exception as e:
        log.error(f"load_profile uid={uid}: {e}")
        return T(lang, "server_error"), {}, False

    lines = [
        f"🛡 *Maakolo Dashboard*",
        f"ID: `{acc_id}`",
        f"{T(lang,'lbl_password')}: {T(lang,'pwd_hidden')}",
        f"{T(lang,'sys_status_lbl')} {T(lang,'sys_status_ok')}",
        "━━━━━━━━━━━━━━━━━━",
        ""
    ]

    has_sub = len(active) > 0
    if active:
        for tariff, days in active:
            emoji = "💎" if tariff == "stealth" else "⚡"
            name  = "Stealth Network" if tariff == "stealth" else "Base Network"
            lines.append(f"{emoji} {name} · {days} {T(lang,'days')}")
        if keys:
            lines.append(f"\n_{T(lang,'key_hint')}_")
    else:
        lines.append(T(lang, "no_sub"))

    return "\n".join(lines), keys, has_sub

# --- Localization ---

_TEXTS: dict[str, dict[str, str]] = {
    "EN": {
        "choose_lang":    "Select language / Выберите язык / Valitse kieli:",
        "onboard_msg":    "*Welcome to Maakolo Network*\n\nDo you already have an account?",
        "onboard_create": "Create new account",
        "onboard_login":  "Log in with existing ID",
        "welcome":        "*Maakolo Network*\n\nPrivate, no-log VPN infrastructure.",
        "btn_profile":    "Profile & VPN",
        "btn_settings":   "Settings & Feedback",
        "settings_msg":   "*Settings & Feedback*\n\nSelect a section:",
        "btn_login":      "Log in",
        "btn_logout":     "Log out",
        "btn_lang":       "Language",
        "btn_ios":        "iOS setup",
        "btn_pc":         "PC / macOS",
        "btn_support":    "Contact Support",
        "btn_bug":        "Report issue",
        "btn_review":     "Leave Review",
        "btn_info":       "About",
        "btn_change_os":  "Change device",
        "btn_copy_creds": "Credentials",
        "btn_buy_access": "Buy access",
        "btn_renew":      "Renew access",
        "btn_acc_settings":"⚙️ Account Settings",
        "acc_settings_msg":"*Account Settings*\n\nManage your ID, password, and active devices.",
        "btn_delete":     "Delete account",
        "btn_back":       "Back",
        "btn_cancel":     "Cancel",
        "btn_close_conv": "Close conversation",
        "lbl_password":   "Password",
        "pwd_hidden":     "_hidden — tap «Account Settings»_",
        "no_sub":         "No active subscription",
        "days":           "d. left",
        "key_hint":       "Tap the button below to copy your access key",
        "btn_copy_key_base":    "Copy key · Base Network",
        "btn_copy_key_stealth": "Copy key · Stealth Network",
        "btn_refresh":    "🔄 Refresh",
        "btn_fix_conn":   "🛠 Fix connection",
        "sys_status_lbl": "Status:",
        "sys_status_ok":  "🟢 Operational",
        "switching_route":"Switching route...",
        "route_switched": "Route switched. Tap Refresh to get your new key.",
        "route_no_sub":   "No active subscriptions to switch.",
        "creds_msg":      "*Your Credentials*\n\nID: `{id}`\nPassword: `{pwd}`\n\n_Auto-deletes in 30 s._",
        "btn_copy_id":    "Copy ID",
        "btn_copy_pwd":   "Copy Password",
        "loading":        "Loading...",
        "cancelled":      "Cancelled.",
        "conv_closed":    "Conversation closed.",
        "ticket_sent":    "Sent. An engineer will reply here.\n\n_You can continue writing below._",
        "followup_sent":  "Follow-up sent.",
        "rate_limit":     "Too many requests. Wait {sec}s.",
        "reply_prefix":   "*Support reply:*\n\n",
        "bug_prompt":     "*Report an issue*\n\nDescribe the problem.\n\n_You can send multiple messages._",
        "review_prompt":  "*Leave a Review*\n\nTell us what you think! Your feedback helps us improve.",
        "os_prompt":      "Select your device OS:",
        "android_msg":    "For Android, use our official app.\nSign in with your ID and Password from the profile.",
        "btn_dl_apk":     "Download client",
        "info_msg":       (
            "*Maakolo Network*\n\n"
            "Traffic obfuscation using Hysteria 2 + QUIC.\n"
            "To ISPs your connection looks like regular HTTPS/3.\n\n"
            "• No logs — sessions are never stored\n"
            "• No throttling — optimal routing\n"
            "• Bypasses DPI-based blocks"
        ),
        "ios_msg":        (
            "*iOS Configuration*\n\n"
            "1. Install *Hiddify* from the App Store.\n"
            "2. Open *Profile & VPN* and copy your key.\n"
            "3. In Hiddify: tap *➕ Add Profile* → *Import from Clipboard*.\n\n"
            "⚠️ *Note:* For Android, always use our official APK for better routing."
        ),
        "pc_msg":         (
            "*Windows / macOS setup*\n\n"
            "1. Download *Hiddify* (Win/Mac) or *V2rayN* (Win) / *FoXray* (Mac).\n"
            "2. Copy your access key from *Profile & VPN*.\n"
            "3. In the app — *Add profile* → paste from clipboard."
        ),
        "server_error":   "Server error. Please try again in a minute.",
        "login_id_prompt": "*Log in*\n\nSend your account ID.\n\n_Example:_ `1234567890123456`",
        "login_pwd_prompt": "*Log in*\n\nSend your password.\n_It will be deleted immediately._",
        "login_id_fmt":   "Invalid ID. Enter exactly 16 digits (spaces are OK).",
        "login_ok":       "Logged in.",
        "login_out_ok":   "Logged out.",
        "login_fail":     "Invalid ID or password.",
        "del_prompt":     "*Delete account*\n\nEnter your VPN password to confirm.\n\n_This is irreversible._",
        "del_2fa":        "2FA is enabled. Enter the 6-digit code from your authenticator app:",
        "del_ok":         "Account deleted.",
        "choose_tariff":  "Select tier:",
        "tariff_base":    "Base Network — standard encryption\nfrom {p_xtr} XTR / mo.",
        "tariff_stealth": "Stealth Network — maximum obfuscation\nfrom {p_xtr} XTR / mo.",
        "choose_months":  "Select subscription term:",
        "mo_1": "1 Month", "mo_3": "3 Months", "mo_6": "6 Months", "mo_12": "12 Months",
        "choose_pay":     "Select payment method:",
        "btn_fiat":       "Bank card",
        "btn_stars":      "Telegram Stars",
        "btn_crypto":     "Crypto (TON / USDT / LTC / ETH)",
        "stars_note":     "Invoice below — pay there, profile updates automatically.",
        "stars_loader":   "Processing payment...",
        "stars_ok":       "Payment confirmed. Access upgraded.",
        "stars_err":      "Activation error. Contact support — transaction is logged.",
        "invoice_ready":  "*Invoice ready*\n\nPlan: {plan}\n\nClick below to pay.",
        "btn_pay_fiat":   "Go to payment",
        "btn_profile_refresh": "Check profile",
        "gw_err":         "Payment gateway error. Try again.",
        "crypto_select":  "Select asset:",
        "usdt_min_btn":   "USDT (min 3 mo.)",
        "usdt_min_err":   "USDT minimum is $5. Choose 3+ months or a different asset.",
        "crypto_addr":    "*{coin} transaction*\n\nAmount: `{amount}` {coin}\nAddress:\n`{wallet}`\n\n_Transfer the exact amount, then tap the button below._",
        "btn_verify":     "Verify transaction",
        "crypto_no_inv":  "No active invoice found. Check your profile.",
        "crypto_wait":    "Not confirmed yet. Try again in a moment.",
        "crypto_ok":      "Transaction confirmed. Access upgraded.",
        "crypto_err":      "Verification failed. Try again or contact support.",
        "crypto_inv_err": "Invoice creation failed. Try again later.",
    },
    "RU": {
        "choose_lang":    "Select language / Выберите язык / Valitse kieli:",
        "onboard_msg":    "*Добро пожаловать в Maakolo Network*\n\nУ вас уже есть аккаунт?",
        "onboard_create": "Создать новый аккаунт",
        "onboard_login":  "Войти с существующим ID",
        "welcome":        "*Maakolo Network*\n\nЗащищённая VPN-инфраструктура без логов.",
        "btn_profile":    "Профиль и VPN",
        "btn_settings":   "Настройки и отзывы",
        "settings_msg":   "*Настройки и отзывы*\n\nВыберите раздел:",
        "btn_login":      "Войти в аккаунт",
        "btn_logout":     "Выйти из аккаунта",
        "btn_lang":       "Язык",
        "btn_ios":        "Настройка iOS",
        "btn_pc":         "Настройка PC / macOS",
        "btn_support":    "Поддержка",
        "btn_bug":        "Найдена ошибка",
        "btn_review":     "Оставить отзыв",
        "btn_info":       "О системе",
        "btn_change_os":  "Сменить устройство",
        "btn_copy_creds": "Данные для входа",
        "btn_buy_access": "Купить доступ",
        "btn_renew":      "Продлить доступ",
        "btn_acc_settings":"⚙️ Настройки аккаунта",
        "acc_settings_msg":"*Настройки аккаунта*\n\nУправление ID, паролем и привязанным устройством.",
        "btn_delete":     "Удалить аккаунт",
        "btn_back":       "Назад",
        "btn_cancel":     "Отмена",
        "btn_close_conv": "Закрыть диалог",
        "lbl_password":   "Пароль",
        "pwd_hidden":     "_скрыт — нажмите «Настройки аккаунта»_",
        "no_sub":         "Нет активных подписок",
        "days":           "дн. осталось",
        "key_hint":       "Нажмите кнопку ниже, чтобы скопировать ключ подключения",
        "btn_copy_key_base":    "Скопировать ключ · Base Network",
        "btn_copy_key_stealth": "Скопировать ключ · Stealth Network",
        "btn_refresh":    "🔄 Обновить",
        "btn_fix_conn":   "🛠 Исправить соединение",
        "sys_status_lbl": "Статус:",
        "sys_status_ok":  "🟢 Connected",
        "switching_route":"Смена маршрута...",
        "route_switched": "Маршрут изменён. Нажмите «Обновить» чтобы получить новый ключ.",
        "route_no_sub":   "Нет активных подписок для смены маршрута.",
        "creds_msg":      "*Данные для входа*\n\nID: `{id}`\nПароль: `{pwd}`\n\n_Автоудаление через 30 с._",
        "btn_copy_id":    "Скопировать ID",
        "btn_copy_pwd":   "Скопировать Пароль",
        "loading":        "Загрузка...",
        "cancelled":      "Отменено.",
        "conv_closed":    "Диалог закрыт.",
        "ticket_sent":    "Отправлено. Инженер ответит здесь.\n\n_Можете добавить уточнения ниже._",
        "followup_sent":  "Дополнение отправлено.",
        "rate_limit":     "Лимит запросов. Подождите {sec}с.",
        "reply_prefix":   "*Ответ поддержки:*\n\n",
        "bug_prompt":     "*Отчёт об ошибке*\n\nОпишите проблему. Скриншоты приветствуются.\n\n_Можно отправить несколько сообщений._",
        "review_prompt":  "*Оставить отзыв*\n\nНапишите, что вы думаете о сервисе! Мы всё читаем.",
        "os_prompt":      "Укажите ОС вашего устройства:",
        "android_msg":    "Для Android используйте официальное приложение.\nВойдите с ID и Паролем из профиля.",
        "btn_dl_apk":     "Скачать клиент",
        "info_msg":       (
            "*Maakolo Network*\n\n"
            "Обфускация трафика через Hysteria 2 + QUIC.\n"
            "Для провайдеров соединение выглядит как обычный HTTPS/3.\n\n"
            "• Без логов — сессии не хранятся\n"
            "• Без лимитов — оптимальная маршрутизация\n"
            "• Обходит DPI-блокировки"
        ),
        "ios_msg":        (
            "*Настройка iOS*\n\n"
            "1. Установите *Hiddify* из App Store.\n"
            "2. Откройте *Профиль и VPN* и скопируйте ключ.\n"
            "3. В приложении Hiddify: нажмите *➕ Add Profile* → *Import from Clipboard*.\n\n"
            "⚠️ *Внимание:* Для Android используйте наш официальный APK для лучшей маршрутизации."
        ),
        "pc_msg":         (
            "*Настройка Windows / macOS*\n\n"
            "1. Загрузите *Hiddify* (Win/Mac) или *V2rayN* (Win) / *FoXray* (Mac).\n"
            "2. Скопируйте ключ доступа из *Профиль и VPN*.\n"
            "3. В приложении — *Add profile* → вставьте из буфера."
        ),
        "server_error":   "Ошибка соединения с сервером. Попробуйте через минуту.",
        "login_id_prompt": "*Вход в аккаунт*\n\nОтправьте ваш ID.\n\n_Пример:_ `1234567890123456`",
        "login_pwd_prompt": "*Вход в аккаунт*\n\nОтправьте пароль.\n_Сообщение будет удалено сразу._",
        "login_id_fmt":   "Неверный формат ID. Введите 16 цифр (пробелы допустимы).",
        "login_ok":       "Вход выполнен.",
        "login_out_ok":   "Выход выполнен.",
        "login_fail":     "Неверный ID или пароль.",
        "del_prompt":     "*Удаление аккаунта*\n\nВведите пароль от VPN для подтверждения.\n\n_Действие необратимо._",
        "del_2fa":        "Включена 2FA. Введите 6-значный код из приложения-аутентификатора:",
        "del_ok":         "Аккаунт удалён.",
        "choose_tariff":  "Выберите сеть:",
        "tariff_base":    "Base Network — базовое шифрование\nот {p_rub}₽ / {p_xtr} XTR в мес.",
        "tariff_stealth": "Stealth Network — максимальная маскировка\nот {p_rub}₽ / {p_xtr} XTR в мес.",
        "choose_months":  "Выберите срок подписки:",
        "mo_1": "1 месяц", "mo_3": "3 месяца", "mo_6": "6 месяцев", "mo_12": "12 месяцев",
        "choose_pay":     "Выберите способ оплаты:",
        "btn_fiat":       "Банковская карта / СБП",
        "btn_stars":      "Telegram Stars",
        "btn_crypto":     "Криптовалюта (TON / USDT / LTC / ETH)",
        "stars_note":     "Счёт ниже — оплатите там, профиль обновится автоматически.",
        "stars_loader":   "Обработка платежа...",
        "stars_ok":       "Оплата подтверждена. Доступ активирован.",
        "stars_err":      "Ошибка активации. Обратитесь в поддержку — транзакция сохранена.",
        "invoice_ready":  "*Счёт сформирован*\n\nПлан: {plan}\n\nНажмите кнопку ниже для оплаты.",
        "btn_pay_fiat":   "Перейти к оплате",
        "btn_profile_refresh": "Проверить профиль",
        "gw_err":         "Ошибка платёжного шлюза. Попробуйте позже.",
        "crypto_select":  "Выберите актив:",
        "usdt_min_btn":   "USDT (мин. 3 мес.)",
        "usdt_min_err":   "Минимум для USDT — $5. Выберите от 3 месяцев или другой актив.",
        "crypto_addr":    "*Транзакция {coin}*\n\nСумма: `{amount}` {coin}\nАдрес:\n`{wallet}`\n\n_Переведите точную сумму, затем нажмите кнопку ниже._",
        "btn_verify":     "Проверить транзакцию",
        "crypto_no_inv":  "Активный счёт не найден. Проверьте профиль.",
        "crypto_wait":    "Ещё не подтверждено. Попробуйте через момент.",
        "crypto_ok":      "Транзакция подтверждена. Доступ активирован.",
        "crypto_err":      "Ошибка верификации. Повторите или обратитесь в поддержку.",
        "crypto_inv_err": "Ошибка создания инвойса. Попробуйте позже.",
    },
    "FI": {
        "choose_lang":    "Select language / Выберите язык / Valitse kieli:",
        "onboard_msg":    "*Tervetuloa Maakolo Networkiin*\n\nOnko sinulla jo tili?",
        "onboard_create": "Luo uusi tili",
        "onboard_login":  "Kirjaudu olemassa olevalla ID:llä",
        "welcome":        "*Maakolo Network*\n\nTurvallinen, lokiton VPN-infrastruktuuri.",
        "btn_profile":    "Profiili ja VPN",
        "btn_settings":   "Asetukset ja palaute",
        "settings_msg":   "*Asetukset ja palaute*\n\nValitse osio:",
        "btn_login":      "Kirjaudu sisään",
        "btn_logout":     "Kirjaudu ulos",
        "btn_lang":       "Kieli",
        "btn_ios":        "iOS-asetukset",
        "btn_pc":         "PC / macOS",
        "btn_support":    "Tuki",
        "btn_bug":        "Ilmoita ongelmasta",
        "btn_review":     "Anna palautetta",
        "btn_info":       "Tietoa palvelusta",
        "btn_change_os":  "Vaihda laite",
        "btn_copy_creds": "Kirjautumistiedot",
        "btn_buy_access": "Osta pääsy",
        "btn_renew":      "Uudista pääsy",
        "btn_acc_settings":"⚙️ Tilin asetukset",
        "acc_settings_msg":"*Tilin asetukset*\n\nHallitse tunnuksiasi ja laitteitasi.",
        "btn_delete":     "Poista tili",
        "btn_back":       "Takaisin",
        "btn_cancel":     "Peruuta",
        "btn_close_conv": "Sulje keskustelu",
        "lbl_password":   "Salasana",
        "pwd_hidden":     "_piilotettu — paina «Tilin asetukset»_",
        "no_sub":         "Ei aktiivista tilausta",
        "days":           "pv. jäljellä",
        "key_hint":       "Kopioi pääsyavain alla olevalla painikkeella",
        "btn_copy_key_base":    "Kopioi avain · Base Network",
        "btn_copy_key_stealth": "Kopioi avain · Stealth Network",
        "btn_refresh":    "🔄 Päivitä",
        "btn_fix_conn":   "🛠 Korjaa yhteys",
        "sys_status_lbl": "Tila:",
        "sys_status_ok":  "🟢 Operational",
        "switching_route":"Vaihdetaan reittiä...",
        "route_switched": "Reitti vaihdettu. Paina «Päivitä» saadaksesi uuden avaimen.",
        "route_no_sub":   "Ei aktiivisia tilauksia reitin vaihtamiseen.",
        "creds_msg":      "*Tilin tiedot*\n\nID: `{id}`\nSalasana: `{pwd}`\n\n_Poistuu 30 s kuluttua._",
        "btn_copy_id":    "Kopioi ID",
        "btn_copy_pwd":   "Kopioi Salasana",
        "loading":        "Ladataan...",
        "cancelled":      "Peruutettu.",
        "conv_closed":    "Keskustelu suljettu.",
        "ticket_sent":    "Lähetetty. Insinööri vastaa tässä.\n\n_Voit jatkaa kirjoittamista alla._",
        "followup_sent":  "Lisäviesti lähetetty.",
        "rate_limit":     "Liikaa pyyntöjä. Odota {sec}s.",
        "reply_prefix":   "*Tuen vastaus:*\n\n",
        "bug_prompt":     "*Virheraportti*\n\nKerro virheestä tai anna palautetta.\n\n_Voit lähettää useita viestejä._",
        "review_prompt":  "*Anna palautetta*\n\nKerro mielipiteesi palvelusta!",
        "os_prompt":      "Valitse laitteesi käyttöjärjestelmä:",
        "android_msg":    "Androidille on virallinen sovellus.\nKirjaudu profiilisi ID:llä ja salasanalla.",
        "btn_dl_apk":     "Lataa sovellus",
        "info_msg":       (
            "*Maakolo Network*\n\n"
            "Liikenteen hämärtäminen Hysteria 2 + QUIC -tekniikalla.\n"
            "Palveluntarjoajille yhteytesi näyttää tavalliselta HTTPS/3:lta.\n\n"
            "• Ei lokeja — istuntoja ei tallenneta\n"
            "• Ei rajoituksia — optimaalinen reititys\n"
            "• Ohittaa DPI-estot"
        ),
        "ios_msg":        (
            "*iOS-määritys*\n\n"
            "1. Asenna *Hiddify* App Storesta.\n"
            "2. Avaa *Profiili ja VPN* ja kopioi pääsyavain.\n"
            "3. Hiddify-sovelluksessa: paina *➕ Add Profile* → *Import from Clipboard*.\n\n"
            "⚠️ *Huom:* Androidille käytä aina virallista APK:tamme paremman reitityksen varmistamiseksi."
        ),
        "pc_msg":         (
            "*Windows / macOS -määritys*\n\n"
            "1. Lataa *Hiddify* (Win/Mac) tai *V2rayN* (Win) / *FoXray* (Mac).\n"
            "2. Kopioi pääsyavain kohdasta *Profiili ja VPN*.\n"
            "3. Sovelluksessa — *Add profile* → liitä leikepöydältä."
        ),
        "server_error":   "Palvelinvirhe. Yritä uudelleen minuutin kuluttua.",
        "login_id_prompt": "*Kirjaudu sisään*\n\nLähetä tilisi ID.\n\n_Esimerkki:_ `1234567890123456`",
        "login_pwd_prompt": "*Kirjaudu sisään*\n\nLähetä salasanasi.\n_Viesti poistetaan välittömästi._",
        "login_id_fmt":   "Virheellinen ID. Syötä 16 numeroa (välilyönnit sallittu).",
        "login_ok":       "Kirjautuminen onnistui.",
        "login_out_ok":   "Kirjauduttu ulos.",
        "login_fail":     "Virheellinen ID tai salasana.",
        "del_prompt":     "*Poista tili*\n\nSyötä VPN-salasanasi vahvistaaksesi.\n\n_Tämä on peruuttamatonta._",
        "del_2fa":        "2FA on käytössä. Syötä 6-numeroinen koodi authenticator-sovelluksestasi:",
        "del_ok":         "Tili poistettu.",
        "choose_tariff":  "Valitse palvelutaso:",
        "tariff_base":    "Base Network — perussalaus\nalkaen {p_xtr} XTR / kk",
        "tariff_stealth": "Stealth Network — maksimaalinen hämärtäminen\nalkaen {p_xtr} XTR / kk",
        "choose_months":  "Valitse tilauksen kesto:",
        "mo_1": "1 kuukausi", "mo_3": "3 kuukautta", "mo_6": "6 kuukautta", "mo_12": "12 kuukautta",
        "choose_pay":     "Valitse maksutapa:",
        "btn_fiat":       "Pankkikortti",
        "btn_stars":      "Telegram Stars",
        "btn_crypto":     "Krypto (TON / USDT / LTC / ETH)",
        "stars_note":     "Lasku alla — maksa sieltä, profiili päivittyy automaattisesti.",
        "stars_loader":   "Käsitellään maksua...",
        "stars_ok":       "Maksu vahvistettu. Pääsy aktivoitu.",
        "stars_err":      "Aktivointivirhe. Ota yhteyttä tukeen — tapahtuma on tallennettu.",
        "invoice_ready":  "*Lasku valmis*\n\nSuunnitelma: {plan}\n\nPaina alla maksaaksesi.",
        "btn_pay_fiat":   "Siirry maksamaan",
        "btn_profile_refresh": "Tarkista profiili",
        "gw_err":         "Maksuportaalin virhe. Yritä myöhemmin.",
        "crypto_select":  "Valitse valuutta:",
        "usdt_min_btn":   "USDT (min. 3 kk)",
        "usdt_min_err":   "USDT-minimi on $5. Valitse 3+ kuukautta tai toinen valuutta.",
        "crypto_addr":    "*{coin}-tapahtuma*\n\nMäärä: `{amount}` {coin}\nOsoite:\n`{wallet}`\n\n_Siirrä tarkka summa, paina sitten alla olevaa painiketta._",
        "btn_verify":     "Vahvista tapahtuma",
        "crypto_no_inv":  "Aktiivista laskua ei löydy. Tarkista profiilisi.",
        "crypto_wait":    "Ei vielä vahvistettu. Yritä hetken kuluttua.",
        "crypto_ok":      "Tapahtuma vahvistettu. Pääsy aktivoitu.",
        "crypto_err":      "Vahvistus epäonnistui. Yritä uudelleen tai ota yhteyttä tukeen.",
        "crypto_inv_err": "Laskun luominen epäonnistui. Yritä myöhemmin.",
    },
}

def T(lang: str, key: str) -> str:
    return _TEXTS.get(lang, _TEXTS["EN"]).get(key, _TEXTS["EN"].get(key, key))

def _btn_set(key: str) -> set[str]:
    return {T(lg, key) for lg in _TEXTS}

PROFILE_BTNS  = _btn_set("btn_profile")
SETTINGS_BTNS = _btn_set("btn_settings")

# --- Keyboards ---

def kb_main(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=T(lang, "btn_profile"))],
                  [KeyboardButton(text=T(lang, "btn_settings"))]],
        resize_keyboard=True,
        input_field_placeholder="Maakolo Network",
    )

def kb_lang() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="FI", callback_data="lang_FI"),
        InlineKeyboardButton(text="EN", callback_data="lang_EN"),
        InlineKeyboardButton(text="RU", callback_data="lang_RU"),
    ]])

def kb_onboarding(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T(lang, "onboard_create"), callback_data="onboard_create")],
        [InlineKeyboardButton(text=T(lang, "onboard_login"),  callback_data="onboard_login")],
    ])

def kb_settings(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T(lang, "btn_lang"), callback_data="menu_lang"),
         InlineKeyboardButton(text=T(lang, "btn_info"), callback_data="menu_info")],
        [InlineKeyboardButton(text=T(lang, "btn_ios"),  callback_data="menu_ios"),
         InlineKeyboardButton(text=T(lang, "btn_pc"),   callback_data="menu_pc")],
        [InlineKeyboardButton(text=T(lang, "btn_review"), callback_data="menu_review"),
         InlineKeyboardButton(text=T(lang, "btn_bug"),    callback_data="menu_bug")],
        [InlineKeyboardButton(text=T(lang, "btn_support"), url=SUPPORT_URL)],
    ])

def kb_account_settings(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T(lang, "btn_copy_creds"), callback_data="copy_creds")],
        [InlineKeyboardButton(text=T(lang, "btn_change_os"),  callback_data="reset_os")],
        [InlineKeyboardButton(text=T(lang, "btn_logout"),     callback_data="logout_confirm")],
        [InlineKeyboardButton(text=T(lang, "btn_delete"),     callback_data="delete_start")],
        [InlineKeyboardButton(text=T(lang, "btn_back"),       callback_data="go_to_profile")],
    ])

def kb_creds_copy(lang: str, acc_id: str, acc_pwd: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T(lang, "btn_copy_id"), copy_text=CopyTextButton(text=acc_id))],
        [InlineKeyboardButton(text=T(lang, "btn_copy_pwd"), copy_text=CopyTextButton(text=acc_pwd))],
        [InlineKeyboardButton(text=T(lang, "btn_back"), callback_data="menu_account_settings")]
    ])

def kb_os() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="iOS",              callback_data="os_ios")],
        [InlineKeyboardButton(text="Windows / macOS", callback_data="os_pc")],
        [InlineKeyboardButton(text="Android",         callback_data="os_android")],
    ])

def kb_cancel(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T(lang, "btn_cancel"), callback_data="cancel_action")]
    ])

def kb_close_conv(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T(lang, "btn_close_conv"), callback_data="cancel_action")]
    ])

def kb_back(lang: str, custom_cb: str = "back_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T(lang, "btn_back"), callback_data=custom_cb)]
    ])

def kb_nav(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T(lang, "btn_profile"), callback_data="go_to_profile")],
        [InlineKeyboardButton(text=T(lang, "btn_back"),    callback_data="back_menu")],
    ])

def kb_android(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T(lang, "btn_dl_apk"),    callback_data="download_apk")],
        [InlineKeyboardButton(text=T(lang, "btn_change_os"), callback_data="reset_os")],
        [InlineKeyboardButton(text=T(lang, "btn_back"),      callback_data="back_menu")],
    ])

def kb_profile(lang: str, keys: dict[str, str], has_sub: bool) -> InlineKeyboardMarkup:
    rows = []

    for tariff in ("stealth", "base"):
        if tariff in keys:
            rows.append([InlineKeyboardButton(
                text=T(lang, f"btn_copy_key_{tariff}"),
                copy_text=CopyTextButton(text=keys[tariff])
            )])

    action_row = [InlineKeyboardButton(text=T(lang, "btn_refresh"), callback_data="refresh_profile")]
    if keys:
        action_row.append(InlineKeyboardButton(text=T(lang, "btn_fix_conn"), callback_data="trigger_failover"))
    rows.append(action_row)

    btn_sub_text = T(lang, "btn_renew") if has_sub else T(lang, "btn_buy_access")
    rows.append([InlineKeyboardButton(text=btn_sub_text, callback_data="buy_sub")])
    rows.append([InlineKeyboardButton(text=T(lang, "btn_acc_settings"), callback_data="menu_account_settings")])

    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_tariff(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Base Network",    callback_data="sel_tariff_base")],
        [InlineKeyboardButton(text="Stealth Network", callback_data="sel_tariff_stealth")],
        [InlineKeyboardButton(text=T(lang, "btn_back"), callback_data="back_menu")],
    ])

def kb_months(lang: str, tariff: str) -> InlineKeyboardMarkup:
    mk = lambda n: InlineKeyboardButton(
        text=T(lang, f"mo_{n}"), callback_data=f"sel_months_{tariff}_{n}"
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [mk(1)], [mk(3)], [mk(6)], [mk(12)],
        [InlineKeyboardButton(text=T(lang, "btn_back"), callback_data="buy_sub")],
    ])

def kb_pay_method(lang: str, tariff: str, months: int) -> InlineKeyboardMarkup:
    rows = []
    if lang == "RU":
        rows.append([InlineKeyboardButton(
            text=T(lang, "btn_fiat"), callback_data=f"pay_fiat_{tariff}_{months}"
        )])
    rows += [
        [InlineKeyboardButton(text=T(lang, "btn_stars"),  callback_data=f"pay_stars_{tariff}_{months}")],
        [InlineKeyboardButton(text=T(lang, "btn_crypto"), callback_data=f"crypto_sel_{tariff}_{months}")],
        [InlineKeyboardButton(text=T(lang, "btn_back"),   callback_data=f"sel_tariff_{tariff}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_crypto_coins(lang: str, tariff: str, months: int) -> InlineKeyboardMarkup:
    usdt = "USDT (TRC20)" if months >= 3 else T(lang, "usdt_min_btn")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="TON",  callback_data=f"pay_crypto_{tariff}_{months}_TON")],
        [InlineKeyboardButton(text="LTC",  callback_data=f"pay_crypto_{tariff}_{months}_LTC")],
        [InlineKeyboardButton(text=usdt,   callback_data=f"pay_crypto_{tariff}_{months}_USDT")],
        [InlineKeyboardButton(text="ETH",  callback_data=f"pay_crypto_{tariff}_{months}_ETH")],
        [InlineKeyboardButton(text=T(lang, "btn_back"),
                              callback_data=f"sel_months_{tariff}_{months}")],
    ])

def kb_crypto_verify(lang: str, tariff: str, months: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T(lang, "btn_verify"), callback_data="check_crypto")],
        [InlineKeyboardButton(text=T(lang, "btn_back"),
                              callback_data=f"crypto_sel_{tariff}_{months}")],
    ])

# --- FSM States ---

class TicketState(StatesGroup):
    review = State()
    bug    = State()

class LoginState(StatesGroup):
    waiting_id  = State()
    waiting_pwd = State()

class DeleteState(StatesGroup):
    waiting_pwd  = State()
    waiting_totp = State()

class AdminState(StatesGroup):
    broadcast         = State()
    broadcast_confirm = State()

# --- Rate Limiting ---

TICKET_COOLDOWN = 300

async def get_cooldown(uid: int, kind: str) -> int:
    d   = await get_user_data(uid)
    rem = TICKET_COOLDOWN - (time.time() - d.get(f"cd_{kind}", 0))
    return int(rem) if rem > 0 else 0

async def mark_cooldown(uid: int, kind: str):
    def _upd(d): d[f"cd_{kind}"] = time.time(); return d
    await update_user_data(uid, _upd)

# --- Helpers ---

async def _delete_after(msg: types.Message, delay: int):
    await asyncio.sleep(delay)
    try: await msg.delete()
    except Exception: pass

async def show_main_menu(chat_id: int, lang: str):
    await bot.send_message(chat_id, T(lang, "welcome"),
                           parse_mode="Markdown", reply_markup=kb_main(lang))

async def send_apk(uid: int, lang: str):
    if os.path.exists(APK_PATH):
        await bot.send_document(uid, FSInputFile(APK_PATH))
    else:
        err = "APK временно недоступен." if lang == "RU" else "APK temporarily unavailable."
        await bot.send_message(uid, err)

async def _forward_to_user(uid: int, msg: types.Message, caption: str):
    if msg.photo:
        await bot.send_photo(uid, msg.photo[-1].file_id, caption=caption, parse_mode="Markdown")
    elif msg.video:
        await bot.send_video(uid, msg.video.file_id, caption=caption, parse_mode="Markdown")
    elif msg.document:
        await bot.send_document(uid, msg.document.file_id, caption=caption, parse_mode="Markdown")
    elif msg.voice:
        await bot.send_voice(uid, msg.voice.file_id, caption=caption, parse_mode="Markdown")
    elif msg.sticker:
        await bot.send_message(uid, caption, parse_mode="Markdown")
        await bot.send_sticker(uid, msg.sticker.file_id)
    else:
        await bot.send_message(uid, caption, parse_mode="Markdown")

async def _send_ticket(msg: types.Message, label: str) -> types.Message | None:
    uid      = msg.from_user.id
    lang     = await get_lang(uid)
    username = f"@{msg.from_user.username}" if msg.from_user.username else "—"
    name     = msg.from_user.full_name or "—"
    text     = msg.text or msg.caption or "[media]"

    safe = text.replace("*","∗").replace("_","＿").replace("`","‵")\
               .replace("[","［").replace("]","］")
    safe = re.sub(r"(?i)password[\"']?\s*[:=]\s*[\"']?[^\s,}]+", "password=***", safe)
    safe = re.sub(r"(?i)(vless|hy2|hysteria2)://[^\s`]+", r"\1://***", safe)

    card = (
        f"*{label}*\n"
        f"ID: ⟨{uid}⟩ | {name} | {username} | {lang}\n\n"
        f"{safe}\n\n_Reply to respond._"
    )

    if msg.photo:    return await bot.send_photo(ADMIN_ID, msg.photo[-1].file_id, caption=card, parse_mode="Markdown")
    if msg.video:    return await bot.send_video(ADMIN_ID, msg.video.file_id, caption=card, parse_mode="Markdown")
    if msg.document: return await bot.send_document(ADMIN_ID, msg.document.file_id, caption=card, parse_mode="Markdown")
    if msg.voice:    return await bot.send_voice(ADMIN_ID, msg.voice.file_id, caption=card, parse_mode="Markdown")
    return await bot.send_message(ADMIN_ID, card, parse_mode="Markdown")

async def _show_profile(uid: int, lang: str, target):
    """target — Message или CallbackQuery."""
    if isinstance(target, types.Message):
        loader = await target.answer(T(lang, "loading"))
    else:
        loader = target.message
        await loader.edit_text(T(lang, "loading"))

    text, keys, has_sub = await load_profile(uid, lang)
    kb = kb_profile(lang, keys, has_sub) if keys or has_sub or text != T(lang, "server_error") else kb_back(lang, "go_to_profile")
    await loader.edit_text(text, parse_mode="Markdown", reply_markup=kb)

# --- Start & Onboarding ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    d   = await get_user_data(uid)

    args = message.text.split()
    if len(args) > 1:
        payload = args[1]; parts = payload.split("_"); action = parts[0]
        if action in ("bug", "support", "download", "pay"):
            lc   = parts[-1].upper()
            lang = lc if lc in _TEXTS else d.get("lang", "EN")
            if lc in _TEXTS: await set_lang(uid, lang)
            if action == "bug":
                await state.set_state(TicketState.bug)
                await message.answer(T(lang, "bug_prompt"), parse_mode="Markdown",
                                     reply_markup=kb_cancel(lang)); return
            if action == "download":
                await send_apk(uid, lang); return
            if action == "pay" and len(parts) >= 5:
                tariff    = parts[1]
                months    = int(parts[2]) if parts[2].isdigit() else 1
                target_id = parts[3]
                price     = (PRICE_BASE_XTR if tariff == "base" else PRICE_STEALTH_XTR) * months
                await message.answer_invoice(
                    title=f"Maakolo Network: {tariff.capitalize()}",
                    description=f"Access {months} mo.",
                    prices=[types.LabeledPrice(label="XTR", amount=price)],
                    payload=f"stars_{tariff}_{months}_{target_id}",
                    currency="XTR", provider_token=""
                ); return

    if "api_id" not in d:
        await set_lang(uid, d.get("lang", "EN"))
        if not d.get("lang"):
            await message.answer(T("EN", "choose_lang"), reply_markup=kb_lang())
        else:
            await message.answer(T(d.get("lang"), "onboard_msg"), parse_mode="Markdown",
                                 reply_markup=kb_onboarding(d.get("lang")))
        return

    lang = d.get("lang", "EN")
    await show_main_menu(uid, lang)

@dp.callback_query(F.data.startswith("lang_"))
async def cb_lang(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    lang = callback.data.split("_")[1]
    uid  = callback.from_user.id
    await set_lang(uid, lang)
    try: await callback.message.delete()
    except Exception: pass

    d = await get_user_data(uid)
    if "api_id" not in d:
        await bot.send_message(uid, T(lang, "onboard_msg"),
                               parse_mode="Markdown", reply_markup=kb_onboarding(lang))
    else:
        await show_main_menu(uid, lang)

@dp.callback_query(F.data == "onboard_create")
async def cb_onboard_create(callback: types.CallbackQuery):
    uid = callback.from_user.id
    lang = await get_lang(uid)
    loader = await callback.message.edit_text(T(lang, "loading"))

    acc_id, _ = await create_account(uid)
    if acc_id:
        try: await loader.delete()
        except: pass
        await show_main_menu(uid, lang)
    else:
        await loader.edit_text(T(lang, "server_error"))

@dp.callback_query(F.data == "onboard_login")
async def cb_onboard_login(callback: types.CallbackQuery, state: FSMContext):
    uid  = callback.from_user.id
    lang = await get_lang(uid)
    await callback.message.edit_text(T(lang, "login_id_prompt"),
                                     parse_mode="Markdown", reply_markup=kb_cancel(lang))
    await state.set_state(LoginState.waiting_id)

@dp.callback_query(F.data == "back_menu")
async def cb_back_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    lang = await get_lang(callback.from_user.id)
    try: await callback.message.delete()
    except Exception: pass
    await show_main_menu(callback.from_user.id, lang)

# --- Main Menu & Settings ---

@dp.message(F.text.in_(SETTINGS_BTNS))
async def menu_settings(message: types.Message, state: FSMContext):
    await state.clear()
    lang = await get_lang(message.from_user.id)
    await message.answer(T(lang, "settings_msg"), parse_mode="Markdown",
                         reply_markup=kb_settings(lang))

@dp.callback_query(F.data == "menu_lang")
async def cb_menu_lang(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    lang = await get_lang(callback.from_user.id)
    await callback.message.edit_text(T(lang, "choose_lang"), reply_markup=kb_lang())

@dp.callback_query(F.data == "menu_info")
async def cb_info(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    lang = await get_lang(callback.from_user.id)
    await callback.message.edit_text(T(lang, "info_msg"), parse_mode="Markdown",
                                     reply_markup=kb_nav(lang))

@dp.callback_query(F.data == "menu_ios")
async def cb_ios(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    lang = await get_lang(callback.from_user.id)
    await callback.message.edit_text(T(lang, "ios_msg"), parse_mode="Markdown",
                                     reply_markup=kb_nav(lang))

@dp.callback_query(F.data == "menu_pc")
async def cb_pc(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    lang = await get_lang(callback.from_user.id)
    await callback.message.edit_text(T(lang, "pc_msg"), parse_mode="Markdown",
                                     reply_markup=kb_nav(lang))

# --- Profile & Account Settings ---

@dp.message(F.text.in_(PROFILE_BTNS))
async def menu_profile(message: types.Message, state: FSMContext):
    await state.clear()
    uid  = message.from_user.id
    lang = await get_lang(uid)
    os_v = await get_os(uid)

    if os_v == "android":
        await message.answer(T(lang, "android_msg"), parse_mode="Markdown",
                             reply_markup=kb_android(lang)); return
    if os_v in ("ios", "pc"):
        await _show_profile(uid, lang, message); return
    await message.answer(T(lang, "os_prompt"), reply_markup=kb_os())

@dp.callback_query(F.data == "go_to_profile")
async def cb_go_profile(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    uid  = callback.from_user.id
    lang = await get_lang(uid)
    os_v = await get_os(uid)

    if os_v == "android":
        await callback.message.edit_text(T(lang, "android_msg"), parse_mode="Markdown",
                                         reply_markup=kb_android(lang)); return
    if os_v in ("ios", "pc"):
        await _show_profile(uid, lang, callback); return
    await callback.message.edit_text(T(lang, "os_prompt"), reply_markup=kb_os())

@dp.callback_query(F.data == "refresh_profile")
async def cb_refresh_profile(callback: types.CallbackQuery):
    uid = callback.from_user.id
    lang = await get_lang(uid)
    await callback.answer(T(lang, "loading"))
    await _show_profile(uid, lang, callback)

async def _do_switch(session, acc_id, acc_pwd, tariff):
    try:
        async with session.post(
            f"{API_URL}/switch_slot",
            json={"account_id": acc_id, "password": acc_pwd, "tariff": tariff}
        ) as r:
            return tariff, await r.json() if r.status == 200 else None
    except Exception as e:
        return tariff, e

@dp.callback_query(F.data == "trigger_failover")
async def cb_failover(callback: types.CallbackQuery):
    uid = callback.from_user.id
    lang = await get_lang(uid)
    acc_id, acc_pwd = await get_account(uid)

    if not acc_id:
        await callback.answer(T(lang, "server_error"), show_alert=True)
        return

    await callback.answer(T(lang, "switching_route"))

    try:
        s = get_session()
        async with s.post(f"{API_URL}/login",
                          json={"account_id": acc_id, "password": acc_pwd}) as r:
            ld = await r.json() if r.status == 200 else {}

        now_ms = int(time.time() * 1000)
        active_tariffs = []
        if ld.get("expiry_base", 0) > now_ms:
            active_tariffs.append("base")
        if ld.get("expiry_stealth", 0) > now_ms:
            active_tariffs.append("stealth")

        if not active_tariffs:
            await callback.answer(T(lang, "route_no_sub"), show_alert=True)
            return

        switch_tasks = [
            _do_switch(s, acc_id, acc_pwd, t)
            for t in active_tariffs
        ]
        results = await asyncio.gather(*switch_tasks)

        for tariff, result in results:
            if isinstance(result, Exception):
                log.error(f"switch_slot {tariff} uid={uid}: {result}")
            elif result is None:
                log.warning(f"switch_slot {tariff} uid={uid}: non-200 response")

        await _show_profile(uid, lang, callback)

    except Exception as e:
        log.error(f"Failover error uid={uid}: {e}")
        await callback.answer(T(lang, "server_error"), show_alert=True)

@dp.callback_query(F.data == "menu_account_settings")
async def cb_account_settings(callback: types.CallbackQuery):
    uid = callback.from_user.id
    lang = await get_lang(uid)
    await callback.message.edit_text(T(lang, "acc_settings_msg"), parse_mode="Markdown",
                                     reply_markup=kb_account_settings(lang))

@dp.callback_query(F.data == "logout_confirm")
async def cb_logout(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    uid = callback.from_user.id
    lang = await get_lang(uid)

    await clear_account_state(uid)

    await callback.message.edit_text(T(lang, "login_out_ok"))
    await bot.send_message(uid, T(lang, "onboard_msg"), parse_mode="Markdown", reply_markup=kb_onboarding(lang))

@dp.callback_query(F.data.startswith("os_"))
async def cb_os(callback: types.CallbackQuery):
    os_val = callback.data.split("_")[1]
    uid    = callback.from_user.id
    lang   = await get_lang(uid)
    await set_os(uid, os_val)

    if os_val == "android":
        await callback.message.edit_text(T(lang, "android_msg"), parse_mode="Markdown",
                                         reply_markup=kb_android(lang))
    else:
        await _show_profile(uid, lang, callback)

@dp.callback_query(F.data == "reset_os")
async def cb_reset_os(callback: types.CallbackQuery):
    uid = callback.from_user.id
    def _upd(d): d.pop("os", None); return d
    await update_user_data(uid, _upd)
    lang = await get_lang(uid)
    await callback.message.edit_text(T(lang, "os_prompt"), reply_markup=kb_os())

@dp.callback_query(F.data == "copy_creds")
async def cb_copy_creds(callback: types.CallbackQuery):
    uid  = callback.from_user.id
    lang = await get_lang(uid)
    acc_id, acc_pwd = await get_account(uid)
    if acc_id:
        msg = await callback.message.answer(
            T(lang, "creds_msg").format(id=acc_id, pwd=acc_pwd),
            parse_mode="Markdown", reply_markup=kb_creds_copy(lang, acc_id, acc_pwd)
        )
        asyncio.create_task(_delete_after(msg, 30))
        await callback.answer()
    else:
        await callback.answer(T(lang, "server_error"), show_alert=True)

@dp.callback_query(F.data == "download_apk")
async def cb_dl_apk(callback: types.CallbackQuery):
    lang = await get_lang(callback.from_user.id)
    await callback.answer()
    await send_apk(callback.from_user.id, lang)

# --- Payments ---

@dp.callback_query(F.data == "buy_sub")
async def cb_buy(callback: types.CallbackQuery):
    lang = await get_lang(callback.from_user.id)
    t_base = T(lang, 'tariff_base').format(p_rub=PRICE_BASE_RUB, p_xtr=PRICE_BASE_XTR)
    t_stealth = T(lang, 'tariff_stealth').format(p_rub=PRICE_STEALTH_RUB, p_xtr=PRICE_STEALTH_XTR)
    text = f"{T(lang,'choose_tariff')}\n\n{t_base}\n\n{t_stealth}"
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb_tariff(lang))

@dp.callback_query(F.data.startswith("sel_tariff_"))
async def cb_sel_tariff(callback: types.CallbackQuery):
    tariff = callback.data.split("_")[2]
    lang   = await get_lang(callback.from_user.id)
    label  = "Base Network" if tariff == "base" else "Stealth Network"
    await callback.message.edit_text(
        f"*{label}* — {T(lang,'choose_months')}",
        parse_mode="Markdown", reply_markup=kb_months(lang, tariff)
    )

@dp.callback_query(F.data.startswith("sel_months_"))
async def cb_sel_months(callback: types.CallbackQuery):
    _, _, tariff, mo = callback.data.split("_")
    months = int(mo)
    lang   = await get_lang(callback.from_user.id)
    p_rub  = (PRICE_BASE_RUB if tariff == "base" else PRICE_STEALTH_RUB) * months
    p_xtr  = (PRICE_BASE_XTR if tariff == "base" else PRICE_STEALTH_XTR) * months
    price  = f"{p_rub}₽ / {p_xtr} XTR" if lang == "RU" else f"{p_xtr} XTR"
    label  = "Base Network" if tariff == "base" else "Stealth Network"
    await callback.message.edit_text(
        f"*{label} · {T(lang,f'mo_{months}')}*\n\n{T(lang,'choose_pay')}\n_{price}_",
        parse_mode="Markdown", reply_markup=kb_pay_method(lang, tariff, months)
    )

@dp.callback_query(F.data.startswith("pay_fiat_"))
async def cb_fiat(callback: types.CallbackQuery):
    _, _, tariff, mo = callback.data.split("_")
    months = int(mo); uid = callback.from_user.id; lang = await get_lang(uid)
    acc_id, _ = await get_account(uid)
    if not acc_id:
        await callback.answer(T(lang, "server_error"), show_alert=True); return

    await callback.message.edit_text(T(lang, "loading"))
    try:
        global bot_username
        if not bot_username:
            bot_info = await bot.get_me()
            bot_username = bot_info.username

        s = get_session()
        async with s.post(f"{API_URL}/create_fiat_invoice", json={
            "account_id": acc_id, "tariff": tariff, "months": months,
            "currency": "RUB", "method": "sbp",
            "return_url": f"https://t.me/{bot_username}"
        }) as r:
            res = await r.json()

        if res.get("status") == "success":
            plan = f"{'Base Network' if tariff=='base' else 'Stealth Network'} · {T(lang, f'mo_{months}')}"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=T(lang, "btn_pay_fiat"), url=res["pay_url"])],
                [InlineKeyboardButton(text=T(lang, "btn_profile_refresh"), callback_data="check_after_pay")],
                [InlineKeyboardButton(text=T(lang, "btn_back"), callback_data=f"sel_months_{tariff}_{months}")],
            ])
            await callback.message.edit_text(
                T(lang, "invoice_ready").format(plan=plan),
                parse_mode="Markdown", reply_markup=kb
            )
        else:
            await callback.message.edit_text(T(lang, "gw_err"), reply_markup=kb_back(lang))
    except Exception as e:
        log.error(f"fiat error uid={uid}: {e}")
        await callback.message.edit_text(T(lang, "gw_err"), reply_markup=kb_back(lang))

@dp.callback_query(F.data.startswith("pay_stars_"))
async def cb_stars(callback: types.CallbackQuery):
    _, _, tariff, mo = callback.data.split("_")
    months  = int(mo); lang = await get_lang(callback.from_user.id)
    price   = (PRICE_BASE_XTR if tariff == "base" else PRICE_STEALTH_XTR) * months
    acc_id, _ = await get_account(callback.from_user.id)
    if not acc_id:
        await callback.answer(T(lang, "server_error"), show_alert=True); return

    await callback.message.edit_text(T(lang, "stars_note"),
                                     parse_mode="Markdown", reply_markup=kb_back(lang))
    await callback.message.answer_invoice(
        title=f"Maakolo Network: {'Base' if tariff=='base' else 'Stealth'}",
        description=f"Access {months} mo.",
        prices=[types.LabeledPrice(label="XTR", amount=price)],
        payload=f"stars_{tariff}_{months}_{acc_id}",
        currency="XTR", provider_token=""
    )
    await callback.answer()

@dp.pre_checkout_query()
async def pre_checkout(q: types.PreCheckoutQuery):
    await q.answer(ok=True)

@dp.message(F.successful_payment)
async def on_payment(message: types.Message):
    uid  = message.from_user.id
    lang = await get_lang(uid)
    pay  = message.successful_payment
    parts = pay.invoice_payload.split("_")

    if len(parts) == 4 and parts[0] == "stars":
        tariff, months, target_id = parts[1], parts[2], parts[3]
        own_id, _ = await get_account(uid)

        loader = await message.answer(T(lang, "stars_loader"))

        if own_id != target_id:
            log.warning(f"Stars mismatch: uid={uid} paid for target={target_id}")

        try:
            s = get_session()
            async with s.post(f"{API_URL}/grant_stars_sub",
                              json={"account_id": target_id, "tariff": tariff,
                                    "months": int(months),
                                    "stars_amount": pay.total_amount,
                                    "telegram_charge_id": pay.telegram_payment_charge_id},
                              headers={"X-Bot-Secret": BOT_API_SECRET}) as r:
                ok = r.status == 200
        except Exception as e:
            log.error(f"stars api uid={uid}: {e}"); ok = False

        if ok:
            await loader.edit_text(T(lang, "stars_ok"))
            text, keys, has_sub = await load_profile(uid, lang)
            await message.answer(text, parse_mode="Markdown",
                                 reply_markup=kb_profile(lang, keys, has_sub))
        else:
            log.error(f"stars fail uid={uid} charge={pay.telegram_payment_charge_id}")
            await loader.edit_text(T(lang, "stars_err"))

@dp.callback_query(F.data.startswith("crypto_sel_"))
async def cb_crypto_sel(callback: types.CallbackQuery):
    _, _, tariff, mo = callback.data.split("_")
    lang = await get_lang(callback.from_user.id)
    await callback.message.edit_text(T(lang, "crypto_select"),
                                     reply_markup=kb_crypto_coins(lang, tariff, int(mo)))

@dp.callback_query(F.data.startswith("pay_crypto_"))
async def cb_pay_crypto(callback: types.CallbackQuery):
    parts  = callback.data.split("_")
    tariff, months, coin = parts[2], int(parts[3]), "_".join(parts[4:])
    uid    = callback.from_user.id; lang = await get_lang(uid)

    usd = (1.99 if tariff == "base" else 2.99) * months
    if coin == "USDT" and usd < 4.99:
        await callback.answer(T(lang, "usdt_min_err"), show_alert=True); return

    acc_id, _ = await get_account(uid)
    if not acc_id:
        await callback.answer(T(lang, "server_error"), show_alert=True); return

    await callback.message.edit_text(T(lang, "loading"))
    try:
        s = get_session()
        async with s.post(f"{API_URL}/create_crypto_invoice",
                          json={"account_id": acc_id, "tariff": tariff,
                                "months": months, "currency": coin}) as r:
            data = await r.json()
        if data.get("status") != "success":
            raise RuntimeError(data.get("message", ""))

        txn_id = data["txn_id"]; wallet = data["wallet_hash"]; amount = data["amount_crypto"]
        await set_crypto_pending(uid, txn_id)

        display = "USDT (TRC20)" if coin == "USDT" else coin
        await callback.message.edit_text(
            T(lang, "crypto_addr").format(coin=display, amount=amount, wallet=wallet),
            parse_mode="Markdown", reply_markup=kb_crypto_verify(lang, tariff, months)
        )
    except Exception as e:
        log.error(f"crypto inv uid={uid}: {e}")
        await callback.message.edit_text(T(lang, "crypto_inv_err"),
                                         reply_markup=kb_pay_method(lang, tariff, months))

@dp.callback_query(F.data == "check_crypto")
async def cb_check_crypto(callback: types.CallbackQuery):
    uid    = callback.from_user.id; lang = await get_lang(uid)
    txn_id = await get_crypto_pending(uid)
    if not txn_id:
        await callback.answer(T(lang, "crypto_no_inv"), show_alert=True); return

    await callback.answer("...")
    acc_id, acc_pwd = await get_account(uid)
    try:
        s = get_session()
        async with s.post(f"{API_URL}/check_crypto_tx",
                          json={"txn_id": txn_id, "account_id": acc_id,
                                "password": acc_pwd}) as r:
            data = await r.json()
        if data.get("paid"):
            await clear_crypto_pending(uid)
            await callback.message.edit_text(T(lang, "crypto_ok"), reply_markup=kb_back(lang))
            text, keys, has_sub = await load_profile(uid, lang)
            await callback.message.answer(text, parse_mode="Markdown",
                                          reply_markup=kb_profile(lang, keys, has_sub))
        else:
            await callback.answer(data.get("message", T(lang, "crypto_wait")), show_alert=True)
    except Exception as e:
        log.error(f"check_crypto uid={uid}: {e}")
        await callback.answer(T(lang, "crypto_err"), show_alert=True)

@dp.callback_query(F.data == "check_after_pay")
async def cb_check_after_pay(callback: types.CallbackQuery):
    uid  = callback.from_user.id; lang = await get_lang(uid)
    await callback.answer("Loading...")
    text, keys, has_sub = await load_profile(uid, lang)
    kb = kb_profile(lang, keys, has_sub) if keys or has_sub or text != T(lang, "server_error") else kb_back(lang, "go_to_profile")
    await callback.message.answer(text, parse_mode="Markdown", reply_markup=kb)

# --- Feedback & Bug Reports ---

@dp.callback_query(F.data == "menu_review")
async def cb_menu_review(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    uid  = callback.from_user.id; lang = await get_lang(uid)
    wait = await get_cooldown(uid, "review")
    if wait:
        await callback.answer(T(lang, "rate_limit").format(sec=wait), show_alert=True); return
    await callback.message.edit_text(T(lang, "review_prompt"),
                                     parse_mode="Markdown", reply_markup=kb_cancel(lang))
    await state.set_state(TicketState.review)

@dp.callback_query(F.data == "menu_bug")
async def cb_menu_bug(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    uid  = callback.from_user.id; lang = await get_lang(uid)
    wait = await get_cooldown(uid, "bug")
    if wait:
        await callback.answer(T(lang, "rate_limit").format(sec=wait), show_alert=True); return
    await callback.message.edit_text(T(lang, "bug_prompt"),
                                     parse_mode="Markdown", reply_markup=kb_cancel(lang))
    await state.set_state(TicketState.bug)

@dp.callback_query(F.data == "cancel_action")
async def cb_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    lang = await get_lang(callback.from_user.id)
    try:    await callback.message.edit_text(T(lang, "conv_closed"))
    except: await callback.message.edit_text(T(lang, "cancelled"))

@dp.message(TicketState.review)
@dp.message(TicketState.bug)
async def receive_ticket(message: types.Message, state: FSMContext):
    current  = await state.get_state()
    uid      = message.from_user.id; lang = await get_lang(uid)
    is_rev   = current == TicketState.review.state

    wait = await get_cooldown(uid, "review" if is_rev else "bug")

    if wait > TICKET_COOLDOWN - 5:
        await message.answer(T(lang, "rate_limit").format(sec=wait)); return

    s_data   = await state.get_data()
    is_first = not s_data.get("sent")
    label    = ("REVIEW" if is_rev else "BUG") + ("" if is_first else " [+]")

    try:
        sent = await _send_ticket(message, label)
        if sent: await _ticket_set(sent.message_id, uid)
        if is_first:
            await mark_cooldown(uid, "review" if is_rev else "bug")
            await state.update_data(sent=True)
        reply = T(lang, "ticket_sent") if is_first else T(lang, "followup_sent")
        await message.answer(reply, reply_markup=kb_close_conv(lang))
    except Exception as e:
        log.error(f"ticket uid={uid}: {e}")
        await message.answer(T(lang, "server_error"))
        await state.clear()

@dp.message(F.reply_to_message, F.from_user.id == ADMIN_ID)
async def admin_reply(message: types.Message, state: FSMContext):
    if await state.get_state() == AdminState.broadcast.state:
        return

    uid = await _ticket_get(message.reply_to_message.message_id)
    if not uid:
        orig = message.reply_to_message.text or message.reply_to_message.caption or ""
        match = re.search(r"ID:\s*⟨(\d+)⟩", orig)
        if not match:
            return
        try:
            uid = int(match.group(1))
        except Exception:
            await message.answer("Не удалось определить получателя."); return

    lang = await get_lang(uid)
    text = message.text or message.caption or ""
    safe = text.replace("*","∗").replace("_","＿").replace("`","‵")\
               .replace("[","［").replace("]","］")

    try:
        await _forward_to_user(uid, message, f"{T(lang,'reply_prefix')}{safe}")
        await message.answer(f"Доставлено → `{uid}`", parse_mode="Markdown")
    except Exception as e:
        log.error(f"admin_reply uid={uid}: {e}")
        await message.answer(f"Ошибка доставки.\n`{e}`", parse_mode="Markdown")

# --- Login ---

@dp.message(LoginState.waiting_id)
async def login_id(message: types.Message, state: FSMContext):
    uid  = message.from_user.id; lang = await get_lang(uid)
    text = (message.text or "").strip().replace(" ", "").replace("\u00a0", "")
    if not re.fullmatch(r"\d{16}", text):
        await message.answer(T(lang, "login_id_fmt"), reply_markup=kb_cancel(lang)); return
    await state.update_data(login_id=text)
    await message.answer(T(lang, "login_pwd_prompt"),
                         parse_mode="Markdown", reply_markup=kb_cancel(lang))
    await state.set_state(LoginState.waiting_pwd)

@dp.message(LoginState.waiting_pwd)
async def login_pwd(message: types.Message, state: FSMContext):
    try: await message.delete()
    except Exception: pass

    uid  = message.from_user.id; lang = await get_lang(uid)
    pwd  = (message.text or "").strip()

    if not pwd:
        await message.answer(T(lang, "login_fail"), reply_markup=kb_cancel(lang)); return

    sd   = await state.get_data(); acc_id = sd.get("login_id")
    wait = await message.answer("...")

    try:
        s = get_session()
        async with s.post(f"{API_URL}/login",
                          json={"account_id": acc_id, "password": pwd}) as r:
            resp = await r.json()

        if resp.get("status") == "success":
            def _upd(d):
                d["api_id"] = acc_id; d["api_pwd"] = _enc(pwd)
                return d
            await update_user_data(uid, _upd)

            await wait.edit_text(T(lang, "login_ok"))
            await state.clear()

            text, keys, has_sub = await load_profile(uid, lang)
            kb = kb_profile(lang, keys, has_sub) if keys or has_sub or text != T(lang, "server_error") else kb_back(lang, "go_to_profile")
            await message.answer(text, parse_mode="Markdown", reply_markup=kb)
        else:
            await wait.edit_text(T(lang, "login_fail"), reply_markup=kb_cancel(lang))
    except Exception as e:
        log.error(f"login uid={uid}: {e}")
        await wait.edit_text(T(lang, "server_error"), reply_markup=kb_cancel(lang))

# --- Account Deletion ---

@dp.callback_query(F.data == "delete_start")
async def cb_delete_start(callback: types.CallbackQuery, state: FSMContext):
    lang = await get_lang(callback.from_user.id)
    await callback.message.edit_text(T(lang, "del_prompt"),
                                     parse_mode="Markdown", reply_markup=kb_cancel(lang))
    await state.set_state(DeleteState.waiting_pwd)

@dp.message(DeleteState.waiting_pwd)
async def delete_pwd(message: types.Message, state: FSMContext):
    try: await message.delete()
    except Exception: pass

    uid  = message.from_user.id; lang = await get_lang(uid)
    pwd  = (message.text or "").strip()
    acc_id, _ = await get_account(uid)
    if not acc_id:
        await message.answer(T(lang, "server_error")); await state.clear(); return

    wait = await message.answer("...")
    try:
        s = get_session()
        async with s.post(f"{API_URL}/login",
                          json={"account_id": acc_id, "password": pwd}) as r:
            ld = await r.json()

        if ld.get("status") == "2fa_required":
            await state.update_data(del_pwd=pwd)
            await wait.edit_text(T(lang, "del_2fa"), reply_markup=kb_cancel(lang))
            await state.set_state(DeleteState.waiting_totp); return

        if ld.get("status") != "success":
            await wait.edit_text(f"[!] {ld.get('message','Invalid password')}")
            await state.clear(); return

        async with s.post(f"{API_URL}/delete_account",
                          json={"account_id": acc_id, "password": pwd}) as r:
            dr = await r.json()

        if dr.get("status") == "success":
            await clear_account_state(uid)
            await wait.edit_text(T(lang, "del_ok"))
            await bot.send_message(uid, T(lang, "onboard_msg"), parse_mode="Markdown", reply_markup=kb_onboarding(lang))
        else:
            await wait.edit_text(f"[!] {dr.get('message','Error')}")
    except Exception as e:
        log.error(f"delete uid={uid}: {e}")
        await wait.edit_text(T(lang, "server_error"))
    await state.clear()

@dp.message(DeleteState.waiting_totp)
async def delete_totp(message: types.Message, state: FSMContext):
    uid  = message.from_user.id; lang = await get_lang(uid)
    code = (message.text or "").strip()
    sd   = await state.get_data(); pwd = sd.get("del_pwd")
    acc_id, _ = await get_account(uid)
    wait = await message.answer("...")

    try:
        s = get_session()
        async with s.post(f"{API_URL}/delete_account",
                          json={"account_id": acc_id, "password": pwd,
                                "totp_code": code}) as r:
            dr = await r.json()
        if dr.get("status") == "success":
            await clear_account_state(uid)
            await wait.edit_text(T(lang, "del_ok"))
            await bot.send_message(uid, T(lang, "onboard_msg"), parse_mode="Markdown", reply_markup=kb_onboarding(lang))
        else:
            await wait.edit_text(f"[!] {dr.get('message','Invalid code')}")
    except Exception as e:
        log.error(f"delete totp uid={uid}: {e}")
        await wait.edit_text(T(lang, "server_error"))
    await state.clear()

# --- Admin Panel ---

@dp.message(Command("stats"), F.from_user.id == ADMIN_ID)
async def cmd_stats(message: types.Message):
    ids = await get_all_user_ids()
    lc  = {"EN": 0, "RU": 0, "FI": 0}
    oc  = {"ios": 0, "pc": 0, "android": 0, "—": 0}
    for uid in ids:
        d = await get_user_data(uid)
        lg = d.get("lang", "EN"); lc[lg] = lc.get(lg, 0) + 1
        ov = d.get("os") or "—"; oc[ov] = oc.get(ov, 0) + 1
    tc = await _ticket_count()
    await message.answer(
        f"*Stats*\n\nUsers: *{len(ids)}*\n\n"
        f"Lang: RU {lc.get('RU',0)} | EN {lc.get('EN',0)} | FI {lc.get('FI',0)}\n"
        f"OS: iOS {oc.get('ios',0)} | PC {oc.get('pc',0)} | "
        f"Android {oc.get('android',0)} | Unknown {oc.get('—',0)}\n\n"
        f"Tickets in DB: {tc}",
        parse_mode="Markdown"
    )

@dp.message(Command("broadcast"), F.from_user.id == ADMIN_ID)
async def cmd_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "*Broadcast*\n\nSend a message to preview, then confirm.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Abort", callback_data="abort_broadcast")]
        ])
    )
    await state.set_state(AdminState.broadcast)

@dp.callback_query(F.data == "abort_broadcast", F.from_user.id == ADMIN_ID)
async def cb_abort_bc(callback: types.CallbackQuery, state: FSMContext):
    await state.clear(); await callback.message.edit_text("Aborted.")

@dp.message(AdminState.broadcast, F.from_user.id == ADMIN_ID)
async def bc_preview(message: types.Message, state: FSMContext):
    if message.reply_to_message:
        await state.clear(); await admin_reply(message, state); return
    await state.update_data(bc_msg=message.message_id)
    await state.set_state(AdminState.broadcast_confirm)
    await message.copy_to(ADMIN_ID)
    await message.answer("Preview above. Send to all?", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Send",   callback_data="bc_go"),
            InlineKeyboardButton(text="Cancel", callback_data="abort_broadcast"),
        ]]
    ))

@dp.callback_query(F.data == "bc_go", AdminState.broadcast_confirm)
async def bc_send(callback: types.CallbackQuery, state: FSMContext):
    d = await state.get_data(); msg_id = d.get("bc_msg")
    await state.clear()
    ids    = await get_all_user_ids()
    ok = fail = 0
    status = await callback.message.edit_text(f"Sending... 0/{len(ids)}")

    for i, uid in enumerate(ids):
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=ADMIN_ID, message_id=msg_id)
            ok += 1
        except TelegramRetryAfter as e:
            log.warning(f"bc retry after {e.retry_after} for uid={uid}")
            await asyncio.sleep(e.retry_after + 1)
            try: await bot.copy_message(uid, ADMIN_ID, msg_id); ok += 1
            except Exception as e_inner:
                log.warning(f"bc fail retry uid={uid}: {e_inner}")
                fail += 1
        except Exception as err:
            log.warning(f"bc fail uid={uid}: {err}")
            fail += 1

        if (i + 1) % 20 == 0:
            try: await status.edit_text(f"Sending... {i+1}/{len(ids)}")
            except: pass
        await asyncio.sleep(0.1)

    await status.edit_text(f"*Done*\n\nDelivered: {ok}\nFailed: {fail}",
                           parse_mode="Markdown")

@dp.message(Command("help"), F.from_user.id == ADMIN_ID)
async def cmd_help(message: types.Message):
    await message.answer(
        "*Admin commands*\n\n"
        "/stats — statistics\n"
        "/broadcast — mass message\n"
        "/help — this list\n\n"
        "_Reply to any ticket card to respond to a user._",
        parse_mode="Markdown"
    )

# --- Entry Point ---

async def main():
    log.info("Starting...")
    await bot.delete_webhook(drop_pending_updates=True)
    await _get_db()

    global bot_username
    bot_info = await bot.get_me()
    bot_username = bot_info.username

    log.info("Ready.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
