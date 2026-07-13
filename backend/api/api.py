from gevent import monkey
monkey.patch_all()

import flask
import psycopg2
from psycopg2 import pool
from contextlib import contextmanager
import random
import string
import time
import threading
import uuid
import json
import requests
import os
import hmac
import hashlib
import logging
import logging.handlers
import re
import subprocess
import pyotp
import urllib3
from dotenv import load_dotenv
from flask import Flask, render_template, session, redirect, request, jsonify, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
from itsdangerous import URLSafeTimedSerializer
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

app = Flask(__name__, template_folder='/var/www/maakolo/templates')

TOLERANCE = 0.99
_DUMMY_HASH = generate_password_hash("dummy_timing_protection")

_payment_session = requests.Session()
_retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503])
_payment_session.mount('https://', HTTPAdapter(max_retries=_retry))

# Глобальный лок для предотвращения Race Condition при перезаписи инбаундов X-UI
xui_sync_lock = threading.Lock()

# --- Logging ---

def setup_logger(name, filepath, level=logging.INFO):
    handler = logging.handlers.RotatingFileHandler(
        filepath, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8'
    )
    handler.setFormatter(logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.addHandler(handler)
    return logger

os.makedirs('/var/www/maakolo', exist_ok=True)
reports_logger   = setup_logger('reports',   '/var/www/maakolo/reports.log')
feedbacks_logger = setup_logger('feedbacks', '/var/www/maakolo/feedbacks.log')
app_logger       = setup_logger('app',       '/var/www/maakolo/app.log', level=logging.INFO)

class SensitiveDataFilter(logging.Filter):
    PATTERNS = [
        (r'(?i)password["\']?\s*[:=]\s*["\']?[^\s,}]+', 'password=***'),
        (r'(?i)vless://[^\s`]+', 'vless://***'),
        (r'(?i)hysteria2://[^\s`]+', 'hysteria2://***'),
        (r'(?i)hy2://[^\s`]+', 'hy2://***'),
    ]
    def filter(self, record):
        if isinstance(record.msg, str):
            for pattern, replacement in self.PATTERNS:
                record.msg = re.sub(pattern, replacement, record.msg)
        return True

for _logger in [reports_logger, feedbacks_logger, app_logger]:
    _logger.addFilter(SensitiveDataFilter())

env_secret = os.environ.get("FLASK_SECRET_KEY")
if not env_secret or env_secret == "default_secret_999":
    app.secret_key = os.urandom(32).hex()
else:
    app.secret_key = env_secret

app.config["PROPAGATE_EXCEPTIONS"] = True
token_serializer = URLSafeTimedSerializer(app.secret_key)

# --- Environment & Config ---

BASE_DIR          = os.getenv("APP_BASE_DIR", "/var/www/maakolo")
NODES_FILE        = os.path.join(BASE_DIR, "nodes.json")

DB_URL            = os.getenv('DB_URL')
PANEL_URL         = os.getenv("PANEL_URL", "").rstrip('/')
PANEL_API_TOKEN   = os.getenv("PANEL_API_TOKEN", "").replace("\"", "").replace("'", "").strip()
SERVER_IP         = os.getenv("SERVER_IP")
PLISIO_SECRET_KEY = os.getenv("PLISIO_SECRET_KEY")
PLATEGA_SHOP_ID   = os.getenv("PLATEGA_SHOP_ID")
PLATEGA_SECRET    = os.getenv("PLATEGA_SECRET")
BOT_API_SECRET    = os.getenv("BOT_API_SECRET")

PANEL_VERIFY_SSL_STR = os.getenv("PANEL_VERIFY_SSL", "false").lower()
if PANEL_VERIFY_SSL_STR == "false":
    PANEL_VERIFY_SSL = False
elif os.path.isfile(PANEL_VERIFY_SSL_STR):
    PANEL_VERIFY_SSL = PANEL_VERIFY_SSL_STR
else:
    PANEL_VERIFY_SSL = True

ADMIN_URL  = os.environ.get("ADMIN_PANEL_URL", "/nexus_core_override_b7X92Q")
env_admin  = os.environ.get("ADMIN_PANEL_PASSWORD")
if not env_admin or env_admin == "admin":
    raise ValueError("CRITICAL: ADMIN_PANEL_PASSWORD не задан или небезопасен в .env!")
ADMIN_PASS = env_admin

_missing = [k for k, v in {
    'DB_URL': DB_URL, 'PANEL_URL': PANEL_URL,
    'PANEL_API_TOKEN': PANEL_API_TOKEN,
    'SERVER_IP': SERVER_IP,
    'PLISIO_SECRET_KEY': PLISIO_SECRET_KEY,
    'PLATEGA_SHOP_ID': PLATEGA_SHOP_ID, 'PLATEGA_SECRET': PLATEGA_SECRET,
    'BOT_API_SECRET': BOT_API_SECRET,
}.items() if not v]
if _missing:
    raise ValueError(f"CRITICAL: Не заданы в .env: {', '.join(_missing)}")

PRICING = {
    'base':    {'RUB': 150,  'USD': 1.99, 'EUR': 1.89},
    'stealth': {'RUB': 200,  'USD': 2.99, 'EUR': 2.89},
}

SAFE_FINGERPRINTS = ['firefox', 'safari', 'ios', 'random']
BASE_TRAFFIC_LIMIT_BYTES = 40 * 1024 * 1024 * 1024

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.config['MAX_CONTENT_LENGTH'] = 1 * 1024 * 1024

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["1000 per day", "100 per hour"],
    storage_uri=REDIS_URL,
)

@limiter.request_filter
def ip_whitelist():
    return get_remote_address() == '127.0.0.1'

# --- Localization ---

API_MESSAGES = {
    "ru": {
        "err_auth":             "Неверный ID или пароль",
        "err_2fa_req":          "Введите код из Authenticator",
        "err_2fa_inv":          "Неверный код 2FA",
        "err_months":           "Неверный формат месяцев",
        "err_tariff_curr":      "Тариф или валюта не поддерживается",
        "err_data":             "Отсутствуют данные",
        "err_gateway":          "Ошибка шлюза",
        "err_conn_pay":         "Ошибка соединения с кассой",
        "err_period":           "Неверный срок",
        "err_tariff_not_found": "Тариф не найден",
        "err_cashbox":          "Ошибка кассы",
        "err_plisio":           "Ошибка сервера оплаты",
        "msg_wait_tx":          "Ожидаем подтверждения сети...",
        "msg_no_tx":            "Оплата еще не поступила.",
        "err_tx_not_found":     "Транзакция не найдена",
        "err_check":            "Ошибка проверки",
        "err_data_req":         "Нужны данные",
        "err_short_pass":       "Пароль слишком короткий",
        "err_id_taken":         "ID уже занят",
        "err_db":               "Ошибка БД",
        "err_sub_expired":      "ПОДПИСКА {tariff} ИСТЕКЛА",
        "err_acc_not_found":    "Аккаунт не найден",
        "err_token_expired":    "Сессия истекла. Войдите заново.",
    },
    "en": {
        "err_auth":             "Invalid ID or password",
        "err_2fa_req":          "Enter code from Authenticator",
        "err_2fa_inv":          "Invalid 2FA code",
        "err_months":           "Invalid months format",
        "err_tariff_curr":      "Tariff or currency not supported",
        "err_data":             "Missing data",
        "err_gateway":          "Gateway error",
        "err_conn_pay":         "Payment gateway connection error",
        "err_period":           "Invalid period",
        "err_tariff_not_found": "Tariff not found",
        "err_cashbox":          "Checkout error",
        "err_plisio":           "Payment server error",
        "msg_wait_tx":          "Waiting for network confirmation...",
        "msg_no_tx":            "Payment not received yet.",
        "err_tx_not_found":     "Transaction not found",
        "err_check":            "Verification error",
        "err_data_req":         "Data required",
        "err_short_pass":       "Password is too short",
        "err_id_taken":         "ID is already taken",
        "err_db":               "Database error",
        "err_sub_expired":      "{tariff} SUBSCRIPTION EXPIRED",
        "err_acc_not_found":    "Account not found",
        "err_token_expired":    "Session expired. Please login again.",
    },
    "fi": {
        "err_auth":             "Virheellinen ID tai salasana",
        "err_2fa_req":          "Syötä koodi Authenticatorista",
        "err_2fa_inv":          "Virheellinen 2FA-koodi",
        "err_months":           "Virheellinen kuukausimuoto",
        "err_tariff_curr":      "Tariffia tai valuuttaa ei tueta",
        "err_data":             "Tietoja puuttuu",
        "err_gateway":          "Yhdyskäytävävirhe",
        "err_conn_pay":         "Yhteysvirhe maksujärjestelmään",
        "err_period":           "Virheellinen ajanjakso",
        "err_tariff_not_found": "Tariffia ei löydy",
        "err_cashbox":          "Kassavirhe",
        "err_plisio":           "Maksupalvelimen virhe",
        "msg_wait_tx":          "Odotetaan verkon vahvistusta...",
        "msg_no_tx":            "Maksua ei ole vielä vastaanotettu.",
        "err_tx_not_found":     "Tapahtumaa ei löydy",
        "err_check":            "Tarkistusvirhe",
        "err_data_req":         "Tarvitaan tietoja",
        "err_short_pass":       "Salasana on liian lyhyt",
        "err_id_taken":         "ID on jo varattu",
        "err_db":               "Tietokantavirhe",
        "err_sub_expired":      "{tariff} TILAUS PÄÄTTYNYT",
        "err_acc_not_found":    "Tiliä ei löydy",
        "err_token_expired":    "Istunto vanhentui. Kirjaudu uudelleen.",
    }
}

def get_msg(key, req, **kwargs):
    lang = req.headers.get("Accept-Language", "en").lower()
    if lang not in API_MESSAGES:
        lang = "en"
    text = API_MESSAGES[lang].get(key, API_MESSAGES["en"].get(key, key))
    return text.format(**kwargs) if kwargs else text

class DynamicInbounds:
    def __init__(self, ttl: int = 30):
        self._cache    = None
        self._cache_ts = 0.0
        self._TTL      = ttl
        self._lock     = threading.Lock()

    def _load(self) -> dict:
        now = time.monotonic()
        if self._cache is not None and now - self._cache_ts < self._TTL:
            return self._cache
        with self._lock:
            now = time.monotonic()
            if self._cache is None or now - self._cache_ts >= self._TTL:
                try:
                    with open(NODES_FILE, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    self._cache    = data
                    self._cache_ts = now
                except Exception as e:
                    app_logger.error(f"Ошибка чтения nodes.json: {e}")
                    if self._cache is None:
                        self._cache = {"base": [], "stealth": []}
        return self._cache

    def __getitem__(self, key):
        return self._load()[key]

    def __contains__(self, item):
        return item in self._load()

    def get(self, key, default=None):
        return self._load().get(key, default)

INBOUNDS = DynamicInbounds(ttl=30)

try:
    db_pool = pool.ThreadedConnectionPool(5, 50, DB_URL)
    app_logger.info("Пул соединений с БД создан.")
except Exception as e:
    app_logger.error(f"Ошибка создания пула БД: {e}")
    db_pool = None

@contextmanager
def get_db():
    if db_pool is None:
        raise RuntimeError("DB pool not initialized")
    conn = None
    for _ in range(20):
        try:
            conn = db_pool.getconn()
            break
        except pool.PoolError:
            time.sleep(0.1)
    if conn is None:
        raise RuntimeError("DB pool exhausted")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        db_pool.putconn(conn)

def init_db():
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    account_id      TEXT PRIMARY KEY,
                    password_hash   TEXT,
                    balance         DOUBLE PRECISION DEFAULT 0.0,
                    expiry_base     BIGINT DEFAULT 0,
                    expiry_stealth  BIGINT DEFAULT 0,
                    base_slot       INTEGER DEFAULT 0,
                    stealth_slot    INTEGER DEFAULT 0,
                    totp_secret     TEXT,
                    password_changed_at BIGINT DEFAULT 0
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS processed_payments (
                    payment_id   TEXT PRIMARY KEY,
                    acc_id       TEXT NOT NULL,
                    tariff       TEXT NOT NULL,
                    processed_at BIGINT NOT NULL
                )
            ''')
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_account_id ON users(account_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_payments_id      ON processed_payments(payment_id)")
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS base_slot    INTEGER DEFAULT 0")
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS stealth_slot INTEGER DEFAULT 0")
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_secret  TEXT")
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_changed_at BIGINT DEFAULT 0")
    except Exception as e:
        app_logger.warning(f"init_db: {e}")

init_db()

# --- X-UI Panel: Atomic Inbound Overwrite ---

req_session = requests.Session()

def panel_login() -> bool:
    return bool(PANEL_API_TOKEN)

def panel_request(method: str, url: str, retries: int = 3, **kwargs):
    kwargs.setdefault("verify",  PANEL_VERIFY_SSL)
    kwargs.setdefault("timeout", 10)
    headers = kwargs.get("headers", {})
    headers["Accept"]        = "application/json"
    headers["Authorization"] = f"Bearer {PANEL_API_TOKEN}"
    kwargs["headers"] = headers

    for attempt in range(retries):
        try:
            return req_session.request(method, url, **kwargs)
        except requests.exceptions.RequestException as e:
            app_logger.warning(f"Panel request failed (attempt {attempt+1}/{retries}): {e}")
            time.sleep(1)  
            
    dummy_resp = requests.Response()
    dummy_resp.status_code = 500
    dummy_resp._content = b'{"success": false, "msg": "Panel unreachable after retries"}'
    return dummy_resp

def get_client_data(protocol, u_uuid, email, expiry, enable, tariff):
    is_hysteria = protocol in ('hysteria', 'hysteria2')
    data = {
        "email":      email,
        "limitIp":    0,
        "totalGB":    0 if is_hysteria else (BASE_TRAFFIC_LIMIT_BYTES if tariff == "base" else 0),
        "expiryTime": expiry,
        "enable":     enable,
        "tgId":       0,
        "subId":      ""
    }
    if is_hysteria:
        data["password"] = u_uuid
    else:
        data["id"]   = u_uuid
        data["flow"] = "xtls-rprx-vision"
    return data

def sync_xui_client(inbound_id_raw, u_uuid, email, expiry, enable=True, tariff="stealth", protocol="vless"):
    """
    Атомарное обновление инбаунда: скачиваем, модифицируем массив клиентов, сохраняем.
    """
    if not panel_login(): return False
    try: inbound_id = int(inbound_id_raw)
    except: return False

    with xui_sync_lock:
        r = panel_request("GET", f"{PANEL_URL}/panel/api/inbounds/list")
        if r.status_code != 200: return False
        
        inbounds = r.json().get("obj", [])
        target = next((ib for ib in inbounds if ib["id"] == inbound_id), None)
        if not target: return False

        settings_obj = target.get("settings", {})
        if isinstance(settings_obj, str): 
            settings_obj = json.loads(settings_obj)

        clients = settings_obj.get("clients", [])
        new_client = get_client_data(protocol, u_uuid, email, expiry, enable, tariff)

        client_found = False
        for i, c in enumerate(clients):
            if c.get("email") == email:
                clients[i].update(new_client)
                client_found = True
                break

        if not client_found:
            clients.append(new_client)

        settings_obj["clients"] = clients
        stream_set = target.get("streamSettings", {})
        sniff_set = target.get("sniffing", {})

        payload = {
            "id": target["id"],
            "up": target["up"],
            "down": target["down"],
            "total": target["total"],
            "remark": target["remark"],
            "enable": target["enable"],
            "expiryTime": target["expiryTime"],
            "listen": target.get("listen", ""),
            "port": target["port"],
            "protocol": target["protocol"],
            "settings": json.dumps(settings_obj),
            "streamSettings": json.dumps(stream_set) if isinstance(stream_set, dict) else stream_set,
            "sniffing": json.dumps(sniff_set) if isinstance(sniff_set, dict) else sniff_set
        }

        resp = panel_request("POST", f"{PANEL_URL}/panel/api/inbounds/update/{inbound_id}", json=payload)
        if resp.status_code == 200 and resp.json().get("success"):
            app_logger.info(f"sync_xui_client: Успешно обновлен инбаунд {inbound_id} для {email}")
            return True
            
        app_logger.error(f"sync_xui_client: Ошибка обновления {email}. HTTP {resp.status_code}. Body: {resp.text[:300]}")
        return False

def delete_xui_client(inbound_id_raw, u_uuid):
    """
    Атомарное удаление клиента из инбаунда.
    """
    if not panel_login(): return
    try: inbound_id = int(inbound_id_raw)
    except: return

    with xui_sync_lock:
        r = panel_request("GET", f"{PANEL_URL}/panel/api/inbounds/list")
        if r.status_code != 200: return
        
        inbounds = r.json().get("obj", [])
        target = next((ib for ib in inbounds if ib["id"] == inbound_id), None)
        if not target: return

        settings_obj = target.get("settings", {})
        if isinstance(settings_obj, str): 
            settings_obj = json.loads(settings_obj)

        clients = settings_obj.get("clients", [])
        original_len = len(clients)
        
        clients = [c for c in clients if c.get("id") != u_uuid and c.get("password") != u_uuid]

        if len(clients) == original_len:
            return 

        settings_obj["clients"] = clients
        stream_set = target.get("streamSettings", {})
        sniff_set = target.get("sniffing", {})

        payload = {
            "id": target["id"],
            "up": target["up"],
            "down": target["down"],
            "total": target["total"],
            "remark": target["remark"],
            "enable": target["enable"],
            "expiryTime": target["expiryTime"],
            "listen": target.get("listen", ""),
            "port": target["port"],
            "protocol": target["protocol"],
            "settings": json.dumps(settings_obj),
            "streamSettings": json.dumps(stream_set) if isinstance(stream_set, dict) else stream_set,
            "sniffing": json.dumps(sniff_set) if isinstance(sniff_set, dict) else sniff_set
        }
        
        resp = panel_request("POST", f"{PANEL_URL}/panel/api/inbounds/update/{inbound_id}", json=payload)
        if resp.status_code == 200 and resp.json().get("success"):
            app_logger.info(f"delete_xui_client: Удален {u_uuid} из инбаунда {inbound_id}")

def xui_client_exists(email: str) -> bool:
    if not panel_login(): return False
    try:
        r = panel_request("GET", f"{PANEL_URL}/panel/api/inbounds/list")
        if r.status_code == 200:
            for ib in r.json().get("obj", []):
                settings = ib.get("settings", {})
                if isinstance(settings, str): 
                    settings = json.loads(settings)
                for c in settings.get("clients", []):
                    if c.get("email") == email:
                        return True
    except Exception as e:
        app_logger.warning(f"xui_client_exists check failed: {e}")
    return False

def reset_client_traffic(inbound_id_raw, email):
    if not panel_login(): return
    try:
        inbound_id = int(inbound_id_raw)
        resp = panel_request("POST", f"{PANEL_URL}/panel/api/inbounds/{inbound_id}/resetClientTraffic/{email}")
        if resp.status_code != 200 or not resp.json().get("success"):
            app_logger.warning(f"reset_client_traffic failed for {email}: {resp.text}")
    except Exception as e:
        app_logger.error(f"reset_client_traffic error: {e}")

# --- Authentication & Tokens ---

def authenticate_user(acc_id, password, totp_code=None, check_2fa=True):
    if not acc_id or not password:
        return False, "err_data_req", None

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT password_hash, totp_secret, balance, expiry_base, expiry_stealth, base_slot, stealth_slot '
            'FROM users WHERE account_id = %s',
            (acc_id,)
        )
        user = cursor.fetchone()

    if not user:
        check_password_hash(_DUMMY_HASH, password)
        return False, "err_auth", None

    if not check_password_hash(user[0], password):
        return False, "err_auth", None

    totp_secret = user[1]
    if check_2fa and totp_secret:
        if not totp_code:
            return False, "err_2fa_req", None
        if not pyotp.TOTP(totp_secret).verify(totp_code):
            return False, "err_2fa_inv", None

    return True, "ok", user

def verify_auth_request(req):
    auth_header = req.headers.get('Authorization')
    if auth_header and auth_header.startswith('Bearer '):
        token = auth_header.split(' ')[1]
        try:
            payload = token_serializer.loads(token, max_age=3 * 24 * 3600)
            token_acc_id = payload.get('acc_id')
            token_iat    = payload.get('iat', 0)
            if token_acc_id:
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        'SELECT password_hash, totp_secret, balance, expiry_base, expiry_stealth, '
                        'base_slot, stealth_slot, password_changed_at '
                        'FROM users WHERE account_id = %s', (token_acc_id,)
                    )
                    user = cursor.fetchone()
                    if user:
                        if token_iat < user[7]:
                            return False, "err_token_expired", None, None
                        return True, "ok", user, token_acc_id
        except Exception:
            return False, "err_token_expired", None, None

    data      = req.json or {}
    acc_id    = data.get('account_id')
    password  = data.get('password')
    totp_code = data.get('totp_code')

    is_valid, msg_key, user = authenticate_user(acc_id, password, totp_code=totp_code, check_2fa=True)
    return is_valid, msg_key, user, acc_id

@app.route('/me', methods=['GET'])
@limiter.limit("60 per minute")
def get_me():
    is_valid, msg_key, user_data, acc_id = verify_auth_request(request)
    if not is_valid:
        return jsonify({"status": "error", "message": get_msg(msg_key, request)}), 401

    new_token = token_serializer.dumps({'acc_id': acc_id, 'iat': int(time.time())})

    return jsonify({
        "status":         "success",
        "token":          new_token,
        "balance":        user_data[2],
        "expiry_base":    user_data[3],
        "expiry_stealth": user_data[4],
        "base_slot":      user_data[5],
        "stealth_slot":   user_data[6],
        "2fa_enabled":    user_data[1] is not None,
    })

# --- Subscription Management ---

def grant_subscription(acc_id: str, tariff: str, months: int, amount: float, payment_id: str | None = None) -> bool:
    if tariff not in INBOUNDS:
        return False

    now_ms  = int(time.time() * 1000)
    days_ms = months * 30 * 24 * 60 * 60 * 1000

    with get_db() as conn:
        cursor = conn.cursor()
        if payment_id:
            cursor.execute(
                "INSERT INTO processed_payments (payment_id, acc_id, tariff, processed_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (payment_id, acc_id, tariff, now_ms),
            )
            if cursor.rowcount == 0:
                return True

        if tariff == "base":
            cursor.execute("""
                UPDATE users
                SET expiry_base = GREATEST(expiry_base, %s) + %s, balance = balance + %s
                WHERE account_id = %s RETURNING base_slot, expiry_base
            """, (now_ms, days_ms, amount, acc_id))
        else:
            cursor.execute("""
                UPDATE users
                SET expiry_stealth = GREATEST(expiry_stealth, %s) + %s, balance = balance + %s
                WHERE account_id = %s RETURNING stealth_slot, expiry_stealth
            """, (now_ms, days_ms, amount, acc_id))

        row = cursor.fetchone()
        if not row:
            raise RuntimeError(f"User not found during grant: {acc_id}")
        current_slot, new_expiry = row

    if len(INBOUNDS[tariff]) == 0:
        return True

    slot         = current_slot % len(INBOUNDS[tariff])
    u_uuid       = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{acc_id}_{tariff}_{slot}"))
    client_email = f"Maakolo_{tariff}_{acc_id}_{slot}"
    protocol     = INBOUNDS[tariff][slot].get('protocol', 'vless')

    sync_xui_client(INBOUNDS[tariff][slot]['id'], u_uuid, client_email, new_expiry, tariff=tariff, protocol=protocol)
    
    # Возвращен сброс трафика для Base
    if tariff == "base":
        reset_client_traffic(INBOUNDS[tariff][slot]['id'], client_email)
        
    return True

@app.route('/generate_id', methods=['GET'])
@limiter.limit("5 per minute")
def get_new_id():
    found = False
    with get_db() as conn:
        cursor = conn.cursor()
        for _ in range(10):
            new_id = ''.join(random.choices(string.digits, k=16))
            cursor.execute('SELECT 1 FROM users WHERE account_id = %s', (new_id,))
            if not cursor.fetchone():
                found = True
                break
    if found:
        return jsonify({"account_id": new_id})
    return jsonify({"error": "ID generation failed"}), 500

@app.route('/register', methods=['POST'])
@limiter.limit("5 per minute")
def register():
    data     = request.json or {}
    acc_id   = data.get('account_id', '').strip()
    password = data.get('password', '')

    if not acc_id or not password:
        return jsonify({"error": get_msg("err_data_req", request)}), 400
    if not re.fullmatch(r'\d{16}', acc_id):
        return jsonify({"error": "Invalid account ID format"}), 400
    if len(password) < 8:
        return jsonify({"error": get_msg("err_short_pass", request)}), 400

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'INSERT INTO users (account_id, password_hash, balance, expiry_base, expiry_stealth) '
                'VALUES (%s, %s, 0.0, 0, 0) ON CONFLICT (account_id) DO NOTHING RETURNING account_id',
                (acc_id, generate_password_hash(password)),
            )
            inserted = cursor.fetchone()
        if inserted:
            return jsonify({"status": "success"}), 201
        return jsonify({"error": get_msg("err_id_taken", request)}), 409
    except Exception as e:
        app_logger.error(f"register error: {e}")
        return jsonify({"error": get_msg("err_db", request)}), 500

@app.route('/login', methods=['POST'])
@limiter.limit("10 per minute")
def login():
    data      = request.json or {}
    acc_id    = data.get('account_id')
    password  = data.get('password')
    totp_code = data.get('totp_code')

    is_valid, msg_key, user_data = authenticate_user(acc_id, password, totp_code, check_2fa=True)
    if not is_valid:
        status = "2fa_required" if msg_key == "err_2fa_req" else "error"
        return jsonify({"status": status, "message": get_msg(msg_key, request)}), 401

    token = token_serializer.dumps({'acc_id': acc_id, 'iat': int(time.time())})

    return jsonify({
        "status":         "success",
        "token":          token,
        "balance":        user_data[2],
        "expiry_base":    user_data[3],
        "expiry_stealth": user_data[4],
        "base_slot":      user_data[5],
        "stealth_slot":   user_data[6],
        "2fa_enabled":    user_data[1] is not None,
    })

@app.route('/change_password', methods=['POST'])
@limiter.limit("5 per minute")
def change_password():
    data         = request.json or {}
    acc_id       = data.get('account_id')
    old_password = data.get('old_password')
    new_password = data.get('new_password', '')
    totp_code    = data.get('totp_code')

    is_valid, msg_key, _ = authenticate_user(acc_id, old_password, totp_code, check_2fa=True)
    if not is_valid:
        return jsonify({"status": "error", "message": get_msg(msg_key, request)}), 401
    if not new_password or len(new_password) < 8:
        return jsonify({"status": "error", "message": get_msg("err_short_pass", request)}), 400

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET password_hash = %s, password_changed_at = %s WHERE account_id = %s",
            (generate_password_hash(new_password), int(time.time()), acc_id),
        )
    return jsonify({"status": "success"})

@app.route('/delete_account', methods=['POST'])
@limiter.limit("3 per minute")
def delete_account():
    data      = request.json or {}
    acc_id    = data.get('account_id')
    password  = data.get('password')
    totp_code = data.get('totp_code')

    user_cache = None
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT totp_secret, expiry_base, expiry_stealth, base_slot, stealth_slot, password_hash "
            "FROM users WHERE account_id = %s FOR UPDATE",
            (acc_id,),
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({"status": "error", "message": get_msg("err_acc_not_found", request)}), 404

        if not check_password_hash(row[5], password):
            return jsonify({"status": "error", "message": get_msg("err_auth", request)}), 401

        totp_secret = row[0]
        if totp_secret:
            if not totp_code:
                return jsonify({"status": "error", "message": get_msg("err_2fa_req", request)}), 401
            if not pyotp.TOTP(totp_secret).verify(totp_code):
                return jsonify({"status": "error", "message": get_msg("err_2fa_inv", request)}), 401

        user_cache = row

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE account_id = %s", (acc_id,))

    for tariff, slot, u_exp in [("base", user_cache[3], user_cache[1]), ("stealth", user_cache[4], user_cache[2])]:
        inbound_list = INBOUNDS.get(tariff, [])
        if u_exp > 0 and len(inbound_list) > 0:
            slot   = slot % len(inbound_list)
            u_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{acc_id}_{tariff}_{slot}"))
            delete_xui_client(inbound_list[slot]['id'], u_uuid)

    return jsonify({"status": "success"})

@app.route('/get_key', methods=['POST'])
@limiter.limit("30 per minute")
def get_key():
    data   = request.json or {}
    tariff = data.get('tariff', 'base')

    if tariff not in INBOUNDS:
        return jsonify({"status": "error", "message": get_msg("err_tariff_not_found", request)}), 400

    is_valid, msg_key, user_data, acc_id = verify_auth_request(request)
    if not is_valid:
        return jsonify({"status": "error", "message": get_msg(msg_key, request)}), 401

    expiry = user_data[3] if tariff == "base" else user_data[4]
    if expiry < int(time.time() * 1000):
        return jsonify({"status": "error", "message": get_msg("err_sub_expired", request, tariff=tariff.upper())}), 403

    if len(INBOUNDS[tariff]) == 0:
        return jsonify({"status": "error", "message": "No servers available"}), 500

    current_slot   = (user_data[5] if tariff == "base" else user_data[6]) % len(INBOUNDS[tariff])
    inbound_config = INBOUNDS[tariff][current_slot]
    u_uuid         = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{acc_id}_{tariff}_{current_slot}"))
    client_email   = f"Maakolo_{tariff}_{acc_id}_{current_slot}"
    protocol       = inbound_config.get('protocol', 'vless')

    if panel_login():
        if not xui_client_exists(client_email):
            app_logger.info(f"get_key: client {client_email} not found in x-ui, restoring via Full Update")
            sync_xui_client(inbound_config['id'], u_uuid, client_email, expiry, tariff=tariff, protocol=protocol)

    try:
        _fp = random.choice(SAFE_FINGERPRINTS)
        
        if protocol in ('hysteria', 'hysteria2'):
            vpn_link = (
                f"hysteria2://{u_uuid}@{SERVER_IP}:{inbound_config['port']}"
                f"?sni={inbound_config['sni']}&alpn=h3&insecure=1&obfs=salamander&obfs-password=maakolo&fp={_fp}#Maakolo_{tariff}"
            )
        else:
            vpn_link = (
                f"vless://{u_uuid}@{SERVER_IP}:{inbound_config['port']}"
                f"?type=tcp&security=reality&encryption=none"
                f"&pbk={inbound_config.get('pbk', '')}&fp={_fp}"
                f"&sni={inbound_config['sni']}&sid={inbound_config.get('sid', '')}"
                f"&flow=xtls-rprx-vision#Maakolo_{tariff}"
            )

        return jsonify({
            "status":     "success",
            "vless_link": vpn_link,
            "slot_info":  f"Слот {current_slot + 1} ({inbound_config['name']})",
        })
    except Exception as e:
        app_logger.error(f"get_key error: {e}")
        return jsonify({"status": "error"}), 500

@app.route('/switch_slot', methods=['POST'])
@limiter.limit("30 per minute")
def switch_slot():
    data   = request.json or {}
    tariff = data.get('tariff', 'base')

    if tariff not in INBOUNDS:
        return jsonify({"status": "error", "message": get_msg("err_tariff_not_found", request)}), 400

    is_valid, msg_key, user_data, acc_id = verify_auth_request(request)
    if not is_valid:
        return jsonify({"status": "error", "message": get_msg(msg_key, request)}), 401

    expiry = user_data[3] if tariff == "base" else user_data[4]
    if expiry < int(time.time() * 1000):
        return jsonify({"status": "error", "message": get_msg("err_sub_expired", request, tariff=tariff.upper())}), 403

    if len(INBOUNDS[tariff]) == 0:
        return jsonify({"status": "error", "message": "No servers available"}), 500

    current_slot = (user_data[5] if tariff == "base" else user_data[6]) % len(INBOUNDS[tariff])
    new_slot     = (current_slot + 1) % len(INBOUNDS[tariff])

    if new_slot != current_slot:
        old_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{acc_id}_{tariff}_{current_slot}"))
        delete_xui_client(INBOUNDS[tariff][current_slot]['id'], old_uuid)

    FIELD_MAP = {"base": "base_slot", "stealth": "stealth_slot"}
    field = FIELD_MAP.get(tariff)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE users SET {field} = %s WHERE account_id = %s", (new_slot, acc_id))

    new_inbound = INBOUNDS[tariff][new_slot]
    new_uuid    = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{acc_id}_{tariff}_{new_slot}"))
    new_email   = f"Maakolo_{tariff}_{acc_id}_{new_slot}"
    protocol    = new_inbound.get('protocol', 'vless')

    sync_xui_client(new_inbound['id'], new_uuid, new_email, expiry, tariff=tariff, protocol=protocol)

    try:
        _fp = random.choice(SAFE_FINGERPRINTS)
        if protocol in ('hysteria', 'hysteria2'):
            vpn_link = (
                f"hysteria2://{new_uuid}@{SERVER_IP}:{new_inbound['port']}"
                f"?sni={new_inbound['sni']}&alpn=h3&insecure=1&obfs=salamander&obfs-password=maakolo&fp={_fp}#Maakolo_{tariff}"
            )
        else:
            vpn_link = (
                f"vless://{new_uuid}@{SERVER_IP}:{new_inbound['port']}"
                f"?type=tcp&security=reality&encryption=none"
                f"&pbk={new_inbound.get('pbk', '')}&fp={_fp}"
                f"&sni={new_inbound['sni']}&sid={new_inbound.get('sid', '')}"
                f"&flow=xtls-rprx-vision#Maakolo_{tariff}"
            )
    except Exception:
        vpn_link = None

    return jsonify({
        "status":     "success",
        "new_slot":   new_slot,
        "slot_info":  f"Слот {new_slot + 1} ({new_inbound['name']})",
        "vless_link": vpn_link,
    })

@app.route('/get_traffic', methods=['POST'])
@limiter.limit("30 per minute")
def get_traffic():
    is_valid, msg_key, user_data, acc_id = verify_auth_request(request)
    if not is_valid:
        return jsonify({"status": "error", "message": get_msg(msg_key, request)}), 401

    expiry_base = user_data[3]
    if expiry_base < int(time.time() * 1000):
        return jsonify({"status": "error", "message": get_msg("err_sub_expired", request, tariff="BASE")}), 403

    inbound_list = INBOUNDS.get("base", [])
    if not inbound_list:
        return jsonify({"status": "error", "message": "No servers available"}), 500

    slot = user_data[5] % len(inbound_list)
    client_email = f"Maakolo_base_{acc_id}_{slot}"

    if not panel_login():
        return jsonify({"status": "error", "message": "Panel unavailable"}), 500

    try:
        r = panel_request("GET", f"{PANEL_URL}/panel/api/inbounds/list")
        if r.status_code == 200:
            for ib in r.json().get("obj", []):
                for stats in ib.get("clientStats", []):
                    if stats.get("email") == client_email:
                        used_bytes = int(stats.get("up", 0)) + int(stats.get("down", 0))
                        remaining  = max(0, BASE_TRAFFIC_LIMIT_BYTES - used_bytes)
                        return jsonify({
                            "status":          "success",
                            "used_bytes":      used_bytes,
                            "total_bytes":     BASE_TRAFFIC_LIMIT_BYTES,
                            "remaining_bytes": remaining,
                        })

        return jsonify({
            "status":          "success",
            "used_bytes":      0,
            "total_bytes":     BASE_TRAFFIC_LIMIT_BYTES,
            "remaining_bytes": BASE_TRAFFIC_LIMIT_BYTES,
        })
    except Exception as e:
        app_logger.error(f"get_traffic error for {acc_id}: {e}")
        return jsonify({"status": "error", "message": "Internal error"}), 500


@app.route('/check_2fa_status', methods=['POST'])
@limiter.limit("20 per minute")
def check_2fa_status():
    is_valid, msg_key, user_data, _ = verify_auth_request(request)
    if not is_valid:
        return jsonify({"status": "error", "message": get_msg(msg_key, request)}), 401
    return jsonify({"status": "success", "is_enabled": user_data[1] is not None})

@app.route('/enable_2fa', methods=['POST'])
@limiter.limit("5 per minute")
def enable_2fa():
    is_valid, msg_key, _, acc_id = verify_auth_request(request)
    if not is_valid:
        return jsonify({"status": "error", "message": get_msg(msg_key, request)}), 401

    data     = request.json or {}
    password = data.get('password')
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT password_hash FROM users WHERE account_id = %s', (acc_id,))
        row = cursor.fetchone()

    if not row or not password or not check_password_hash(row[0], password):
        return jsonify({"status": "error", "message": "Password required"}), 401

    secret = pyotp.random_base32()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET totp_secret = %s WHERE account_id = %s", (secret, acc_id))
    return jsonify({"status": "success", "secret": secret})

@app.route('/disable_2fa', methods=['POST'])
@limiter.limit("5 per minute")
def disable_2fa():
    data      = request.json or {}
    acc_id    = data.get('account_id')
    password  = data.get('password')
    totp_code = data.get('totp_code')

    is_valid, msg_key, _ = authenticate_user(acc_id, password, totp_code, check_2fa=True)
    if not is_valid:
        return jsonify({"status": "error", "message": get_msg(msg_key, request)}), 401

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET totp_secret = NULL WHERE account_id = %s", (acc_id,))
    return jsonify({"status": "success"})

@app.route('/pricing', methods=['GET'])
def get_pricing():
    return jsonify({"status": "success", "pricing": PRICING})

@app.route('/crypto_rates', methods=['GET'])
@limiter.limit("60 per minute")
def get_crypto_rates():
    try:
        resp = _payment_session.get(
            'https://api.binance.com/api/v3/ticker/price',
            params={'symbols': '["TONUSDT","LTCUSDT","ETHUSDT"]'},
            timeout=5,
            verify=True
        )
        if resp.status_code == 200:
            return resp.content, 200, {'Content-Type': 'application/json'}
        app_logger.error(f"crypto_rates: upstream returned {resp.status_code}")
        return jsonify({"error": "upstream_error"}), 502
    except Exception as e:
        app_logger.error(f"crypto_rates error: {e}")
        return jsonify({"error": "fetch_failed"}), 500

@app.route('/create_fiat_invoice', methods=['POST'])
@limiter.limit("15 per minute")
def create_fiat_invoice():
    data       = request.json or {}
    account_id = data.get('account_id')
    tariff     = data.get('tariff', 'base')
    currency   = data.get('currency', 'RUB')
    pay_method = data.get('method', 'card')
    return_url = data.get('return_url', 'https://maakolo.sbs:8445/success')

    try:
        months = int(data.get('months', 1))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": get_msg("err_months", request)}), 400

    if tariff not in PRICING or currency not in PRICING[tariff]:
        return jsonify({"status": "error", "message": get_msg("err_tariff_curr", request)}), 400
    if not account_id or not (1 <= months <= 999):
        return jsonify({"status": "error", "message": get_msg("err_data", request)}), 400

    method_id    = 2 if pay_method == 'sbp' else 11
    amount_float = float(PRICING[tariff]['RUB'] * months)
    order_id     = f"{account_id}_{tariff}_{months}_{int(time.time())}"

    payload = {
        "paymentMethod":  method_id,
        "paymentDetails": {"amount": amount_float, "currency": currency},
        "description":    f"Maakolo: {tariff.capitalize()} ({months} мес.)",
        "return":         return_url,
        "payload":        order_id,
    }
    headers = {
        "Content-Type": "application/json",
        "X-MerchantId": PLATEGA_SHOP_ID,
        "X-Secret":     PLATEGA_SECRET,
    }

    try:
        resp   = _payment_session.post("https://app.platega.io/transaction/process", json=payload, headers=headers, timeout=10, verify=True)
        result = resp.json()
        pay_url = result.get("redirect")
        if pay_url:
            return jsonify({"status": "success", "pay_url": pay_url})
        app_logger.error(f"Platega API Error: code={result.get('code')}")
        return jsonify({"status": "error", "message": result.get("message", get_msg("err_gateway", request))})
    except Exception as e:
        app_logger.error(f"create_fiat_invoice: {e}")
        return jsonify({"status": "error", "message": get_msg("err_conn_pay", request)}), 500

@app.route('/platega_webhook', methods=['POST'])
@limiter.exempt
def platega_webhook():
    merchant_id = request.headers.get('X-MerchantId')
    secret_key  = request.headers.get('X-Secret')

    if not merchant_id or not secret_key or not hmac.compare_digest(str(merchant_id), str(PLATEGA_SHOP_ID)) or not hmac.compare_digest(str(secret_key), str(PLATEGA_SECRET)):
        app_logger.warning(f"platega_webhook: Unauthorized IP={request.remote_addr}")
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    data   = request.json or {}
    status = str(data.get('status', '')).upper()

    if status in ('CONFIRMED', 'SUCCESS', 'PAID', 'COMPLETED', 'FINISHED'):
        order_id = str(data.get('payload', data.get('order_id', '')))
        parts    = order_id.split('_')
        if len(parts) < 4:
            return jsonify({"status": "error", "message": "Malformed payload"}), 400

        acc_id, tariff, months_str = parts[0], parts[1], parts[2]
        if tariff not in INBOUNDS: return jsonify({"status": "error", "message": "Invalid tariff"}), 400
        
        try: months = int(months_str)
        except: return jsonify({"status": "error", "message": "Invalid months"}), 400

        try:
            expected    = float(PRICING[tariff]['RUB'] * months)
            details     = data.get('paymentDetails', {})
            amount_paid = float(details.get('amount', data.get('amount', 0))) if isinstance(details, dict) else float(data.get('amount', 0))

            if amount_paid < expected:
                app_logger.error(f"FRAUD: order={order_id} paid={amount_paid} expected={expected}")
                return jsonify({"status": "error", "message": "Invalid amount"}), 400

            grant_subscription(acc_id, tariff, months, amount_paid, payment_id=order_id)
            app_logger.info(f"platega_webhook: OK order={order_id} acc={acc_id}")
        except Exception as e:
            app_logger.error(f"platega_webhook: grant failed: {e}")
            return jsonify({"status": "error", "message": "Internal DB error"}), 500

        return jsonify({"status": "success"}), 200
    elif status in ('CANCELED', 'CHARGEBACK', 'REJECTED', 'FAIL'):
        app_logger.info(f"platega_webhook: payment failed status={status}")
        return jsonify({"status": "success"}), 200

    return jsonify({"status": "ignored"}), 200

@app.route('/create_crypto_invoice', methods=['POST'])
@limiter.limit("10 per minute")
def create_crypto_invoice():
    data       = request.json or {}
    account_id = data.get('account_id')
    tariff     = data.get('tariff', 'base')
    raw_curr   = str(data.get('currency', 'USDT_TRX')).upper()

    currency_map = {
        'TON': 'TON', 'ETH': 'ETH', 'LTC': 'LTC',
        'USDT': 'USDT_TRX', 'USDT_TRX': 'USDT_TRX', 'USDT (TRC20)': 'USDT_TRX',
    }
    target_currency = currency_map.get(raw_curr, raw_curr)

    try: months = int(data.get('months', 1))
    except: return jsonify({"status": "error", "message": get_msg("err_period", request)}), 400

    if tariff not in PRICING: return jsonify({"status": "error", "message": get_msg("err_tariff_not_found", request)}), 400
    if not account_id or not (1 <= months <= 999): return jsonify({"status": "error", "message": get_msg("err_data", request)}), 400

    amount_usd   = round(PRICING[tariff]['USD'] * months, 2)
    order_number = f"{account_id}_{tariff}_{months}_{int(time.time())}"

    params = {
        "source_currency": "USD", "source_amount": amount_usd, "currency": target_currency,
        "order_name": f"Maakolo VPN | {tariff.capitalize()} ({months}m)", "order_number": order_number, "api_key": PLISIO_SECRET_KEY,
    }

    try:
        resp = _payment_session.get("https://api.plisio.net/api/v1/invoices/new", params=params, verify=True, timeout=10).json()
        if resp.get("status") == "success":
            d = resp["data"]
            return jsonify({
                "status": "success", "txn_id": d["txn_id"], "wallet_hash": d.get("wallet_hash", ""),
                "amount_crypto": d.get("invoice_total_sum", ""), "qr_code": d.get("qr_code", ""), "invoice_url": d.get("invoice_url", ""),
            })
        app_logger.error(f"Plisio error ({target_currency}): {resp.get('data', {}).get('message')}")
        return jsonify({"status": "error", "message": get_msg("err_cashbox", request)})
    except Exception as e:
        app_logger.error(f"create_crypto_invoice: {e}")
        return jsonify({"status": "error", "message": get_msg("err_plisio", request)}), 500

@app.route('/check_crypto_tx', methods=['POST'])
@limiter.limit("15 per minute")
def check_crypto_tx():
    data   = request.json or {}
    txn_id = data.get('txn_id')

    is_valid, msg_key, _, acc_id = verify_auth_request(request)
    if not is_valid: return jsonify({"status": "error", "message": "Unauthorized"}), 401

    try:
        resp = _payment_session.get(f"https://api.plisio.net/api/v1/operations/{txn_id}", params={"api_key": PLISIO_SECRET_KEY}, verify=True, timeout=10).json()
        if resp.get("status") == "success":
            tx_status    = resp["data"]["status"]
            order_number = resp["data"].get("order_number", "")
            if tx_status in ("completed", "mismatch"):
                parts = order_number.split('_')
                if len(parts) >= 3:
                    pay_acc_id, tariff, months_str = parts[0], parts[1], parts[2]
                    if pay_acc_id != acc_id: return jsonify({"status": "error", "message": "Transaction belongs to another user"}), 403
                    if tariff in INBOUNDS:
                        try:
                            months   = int(months_str)
                            amount   = float(resp["data"].get("source_amount", 0))
                            expected = float(PRICING[tariff]['USD'] * months)
                            if amount < expected * TOLERANCE: 
                                app_logger.error(f"CRYPTO FRAUD: {acc_id} paid={amount} expected={expected}")
                                return jsonify({"status": "success", "paid": False, "message": get_msg("msg_no_tx", request)})
                            if grant_subscription(acc_id, tariff, months, amount, payment_id=txn_id): return jsonify({"status": "success", "paid": True})
                            return jsonify({"status": "error", "message": "Activation failed"})
                        except Exception as e:
                            app_logger.error(f"check_crypto_tx grant: {e}")
                            return jsonify({"status": "error", "message": "Internal error"})
            elif tx_status == "pending": return jsonify({"status": "success", "paid": False, "message": get_msg("msg_wait_tx", request)})
            else: return jsonify({"status": "success", "paid": False, "message": get_msg("msg_no_tx", request)})
        return jsonify({"status": "error", "message": get_msg("err_tx_not_found", request)})
    except Exception as e:
        app_logger.error(f"check_crypto_tx: {e}")
        return jsonify({"status": "error", "message": get_msg("err_check", request)}), 500

@app.route('/plisio_webhook', methods=['POST'])
@limiter.exempt
def plisio_webhook():
    if not PLISIO_SECRET_KEY: return jsonify({"status": "error", "message": "Secret key not configured"}), 500
    plisio_signature = request.headers.get('X-Plisio-Signature')
    if not plisio_signature: return jsonify({"status": "error", "message": "Missing signature"}), 403

    raw_data = request.get_data()
    mac = hmac.new(PLISIO_SECRET_KEY.encode('utf-8'), raw_data, hashlib.sha256)
    if not hmac.compare_digest(mac.hexdigest(), plisio_signature): 
        app_logger.warning(f"plisio_webhook: Invalid signature IP={request.remote_addr}")
        return jsonify({"status": "error", "message": "Invalid signature"}), 403

    data   = request.json or {}
    status = data.get('status')
    if status in ('completed', 'mismatch'):
        order_number = str(data.get('order_number', ''))
        parts        = order_number.split('_')
        if len(parts) < 4: return jsonify({"status": "error", "message": "Malformed order_number"}), 400

        acc_id, tariff, months_str = parts[0], parts[1], parts[2]
        if tariff not in INBOUNDS: return jsonify({"status": "error", "message": "Invalid tariff"}), 400
        try: months = int(months_str)
        except: return jsonify({"status": "error", "message": "Invalid months"}), 400

        try:
            amount   = float(data.get('source_amount', 0))
            expected = float(PRICING[tariff]['USD'] * months)
            if amount < expected * TOLERANCE: 
                app_logger.error(f"PLISIO FRAUD: order={order_number} paid={amount} expected={expected}")
                return jsonify({"status": "error", "message": "Invalid amount"}), 400
            grant_subscription(acc_id, tariff, months, amount, payment_id=order_number)
            app_logger.info(f"plisio_webhook: OK order={order_number} acc={acc_id}")
            return jsonify({"status": "success"}), 200
        except Exception as e:
            app_logger.error(f"plisio_webhook: grant failed: {e}")
    return jsonify({"status": "ignored"}), 200

@app.route('/grant_stars_sub', methods=['POST'])
@limiter.limit("60 per hour")
def grant_stars_sub():
    client_secret = request.headers.get("X-Bot-Secret")
    if not client_secret or not hmac.compare_digest(client_secret, BOT_API_SECRET): 
        app_logger.warning(f"Unauthorized /grant_stars_sub IP={request.remote_addr}")
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    data       = request.json or {}
    account_id = data.get('account_id')
    tariff     = data.get('tariff')
    try:
        months     = int(data.get('months', 1))
        stars_paid = float(data.get('stars_amount', 0))
    except: return jsonify({"status": "error", "message": "Invalid data format"}), 400

    if not account_id or not tariff or not (1 <= months <= 999): return jsonify({"status": "error", "message": "Missing data"}), 400
    STARS_PRICING  = {'base': 145, 'stealth': 225}
    expected_stars = STARS_PRICING.get(tariff, 0) * months
    if stars_paid < expected_stars * TOLERANCE: 
        app_logger.error(f"STARS FRAUD: acc={account_id} paid={stars_paid} expected={expected_stars}")
        return jsonify({"status": "error", "message": "Invalid stars amount"}), 400

    try:
        telegram_charge_id = data.get('telegram_charge_id')
        payment_id = f"STARS_{telegram_charge_id}" if telegram_charge_id else f"STARS_{account_id}_{int(time.time())}"
        grant_subscription(account_id, tariff, months, stars_paid, payment_id=payment_id)
        app_logger.info(f"Stars: granted {tariff} for {account_id} ({stars_paid} XTR)")
        return jsonify({"status": "success"})
    except Exception as e:
        app_logger.error(f"grant_stars_sub: {e}")
        return jsonify({"status": "error", "message": "DB Error"}), 500

@app.route('/report_error', methods=['POST'])
@limiter.limit("5 per minute")
def report_error():
    data       = request.json or {}
    error_text = str(data.get('error_text', '')).strip()[:500].replace('\n', ' ').replace('\r', '')
    is_valid, _, _, acc_id = verify_auth_request(request)
    if not is_valid: return jsonify({"status": "error", "message": "Unauthorized"}), 401
    reports_logger.info(f"REPORT from {acc_id}: {error_text}")
    return jsonify({"status": "success"})

@app.route('/leave_feedback', methods=['POST'])
@limiter.limit("5 per minute")
def leave_feedback():
    data    = request.json or {}
    fb_text = str(data.get('feedback_text', '')).strip()[:500].replace('\n', ' ').replace('\r', '')
    rating  = int(data.get('rating', 5)) if str(data.get('rating', 5)).isdigit() else 5
    is_valid, _, _, acc_id = verify_auth_request(request)
    if not is_valid: return jsonify({"status": "error", "message": "Unauthorized"}), 401
    feedbacks_logger.info(f"FEEDBACK from {acc_id} ({rating}/5): {fb_text}")
    return jsonify({"status": "success"})

APP_VERSION_CONFIG = {
    "latest_version": "1.0.0", "min_required_version_android": "1.0.0", "min_required_version_ios": "1.0.0",
    "update_links": {"telegram": "https://t.me/maakolohelp_bot", "website":  "https://maakolo.sbs:8445/download/android"},
    "changelog": {
        "ru": "• Подготовка инфраструктуры\n• Улучшена стабильность соединения\n• Обновлён дизайн",
        "en": "• Infrastructure prep\n• Connection stability improved\n• Design updated",
        "fi": "• Infrastruktuurin valmistelu\n• Yhteyden vakautta parannettu\n• Suunnittelu päivitetty",
    }
}

def _version_tuple(v: str):
    try: return tuple(int(x) for x in str(v).split('.'))
    except: return (0, 0, 0)

@app.route('/check_version', methods=['GET'])
@limiter.limit("20 per minute")
def check_version():
    client_version = request.args.get('v', '0.0.0')
    platform       = request.args.get('platform', 'android').lower()
    latest  = APP_VERSION_CONFIG["latest_version"]
    min_req = APP_VERSION_CONFIG.get(f"min_required_version_{platform}", APP_VERSION_CONFIG.get("min_required_version_android", "1.0.0"))
    lang = request.headers.get("Accept-Language", "en").lower()
    if lang not in APP_VERSION_CONFIG["changelog"]: lang = "en"

    return jsonify({
        "has_update": _version_tuple(client_version) < _version_tuple(latest),
        "is_mandatory": _version_tuple(client_version) < _version_tuple(min_req),
        "latest_version": latest, "update_links": APP_VERSION_CONFIG.get("update_links", {}), "changelog": APP_VERSION_CONFIG["changelog"][lang],
    })

@app.route('/health', methods=['GET'])
@limiter.exempt
def health():
    try:
        with get_db() as conn: conn.cursor().execute("SELECT 1")
        return jsonify({"status": "ok"}), 200
    except: return jsonify({"status": "db_error"}), 500

@app.route('/')
def index(): return render_template('landing.html')

@app.route('/download/android')
@limiter.exempt
def download_android():
    try: return send_from_directory('/var/www/maakolo/downloads', 'maakolo.apk', as_attachment=True)
    except FileNotFoundError: return "APK пока не загружен.", 404

@app.route('/success')
def pay_success(): return "<h1>Оплата прошла успешно!</h1><p>Вернитесь в приложение, подписка уже активна.</p>"

@app.route('/fail')
def pay_fail(): return "<h1>Ошибка оплаты</h1><p>Попробуйте ещё раз или напишите в поддержку.</p>"

def _proc_running(name: str) -> bool:
    try: return subprocess.run(["pgrep", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except: return False

def get_sys_stats() -> dict:
    try: cpu = f"{os.getloadavg()[0]:.2f}"
    except: cpu = "0.00"
    try:
        with open('/proc/meminfo') as f: lines = f.readlines()
        total = int(lines[0].split()[1]); avail = int(lines[2].split()[1])
        ram = f"{((total - avail) / total * 100):.1f}"
    except: ram = "0.0"
    try:
        st = os.statvfs('/')
        disk = f"{((st.f_blocks - st.f_bavail) * st.f_frsize / (1024 ** 3)):.1f}GB"
    except: disk = "0GB"
    return {"cpu": cpu, "ram": ram, "disk": disk, "xray": "ONLINE" if _proc_running("xray") else "OFFLINE", "nginx": "ONLINE" if _proc_running("nginx") else "OFFLINE"}

@app.route(ADMIN_URL, methods=['GET', 'POST'])
def secret_admin():
    if request.method == 'GET' and 'csrf_token' not in session: session['csrf_token'] = os.urandom(16).hex()
    if request.args.get('logout'):
        session.pop('admin_auth', None)
        return redirect(ADMIN_URL)

    message = ""
    if request.method == 'POST':
        token_client = request.form.get('csrf_token')
        token_server = session.get('csrf_token')
        if not token_client or not hmac.compare_digest(token_client, str(token_server)): return render_template('admin.html', logged_in=False, message="CSRF_FAILED", csrf_token=token_server)
        if 'admin_key' in request.form:
            if hmac.compare_digest(request.form['admin_key'], ADMIN_PASS):
                session['admin_auth'] = True
                return redirect(ADMIN_URL)
            return render_template('admin.html', logged_in=False, message="ERR_AUTH_FAILED", csrf_token=token_server)
        if not session.get('admin_auth'): return render_template('admin.html', logged_in=False, csrf_token=token_server)

        action = request.form.get('action')
        target_id = request.form.get('target_id', '').replace(' ', '').strip()

        if action == 'add_days':
            tariff = request.form.get('tariff', 'base')
            try: days = int(request.form.get('days', 0))
            except: days = 0
            if not re.fullmatch(r'\d{10,16}', target_id): message = "ERR_INVALID_ID"
            elif tariff not in ('base', 'stealth'): message = "ERR_INVALID_TARIFF"
            elif days <= 0: message = "ERR_INVALID_DAYS"
            else:
                try:
                    with get_db() as conn:
                        cursor = conn.cursor()
                        cursor.execute("SELECT expiry_base, expiry_stealth FROM users WHERE account_id = %s", (target_id,))
                        user = cursor.fetchone()
                        if user:
                            now_ms = int(time.time() * 1000)
                            add_ms = days * 24 * 60 * 60 * 1000
                            if tariff == 'base':
                                cur_exp = user[0] if (user[0] and user[0] > now_ms) else now_ms
                                cursor.execute("UPDATE users SET expiry_base = %s WHERE account_id = %s", (cur_exp + add_ms, target_id))
                            else:
                                cur_exp = user[1] if (user[1] and user[1] > now_ms) else now_ms
                                cursor.execute("UPDATE users SET expiry_stealth = %s WHERE account_id = %s", (cur_exp + add_ms, target_id))
                            message = f"SUCCESS: {target_id} +{days}d {tariff}"
                        else: message = f"ERR_NOT_FOUND: {target_id}"
                except Exception as e: message = f"ERR_DB: {e}"

        elif action == 'delete_user':
            if not re.fullmatch(r'\d{10,16}', target_id): message = "ERR_INVALID_ID"
            else:
                try:
                    with get_db() as conn:
                        cursor = conn.cursor()
                        cursor.execute("SELECT expiry_base, expiry_stealth, base_slot, stealth_slot FROM users WHERE account_id = %s", (target_id,))
                        row = cursor.fetchone()
                        if row:
                            for tariff, slot, u_exp in [("base", row[2], row[0]), ("stealth", row[3], row[1])]:
                                inbound_list = INBOUNDS.get(tariff, [])
                                if u_exp > 0 and len(inbound_list) > 0:
                                    slot = slot % len(inbound_list)
                                    u_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{target_id}_{tariff}_{slot}"))
                                    delete_xui_client(inbound_list[slot]['id'], u_uuid)
                            cursor.execute("DELETE FROM users WHERE account_id = %s", (target_id,))
                            message = f"SUCCESS: {target_id} permanently DELETED."
                        else: message = f"ERR_NOT_FOUND: {target_id}"
                except Exception as e: message = f"ERR_DB: {e}"

    if not session.get('admin_auth'): return render_template('admin.html', logged_in=False, csrf_token=session.get('csrf_token'))

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            now_ms = int(time.time() * 1000)
            cursor.execute("SELECT COUNT(*) FROM users")
            total = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM users WHERE expiry_base > %s OR expiry_stealth > %s", (now_ms, now_ms))
            active = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM users WHERE expiry_base > %s", (now_ms,))
            base = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM users WHERE expiry_stealth > %s", (now_ms,))
            stealth = cursor.fetchone()[0]
    except: total = active = base = stealth = 0

    return render_template('admin.html', logged_in=True, message=message, sys=get_sys_stats(), stats={'total_users': total, 'active_users': active, 'base_users': base, 'stealth_users': stealth}, csrf_token=session.get('csrf_token'))

def expiration_watchdog():
    while True:
        try:
            now_ms = int(time.time() * 1000)
            threshold = now_ms - (24 * 3600 * 1000)
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT account_id, base_slot, stealth_slot, expiry_base, expiry_stealth FROM users WHERE (expiry_base < %s AND expiry_base > 0) OR (expiry_stealth < %s AND expiry_stealth > 0)", (threshold, threshold))
                expired_users = cursor.fetchall()
            expired_base_ids = []; expired_stealth_ids = []
            for user in expired_users:
                acc_id, b_slot, s_slot, exp_base, exp_stealth = user
                expired_base = (0 < exp_base < threshold)
                expired_stealth = (0 < exp_stealth < threshold)
                if expired_base:
                    expired_base_ids.append(acc_id)
                    if "base" in INBOUNDS and len(INBOUNDS["base"]) > 0:
                        slot = b_slot % len(INBOUNDS["base"])
                        u_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{acc_id}_base_{slot}"))
                        delete_xui_client(INBOUNDS["base"][slot]['id'], u_uuid)
                if expired_stealth:
                    expired_stealth_ids.append(acc_id)
                    if "stealth" in INBOUNDS and len(INBOUNDS["stealth"]) > 0:
                        slot = s_slot % len(INBOUNDS["stealth"])
                        u_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{acc_id}_stealth_{slot}"))
                        delete_xui_client(INBOUNDS["stealth"][slot]['id'], u_uuid)
            if expired_base_ids or expired_stealth_ids:
                with get_db() as conn:
                    cursor = conn.cursor()
                    if expired_base_ids: cursor.execute("UPDATE users SET expiry_base=0 WHERE account_id = ANY(%s)", (expired_base_ids,))
                    if expired_stealth_ids: cursor.execute("UPDATE users SET expiry_stealth=0 WHERE account_id = ANY(%s)", (expired_stealth_ids,))
        except Exception as e: app_logger.error(f"Watchdog error: {e}")
        finally: time.sleep(3600)

threading.Thread(target=expiration_watchdog, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
