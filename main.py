import os
import json
import uuid
from datetime import datetime, timedelta
import pytz
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, StickerMessage, TextSendMessage, ImageSendMessage
import gspread
from google.oauth2.service_account import Credentials
from google import genai
from google.genai import types
import cloudinary
import cloudinary.uploader
from googleapiclient.discovery import build
import threading
import time

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

ADMIN_USER_ID = "U4408dd0ab71b56db6074273eb28651b0"
FAMILY_GROUP_ID = "C..."
FAMILY_CALENDAR_ID = "472254e055528125304e1d3cb80cf50f8cab156a67d12c7eda0c94dfb0c02791@group.calendar.google.com"

cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME'),
    api_key=os.environ.get('CLOUDINARY_API_KEY'),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET')
)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

gemini_client = None
if GEMINI_API_KEY:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
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
    return client.open_by_key(sheet_id).sheet1

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
    try:
        profile = line_bot_api.get_profile(user_id)
        if profile and profile.display_name:
            return profile.display_name
    except Exception as e:
        print(f"Could not get profile name for {user_id}: {e}")
    return "สมาชิกในครอบครัว"

def analyze_image_with_gemini(image_path):
    if not gemini_client:
        return {"category": "บันทึกทั่วไป", "location": "-", "detail": "ส่งรูปภาพบันทึกครอบครัว"}

    prompt = (
        "วิเคราะห์รูปภาพนี้สำหรับบันทึกครอบครัว ตอบเป็น JSON Object เท่านั้น:\n"
        "{\n"
        '  "category": "เลือกหมวดหมู่ให้แม่นยำที่สุดจาก 6 หมวดนี้เท่านั้น: อาหารการกิน, สุขภาพ, การเดินทาง, การนัดหมาย/แจ้งเตือน, กิจกรรม, บันทึกทั่วไป",\n'
        '  "location": "สถานที่หรือร้าน/ที่พัก (ถ้าไม่ทราบใส่ \'-\')",\n'
        '  "detail": "คำอธิบายสิ่งที่เห็นในรูป เช่น อาหารที่ทาน, แผนที่เส้นทาง, หรือบุคคลในภาพ"\n'
        "}"
    )
    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()

        response = gemini_client.models.generate_content(
            model='gemini-3.5-flash-lite',
            contents=[
                prompt,
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"Error analyzing image with Gemini: {e}")
        return {"category": "บันทึกทั่วไป", "location": "-", "detail": "ส่งรูปภาพบันทึกครอบครัว"}

def classify_text_message(text_message, sheet_data):
    if not gemini_client:
        return {"category": "บันทึกทั่วไป", "detail": text_message}

    prompt = (
        "คุณคือ AI ผู้ช่วยอัจฉริยะของระบบครอบครัว (Family Bot)\n"
        "หน้าที่ของคุณคืออ่านข้อความทุกรูปแบบที่สมาชิกในครอบครัวพิมพ์คุยกัน แล้วจัดหมวดหมู่ให้ตรงกับ 1 ใน 6 หมวดนี้เท่านั้น:\n"
        "1. อาหารการกิน (เช่น พูดคุยเรื่องเมนูอาหาร, ของกิน, ร้านอาหาร)\n"
        "2. สุขภาพ (เช่น อาการเจ็บป่วย, อาการผิดปกติของร่างกาย เช่น หมดแรง, หน้ามืด, ใจสั่น, ไปหาหมอ, ทานยา)\n"
        "3. การเดินทาง (เช่น เส้นทาง, การเดินทางกลับ, แผนที่, รถไฟ)\n"
        "4. การนัดหมาย/แจ้งเตือน (เช่น นัดหมายล่วงหน้า, เตือนเปิดไฟ, แจ้งเตือนเวลาต่างๆ, ฝากเตือนงานบ้าน)\n"
        "5. กิจกรรม (เช่น ทริปท่องเที่ยว, งานเทศกาล, งานสังสรรค์)\n"
        "6. บันทึกทั่วไป (ข้อความพูดคุยทักทาย หรือเรื่องอื่นๆ ทั่วไป)\n\n"
        f"ข้อความที่ผู้ใช้พิมพ์: \"{text_message}\"\n\n"
        "ตอบกลับเป็น JSON Object เท่านั้น:\n"
        "{\n"
        '  "category": "เลือกหนึ่งใน 6 หมวดหมู่ข้างต้น",\n'
        '  "detail": "สรุปเนื้อหาหรือข้อความนั้นให้กระชับและเข้าใจง่าย"\n'
        "}"
    )
    try:
        response = gemini_client.models.generate_content(
            model='gemini-3.5-flash-lite',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"Error classifying text: {e}")
        return {"category": "บันทึกทั่วไป", "detail": text_message}

def extract_calendar_event(user_message, current_time_str):
    if not gemini_client:
        return None

    prompt = (
        "คุณคือ AI ผู้ช่วยจัดการนัดหมายของครอบครัว ปัจจุบันเวลาอ้างอิงคือ: " + current_time_str + "\n"
        "จงตรวจสอบข้อความของผู้ใช้: \"" + user_message + "\"\n\n"
        "วิเคราะห์ว่าข้อความนี้มีเจตนา \"นัดหมาย\" หรือ \"ตั้งเตือนเวลาทำกิจกรรม\" หรือไม่\n"
        "- หากใช่: ให้แปลงเป็น JSON Object โดยดึงข้อมูลดังนี้:\n"
        "  - summary: หัวข้อสั้นๆ ของนัดหมาย โดยตัดข้อความสั่งแจ้งเตือนออก\n"
        "  - start_time: วันที่และเวลาเริ่มต้น (รูปแบบ ISO 8601 เช่น YYYY-MM-DDTHH:MM:00) ถ้าไม่ระบุเวลาให้กำหนดเป็น 09:00:00\n"
        "  - end_time: วันที่และเวลาสิ้นสุด (ถ้าไม่มีกำหนด ให้บวกเพิ่มจาก start_time ไป 1 ชั่วโมง)\n"
        "  - reminder_minutes: จำนวนนาทีที่ต้องการให้แจ้งเตือนล่วงหน้า (เช่น ก่อน 3 นาที = 3, ก่อน 5 นาที = 5, ก่อน 15 นาที = 15, ก่อน 30 นาที = 30 ถ้าไม่ระบุให้ค่าเป็น 30)\n"
        "  - description: รายละเอียดเพิ่มเติมอื่นๆ\n"
        "- หากไม่ใช่: ให้ตอบกลับคำว่า null\n\n"
        "ตอบกลับเป็น JSON Object เท่านั้น (หรือ null):\n"
        "{\n"
        '  "summary": "...",\n'
        '  "start_time": "YYYY-MM-DDTHH:MM:00",\n'
        '  "end_time": "YYYY-MM-DDTHH:MM:00",\n'
        '  "reminder_minutes": 30,\n'
        '  "description": "..."\n'
        "}"
    )
    try:
        response = gemini_client.models.generate_content(
            model='gemini-3.5-flash-lite',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        result_text = response.text.strip()
        print(f"Gemini Calendar Extraction Result: {result_text}")
        
        if "null" in result_text.lower() or "{" not in result_text:
            return None
            
        return json.loads(result_text)
    except Exception as e:
        print(f"Error extracting calendar event: {e}")
        return None

def create_google_calendar_event(event_data):
    try:
        service = get_calendar_service()
        reminder_mins = int(event_data.get('reminder_minutes', 30))
        event = {
            'summary': event_data.get('summary'),
            'description': event_data.get('description', 'บันทึกโดย Family Bot'),
            'start': {
                'dateTime': event_data.get('start_time'),
                'timeZone': 'Asia/Bangkok',
            },
            'end': {
                'dateTime': event_data.get('end_time'),
                'timeZone': 'Asia/Bangkok',
            },
            'reminders': {
                'useDefault': False,
                'overrides': [
                    {'method': 'popup', 'minutes': reminder_mins},
                    {'method': 'email', 'minutes': reminder_mins}
                ],
            },
        }
        created_event = service.events().insert(calendarId=FAMILY_CALENDAR_ID, body=event, sendUpdates='all').execute()
        print(f"Event created successfully: {created_event.get('htmlLink')}")
        return True
    except Exception as e:
        print(f"Error creating Google Calendar event: {e}")
        return False

def get_family_calendar_events():
    try:
        service = get_calendar_service()
        now = datetime.now(pytz.timezone('Asia/Bangkok'))
        time_min = now.isoformat()
        events_result = service.events().list(
            calendarId=FAMILY_CALENDAR_ID,
            timeMin=time_min,
            maxResults=20,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        return events_result.get('items', [])
    except Exception as e:
        print(f"Error fetching calendar events: {e}")
        return []

def ask_gemini_agent(user_message, sheet_data, calendar_events):
    if not gemini_client:
        return {"action": "SAVE"}

    sheet_json_str
