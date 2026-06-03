from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from typing import Optional, Dict, List
import time
import uuid
import os
import base64
from datetime import datetime
from fastapi.responses import HTMLResponse

app = FastAPI(title="TikTok Cluster Control Server Web Admin V4.7")

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
    Web V4.7：桌面端控制区改为两行按钮，底部参数同步改为单行显示（仅桌面端）。
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
            item["running"] = False
        if not item["online"]:
            item["display_state"] = "offline"
        elif item.get("status") == "switching_ip":
            item["display_state"] = "switching_ip"
        elif item.get("status") == "screenshotting":
            # V36：如果已经收到截图，就不要一直显示截图中
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
        "version": "v26-web-v4.7",
        "features": ["heartbeat", "ip_location", "commands", "daily_sequence", "screenshot_upload", "screenshot_file_save", "online_timeout_120s", "mobile_admin_v4_7"]
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
<title>TikTok 集群控制台 Web V4.7</title>
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

</style>
</head>
<body>
<div class="header">
  <div class="title-row">
    <h1>TikTok 集群控制台</h1>
  </div>
  <div class="header-status-row">
    <div class="stats" id="stats">加载中...</div>
    <div class="header-right-tools">
      <div class="offline-cleaner">
        <label><input id="autoHideOffline" type="checkbox" onchange="saveOfflineCleaner(); render()">自动隐藏离线</label>
        <label><span class="offline-over-text">离线超过</span> <input id="offlineHideMinutes" type="number" value="30" min="1" onchange="saveOfflineCleaner(); render()"> 分钟</label>
      </div>
      <button class="mobile-header-toggle mobile-only" id="mobileControlsToggle" onclick="toggleMobileControls()">⬇️ 展开</button>
      <button class="refresh-btn" onclick="loadDevices()">刷新</button>
    </div>
  </div>
</div>

<div class="wrap">
  <div id="mobileAllControls" class="all-grid mobile-only mobile-control-section collapsed">
    <button class="btn blue" onclick="sendAll('open_target')">全部打开软件</button>
    <button class="btn blue" onclick="sendAll('start_target')">全部启动软件</button>
    <button class="btn orange" onclick="sendAll('restart_app_only')">全部重启软件</button>

    <button class="btn green" onclick="sendAll('start_monitor')">全部打开监控</button>
    <button class="btn red" onclick="sendAll('stop_monitor')">全部停止监控</button>
    <button class="btn orange" onclick="sendAll('restart_app_start')">全部重启并启动</button>

    <button class="btn dark" onclick="batchScreenshotAll()">全部批量截图</button>
    <button class="btn dark" onclick="sendAll('update_github_config')">全部更新GitHub</button>
  </div>

  <div class="desktop-top-grid">
    <button class="btn blue" onclick="sendAll('open_target')">全部打开软件</button>
    <button class="btn blue" onclick="sendAll('start_target')">全部启动软件</button>
    <button class="btn green" onclick="sendAll('start_monitor')">全部打开监控</button>
    <button class="btn red" onclick="sendAll('stop_monitor')">全部停止监控</button>
    <button class="btn dark" onclick="batchScreenshotAll()">全部批量截图</button>
    <button class="btn orange" onclick="sendAll('restart_app_only')">全部重启软件</button>
    <button class="btn orange" onclick="sendAll('restart_app_start')">全部重启并启动</button>
    <button class="btn dark" onclick="sendAll('update_github_config')">全部更新GitHub</button>
  </div>

  <div id="mobileSelectedControls" class="multi-grid multi mobile-only mobile-control-section collapsed">
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

  <div class="desktop-action-grid">
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

<div id="imgModal" class="modal" onclick="closeModal()">
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
function render(){
  const visibleDevices = DEVICES.filter(d=>!shouldHideOfflineDevice(d));
  const online = visibleDevices.filter(d=>d.online).length;
  const running = visibleDevices.filter(d=>d.online && d.running).length;
  const hiddenCount = DEVICES.length - visibleDevices.length;
  const deviceText = hiddenCount > 0 ? `${visibleDevices.length}/${DEVICES.length}` : `${visibleDevices.length}`;
  document.getElementById("stats").textContent = `设备：${deviceText}，在线：${online}，监控：${running}，刷新时间：${new Date().toLocaleTimeString()}`;
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
  img.src = `/api/devices/${encodeURIComponent(code)}/screenshot/image?key=${encodeURIComponent(ADMIN_KEY)}&t=${t}`;
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


loadOfflineCleaner();
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

