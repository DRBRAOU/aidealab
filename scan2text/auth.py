"""Users, sessions and the activity log for Scan2Text (SQLite, standard library only).

- Only e-mail addresses on ALLOWED_DOMAIN (default braou.ac.in) can have accounts.
- Accounts are created by admins; there is no self sign-up.
- Passwords are hashed with scrypt; sessions are random tokens in an HttpOnly cookie.
"""
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time

ALLOWED_DOMAIN = os.environ.get("S2T_DOMAIN", "braou.ac.in").lower().lstrip("@")
SESSION_DAYS = 7
MAX_FAILS, LOCK_MINUTES = 5, 15

_lock = threading.Lock()
_db: sqlite3.Connection | None = None


def init(path):
    global _db
    _db = sqlite3.connect(path, check_same_thread=False)
    _db.row_factory = sqlite3.Row
    _db.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
        pw_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user',
        active INTEGER NOT NULL DEFAULT 1, must_change INTEGER NOT NULL DEFAULT 1,
        created REAL NOT NULL, created_by TEXT, last_login REAL);
    CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY, ts REAL NOT NULL, email TEXT, action TEXT NOT NULL,
        detail TEXT, ip TEXT);
    CREATE INDEX IF NOT EXISTS logs_ts ON logs(ts);
    """)


def _q(sql, args=(), one=False, write=False):
    with _lock:
        cur = _db.execute(sql, args)
        if write:
            _db.commit()
            return cur.lastrowid
        rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


# ---------------- passwords ----------------
def hash_pw(pw):
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_pw(pw, stored):
    try:
        _, salt, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


def temp_password():
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))


def password_problem(pw):
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if pw.isdigit() or pw.isalpha():
        return "Use a mix of letters and numbers."
    return None


# ---------------- users ----------------
def normalize_email(email):
    return (email or "").strip().lower()


def email_allowed(email):
    email = normalize_email(email)
    local, _, domain = email.partition("@")
    return bool(local) and domain == ALLOWED_DOMAIN and " " not in email


def user_count():
    return _q("SELECT COUNT(*) c FROM users", one=True)["c"]


def public_user(u):
    if not u:
        return None
    return {"id": u["id"], "email": u["email"], "name": u["name"], "role": u["role"],
            "active": bool(u["active"]), "must_change": bool(u["must_change"]),
            "created": u["created"], "created_by": u["created_by"], "last_login": u["last_login"]}


def get_user(uid):
    return _q("SELECT * FROM users WHERE id=?", (uid,), one=True)


def get_user_by_email(email):
    return _q("SELECT * FROM users WHERE email=?", (normalize_email(email),), one=True)


def list_users():
    return [public_user(u) for u in _q("SELECT * FROM users ORDER BY role DESC, name")]


def create_user(email, name, password, role="user", created_by=None, must_change=True):
    email = normalize_email(email)
    if not email_allowed(email):
        raise ValueError(f"Only @{ALLOWED_DOMAIN} e-mail addresses can be added.")
    if get_user_by_email(email):
        raise ValueError("A user with this e-mail already exists.")
    if role not in ("user", "admin"):
        raise ValueError("Unknown role.")
    uid = _q("INSERT INTO users(email,name,pw_hash,role,must_change,created,created_by) "
             "VALUES (?,?,?,?,?,?,?)",
             (email, name.strip() or email.split("@")[0], hash_pw(password), role,
              int(must_change), time.time(), created_by), write=True)
    return get_user(uid)


def update_user(uid, **fields):
    allowed = {"name", "role", "active", "must_change", "pw_hash", "last_login"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    cols = ", ".join(f"{k}=?" for k in sets)
    _q(f"UPDATE users SET {cols} WHERE id=?", (*sets.values(), uid), write=True)
    if "active" in sets and not sets["active"]:
        _q("DELETE FROM sessions WHERE user_id=?", (uid,), write=True)


def active_admins():
    return _q("SELECT COUNT(*) c FROM users WHERE role='admin' AND active=1", one=True)["c"]


# ---------------- login & sessions ----------------
def recent_failures(email):
    since = time.time() - LOCK_MINUTES * 60
    return _q("SELECT COUNT(*) c FROM logs WHERE action='login_failed' AND email=? AND ts>?",
              (normalize_email(email), since), one=True)["c"]


def authenticate(email, password):
    """Return (user, error_message)."""
    email = normalize_email(email)
    if recent_failures(email) >= MAX_FAILS:
        return None, f"Too many attempts. Try again in {LOCK_MINUTES} minutes."
    u = get_user_by_email(email)
    if not u or not check_pw(password, u["pw_hash"]):
        return None, "Wrong e-mail or password."
    if not u["active"]:
        return None, "This account is disabled. Contact an administrator."
    return u, None


def new_session(uid):
    token = secrets.token_urlsafe(32)
    _q("INSERT INTO sessions(token,user_id,expires) VALUES (?,?,?)",
       (token, uid, time.time() + SESSION_DAYS * 86400), write=True)
    update_user(uid, last_login=time.time())
    return token


def session_user(token):
    if not token:
        return None
    row = _q("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
             "WHERE s.token=? AND s.expires>? AND u.active=1", (token, time.time()), one=True)
    return row


def end_session(token):
    _q("DELETE FROM sessions WHERE token=?", (token,), write=True)


def end_all_sessions(uid, keep=None):
    _q("DELETE FROM sessions WHERE user_id=? AND token IS NOT ?", (uid, keep), write=True)


# ---------------- activity log ----------------
def log(action, email=None, detail="", ip=None):
    _q("INSERT INTO logs(ts,email,action,detail,ip) VALUES (?,?,?,?,?)",
       (time.time(), email, action, detail, ip), write=True)


def get_logs(email=None, action=None, limit=500, before=None):
    sql, args = "SELECT * FROM logs WHERE 1=1", []
    if email:
        sql += " AND email=?"; args.append(normalize_email(email))
    if action:
        sql += " AND action=?"; args.append(action)
    if before:
        sql += " AND id<?"; args.append(before)
    sql += " ORDER BY id DESC LIMIT ?"; args.append(max(1, min(int(limit), 5000)))
    return [dict(r) for r in _q(sql, args)]


def log_actions():
    return [r["action"] for r in _q("SELECT DISTINCT action FROM logs ORDER BY action")]
