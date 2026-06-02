from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from typing import Optional, Dict, List
import time
import uuid
import os
import base64
from datetime import datetime
from fastapi.responses import HTMLResponse

app = FastAPI(title="TikTok Cluster Control Server Web Admin V2.9")

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



def extract_work_time_from_log_text(text: str):
    """
    Web V2.9：从客户端上传/保存的运行日志中解析最近一次工作时间。
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
        if isinstance(logs, list):
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
    return {"ok": True, "msg": "TikTok cluster server web admin v2.1 is running", "admin": "/admin"}

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
        item["online"] = now - item.get("last_seen", 0) <= 120
        item["last_seen_ago"] = int(now - item.get("last_seen", 0))
        if not item["online"]:
            item["display_state"] = "offline"
        elif item.get("status") == "switching_ip":
            item["display_state"] = "switching_ip"
        elif item.get("status") == "screenshotting":
            # V36：如果已经收到截图，就不要一直显示截图中
            item["display_state"] = "online" if screenshots.get(item["machine_code"]) else "screenshotting"
        elif item.get("status") in ("starting_app", "restarting_app"):
            item["display_state"] = "online"
        else:
            item["display_state"] = "online"
        shot = screenshots.get(item["machine_code"])
        item["has_screenshot"] = bool(shot)
        item["screenshot_time"] = shot.get("created_at") if shot else None
        item["work_time"] = get_device_work_time(machine_code, item)
        result.append(item)
    result.sort(
        key=lambda x: (
            x.get("daily_seq", 999999),
            not x.get("online", False),
            x.get("device_name", "")
        )
    )
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
        if now - d.get("last_seen", 0) > 120:
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
    # V32：截图上传成功后恢复在线状态，避免控制台一直显示“截图中”
    try:
        devices[machine_code]["status"] = "online"
        devices[machine_code]["last_seen"] = time.time()
    except Exception:
        pass
    # V33：截图上传成功后恢复状态，避免控制台一直显示“截图中”
    try:
        devices[machine_code]["status"] = "online"
        devices[machine_code]["last_seen"] = time.time()
    except Exception:
        pass
    # V36：截图上传成功后恢复 online
    try:
        devices[machine_code]["status"] = "online"
        devices[machine_code]["last_seen"] = time.time()
    except Exception:
        pass
    return {"ok": True, "filename": filename if filepath else "", "filepath": filepath}

@app.get("/api/devices/{machine_code}/screenshot")
def get_screenshot(machine_code: str):
    shot = screenshots.get(machine_code)
    if not shot:
        raise HTTPException(status_code=404, detail="screenshot not found")
    return {"ok": True, "screenshot": shot}



@app.delete("/api/devices/{machine_code}")
def delete_device(machine_code: str):
    """V32：删除客户端记录、待执行命令、截图、配置和日志缓存。"""
    removed = machine_code in devices
    devices.pop(machine_code, None)
    commands.pop(machine_code, None)
    screenshots.pop(machine_code, None)
    configs.pop(machine_code, None)
    logs_store.pop(machine_code, None)
    try:
        global daily_seq_map
        daily_seq_map.pop(machine_code, None)
    except Exception:
        pass
    return {"ok": True, "removed": removed, "machine_code": machine_code}


@app.get("/api/version")
def version():
    return {
        "ok": True,
        "version": "v26-web-v2.9",
        "features": ["heartbeat", "ip_location", "commands", "daily_sequence", "screenshot_upload", "screenshot_file_save", "online_timeout_120s", "mobile_admin_v2_9"]
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
        if now - d.get("last_seen", 0) > 120:
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


MOBILE_ADMIN_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>TikTok 集群控制台 Web V2.9</title>
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
.stats{margin-top:9px;color:var(--muted);font-size:14px}
.wrap{padding:12px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.all-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.multi-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.multi-grid .wide{grid-column:span 2}

.btn{border:0;border-radius:13px;color:#fff;font-size:15px;font-weight:850;padding:12px 8px;min-height:44px}
.btn.blue{background:var(--blue)}.btn.green{background:var(--green)}.btn.red{background:var(--red)}.btn.orange{background:var(--orange)}.btn.dark{background:var(--dark)}
.btn.gray{background:#e5e7eb;color:#111827}
.multi{margin-top:10px;padding-top:10px;border-top:1px solid #e5e7eb}
.card{position:relative;background:var(--card);border-radius:15px;margin:12px 0;padding:14px 12px;border-top:5px solid var(--blue);box-shadow:0 1px 4px rgba(0,0,0,.08)}
.card.offline{border-top-color:var(--red);opacity:.72}.card.bad{background:#fff0f0;border-top-color:var(--red)}
.card.busy{border-top-color:var(--orange)}.card.running{border-top-color:var(--green)}
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
.sync-title{font-size:13px;font-weight:900;color:#111827;margin-bottom:6px}
.sync-row{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;align-items:center;margin-top:6px}
.sync-row label{font-size:12px;color:#111827;font-weight:700;display:flex;align-items:center;gap:4px;min-width:0}
.sync-row input[type="number"]{width:58px;min-width:48px;border:1px solid #d0d5dd;border-radius:8px;padding:7px 5px;font-size:12px}
.sync-row input[type="checkbox"],.sync-row input[type="radio"]{width:16px;height:16px;accent-color:#1d9bf0}
.sync-btn{border:0;border-radius:9px;padding:9px 6px;font-size:12px;font-weight:850;color:#fff;background:#465465;min-width:0}
.sync-btn.primary{background:#1d9bf0}
.sync-btn.green{background:#1db954}
@media (min-width:900px){
  body{padding-bottom:170px}
  .syncbar{padding:8px 14px}
  .sync-row{grid-template-columns:repeat(8,minmax(0,1fr))}

  body{padding-bottom:170px}
  .syncbar{position:fixed;left:0;right:0;bottom:0;z-index:30;background:#fff;border-top:1px solid #d0d5dd;box-shadow:0 -2px 10px rgba(15,23,42,.12);padding:8px 10px calc(8px + env(safe-area-inset-bottom))}
}

.small{font-size:13px;color:#667085}
@media (min-width:900px){.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.card{margin:0}.actions{grid-template-columns:repeat(2,1fr)}.action-side-shot{max-width:104px}.action-side-shot .thumb{width:100px;max-height:72px}}
</style>
</head>
<body>
<div class="header">
  <div class="title-row">
    <h1>TikTok 集群控制台</h1><span class="ver">Web V2.9</span>
    <button class="refresh-btn" onclick="loadDevices()">刷新</button>
  </div>
  <input id="serverBox" class="server" readonly>
  <div class="stats" id="stats">加载中...</div>
</div>

<div class="wrap">
  <div class="all-grid">
    <button class="btn blue" onclick="sendAll('open_target')">全部打开软件</button>
    <button class="btn blue" onclick="sendAll('start_target')">全部启动软件</button>
    <button class="btn orange" onclick="sendAll('restart_app_only')">全部重启软件</button>

    <button class="btn green" onclick="sendAll('start_monitor')">全部打开监控</button>
    <button class="btn red" onclick="sendAll('stop_monitor')">全部停止监控</button>
    <button class="btn orange" onclick="sendAll('restart_app_start')">全部重启并启动</button>

    <button class="btn dark" onclick="batchScreenshotAll()">全部批量截图</button>
    <button class="btn dark" onclick="sendAll('update_github_config')">全部更新GitHub</button>
    <button class="btn gray" onclick="loadDevices()">刷新状态</button>
  </div>

  <div class="multi-grid multi">
    <button class="btn gray wide" onclick="selectOnline()">多选在线</button>
    <button class="btn gray wide" onclick="clearSelected()">取消选择</button>

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
  <div class="footer">Safari 可添加到主屏幕，当作手机 App 使用</div>
</div>


<div class="syncbar">
  <div class="sync-title">集群参数同步</div>
  <div class="sync-row">
    <label>切IP <input id="sync_cut_ip" type="number" value="5"></label>
    <label>网络 <span><input name="sync_network" type="radio" value="4G">4G <input name="sync_network" type="radio" value="5G" checked>5G</span></label>
    <label>蓝色不变 <input id="sync_blue_no_change_auto_ip" type="number" value="210"></label>
    <label>OCR间隔 <input id="sync_ocr_interval" type="number" value="30"></label>
  </div>
  <div class="sync-row">
    <label><input id="sync_no_restart_when_duration_ok" type="checkbox" checked>时长不走重启</label>
    <label><input id="sync_restart_then_start" type="checkbox" checked>重启后启动</label>
    <label>重启迟开 <input id="sync_restart_open_delay" type="number" value="2"></label>
    <label>点启动 <input id="sync_start_clicks" type="number" value="6"></label>
  </div>
  <div class="sync-row">
    <button class="sync-btn primary" onclick="syncConfigSelected()">保存并同步选中</button>
    <button class="sync-btn green" onclick="syncConfigAll()">保存并同步全部</button>
    <button class="sync-btn" onclick="selectOnline()">多选在线</button>
    <button class="sync-btn" onclick="clearSelected()">取消选择</button>
  </div>
</div>

<div id="imgModal"<div id="imgModal" class="modal" onclick="closeModal()">
  <button class="close" onclick="closeModal()">×</button>
  <img id="modalImg">
</div>

<script>
const SERVER = location.origin;
const params = new URLSearchParams(location.search);
const keyFromUrl = params.get("key") || "";
if (keyFromUrl) localStorage.setItem("ADMIN_KEY", keyFromUrl);
const ADMIN_KEY = keyFromUrl || localStorage.getItem("ADMIN_KEY") || "";
let DEVICES = [];
let selected = new Set();

document.getElementById("serverBox").value = SERVER;

function headers(){
  return {"Content-Type":"application/json","X-Admin-Key":ADMIN_KEY};
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
  if(isBadCarrier(d)) return "bad";
  if(!d.online) return "offline";
  if(d.running) return "running";
  if(["screenshotting","switching_ip"].includes(d.display_state||d.status)) return "busy";
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
  return "在线";
}
function render(){
  const online = DEVICES.filter(d=>d.online).length;
  const running = DEVICES.filter(d=>d.running).length;
  document.getElementById("stats").textContent = `设备：${DEVICES.length}，在线：${online}，监控：${running}，刷新时间：${new Date().toLocaleTimeString()}`;
  const cards = document.getElementById("cards");
  cards.innerHTML = "";
  for(const d of DEVICES){
    const code = d.machine_code || "";
    const seq = d.daily_seq || "";
    const card = document.createElement("div");
    card.className = "card " + stateClass(d);
    const checked = selected.has(code) ? "checked" : "";
    const loc = d.location_carrier || `${d.location||""}${d.carrier||""}` || "-";
    const bad = isBadCarrier(d);
    const thumb = d.has_screenshot ? `<div class="action-side-shot"><img class="thumb" onclick="event.stopPropagation();showShot('${code}')" src="/api/devices/${encodeURIComponent(code)}/screenshot/image?key=${encodeURIComponent(ADMIN_KEY)}&t=${d.screenshot_time||0}"><div>点击放大</div></div>` : `<div class="action-side-shot action-side-empty">缩略图<br>暂无</div>`;
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
        <b>监控</b>：${d.running?"是":"否"}　
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
        <button class="btn orange" onclick="sendOne('${code}','restart_app_start')">重启后启动</button>
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
  DEVICES.filter(d=>d.online).forEach(d=>selected.add(d.machine_code));
  render();
}
function clearSelected(){ selected.clear(); render(); }
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
  for(const code of list){
    await sendOne(code,cmd,cmd==="screenshot");
  }
}
async function screenshotSelected(){ await sendSelected("screenshot"); }
async function batchScreenshotAll(){ await sendAll("screenshot"); setTimeout(loadDevices, 4500); setTimeout(loadDevices, 8500); setTimeout(loadDevices, 12500); }

async function renameSelected(){
  const list = [...selected];
  if(list.length===0){ alert("请先勾选设备"); return; }
  if(list.length===1){ return renameOne(list[0]); }
  const base = prompt(`已选择 ${list.length} 台设备，输入批量基础名称：`);
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
  const name = prompt("输入新设备名称：", d.device_name || "");
  if(!name) return;
  try{
    await api(`/api/devices/${encodeURIComponent(code)}/command`,{method:"POST",body:JSON.stringify({command:"rename",value:name})});
    setTimeout(loadDevices,1000);
    // V2.9：改名后延迟3秒自动截图回传，方便确认改名是否成功
    setTimeout(()=>sendOne(code,'screenshot', true),3000);
    setTimeout(loadDevices,7000);
    setTimeout(loadDevices,11000);
  }catch(e){ alert("改名失败：" + e.message); }
}
function showShot(code){
  const img = document.getElementById("modalImg");
  img.src = `/api/devices/${encodeURIComponent(code)}/screenshot/image?key=${encodeURIComponent(ADMIN_KEY)}&t=${d.screenshot_time||0}`;
  document.getElementById("imgModal").classList.add("show");
}
function closeModal(){
  document.getElementById("imgModal").classList.remove("show");
}

function getSyncConfig(){
  const networkEl = document.querySelector('input[name="sync_network"]:checked');
  return {
    cut_ip: Number(document.getElementById("sync_cut_ip").value || 5),
    network: networkEl ? networkEl.value : "5G",
    blue_no_change_auto_ip: Number(document.getElementById("sync_blue_no_change_auto_ip").value || 210),
    ocr_interval: Number(document.getElementById("sync_ocr_interval").value || 30),
    no_restart_when_duration_ok: document.getElementById("sync_no_restart_when_duration_ok").checked,
    restart_then_start: document.getElementById("sync_restart_then_start").checked,
    restart_open_delay: Number(document.getElementById("sync_restart_open_delay").value || 2),
    start_clicks: Number(document.getElementById("sync_start_clicks").value || 6)
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
  if(list.length===0){ alert("请先勾选设备"); return; }
  const cfg = getSyncConfig();
  if(!confirm(`确定同步参数到选中的 ${list.length} 台设备？`)) return;
  try{
    for(const code of list){ await syncConfigOne(code, cfg); }
    alert("已同步选中设备");
    setTimeout(loadDevices,1000);
  }catch(e){ alert("同步失败：" + e.message); }
}
async function syncConfigAll(){
  const cfg = getSyncConfig();
  if(!confirm("确定同步参数到全部在线设备？")) return;
  try{
    const data = await api("/api/config/all",{method:"POST",body:JSON.stringify({config:cfg})});
    alert(`已同步全部在线设备，数量：${data.count||0}`);
    setTimeout(loadDevices,1000);
  }catch(e){ alert("同步失败：" + e.message); }
}

loadDevices();
setInterval(loadDevices,5000);
</script>
</body>
</html>
"""

def admin_auth_ok(request: Request, key: Optional[str] = None):
    expected = os.environ.get("ADMIN_KEY", "").strip()
    if not expected:
        return True
    provided = (key or request.headers.get("X-Admin-Key") or "").strip()
    return provided == expected

@app.get("/admin", response_class=HTMLResponse)
def mobile_admin(request: Request, key: Optional[str] = None):
    if not admin_auth_ok(request, key):
        return HTMLResponse("""
        <html><head><meta name='viewport' content='width=device-width,initial-scale=1'>
        <style>body{font-family:-apple-system,BlinkMacSystemFont,'Microsoft YaHei',sans-serif;padding:24px}input,button{font-size:18px;padding:12px;border-radius:10px;margin-top:10px;width:100%}</style></head>
        <body><h2>请输入控制台密码</h2><input id='k' placeholder='ADMIN_KEY' type='password'><button onclick='go()'>进入</button>
        <script>function go(){location.href='/admin?key='+encodeURIComponent(document.getElementById('k').value)}</script></body></html>
        """, status_code=401)
    return HTMLResponse(MOBILE_ADMIN_HTML)

@app.get("/api/devices/{machine_code}/screenshot/image")
def get_screenshot_image(machine_code: str, request: Request, key: Optional[str] = None):
    if not admin_auth_ok(request, key):
        raise HTTPException(status_code=401, detail="bad admin key")
    shot = screenshots.get(machine_code)
    if not shot:
        raise HTTPException(status_code=404, detail="screenshot not found")
    try:
        img = base64.b64decode(shot["image_base64"])
    except Exception:
        raise HTTPException(status_code=500, detail="bad image")
    from fastapi.responses import Response
    return Response(content=img, media_type="image/jpeg")

