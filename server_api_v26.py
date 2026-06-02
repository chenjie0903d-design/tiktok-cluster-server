from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from typing import Optional, Dict, List
import time
import uuid
import os
import base64
from datetime import datetime
from fastapi.responses import HTMLResponse

app = FastAPI(title="TikTok Cluster Control Server Web Admin V2.1")

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
        "version": "v26-web-v2.1-railway-test",
        "features": ["heartbeat", "ip_location", "commands", "daily_sequence", "screenshot_upload", "screenshot_file_save", "online_timeout_120s", "mobile_admin_v2_1"]
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
<title>TikTok 集群控制台 Web V2.1</title>
<style>
:root{
  --blue:#1d9bf0;--green:#1db954;--red:#ff2d2f;--orange:#ff9f1a;--dark:#465465;
  --bg:#f4f6fa;--card:#fff;--text:#111827;--muted:#667085;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",Arial,sans-serif;font-size:15px}
.header{position:sticky;top:0;z-index:10;background:#fff;padding:10px 12px 8px;border-bottom:1px solid #e5e7eb}
.title-row{display:flex;align-items:center;gap:8px}
h1{font-size:22px;margin:0;font-weight:900}
.ver{font-size:12px;background:#111827;color:#fff;border-radius:999px;padding:2px 7px}
.refresh-btn{margin-left:auto;border:0;border-radius:12px;background:#eef2f7;padding:10px 14px;font-weight:800;color:#111827}
.server{margin-top:10px;width:100%;border:1px solid #d0d5dd;border-radius:13px;padding:11px 12px;font-size:14px;background:#fff}
.stats{margin-top:9px;color:var(--muted);font-size:14px}
.wrap{padding:12px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.btn{border:0;border-radius:13px;color:#fff;font-size:16px;font-weight:850;padding:13px 10px;min-height:46px}
.btn.blue{background:var(--blue)}.btn.green{background:var(--green)}.btn.red{background:var(--red)}.btn.orange{background:var(--orange)}.btn.dark{background:var(--dark)}
.btn.gray{background:#e5e7eb;color:#111827}
.multi{margin-top:10px;padding-top:10px;border-top:1px solid #e5e7eb}
.card{position:relative;background:var(--card);border-radius:15px;margin:12px 0;padding:14px 12px;border-top:5px solid var(--blue);box-shadow:0 1px 4px rgba(0,0,0,.08)}
.card.offline{border-top-color:var(--red)}.card.bad{background:#fff0f0;border-top-color:var(--red)}
.card.busy{border-top-color:var(--orange)}.card.running{border-top-color:var(--green)}
.seq{position:absolute;right:12px;top:12px;background:#111827;color:#fff;border-radius:999px;width:34px;height:34px;display:flex;align-items:center;justify-content:center;font-weight:900}
.dev-head{display:flex;align-items:center;gap:9px;padding-right:45px}
.select-box{width:24px;height:24px;accent-color:#1d9bf0}
.name{font-size:20px;font-weight:900;line-height:1.25}
.bad-text{color:#d92d20;font-weight:900}
.line{margin-top:6px;color:#344054;word-break:break-all}
.badge{display:inline-block;border-radius:999px;padding:4px 8px;margin-right:6px;font-size:13px;font-weight:850;background:#e8f3ff;color:#0270c9}
.badge.red{background:#ffe4e2;color:#d92d20}.badge.green{background:#dcfae6;color:#079455}.badge.orange{background:#fff4e5;color:#b54708}
.actions{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px}
.actions .btn{font-size:15px;padding:10px 8px;min-height:42px}
.thumb-row{margin-top:12px;display:flex;align-items:center;gap:10px}
.thumb{width:120px;max-height:80px;border:1px solid #d0d5dd;border-radius:8px;object-fit:contain;background:#f8fafc}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.82);display:none;align-items:center;justify-content:center;z-index:99;padding:16px}
.modal.show{display:flex}
.modal img{max-width:100%;max-height:88vh;border-radius:10px;background:#fff}
.close{position:fixed;right:16px;top:16px;background:#fff;border:0;border-radius:999px;font-size:18px;font-weight:900;padding:8px 12px}
.footer{color:#667085;text-align:center;padding:30px 10px 45px}
.small{font-size:13px;color:#667085}
@media (min-width:900px){.cards{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}.card{margin:0}}
</style>
</head>
<body>
<div class="header">
  <div class="title-row">
    <h1>TikTok 集群控制台</h1><span class="ver">Web V2.1</span>
    <button class="refresh-btn" onclick="loadDevices()">刷新</button>
  </div>
  <input id="serverBox" class="server" readonly>
  <div class="stats" id="stats">加载中...</div>
</div>

<div class="wrap">
  <div class="grid">
    <button class="btn green" onclick="sendAll('start_monitor')">全部启动监控</button>
    <button class="btn red" onclick="sendAll('stop_monitor')">全部停止监控</button>
    <button class="btn blue" onclick="sendAll('open_target')">全部打开软件</button>
    <button class="btn blue" onclick="sendAll('start_target')">全部启动软件</button>
    <button class="btn orange" onclick="sendAll('restart_app_only')">重启全部软件</button>
    <button class="btn orange" onclick="sendAll('restart_app_start')">重启全部并启动</button>
    <button class="btn dark" onclick="batchScreenshotAll()">批量截图</button>
    <button class="btn dark" onclick="sendAll('update_github_config')">更新GitHub</button>
  </div>

  <div class="grid multi">
    <button class="btn gray" onclick="selectOnline()">多选在线</button>
    <button class="btn gray" onclick="clearSelected()">取消选择</button>
    <button class="btn green" onclick="sendSelected('start_monitor')">选中开监控</button>
    <button class="btn red" onclick="sendSelected('stop_monitor')">选中停监控</button>
    <button class="btn orange" onclick="sendSelected('restart_app_only')">选中重启</button>
    <button class="btn dark" onclick="screenshotSelected()">选中截图</button>
    <button class="btn dark" onclick="sendSelected('update_github_config')">选中更新GitHub</button>
  </div>

  <div id="cards" class="cards"></div>
  <div class="footer">Safari 可添加到主屏幕，当作手机 App 使用</div>
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
function stateText(d){
  if(!d.online) return "离线";
  const s = d.display_state || d.status || "online";
  if(s==="switching_ip") return "切IP中";
  if(s==="screenshotting") return d.has_screenshot ? "在线" : "截图中";
  return "在线";
}
function render(){
  const online = DEVICES.filter(d=>d.online).length;
  document.getElementById("stats").textContent = `设备：${DEVICES.length}，在线：${online}，刷新时间：${new Date().toLocaleTimeString()}`;
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
    const thumb = d.has_screenshot ? `<div class="thumb-row"><span class="small">缩略图：</span><img class="thumb" onclick="event.stopPropagation();showShot('${code}')" src="/api/devices/${encodeURIComponent(code)}/screenshot/image?key=${encodeURIComponent(ADMIN_KEY)}&t=${Date.now()}"><span class="small">点击放大</span></div>` : `<div class="thumb-row small">缩略图：暂无</div>`;
    card.innerHTML = `
      <div class="seq">${seq}</div>
      <div class="dev-head">
        <input class="select-box" type="checkbox" ${checked} onchange="toggleSelect('${code}', this.checked)">
        <div class="name">${escapeHtml(d.device_name||code.slice(0,8)||"未命名")}</div>
      </div>
      <div class="line">
        <span class="badge ${d.online?'':'red'}">${stateText(d)}</span>
        <b>在线</b>：${d.online?"是":"否"}　
        <b>监控</b>：${d.running?"是":"否"}　
        <b>${d.last_seen_ago||0}秒前</b>
      </div>
      <div class="line ${bad?'bad-text':''}">位置运营商：${escapeHtml(loc)}</div>
      <div class="line">公网IP：${escapeHtml(d.public_ip||"-")}</div>
      <div class="line small">机器码：${escapeHtml(code)}</div>
      <div class="actions">
        <button class="btn blue" onclick="sendOne('${code}','open_target')">打开</button>
        <button class="btn blue" onclick="sendOne('${code}','start_target')">启动</button>
        <button class="btn orange" onclick="sendOne('${code}','restart_app_only')">重启</button>
        <button class="btn orange" onclick="sendOne('${code}','restart_app_start')">重启启动</button>
        <button class="btn green" onclick="sendOne('${code}','start_monitor')">开监控</button>
        <button class="btn red" onclick="sendOne('${code}','stop_monitor')">停监控</button>
        <button class="btn dark" onclick="renameOne('${code}')">改名</button>
        <button class="btn dark" onclick="sendOne('${code}','screenshot', true)">截图</button>
        <button class="btn dark" onclick="sendOne('${code}','update_github_config')">更新GitHub</button>
      </div>
      ${thumb}
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
async function renameOne(code){
  const d = DEVICES.find(x=>x.machine_code===code) || {};
  const name = prompt("输入新设备名称：", d.device_name || "");
  if(!name) return;
  try{
    await api(`/api/devices/${encodeURIComponent(code)}/command`,{method:"POST",body:JSON.stringify({command:"rename",value:name})});
    setTimeout(loadDevices,1000);
  }catch(e){ alert("改名失败：" + e.message); }
}
function showShot(code){
  const img = document.getElementById("modalImg");
  img.src = `/api/devices/${encodeURIComponent(code)}/screenshot/image?key=${encodeURIComponent(ADMIN_KEY)}&t=${Date.now()}`;
  document.getElementById("imgModal").classList.add("show");
}
function closeModal(){
  document.getElementById("imgModal").classList.remove("show");
}
loadDevices();
setInterval(loadDevices,8000);
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

