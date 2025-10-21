# 檔名: scraper_task.py (最終部署版本：【移除】動態特徵計算和 SQLite 歷史記錄)

import schedule
import time
import json
from datetime import datetime, timedelta 
import re
import sys
import io
import argparse
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from typing import List, Dict, Any

# --- 設置與錯誤修正 ---
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
CLINIC_STATUS_FILE = "clinic_status.json" # 最終 JSON 檔案名稱

# 【移除】所有關於 SQLite DB 的設置和操作函數 (setup_db, log_snapshot, calculate_dynamic_features)
# --------------------------------------------------------

# --- 原始爬蟲和解析函數 (已調整，用於計算靜態特徵) ---

def parse_modal_text(modal_text, dname):
    # 您的原始 parse_modal_text 邏輯
    cut_keywords = ["狀態說明", "※實際看診", "門診看診時間預估", "離開", "報到時間", "重整"]
    for kw in cut_keywords:
        if kw in modal_text:
            modal_text = modal_text.split(kw)[0].strip()
            break

    lines = modal_text.splitlines()
    doctor, location, current, waiting, completed = "", "", "", "", ""
    patients = []

    for i, line in enumerate(lines):
        if "醫師" in line:
            doctor = line.strip().replace(" 醫師", "")
        elif "地點" in line:
            location = lines[i + 1].strip() if i + 1 < len(lines) else ""
        elif "目前叫號" in line:
            current = lines[i + 1].strip() if i + 1 < len(lines) else ""
        elif "等待人數" in line:
            waiting = lines[i + 1].strip() if i + 1 < len(lines) else ""
        elif "完診人數" in line:
            completed = lines[i + 1].strip() if i + 1 < len(lines) else ""

    clinic_room = ""
    department_name = dname
    match = re.search(r"(\d+診)", dname)
    if match:
        clinic_room = match.group(1)
        department_name = dname.replace(clinic_room, "").strip()

    status_keywords = {"過號", "已報到", "看診中", "未報到", "優先號", "檢後再診"}
    i = 0
    while i < len(lines) - 1:
        line = lines[i].strip()
        next_line = lines[i + 1].strip()
        if line.isdigit() and next_line in status_keywords:
            patients.append({"number": int(line), "status": next_line})
            i += 2
        else:
            i += 1

    overdue_patients = [p["number"] for p in patients if p["status"] == "過號"]
    overdue_patients.sort()

    timestamp = datetime.now()
    hour = timestamp.hour
    weekday = timestamp.weekday()
    time_slot = "morning" if hour < 12 else ("afternoon" if hour < 18 else "evening")
    current_num = int(current) if current.isdigit() else None

    # 這裡的 results 列表只保留第一個病患的資訊作為診間狀態快照，這部分可以根據需要調整
    # 由於 API 只需要診間的整體狀態和病人列表，我們優化這裡的輸出
    
    # 建立詳細的病人狀態列表 (用於 API 內計算 sequence_in_session 和 unaccounted_gap_count)
    detailed_patients_status = []
    for p in patients:
        if isinstance(p.get('number'), int) and p.get('status'):
             detailed_patients_status.append({
                 'number': p['number'], 
                 'status': p['status']
             })

    # 計算 unaccounted_gap_count (雖然模型不使用，但為了保持數據完整性，將邏輯放在這裡)
    def get_unaccounted_gap_count(target_patient_num, current_num, patient_list):
        if not current_num or target_patient_num <= current_num:
            return 0
        
        count = 0
        for p in patient_list:
            if current_num < p['number'] < target_patient_num:
                if p['status'] in ['未報到', '過號']:
                    count += 1
        return count

    # 返回診間快照資訊
    clinic_snapshot = {
        "department": department_name,
        "clinic_room": clinic_room,
        "doctor": doctor,
        "location": location,
        "current_number": current_num if current_num is not None else 0, # 確保為數字或 0
        "waiting": int(waiting) if waiting.isdigit() else 0,
        "completed": int(completed) if completed.isdigit() else 0,
        "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "hour": hour,
        "weekday": weekday,
        "time_slot": time_slot,
        "all_patients_status": detailed_patients_status # 包含所有病患的號碼和狀態
    }

    return [clinic_snapshot] # 返回單一診間的快照列表

# ... (get_modal_info 函數保持不變，略過以簡化)

def get_modal_info(dept_keywords=None, doctor_keyword=None):
    results = []
    try:
        chrome_options = Options()
        chrome_options.add_argument('--headless')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--disable-extensions")
        chrome_options.add_argument("start-maximized")

        user_agent = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.0.0 Safari/537.36'
        chrome_options.add_argument(f'user-agent={user_agent}')
        
        # 使用 WebDriver Manager 確保驅動程式版本正確
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
        url = "https://www.kmugh.org.tw/Web/WebRegistration/OPDSeq/ProcessMain?lang=twtw"
        driver.get(url)
        time.sleep(3)

        blocks = driver.find_elements(By.CLASS_NAME, "c_table")

        for idx, block in enumerate(blocks):
            try:
                block_text = block.text.strip()
                dname = block.get_attribute("data-dname") or ""

                if dept_keywords and not any(kw in dname for kw in dept_keywords):
                    continue
                if doctor_keyword and doctor_keyword not in block_text:
                    continue

                try:
                    status_span = block.find_element(By.CSS_SELECTOR, "span.Title.CurrentSeq")
                    if status_span and "結束看診" in status_span.text:
                        # ... (處理結束看診的邏輯保持不變)
                        doctor = ""
                        lines = block_text.splitlines()
                        for i, line in enumerate(lines):
                            if "醫師" in line and i + 1 < len(lines):
                                doctor = lines[i + 1].strip()
                                break

                        clinic_room = ""
                        department_name = dname
                        match = re.search(r"(\d+診)", dname)
                        if match:
                            clinic_room = match.group(1)
                            department_name = dname.replace(clinic_room, "").strip()
                            
                        # 返回簡化的結束看診狀態
                        results.append([{
                            "department": department_name,
                            "clinic_room": clinic_room,
                            "doctor": doctor,
                            "current_number": 0, # 結束看診設為 0
                            "waiting": 0,
                            "completed": 0,
                            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "all_patients_status": [],
                            "hour": datetime.now().hour,
                            "weekday": datetime.now().weekday(),
                            "time_slot": "morning" # 時段可能不準確，但影響不大
                        }])
                        continue
                except:
                    pass

                driver.execute_script("arguments[0].scrollIntoView({behavior: 'instant', block: 'center'});", block)
                time.sleep(0.5)
                driver.execute_script("arguments[0].click();", block)
                time.sleep(2)

                WebDriverWait(driver, 5).until(EC.visibility_of_element_located((By.CLASS_NAME, "modal")))
                modal = driver.find_element(By.CLASS_NAME, "modal")
                modal_text = modal.text.strip()

                if modal_text:
                    result = parse_modal_text(modal_text, dname)
                    results.append(result)

                try:
                    close_btn = driver.find_element(By.XPATH, "//button[contains(text(), '離開畫面')]")
                    close_btn.click()
                except:
                    pass

                time.sleep(1)

            except Exception as inner_e:
                print(f"內層爬蟲錯誤: {inner_e}", flush=True)
                # 這裡不返回錯誤，避免污染 JSON
                continue

        driver.quit()
    except Exception as e:
        print(f"主流程發生錯誤：{e}", flush=True)
        return []

    return results

# --- 整合函數 (修正後) ---
def scrape_and_process_to_json():
    """
    執行一次爬蟲任務，並將數據寫入 JSON 檔案。
    """
    current_timestamp = datetime.now()
    print(f"[{current_timestamp.strftime('%Y-%m-%d %H:%M:%S')}] 開始執行爬蟲與資料處理任務...", flush=True)
    
    try:
        # 1. 抓取原始資料
        raw_data = get_modal_info(dept_keywords=["中醫"]) # 🎯 這裡只針對中醫進行抓取，可依需求調整
        
        if not raw_data or not isinstance(raw_data, list):
            print(f"[{current_timestamp.strftime('%Y-%m-%d %H:%M:%S')}] ⚠️ 爬蟲返回數據無效或為空，任務跳過。", flush=True)
            return

        processed_clinics = {}
        
        for clinic_snapshot_list in raw_data:
            if not clinic_snapshot_list or not isinstance(clinic_snapshot_list, list) or not clinic_snapshot_list[0]:
                continue
            
            ref_clinic = clinic_snapshot_list[0]
            
            # 從原始快照中提取必要的靜態資訊
            clinic_key = f"{ref_clinic.get('department')}_{ref_clinic.get('clinic_room')}_{ref_clinic.get('doctor')}_{ref_clinic.get('time_slot')}"
            
            # 提取所有病人號碼 (sorted by number)
            all_numbers = sorted([p['number'] for p in ref_clinic.get("all_patients_status", []) if isinstance(p.get('number'), int)])
            
            processed_clinics[clinic_key] = {
                "current_number": ref_clinic.get("current_number", 0),
                "waiting": ref_clinic.get("waiting", 0),
                "completed": ref_clinic.get("completed", 0),
                "all_numbers_in_session": all_numbers, # 門診所有號碼列表 (用於 API 計算 sequence_in_session)
            }
            
        output_data = {
            "update_timestamp": current_timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "clinics": processed_clinics
        }

        # 2. 寫入 JSON
        with open(CLINIC_STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
            
        print(f"[{current_timestamp.strftime('%Y-%m-%d %H:%M:%S')}] 任務完成，已更新 {CLINIC_STATUS_FILE} (僅靜態特徵)", flush=True)
        
    except Exception as e:
        print(f"[{current_timestamp.strftime('%Y-%m-%d %H:%M:%S')}] 任務失敗: {e}", flush=True)


if __name__ == "__main__":
    # 首次執行
    scrape_and_process_to_json()
    
    # 排程每 1 分鐘執行一次
    schedule.every(1).minutes.do(scrape_and_process_to_json)
    
    print("背景爬蟲排程已啟動，將每分鐘更新一次看診進度...", flush=True)
    while True:
        schedule.run_pending()

        time.sleep(1)


