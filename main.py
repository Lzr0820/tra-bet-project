from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import requests
from typing import Dict, Literal, Optional
from datetime import datetime, timedelta
import uuid
import random

CLIENT_ID = ""
CLIENT_SECRET = ""

app = FastAPI(title="TRA BahnBet API")
app.mount("/static", StaticFiles(directory="static"), name="static")

# -----------------------------
# 模擬使用者資料
# -----------------------------
user_data = {
    "name": "Ray",
    "balance": 1000,
    "level": 1,
    "xp": 0,
    "win_streak": 0,
    "total_trades": 0,
    "settled_trades": 0,
    "wins": 0,
    "losses": 0,
    "daily_mission": {
        "title": "完成 3 筆交易",
        "target": 3,
        "progress": 0,
        "reward": 80,
        "claimed": False
    }
}

# bet_id -> bet
bets: Dict[str, dict] = {}

# 5 分鐘後結算
SETTLE_WAIT_SECONDS = 300

# 避免 TDX 429：60 秒內重複請求都用快取
TDX_CACHE_SECONDS = 60
_live_delay_cache = {
    "fetched_at": None,
    "data": None
}

_timetable_cache = {
    "train_date": None,
    "fetched_at": None,
    "data": None,
    "by_train_no": None
}

DELAY_HISTORY_LIMIT = 180
# train_no -> [{time, station_name, delay}]
delay_history: Dict[str, list] = {}

# -----------------------------
# 資料模型
# -----------------------------
class BetRequest(BaseModel):
    train_no: str
    direction: Literal["long", "short"]
    prediction_change: int
    stake: int
    risk_level: Literal["safe", "standard", "bold"] = "standard"
    confidence: int = 50


# -----------------------------
# 基本工具
# -----------------------------
def zh_name(obj):
    if not obj:
        return "—"
    if isinstance(obj, str):
        return obj
    return obj.get("Zh_tw") or obj.get("ZhTw") or obj.get("En") or "—"


def get_access_token():
    auth_url = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
    auth_data = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET
    }
    response = requests.post(auth_url, data=auth_data, timeout=20)
    response.raise_for_status()
    return response.json()["access_token"]


def fetch_live_delays_from_tdx():
    token = get_access_token()
    url = "https://tdx.transportdata.tw/api/basic/v2/Rail/TRA/LiveTrainDelay"
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    return response.json()


def update_delay_history(data):
    now = datetime.now().isoformat(timespec="seconds")
    for train in data:
        train_no = str(train.get("TrainNo", "")).strip()
        if not train_no:
            continue

        delay = int(train.get("DelayTime", 0) or 0)
        station_name = zh_name(train.get("StationName"))
        history = delay_history.setdefault(train_no, [])

        if history and history[-1].get("station_name") == station_name and history[-1].get("delay") == delay:
            history[-1]["time"] = now
        else:
            history.append({"time": now, "station_name": station_name, "delay": delay})

        if len(history) > DELAY_HISTORY_LIMIT:
            delay_history[train_no] = history[-DELAY_HISTORY_LIMIT:]


def build_station_delay_map(train_no: str):
    result = {}
    for item in delay_history.get(str(train_no), []):
        station_name = item.get("station_name")
        if station_name and station_name != "—":
            result[station_name] = item.get("delay")
    return result


def fetch_live_delays(force_refresh: bool = False):
    now = datetime.now()
    fetched_at: Optional[datetime] = _live_delay_cache["fetched_at"]

    if (
        not force_refresh
        and _live_delay_cache["data"] is not None
        and fetched_at is not None
        and (now - fetched_at).total_seconds() < TDX_CACHE_SECONDS
    ):
        return _live_delay_cache["data"]

    data = fetch_live_delays_from_tdx()
    update_delay_history(data)
    _live_delay_cache["data"] = data
    _live_delay_cache["fetched_at"] = now
    return data


def fetch_daily_timetable_from_tdx(train_date: str):
    token = get_access_token()
    url = f"https://tdx.transportdata.tw/api/basic/v3/Rail/TRA/DailyTrainTimetable/TrainDate/{train_date}"
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()


def get_today_train_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def extract_timetable_items(raw):
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ["TrainTimetables", "DailyTrainTimetables", "data", "Data"]:
            if isinstance(raw.get(key), list):
                return raw[key]
    return []


def build_timetable_index(items):
    by_train_no = {}
    for item in items:
        train_info = item.get("TrainInfo") or item.get("TrainInfoDto") or {}
        train_no = str(train_info.get("TrainNo") or item.get("TrainNo") or "").strip()
        if train_no:
            by_train_no[train_no] = item
    return by_train_no


def fetch_daily_timetable(force_refresh: bool = False):
    train_date = get_today_train_date()
    if (
        not force_refresh
        and _timetable_cache["data"] is not None
        and _timetable_cache["by_train_no"] is not None
        and _timetable_cache["train_date"] == train_date
    ):
        return _timetable_cache["data"], _timetable_cache["by_train_no"]

    raw = fetch_daily_timetable_from_tdx(train_date)
    items = extract_timetable_items(raw)
    by_train_no = build_timetable_index(items)

    _timetable_cache["train_date"] = train_date
    _timetable_cache["fetched_at"] = datetime.now()
    _timetable_cache["data"] = items
    _timetable_cache["by_train_no"] = by_train_no
    return items, by_train_no


def safe_get_timetable(train_no: str):
    try:
        _, by_train_no = fetch_daily_timetable()
        return by_train_no.get(str(train_no))
    except Exception:
        return None


def parse_stop_time(stop: dict):
    arrival_time = stop.get("ArrivalTime") or "—"
    departure_time = stop.get("DepartureTime") or "—"
    display_time = departure_time if departure_time != "—" else arrival_time
    return {
        "station_name": zh_name(stop.get("StationName")),
        "station_id": stop.get("StationID") or stop.get("StationId"),
        "arrival_time": arrival_time,
        "departure_time": departure_time,
        "time": display_time,
        "delay_time": 0,
        "stop_sequence": stop.get("StopSequence")
    }


def extract_timetable_detail(timetable: dict, current_station_name: str = "—"):
    if not timetable:
        return {
            "scheduled_arrival_time": "—",
            "scheduled_departure_time": "—",
            "scheduled_current_station": "—",
            "first_station_name": "—",
            "last_station_name": "—",
            "stops_count": 0,
            "timetable_stops": []
        }

    train_info = timetable.get("TrainInfo") or {}
    stop_times = timetable.get("StopTimes") or timetable.get("StopTime") or []
    stops = [parse_stop_time(stop) for stop in stop_times]

    first_station = zh_name(train_info.get("StartingStationName"))
    last_station = zh_name(train_info.get("EndingStationName"))

    if first_station == "—" and stops:
        first_station = stops[0]["station_name"]
    if last_station == "—" and stops:
        last_station = stops[-1]["station_name"]

    current_stop = None
    if current_station_name and current_station_name != "—":
        for stop in stops:
            if stop["station_name"] == current_station_name:
                current_stop = stop
                break

    return {
        "scheduled_arrival_time": current_stop["arrival_time"] if current_stop else "—",
        "scheduled_departure_time": current_stop["departure_time"] if current_stop else "—",
        "scheduled_current_station": current_stop["station_name"] if current_stop else "—",
        "first_station_name": first_station,
        "last_station_name": last_station,
        "stops_count": len(stops),
        "timetable_stops": stops
    }


def simplify_train_type(raw_name: str) -> str:
    if not raw_name or raw_name == "—":
        return "—"
    name = str(raw_name)
    if "3000" in name or "EMU3000" in name:
        return "自強 3000"
    if "太魯閣" in name:
        return "太魯閣"
    if "普悠瑪" in name:
        return "普悠瑪"
    if "推拉" in name or "PP" in name:
        return "自強 PP"
    if "自強" in name:
        return "自強"
    if "莒光" in name:
        return "莒光"
    if "區間快" in name:
        return "區間快"
    if "區間" in name:
        return "區間"
    if "觀光" in name or "鳴日" in name or "藍皮" in name:
        return "觀光列車"
    return name.replace("（", "(").split("(")[0].strip() or name


def get_delay_status(delay: int) -> dict:
    if delay >= 60:
        return {"label": "嚴重誤點", "class_name": "danger", "risk": 88}
    if delay >= 30:
        return {"label": "大幅誤點", "class_name": "hot", "risk": 72}
    if delay >= 15:
        return {"label": "中度誤點", "class_name": "warn", "risk": 48}
    if delay >= 5:
        return {"label": "小幅誤點", "class_name": "calm", "risk": 28}
    return {"label": "接近準點", "class_name": "normal", "risk": 10}


def make_delay_series(train_no: str, current_delay: int):
    """前端小圖用。若歷史不足，就補一點 demo 式平滑資料，不影響結算。"""
    history = delay_history.get(str(train_no), [])[-18:]
    if history:
        return [int(item.get("delay", 0) or 0) for item in history]

    values = []
    base = max(0, current_delay)
    for i in range(18):
        drift = int((i - 9) * 0.15)
        noise = random.choice([-1, 0, 0, 1])
        values.append(max(0, base + drift + noise))
    values[-1] = base
    return values


def normalize_train(train: dict):
    train_no = str(train.get("TrainNo", "—"))
    current_station = zh_name(train.get("StationName"))
    live_ending_station = zh_name(train.get("EndingStationName"))
    live_starting_station = zh_name(train.get("StartingStationName")) if train.get("StartingStationName") else "查無起點"

    timetable = safe_get_timetable(train_no)
    train_info = timetable.get("TrainInfo", {}) if timetable else {}
    timetable_detail = extract_timetable_detail(timetable, current_station)

    raw_train_type = zh_name(train.get("TrainTypeName"))
    if raw_train_type == "—":
        raw_train_type = zh_name(train_info.get("TrainTypeName"))
    train_type = simplify_train_type(raw_train_type)

    starting_station = timetable_detail["first_station_name"] if timetable_detail["first_station_name"] != "—" else live_starting_station
    ending_station = timetable_detail["last_station_name"] if timetable_detail["last_station_name"] != "—" else live_ending_station
    delay_time = int(train.get("DelayTime", 0) or 0)

    station_delay_map = build_station_delay_map(train_no)
    for stop in timetable_detail.get("timetable_stops", []):
        station_name = stop.get("station_name")
        if station_name == current_station:
            stop["delay_time"] = delay_time
            stop["delay_source"] = "current"
        elif station_name in station_delay_map:
            stop["delay_time"] = station_delay_map[station_name]
            stop["delay_source"] = "history"
        else:
            stop["delay_time"] = None
            stop["delay_source"] = "unknown"

    status = get_delay_status(delay_time)
    return {
        "train_no": train_no,
        "train_type": train_type,
        "current_station": current_station,
        "starting_station": starting_station,
        "ending_station": ending_station,
        "delay_time": delay_time,
        "delay_status": status,
        "risk_score": status["risk"],
        "update_time": train.get("UpdateTime") or train.get("SrcUpdateTime"),
        "sparkline": make_delay_series(train_no, delay_time),
        **timetable_detail
    }


def get_delayed_trains_top12():
    data = fetch_live_delays()
    delayed_trains = [train for train in data if int(train.get("DelayTime", 0) or 0) > 0]
    sorted_data = sorted(delayed_trains, key=lambda x: int(x.get("DelayTime", 0) or 0), reverse=True)
    return [normalize_train(train) for train in sorted_data[:12]]


def find_train_by_no(train_no: str):
    data = fetch_live_delays()
    for train in data:
        if str(train.get("TrainNo")) == str(train_no):
            return normalize_train(train)
    return None


def seconds_until_settle(placed_at_str: str) -> int:
    placed_at = datetime.fromisoformat(placed_at_str)
    settle_at = placed_at + timedelta(seconds=SETTLE_WAIT_SECONDS)
    remaining = int((settle_at - datetime.now()).total_seconds())
    return max(0, remaining)


def calc_payout(stake: int, direction: str, prediction_change: int, entry_delay: int, actual_delay: int, risk_level: str, confidence: int) -> dict:
    """
    新版規則：
    1. 買多：延誤增加賺；買空：延誤減少賺。
    2. 不只看方向，也看你猜的變化量準不準。
    3. risk_level 影響倍率與風險。
    4. confidence 類似信心加成；信心越高，準的時候多賺，錯的時候多扣。
    """
    actual_change = actual_delay - entry_delay

    if direction == "long":
        directional_move = actual_change
        signed_prediction = abs(prediction_change)
    else:
        directional_move = -actual_change
        signed_prediction = -abs(prediction_change)

    risk_table = {
        "safe": {"name": "保守", "per_minute": 0.08, "bonus_scale": 0.7, "cap_gain": 1.0, "cap_loss": -0.7},
        "standard": {"name": "標準", "per_minute": 0.12, "bonus_scale": 1.0, "cap_gain": 1.6, "cap_loss": -1.0},
        "bold": {"name": "激進", "per_minute": 0.18, "bonus_scale": 1.35, "cap_gain": 2.5, "cap_loss": -1.0},
    }
    rule = risk_table.get(risk_level, risk_table["standard"])
    confidence = max(1, min(100, int(confidence)))
    confidence_factor = 0.85 + confidence / 100 * 0.35

    if actual_change == 0:
        return {
            "actual_change": actual_change,
            "signed_prediction": signed_prediction,
            "profit": 0,
            "payout": stake,
            "return_rate": 0.0,
            "accuracy_bonus": 0.0,
            "message": "延誤沒有變化，不賺不賠",
            "direction_correct": False
        }

    direction_correct = directional_move > 0
    base_return_rate = directional_move * rule["per_minute"] * confidence_factor

    accuracy_error = abs(signed_prediction - actual_change)
    accuracy_bonus = 0.0
    if direction_correct:
        if accuracy_error == 0:
            accuracy_bonus = 0.45 * rule["bonus_scale"]
        elif accuracy_error <= 1:
            accuracy_bonus = 0.28 * rule["bonus_scale"]
        elif accuracy_error <= 3:
            accuracy_bonus = 0.14 * rule["bonus_scale"]

    raw_return_rate = base_return_rate + accuracy_bonus
    return_rate = max(rule["cap_loss"], min(rule["cap_gain"], raw_return_rate))
    profit = int(round(stake * return_rate))
    payout = max(0, stake + profit)

    if direction_correct and accuracy_error == 0:
        message = "神預測：方向與變化量都命中"
    elif direction_correct:
        message = "方向正確"
    else:
        message = "方向錯誤"

    return {
        "actual_change": actual_change,
        "signed_prediction": signed_prediction,
        "profit": profit,
        "payout": payout,
        "return_rate": round(return_rate, 4),
        "accuracy_bonus": round(accuracy_bonus, 4),
        "message": message,
        "direction_correct": direction_correct
    }


def add_xp(amount: int):
    user_data["xp"] += amount
    while user_data["xp"] >= user_data["level"] * 100:
        user_data["xp"] -= user_data["level"] * 100
        user_data["level"] += 1


# -----------------------------
# 頁面
# -----------------------------
@app.get("/")
def home():
    return FileResponse("static/index.html")


# -----------------------------
# API
# -----------------------------
@app.get("/api/me")
def get_me():
    return user_data


@app.get("/api/rules")
def get_rules():
    return {
        "settle_wait_seconds": SETTLE_WAIT_SECONDS,
        "tdx_cache_seconds": TDX_CACHE_SECONDS,
        "risk_levels": {
            "safe": "保守：波動小，最多不會賠光，適合試玩",
            "standard": "標準：正常倍率，最多賠光本金",
            "bold": "激進：猜對賺比較多，但錯也更痛"
        },
        "simple_rules": [
            "買多：你覺得 5 分鐘後延誤會增加。",
            "買空：你覺得 5 分鐘後延誤會減少。",
            "預測變化量：不是猜總延誤，而是猜會多誤點或少誤點幾分鐘。",
            "方向正確會依變化幅度獲利；猜得越準有額外 bonus。",
            "延誤完全沒變，退回本金。"
        ]
    }


@app.get("/api/markets")
def get_markets():
    try:
        trains = get_delayed_trains_top12()
        result = []
        for train in trains:
            train_no = train["train_no"]
            train_bets = [b for b in bets.values() if b["train_no"] == train_no]
            my_open_bet = next((b for b in train_bets if b["status"] == "open"), None)
            pool = sum(b["stake"] for b in train_bets)
            result.append({
                **train,
                "pool": pool,
                "my_bet": my_open_bet,
                "trade_count": len(train_bets)
            })
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/my-bets")
def get_my_bets():
    result = []
    sorted_bets = sorted(bets.values(), key=lambda b: b.get("placed_at", ""), reverse=True)
    for bet in sorted_bets:
        item = dict(bet)
        item["seconds_left"] = seconds_until_settle(bet["placed_at"]) if bet["status"] == "open" else 0
        result.append(item)
    return result


@app.get("/api/history/{train_no}")
def get_train_history(train_no: str):
    return {"train_no": train_no, "history": delay_history.get(str(train_no), [])}


@app.post("/api/bet")
def place_bet(payload: BetRequest):
    try:
        train = find_train_by_no(payload.train_no)
        if not train:
            raise HTTPException(status_code=404, detail="找不到這班列車")

        if abs(payload.prediction_change) > 120:
            raise HTTPException(status_code=400, detail="預測變化量必須介於 0 到 120 分鐘")

        if payload.stake not in [10, 20, 50, 100, 200, 500]:
            raise HTTPException(status_code=400, detail="下注金額只允許 10 / 20 / 50 / 100 / 200 / 500")

        if user_data["balance"] < payload.stake:
            raise HTTPException(status_code=400, detail="餘額不足")

        # 同一班車只限制一筆持倉，結算後可再下
        for b in bets.values():
            if b["train_no"] == str(payload.train_no) and b["status"] == "open":
                raise HTTPException(status_code=400, detail="這班車你已經有持倉，請先結算或等待")

        user_data["balance"] -= payload.stake
        user_data["total_trades"] += 1
        bet_id = str(uuid.uuid4())[:8]
        normalized_prediction = abs(payload.prediction_change)
        signed_prediction = normalized_prediction if payload.direction == "long" else -normalized_prediction

        bets[bet_id] = {
            "bet_id": bet_id,
            "train_no": str(payload.train_no),
            "stake": payload.stake,
            "direction": payload.direction,
            "prediction_change": signed_prediction,
            "risk_level": payload.risk_level,
            "confidence": max(1, min(100, int(payload.confidence))),
            "entry_delay": train["delay_time"],
            "status": "open",
            "actual_delay": None,
            "actual_change": None,
            "payout": None,
            "profit": None,
            "return_rate": None,
            "accuracy_bonus": None,
            "settlement_message": None,
            "placed_at": datetime.now().isoformat(timespec="seconds"),
            "train_type": train["train_type"],
            "current_station": train["current_station"],
            "starting_station": train["starting_station"],
            "ending_station": train["ending_station"],
            "delay_status": train["delay_status"],
            "stops_count": train.get("stops_count", 0),
        }

        return {"message": "下注成功，5 分鐘後可結算", "balance": user_data["balance"], "bet": bets[bet_id]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def settle_one_bet(bet_id: str):
    if bet_id not in bets:
        raise HTTPException(status_code=404, detail="找不到這筆下注")

    bet = bets[bet_id]
    if bet["status"] == "settled":
        return bet

    remaining = seconds_until_settle(bet["placed_at"])
    if remaining > 0:
        raise HTTPException(status_code=400, detail=f"尚未達到可結算時間，還要再等 {remaining} 秒")

    train = find_train_by_no(bet["train_no"])
    if not train:
        raise HTTPException(status_code=404, detail="目前 API 找不到這班列車，暫時無法結算")

    result = calc_payout(
        stake=bet["stake"],
        direction=bet["direction"],
        prediction_change=bet["prediction_change"],
        entry_delay=bet["entry_delay"],
        actual_delay=train["delay_time"],
        risk_level=bet["risk_level"],
        confidence=bet["confidence"]
    )

    bet["actual_delay"] = train["delay_time"]
    bet["actual_change"] = result["actual_change"]
    bet["payout"] = result["payout"]
    bet["profit"] = result["profit"]
    bet["return_rate"] = result["return_rate"]
    bet["accuracy_bonus"] = result["accuracy_bonus"]
    bet["settlement_message"] = result["message"]
    bet["status"] = "settled"
    bet["settled_at"] = datetime.now().isoformat(timespec="seconds")

    user_data["balance"] += result["payout"]
    user_data["settled_trades"] += 1
    user_data["daily_mission"]["progress"] = min(user_data["daily_mission"]["target"], user_data["daily_mission"]["progress"] + 1)

    if result["profit"] > 0:
        user_data["wins"] += 1
        user_data["win_streak"] += 1
        add_xp(30 + min(50, user_data["win_streak"] * 5))
    elif result["profit"] < 0:
        user_data["losses"] += 1
        user_data["win_streak"] = 0
        add_xp(10)
    else:
        add_xp(15)

    return bet


@app.post("/api/settle/{bet_id}")
def settle_bet(bet_id: str):
    try:
        bet = settle_one_bet(bet_id)
        return {"message": "結算完成", "bet": bet, "balance": user_data["balance"]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/settle-all")
def settle_all_bets():
    results = []
    for bet_id, bet in list(bets.items()):
        if bet["status"] != "open":
            continue
        if seconds_until_settle(bet["placed_at"]) > 0:
            continue
        try:
            settled = settle_one_bet(bet_id)
            results.append({
                "bet_id": bet_id,
                "train_no": settled["train_no"],
                "actual_delay": settled["actual_delay"],
                "actual_change": settled["actual_change"],
                "profit": settled["profit"],
                "payout": settled["payout"],
                "return_rate": settled["return_rate"]
            })
        except Exception:
            continue

    return {"message": "可結算的下注已批次結算完成", "results": results, "balance": user_data["balance"]}


@app.post("/api/claim-mission")
def claim_mission():
    mission = user_data["daily_mission"]
    if mission["claimed"]:
        raise HTTPException(status_code=400, detail="今天任務已領取")
    if mission["progress"] < mission["target"]:
        raise HTTPException(status_code=400, detail="任務尚未完成")
    mission["claimed"] = True
    user_data["balance"] += mission["reward"]
    add_xp(40)
    return {"message": f"已領取任務獎勵 +{mission['reward']} 點", "balance": user_data["balance"], "me": user_data}