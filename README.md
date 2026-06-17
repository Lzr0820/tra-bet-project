# TRA Bet：B+C+A + 時刻表版

## 啟動

```bash
cd tra_bet_bca
uvicorn main:app --reload
```

打開：

```text
http://127.0.0.1:8000
```

## 這版新增

- 玩法維持 B+C+A：買多 / 買空 + 預測延誤變化 + 5 分鐘結算
- LiveTrainDelay 保留 60 秒快取，避免 TDX 429
- 新增 DailyTrainTimetable 今日時刻表快取
- 用 TrainNo 對時刻表，補上：
  - 車種
  - 起站、終點站
  - 目前站原定到達時間
  - 目前站原定發車時間
  - 停靠站數

## TDX API 使用

主要使用：

```text
/v2/Rail/TRA/LiveTrainDelay
/v3/Rail/TRA/DailyTrainTimetable/TrainDate/{TrainDate}
```

如果你的 TDX 權限或版本不支援 TrainDate 這支，可以在 main.py 的 `fetch_daily_timetable_from_tdx()` 裡改成：

```text
/v3/Rail/TRA/DailyTrainTimetable/Today
```
