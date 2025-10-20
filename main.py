# 檔名: main.py (最終部署版本：接收 App 傳入的路程分鐘數)

import pandas as pd
import joblib
import json
import sys
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os

# --- 初始化 ---
app = FastAPI(title="診所看診時間預測 API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# 建立模型的快取，用於避免重複載入
models_cache: Dict[str, Any] = {}
columns_cache: Dict[str, List[str]] = {}
CLINIC_STATUS_FILE = "./clinic_status.json"

# --- 號碼轉換工具 (SequenceConverter 保持不變) ---
class SequenceConverter:
    """將實際的病人號碼轉換為場次中的順序 (sequence_in_session)"""
    def __init__(self, all_patient_numbers: list):
        # 排除非整數或重複號碼，並依數字大小排序
        sorted_unique_numbers = sorted(list(set(n for n in all_patient_numbers if isinstance(n, int))))
        # 建立一個「原始號碼 -> 順位」的對照字典
        self._sequence_map = {number: rank + 1 for rank, number in enumerate(sorted_unique_numbers)}
    
    def get_sequence(self, patient_number: int) -> Optional[int]:
        """獲取指定號碼在本次門診中的順序。"""
        return self._sequence_map.get(patient_number)

# --- 載入模型和欄位列表 (保持不變) ---
def load_model_and_columns(department: str):
    """從檔案載入模型和訓練時使用的特徵欄位，並進行快取。"""
    
    if department in models_cache and department in columns_cache:
        return models_cache[department], columns_cache[department]
        
    MODEL_OUTPUT_PATH = f"./model_{department}.joblib"
    COLUMNS_OUTPUT_PATH = f"./columns_{department}.json"

    try:
        # 載入模型
        model = joblib.load(MODEL_OUTPUT_PATH)
        models_cache[department] = model
        
        # 載入訓練時使用的欄位順序 (OHE 產生的完整欄位名)
        with open(COLUMNS_OUTPUT_PATH, "r", encoding="utf-8") as f:
            # 假設欄位文件格式為 {"columns": [...]}
            data = json.load(f)
            if 'columns' in data:
                 required_columns = data['columns']
            else:
                 required_columns = data # 如果是舊的直接 array 格式
                 
        columns_cache[department] = required_columns
        
        return model, required_columns
        
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail=f"服務端模型或欄位檔案未找到 ({department})。請確認已執行訓練腳本。")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"載入模型時發生錯誤: {e}")

# --- API 輸入模型 (修正) ---
class PredictionRequest(BaseModel):
    department: str
    clinic_room: str
    doctor: str
    time_slot: str
    patient_number: int
    # 【新增】由 App 計算並傳入的路程時間 (分鐘)
    estimated_travel_minutes: Optional[int] = None 

# --- 預測 API 終端 ---
@app.post("/predict_consult_time", tags=["預測服務"])
async def predict_consult_time(request: PredictionRequest):
    
    # 1. 載入模型和欄位列表
    model, required_columns = load_model_and_columns(request.department)
    
    # 2. 獲取診間即時狀態
    try:
        with open(CLINIC_STATUS_FILE, "r", encoding="utf-8") as f:
            status_data = json.load(f)
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="服務端看診狀態檔案未找到，請檢查爬蟲任務是否運行。")

    clinic_key = f"{request.department}_{request.clinic_room}_{request.doctor}_{request.time_slot}"
    clinic_status = status_data["clinics"].get(clinic_key)

    if not clinic_status:
        raise HTTPException(status_code=404, detail=f"未找到 {clinic_key} 門診的即時狀態。請確認輸入資訊或檢查爬蟲數據。")

    # 3. 計算 sequence_in_session
    converter = SequenceConverter(clinic_status["all_numbers_in_session"])
    sequence_in_session = converter.get_sequence(request.patient_number)

    if sequence_in_session is None:
        raise HTTPException(status_code=400, detail=f"號碼 {request.patient_number} 不在本次門診的掛號名單中。")


    # 4. 準備輸入特徵 (t_report_minutes 邏輯修正)
    now = datetime.now()
    travel_duration_minutes = 0 # 初始化為 0

    # 【關鍵邏輯修正】計算預計報到時間
    if request.estimated_travel_minutes is not None and request.estimated_travel_minutes >= 0:
        travel_duration_minutes = request.estimated_travel_minutes
        # 使用當前時間 + 路程時間 = 預計報到時間
        predicted_report_time = now + timedelta(minutes=travel_duration_minutes)
    else:
        # App 沒有傳入路程時間，退回使用當前時間作為報到時間
        predicted_report_time = now
        print("⚠️ App 未傳入路程時間，使用當前時間作為報到時間。")


    # 計算 t_report_minutes (使用預計報到時間)
    t_report_minutes = predicted_report_time.hour * 60 + predicted_report_time.minute 

    # 其他靜態特徵計算
    weekday = predicted_report_time.weekday() 
    current_num = clinic_status.get("current_number", 0)
    waiting_num = clinic_status.get("waiting", 0)
    completed_num = clinic_status.get("completed", 0)
    number_gap_at_report = max(0, request.patient_number - current_num) 

    # 組合原始輸入資料
    input_data = {
        "department": request.department,
        "clinic_room": request.clinic_room,
        "doctor": request.doctor,
        "time_slot": request.time_slot,
        "patient_number": request.patient_number,
        "sequence_in_session": sequence_in_session,
        "current_number_at_report": current_num,
        "waiting_at_report": waiting_num,
        "completed_at_report": completed_num,
        "number_gap_at_report": number_gap_at_report,
        "hour_at_report": predicted_report_time.hour,
        "t_report_minutes": t_report_minutes, 
        "weekday": weekday,  
    }
    
    # 5. 特徵工程與預測
    input_df = pd.DataFrame([input_data])
    
    categorical_cols_for_ohe = ['department', 'clinic_room', 'doctor', 'time_slot', 'weekday']
    
    input_df_encoded = pd.get_dummies(input_df, columns=categorical_cols_for_ohe) 

    # 確保輸入欄位順序和數量與訓練時一致 (OHE 核心步驟)
    input_df_aligned = input_df_encoded.reindex(columns=required_columns, fill_value=0)

    # 執行預測
    try:
        prediction_minutes = model.predict(input_df_aligned)[0]
    except Exception as e:
        print(f"預測失敗：{e}")
        raise HTTPException(status_code=500, detail=f"預測模型執行失敗: {e}")
    
    # 6. 結果轉換：將預測的 t_consult_minutes 轉換為預計看診時間 (HH:MM)
    prediction_minutes = max(0, prediction_minutes) 

    # 預測時間 (當日 00:00 起的總分鐘數) 轉為 Datetime 物件
    today = datetime.now().date()
    predicted_datetime = datetime(today.year, today.month, today.day) + timedelta(minutes=int(prediction_minutes))

    # 核心修正邏輯：確保預測時間不早於 預計報到時間 (predicted_report_time)
    # 設置緩衝：預計報到時間 + 1 分鐘
    report_time_with_buffer = predicted_report_time + timedelta(minutes=1)
    
    if predicted_datetime < report_time_with_buffer:
        corrected_datetime = report_time_with_buffer
    else:
        corrected_datetime = predicted_datetime

    # 最終格式化
    predicted_time_str = corrected_datetime.strftime("%H:%M")

    # 重新計算準確的等待時間 (corrected_datetime 減去當前時間)
    wait_minutes = int((corrected_datetime - now).total_seconds() / 60)
    wait_minutes = max(0, wait_minutes) 

    return {
        "status": "success",
        "predicted_consult_time": predicted_time_str, # HH:MM 預計看診時間
        "estimated_wait_minutes": wait_minutes,       # 預計還需等待分鐘數
        "estimated_travel_minutes": travel_duration_minutes, # App 傳入或預設值
        "predicted_report_time": predicted_report_time.strftime("%H:%M"), # 預計報到時間
        "details": {
            "prediction_target": "t_consult_minutes",
            "model_feature_count": len(required_columns),
            "input_sequence_in_session": sequence_in_session,
            "raw_t_consult_minutes": int(prediction_minutes)
        }
    }

# --- 服務啟動訊息 ---
@app.on_event("startup")
async def startup_event():
    print("FastAPI 服務啟動中...")

# 提示：若要在本地運行，請使用命令：uvicorn main:app --reload