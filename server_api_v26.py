from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, List
import time
import uuid
import os
import base64
from datetime import datetime

app = FastAPI(title="TikTok Cluster Control Server V26")

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
    return {"ok": True, "msg": "TikTok cluster server v26 is running"}

@app.post("/api/heartbeat")
def heartbeat(data: Heartbeat):
    now = time.time()
    old = devices.get(data.machine_code, {})

    location_carrier = data.location_carrier
    if not location_carrier:
        location_carrier = f"{data.location or ''}{data.carrier or ''}".strip()

    daily_seq = assign_daily_seq(data.machine_code)

    devices[data.machine_code] = {
        **old,
        "daily_seq": daily_seq,
        "daily_seq_date": daily_seq_date,
        "machine_code": data.machine_code,
        "device_name": (
            old.get("device_name", "")
            if str(data.device_name or "").strip() in ("", "1", "未知设备", "未识别设备名") and old.get("device_name")
            else data.device_name
        ),
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
    commands.setdefault(data.machine_code, [])
    return {"ok": True, "server_time": now}

@app.get("/api/devices")
def list_devices():
    now = time.time()
    result = []
    for d in devices.values():
        item = dict(d)
        item["online"] = now - item.get("last_seen", 0) <= 15
        item["last_seen_ago"] = int(now - item.get("last_seen", 0))
        if not item["online"]:
            item["display_state"] = "offline"
        elif item.get("status") == "switching_ip":
            item["display_state"] = "switching_ip"
        elif item.get("status") in ("starting_app", "restarting_app", "screenshotting"):
            item["display_state"] = item.get("status")
        else:
            item["display_state"] = "online"
        shot = screenshots.get(item["machine_code"])
        item["has_screenshot"] = bool(shot)
        item["screenshot_time"] = shot.get("created_at") if shot else None
        result.append(item)
    result.sort(key=lambda x: (x.get("daily_seq", 999999), not x.get("online", False), x.get("device_name", "")))))
    return {"ok": True, "devices": result}

@app.post("/api/devices/{machine_code}/command")
def send_command(machine_code: str, cmd: CommandIn):
    if machine_code not in devices:
        raise HTTPException(status_code=404, detail="device not found")
    item = {
        "id": str(uuid.uuid4()),
        "command": cmd.command,
        "value": cmd.value,
        "created_at": time.time(),
    }
    commands.setdefault(machine_code, []).append(item)

    # V26：远程改名命令下发时，服务端先把列表里的设备名改掉；
    # 后续如果客户端心跳误传“1”，heartbeat 也会保留这个有效名称。
    if str(cmd.command).strip() == "rename" and cmd.value:
        devices[machine_code]["device_name"] = str(cmd.value).strip()

    return {"ok": True, "queued": item}

@app.post("/api/commands/all")
def send_all(cmd: CommandIn):
    # V26：全部按钮只下发给在线客户端；离线设备保留显示，但不下发命令
    count = 0
    now = time.time()
    for machine_code, d in devices.items():
        if now - d.get("last_seen", 0) > 15:
            continue
        item = {
            "id": str(uuid.uuid4()),
            "command": cmd.command,
            "value": cmd.value,
            "created_at": time.time(),
        }
        commands.setdefault(machine_code, []).append(item)
        count += 1
    return {"ok": True, "count": count}

@app.get("/api/devices/{machine_code}/commands")
def pull_commands(machine_code: str):
    pending = commands.get(machine_code, [])
    commands[machine_code] = []
    return {"ok": True, "commands": pending}

@app.post("/api/devices/{machine_code}/screenshot")
def upload_screenshot(machine_code: str, shot: ScreenshotIn):
    if machine_code not in devices:
        devices[machine_code] = {
            "machine_code": machine_code,
            "device_name": machine_code[:8],
            "status": "online",
            "running": False,
            "last_seen": time.time(),
        }

    # V10：截图除了保存在内存供控制台显示，也保存到服务端 screenshots 文件夹。
    safe_code = "".join(c for c in machine_code if c.isalnum() or c in ("-", "_"))[:80]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{safe_code}_{ts}.jpg"
    filepath = os.path.join(SCREENSHOT_DIR, filename)

    try:
        with open(filepath, "wb") as f:
            f.write(base64.b64decode(shot.image_base64))
    except Exception:
        filepath = ""

    screenshots[machine_code] = {
        "machine_code": machine_code,
        "image_base64": shot.image_base64,
        "width": shot.width,
        "height": shot.height,
        "created_at": time.time(),
        "filename": filename if filepath else "",
        "filepath": filepath,
    }
    return {"ok": True, "filename": filename if filepath else "", "filepath": filepath}

@app.get("/api/devices/{machine_code}/screenshot")
def get_screenshot(machine_code: str):
    shot = screenshots.get(machine_code)
    if not shot:
        raise HTTPException(status_code=404, detail="screenshot not found")
    return {"ok": True, "screenshot": shot}



@app.delete("/api/devices/{machine_code}")
def delete_device(machine_code: str):
    """删除客户端记录：设备状态、命令、截图、日志缓存。客户端程序本身不会被关闭。"""
    removed = False
    if machine_code in devices:
        devices.pop(machine_code, None)
        removed = True
    commands.pop(machine_code, None)
    screenshots.pop(machine_code, None)
    logs.pop(machine_code, None)

    # daily sequence 记录只删除该机器当天绑定，避免删除后旧机器继续占位
    try:
        today = current_day()
        if daily_sequences.get(today, {}).get(machine_code) is not None:
            daily_sequences[today].pop(machine_code, None)
    except Exception:
        pass

    return {"ok": True, "removed": removed, "machine_code": machine_code}


@app.get("/api/version")
def version():
    return {
        "ok": True,
        "version": "v26",
        "features": ["heartbeat", "ip_location", "commands", "daily_sequence", "screenshot_upload", "screenshot_file_save"]
    }

@app.get("/api/debug/devices")
def debug_devices():
    return {"ok": True, "devices": devices, "screenshots": list(screenshots.keys())}


@app.post("/api/devices/{machine_code}/config")
def set_device_config(machine_code: str, data: ConfigIn):
    if machine_code not in devices:
        raise HTTPException(status_code=404, detail="device not found")
    configs[machine_code] = {
        "config": data.config,
        "updated_at": time.time(),
    }
    item = {
        "id": str(uuid.uuid4()),
        "command": "update_config",
        "value": data.config,
        "created_at": time.time(),
    }
    commands.setdefault(machine_code, []).append(item)
    return {"ok": True, "config": data.config}

@app.post("/api/config/all")
def set_all_config(data: ConfigIn):
    # V26：全部同步配置只同步在线客户端
    count = 0
    now = time.time()
    for machine_code, d in devices.items():
        if now - d.get("last_seen", 0) > 15:
            continue
        configs[machine_code] = {
            "config": data.config,
            "updated_at": time.time(),
        }
        item = {
            "id": str(uuid.uuid4()),
            "command": "update_config",
            "value": data.config,
            "created_at": time.time(),
        }
        commands.setdefault(machine_code, []).append(item)
        count += 1
    return {"ok": True, "count": count}

@app.post("/api/devices/{machine_code}/log")
def upload_log(machine_code: str, data: LogIn):
    logs_store[machine_code] = {
        "machine_code": machine_code,
        "text": data.text or "",
        "created_at": time.time(),
    }
    return {"ok": True}

@app.get("/api/devices/{machine_code}/log")
def get_log(machine_code: str):
    log = logs_store.get(machine_code)
    if not log:
        raise HTTPException(status_code=404, detail="log not found")
    return {"ok": True, "log": log}
