import os
import json
import time
from datetime import datetime, timedelta
import pytz
import threading
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from google import genai

app = Flask(__name__)

# ตั้งค่า Environment Variables
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
ADMIN_USER_ID = os.environ.get('ADMIN_USER_ID')
FAMILY_GROUP_ID = os.environ.get('FAMILY_GROUP_ID')
FAMILY_CALENDAR_ID = os.environ.get('FAMILY_CALENDAR_ID', 'primary')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

if GEMINI_API_KEY:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
else:
    gemini_client = None

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/calendar'
]

def get_google_creds():
    creds_json = os.environ.get('GOOGLE_CREDENTIALS_JSON')
    if creds_json:
        creds_dict = json.loads(creds_json)
        return Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return None

def get_sheet():
    creds = get_google_creds()
    client = gspread.authorize(creds)
    sheet_id = os.environ.get('GOOGLE_SHEET_ID')
    return client.open_by_key(sheet_id).worksheet("log")

def get_calendar_service():
    creds = get_google_creds()
    return build('calendar', 'v3', credentials=creds)

def get_thai_time(timestamp_ms):
    tz = pytz.timezone('Asia/Bangkok')
    dt = datetime.fromtimestamp(timestamp_ms / 1000.0, tz)
    return dt.strftime('%Y-%m-%d %H:%M:%S')

def get_user_display_name(user_id):
    for attempt in range(3):
        try:
            profile = line_bot_api.get_profile(user_id)
            return profile.display_name
        except Exception as e:
            print(f"Attempt {attempt+1} - Error fetching profile: {e}")
            time.sleep(1)
    return "Unknown User"

def analyze_text_with_gemini(text):
    if not gemini_client:
        return "บันทึกทั่วไป", text
    prompt = f"""
วิเคราะห์ข้อความนี้: "{text}"
จัดหมวดหมู่ให้อยู่ในประเภทใดประเภทหนึ่งต่อไปนี้เท่านั้น:
- อาหาร/เครื่องดื่ม
- ซื้อของ/ใช้จ่าย
- การเดินทาง
- บันทึกทั่วไป

ตอบกลับในรูปแบบ: [หมวดหมู่] | [ข้อความสรุปสั้นๆ]
"""
    try:
        # อัปเดตใช้โมเดลล่าสุดตามคำแนะนำของ Google API
        response = gemini_client.models.generate_content(
            model='gemini-3.8-flash',
            contents=prompt,
        )
        parts = response.text.split("|")
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
        return "บันทึกทั่วไป", text
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
3. หากเป็นการพิมพ์พูดคุยสนทนาทั่วไป เล่าเรื่อง แชทตอบโต้กันในกลุ่ม หรือบอกเล่าแผนชีวิตประจำวันทั่วๆไป โดยไม่มีเจตนาสั่งให้บันทึกนัดหมาย ให้ถือว่าไม่ใช่ (is_calendar: false ทันที)

ตอบกลับในรูปแบบ JSON เท่านั้น:
{{
  "is_calendar": true/false,
  "summary": "หัวข้อเรื่อง",
  "start_time": "YYYY-MM-DDTHH:MM:SS",
  "reminder_minutes": 30
}}
"""
    try:
        response = gemini_client.models.generate_content(
            model='gemini-3.8-flash',
            contents=prompt,
        )
        text_resp = response.text.strip()
        if text_resp.startswith("```json"):
            text_resp = text_resp[7:-3].strip()
        return json.loads(text_resp)
    except Exception as e:
        print(f"Calendar intent parse error: {e}")
        return None

def add_to_calendar(summary, start_time_str, reminder_minutes=30):
    try:
        service = get_calendar_service()
        tz = pytz.timezone('Asia/Bangkok')
        start_dt = tz.localize(datetime.strptime(start_time_str, '%Y-%m-%dT%H:%M:%S'))
        end_dt = start_dt + timedelta(hours=1)

        event = {
            'summary': summary,
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Bangkok'},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Bangkok'},
            'reminders': {
                'useDefault': False,
                'overrides': [{'method': 'popup', 'minutes': reminder_minutes}],
            },
        }
        event_result = service.events().insert(calendarId=FAMILY_CALENDAR_ID, body=event).execute()
        return event_result.get('htmlLink')
    except Exception as e:
        print(f"Error adding to calendar: {e}")
        return None

def background_calendar_reminder_worker():
    notified_events = set()
    while True:
        try:
            time.sleep(60)
            service = get_calendar_service()
            tz = pytz.timezone('Asia/Bangkok')
            now = datetime.now(tz)
            later = now + timedelta(minutes=35)

            events_result = service.events().list(
                calendarId=FAMILY_CALENDAR_ID,
                timeMin=now.isoformat(),
                timeMax=later.isoformat(),
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            events = events_result.get('items', [])

            for event in events:
                event_id = event['id']
                if event_id in notified_events:
                    continue
                
                start_str = event['start'].get('dateTime') or event['start'].get('date')
                if 'T' not in start_str:
                    continue
                
                start_dt = datetime.fromisoformat(start_str).astimezone(tz)
                diff_minutes = (start_dt - now).total_seconds() / 60

                if 0 <= diff_minutes <= 30:
                    summary = event.get('summary', 'ไม่มีหัวข้อ')
                    time_formatted = start_dt.strftime('%H:%M น.')
                    
                    alert_text = (
                        f"⏰ **แจ้งเตือนกำหนดการใกล้ถึงเวลา!**\n"
                        f"📌 เรื่อง: {summary}\n"
                        f"🕒 เวลา: {time_formatted}"
                    )
                    
                    target_id = FAMILY_GROUP_ID if FAMILY_GROUP_ID else ADMIN_USER_ID
                    if target_id:
                        line_bot_api.push_message(target_id, TextSendMessage(text=alert_text))
                    
                    notified_events.add(event_id)
        except Exception as e:
            print(f"Reminder worker error: {e}")

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    try:
        user_id = event.source.user_id
        sender_name = get_user_display_name(user_id)
        msg_text = event.message.text
        timestamp = event.timestamp
        thai_time = get_thai_time(timestamp)

        # 1. ตรวจสอบนัดหมายปฏิทิน
        cal_intent = parse_calendar_intent(msg_text)
        if cal_intent and cal_intent.get("is_calendar"):
            summary = cal_intent.get("summary", "นัดหมายครอบครัว")
            start_time = cal_intent.get("start_time")
            reminder_mins = cal_intent.get("reminder_minutes", 30)
            
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
                return

        # 2. บันทึกข้อความทั่วไปลง Google Sheets
        category, summary_text = analyze_text_with_gemini(msg_text)
        for attempt in range(3):
            try:
                sheet = get_sheet()
                sheet.append_row([thai_time, sender_name, category, "-", summary_text])
                break
            except Exception as e:
                print(f"Attempt {attempt+1} - Error saving to Google Sheets: {e}")
                time.sleep(1)

        # 3. ส่ง Log รายงานส่วนตัวหา Admin
        if ADMIN_USER_ID:
            admin_msg = (
                f"📝 **[Train] แจ้งเตือนข้อความใหม่**\n\n"
                f"👤 ผู้ส่ง: {sender_name}\n"
                f"📁 หมวดหมู่: {category}\n"
                f"💬 ข้อความ: {summary_text}\n"
                f"⏰ เวลา: {thai_time}"
            )
            try:
                line_bot_api.push_message(ADMIN_USER_ID, TextSendMessage(text=admin_msg))
            except Exception as e:
                print(f"Error sending admin log: {e}")

    except Exception as e:
        print(f"Error handling message: {e}")

if __name__ == "__main__":
    reminder_thread = threading.Thread(target=background_calendar_reminder_worker, daemon=True)
    reminder_thread.start()
    
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
