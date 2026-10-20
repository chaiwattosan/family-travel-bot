import os
import json
from datetime import datetime, timedelta
import pytz
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import threading
import time
import google.generativeai as genai

app = Flask(__name__)

# ดึงค่า Environment Variables
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
ADMIN_USER_ID = os.environ.get('ADMIN_USER_ID')
FAMILY_GROUP_ID = os.environ.get('FAMILY_GROUP_ID')
FAMILY_CALENDAR_ID = os.environ.get('FAMILY_CALENDAR_ID', 'primary')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_client = genai.GenerativeModel('gemini-2.5-flash')
else:
    gemini_client = None

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/calendar'
]

def get_google_credentials():
    creds_json = os.environ.get('GOOGLE_CREDENTIALS_JSON')
    if creds_json:
        creds_dict = json.loads(creds_json)
        return Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return Credentials.from_service_account_file('credentials.json', scopes=SCOPES)

def get_sheet():
    creds = get_google_credentials()
    client = gspread.authorize(creds)
    sheet_id = os.environ.get('GOOGLE_SHEET_ID')
    return client.open_by_key(sheet_id).worksheet('log')

def get_calendar_service():
    creds = get_google_credentials()
    return build('calendar', 'v3', credentials=creds)

def get_thai_time(timestamp_ms=None):
    tz = pytz.timezone('Asia/Bangkok')
    if timestamp_ms:
        dt = datetime.fromtimestamp(timestamp_ms / 1000.0, tz)
    else:
        dt = datetime.now(tz)
    return dt.strftime('%Y-%m-%d %H:%M:%S')

def get_user_display_name(user_id):
    # เพิ่มระบบลองดึงชื่อซ้ำ (Retry) ป้องกันจังหวะเซิร์ฟเวอร์ตื่นจากหลับ
    for attempt in range(3):
        try:
            profile = line_bot_api.get_profile(user_id)
            if profile and profile.display_name:
                return profile.display_name
        except Exception as e:
            print(f"Attempt {attempt+1} - Could not get profile name for {user_id}: {e}")
            time.sleep(1)
    return "สมาชิกในครอบครัว"

def analyze_text_with_gemini(text):
    if not gemini_client:
        return "บันทึกทั่วไป", text
    prompt = f"""
วิเคราะห์ข้อความต่อไปนี้สำหรับระบบจัดการครอบครัว:
"{text}"

โปรดจัดหมวดหมู่ข้อความนี้ให้อยู่ในประเภทใดประเภทหนึ่งดังต่อไปนี้เท่านั้น:
- อาหารการกิน
- สุขภาพ
- การเงิน
- การนัดหมาย/แจ้งเตือน
- บันทึกทั่วไป

และสรุปข้อความให้กระชับเข้าใจง่าย
ตอบกลับในรูปแบบ JSON ดังนี้:
{{
  "category": "หมวดหมู่ที่เลือก",
  "summary": "ข้อความสรุป"
}}
"""
    try:
        response = gemini_client.generate_content(prompt)
        text_resp = response.text.strip()
        if text_resp.startswith("```json"):
            text_resp = text_resp[7:-3].strip()
        data = json.loads(text_resp)
        return data.get("category", "บันทึกทั่วไป"), data.get("summary", text)
    except Exception as e:
        print(f"Gemini analysis error: {e}")
        return "บันทึกทั่วไป", text

def parse_calendar_intent(text):
    if not gemini_client:
        return None
    prompt = f"""
วิเคราะห์ข้อความนี้อย่างละเอียดว่าเป็น "คำสั่งหรือความต้องการให้นัดหมาย/บันทึกปฏิทินอย่างแท้จริง" หรือไม่: "{text}"

กฎเหล็กขั้นเด็ดขาดในการตัดสินใจ (is_calendar ต้องเป็น true ก็ต่อเมื่อผ่านทุกข้อ):
1. ข้อความต้องระบุทั้ง "วัน" (เช่น วันนี้, พรุ่งนี้, วันพุธ) และ "เวลาที่เจาะจง" (เช่น 06:45, 18:00 น.) อย่างชัดเจน
2. ข้อความนั้นต้องมีบริบทเชิง "การนัดหมาย, กำหนดการทางการ, หรือคำสั่งให้เตือน" (เช่น มีคำว่า นัด, เตือน, ไปพบ, ประชุม, หรือกำหนดเวลาทำธุระ) 
3. หากเป็นการพิมพ์พูดคุยสนทนาทั่วไป เล่าเรื่อง แชทตอบโต้กันในกลุ่ม หรือบอกเล่าแผนชีวิตประจำวันทั่วๆไป (เช่น "พรุ่งนี้กินข้าว 6.45", "รอดมาทำงาน", "พรุ่งนี้ออกบ้าน 7 โมง") โดยไม่มีเจตนาสั่งให้บันทึกนัดหมาย ให้ถือว่าไม่ใช่ (is_calendar: false ทันที)

ตอบกลับในรูปแบบ JSON เท่านั้น:
{{
  "is_calendar": true/false,
  "summary": "หัวข้อเรื่อง",
  "start_time": "YYYY-MM-DDTHH:MM:SS",
  "reminder_minutes": 30
}}
"""
    try:
        response = gemini_client.generate_content(prompt)
        text_resp = response.text.strip()
        if text_resp.startswith("```json"):
            text_resp = text_resp[7:-3].strip()
        return json.loads(text_resp)
    except Exception as e:
        print(f"Calendar intent parse error: {e}")
        return None

def add_to_calendar(summary, start_time_str, reminder_mins=10):
    try:
        service = get_calendar_service()
        tz = pytz.timezone('Asia/Bangkok')
        start_dt = datetime.fromisoformat(start_time_str)
        if start_dt.tzinfo is None:
            start_dt = tz.localize(start_dt)
        end_dt = start_dt + timedelta(hours=1)

        event = {
            'summary': summary,
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Bangkok'},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Bangkok'},
            'reminders': {
                'useDefault': False,
                'overrides': [{'method': 'popup', 'minutes': reminder_mins}]
            }
        }
        
        cal_id = os.environ.get('FAMILY_CALENDAR_ID', 'primary')
        event_result = service.events().insert(calendarId=cal_id, body=event).execute()
        return event_result
    except Exception as e:
        print(f"Error adding to calendar: {e}")
        return None

def get_family_calendar_events():
    try:
        service = get_calendar_service()
        now = datetime.now(pytz.timezone('Asia/Bangkok')).isoformat()
        cal_id = os.environ.get('FAMILY_CALENDAR_ID', 'primary')
        
        events_result = service.events().list(
            calendarId=cal_id,
            timeMin=now,
            maxResults=20,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        return events_result.get('items', [])
    except Exception as e:
        print(f"Error getting calendar events: {e}")
        return []

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    user_id = event.source.user_id
    sender_name = get_user_display_name(user_id)
    msg_text = event.message.text
    timestamp = event.timestamp
    thai_time = get_thai_time(timestamp)

    # ตรวจสอบนัดหมายเฉพาะเมื่อเข้าเงื่อนไขชัดเจน
    cal_intent = parse_calendar_intent(msg_text)
    if cal_intent and cal_intent.get("is_calendar"):
        summary = cal_intent.get("summary", "นัดหมายครอบครัว")
        start_time = cal_intent.get("start_time")
        reminder_mins = cal_intent.get("reminder_minutes", 10)
        
        if start_time:
            res = add_to_calendar(summary, start_time, reminder_mins)
            if res:
                reply_text = (
                    f"📅 **บันทึกนัดหมายลงปฏิทินครอบครัวสำเร็จ!**\n"
                    f"📌 หัวข้อ: {summary}\n"
                    f"⏰ เริ่ม: {start_time}\n"
                    f"🔔 แจ้งเตือนล่วงหน้า: {reminder_mins} นาที"
                )
            else:
                reply_text = "❌ เกิดข้อผิดพลาดในการบันทึกปฏิทิน"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

    category, summary = analyze_text_with_gemini(msg_text)
    
    # เพิ่มระบบ Retry สำหรับบันทึกลง Google Sheets ป้องกันจังหวะ Cold Start
    for attempt in range(3):
        try:
            sheet = get_sheet()
            sheet.append_row([thai_time, sender_name, category, "-", summary])
            break
        except Exception as e:
            print(f"Attempt {attempt+1} - Error saving to Google Sheets: {e}")
            time.sleep(1)

def background_calendar_reminder_worker():
    notified_events = set()
    while True:
        try:
            time.sleep(60)
            events = get_family_calendar_events()
            now = datetime.now(pytz.timezone('Asia/Bangkok'))
            
            for event in events:
                start_str = event.get('start', {}).get('dateTime')
                if not start_str:
                    continue
                
                event_id = event.get('id')
                event_start = datetime.fromisoformat(start_str)
                
                reminders_setting = event.get('reminders', {}).get('overrides', [{'minutes': 30}])
                reminder_mins = reminders_setting[0].get('minutes', 30) if reminders_setting else 30
                
                trigger_time = event_start - timedelta(minutes=reminder_mins)
                time_diff = (now - trigger_time).total_seconds()
                
                if 0 <= time_diff < 900 and event_id not in notified_events:
                    summary = event.get('summary', 'ไม่มีชื่อนัดหมาย')
                    start_time_formatted = event_start.strftime('%H:%M น.')
                    
                    alert_msg = (
                        f"🚨 **แจ้งเตือนนัดหมายครอบครัว!**\n\n"
                        f"📌 เรื่อง: {summary}\n"
                        f"⏰ เวลาเริ่ม: {start_time_formatted}\n"
                        f"🔔 (แจ้งเตือนล่วงหน้า {reminder_mins} นาที)"
                    )
                    
                    if FAMILY_GROUP_ID and FAMILY_GROUP_ID.startswith("C"):
                        line_bot_api.push_message(FAMILY_GROUP_ID, TextSendMessage(text=alert_msg))
                    elif ADMIN_USER_ID:
                        line_bot_api.push_message(ADMIN_USER_ID, TextSendMessage(text=alert_msg))
                    
                    notified_events.add(event_id)
        except Exception as e:
            print(f"Error in background reminder worker: {e}")

worker_thread = threading.Thread(target=background_calendar_reminder_worker, daemon=True)
worker_thread.start()

@app.route("/")
def index():
    return "Family Travel & Kitchen Bot is running!"

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
