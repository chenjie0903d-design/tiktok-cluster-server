from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, List
import time
import uuid
import os
import base64
from datetime import datetime
from fastapi.responses import HTMLResponse

app = FastAPI(title="TikTok Cluster Control Server V39 WebAdmin")

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

# 网页控制台密码：可在 Railway Variables 里添加 ADMIN_KEY。
# 不设置 ADMIN_KEY 时，/admin 不需要密码；设置后访问 /admin?key=你的密码。
ADMIN_KEY = os.environ.get("ADMIN_KEY", "").strip()


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
    return {"ok": True, "msg": "TikTok cluster server v39 web admin is running"}

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



def check_admin_key(key: Optional[str] = None):
    """网页控制台简单密码校验。桌面控制台和客户端原有 /api 接口不受影响。"""
    if ADMIN_KEY and str(key or "").strip() != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="admin key required")
    return True


ADMIN_PAGE_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
<title>TikTok 集群手机控制台</title>
<style>
:root{--bg:#f5f7fb;--card:#fff;--text:#151922;--muted:#667085;--line:#e6e8ef;--blue:#159BFF;--red:#ff3030;--green:#20b455;--orange:#ff9f1a;--btn:#111827}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",Arial,sans-serif;font-size:15px}
header{position:sticky;top:0;z-index:20;background:rgba(255,255,255,.96);backdrop-filter:blur(10px);border-bottom:1px solid var(--line);padding:10px 12px}
.title{display:flex;align-items:center;justify-content:space-between;gap:8px;font-weight:800;font-size:18px}.server{margin-top:8px;display:flex;gap:6px}.server input{flex:1;border:1px solid var(--line);border-radius:10px;padding:9px 10px;font-size:13px;background:#fff}.summary{margin-top:7px;color:var(--muted);font-size:13px;line-height:1.35}
.toolbar{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;padding:10px 12px}button{border:0;border-radius:11px;background:var(--btn);color:#fff;font-weight:700;padding:10px 9px;font-size:14px;min-height:40px}button.secondary{background:#475467}button.blue{background:var(--blue)}button.green{background:var(--green)}button.orange{background:var(--orange)}button.red{background:var(--red)}button.light{background:#eef2f7;color:#111827}button:active{transform:scale(.98)}
main{padding:0 12px 24px}.card{background:var(--card);border:1px solid var(--line);border-radius:16px;box-shadow:0 1px 2px rgba(16,24,40,.04);margin:10px 0;overflow:hidden}.card.bad{border-color:var(--red);box-shadow:0 0 0 2px rgba(255,48,48,.12)}
.bar{height:4px;background:var(--blue)}.bar.offline{background:var(--red)}.bar.busy{background:var(--orange)}.bar.monitoring{background:var(--green)}.bar.bad{background:var(--red)}
.info{padding:12px}.row1{display:flex;align-items:center;justify-content:space-between;gap:10px}.name{font-size:17px;font-weight:900;line-height:1.25;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.seq{flex:0 0 auto;background:#111827;color:#fff;border-radius:999px;min-width:32px;height:32px;padding:0 9px;display:flex;align-items:center;justify-content:center;font-weight:900}
.meta{margin-top:8px;color:#344054;line-height:1.6;font-size:14px;word-break:break-all}.state{display:inline-block;padding:2px 8px;border-radius:999px;font-weight:800;color:#fff;background:var(--blue);font-size:12px;margin-right:6px}.state.offline{background:var(--red)}.state.busy{background:var(--orange)}.state.monitoring{background:var(--green)}
.warn{display:none;margin-top:7px;color:#fff;background:var(--red);border-radius:10px;padding:6px 8px;font-weight:800;font-size:13px}.card.bad .warn{display:block}
.actions{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px;padding:0 12px 12px}.actions button{min-height:36px;font-size:13px;padding:8px 5px;border-radius:10px}
.thumb{padding:0 12px 12px;display:flex;align-items:center;gap:8px}.thumb img{width:128px;max-height:82px;object-fit:contain;border:1px solid var(--line);border-radius:8px;background:#f2f4f7}.thumb span{color:var(--muted);font-size:13px}.empty{color:var(--muted);text-align:center;padding:40px 12px}.footer{color:var(--muted);text-align:center;padding:18px 8px;font-size:12px}
@media (min-width:820px){.toolbar{grid-template-columns:repeat(6,minmax(0,1fr))}main{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.card{margin:0}}
</style>
</head>
<body>
<header>
<div class="title"><span>TikTok 集群控制台</span><button class="light" onclick="loadDevices()">刷新</button></div>
<div class="server"><input id="serverBox" readonly /></div>
<div id="summary" class="summary">正在加载...</div>
</header>
<section class="toolbar">
<button class="green" onclick="sendAll('start_monitor')">全部启动监控</button>
<button class="red" onclick="sendAll('stop_monitor')">全部停止监控</button>
<button class="blue" onclick="sendAll('open_target')">全部打开软件</button>
<button class="blue" onclick="sendAll('start_target')">全部启动软件</button>
<button class="orange" onclick="sendAll('restart_app_only')">重启全部软件</button>
<button class="orange" onclick="sendAll('restart_app_start')">重启全部并启动</button>
<button class="secondary" onclick="sendAll('screenshot', true)">批量截图</button>
<button class="secondary" onclick="sendAll('update_github_config')">更新GitHub</button>
</section>
<main id="devices"></main>
<div class="footer">Safari 可添加到主屏幕，当作手机 App 使用</div>
<script>
const urlParams=new URLSearchParams(location.search);
const keyFromUrl=urlParams.get("key")||"";
if(keyFromUrl)localStorage.setItem("ADMIN_KEY",keyFromUrl);
const ADMIN_KEY=keyFromUrl||localStorage.getItem("ADMIN_KEY")||"";
document.getElementById("serverBox").value=location.origin;
function apiUrl(path){const sep=path.includes("?")?"&":"?";return path+sep+"key="+encodeURIComponent(ADMIN_KEY);}
function stateText(s){return {online:"在线",offline:"离线",switching_ip:"切IP中",screenshotting:"截图中",starting_app:"在线",restarting_app:"在线"}[s||"online"]||(s||"在线");}
function badCarrier(d){const txt=`${d.location_carrier||""} ${d.carrier||""}`;return txt.includes("电信")||txt.includes("广电");}
function stateClass(d){if(badCarrier(d))return"bad";if(!d.online||d.display_state==="offline")return"offline";if(d.running)return"monitoring";if(["switching_ip","screenshotting"].includes(d.display_state))return"busy";return"";}
function escapeHtml(s){return String(s??"").replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[m]));}
async function loadDevices(){
 const box=document.getElementById("devices"),summary=document.getElementById("summary");
 try{
  const res=await fetch(apiUrl("/admin/api/devices"),{cache:"no-store"});
  if(!res.ok)throw new Error(await res.text());
  const data=await res.json(),devices=data.devices||[],online=devices.filter(d=>d.online).length;
  summary.textContent=`设备：${devices.length}，在线：${online}，刷新时间：${new Date().toLocaleTimeString()}`;
  if(!devices.length){box.innerHTML=`<div class="empty">暂无客户端上线</div>`;return;}
  box.innerHTML=devices.map(d=>renderDevice(d)).join("");
  devices.forEach(d=>{if(d.has_screenshot)loadThumb(d.machine_code);});
 }catch(e){summary.textContent="加载失败："+e.message;box.innerHTML=`<div class="empty">加载失败，请检查服务端或密码</div>`;}
}
function renderDevice(d){
 const seq=d.daily_seq||"-",cls=stateClass(d),carrier=d.location_carrier||`${d.location||""}${d.carrier||""}`||"-",state=stateText(d.display_state||d.status),code=d.machine_code||"";
 return `<article class="card ${badCarrier(d)?"bad":""}" id="card-${escapeHtml(code)}">
 <div class="bar ${cls}"></div><div class="info"><div class="row1"><div class="name">${escapeHtml(d.device_name||code.slice(0,8)||"未知设备")}</div><div class="seq">${escapeHtml(seq)}</div></div>
 <div class="meta"><span class="state ${cls}">${escapeHtml(state)}</span><b>${d.online?"在线":"离线"}</b>　监控：${d.running?"是":"否"}　${escapeHtml(d.last_seen_ago??0)}秒前<br>位置运营商：${escapeHtml(carrier)}<br>公网IP：${escapeHtml(d.public_ip||"-")}<br>机器码：${escapeHtml(code)}</div><div class="warn">运营商警告：检测到电信/广电</div></div>
 <div class="actions">
 <button class="blue" onclick="sendOne('${encodeURIComponent(code)}','open_target')">打开</button><button class="blue" onclick="sendOne('${encodeURIComponent(code)}','start_target')">启动</button><button class="orange" onclick="sendOne('${encodeURIComponent(code)}','restart_app_only')">重启</button>
 <button class="orange" onclick="sendOne('${encodeURIComponent(code)}','restart_app_start')">重启启动</button><button class="green" onclick="sendOne('${encodeURIComponent(code)}','start_monitor')">开监控</button><button class="red" onclick="sendOne('${encodeURIComponent(code)}','stop_monitor')">停监控</button>
 <button class="secondary" onclick="renameOne('${encodeURIComponent(code)}')">改名</button><button class="secondary" onclick="screenshotOne('${encodeURIComponent(code)}')">截图</button><button class="secondary" onclick="sendOne('${encodeURIComponent(code)}','update_github_config')">更新GitHub</button></div>
 <div class="thumb" id="thumb-${escapeHtml(code)}"><span>缩略图：${d.has_screenshot?"加载中...":"暂无"}</span></div></article>`;
}
async function postJson(path,payload){const res=await fetch(apiUrl(path),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload||{})});if(!res.ok)throw new Error(await res.text());return await res.json();}
async function sendAll(command,isShot=false){try{const data=await postJson("/admin/api/commands/all",{command});alert(`已下发：${command}，在线设备：${data.count??0}`);setTimeout(loadDevices,isShot?5500:800);if(isShot)setTimeout(loadDevices,9000);}catch(e){alert("下发失败："+e.message);}}
async function sendOne(codeEnc,command,value=null){const code=decodeURIComponent(codeEnc);try{await postJson(`/admin/api/devices/${encodeURIComponent(code)}/command`,{command,value});if(command==="screenshot"){const el=document.getElementById("thumb-"+code);if(el)el.innerHTML="<span>截图中...</span>";setTimeout(()=>loadThumb(code),5500);setTimeout(()=>loadThumb(code),9000);}else{setTimeout(loadDevices,800);}}catch(e){alert("下发失败："+e.message);}}
function screenshotOne(codeEnc){sendOne(codeEnc,"screenshot");}
function renameOne(codeEnc){const code=decodeURIComponent(codeEnc),name=prompt("输入新设备名称：");if(!name)return;sendOne(encodeURIComponent(code),"rename",name);}
async function loadThumb(code){const el=document.getElementById("thumb-"+code);if(!el)return;try{const res=await fetch(apiUrl(`/admin/api/devices/${encodeURIComponent(code)}/screenshot`),{cache:"no-store"});if(!res.ok)throw new Error("无截图");const data=await res.json(),shot=data.screenshot||{};if(!shot.image_base64)throw new Error("无截图");el.innerHTML=`<img src="data:image/jpeg;base64,${shot.image_base64}" onclick="window.open(this.src)" /><span>点击放大</span>`;}catch(e){el.innerHTML="<span>缩略图：暂无</span>";}}
loadDevices();setInterval(loadDevices,8000);
</script>
</body>
</html>
"""


@app.get("/admin", response_class=HTMLResponse)
def mobile_admin_page(key: Optional[str] = None):
    check_admin_key(key)
    return HTMLResponse(ADMIN_PAGE_HTML)


@app.get("/admin/api/devices")
def mobile_admin_devices(key: Optional[str] = None):
    check_admin_key(key)
    return list_devices()


@app.post("/admin/api/commands/all")
def mobile_admin_send_all(cmd: CommandIn, key: Optional[str] = None):
    check_admin_key(key)
    return send_all(cmd)


@app.post("/admin/api/devices/{machine_code}/command")
def mobile_admin_send_command(machine_code: str, cmd: CommandIn, key: Optional[str] = None):
    check_admin_key(key)
    return send_command(machine_code, cmd)


@app.get("/admin/api/devices/{machine_code}/screenshot")
def mobile_admin_get_screenshot(machine_code: str, key: Optional[str] = None):
    check_admin_key(key)
    return get_screenshot(machine_code)


@app.delete("/admin/api/devices/{machine_code}")
def mobile_admin_delete_device(machine_code: str, key: Optional[str] = None):
    check_admin_key(key)
    return delete_device(machine_code)


@app.get("/api/version")
def version():
    return {
        "ok": True,
        "version": "v39-web-admin-v1",
        "features": ["heartbeat", "ip_location", "commands", "daily_sequence", "screenshot_upload", "screenshot_file_save", "online_timeout_120s", "mobile_web_admin"]
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
