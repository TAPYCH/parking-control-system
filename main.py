import uvicorn
import json
import logging
import time
import csv
import io
from fastapi import FastAPI, Depends, Request, BackgroundTasks, Body, HTTPException, UploadFile, File
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from datetime import datetime

import database as db
from hardware import HardwareController

# --- ЛОГИРОВАНИЕ ---
class AppFilter(logging.Filter):
    def filter(self, record):
        return not any(x in record.getMessage() for x in ["get_display_data", "POST /post", "/api/config", "/admin/sys_data", "favicon"])

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s", datefmt="%H:%M:%S")
logging.getLogger("uvicorn.access").addFilter(AppFilter())

app = FastAPI(title="Universal Parking OS")
templates = Jinja2Templates(directory="templates")
db.init_db()

START_TIME = time.time()
runtime = {"antispam": {}, "active_passages": {}, "sys_logs":[]}

def add_log(level: str, msg: str):
    time_str = datetime.now().strftime("%H:%M:%S")
    runtime["sys_logs"].insert(0, {"time": time_str, "level": level, "msg": msg})
    if len(runtime["sys_logs"]) > 100: runtime["sys_logs"].pop()
    print(f"[{level}] {msg}")

# --- КОНФИГУРАЦИЯ (УНИВЕРСАЛЬНАЯ ДЛЯ ЛЮБОГО ОБЪЕКТА) ---
DEFAULT_CONFIG = {
    "system": {
        "max_places": 50,
        "strict_whitelist_only": False, # Если True, гостей и GSM не пускаем
        "auto_ban_overstay": False,     # Бан за просрочку времени
        "auto_ban_tailgate": False,     # Бан за "паровозик"
        "white_limit_min": 120,
        "guest_limit_min": 15,
        "antispam_sec": 10
    },
    "gates": {
        "gate_1": {"relay_type": "dingtian", "relay_ip": "192.168.0.100", "ch_barrier": 6, "ch_red": 7, "ch_green": 8}
    }
}

def load_config():
    try:
        with open("config.json", "r", encoding="utf-8") as f: return json.load(f)
    except: return DEFAULT_CONFIG

def save_config(cfg):
    with open("config.json", "w", encoding="utf-8") as f: json.dump(cfg, f, indent=4)

CONFIG = load_config()

def get_db():
    s = db.SessionLocal(); 
    try: yield s
    finally: s.close()

# ================================================================
# 🚗 ОСНОВНАЯ ЛОГИКА (КАМЕРЫ И ДАТЧИКИ)
# ================================================================

@app.get("/line/event")
@app.get("/api/v1/camera/event")
async def camera_event(plate: str, direction: str, tasks: BackgroundTasks, gate_id: str = "gate_1", dbs: Session = Depends(get_db)):
    plate = plate.upper().replace("\\", "").strip()
    if "%" in plate or len(plate) < 3: return {"status": "ignore"}

    sys_cfg = CONFIG.get("system", {})
    gate_cfg = CONFIG.get("gates", {}).get(gate_id)
    if not gate_cfg: return {"error": "gate_not_found"}

    # Анти-спам
    now = datetime.now()
    if plate in runtime["antispam"] and (now - runtime["antispam"][plate]).total_seconds() < sys_cfg["antispam_sec"]:
        return {"status": "spam"}
    runtime["antispam"][plate] = now

    user = dbs.query(db.AccessList).filter_by(plate_number=plate).first()
    
    if direction == "in":
        # 1. ЧС
        if user and user.category == "black":
            add_log("ERROR", f"🚫 БЛОК: {plate} в черном списке ({user.note})")
            return {"action": "deny"}

        # 2. Строгий режим
        if sys_cfg.get("strict_whitelist_only", False) and (not user or user.category != "white"):
            add_log("WARN", f"⛔ ОТКАЗ: {plate}. Включен режим 'Только Белый список'.")
            return {"action": "strict_deny"}

        # 3. Успешный въезд
        tasks.add_task(HardwareController.open_barrier, gate_id, gate_cfg, add_log)
        runtime["active_passages"][gate_id] = {"plate": plate, "dir": "in"}
        add_log("INFO", f"🚗 ВЪЕЗД: {plate} открыт шлагбаум")
        return {"action": "open"}

    elif direction == "out":
        tasks.add_task(HardwareController.open_barrier, gate_id, gate_cfg, add_log)
        runtime["active_passages"][gate_id] = {"plate": plate, "dir": "out"}
        add_log("INFO", f"🏁 ВЫЕЗД: {plate} открыт шлагбаум")
        return {"action": "open_out"}

@app.get("/sensor/trigger")
@app.get("/api/v1/sensor/trigger")
async def sensor_event(gate_id: str, sensor_id: str, dbs: Session = Depends(get_db)):
    g_id = gate_id.replace("\\", "").strip()
    s_num = int(sensor_id.replace("\\", "").strip())
    
    active = runtime["active_passages"].get(g_id)
    sys_cfg = CONFIG.get("system", {})
    gate_cfg = CONFIG.get("gates", {}).get(g_id, {})

    # ЛОГИКА "ПАРОВОЗИКА"
    if s_num == 1 and active and active["dir"] == "in":
        # Если датчик 1 сработал снова, а машина еще не уехала (не сработал датчик 3)
        if sys_cfg.get("auto_ban_tailgate", False):
            plate = active["plate"]
            add_log("ERROR", f"🚨 ПАРОВОЗИК: За авто {plate} едет другая машина! Блокировка.")
            # Блокируем текущего или заносим в базу
            user = dbs.query(db.AccessList).filter_by(plate_number=plate).first()
            if not user: user = db.AccessList(plate_number=plate)
            user.category = "black"
            user.note = "Паровозик"
            dbs.add(user)
            dbs.commit()
            HardwareController.close_barrier(g_id, gate_cfg, add_log)

    # ФИНИШ ПРОЕЗДА
    if active and ((active["dir"] == "in" and s_num == 3) or (active["dir"] == "out" and s_num == 1)):
        plate = active["plate"]
        if active["dir"] == "in":
            dbs.add(db.ParkingLog(plate_number=plate, gate_id=g_id, direction="in", is_confirmed=True))
            add_log("SUCCESS", f"✅ ФАКТ ВЪЕЗДА: {plate} заехал")
        else:
            log = dbs.query(db.ParkingLog).filter_by(plate_number=plate, exit_time=None).first()
            if log: 
                log.exit_time = datetime.now()
                # АВТО-БАН ЗА ПРОСРОЧКУ
                if sys_cfg.get("auto_ban_overstay", False):
                    duration = (log.exit_time - log.entry_time).total_seconds() / 60
                    user = dbs.query(db.AccessList).filter_by(plate_number=plate).first()
                    limit = sys_cfg["white_limit_min"] if (user and user.category=="white") else sys_cfg["guest_limit_min"]
                    if duration > limit:
                        if not user: user = db.AccessList(plate_number=plate)
                        user.category = "black"
                        user.note = f"Просрочка {int(duration)} мин"
                        dbs.add(user)
                        add_log("ERROR", f"🛑 АВТО-БАН: {plate} превысил лимит времени!")
            
            add_log("SUCCESS", f"✅ ФАКТ ВЫЕЗДА: {plate} уехал")
        
        dbs.commit()
        HardwareController.close_barrier(g_id, gate_cfg, add_log)
        del runtime["active_passages"][g_id]

    return {"status": "ok"}

# ================================================================
# 📊 API ДЛЯ АДМИНКИ (GUI)
# ================================================================

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request): return templates.TemplateResponse("admin.html", {"request": request})

@app.get("/api/config")
async def get_config_api(): return CONFIG

@app.post("/admin/save_config")
async def save_config_api(new_config: dict = Body(...)):
    global CONFIG
    CONFIG = new_config
    save_config(new_config)
    add_log("SUCCESS", "⚙️ Настройки ядра обновлены")
    return {"status": "ok"}

@app.get("/admin/sys_data")
async def sys_data(dbs: Session = Depends(get_db)):
    occupied = dbs.query(db.ParkingLog).filter_by(exit_time=None).count()
    up = int(time.time() - START_TIME)
    return {
        "logs": runtime["sys_logs"], 
        "occupied": occupied, 
        "max": CONFIG["system"].get("max_places", 50),
        "uptime": f"{up//3600:02d}:{(up%3600)//60:02d}:{up%60:02d}"
    }

# --- УПРАВЛЕНИЕ БАЗОЙ ЧЕРЕЗ GUI ---
@app.get("/api/lists/get")
async def get_lists(dbs: Session = Depends(get_db)):
    users = dbs.query(db.AccessList).all()
    return[{"id": u.id, "plate": u.plate_number, "category": u.category, "note": u.note} for u in users]

@app.post("/api/lists/upload")
async def upload_csv(file: UploadFile = File(...), dbs: Session = Depends(get_db)):
    """Загрузка базы из Excel/CSV файла"""
    content = await file.read()
    decoded = content.decode('utf-8-sig', errors='ignore') # utf-8-sig убирает BOM из Excel
    reader = csv.reader(io.StringIO(decoded), delimiter=';') # Excel использует точку с запятой
    count = 0
    for row in reader:
        if len(row) >= 2:
            plate, category = row[0].strip().upper(), row[1].strip()
            if plate == "НОМЕР": continue # Пропуск заголовка
            
            existing = dbs.query(db.AccessList).filter_by(plate_number=plate).first()
            if not existing:
                dbs.add(db.AccessList(plate_number=plate, category=category, note=row[2] if len(row)>2 else ""))
                count += 1
    dbs.commit()
    add_log("SUCCESS", f"📂 Загружена база из файла: {count} номеров добавлено.")
    return {"status": "ok", "added": count}

if __name__ == "__main__":
    add_log("INFO", "🚀 Платформа управления запущена")
    uvicorn.run(app, host="0.0.0.0", port=8000)