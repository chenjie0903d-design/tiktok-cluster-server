from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from typing import Optional, Dict, List
import time
import uuid
import os
import base64
import secrets
import hmac
import hashlib
import re
from datetime import datetime
from fastapi.responses import HTMLResponse
from fastapi import Header

app = FastAPI(title="TikTok Cluster Control Server Web Admin V6.3")

devices: Dict[str, dict] = {}
commands: Dict[str, List[dict]] = {}
screenshots: Dict[str, dict] = {}
configs: Dict[str, dict] = {}
logs_store: Dict[str, dict] = {}
daily_seq_date = ""
daily_seq_map: Dict[str, int] = {}
daily_seq_next = 1
SCREENSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


# ==================== V5.0 轻量多用户 / PostgreSQL ====================
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
API_KEY_PEPPER = os.environ.get("API_KEY_PEPPER", os.environ.get("ADMIN_KEY", "TikTokClusterPepper2026"))
try:
    import psycopg2
    import psycopg2.extras
except Exception:
    psycopg2 = None


def multi_user_enabled() -> bool:
    return bool(DATABASE_URL and psycopg2 is not None)


def db_conn():
    if not multi_user_enabled():
        raise RuntimeError("DATABASE_URL/psycopg2 unavailable")
    return psycopg2.connect(DATABASE_URL)


def db_query(sql, params=None, one=False, commit=False):
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or [])
            rows = []
            if cur.description:
                rows = cur.fetchall()
            if commit:
                conn.commit()
            if one:
                return dict(rows[0]) if rows else None
            return [dict(r) for r in rows]


def api_key_hash(key: str) -> str:
    key = str(key or "").strip()
    return hashlib.sha256((key + "|" + API_KEY_PEPPER).encode("utf-8")).hexdigest()


def make_api_key() -> str:
    return "tk_live_" + secrets.token_urlsafe(32).replace("-", "").replace("_", "")[:40]


def make_password_hash(password: str) -> str:
    password = str(password or "")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="password must be at least 6 characters")
    salt = secrets.token_urlsafe(18)
    iterations = 120000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${base64.b64encode(digest).decode('ascii')}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algo, iterations, salt, digest_b64 = str(stored_hash or "").split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt.encode("utf-8"), int(iterations))
        expected = base64.b64decode(digest_b64.encode("ascii"))
        return hmac.compare_digest(digest, expected)
    except Exception:
        return False


def normalize_machine_code(machine_code: str) -> str:
    code = str(machine_code or "").strip()
    code = re.sub(r"[^A-Za-z0-9_\-]", "", code)
    return code[:128]


def init_db():
    if not multi_user_enabled():
        print("[multi-user] DATABASE_URL missing or psycopg2 unavailable; legacy memory mode")
        return False
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                status TEXT NOT NULL DEFAULT 'active',
                expires_at TIMESTAMPTZ NULL,
                max_devices INTEGER NOT NULL DEFAULT 3,
                bind_mode TEXT NOT NULL DEFAULT 'whitelist',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            ''')
            cur.execute('''
            CREATE TABLE IF NOT EXISTS api_keys (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                key_hash TEXT UNIQUE NOT NULL,
                key_prefix TEXT NOT NULL,
                key_plain TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                expires_at TIMESTAMPTZ NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_used_at TIMESTAMPTZ NULL
            );
            ''')
            cur.execute('''
            CREATE TABLE IF NOT EXISTS bound_devices (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                machine_code TEXT UNIQUE NOT NULL,
                device_name TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                bound_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen TIMESTAMPTZ NULL,
                client_version TEXT DEFAULT '',
                note TEXT DEFAULT '',
                bind_source TEXT NOT NULL DEFAULT 'manual_user'
            );
            ''')
            cur.execute("CREATE INDEX IF NOT EXISTS idx_bound_devices_user_id ON bound_devices(user_id);")
            cur.execute("ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS key_plain TEXT;")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT;")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_user_id ON api_keys(user_id);")
            conn.commit()
    print("[multi-user] PostgreSQL tables ready")
    return True

try:
    init_db()
except Exception as e:
    print("[multi-user] init_db failed:", repr(e))


def extract_bearer(request: Request, api_key: Optional[str] = None):
    if api_key:
        return str(api_key).strip()
    auth = request.headers.get("Authorization", "") if request else ""
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return ""


def admin_auth_ok(request: Request, key: Optional[str] = None):
    """
    V5.1：后台密码必须严格校验。
    如果 Railway 没有设置 ADMIN_KEY，后台不再放行，避免任意 key 都能进。
    """
    expected = os.environ.get("ADMIN_KEY", "").strip()
    provided = (key or request.headers.get("X-Admin-Key") or "").strip()

    if not expected:
        return False

    return hmac.compare_digest(provided, expected)


def get_user_by_api_key(raw_key: str):
    if not multi_user_enabled() or not raw_key:
        return None
    kh = api_key_hash(raw_key)
    row = db_query('''
        SELECT ak.id AS api_key_id, ak.user_id, ak.status AS key_status, ak.expires_at AS key_expires_at,
               u.username, u.role, u.status AS user_status, u.expires_at AS user_expires_at,
               u.max_devices, u.bind_mode
        FROM api_keys ak
        JOIN users u ON u.id = ak.user_id
        WHERE ak.key_hash=%s
    ''', [kh], one=True)
    if not row:
        return None
    db_query("UPDATE api_keys SET last_used_at=NOW() WHERE id=%s", [row["api_key_id"]], commit=True)
    return row


def check_user_active(row):
    if not row:
        raise HTTPException(status_code=401, detail="api_key invalid")
    if row.get("key_status") != "active":
        raise HTTPException(status_code=403, detail="api_key disabled")
    if row.get("user_status") != "active":
        raise HTTPException(status_code=403, detail="user disabled")
    if row.get("key_expires_at"):
        # PostgreSQL handles timezone-aware datetime; compare in SQL for reliability.
        expired = db_query("SELECT NOW() > %s AS expired", [row.get("key_expires_at")], one=True)
        if expired and expired.get("expired"):
            raise HTTPException(status_code=403, detail="api_key expired")
    if row.get("user_expires_at"):
        expired = db_query("SELECT NOW() > %s AS expired", [row.get("user_expires_at")], one=True)
        if expired and expired.get("expired"):
            raise HTTPException(status_code=403, detail="user expired")
    return row


def check_plain_user_active(row):
    if not row:
        raise HTTPException(status_code=401, detail="user invalid")
    if row.get("status") != "active":
        raise HTTPException(status_code=403, detail="user disabled")
    if row.get("expires_at"):
        expired = db_query("SELECT NOW() > %s AS expired", [row.get("expires_at")], one=True)
        if expired and expired.get("expired"):
            raise HTTPException(status_code=403, detail="user expired")
    return row


def get_or_create_login_api_key(user_id: int) -> str:
    row = db_query("""
        SELECT key_plain
        FROM api_keys
        WHERE user_id=%s
          AND status='active'
          AND key_plain IS NOT NULL
          AND (expires_at IS NULL OR expires_at > NOW())
        ORDER BY id DESC
        LIMIT 1
    """, [user_id], one=True)
    if row and row.get("key_plain"):
        db_query("UPDATE api_keys SET last_used_at=NOW() WHERE key_plain=%s", [row["key_plain"]], commit=True)
        return row["key_plain"]
    raw = make_api_key()
    db_query("""
        INSERT INTO api_keys(user_id,key_hash,key_prefix,key_plain,last_used_at)
        VALUES(%s,%s,%s,%s,NOW())
    """, [user_id, api_key_hash(raw), raw[:16], raw], commit=True)
    return raw


def get_auth_context(request: Request, key: Optional[str] = None, api_key: Optional[str] = None, allow_legacy=True):
    if admin_auth_ok(request, key):
        return {"is_admin": True, "user_id": None, "username": "ADMIN", "role": "admin"}
    raw_key = extract_bearer(request, api_key)
    if raw_key and multi_user_enabled():
        row = check_user_active(get_user_by_api_key(raw_key))
        return {"is_admin": False, "user_id": int(row["user_id"]), "username": row["username"], "role": row.get("role") or "user", "user": row}
    if allow_legacy and not multi_user_enabled():
        return {"is_admin": True, "user_id": None, "username": "LEGACY", "role": "admin"}
    raise HTTPException(status_code=401, detail="bad admin key or api_key")


def get_bound_device(machine_code: str):
    if not multi_user_enabled():
        return None
    return db_query('''
        SELECT bd.*, u.username
        FROM bound_devices bd
        JOIN users u ON u.id=bd.user_id
        WHERE bd.machine_code=%s
    ''', [machine_code], one=True)


def check_bound_device_client(machine_code: str):
    """
    V6.3：客户端不再需要 API Key。
    客户端接口只按 machine_code 授权：
    - machine_code 必须已在后台绑定到某个用户；
    - 用户必须 active 且未过期；
    - 设备必须 active。
    """
    code = normalize_machine_code(machine_code)
    if not code:
        raise HTTPException(status_code=400, detail="machine_code invalid")
    if not multi_user_enabled():
        return {"is_admin": True, "user_id": None, "username": "LEGACY", "role": "admin"}
    row = db_query("""
        SELECT bd.*, u.username, u.status AS user_status, u.expires_at AS user_expires_at, u.max_devices
        FROM bound_devices bd
        JOIN users u ON u.id=bd.user_id
        WHERE bd.machine_code=%s
    """, [code], one=True)
    if not row:
        raise HTTPException(status_code=403, detail="device not bound")
    if row.get("status") != "active":
        raise HTTPException(status_code=403, detail="device disabled")
    if row.get("user_status") != "active":
        raise HTTPException(status_code=403, detail="user disabled")
    if row.get("user_expires_at"):
        expired = db_query("SELECT NOW() > %s AS expired", [row.get("user_expires_at")], one=True)
        if expired and expired.get("expired"):
            raise HTTPException(status_code=403, detail="user expired")
    return {"is_admin": False, "user_id": int(row["user_id"]), "username": row.get("username") or "", "role": "user", "bound_device": row}



def verify_device_access(request: Request, machine_code: str, key: Optional[str] = None, api_key: Optional[str] = None):
    ctx = get_auth_context(request, key, api_key)
    if ctx["is_admin"] or not multi_user_enabled():
        return ctx
    code = normalize_machine_code(machine_code)
    bd = get_bound_device(code)
    if not bd:
        raise HTTPException(status_code=403, detail="device not bound")
    if int(bd["user_id"]) != int(ctx["user_id"]):
        raise HTTPException(status_code=403, detail="device bound to another user")
    if bd.get("status") != "active":
        raise HTTPException(status_code=403, detail="device disabled")
    return ctx


def count_user_devices(user_id: int):
    row = db_query("SELECT COUNT(*) AS c FROM bound_devices WHERE user_id=%s", [user_id], one=True)
    return int(row.get("c", 0) if row else 0)


def add_bound_device(user_id: int, machine_code: str, device_name: str = "", bind_source: str = "manual_user"):
    code = normalize_machine_code(machine_code)
    if len(code) < 4:
        raise HTTPException(status_code=400, detail="machine_code invalid")
    user = db_query("SELECT * FROM users WHERE id=%s", [user_id], one=True)
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    if user.get("status") != "active":
        raise HTTPException(status_code=403, detail="user disabled")
    existing = get_bound_device(code)
    if existing:
        if int(existing["user_id"]) == int(user_id):
            return existing
        raise HTTPException(status_code=409, detail="machine_code bound to another user")
    max_devices = int(user.get("max_devices") or 0)
    if max_devices > 0 and count_user_devices(user_id) >= max_devices:
        raise HTTPException(status_code=403, detail="device limit reached")
    return db_query('''
        INSERT INTO bound_devices(user_id, machine_code, device_name, bind_source)
        VALUES(%s,%s,%s,%s)
        RETURNING *
    ''', [user_id, code, str(device_name or "").strip(), bind_source], one=True, commit=True)


def cleanup_runtime_device(machine_code: str):
    devices.pop(machine_code, None)
    commands.pop(machine_code, None)
    screenshots.pop(machine_code, None)
    configs.pop(machine_code, None)
    logs_store.pop(machine_code, None)
    try:
        daily_seq_map.pop(machine_code, None)
    except Exception:
        pass
    try:
        safe_code = "".join(c for c in machine_code if c.isalnum() or c in ("-", "_"))[:80]
        for fn in os.listdir(SCREENSHOT_DIR):
            if fn.startswith(safe_code):
                try: os.remove(os.path.join(SCREENSHOT_DIR, fn))
                except Exception: pass
    except Exception:
        pass


class Heartbeat(BaseModel):
    machine_code: str
    device_name: str
    status: str = "online"
    running: bool = False
    work_time: Optional[str] = None
    public_ip: Optional[str] = None
    location: Optional[str] = None
    carrier: Optional[str] = None
    location_carrier: Optional[str] = None
    app_version: str = "v26"

class CommandIn(BaseModel):
    command: str
    value: Optional[str] = None

class ScreenshotIn(BaseModel):
    image_base64: str
    width: Optional[int] = None
    height: Optional[int] = None

class ConfigIn(BaseModel):
    config: dict

class LogIn(BaseModel):
    text: str

class UserCreateIn(BaseModel):
    username: str
    max_devices: int = 3
    expires_at: Optional[str] = None
    expires_days: Optional[int] = None
    password: Optional[str] = None

class UserUpdateIn(BaseModel):
    username: Optional[str] = None
    status: Optional[str] = None
    expires_at: Optional[str] = None
    max_devices: Optional[int] = None
    bind_mode: Optional[str] = None

class PasswordLoginIn(BaseModel):
    username: str
    password: str

class PasswordLoginChangeIn(BaseModel):
    username: str
    old_password: str
    new_password: str

class PasswordChangeIn(BaseModel):
    old_password: Optional[str] = None
    new_password: str

class PasswordResetIn(BaseModel):
    password: str

class BoundDeviceIn(BaseModel):
    machine_code: str
    device_name: Optional[str] = ""
    user_id: Optional[int] = None

class StatusIn(BaseModel):
    status: str



def extract_work_time_from_log_text(text: str):
    """
    Web V6.3：桌面端控制区改为两行按钮，底部参数同步改为单行显示（仅桌面端）。
    兼容类似：
    工作时间：00:12:31
    工作时长：12分钟
    运行时长：1小时2分钟
    """
    if not text:
        return None
    import re
    patterns = [
        r"(?:工作时间|工作时长|运行时长)[:：]\s*([0-9]{1,2}[:：][0-9]{1,2}(?:[:：][0-9]{1,2})?)",
        r"(?:工作时间|工作时长|运行时长)[:：]\s*([0-9]+小时[0-9]+分钟(?:[0-9]+秒)?)",
        r"(?:工作时间|工作时长|运行时长)[:：]\s*([0-9]+分钟(?:[0-9]+秒)?)",
        r"(?:工作时间|工作时长|运行时长)[:：]\s*([0-9]+秒)",
    ]
    for pat in patterns:
        ms = list(re.finditer(pat, text))
        if ms:
            return ms[-1].group(1).replace("：", ":")
    return None

def get_device_work_time(machine_code: str, item: dict):
    # 优先使用心跳直接上报字段；没有时从实时运行日志里抓最后一次工作时间
    direct = item.get("work_time")
    if direct not in (None, "", "-"):
        return direct
    try:
        logs = logs_store.get(machine_code)
        if isinstance(logs, dict):
            text = str(logs.get("text") or "")
        elif isinstance(logs, list):
            text = "\n".join(str(x) for x in logs[-80:])
        else:
            text = str(logs or "")
        parsed = extract_work_time_from_log_text(text)
        return parsed or "-"
    except Exception:
        return "-"


def get_today_key():
    return datetime.now().strftime("%Y-%m-%d")

def assign_daily_seq(machine_code: str) -> int:
    """
    V26：每天自动重新排当天序号。
    服务器不关也没事；日期变了后，下一次心跳会清空当天序号池，从1重新开始。
    """
    global daily_seq_date, daily_seq_map, daily_seq_next
    today = get_today_key()

    if daily_seq_date != today:
        daily_seq_date = today
        daily_seq_map = {}
        daily_seq_next = 1

    if machine_code not in daily_seq_map:
        daily_seq_map[machine_code] = daily_seq_next
        daily_seq_next += 1

    return daily_seq_map[machine_code]


@app.get("/")
def home():
    return {"ok": True, "msg": "TikTok cluster server web admin v6.3 multi-user is running", "admin": "/tiktok", "version":"v26-web-v6.3-multi-user"}

@app.post("/api/heartbeat")
def heartbeat(data: Heartbeat, request: Request):
    now = time.time()
    machine_code = normalize_machine_code(data.machine_code)
    if not machine_code:
        raise HTTPException(status_code=400, detail="machine_code invalid")

    user_id = None
    username = ""
    if multi_user_enabled():
        ctx = check_bound_device_client(machine_code)
        user_id = int(ctx["user_id"])
        username = ctx.get("username") or ""
        db_query("""
            UPDATE bound_devices SET last_seen=NOW(), device_name=COALESCE(NULLIF(%s,''), device_name), client_version=%s
            WHERE machine_code=%s
        """, [str(data.device_name or "").strip(), data.app_version, machine_code], commit=True)

    old = devices.get(machine_code, {})
    location_carrier = data.location_carrier
    if not location_carrier:
        location_carrier = f"{data.location or ''}{data.carrier or ''}".strip()
    daily_seq = assign_daily_seq(machine_code)
    devices[machine_code] = {
        **old,
        "user_id": user_id,
        "username": username,
        "daily_seq": daily_seq,
        "daily_seq_date": daily_seq_date,
        "machine_code": machine_code,
        "device_name": (old.get("device_name", "") if str(data.device_name or "").strip() in ("", "1", "未知设备", "未识别设备名") and old.get("device_name") else data.device_name),
        "status": data.status,
        "running": data.running,
        "work_time": data.work_time,
        "public_ip": data.public_ip or old.get("public_ip", ""),
        "location": data.location or old.get("location", ""),
        "carrier": data.carrier or old.get("carrier", ""),
        "location_carrier": location_carrier or old.get("location_carrier", ""),
        "app_version": data.app_version,
        "last_seen": now,
    }
    commands.setdefault(machine_code, [])
    return {"ok": True, "server_time": now, "user_id": user_id, "username": username}


def build_device_item(d: dict, now: float):
    item = dict(d)
    item["online"] = now - item.get("last_seen", 0) <= 120 if item.get("last_seen") else False
    item["last_seen_ago"] = int(now - item.get("last_seen", 0)) if item.get("last_seen") else 999999
    if not item["online"]:
        item["running"] = False
    if not item["online"]:
        item["display_state"] = "offline"
    elif item.get("status") == "switching_ip":
        item["display_state"] = "switching_ip"
    elif item.get("status") == "screenshotting":
        item["display_state"] = "online" if screenshots.get(item.get("machine_code")) else "screenshotting"
    elif item.get("status") == "updating_package":
        item["display_state"] = "updating_package"
    elif item.get("status") in ("starting_app", "restarting_app"):
        item["display_state"] = "online"
    else:
        item["display_state"] = "online"
    shot = screenshots.get(item.get("machine_code"))
    item["has_screenshot"] = bool(shot)
    item["screenshot_time"] = shot.get("created_at") if shot else None
    item["work_time"] = get_device_work_time(item.get("machine_code"), item)
    return item

@app.get("/api/devices")
def list_devices(request: Request, key: Optional[str] = None, api_key: Optional[str] = None):
    ctx = get_auth_context(request, key, api_key)
    now = time.time()
    result = []
    seen = set()
    for machine_code, d in devices.items():
        if multi_user_enabled() and not ctx["is_admin"] and int(d.get("user_id") or -1) != int(ctx["user_id"]):
            continue
        item = build_device_item(d, now)
        result.append(item)
        seen.add(machine_code)
    if multi_user_enabled():
        if ctx["is_admin"]:
            rows = db_query('''SELECT bd.*, u.username FROM bound_devices bd JOIN users u ON u.id=bd.user_id ORDER BY bd.bound_at DESC''')
        else:
            rows = db_query('''SELECT bd.*, u.username FROM bound_devices bd JOIN users u ON u.id=bd.user_id WHERE bd.user_id=%s ORDER BY bd.bound_at DESC''', [ctx["user_id"]])
        for bd in rows:
            code = bd["machine_code"]
            if code in seen:
                continue
            item = {
                "user_id": bd["user_id"], "username": bd.get("username", ""),
                "machine_code": code,
                "device_name": bd.get("device_name") or code[:8],
                "status": "disabled" if bd.get("status") == "disabled" else "offline",
                "running": False,
                "last_seen": 0,
                "app_version": bd.get("client_version") or "",
                "public_ip": "", "location":"", "carrier":"", "location_carrier":"",
                "daily_seq": 999999,
            }
            result.append(build_device_item(item, now))
    result.sort(key=lambda x: (not x.get("online", False), x.get("daily_seq", 999999), str(x.get("username") or ""), str(x.get("device_name") or "")))
    return {"ok": True, "devices": result, "is_admin": ctx["is_admin"], "username": ctx.get("username")}

@app.post("/api/devices/{machine_code}/command")
def send_command(machine_code: str, cmd: CommandIn, request: Request, key: Optional[str] = None, api_key: Optional[str] = None):
    machine_code = normalize_machine_code(machine_code)
    verify_device_access(request, machine_code, key, api_key)
    if machine_code not in devices:
        # 允许给已绑定但当前离线的设备排队？为了避免无效堆积，保持旧逻辑：必须已心跳出现。
        raise HTTPException(status_code=404, detail="device not found")
    item = {"id": str(uuid.uuid4()), "command": cmd.command, "value": cmd.value, "created_at": time.time()}
    commands.setdefault(machine_code, []).append(item)
    if str(cmd.command).strip() == "rename" and cmd.value:
        devices[machine_code]["device_name"] = str(cmd.value).strip()
        if multi_user_enabled():
            db_query("UPDATE bound_devices SET device_name=%s WHERE machine_code=%s", [str(cmd.value).strip(), machine_code], commit=True)
    return {"ok": True, "queued": item}

@app.post("/api/commands/all")
def send_all(cmd: CommandIn, request: Request, key: Optional[str] = None, api_key: Optional[str] = None):
    ctx = get_auth_context(request, key, api_key)
    count = 0
    now = time.time()
    for machine_code, d in devices.items():
        if multi_user_enabled() and not ctx["is_admin"] and int(d.get("user_id") or -1) != int(ctx["user_id"]):
            continue
        if now - d.get("last_seen", 0) > 120:
            continue
        item = {"id": str(uuid.uuid4()), "command": cmd.command, "value": cmd.value, "created_at": time.time()}
        commands.setdefault(machine_code, []).append(item)
        count += 1
    return {"ok": True, "count": count}

@app.get("/api/devices/{machine_code}/commands")
def pull_commands(machine_code: str, request: Request):
    machine_code = normalize_machine_code(machine_code)
    if multi_user_enabled():
        check_bound_device_client(machine_code)
    pending = commands.get(machine_code, [])
    commands[machine_code] = []
    return {"ok": True, "commands": pending}


@app.post("/api/devices/{machine_code}/screenshot")
def upload_screenshot(machine_code: str, shot: ScreenshotIn, request: Request):
    machine_code = normalize_machine_code(machine_code)
    if multi_user_enabled():
        ctx = check_bound_device_client(machine_code)
    else:
        ctx = {"user_id": None}
    if machine_code not in devices:
        devices[machine_code] = {"machine_code": machine_code, "device_name": machine_code[:8], "status": "online", "running": False, "last_seen": time.time(), "user_id": ctx.get("user_id")}
    safe_code = "".join(c for c in machine_code if c.isalnum() or c in ("-", "_"))[:80]
    if multi_user_enabled() and ctx.get("user_id"):
        user_dir = os.path.join(SCREENSHOT_DIR, f"user_{ctx['user_id']}")
        os.makedirs(user_dir, exist_ok=True)
        filepath = os.path.join(user_dir, f"{safe_code}.jpg")
    else:
        filepath = os.path.join(SCREENSHOT_DIR, f"{safe_code}.jpg")
    filename = os.path.basename(filepath)
    try:
        with open(filepath, "wb") as f:
            f.write(base64.b64decode(shot.image_base64))
    except Exception:
        filepath = ""
    screenshots[machine_code] = {"machine_code": machine_code, "image_base64": shot.image_base64, "width": shot.width, "height": shot.height, "created_at": time.time(), "filename": filename if filepath else "", "filepath": filepath}
    try:
        devices[machine_code]["status"] = "online"
        devices[machine_code]["last_seen"] = time.time()
    except Exception:
        pass
    return {"ok": True, "filename": filename if filepath else "", "filepath": filepath}

@app.get("/api/devices/{machine_code}/screenshot")
def get_screenshot(machine_code: str, request: Request, key: Optional[str] = None, api_key: Optional[str] = None):
    machine_code = normalize_machine_code(machine_code)
    verify_device_access(request, machine_code, key, api_key)
    shot = screenshots.get(machine_code)
    if not shot:
        raise HTTPException(status_code=404, detail="screenshot not found")
    return {"ok": True, "screenshot": shot}

@app.delete("/api/devices/{machine_code}")
def delete_device(machine_code: str, request: Request, key: Optional[str] = None, api_key: Optional[str] = None):
    machine_code = normalize_machine_code(machine_code)
    verify_device_access(request, machine_code, key, api_key)
    removed = machine_code in devices
    cleanup_runtime_device(machine_code)
    return {"ok": True, "removed": removed, "machine_code": machine_code}

@app.get("/api/version")
def version():
    return {"ok": True, "version": "v26-web-v6.3-multi-user", "features": ["multi_user", "postgresql", "api_key", "machine_code_whitelist", "heartbeat", "ip_location", "commands", "daily_sequence", "screenshot_last_only", "mobile_admin_v6_3", "admin_key_strict", "admin_page_auth_gate", "api_key_modal_persistent", "admin_full_user_device_manage", "expires_days_input", "user_expire_title", "create_user_days_modal_fix", "mobile_user_button", "layout_tune_v5_8", "header_spacing_fix_v5_9", "bind_select_clear_v6_0", "single_login_auto_role_v6_1", "tiktok_path_v6_2", "machine_code_client_auth_v6_3"]}

@app.get("/api/debug/devices")
def debug_devices(request: Request, key: Optional[str] = None):
    if not admin_auth_ok(request, key):
        raise HTTPException(status_code=401, detail="bad admin key")
    return {"ok": True, "devices": devices, "screenshots": list(screenshots.keys())}

@app.post("/api/devices/{machine_code}/config")
def set_device_config(machine_code: str, data: ConfigIn, request: Request, key: Optional[str] = None, api_key: Optional[str] = None):
    machine_code = normalize_machine_code(machine_code)
    verify_device_access(request, machine_code, key, api_key)
    if machine_code not in devices:
        raise HTTPException(status_code=404, detail="device not found")
    configs[machine_code] = {"config": data.config, "updated_at": time.time()}
    item = {"id": str(uuid.uuid4()), "command": "update_config", "value": data.config, "created_at": time.time()}
    commands.setdefault(machine_code, []).append(item)
    return {"ok": True, "config": data.config}

@app.post("/api/config/all")
def set_all_config(data: ConfigIn, request: Request, key: Optional[str] = None, api_key: Optional[str] = None):
    ctx = get_auth_context(request, key, api_key)
    count = 0
    now = time.time()
    for machine_code, d in devices.items():
        if multi_user_enabled() and not ctx["is_admin"] and int(d.get("user_id") or -1) != int(ctx["user_id"]):
            continue
        if now - d.get("last_seen", 0) > 120:
            continue
        configs[machine_code] = {"config": data.config, "updated_at": time.time()}
        item = {"id": str(uuid.uuid4()), "command": "update_config", "value": data.config, "created_at": time.time()}
        commands.setdefault(machine_code, []).append(item)
        count += 1
    return {"ok": True, "count": count}

@app.post("/api/devices/{machine_code}/log")
def upload_log(machine_code: str, data: LogIn, request: Request):
    machine_code = normalize_machine_code(machine_code)
    if multi_user_enabled():
        check_bound_device_client(machine_code)
    logs_store[machine_code] = {"machine_code": machine_code, "text": data.text or "", "created_at": time.time()}
    return {"ok": True}


@app.get("/api/devices/{machine_code}/log")
def get_log(machine_code: str, request: Request, key: Optional[str] = None, api_key: Optional[str] = None):
    machine_code = normalize_machine_code(machine_code)
    verify_device_access(request, machine_code, key, api_key)
    log = logs_store.get(machine_code)
    if not log:
        raise HTTPException(status_code=404, detail="log not found")
    return {"ok": True, "log": log}

# ==================== V5.0 用户/密钥/绑定设备接口 ====================

@app.get("/admin/api/users/{user_id}/api-keys")
def admin_list_user_api_keys(user_id: int, request: Request, key: Optional[str] = None):
    if not admin_auth_ok(request, key):
        raise HTTPException(status_code=401, detail="bad admin key")
    if not multi_user_enabled():
        raise HTTPException(status_code=400, detail="database disabled")
    rows = db_query("SELECT id,user_id,key_prefix,key_plain,status,created_at,last_used_at,expires_at FROM api_keys WHERE user_id=%s ORDER BY id DESC", [user_id])
    return {"ok": True, "api_keys": rows}

@app.delete("/admin/api/users/{user_id}")
def admin_delete_user(user_id: int, request: Request, key: Optional[str] = None):
    if not admin_auth_ok(request, key):
        raise HTTPException(status_code=401, detail="bad admin key")
    if not multi_user_enabled():
        raise HTTPException(status_code=400, detail="database disabled")
    codes = db_query("SELECT machine_code FROM bound_devices WHERE user_id=%s", [user_id])
    db_query("DELETE FROM users WHERE id=%s", [user_id], commit=True)
    for r in codes:
        code = r.get("machine_code")
        devices.pop(code, None); commands.pop(code, None); screenshots.pop(code, None); configs.pop(code, None); logs_store.pop(code, None)
    return {"ok": True}

@app.post("/admin/api/users/{user_id}/bound-devices")
def admin_add_user_bound_device(user_id: int, data: BoundDeviceIn, request: Request, key: Optional[str] = None):
    if not admin_auth_ok(request, key):
        raise HTTPException(status_code=401, detail="bad admin key")
    if not multi_user_enabled():
        raise HTTPException(status_code=400, detail="database disabled")
    code = normalize_machine_code(data.machine_code)
    if len(code) < 4:
        raise HTTPException(status_code=400, detail="machine_code invalid")
    old = get_bound_device(code)
    if old and int(old["user_id"]) != int(user_id):
        raise HTTPException(status_code=409, detail="machine_code bound to another user")
    if old:
        row = db_query("""
            UPDATE bound_devices
            SET device_name=COALESCE(NULLIF(%s,''), device_name),
                status='active',
                bind_source='manual_admin'
            WHERE machine_code=%s
            RETURNING *
        """, [data.device_name or code[:8], code], one=True, commit=True)
    else:
        row = add_bound_device(user_id, code, data.device_name or code[:8], "manual_admin")
    return {"ok": True, "device": row}

@app.get("/admin/api/bound-devices")
def admin_list_all_bound_devices(request: Request, key: Optional[str] = None, user_id: Optional[int] = None):
    if not admin_auth_ok(request, key):
        raise HTTPException(status_code=401, detail="bad admin key")
    if not multi_user_enabled():
        raise HTTPException(status_code=400, detail="database disabled")
    params = []
    where = ""
    if user_id:
        where = "WHERE bd.user_id=%s"
        params.append(user_id)
    rows = db_query(f"""
        SELECT bd.*, u.username
        FROM bound_devices bd
        JOIN users u ON u.id=bd.user_id
        {where}
        ORDER BY bd.id DESC
    """, params)
    return {"ok": True, "devices": rows}

@app.delete("/admin/api/bound-devices/{machine_code}")
def admin_delete_any_bound_device(machine_code: str, request: Request, key: Optional[str] = None):
    if not admin_auth_ok(request, key):
        raise HTTPException(status_code=401, detail="bad admin key")
    code = normalize_machine_code(machine_code)
    if not multi_user_enabled():
        raise HTTPException(status_code=400, detail="database disabled")
    db_query("DELETE FROM bound_devices WHERE machine_code=%s", [code], commit=True)
    cleanup_runtime_device(code)
    return {"ok": True, "removed": True}


@app.get("/api/me")
def api_me(request: Request, key: Optional[str] = None, api_key: Optional[str] = None):
    ctx = get_auth_context(request, key, api_key)
    user = None
    if not ctx["is_admin"] and ctx.get("user_id") and multi_user_enabled():
        user = db_query("SELECT id, username, status, expires_at, max_devices, bind_mode FROM users WHERE id=%s", [ctx.get("user_id")], one=True)
    return {
        "ok": True,
        "is_admin": ctx["is_admin"],
        "user_id": ctx.get("user_id"),
        "username": ctx.get("username"),
        "user": user or {"username": ctx.get("username")}
    }

@app.get("/api/bound-devices")
def api_bound_devices(request: Request, key: Optional[str] = None, api_key: Optional[str] = None):
    ctx = get_auth_context(request, key, api_key)
    if not multi_user_enabled():
        return {"ok": True, "devices": []}
    if ctx["is_admin"]:
        rows = db_query('''SELECT bd.*, u.username FROM bound_devices bd JOIN users u ON u.id=bd.user_id ORDER BY bd.bound_at DESC''')
    else:
        rows = db_query('''SELECT bd.*, u.username FROM bound_devices bd JOIN users u ON u.id=bd.user_id WHERE bd.user_id=%s ORDER BY bd.bound_at DESC''', [ctx["user_id"]])
    for r in rows:
        rt = devices.get(r["machine_code"], {})
        r["online"] = bool(rt and time.time() - rt.get("last_seen", 0) <= 120)
        r["running"] = bool(rt.get("running")) if rt else False
    return {"ok": True, "devices": rows, "is_admin": ctx["is_admin"]}

@app.post("/api/bound-devices")
def api_add_bound_device(data: BoundDeviceIn, request: Request, key: Optional[str] = None, api_key: Optional[str] = None):
    ctx = get_auth_context(request, key, api_key)
    if not multi_user_enabled():
        raise HTTPException(status_code=400, detail="database disabled")
    user_id = int(data.user_id) if ctx["is_admin"] and data.user_id else int(ctx["user_id"])
    row = add_bound_device(user_id, data.machine_code, data.device_name or "", "manual_admin" if ctx["is_admin"] else "manual_user")
    return {"ok": True, "device": row}

@app.delete("/api/bound-devices/{machine_code}")
def api_delete_bound_device(machine_code: str, request: Request, key: Optional[str] = None, api_key: Optional[str] = None):
    ctx = get_auth_context(request, key, api_key)
    code = normalize_machine_code(machine_code)
    bd = get_bound_device(code)
    if not bd:
        return {"ok": True, "removed": False}
    if not ctx["is_admin"] and int(bd["user_id"]) != int(ctx["user_id"]):
        raise HTTPException(status_code=403, detail="forbidden")
    db_query("DELETE FROM bound_devices WHERE machine_code=%s", [code], commit=True)
    cleanup_runtime_device(code)
    return {"ok": True, "removed": True}

@app.get("/admin/api/users")
def admin_list_users(request: Request, key: Optional[str] = None):
    if not admin_auth_ok(request, key):
        raise HTTPException(status_code=401, detail="bad admin key")
    if not multi_user_enabled():
        return {"ok": True, "users": []}
    rows = db_query('''
        SELECT u.id, u.username, u.role, u.status, u.expires_at, u.max_devices, u.bind_mode,
               u.created_at, u.updated_at,
               COALESCE(d.c,0) AS device_count, COALESCE(k.c,0) AS key_count
        FROM users u
        LEFT JOIN (SELECT user_id, COUNT(*) c FROM bound_devices GROUP BY user_id) d ON d.user_id=u.id
        LEFT JOIN (SELECT user_id, COUNT(*) c FROM api_keys GROUP BY user_id) k ON k.user_id=u.id
        ORDER BY u.id DESC
    ''')
    return {"ok": True, "users": rows}

@app.post("/admin/api/users")
def admin_create_user(data: UserCreateIn, request: Request, key: Optional[str] = None):
    if not admin_auth_ok(request, key):
        raise HTTPException(status_code=401, detail="bad admin key")
    if not multi_user_enabled():
        raise HTTPException(status_code=400, detail="database disabled")
    username = str(data.username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username required")
    password_hash = make_password_hash(data.password) if str(data.password or "").strip() else None

    # V5.4：优先按“天数”创建到期时间；兼容旧 expires_at 字符串。
    if data.expires_days is not None:
        try:
            days = int(data.expires_days)
        except Exception:
            days = 0
        if days > 0:
            row = db_query("""
                INSERT INTO users(username, max_devices, expires_at, bind_mode, password_hash)
                VALUES(%s,%s,NOW() + (%s || ' days')::interval,'whitelist',%s) RETURNING *
            """, [username, int(data.max_devices or 3), days, password_hash], one=True, commit=True)
            return {"ok": True, "user": row}

    row = db_query("""
        INSERT INTO users(username, max_devices, expires_at, bind_mode, password_hash)
        VALUES(%s,%s,NULLIF(%s,'')::timestamptz,'whitelist',%s) RETURNING *
    """, [username, int(data.max_devices or 3), data.expires_at or "", password_hash], one=True, commit=True)
    return {"ok": True, "user": row}

@app.patch("/admin/api/users/{user_id}")
def admin_update_user(user_id: int, data: UserUpdateIn, request: Request, key: Optional[str] = None):
    if not admin_auth_ok(request, key):
        raise HTTPException(status_code=401, detail="bad admin key")
    fields=[]; params=[]
    for col in ("username", "status", "bind_mode"):
        val = getattr(data, col)
        if val is not None:
            fields.append(f"{col}=%s"); params.append(val)
    if data.max_devices is not None:
        fields.append("max_devices=%s"); params.append(int(data.max_devices))
    if data.expires_at is not None:
        fields.append("expires_at=NULLIF(%s,'')::timestamptz"); params.append(data.expires_at)
    if not fields:
        return {"ok": True}
    fields.append("updated_at=NOW()")
    params.append(user_id)
    row = db_query(f"UPDATE users SET {', '.join(fields)} WHERE id=%s RETURNING *", params, one=True, commit=True)
    return {"ok": True, "user": row}

@app.post("/admin/api/users/{user_id}/api-key")
def admin_generate_api_key(user_id: int, request: Request, key: Optional[str] = None):
    if not admin_auth_ok(request, key):
        raise HTTPException(status_code=401, detail="bad admin key")
    if not multi_user_enabled():
        raise HTTPException(status_code=400, detail="database disabled")
    user = db_query("SELECT * FROM users WHERE id=%s", [user_id], one=True)
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    raw = make_api_key()
    prefix = raw[:16]
    row = db_query('''INSERT INTO api_keys(user_id,key_hash,key_prefix,key_plain) VALUES(%s,%s,%s,%s) RETURNING id,user_id,key_prefix,key_plain,status,created_at''', [user_id, api_key_hash(raw), prefix, raw], one=True, commit=True)
    return {"ok": True, "api_key": raw, "record": row}

@app.post("/api/login-password")
def login_password(data: PasswordLoginIn):
    if not multi_user_enabled():
        raise HTTPException(status_code=400, detail="database disabled")
    username = str(data.username or "").strip()
    password = str(data.password or "")
    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password required")
    user = db_query("SELECT * FROM users WHERE username=%s", [username], one=True)
    check_plain_user_active(user)
    if not user.get("password_hash") or not verify_password(password, user.get("password_hash")):
        raise HTTPException(status_code=401, detail="username or password incorrect")
    raw = get_or_create_login_api_key(int(user["id"]))
    return {"ok": True, "role": "user", "api_key": raw, "url": f"/tiktok?api_key={raw}&v=63"}

@app.post("/api/change-password-login")
def change_password_from_login(data: PasswordLoginChangeIn):
    if not multi_user_enabled():
        raise HTTPException(status_code=400, detail="database disabled")
    username = str(data.username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username required")
    user = db_query("SELECT * FROM users WHERE username=%s", [username], one=True)
    check_plain_user_active(user)
    if not user.get("password_hash") or not verify_password(data.old_password or "", user.get("password_hash")):
        raise HTTPException(status_code=401, detail="old password incorrect")
    password_hash = make_password_hash(data.new_password)
    db_query("UPDATE users SET password_hash=%s, updated_at=NOW() WHERE id=%s", [password_hash, user["id"]], commit=True)
    return {"ok": True}

@app.post("/admin/api/users/{user_id}/password")
def admin_reset_user_password(user_id: int, data: PasswordResetIn, request: Request, key: Optional[str] = None):
    if not admin_auth_ok(request, key):
        raise HTTPException(status_code=401, detail="bad admin key")
    if not multi_user_enabled():
        raise HTTPException(status_code=400, detail="database disabled")
    password_hash = make_password_hash(data.password)
    row = db_query("UPDATE users SET password_hash=%s, updated_at=NOW() WHERE id=%s RETURNING id,username", [password_hash, user_id], one=True, commit=True)
    if not row:
        raise HTTPException(status_code=404, detail="user not found")
    return {"ok": True, "user": row}

@app.post("/api/me/password")
def change_my_password(data: PasswordChangeIn, request: Request, key: Optional[str] = None, api_key: Optional[str] = None):
    ctx = get_auth_context(request, key, api_key)
    if ctx["is_admin"]:
        raise HTTPException(status_code=400, detail="admin password is ADMIN_KEY env")
    if not multi_user_enabled():
        raise HTTPException(status_code=400, detail="database disabled")
    user = db_query("SELECT * FROM users WHERE id=%s", [ctx["user_id"]], one=True)
    check_plain_user_active(user)
    if user.get("password_hash") and not verify_password(data.old_password or "", user.get("password_hash")):
        raise HTTPException(status_code=401, detail="old password incorrect")
    password_hash = make_password_hash(data.new_password)
    db_query("UPDATE users SET password_hash=%s, updated_at=NOW() WHERE id=%s", [password_hash, ctx["user_id"]], commit=True)
    return {"ok": True}

MOBILE_ADMIN_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>TikTok 集群控制台 Web V6.3</title>
<style>
:root{
  --blue:#1d9bf0;--green:#1db954;--red:#ff2d2f;--orange:#ff9f1a;--dark:#465465;
  --bg:#f4f6fa;--card:#fff;--text:#111827;--muted:#667085;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",Arial,sans-serif;font-size:15px;padding-bottom:230px}
.header{position:sticky;top:0;z-index:10;background:#fff;padding:10px 12px 8px;border-bottom:1px solid #e5e7eb}
.title-row{display:flex;align-items:center;gap:8px}
h1{font-size:22px;margin:0;font-weight:900}
.ver{font-size:12px;background:#111827;color:#fff;border-radius:999px;padding:2px 7px}
.refresh-btn{margin-left:auto;border:0;border-radius:12px;background:#eef2f7;padding:10px 14px;font-weight:800;color:#111827}
.server{margin-top:10px;width:100%;border:1px solid #d0d5dd;border-radius:13px;padding:11px 12px;font-size:14px;background:#fff}
.stats{margin-top:9px;color:var(--muted);font-size:16px;font-weight:800}
.wrap{padding:12px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.all-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.multi-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.multi-grid .wide{grid-column:span 2}
body:not(.is-admin-mode) .admin-only{display:none!important}
.desktop-top-grid,.desktop-action-grid{display:none}

.btn{border:0;border-radius:13px;color:#fff;font-size:15px;font-weight:850;padding:12px 8px;min-height:44px}
.btn.blue{background:var(--blue)}.btn.green{background:var(--green)}.btn.red{background:var(--red)}.btn.orange{background:var(--orange)}.btn.dark{background:var(--dark)}
.btn.gray{background:#e5e7eb;color:#111827}
.multi{margin-top:10px;padding-top:10px;border-top:1px solid #e5e7eb}
.card{position:relative;background:var(--card);border-radius:15px;margin:12px 0;padding:14px 12px;border:3px solid var(--blue);border-top-width:5px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.card.offline{border-color:var(--red);opacity:.72}.card.bad{background:#fff0f0;border-color:var(--red)}
.card.busy{border-color:var(--orange)}.card.running{border-color:var(--green)}
.seq{position:absolute;right:12px;top:12px;background:#111827;color:#fff;border-radius:999px;width:34px;height:34px;display:flex;align-items:center;justify-content:center;font-weight:900}
.dev-head{display:flex;align-items:center;gap:9px;padding-right:88px}
.select-box{width:24px;height:24px;accent-color:#1d9bf0}
.name{font-size:20px;font-weight:900;line-height:1.25;flex:0 1 12em;max-width:12em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ago-top{margin-left:auto;margin-right:4px;font-size:16px;font-weight:900;color:#111827;white-space:nowrap}
.card.offline .ago-top{color:#d92d20}
.bad-text{color:#d92d20;font-weight:900}
.line{margin-top:6px;color:#344054;word-break:break-all}
.badge{display:inline-block;border-radius:999px;padding:4px 8px;margin-right:6px;font-size:13px;font-weight:850;background:#e8f3ff;color:#0270c9}
.badge.red{background:#ffe4e2;color:#d92d20}.badge.green{background:#dcfae6;color:#079455}.badge.orange{background:#fff4e5;color:#b54708}
.actions{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:12px}
.actions .btn{font-size:15px;padding:10px 8px;min-height:42px}

.info-shot-wrap{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:start;margin-top:6px}
.info-shot-main{min-width:0}
.top-shot{min-width:96px;text-align:center;color:#667085;font-size:12px}
.top-shot .thumb{width:120px;max-height:74px;border:1px solid #d0d5dd;border-radius:8px;object-fit:contain;background:#f8fafc}
.top-shot-empty{font-size:12px;color:#98a2b3;padding-top:4px}
@media (max-width:520px){
  .info-shot-wrap{grid-template-columns:1fr}
  .top-shot{text-align:left;margin-top:8px}
  .top-shot .thumb{width:150px;max-height:90px}
}


.action-shot-wrap{display:grid;grid-template-columns:minmax(0,1fr) 104px;gap:8px;align-items:stretch;margin-top:10px}
.action-shot-wrap .actions{margin-top:0}
.action-side-shot{display:flex;align-items:center;justify-content:center;text-align:center;color:#667085;font-size:11px;min-height:78px}
.action-side-shot .thumb{width:100px;max-height:76px;border:1px solid #d0d5dd;border-radius:8px;object-fit:contain;background:#f8fafc}
.action-side-empty{border:1px dashed #d0d5dd;border-radius:8px;color:#98a2b3;padding:10px 4px}
@media (max-width:520px){
  .card{padding:12px 10px}
  .actions{grid-template-columns:repeat(4,1fr);gap:6px}
  .actions .btn{font-size:13px;padding:8px 4px;min-height:36px;border-radius:10px}
  .action-shot-wrap{grid-template-columns:minmax(0,1fr) 92px;gap:6px}
  .action-side-shot .thumb{width:88px;max-height:70px}
}
@media (max-width:390px){
  .actions .btn{font-size:12px;padding:7px 3px;min-height:34px}
  .action-shot-wrap{grid-template-columns:minmax(0,1fr) 84px}
  .action-side-shot .thumb{width:82px;max-height:64px}
}

.thumb-row{margin-top:12px;display:flex;align-items:center;gap:10px}
.thumb{width:120px;max-height:80px;border:1px solid #d0d5dd;border-radius:8px;object-fit:contain;background:#f8fafc}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.82);display:none;align-items:center;justify-content:center;z-index:99;padding:16px}
.modal.show{display:flex}
.modal img{max-width:100%;max-height:88vh;border-radius:10px;background:#fff}
.close{position:fixed;right:16px;top:16px;background:#fff;border:0;border-radius:999px;font-size:18px;font-weight:900;padding:8px 12px}
.footer{color:#667085;text-align:center;padding:30px 10px 45px}

.syncbar{position:fixed;left:0;right:0;bottom:0;z-index:30;background:#fff;border-top:1px solid #d0d5dd;box-shadow:0 -2px 10px rgba(15,23,42,.12);padding:8px 10px calc(8px + env(safe-area-inset-bottom))}
.sync-title{font-size:15px;font-weight:900;color:#111827;margin-bottom:6px}
.sync-row{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;align-items:center;margin-top:6px}
.sync-row label{font-size:14px;color:#111827;font-weight:700;display:flex;align-items:center;gap:4px;min-width:0}
.sync-row input[type="number"]{width:58px;min-width:48px;border:1px solid #d0d5dd;border-radius:8px;padding:7px 5px;font-size:14px}
.sync-row input[type="checkbox"],.sync-row input[type="radio"]{width:16px;height:16px;accent-color:#1d9bf0}
.sync-btn{border:0;border-radius:9px;padding:9px 6px;font-size:14px;font-weight:850;color:#fff;background:#465465;min-width:0}
.sync-btn.primary{background:#1d9bf0}
.sync-btn.green{background:#1db954}
@media (min-width:900px){
  body{padding-bottom:92px}
  .footer{display:none}
  .mobile-only{display:none !important}
  .desktop-top-grid,.desktop-action-grid{display:grid;gap:10px;margin-bottom:10px}
  .desktop-top-grid{grid-template-columns:repeat(8,minmax(0,1fr))}
  .desktop-action-grid{grid-template-columns:repeat(8,minmax(0,1fr))}
  .desktop-top-grid .btn,.desktop-action-grid .btn{padding:11px 6px;font-size:14px;min-height:42px;white-space:nowrap}

  .syncbar{
    display:grid;
    grid-template-columns:max-content repeat(12,minmax(0,1fr));
    align-items:center;
    gap:7px;
    padding:8px 8px;
    overflow:visible;
  }
  .sync-title{margin:0;white-space:nowrap;font-size:14px}
  .sync-row{display:contents}
  .sync-row label{
    font-size:13px;
    white-space:nowrap;
    display:flex;
    align-items:center;
    justify-content:center;
    gap:3px;
    min-width:0;
  }
  .sync-row input[type="number"]{width:52px;min-width:48px;padding:6px 4px;font-size:13px}
  .sync-row input[type="checkbox"],.sync-row input[type="radio"]{width:14px;height:14px}
  .sync-btn{
    font-size:13px;
    padding:8px 4px;
    min-height:34px;
    width:100%;
    white-space:nowrap;
  }
  .sync-network,.sync-blue,.sync-restartthen,.sync-restartdelay,.sync-save-all,.sync-select-online,
  .sync-cutip,.sync-ocr,.sync-norestart,.sync-startclicks,.sync-save-selected,.sync-clear{
    display:flex !important;
  }
  .sync-save-selected,.sync-save-all,.sync-select-online,.sync-clear{
    display:inline-flex !important;
    align-items:center;
    justify-content:center;
  }
}

/* V3.6：手机端顶部两组控制按钮紧凑化 */
@media (max-width:899px){
  .wrap{padding:8px}
  .all-grid,.multi-grid{gap:6px}
  .multi{margin-top:6px;padding-top:6px}

  /* 第一排批量控制按钮：保持紧凑 */
  .all-grid .btn{
    min-height:24px;
    padding:5px 4px;
    border-radius:9px;
    font-size:13px;
    line-height:1.1;
  }

  /* 第二排单选控制按钮：比 V3.6 增高 0.5 倍 */
  .multi-grid .btn{
    min-height:36px;
    padding:8px 5px;
    border-radius:10px;
    font-size:13px;
    line-height:1.15;
  }
}

/* V3.6：底部集群参数同步可折叠 */
.syncbar{
  transition:transform .22s ease;
}
.syncbar.collapsed{
  transform:translateY(calc(100% - 10px));
}
.sync-toggle{
  position:absolute;
  left:50%;
  top:-30px;
  transform:translateX(-50%);
  width:54px;
  height:30px;
  border:0;
  border-radius:16px 16px 0 0;
  background:#111827;
  color:#fff;
  font-size:18px;
  font-weight:900;
  line-height:1;
  box-shadow:0 -2px 8px rgba(15,23,42,.18);
}
body.sync-collapsed{padding-bottom:42px}
@media (min-width:900px){
  body.sync-collapsed{padding-bottom:42px}
}

/* V3.8：手机端顶部控制区默认隐藏，可展开/隐藏 */
@media (max-width:899px){
    .mobile-control-section.collapsed{
    display:none !important;
  }
  #mobileSelectedControls .wide{
    grid-column:span 2;
  }
}

/* V3.9：同步参数确认弹窗居中 */
.dialog-modal{
  position:fixed;
  inset:0;
  display:none;
  align-items:center;
  justify-content:center;
  background:rgba(15,23,42,.35);
  z-index:120;
  padding:18px;
}
.dialog-modal.show{display:flex}
.dialog-box{
  width:min(420px,92vw);
  background:#fff;
  border-radius:16px;
  box-shadow:0 12px 36px rgba(15,23,42,.25);
  padding:18px;
  text-align:center;
}
.dialog-text{
  font-size:17px;
  font-weight:800;
  color:#111827;
  line-height:1.45;
  margin:6px 0 16px;
}
.dialog-actions{
  display:grid;
  grid-template-columns:1fr 1fr;
  gap:10px;
}
.dialog-actions.single{grid-template-columns:1fr}
.dialog-actions button{
  border:0;
  border-radius:12px;
  padding:11px 12px;
  font-size:15px;
  font-weight:900;
}
.dialog-cancel{background:#e5e7eb;color:#111827}
.dialog-ok{background:#1d9bf0;color:#fff}
.two-line{display:inline-block;line-height:1.12}
@media (max-width:899px){
  .device-restart-start .two-line{line-height:1.05}
}

/* V4.2：远程软件更新包，两行四列等分 */
.package-section{
  grid-column:1 / -1;
  border-top:1px solid #e5e7eb;
  margin-top:8px;
  padding-top:8px;
  display:grid;
  grid-template-columns:repeat(4,minmax(0,1fr));
  gap:7px;
  align-items:center;
}
.package-section label,
.package-section .pkg-checks{
  font-size:13px;
  font-weight:800;
  color:#111827;
}
.package-section .pkg-field{
  display:grid;
  grid-template-columns:auto minmax(0,1fr);
  align-items:center;
  gap:6px;
  white-space:nowrap;
  min-width:0;
}
.package-section input[type="text"]{
  width:100%;
  min-width:0;
  border:1px solid #d0d5dd;
  border-radius:8px;
  padding:7px 6px;
  font-size:13px;
}
.package-section input[type="checkbox"]{
  width:15px;
  height:15px;
  accent-color:#1d9bf0;
}
.package-section .pkg-checks{
  display:grid;
  grid-template-columns:1fr 1fr;
  gap:8px;
  align-items:center;
  min-width:0;
}
.package-section .pkg-checks label{
  display:flex;
  align-items:center;
  gap:5px;
  white-space:nowrap;
}
.pkg-btn{
  border:0;
  border-radius:9px;
  padding:9px 6px;
  font-size:13px;
  font-weight:900;
  color:#fff;
  background:#465465;
  white-space:nowrap;
  width:100%;
  min-height:38px;
}
.pkg-btn.primary{background:#1d9bf0}
.pkg-btn.green{background:#1db954}
@media (max-width:899px){
  .package-section{
    grid-template-columns:1fr;
    gap:7px;
  }
  .package-section .pkg-field{
    grid-template-columns:78px minmax(0,1fr);
    gap:7px;
  }
  .package-section label,
  .package-section .pkg-checks{
    font-size:13px;
  }
  .package-section input[type="text"]{
    font-size:13px;
    padding:8px 6px;
  }
  .package-section .pkg-checks{
    grid-template-columns:1fr 1fr;
  }
  .pkg-btn{
    font-size:13px;
    min-height:36px;
  }
}


/* V4.2：手机端底部同步按钮等高，两行文字 */
.sync-btn .two-line{
  display:inline-block;
  line-height:1.08;
}
@media (max-width:899px){
  .sync-row .sync-btn{
    min-height:44px;
    height:44px;
    display:flex;
    align-items:center;
    justify-content:center;
    line-height:1.08;
  }
  .sync-row .sync-select-online,
  .sync-row .sync-clear{
    min-height:44px;
    height:44px;
  }
}
@media (min-width:900px){
  .sync-row .sync-btn{
    min-height:36px;
    height:36px;
    display:flex !important;
    align-items:center;
    justify-content:center;
    line-height:1.08;
  }
}


/* V4.3：顶部展开/隐藏移到刷新左侧，去掉版本徽标 */
.ver{display:none !important}
.mobile-header-toggle{
  margin-left:auto;
  border:0;
  border-radius:12px;
  background:#eef2f7;
  padding:10px 12px;
  font-size:22px;
  font-weight:900;
  color:#111827;
  line-height:1;
}
.mobile-header-toggle + .refresh-btn{
  margin-left:6px;
}
.refresh-btn{
  font-size:22px;
  font-weight:900;
  line-height:1;
}
@media (min-width:900px){
  .refresh-btn{
    font-size:22px;
    font-weight:900;
  }
}
@media (max-width:899px){
  .title-row{
    gap:6px;
  }
  .mobile-header-toggle,
  .refresh-btn{
    min-height:42px;
    padding:9px 10px;
    font-size:22px;
    border-radius:12px;
    white-space:nowrap;
  }
}

.small{font-size:13px;color:#667085}
@media (min-width:900px){.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.card{margin:0}.actions{grid-template-columns:repeat(2,1fr)}.action-side-shot{max-width:104px}.action-side-shot .thumb{width:100px;max-height:72px}}

.seq-del{display:none !important;
  position:absolute;
  right:17px;
  top:50px;
  width:24px;
  height:24px;
  border:0;
  border-radius:999px;
  background:#ff4d4f;
  color:#fff;
  font-weight:900;
  font-size:15px;
  line-height:24px;
  cursor:pointer;
  box-shadow:0 1px 3px rgba(0,0,0,.18);
}
.seq-del:hover{background:#d92d20}
.offline-cleaner{margin-top:8px;display:flex;align-items:center;gap:12px;color:#344054;font-size:15px;font-weight:800;flex-wrap:wrap}
.offline-cleaner label{display:flex;align-items:center;gap:5px}
.offline-cleaner input[type="checkbox"]{width:16px;height:16px;accent-color:#1d9bf0}
.offline-cleaner input[type="number"]{width:64px;border:1px solid #d0d5dd;border-radius:8px;padding:5px 6px;font-size:15px;font-weight:800}
.dialog-input{
  width:100%;
  border:1px solid #d0d5dd;
  border-radius:12px;
  padding:12px;
  font-size:16px;
  font-weight:700;
  outline:none;
  margin:2px 0 14px;
}
@media (max-width:520px){
  .offline-cleaner{font-size:13px;gap:8px}
  .offline-cleaner input[type="number"]{width:58px;font-size:13px}
}


/* V4.5：顶部标题居中，状态/离线开关/刷新同一行 */
.title-row{
  position:relative;
  justify-content:center;
  text-align:center;
}
.title-row h1{
  width:100%;
  text-align:center;
}
.header-status-row{
  margin-top:8px;
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:12px;
}
.header-right-tools{
  margin-left:auto;
  display:flex;
  align-items:center;
  justify-content:flex-end;
  gap:10px;
  min-width:0;
}
.stats{
  color:#1d9bf0 !important;
  font-size:18px !important;
  font-weight:900 !important;
  white-space:nowrap;
}
.offline-cleaner{
  margin-top:0 !important;
  color:#1d9bf0 !important;
  font-size:17px !important;
  font-weight:900 !important;
}
.offline-cleaner label{
  color:#1d9bf0 !important;
}
@media (max-width:899px){
  .title-row{justify-content:center}
  .title-row h1{text-align:center}
  .header-status-row{
    display:block;
    margin-top:8px;
  }
  .stats{
    color:#1d9bf0 !important;
    font-size:16px !important; /* 手机端字号不变，只改蓝色 */
    font-weight:800 !important;
    white-space:normal;
  }
  .header-right-tools{
    margin-top:8px;
    display:flex;
    justify-content:space-between;
    gap:8px;
  }
  .offline-cleaner{
    color:#1d9bf0 !important;
    font-size:15px !important;
    font-weight:900 !important;
    flex:1 1 auto;
  }
  .offline-cleaner label{color:#1d9bf0 !important}
}
/* V4.5：电脑端上面两排按钮字号加大2号，不增加按钮高度 */
@media (min-width:900px){
  .desktop-top-grid .btn,
  .desktop-action-grid .btn{
    font-size:16px !important;
    min-height:42px !important;
    padding-top:11px !important;
    padding-bottom:11px !important;
    line-height:1.05;
  }
}


/* V4.6：第二行字体统一，比标题小2号 */
.title-row h1{
  font-size:22px !important;
}
.header-status-row .stats,
.header-status-row .offline-cleaner,
.header-status-row .offline-cleaner label,
.header-status-row .refresh-btn,
.header-status-row .mobile-header-toggle{
  font-size:20px !important;
  font-weight:900 !important;
  color:#1d9bf0 !important;
}
.header-status-row .refresh-btn,
.header-status-row .mobile-header-toggle{
  line-height:1 !important;
}
.header-status-row .offline-cleaner input[type="number"]{
  font-size:20px !important;
  font-weight:900 !important;
}
@media (max-width:899px){
  .title-row h1{
    font-size:22px !important;
  }
  .header-status-row .stats,
  .header-status-row .offline-cleaner,
  .header-status-row .offline-cleaner label,
  .header-status-row .refresh-btn,
  .header-status-row .mobile-header-toggle{
    font-size:20px !important;
    font-weight:900 !important;
    color:#1d9bf0 !important;
  }
  .header-status-row .offline-cleaner input[type="number"]{
    font-size:20px !important;
  }
}


/* V4.7：手机端第二行、第三行字小2号；第三行隐藏“离线超过”；展开/刷新不动 */
@media (max-width:899px){
  .header-status-row .stats{
    font-size:18px !important;
    font-weight:900 !important;
    color:#1d9bf0 !important;
  }
  .header-status-row .offline-cleaner,
  .header-status-row .offline-cleaner label{
    font-size:18px !important;
    font-weight:900 !important;
    color:#1d9bf0 !important;
  }
  .header-status-row .offline-cleaner input[type="number"]{
    font-size:18px !important;
    font-weight:900 !important;
    width:70px !important;
  }
  .offline-over-text{
    display:none !important;
  }
  .header-right-tools{
    align-items:center !important;
    gap:8px !important;
  }
  .header-right-tools .offline-cleaner{
    flex:1 1 auto !important;
    display:flex !important;
    align-items:center !important;
    gap:8px !important;
    flex-wrap:nowrap !important;
    min-width:0 !important;
  }
  .header-right-tools .offline-cleaner label{
    white-space:nowrap !important;
  }
  .header-status-row .refresh-btn,
  .header-status-row .mobile-header-toggle{
    font-size:20px !important; /* 展开和刷新保持 V4.6 大小 */
  }
}


/* V4.8：手机端顶部细调 */
@media (max-width:899px){
  .header-status-row .stats{
    font-size:17px !important;
    font-weight:900 !important;
    color:#1d9bf0 !important;
  }
  .header-status-row .offline-cleaner,
  .header-status-row .offline-cleaner label{
    font-size:17px !important;
    font-weight:900 !important;
    color:#1d9bf0 !important;
  }
  .header-status-row .offline-cleaner input[type="number"]{
    width:35px !important;
    min-width:35px !important;
    max-width:35px !important;
    font-size:17px !important;
    font-weight:900 !important;
    padding-left:3px !important;
    padding-right:3px !important;
    text-align:center !important;
  }
  .header-status-row .refresh-btn,
  .header-status-row .mobile-header-toggle{
    font-size:19px !important;
    font-weight:900 !important;
  }
}


/* V5.0 多用户绑定/用户管理 */
.bind-btn,.user-btn{margin-left:0!important;background:#e8f3ff!important;color:#1d9bf0!important}
.bound-modal{position:fixed;inset:0;background:rgba(15,23,42,.45);display:none;align-items:center;justify-content:center;z-index:140;padding:14px}
.bound-modal.show{display:flex}
.bound-box{width:min(760px,96vw);max-height:88vh;overflow:auto;background:#fff;border-radius:16px;padding:16px;box-shadow:0 12px 36px rgba(15,23,42,.25)}
.bound-title{font-size:20px;font-weight:900;margin-bottom:10px;color:#111827}
.bound-row{display:grid;grid-template-columns:1fr 100px 100px;gap:8px;margin-bottom:10px}
.bound-row input,.bound-row select{border:1px solid #d0d5dd;border-radius:10px;padding:10px;font-size:15px;min-width:0}
.bound-row button,.bound-list button,.user-actions button{border:0;border-radius:10px;padding:10px;font-weight:900;background:#1d9bf0;color:#fff}
.bound-list{display:grid;gap:8px}.bound-item{border:1px solid #e5e7eb;border-radius:10px;padding:9px;font-size:14px;display:grid;grid-template-columns:1fr auto;gap:8px;align-items:center}.bound-item .meta{color:#667085;font-size:12px;margin-top:3px}.bound-item button{background:#ff4d4f}.bound-actions{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px}.bound-actions button{border:0;border-radius:10px;padding:11px;font-weight:900}.bound-cancel{background:#e5e7eb;color:#111827}.bound-save{background:#1db954;color:#fff}
.user-panel{display:grid;gap:10px}.user-create{display:grid;grid-template-columns:1fr 90px 130px 130px 90px;gap:8px}.user-create input{border:1px solid #d0d5dd;border-radius:10px;padding:10px}.user-item{border:1px solid #e5e7eb;border-radius:10px;padding:10px}.key-once{background:#fff4e5;color:#b54708;padding:8px;border-radius:8px;margin-top:6px;word-break:break-all;font-weight:900}
@media (max-width:899px){.header-right-tools{gap:5px!important}.bound-row{grid-template-columns:1fr 70px 70px}.user-btn{display:none!important}.bind-btn,.refresh-btn,.mobile-header-toggle{padding-left:8px!important;padding-right:8px!important}.user-create{grid-template-columns:1fr 65px 90px 90px}.user-create button{grid-column:1/-1}}


/* V5.3：API Key 生成后固定弹窗，不被自动刷新冲掉 */
.key-modal{position:fixed;inset:0;background:rgba(15,23,42,.58);display:none;align-items:center;justify-content:center;z-index:9999;padding:18px}
.key-modal.show{display:flex}
.key-box{width:min(620px,94vw);background:#fff;border-radius:18px;padding:20px;box-shadow:0 14px 40px rgba(15,23,42,.30)}
.key-title{font-size:22px;font-weight:950;color:#111827;margin-bottom:10px}
.key-tip{font-size:14px;color:#d92d20;font-weight:800;margin-bottom:12px;line-height:1.45}
.key-value{width:100%;min-height:92px;border:1px solid #d0d5dd;border-radius:12px;padding:12px;font-size:16px;font-weight:800;word-break:break-all;resize:none;background:#f8fafc;color:#111827}
.key-actions{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px}
.key-actions button{border:0;border-radius:12px;padding:13px;font-size:17px;font-weight:900}
.key-copy{background:#1d9bf0;color:#fff}
.key-close{background:#e5e7eb;color:#111827}


/* V5.5：用户到期时间显示在标题上 */
.user-expire-title{
  display:inline-block;
  margin-left:8px;
  font-size:15px;
  font-weight:900;
  color:#1d9bf0;
  vertical-align:middle;
}
.user-expire-title.admin{
  color:#079455;
}
.user-expire-title.expiring{
  color:#b54708;
}
.user-expire-title.expired{
  color:#d92d20;
}
@media (max-width:899px){
  .user-expire-title{
    display:block;
    margin-left:0;
    margin-top:3px;
    font-size:14px;
    line-height:1.2;
  }
}


/* V5.6：修复用户管理弹窗层级，提示框必须在最上层 */
.dialog-modal{z-index:10050 !important}
.input-modal{z-index:10060 !important}
.key-modal{z-index:10070 !important}
.user-modal,.user-manager-modal,.bind-modal{z-index:9000}


/* V5.7：手机端刷新时间后面增加“用户”按钮 */
.stats-line-wrap{
  display:flex;
  align-items:center;
  gap:8px;
  width:100%;
}
.stats-line-wrap .stats{
  flex:1 1 auto;
  min-width:0;
}
.mobile-user-btn{
  flex:0 0 auto;
  border:0;
  border-radius:10px;
  background:#e8f3ff;
  color:#1d9bf0;
  font-weight:950;
  font-size:17px;
  padding:7px 10px;
  line-height:1;
}
@media (min-width:900px){
  .stats-line-wrap{display:block}
  .mobile-user-btn{display:none!important}
}
@media (max-width:899px){
  .stats-line-wrap{
    display:flex!important;
    align-items:center!important;
    justify-content:flex-start!important;
    gap:7px!important;
  }
  .stats-line-wrap .stats{
    white-space:normal!important;
    line-height:1.18!important;
  }
  .mobile-user-btn{
    display:inline-flex!important;
    align-items:center!important;
    justify-content:center!important;
    min-width:48px;
    height:32px;
    padding:6px 8px!important;
    font-size:17px!important;
    margin-left:2px;
  }
}


/* V5.8：电脑端顶部右侧一行显示；手机端用户管理输入框压缩 */
@media (min-width:900px){
  .header-status-row{
    display:flex!important;
    align-items:center!important;
    justify-content:space-between!important;
    gap:10px!important;
  }
  .header-right-tools{
    display:flex!important;
    flex-wrap:nowrap!important;
    align-items:center!important;
    justify-content:flex-end!important;
    gap:8px!important;
    white-space:nowrap!important;
  }
  .header-right-tools .offline-cleaner{
    display:flex!important;
    flex-wrap:nowrap!important;
    align-items:center!important;
    gap:8px!important;
    white-space:nowrap!important;
    flex:0 0 auto!important;
  }
  .header-right-tools .offline-cleaner label{
    white-space:nowrap!important;
    flex-wrap:nowrap!important;
  }
  .header-status-row .refresh-btn,
  .header-status-row .bind-btn,
  .header-status-row .user-btn,
  .header-status-row .mobile-header-toggle{
    white-space:nowrap!important;
    word-break:keep-all!important;
    line-height:1!important;
    min-width:76px!important;
    height:44px!important;
    padding:8px 12px!important;
    display:inline-flex!important;
    align-items:center!important;
    justify-content:center!important;
  }
  .header-status-row .bind-btn,
  .header-status-row .user-btn{
    min-width:86px!important;
  }
  .header-status-row .refresh-btn{
    min-width:64px!important;
  }
}
@media (max-width:899px){
  .stats-line-wrap{
    display:flex!important;
    flex-wrap:nowrap!important;
    align-items:center!important;
    gap:4px!important;
  }
  .stats-line-wrap .stats{
    flex:1 1 auto!important;
    min-width:0!important;
    white-space:nowrap!important;
    overflow:hidden!important;
    text-overflow:clip!important;
    font-size:16px!important;
  }
  .mobile-user-btn{
    min-width:42px!important;
    height:30px!important;
    font-size:16px!important;
    padding:5px 7px!important;
  }
  .user-create{
    grid-template-columns:minmax(0,2fr) 46px 62px 86px 58px!important;
    gap:6px!important;
    align-items:stretch!important;
  }
  .user-create input{
    min-width:0!important;
    padding:9px 6px!important;
    font-size:14px!important;
  }
  #newUsername{width:100%!important}
  #newMax{width:46px!important}
  #newDays{width:62px!important}
  #newPassword{width:86px!important}
  .user-create button{
    grid-column:auto!important;
    width:58px!important;
    padding:6px 4px!important;
    font-size:14px!important;
    line-height:1.05!important;
    white-space:normal!important;
  }
  .user-create button::first-line{
    line-height:1.05!important;
  }
}


/* V5.9：电脑端隐藏展开按钮，右侧按钮拉开；手机统计行加大2号 */
@media (min-width:900px){
  .header-status-row .mobile-header-toggle,
  .header-status-row .mobile-only{
    display:none!important;
  }
  .header-right-tools{
    gap:14px!important;
    padding-right:8px!important;
  }
  .header-right-tools .offline-cleaner{
    margin-right:8px!important;
  }
  .header-status-row .bind-btn,
  .header-status-row .user-btn,
  .header-status-row .refresh-btn{
    margin-left:0!important;
    margin-right:0!important;
    border-radius:12px!important;
    min-width:96px!important;
    height:46px!important;
    padding:8px 14px!important;
    font-size:18px!important;
    white-space:nowrap!important;
  }
  .header-status-row .refresh-btn{
    min-width:74px!important;
  }
  .header-status-row .bind-btn{
    min-width:98px!important;
  }
  .header-status-row .user-btn{
    min-width:98px!important;
  }
}
@media (max-width:899px){
  .stats-line-wrap .stats{
    font-size:18px!important;
    font-weight:950!important;
  }
}


/* V6.0：绑定设备下拉框明确为“选择用户”，普通用户隐藏 */
.bound-row select{
  background:#f8fafc!important;
  color:#111827!important;
  font-weight:800!important;
  text-align:center!important;
}
.bound-row select option{
  color:#111827!important;
}
@media (max-width:899px){
  body:not(.is-admin-mode) .bound-row{
    grid-template-columns:1fr 76px!important;
  }
  body:not(.is-admin-mode) .bound-row select{
    display:none!important;
  }
}
@media (min-width:900px){
  body:not(.is-admin-mode) .bound-row{
    grid-template-columns:1fr 120px!important;
  }
  body:not(.is-admin-mode) .bound-row select{
    display:none!important;
  }
}

</style>
</head>
<body>

<div id="apiKeyModal" class="key-modal">
  <div class="key-box">
    <div class="key-title">API 密钥已生成</div>
    <div class="key-tip">完整密钥只显示这一次。请立即复制保存，关闭后后台不会再显示完整密钥。</div>
    <textarea id="apiKeyValue" class="key-value" readonly></textarea>
    <div class="key-actions">
      <button class="key-copy" onclick="copyGeneratedApiKey()">复制密钥</button>
      <button class="key-close" onclick="closeGeneratedApiKey()">关闭</button>
    </div>
  </div>
</div>

<div class="header">
  <div class="title-row">
    <h1>TikTok 集群控制台 <span id="userExpireTitle" class="user-expire-title"></span></h1>
  </div>
  <div class="header-status-row">
    <div class="stats-line-wrap"><div class="stats" id="stats">加载中...</div><button id="mobileUserBtn" class="mobile-user-btn mobile-only admin-only" onclick="openUserModal()">用户</button></div>
    <div class="header-right-tools">
      <div class="offline-cleaner">
        <label><input id="autoHideOffline" type="checkbox" onchange="saveOfflineCleaner(); render()">自动隐藏离线</label>
        <label><span class="offline-over-text">离线超过</span> <input id="offlineHideMinutes" type="number" value="30" min="1" onchange="saveOfflineCleaner(); render()"> 分钟</label>
      </div>
      <button class="mobile-header-toggle mobile-only admin-only" id="mobileControlsToggle" onclick="toggleMobileControls()">⬇️ 展开</button>
      <button class="refresh-btn bind-btn" onclick="openBoundModal()">绑定设备</button><button class="refresh-btn user-btn admin-only" id="userManageBtn" onclick="openUserModal()">用户管理</button><button class="refresh-btn" onclick="loadDevices()">刷新</button>
    </div>
  </div>
</div>

<div class="wrap">
  <div id="mobileAllControls" class="all-grid mobile-only mobile-control-section collapsed admin-only">
    <button class="btn blue" onclick="sendAll('open_target')">全部打开软件</button>
    <button class="btn blue" onclick="sendAll('start_target')">全部启动软件</button>
    <button class="btn orange" onclick="sendAll('restart_app_only')">全部重启软件</button>

    <button class="btn green" onclick="sendAll('start_monitor')">全部打开监控</button>
    <button class="btn red" onclick="sendAll('stop_monitor')">全部停止监控</button>
    <button class="btn orange" onclick="sendAll('restart_app_start')">全部重启并启动</button>

    <button class="btn dark" onclick="batchScreenshotAll()">全部批量截图</button>
    <button class="btn dark" onclick="sendAll('update_github_config')">全部更新GitHub</button>
  </div>

  <div class="desktop-top-grid admin-only">
    <button class="btn blue" onclick="sendAll('open_target')">全部打开软件</button>
    <button class="btn blue" onclick="sendAll('start_target')">全部启动软件</button>
    <button class="btn green" onclick="sendAll('start_monitor')">全部打开监控</button>
    <button class="btn red" onclick="sendAll('stop_monitor')">全部停止监控</button>
    <button class="btn dark" onclick="batchScreenshotAll()">全部批量截图</button>
    <button class="btn orange" onclick="sendAll('restart_app_only')">全部重启软件</button>
    <button class="btn orange" onclick="sendAll('restart_app_start')">全部重启并启动</button>
    <button class="btn dark" onclick="sendAll('update_github_config')">全部更新GitHub</button>
  </div>

  <div id="mobileSelectedControls" class="multi-grid multi mobile-only mobile-control-section collapsed admin-only">
    <button class="btn blue" onclick="sendSelected('open_target')">打开</button>
    <button class="btn blue" onclick="sendSelected('start_target')">启动</button>
    <button class="btn green" onclick="sendSelected('start_monitor')">开监控</button>
    <button class="btn red" onclick="sendSelected('stop_monitor')">停监控</button>

    <button class="btn dark" onclick="screenshotSelected()">截图</button>
    <button class="btn orange" onclick="sendSelected('restart_app_only')">重启</button>
    <button class="btn orange" onclick="sendSelected('restart_app_start')">重启后启动</button>
    <button class="btn dark" onclick="sendSelected('update_github_config')">更新GitHub</button>

    <button class="btn gray wide" onclick="selectOnline()">多选在线</button>
    <button class="btn gray wide" onclick="clearSelected()">取消选择</button>
  </div>

  <div class="desktop-action-grid admin-only">
    <button class="btn blue" onclick="sendSelected('open_target')">打开</button>
    <button class="btn blue" onclick="sendSelected('start_target')">启动</button>
    <button class="btn green" onclick="sendSelected('start_monitor')">开监控</button>
    <button class="btn red" onclick="sendSelected('stop_monitor')">停监控</button>
    <button class="btn dark" onclick="screenshotSelected()">截图</button>
    <button class="btn orange" onclick="sendSelected('restart_app_only')">重启</button>
    <button class="btn orange" onclick="sendSelected('restart_app_start')">重启后启动</button>
    <button class="btn dark" onclick="sendSelected('update_github_config')">更新GitHub</button>
  </div>

  <div id="cards" class="cards"></div>
  <div class="footer"></div>
</div>


<div class="syncbar collapsed" id="syncbar">
  <button class="sync-toggle" id="syncToggle" onclick="toggleSyncBar()" title="展开/隐藏集群参数同步">⬆️</button>
  <div class="sync-title">集群参数同步</div>
  <div class="sync-row">
    <label class="sync-cutip">切IP <input id="sync_cut_ip" type="number" value="5"></label>
    <label class="sync-network">网络 <span><input name="sync_network" type="radio" value="4G">4G <input name="sync_network" type="radio" value="5G" checked>5G</span></label>
    <label class="sync-blue">蓝色不变切IP <input id="sync_blue_no_change_auto_ip" type="number" value="310"></label>
    <label class="sync-ocr">OCR间隔 <input id="sync_ocr_interval" type="number" value="30"></label>
  </div>
  <div class="sync-row">
    <label class="sync-norestart">时长不走重启 <input id="sync_check_no_response" type="number" value="150"></label>
    <label class="sync-restartthen"><input id="sync_restart_then_start" type="checkbox" checked>重启后启动</label>
    <label class="sync-restartdelay">重启延迟 <input id="sync_restart_open_delay" type="number" value="3"></label>
    <label class="sync-startclicks">点起动 <input id="sync_start_clicks" type="number" value="5"></label>
  </div>
  <div class="sync-row">
    <button class="sync-btn primary sync-save-selected" onclick="syncConfigSelected()"><span class="two-line">保存并<br>同步选中</span></button>
    <button class="sync-btn green sync-save-all" onclick="syncConfigAll()"><span class="two-line">保存并<br>同步全部</span></button>
    <button class="sync-btn sync-select-online" onclick="selectOnline()">多选在线</button>
    <button class="sync-btn sync-clear" onclick="clearSelected()">取消选择</button>
  </div>

  <div class="package-section">
    <label class="pkg-field pkg-url">更新包URL <input id="pkg_url" type="text" placeholder="GitHub Release zip 下载链接"></label>
    <label class="pkg-field pkg-exe">EXE名 <input id="pkg_exe" type="text" placeholder="必须带 .exe 后缀，例如：TIKTOK点赞系统-3.19 D版本.exe"></label>
    <label class="pkg-field pkg-sha">SHA256 <input id="pkg_sha256" type="text" placeholder="可选，建议填写；不要带 sha256: 前缀"></label>
    <label class="pkg-field pkg-folder">文件夹名 <input id="pkg_folder" type="text" placeholder="留空=按zip顶层文件夹"></label>

    <label class="pkg-field pkg-title">窗口标题 <input id="pkg_title" type="text" placeholder="新版窗口标题，可空"></label>
    <div class="pkg-checks">
      <label><input id="pkg_launch" type="checkbox" checked>解压后打开</label>
      <label><input id="pkg_start" type="checkbox">打开后启动</label>
    </div>
    <button class="pkg-btn primary" onclick="updatePackageSelected()">更新选中</button>
    <button class="pkg-btn green" onclick="updatePackageAll()">更新全部在线</button>
  </div>
</div>


<div id="centerDialog" class="dialog-modal">
  <div class="dialog-box">
    <div id="dialogText" class="dialog-text"></div>
    <input id="dialogInput" class="dialog-input" style="display:none" autocomplete="off">
    <div id="dialogActions" class="dialog-actions">
      <button id="dialogCancel" class="dialog-cancel">取消</button>
      <button id="dialogOk" class="dialog-ok">确定</button>
    </div>
  </div>
</div>


<div id="boundModal" class="bound-modal" onclick="closeBoundModal()">
  <div class="bound-box" onclick="event.stopPropagation()">
    <div class="bound-title">绑定设备机器码</div>
    <div class="bound-row">
      <input id="boundMachineInput" placeholder="请输入机器码，一次一个">
      <select id="boundUserSelect" style="display:none"><option value="">选择用户</option></select>
      <button onclick="addBoundDevice()">添加</button>
    </div>
    <div id="boundList" class="bound-list"></div>
    <div class="bound-actions"><button class="bound-cancel" onclick="closeBoundModal()">取消</button><button class="bound-save" onclick="closeBoundModal()">保存</button></div>
  </div>
</div>
<div id="userModal" class="bound-modal" onclick="closeUserModal()">
  <div class="bound-box" onclick="event.stopPropagation()">
    <div class="bound-title">用户管理</div>
    <div class="user-create">
      <input id="newUsername" placeholder="用户名">
      <input id="newMax" type="number" value="3" placeholder="设备数">
      <input id="newDays" placeholder="到期天数">
      <input id="newPassword" type="password" placeholder="登录密码">
      <button onclick="createUser()">创建<br>用户</button>
    </div>
    <div id="userList" class="user-panel"></div>
    <div class="bound-actions"><button class="bound-cancel" onclick="closeUserModal()">关闭</button><button class="bound-save" onclick="loadUsers()">刷新</button></div>
  </div>
</div>

<div id="imgModal" class="modal" onclick="closeModal()">
  <button class="close" onclick="closeModal()">×</button>
  <img id="modalImg">
</div>

<script>
const SERVER = location.origin;
const params = new URLSearchParams(location.search);
const keyFromUrl = params.get("key") || "";
const apiKeyFromUrl = params.get("api_key") || "";
if (keyFromUrl) localStorage.setItem("ADMIN_KEY", keyFromUrl);
if (apiKeyFromUrl) {
  localStorage.setItem("USER_API_KEY", apiKeyFromUrl);
  localStorage.removeItem("ADMIN_KEY");
}
const ADMIN_KEY = keyFromUrl || (apiKeyFromUrl ? "" : (localStorage.getItem("ADMIN_KEY") || ""));
const USER_API_KEY = apiKeyFromUrl || localStorage.getItem("USER_API_KEY") || "";
const IS_ADMIN = !!ADMIN_KEY;
if(IS_ADMIN){ document.body.classList.add("is-admin-mode"); }
let DEVICES = [];
let selected = new Set();

function loadOfflineCleaner(){
  const enabled = localStorage.getItem("AUTO_HIDE_OFFLINE") === "1";
  const minutes = Number(localStorage.getItem("OFFLINE_HIDE_MINUTES") || 30);
  const cb = document.getElementById("autoHideOffline");
  const input = document.getElementById("offlineHideMinutes");
  if(cb) cb.checked = enabled;
  if(input) input.value = minutes > 0 ? minutes : 30;
}
function saveOfflineCleaner(){
  const cb = document.getElementById("autoHideOffline");
  const input = document.getElementById("offlineHideMinutes");
  localStorage.setItem("AUTO_HIDE_OFFLINE", cb && cb.checked ? "1" : "0");
  localStorage.setItem("OFFLINE_HIDE_MINUTES", String(Number(input && input.value || 30)));
}
function shouldHideOfflineDevice(d){
  const cb = document.getElementById("autoHideOffline");
  if(!cb || !cb.checked) return false;
  const input = document.getElementById("offlineHideMinutes");
  const minutes = Math.max(1, Number(input && input.value || 30));
  return !d.online && Number(d.last_seen_ago || 0) >= minutes * 60;
}


function centerDialog(message, options={}){
  return new Promise(resolve=>{
    const modal = document.getElementById("centerDialog");
    const text = document.getElementById("dialogText");
    const actions = document.getElementById("dialogActions");
    const cancel = document.getElementById("dialogCancel");
    const ok = document.getElementById("dialogOk");
    const input = document.getElementById("dialogInput");
    const confirmMode = options.confirm !== false;
    const inputMode = options.input === true;
    text.textContent = message;
    actions.classList.toggle("single", !confirmMode);
    cancel.style.display = confirmMode ? "" : "none";
    ok.textContent = options.okText || "确定";
    cancel.textContent = options.cancelText || "取消";
    if(input){
      input.style.display = inputMode ? "" : "none";
      input.value = inputMode ? (options.defaultValue || "") : "";
    }

    const cleanup = (val)=>{
      modal.classList.remove("show");
      ok.onclick = null;
      cancel.onclick = null;
      if(input) input.onkeydown = null;
      resolve(val);
    };
    ok.onclick = ()=>cleanup(inputMode ? (input ? input.value.trim() : "") : true);
    cancel.onclick = ()=>cleanup(inputMode ? "" : false);
    if(input){
      input.onkeydown = (e)=>{
        if(e.key === "Enter"){ e.preventDefault(); ok.click(); }
        if(e.key === "Escape"){ e.preventDefault(); cancel.click(); }
      };
    }
    modal.classList.add("show");
    if(inputMode && input){
      setTimeout(()=>{ input.focus(); input.select(); }, 50);
    }
  });
}
function centerAlert(message){
  return centerDialog(message, {confirm:false, okText:"知道了"});
}
function centerConfirm(message){
  return centerDialog(message, {confirm:true, okText:"确定", cancelText:"取消"});
}
function centerPrompt(message, defaultValue=""){
  return centerDialog(message, {confirm:true, input:true, defaultValue, okText:"确定", cancelText:"取消"});
}


function setMobileControlsCollapsed(collapsed){
  const allBox = document.getElementById("mobileAllControls");
  const selBox = document.getElementById("mobileSelectedControls");
  const btn = document.getElementById("mobileControlsToggle");
  if(allBox) allBox.classList.toggle("collapsed", collapsed);
  if(selBox) selBox.classList.toggle("collapsed", collapsed);
  if(btn) btn.textContent = collapsed ? "⬇️ 展开" : "⬆️ 隐藏";
  localStorage.setItem("MOBILE_CONTROLS_COLLAPSED", collapsed ? "1" : "0");
}
function toggleMobileControls(){
  const allBox = document.getElementById("mobileAllControls");
  const collapsed = !allBox || allBox.classList.contains("collapsed");
  setMobileControlsCollapsed(!collapsed);
}
document.addEventListener("DOMContentLoaded", ()=>{
  const saved = localStorage.getItem("MOBILE_CONTROLS_COLLAPSED");
  setMobileControlsCollapsed(saved === null ? true : saved === "1");
});


function setSyncBarCollapsed(collapsed){
  const bar = document.getElementById("syncbar");
  const btn = document.getElementById("syncToggle");
  if(!bar || !btn) return;
  bar.classList.toggle("collapsed", collapsed);
  document.body.classList.toggle("sync-collapsed", collapsed);
  btn.textContent = collapsed ? "⬆️" : "⬇️";
  localStorage.setItem("SYNC_BAR_COLLAPSED", collapsed ? "1" : "0");
}
function toggleSyncBar(){
  const bar = document.getElementById("syncbar");
  const collapsed = !bar || !bar.classList.contains("collapsed") ? true : false;
  setSyncBarCollapsed(collapsed);
}
document.addEventListener("DOMContentLoaded", ()=>{
  const saved = localStorage.getItem("SYNC_BAR_COLLAPSED");
  setSyncBarCollapsed(saved === null ? true : saved === "1");
});



function headers(){
  const h = {"Content-Type":"application/json"};
  if(ADMIN_KEY) h["X-Admin-Key"] = ADMIN_KEY;
  if(USER_API_KEY) h["Authorization"] = "Bearer " + USER_API_KEY;
  return h;
}
async function api(path, opts={}){
  opts.headers = Object.assign(headers(), opts.headers || {});
  const r = await fetch(path, opts);
  if(!r.ok){
    const txt = await r.text();
    throw new Error(txt || r.statusText);
  }
  return await r.json();
}
function isBadCarrier(d){
  const txt = `${d.location_carrier||""} ${d.carrier||""}`;
  return txt.includes("电信") || txt.includes("广电");
}
function stateClass(d){
  if(!d.online) return "offline";
  if(isBadCarrier(d)) return "bad";
  if(d.running) return "running";
  if(["screenshotting","switching_ip","updating_package"].includes(d.display_state||d.status)) return "busy";
  return "";
}


function formatAgoSeconds(sec){
  sec = Number(sec || 0);
  if(sec < 60) return `${sec}秒前`;
  if(sec < 3600) return `${Math.floor(sec/60)}分钟前`;
  if(sec < 86400) return `${Math.floor(sec/3600)}小时前`;
  return `${Math.floor(sec/86400)}天前`;
}
function agoText(d){
  const t = formatAgoSeconds(d.last_seen_ago || 0);
  return d.online ? t : `离线 ${t}`;
}

function workTimeText(d){
  const v = d.work_time || d.workTime || d.work_seconds || "";
  if(v === null || v === undefined || String(v).trim()==="") return "-";
  return escapeHtml(v);
}

function stateText(d){
  if(!d.online) return "离线";
  const s = d.display_state || d.status || "online";
  if(s==="switching_ip") return "切IP中";
  if(s==="screenshotting") return d.has_screenshot ? "在线" : "截图中";
  if(s==="updating_package") return "更新中";
  return "在线";
}

function isMobileView(){
  return window.matchMedia && window.matchMedia("(max-width:899px)").matches;
}
function buildStatsText(deviceText, online, running){
  const t = new Date().toLocaleTimeString();
  if(isMobileView()){
    return `设备 ${deviceText}, 在线 ${online}, 监控 ${running}, 刷新时间 ${t}`;
  }
  return `设备：${deviceText}，在线：${online}，监控：${running}，刷新时间：${t}`;
}

function render(){
  const visibleDevices = DEVICES.filter(d=>!shouldHideOfflineDevice(d));
  const online = visibleDevices.filter(d=>d.online).length;
  const running = visibleDevices.filter(d=>d.online && d.running).length;
  const hiddenCount = DEVICES.length - visibleDevices.length;
  const deviceText = hiddenCount > 0 ? `${visibleDevices.length}/${DEVICES.length}` : `${visibleDevices.length}`;
  document.getElementById("stats").textContent = buildStatsText(deviceText, online, running);
  const cards = document.getElementById("cards");
  cards.innerHTML = "";
  for(const [idx,d] of visibleDevices.entries()){
    const code = d.machine_code || "";
    const seq = idx + 1;
    const card = document.createElement("div");
    card.className = "card " + stateClass(d);
    const checked = selected.has(code) ? "checked" : "";
    const loc = d.location_carrier || `${d.location||""}${d.carrier||""}` || "-";
    const bad = isBadCarrier(d);
    const thumb = d.has_screenshot ? `<div class="action-side-shot"><img class="thumb" onclick="event.stopPropagation();showShot('${code}')" src="/api/devices/${encodeURIComponent(code)}/screenshot/image?${ADMIN_KEY ? 'key='+encodeURIComponent(ADMIN_KEY) : 'api_key='+encodeURIComponent(USER_API_KEY)}&t=${d.screenshot_time||0}"><div>点击放大</div></div>` : `<div class="action-side-shot action-side-empty">缩略图<br>暂无</div>`;
    card.innerHTML = `
      <div class="seq">${seq}</div>
      <div class="dev-head">
        <input class="select-box" type="checkbox" ${checked} onchange="toggleSelect('${code}', this.checked)">
        <div class="name" title="${escapeHtml(d.device_name||code.slice(0,8)||"未命名")}">${escapeHtml(d.device_name||code.slice(0,8)||"未命名")}</div>
        <div class="ago-top">${agoText(d)}</div>
      </div>
      <div class="line">
        <span class="badge ${d.online?'':'red'}">${stateText(d)}</span>
        <b>在线</b>：${d.online?"是":"否"}　
        <b>监控</b>：${(d.online && d.running)?"是":"否"}　
        <b>工作时间</b>：${workTimeText(d)}
      </div>
      <div class="info-shot-wrap">
        <div class="info-shot-main">
          <div class="line ${bad?'bad-text':''}">运营商位置：${escapeHtml(loc)}　公网IP：${escapeHtml(d.public_ip||"-")}</div>
          <div class="line small">机器码：${escapeHtml(code)}</div>
        </div>
      </div>
      <div class="action-shot-wrap">
      <div class="actions">
        <button class="btn blue" onclick="sendOne('${code}','open_target')">打开</button>
        <button class="btn blue" onclick="sendOne('${code}','start_target')">启动</button>
        <button class="btn green" onclick="sendOne('${code}','start_monitor')">开监控</button>
        <button class="btn red" onclick="sendOne('${code}','stop_monitor')">停监控</button>

        <button class="btn dark" onclick="renameOne('${code}')">改名</button>
        <button class="btn dark" onclick="sendOne('${code}','screenshot', true)">截图</button>
        <button class="btn orange" onclick="sendOne('${code}','restart_app_only')">重启</button>
        <button class="btn orange device-restart-start" onclick="sendOne('${code}','restart_app_start')"><span class="two-line">重启后<br>启动</span></button>
      </div>
      ${thumb}
      </div>
    `;
    cards.appendChild(card);
  }
}
function escapeHtml(s){
  return String(s||"").replace(/[&<>"']/g, m=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[m]));
}
function toggleSelect(code,on){
  if(on) selected.add(code); else selected.delete(code);
}
function selectOnline(){
  selected.clear();
  DEVICES.filter(d=>!shouldHideOfflineDevice(d) && d.online).forEach(d=>selected.add(d.machine_code));
  render();
}
function clearSelected(){ selected.clear(); render(); }

function fmtTime(v){ if(!v) return "-"; try{return new Date(v).toLocaleString()}catch(e){return String(v)} }
async function ensureUsersForSelect(){
  if(!IS_ADMIN) return [];
  try{
    const data = await api("/admin/api/users");
    const users = data.users || [];
    const sel = document.getElementById("boundUserSelect");
    if(sel){
      sel.style.display = "";
      sel.innerHTML = users.map(u=>`<option value="${u.id}">${escapeHtml(u.username)}(${u.id})</option>`).join("");
    }
    return users;
  }catch(e){return []}
}
async function openBoundModal(){
  document.getElementById("boundModal").classList.add("show");
  await ensureUsersForSelect();
  await loadBoundDevices();
}
function closeBoundModal(){ document.getElementById("boundModal").classList.remove("show"); }
async function loadBoundDevices(){
  try{
    const data = await api("/api/bound-devices");
    const list = document.getElementById("boundList");
    const rows = data.devices || [];
    if(!rows.length){ list.innerHTML = `<div class="small">暂无绑定机器码</div>`; return; }
    list.innerHTML = rows.map(r=>{
      const status = r.status === "disabled" ? "禁用" : (r.online ? "在线" : "离线");
      const user = IS_ADMIN ? `用户：${escapeHtml(r.username||r.user_id||"")}　` : "";
      return `<div class="bound-item"><div><b>${escapeHtml(r.machine_code)}</b><div class="meta">${user}名称：${escapeHtml(r.device_name||"-")}　状态：${status}　绑定：${fmtTime(r.bound_at)}</div></div><button onclick="deleteBoundDevice('${r.machine_code}')">删除</button></div>`;
    }).join("");
  }catch(e){ await centerAlert("读取绑定设备失败："+e.message); }
}
async function addBoundDevice(){
  const input = document.getElementById("boundMachineInput");
  const code = (input.value||"").trim();
  if(!code){ await centerAlert("请输入机器码"); return; }
    const userSel = document.getElementById("boundUserSelect") || document.getElementById("bindUserSelect");
    const user_id = userSel ? userSel.value : "";
    if(IS_ADMIN && userSel && !user_id){ await centerAlert("请选择用户"); return; }
  const body = {machine_code: code};
  const sel = document.getElementById("boundUserSelect");
  if(IS_ADMIN && sel && sel.value) body.user_id = Number(sel.value);
  try{
    await api("/api/bound-devices", {method:"POST", body:JSON.stringify(body)});
    input.value = "";
    await loadBoundDevices();
    await loadDevices();
  }catch(e){ await centerAlert("添加失败："+e.message); }
}
async function deleteBoundDevice(code){
  if(!await centerConfirm(`确定删除绑定机器码？\n${code}`)) return;
  try{
    await api(`/api/bound-devices/${encodeURIComponent(code)}`, {method:"DELETE"});
    await loadBoundDevices();
    await loadDevices();
  }catch(e){ await centerAlert("删除失败："+e.message); }
}
async function openUserModal(){
  if(!IS_ADMIN){ await centerAlert("只有管理员可用"); return; }
  document.getElementById("userModal").classList.add("show");
  await loadUsers();
}
function closeUserModal(){ document.getElementById("userModal").classList.remove("show"); }

let GENERATED_API_KEY_CACHE = "";

function showGeneratedApiKey(key){
  GENERATED_API_KEY_CACHE = key || "";
  const modal = document.getElementById("apiKeyModal");
  const val = document.getElementById("apiKeyValue");
  if(val){
    val.value = GENERATED_API_KEY_CACHE;
    setTimeout(()=>{ try{ val.focus(); val.select(); }catch(e){} }, 80);
  }
  if(modal) modal.classList.add("show");
}

async function copyGeneratedApiKey(){
  const key = GENERATED_API_KEY_CACHE || (document.getElementById("apiKeyValue")?.value || "");
  if(!key) return;
  try{
    await navigator.clipboard.writeText(key);
    await centerAlert("已复制 API 密钥");
  }catch(e){
    const val = document.getElementById("apiKeyValue");
    if(val){ val.focus(); val.select(); try{ document.execCommand("copy"); }catch(_e){} }
    await centerAlert("已尝试复制，如未成功请手动长按/选中复制");
  }
}

function closeGeneratedApiKey(){
  const modal = document.getElementById("apiKeyModal");
  if(modal) modal.classList.remove("show");
  GENERATED_API_KEY_CACHE = "";
  const val = document.getElementById("apiKeyValue");
  if(val) val.value = "";
}

async function loadUsers(){
  if(!IS_ADMIN) return;
  try{
    const data = await api("/admin/api/users");
    const list = document.getElementById("userList");
    const users = data.users || [];
    if(!users.length){ list.innerHTML = `<div class="small">暂无用户，请先创建</div>`; return; }
    list.innerHTML = users.map(u=>`<div class="user-item"><b>${escapeHtml(u.username)}</b>　ID:${u.id}　状态:${u.status}　设备:${u.device_count||0}/${u.max_devices}　密钥:${u.key_count||0}<div class="user-actions" style="margin-top:8px"><button onclick="generateKey(${u.id})">生成密钥</button><button onclick="showUserKeys(${u.id})">查看密钥</button><button onclick="adminResetPassword(${u.id})">改密码</button><button onclick="adminAddDevice(${u.id})">加设备</button><button onclick="deleteUser(${u.id})">删用户</button><button onclick="toggleUser(${u.id}, '${u.status==='active'?'disabled':'active'}')">${u.status==='active'?'禁用':'启用'}</button></div><div id="key_${u.id}"></div></div>`).join("");
  }catch(e){ await centerAlert("读取用户失败："+e.message); }
}
async function createUser(){
  try{
    const usernameEl = document.getElementById("newUsername");
    const maxEl = document.getElementById("newMax");
    const daysEl = document.getElementById("newDays");
    const passwordEl = document.getElementById("newPassword");

    const username = (usernameEl ? usernameEl.value : "").trim();
    const max_devices = Number(maxEl && maxEl.value ? maxEl.value : 3);
    const expires_days = Number(daysEl && daysEl.value ? daysEl.value : 0);
    const password = (passwordEl ? passwordEl.value : "").trim();

    if(!username){ await centerAlert("请输入用户名"); return; }
    if(!expires_days || expires_days <= 0){ await centerAlert("请输入到期天数，例如 30 或 365"); return; }

    await api("/admin/api/users", {
      method:"POST",
      body:JSON.stringify({username, max_devices, expires_days, password})
    });

    if(usernameEl) usernameEl.value="";
    if(maxEl) maxEl.value="3";
    if(daysEl) daysEl.value="";
    if(passwordEl) passwordEl.value="";
    await loadUsers();
    await centerAlert("用户创建成功");
  }
  catch(e){ await centerAlert("创建失败："+e.message); }
}
async function generateKey(uid){
  try{
    const data = await api(`/admin/api/users/${uid}/api-key`, {method:"POST", body:"{}"});
    showGeneratedApiKey(data.api_key || "");
    await loadUsers();
  }
  catch(e){ await centerAlert("生成失败："+e.message); }
}

async function showUserKeys(uid){
  try{
    const data = await api(`/admin/api/users/${uid}/api-keys`);
    const rows = data.api_keys || [];
    if(!rows.length){ await centerAlert("这个用户还没有密钥"); return; }
    const text = rows.map(k=>{
      const full = k.key_plain || "旧密钥不可查看，请重新生成";
      return `ID:${k.id} 状态:${k.status} 前缀:${k.key_prefix}\n${full}`;
    }).join("\n\n");
    showGeneratedApiKey(text);
  }catch(e){ await centerAlert("查看密钥失败："+e.message); }
}

async function adminResetPassword(uid){
  try{
    const password = await centerPrompt("输入新的登录密码，至少 6 位：", "");
    if(!password) return;
    await api(`/admin/api/users/${uid}/password`, {
      method:"POST",
      body:JSON.stringify({password})
    });
    await centerAlert("密码已修改");
  }catch(e){ await centerAlert("修改密码失败：" + e.message); }
}

async function adminAddDevice(uid){
  try{
    const code = await centerPrompt("输入要绑定到该用户的机器码：", "");
    if(!code) return;
    const name = await centerPrompt("设备名称，可空：", "");
    await api(`/admin/api/users/${uid}/bound-devices`, {
      method:"POST",
      body:JSON.stringify({machine_code:code, device_name:name})
    });
    await centerAlert("已添加绑定设备");
    await loadUsers();
  }catch(e){ await centerAlert("添加设备失败："+e.message); }
}

async function deleteUser(uid){
  try{
    if(!await centerConfirm("确定删除这个用户？该用户的密钥和绑定设备也会删除。")) return;
    await api(`/admin/api/users/${uid}`, {method:"DELETE"});
    await centerAlert("用户已删除");
    await loadUsers();
  }catch(e){ await centerAlert("删除用户失败："+e.message); }
}

async function toggleUser(uid,status){
  try{ await api(`/admin/api/users/${uid}`, {method:"PATCH", body:JSON.stringify({status})}); await loadUsers(); }
  catch(e){ await centerAlert("操作失败："+e.message); }
}
async function changeMyPassword(){
  try{
    const old_password = await centerPrompt("输入当前密码：", "");
    if(!old_password) return;
    const new_password = await centerPrompt("输入新密码，至少 6 位：", "");
    if(!new_password) return;
    const repeat = await centerPrompt("再输入一次新密码：", "");
    if(new_password !== repeat){ await centerAlert("两次新密码不一致"); return; }
    await api("/api/me/password", {
      method:"POST",
      body:JSON.stringify({old_password, new_password})
    });
    await centerAlert("密码已修改");
  }catch(e){ await centerAlert("修改密码失败：" + e.message); }
}
document.addEventListener("DOMContentLoaded", ()=>{ const b=document.getElementById("userManageBtn"); if(b && !IS_ADMIN) b.style.display="none"; const m=document.getElementById("mobileUserBtn"); if(m && !IS_ADMIN) m.style.display="none"; const p=document.getElementById("passwordBtn"); if(p && IS_ADMIN) p.style.display="none"; });

async function loadDevices(){
  try{
    const data = await api("/api/devices");
    DEVICES = data.devices || [];
    render();
  }catch(e){
    document.getElementById("stats").textContent = "刷新失败：" + e.message;
  }
}
async function sendAll(cmd){
  if(!confirm("确定下发到全部在线设备？")) return;
  try{
    const data = await api("/api/commands/all",{method:"POST",body:JSON.stringify({command:cmd})});
    alert(`已下发 ${cmd}，数量：${data.count||0}`);
    setTimeout(loadDevices, 1000);
  }catch(e){ alert("失败：" + e.message); }
}
async function sendOne(code,cmd,shot=false){
  try{
    await api(`/api/devices/${encodeURIComponent(code)}/command`,{method:"POST",body:JSON.stringify({command:cmd})});
    if(shot){
      setTimeout(loadDevices, 4500);
      setTimeout(loadDevices, 8500);
      setTimeout(loadDevices, 12500);
    }else{
      setTimeout(loadDevices, 1000);
    }
  }catch(e){ alert("失败：" + e.message); }
}
async function sendSelected(cmd){
  const list = [...selected];
  if(list.length===0){ alert("请先勾选设备"); return; }
  if(!confirm(`确定给选中的 ${list.length} 台设备下发 ${cmd}？`)) return;
  const results = await Promise.all(list.map(code =>
    api(`/api/devices/${encodeURIComponent(code)}/command`, {
      method:"POST",
      body:JSON.stringify({command:cmd})
    }).then(()=>({ok:true, code})).catch(e=>({ok:false, code, error:e.message}))
  ));
  const failed = results.filter(r=>!r.ok);
  if(failed.length){
    alert(`部分设备下发失败：${failed.length}/${list.length}\n` + failed.map(r=>`${r.code}: ${r.error}`).join("\n"));
  }
  if(cmd==="screenshot"){
    setTimeout(loadDevices, 4500);
    setTimeout(loadDevices, 8500);
    setTimeout(loadDevices, 12500);
  }else{
    setTimeout(loadDevices, 1000);
  }
}
async function screenshotSelected(){ await sendSelected("screenshot"); }
async function batchScreenshotAll(){ await sendAll("screenshot"); setTimeout(loadDevices, 4500); setTimeout(loadDevices, 8500); setTimeout(loadDevices, 12500); }

async function deleteDeviceConfirm(event, code){
  if(event) event.stopPropagation();
  const d = DEVICES.find(x=>x.machine_code===code) || {};
  const name = d.device_name || code.slice(0,8) || "未命名";
  const ago = agoText(d);
  if(!await centerConfirm(`确定删除这个设备记录吗？\n\n设备：${name}\n状态：${ago}\n\n删除后网页不再显示，设备重新上线后会重新出现。`)) return;
  try{
    await api(`/api/devices/${encodeURIComponent(code)}`, {method:"DELETE"});
    selected.delete(code);
    await loadDevices();
  }catch(e){
    await centerAlert("删除失败：" + e.message);
  }
}

async function renameSelected(){
  const list = [...selected];
  if(list.length===0){ alert("请先勾选设备"); return; }
  if(list.length===1){ return renameOne(list[0]); }
  const base = await centerPrompt(`已选择 ${list.length} 台设备，输入批量基础名称：`, "");
  if(!base) return;
  let i = 1;
  try{
    for(const code of list){
      await api(`/api/devices/${encodeURIComponent(code)}/command`,{
        method:"POST",
        body:JSON.stringify({command:"rename",value:`${base}${i}`})
      });
      i++;
    }
    setTimeout(loadDevices,1000);
    setTimeout(()=>screenshotSelected(),3000);
  }catch(e){ alert("批量改名失败：" + e.message); }
}

async function renameOne(code){
  const d = DEVICES.find(x=>x.machine_code===code) || {};
  const name = await centerPrompt("输入新设备名称：", d.device_name || "");
  if(!name) return;
  try{
    await api(`/api/devices/${encodeURIComponent(code)}/command`,{method:"POST",body:JSON.stringify({command:"rename",value:name})});
    setTimeout(loadDevices,1000);
    // V3.0：改名后延迟3秒自动截图回传，方便确认改名是否成功
    setTimeout(()=>sendOne(code,'screenshot', true),3000);
    setTimeout(loadDevices,7000);
    setTimeout(loadDevices,11000);
  }catch(e){ alert("改名失败：" + e.message); }
}
function showShot(code){
  const d = DEVICES.find(x=>x.machine_code===code) || {};
  const t = d.screenshot_time || 0;
  const img = document.getElementById("modalImg");
  img.src = `/api/devices/${encodeURIComponent(code)}/screenshot/image?${ADMIN_KEY ? "key="+encodeURIComponent(ADMIN_KEY) : "api_key="+encodeURIComponent(USER_API_KEY)}&t=${t}`;
  document.getElementById("imgModal").classList.add("show");
}
function closeModal(){
  document.getElementById("imgModal").classList.remove("show");
}

function getSyncConfig(){
  const networkEl = document.querySelector('input[name="sync_network"]:checked');
  const noRespEl = document.getElementById("sync_check_no_response");
  const oldNoRestart = document.getElementById("sync_no_restart_when_duration_ok");
  const noResp = noRespEl ? Number(noRespEl.value || 150) : (oldNoRestart ? oldNoRestart.checked : true);
  return {
    switch_ip: Number(document.getElementById("sync_cut_ip").value || 5),
    network_mode: networkEl ? networkEl.value : "5G",
    auto_switch_ip_keep_blue: Number(document.getElementById("sync_blue_no_change_auto_ip").value || 310),
    ocr_interval: Number(document.getElementById("sync_ocr_interval").value || 30),
    check_interval_no_response: noResp,
    no_restart_when_duration_ok: noResp,
    restart_after_launch: document.getElementById("sync_restart_then_start").checked,
    restart_then_start: document.getElementById("sync_restart_then_start").checked,
    restart_launch_delay: Number(document.getElementById("sync_restart_open_delay").value || 3),
    restart_open_delay: Number(document.getElementById("sync_restart_open_delay").value || 3),
    start_click_delay: Number(document.getElementById("sync_start_clicks").value || 5),
    start_clicks: Number(document.getElementById("sync_start_clicks").value || 5)
  };
}
async function syncConfigOne(code, cfg){
  await api(`/api/devices/${encodeURIComponent(code)}/config`,{
    method:"POST",
    body:JSON.stringify({config:cfg})
  });
}
async function syncConfigSelected(){
  const list = [...selected];
  if(list.length===0){ await centerAlert("请先勾选设备"); return; }
  const cfg = getSyncConfig();
  if(!await centerConfirm(`确定同步参数到选中的 ${list.length} 台设备？`)) return;
  try{
    for(const code of list){ await syncConfigOne(code, cfg); }
    await centerAlert("已同步选中设备");
    setTimeout(loadDevices,1000);
  }catch(e){ await centerAlert("同步失败：" + e.message); }
}
async function syncConfigAll(){
  const cfg = getSyncConfig();
  if(!await centerConfirm("确定同步参数到全部在线设备？")) return;
  try{
    const data = await api("/api/config/all",{method:"POST",body:JSON.stringify({config:cfg})});
    await centerAlert(`已同步全部在线设备，数量：${data.count||0}`);
    setTimeout(loadDevices,1000);
  }catch(e){ await centerAlert("同步失败：" + e.message); }
}


function getPackageConfig(){
  return {
    package_url: document.getElementById("pkg_url").value.trim(),
    sha256: document.getElementById("pkg_sha256").value.trim(),
    folder_name: document.getElementById("pkg_folder").value.trim(),
    exe_name: document.getElementById("pkg_exe").value.trim(),
    window_title: document.getElementById("pkg_title").value.trim(),
    extract_base: "desktop",
    auto_launch: document.getElementById("pkg_launch").checked,
    auto_click_start: document.getElementById("pkg_start").checked
  };
}
function validatePackageConfig(cfg){
  if(!cfg.package_url) return "请先填写更新包URL";
  if(!/\.zip($|\?)/i.test(cfg.package_url)) return "目前远程更新只支持 .zip，建议把 rar 重新压成 zip";
  if(!cfg.exe_name) return "请填写主程序 EXE 名，必须带 .exe 后缀，例如：TIKTOK点赞系统-3.19 D版本.exe";
  if(!/\.exe$/i.test(cfg.exe_name)) return "EXE名必须带 .exe 后缀，例如：TIKTOK点赞系统-3.19 D版本.exe";
  return "";
}
async function updatePackageSelected(){
  const list = [...selected];
  if(list.length===0){ await centerAlert("请先勾选设备"); return; }
  const cfg = getPackageConfig();
  const err = validatePackageConfig(cfg);
  if(err){ await centerAlert(err); return; }
  if(!await centerConfirm(`确定给选中的 ${list.length} 台设备远程更新软件包？\n\n默认解压到客户端桌面。`)) return;
  try{
    for(const code of list){
      await api(`/api/devices/${encodeURIComponent(code)}/command`,{
        method:"POST",
        body:JSON.stringify({command:"update_package", value:JSON.stringify(cfg)})
      });
    }
    await centerAlert("已下发远程更新包命令");
    setTimeout(loadDevices, 1000);
  }catch(e){ await centerAlert("下发失败：" + e.message); }
}
async function updatePackageAll(){
  const cfg = getPackageConfig();
  const err = validatePackageConfig(cfg);
  if(err){ await centerAlert(err); return; }
  if(!await centerConfirm("确定给全部在线设备远程更新软件包？\n\n默认解压到客户端桌面。")) return;
  try{
    const data = await api("/api/commands/all",{
      method:"POST",
      body:JSON.stringify({command:"update_package", value:JSON.stringify(cfg)})
    });
    await centerAlert(`已下发远程更新包命令，数量：${data.count||0}`);
    setTimeout(loadDevices, 1000);
  }catch(e){ await centerAlert("下发失败：" + e.message); }
}



function formatExpireDateText(value){
  if(!value) return "永久";
  try{
    const d = new Date(value);
    if(isNaN(d.getTime())) return String(value).replace("T"," ").slice(0,19);
    const pad = n => String(n).padStart(2,"0");
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }catch(e){
    return String(value || "");
  }
}
function expireRemainText(value){
  if(!value) return "";
  try{
    const d = new Date(value);
    if(isNaN(d.getTime())) return "";
    const diff = d.getTime() - Date.now();
    if(diff <= 0) return "已过期";
    const days = Math.ceil(diff / 86400000);
    return `剩余${days}天`;
  }catch(e){ return ""; }
}
async function loadUserExpireTitle(){
  const el = document.getElementById("userExpireTitle");
  if(!el) return;
  try{
    const me = await api("/api/me");
    if(me.is_admin){
      el.textContent = "ADMIN 管理员";
      el.className = "user-expire-title admin";
      return;
    }
    const user = me.user || {};
    const exp = user.user_expires_at || user.expires_at || "";
    const remain = expireRemainText(exp);
    el.textContent = `用户：${user.username || ""}　到期：${formatExpireDateText(exp)}${remain ? "　" + remain : ""}`;
    el.className = "user-expire-title";
    if(remain.includes("已过期")) el.classList.add("expired");
    else{
      try{
        const days = Math.ceil((new Date(exp).getTime() - Date.now()) / 86400000);
        if(days <= 7) el.classList.add("expiring");
      }catch(e){}
    }
  }catch(e){
    el.textContent = "";
  }
}

loadOfflineCleaner();
loadUserExpireTitle();
loadDevices();
setInterval(loadDevices,5000);
</script>
</body>
</html>
"""


LOGIN_HTML = r"""
<html><head><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>TikTok 集群控制台登录</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,'Microsoft YaHei',sans-serif;padding:24px;background:#f4f6fa;color:#111827}
.box{max-width:420px;margin:60px auto;background:#fff;border-radius:18px;padding:22px;box-shadow:0 8px 28px rgba(15,23,42,.12)}
h2{margin:0 0 18px;font-size:22px;font-weight:950}
input,button{font-size:17px;padding:13px;border-radius:12px;margin-top:12px;width:100%;box-sizing:border-box}
input{border:1px solid #d0d5dd}
button{border:0;background:#1d9bf0;color:#fff;font-weight:900}
.link-btn{background:#eef2f7;color:#1d9bf0}
.change-box{display:none;margin-top:8px;padding-top:8px;border-top:1px solid #e5e7eb}
.change-box.show{display:block}
.err{display:none;margin-top:12px;color:#d92d20;font-size:14px;font-weight:800}
</style></head>
<body><div class='box'>
<h2>TikTok 集群控制台</h2>
<input id='username' placeholder='用户名' autocomplete='username'>
<input id='password' placeholder='密码' type='password' autocomplete='current-password'>
<button onclick='goPasswordLogin()'>登录</button>
<button class='link-btn' onclick='toggleChangePassword()' type='button'>修改密码</button>
<div id='changeBox' class='change-box'>
  <input id='changeUsername' placeholder='用户名' autocomplete='username'>
  <input id='oldPassword' placeholder='当前密码' type='password' autocomplete='current-password'>
  <input id='newPassword' placeholder='新密码' type='password' autocomplete='new-password'>
  <input id='repeatPassword' placeholder='确认新密码' type='password' autocomplete='new-password'>
  <button onclick='goChangePassword()' type='button'>确认修改</button>
</div>
<div id='err' class='err'></div>
</div>
<script>
function showErr(msg){
  const err = document.getElementById('err');
  err.textContent = msg || '登录失败';
  err.style.display='block';
}
async function goPasswordLogin(){
  const username = document.getElementById('username').value.trim();
  const password = document.getElementById('password').value;
  document.getElementById('err').style.display='none';
  if(!username || !password){ showErr('请输入用户名和密码'); return; }
  try{
    const r = await fetch('/api/login-password', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username,password})
    });
    const data = await r.json();
    if(!r.ok || !data.ok) throw new Error(data.detail || '登录失败');
    localStorage.removeItem('ADMIN_KEY');
    if(data.api_key) localStorage.setItem('USER_API_KEY', data.api_key);
    location.href = data.url;
  }catch(e){ showErr(e.message); }
}
function toggleChangePassword(){
  document.getElementById('err').style.display='none';
  document.getElementById('changeBox').classList.toggle('show');
}
async function goChangePassword(){
  const username = document.getElementById('changeUsername').value.trim();
  const old_password = document.getElementById('oldPassword').value;
  const new_password = document.getElementById('newPassword').value;
  const repeat = document.getElementById('repeatPassword').value;
  document.getElementById('err').style.display='none';
  if(!username || !old_password || !new_password){ showErr('请填写用户名、当前密码和新密码'); return; }
  if(new_password !== repeat){ showErr('两次新密码不一致'); return; }
  try{
    const r = await fetch('/api/change-password-login', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username, old_password, new_password})
    });
    const data = await r.json();
    if(!r.ok || !data.ok) throw new Error(data.detail || '修改失败');
    document.getElementById('oldPassword').value = '';
    document.getElementById('newPassword').value = '';
    document.getElementById('repeatPassword').value = '';
    showErr('密码已修改，请用新密码登录');
  }catch(e){ showErr(e.message); }
}
document.getElementById('password').addEventListener('keydown', function(e){
  if(e.key === 'Enter') goPasswordLogin();
});
</script></body></html>
"""



@app.get("/login")
def unified_login(request: Request, token: Optional[str] = ""):
    """
    V6.1：统一登录入口。
    输入 ADMIN_KEY 自动进管理员；输入用户 API Key 自动进普通用户控制台。
    """
    raw = str(token or "").strip()
    if not raw:
        return HTMLResponse(LOGIN_HTML, status_code=401)

    if admin_auth_ok(request, raw):
        return {"ok": True, "role": "admin", "url": f"/tiktok?key={raw}&v=63"}

    try:
        row = get_user_by_api_key(raw)
        check_user_active(row)
        return {"ok": True, "role": "user", "url": f"/tiktok?api_key={raw}&v=63"}
    except Exception:
        raise HTTPException(status_code=401, detail="登录失败，密码或密钥不正确")


@app.get("/tiktok", response_class=HTMLResponse)
def mobile_admin(request: Request, key: Optional[str] = None, api_key: Optional[str] = None):
    """
    V5.2：页面层也必须鉴权。
    - ADMIN：/tiktok?key=ADMIN_KEY
    - 普通用户：/tiktok?api_key=用户API密钥
    - 不带 key/api_key 或错误时，只显示登录页，不返回控制台 HTML。
    """
    is_admin = admin_auth_ok(request, key)
    user_row = None
    if api_key:
        try:
            user_row = get_user_by_api_key(api_key)
            check_user_active(user_row)
        except Exception:
            user_row = None

    if not is_admin and not user_row:
        return HTMLResponse(LOGIN_HTML, status_code=401)
        return HTMLResponse("""
        <html><head><meta name='viewport' content='width=device-width,initial-scale=1'>
        <title>TikTok 集群控制台登录</title>
        <style>
        body{font-family:-apple-system,BlinkMacSystemFont,'Microsoft YaHei',sans-serif;padding:24px;background:#f4f6fa;color:#111827}
        .box{max-width:420px;margin:60px auto;background:#fff;border-radius:18px;padding:22px;box-shadow:0 8px 28px rgba(15,23,42,.12)}
        h2{margin:0 0 18px;font-size:22px;font-weight:950}
        input,button{font-size:17px;padding:13px;border-radius:12px;margin-top:12px;width:100%;box-sizing:border-box}
        input{border:1px solid #d0d5dd}
        button{border:0;background:#1d9bf0;color:#fff;font-weight:900}
        .err{display:none;margin-top:12px;color:#d92d20;font-size:14px;font-weight:800}
        </style></head>
        <body><div class='box'>
        <h2>TikTok 集群控制台</h2>
        <input id='token' placeholder='请输入登录密钥' type='password' autocomplete='off'>
        <button onclick='goLogin()'>登录</button>
        <div id='err' class='err'></div>
        </div>
        <script>
        async function goLogin(){
          const token = document.getElementById('token').value.trim();
          const err = document.getElementById('err');
          err.style.display='none';
          if(!token){ err.textContent='请输入登录密钥'; err.style.display='block'; return; }
          try{
            const r = await fetch('/login?token=' + encodeURIComponent(token));
            const data = await r.json();
            if(!r.ok || !data.ok) throw new Error(data.detail || '登录失败');
            location.href = data.url;
          }catch(e){
            err.textContent = e.message || '登录失败';
            err.style.display='block';
          }
        }
        document.getElementById('token').addEventListener('keydown', function(e){
          if(e.key === 'Enter') goLogin();
        });
        </script></body></html>
        """, status_code=401)

    return HTMLResponse(MOBILE_ADMIN_HTML)


@app.get("/api/devices/{machine_code}/screenshot/image")
def get_screenshot_image(machine_code: str, request: Request, key: Optional[str] = None, api_key: Optional[str] = None):
    machine_code = normalize_machine_code(machine_code)
    verify_device_access(request, machine_code, key, api_key)
    shot = screenshots.get(machine_code)
    if not shot:
        raise HTTPException(status_code=404, detail="screenshot not found")
    try:
        img = base64.b64decode(shot["image_base64"])
    except Exception:
        raise HTTPException(status_code=500, detail="bad image")
    from fastapi.responses import Response
    return Response(content=img, media_type="image/jpeg")
