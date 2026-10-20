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

    sheet_json_str = json.dumps(sheet_data, ensure_ascii=False)
    calendar_json_str = json.dumps(calendar_events, ensure_ascii=False)

    prompt = (
        "คุณคือ AI ผู้ช่วยอัจฉริยะของระบบครอบครัว (Family Bot) และระบบทำหน้าที่สรุปข้อมูล (Daily Summary Agent)\n"
        "หน้าที่ของคุณคือตรวจสอบข้อความของผู้ใช้ โดยอ้างอิงจากข้อมูลใน Google Sheet และข้อมูลนัดหมายจากปฏิทินครอบครัวที่ให้ไว้อย่างเคร่งครัด\n\n"
        "ข้อมูลนัดหมายจากปฏิทินครอบครัว (Google Calendar):\n" + calendar_json_str + "\n\n"
        "ข้อมูลใน Google Sheet:\n" + sheet_json_str + "\n\n"
        f"ข้อความจากผู้ใช้: \"{user_message}\"\n\n"
        "กฎเหล็กสำหรับการค้นหาและการสรุปผล (Strict Rules):\n"
        "1. หากผู้ใช้ถามถึงกำหนดการ นัดหมาย หรือตารางเวลา ให้ดึงข้อมูลจากข้อมูลนัดหมายจากปฏิทินครอบครัวมาตอบให้ถูกต้องและตรงประเด็นที่สุด\n"
        "2. หากผู้ใช้สั่งสรุปข้อมูลประจำวัน ให้รวบรวมเหตุการณ์ ข้อความสนทนา อาการป่วย และรูปภาพทั้งหมดมาสรุปแยกตาม 6 หมวดหมู่ พร้อมระบุเวลาและชื่อผู้ส่งให้ชัดเจน\n"
        "3. หากเป็นการพูดคุยทั่วไป ให้เลือก action: \"SAVE\"\n\n"
        "ตอบกลับเป็น JSON Object เท่านั้น:\n"
        "{\n"
        '  "action": "FIND_IMAGE" หรือ "SUMMARIZE" หรือ "SAVE",\n'
        '  "image_url": "URL หรือ null",\n'
        '  "text_reply": "ข้อความตอบกลับผู้ใช้"\n'
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
        print(f"Error querying Gemini: {e}")
        return {"action": "SAVE"}

def save_text_to_google_sheets(date_str, user_name, text_content, category):
    try:
        sheet = get_sheet()
        row = [date_str, user_name, category, "-", text_content, "", "", ""]
        sheet.append_row(row, value_input_option='USER_ENTERED')
        print("Successfully saved text message to Google Sheets!")

        if ADMIN_USER_ID:
            notification_text = (
                f"📝 **[Train] แจ้งเตือนข้อความใหม่**\n\n"
                f"👤 ผู้ส่ง: {user_name}\n"
                f"📂 หมวดหมู่: {category}\n"
                f"💬 ข้อความ: {text_content}\n"
                f"⏰ เวลา: {date_str}"
            )
            line_bot_api.push_message(
                ADMIN_USER_ID,
                TextSendMessage(text=notification_text)
            )
    except Exception as e:
        print(f"Error saving text to Google Sheets: {e}")

def save_to_google_sheets(date_str, user_name, category, location, detail, image_url):
    try:
        sheet = get_sheet()
        preview_formula = f'=IMAGE("{image_url}", 4, 100, 100)' if image_url else ""
        row = [date_str, user_name, category, location, detail, "", preview_formula, image_url]
        sheet.append_row(row, value_input_option='USER_ENTERED')
        print("Successfully saved image to Google Sheets!")

        if ADMIN_USER_ID:
            notification_text = (
                f"🚨 **แจ้งเตือนการบันทึกภาพใหม่**\n\n"
                f"👤 ผู้บันทึก: {user_name}\n"
                f"📂 หมวดหมู่: {category}\n"
                f"📍 สถานที่: {location}\n"
                f"📝 รายละเอียด: {detail}\n"
                f"⏰ เวลา: {date_str}"
            )
            line_bot_api.push_message(
                ADMIN_USER_ID,
                TextSendMessage(text=notification_text)
            )
    except Exception as e:
        print(f"Error saving to Google Sheets or sending notification: {e}")
        raise e

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
                if 0 <= time_diff < 70 and event_id not in notified_events:
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
    return "Family Travel Bot is running!"

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
    user_id = event.source.user_id
    chat_type = event.source.type
    user_message = event.message.text.strip()
    date_str = get_thai_time()
    user_name = get_user_display_name(user_id)

    global FAMILY_GROUP_ID
    if chat_type in ["group", "room"]:
        FAMILY_GROUP_ID = event.source.group_id if chat_type == "group" else event.source.room_id

    try:
        sheet = get_sheet()
        records = sheet.get_all_values()
    except Exception:
        records = []
        
    classified = classify_text_message(user_message, records)
    category = classified.get("category", "บันทึกทั่วไป")
    detail = classified.get("detail", user_message)

    save_text_to_google_sheets(date_str, user_name, detail, category)

    calendar_event = extract_calendar_event(user_message, date_str)
    if calendar_event:
        success = create_google_calendar_event(calendar_event)
        if success:
            success_msg = f"📅 **บันทึกนัดหมายลงปฏิทินครอบครัวสำเร็จ!**\n📌 หัวข้อ: {calendar_event.get('summary')}\n⏰ เริ่ม: {calendar_event.get('start_time')}\n🔔 แจ้งเตือนล่วงหน้า: {calendar_event.get('reminder_minutes')} นาที"
            if chat_type in ["group", "room"]:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=success_msg))
            elif ADMIN_USER_ID:
                line_bot_api.push_message(ADMIN_USER_ID, TextSendMessage(text=success_msg))
            return

    is_command = user_message.startswith("@Bot") or "สรุป" in user_message or "มีนัด" in user_message or "อะไร" in user_message

    if not is_command:
        return

    clean_message = user_message.replace("@Bot", "").strip()

    calendar_events = get_family_calendar_events()
    analysis = ask_gemini_agent(clean_message, records, calendar_events)
    action = analysis.get("action", "SAVE")

    if action == "FIND_IMAGE":
        found_url = analysis.get("image_url")
        reply_txt = analysis.get("text_reply", "ไม่พบรูปภาพที่ค้นหาครับ")

        if found_url and str(found_url).startswith("https://res.cloudinary.com"):
            line_bot_api.reply_message(
                event.reply_token,
                [
                    TextSendMessage(text=reply_txt),
                    ImageSendMessage(original_content_url=found_url, preview_image_url=found_url)
                ]
            )
            return
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_txt if reply_txt else "ไม่พบรูปภาพตามที่ระบุในบันทึกครับ")
            )
            return

    elif action == "SUMMARIZE":
        reply_txt = analysis.get("text_reply", "สรุปข้อมูลการสนทนาวันนี้")
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_txt)
        )
        return

    reply_txt = analysis.get("text_reply", "รับทราบครับ")
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_txt)
    )
    return

@handler.add(MessageEvent, message=StickerMessage)
def handle_sticker_message(event):
    user_id = event.source.user_id
    date_str = get_thai_time()
    user_name = get_user_display_name(user_id)
    
    try:
        sheet = get_sheet()
        row = [date_str, user_name, "บันทึกทั่วไป", "-", "(ส่งสติกเกอร์ LINE)", "", "", ""]
        sheet.append_row(row, value_input_option='USER_ENTERED')
        print("Successfully saved sticker message to Google Sheets!")
    except Exception as e:
        print(f"Error saving sticker to Google Sheets: {e}")

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    user_id = event.source.user_id
    date_str = get_thai_time()
    user_name = get_user_display_name(user_id)

    unique_filename = f"{event.message.id}_{uuid.uuid4().hex[:8]}.jpg"
    image_path = os.path.join("/tmp", unique_filename)

    try:
        message_content = line_bot_api.get_message_content(event.message.id)
        with open(image_path, 'wb') as fd:
            for chunk in message_content.iter_content():
                fd.write(chunk)
    except Exception as e:
        print(f"Error downloading image content: {e}")
        return

    try:
        upload_result = cloudinary.uploader.upload(image_path, folder="family_bot")
        image_url = upload_result.get('secure_url')
    except Exception as e:
        print(f"Cloudinary upload error: {e}")
        if os.path.exists(image_path):
            os.remove(image_path)
        return

    if os.path.exists(image_path):
        os.remove(image_path)

    analysis = analyze_image_with_gemini(image_path if os.path.exists(image_path) else None)
    category = analysis.get("category", "บันทึกทั่วไป")
    location = analysis.get("location", "-")
    detail = analysis.get("detail", "บันทึกภาพถ่ายครอบครัว")

    try:
        save_to_google_sheets(date_str, user_name, category, location, detail, image_url)
    except Exception as e:
        print(f"Failed to save and notify: {e}")
        return

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
