import os
import json
import uuid
from datetime import datetime, timedelta
import pytz
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, TextSendMessage, ImageSendMessage
import gspread
from google.oauth2.service_account import Credentials
from google import genai
from google.genai import types
import cloudinary
import cloudinary.uploader
from googleapiclient.discovery import build

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

ADMIN_USER_ID = "U4408dd0ab71b56db6074273eb28651b0"

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

def get_thai_time(timestamp_ms):
    tz = pytz.timezone('Asia/Bangkok')
    dt = datetime.fromtimestamp(timestamp_ms / 1000.0, tz)
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

    prompt = f"""
คุณคือ AI ผู้ช่วยอัจฉริยะของระบบครอบครัว (Family Bot)
หน้าที่ของคุณคืออ่านข้อความที่สมาชิกในครอบครัวพิมพ์คุยกัน แล้วจัดหมวดหมู่ให้ตรงกับ 1 ใน 6 หมวดนี้เท่านั้น:
1. อาหารการกิน (เช่น พูดคุยเรื่องเมนูอาหาร, ของกิน, ร้านอาหาร)
2. สุขภาพ (เช่น อาการเจ็บป่วย, อาการผิดปกติของร่างกาย เช่น หมดแรง, หน้ามืด, ใจสั่น, ไปหาหมอ, ทานยา)
3. การเดินทาง (เช่น เส้นทาง, การเดินทางกลับ, แผนที่, รถไฟ)
4. การนัดหมาย/แจ้งเตือน (เช่น นัดหมายล่วงหน้า, เตือนเปิดไฟ, แจ้งเตือนเวลาต่างๆ)
5. กิจกรรม (เช่น ทริปท่องเที่ยว, งานเทศกาล, งานสังสรรค์)
6. บันทึกทั่วไป (ข้อความพูดคุยทักทาย หรือเรื่องอื่นๆ ทั่วไป)

ข้อความที่ผู้ใช้พิมพ์: "{text_message}"

ตอบกลับเป็น JSON Object เท่านั้น:
{{
  "category": "เลือกหนึ่งใน 6 หมวดหมู่ข้างต้น",
  "detail": "สรุปเนื้อหาหรือข้อความนั้นให้กระชับและเข้าใจง่าย"
}}
"""
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

    prompt = f"""
คุณคือ AI ผู้ช่วยจัดการนัดหมายของครอบครัว ปัจจุบันเวลาอ้างอิงคือ: "{current_time_str}"
จงตรวจสอบข้อความของผู้ใช้: "{user_message}"
หากข้อความนี้เป็นการนัดหมาย หรือการแจ้งเตือนเวลา (เช่น นัดพบหมอ, พรุ่งนี้ล้อหมุน 7 โมง, วันจันทร์น้ำมาส่ง, อย่าลืมเปิดไฟทุ่มนึง) ให้ดึงข้อมูลออกมาเป็น JSON Object สำหรับสร้างปฏิทิน:
- summary: หัวข้อการนัดหมาย/แจ้งเตือน
- start_time: วันที่และเวลาเริ่มต้น (รูปแบบ ISO 8601 เช่น YYYY-MM-DDTHH:MM:00) ถ้าไม่ระบุเวลาให้กำหนดเป็น 08:00:00
- end_time: วันที่และเวลาสิ้นสุด (ถ้าไม่มีกำหนด ให้บวกเพิ่มจาก start_time ไป 1 ชั่วโมง)
- description: รายละเอียดเพิ่มเติม

หากข้อความนี้ "ไม่ใช่" นัดหมายหรือการแจ้งเตือน ให้ตอบกลับค่า null ทันที

ตอบกลับเป็น JSON Object เท่านั้น (หรือ null):
{{
  "summary": "...",
  "start_time": "YYYY-MM-DDTHH:MM:00",
  "end_time": "YYYY-MM-DDTHH:MM:00",
  "description": "..."
}}
"""
    try:
        response = gemini_client.models.generate_content(
            model='gemini-3.5-flash-lite',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        result_text = response.text.strip()
        if "null" in result_text.lower():
            return None
        return json.loads(result_text)
    except Exception as e:
        print(f"Error extracting calendar event: {e}")
        return None

def create_google_calendar_event(event_data):
    try:
        service = get_calendar_service()
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
                    {'method': 'popup', 'minutes': 1440},  # แจ้งเตือนล่วงหน้า 1 วัน
                    {'method': 'popup', 'minutes': 30},   # แจ้งเตือนก่อนเวลา 30 นาที
                ],
            },
        }
        # กำหนด Calendar ID ของครอบครัวที่คุณสร้างขึ้น
        calendar_id = '472254e055528125304e1d3cb80cf50f8cab156a67d12c7eda0c94dfb0c02791@group.calendar.google.com'
        created_event = service.events().insert(calendarId=calendar_id, body=event).execute()
        print(f"Event created: {created_event.get('htmlLink')}")
        return True
    except Exception as e:
        print(f"Error creating Google Calendar event: {e}")
        return False

def ask_gemini_agent(user_message, sheet_data):
    if not gemini_client:
        return {"action": "SAVE"}

    prompt = f"""
คุณคือ AI ผู้ช่วยอัจฉริยะของระบบครอบครัว (Family Bot) และระบบทำหน้าที่สรุปข้อมูล (Daily Summary Agent)
หน้าที่ของคุณคือตรวจสอบข้อความของผู้ใช้ โดยอ้างอิงจากข้อมูลใน Google Sheet ที่ให้ไว้อย่างเคร่งครัด

ข้อมูลใน Google Sheet (เรียงตามลำดับแถว โดยคอลัมน์ A คือ วันที่/เวลา, คอลัมน์ B คือ ผู้ส่ง, คอลัมน์ C คือ หมวดหมู่, คอลัมน์ D คือ สถานที่, คอลัมน์ E คือ รายละเอียด):
{json.dumps(sheet_data, ensure_ascii=False, indent=2)}

ข้อความจากผู้ใช้: "{user_message}"

กฎเหล็กสำหรับการค้นหาและการสรุปผล (Strict Rules):
1. คำว่า "อากง", "เชฟเทียน" และ "คุณพ่อ" หมายถึงบุคคลเดียวกัน ให้เชื่อมโยงความหมายนี้ในการค้นหาข้อมูลเสมอ
2. หากผู้ใช้สั่งสรุปข้อมูลประจำวัน (เช่น พิมพ์ว่า "สรุปวันนี้ให้หน่อย", "วันนี้มีอะไรบ้าง") ให้รวบรวมเหตุการณ์ ข้อความสนทนา อาการป่วย และรูปภาพทั้งหมดที่เกิดขึ้นใน "วันปัจจุบัน" มาสรุปเป็นประเด็นสั้นๆ กระชับ แยกตาม 6 หมวดหมู่ พร้อมระบุเวลาและชื่อผู้ส่งให้ชัดเจน
3. หากผู้ใช้ถามหารูปภาพมื้ออาหารตามเวลา ให้เทียบเคียงช่วงเวลาที่โพสต์ให้ถูกต้อง (เช้า 06:00-10:00, เที่ยง 10:00-14:00, เย็น 15:00 เป็นต้นไป) และระบุเวลาที่โพสต์กำกับ
4. หากเป็นการพูดคุยทั่วไปของคนในกลุ่ม ให้เลือก action: "SAVE" ทันที

ตอบกลับเป็น JSON Object เท่านั้น:
{{
  "action": "FIND_IMAGE" 혹은 "SUMMARIZE" 혹은 "SAVE",
  "image_url": "URL หรือ null",
  "text_reply": "ข้อความตอบกลับผู้ใช้"
}}
"""
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
    date_str = get_thai_time(event.timestamp)
    user_name = get_user_display_name(user_id)

    is_command = user_message.startswith("@Bot") or "สรุป" in user_message

    if not is_command:
        try:
            sheet = get_sheet()
            records = sheet.get_all_values()
        except Exception:
            records = []
            
        classified = classify_text_message(user_message, records)
        category = classified.get("category", "บันทึกทั่วไป")
        detail = classified.get("detail", user_message)

        save_text_to_google_sheets(date_str, user_name, detail, category)

        # ตรวจสอบว่าเป็นนัดหมายหรือไม่ ถ้าใช่สร้างลง Google Calendar ของครอบครัวอัตโนมัติ
        calendar_event = extract_calendar_event(user_message, date_str)
        if calendar_event:
            success = create_google_calendar_event(calendar_event)
            if success and ADMIN_USER_ID:
                line_bot_api.push_message(
                    ADMIN_USER_ID,
                    TextSendMessage(text=f"📅 **บันทึกนัดหมายลงปฏิทินครอบครัวสำเร็จ!**\n📌 หัวข้อ: {calendar_event.get('summary')}\n⏰ เริ่ม: {calendar_event.get('start_time')}")
                )
        return

    clean_message = user_message.replace("@Bot", "").strip()

    try:
        sheet = get_sheet()
        records = sheet.get_all_values()
    except Exception as e:
        records = []
        print(f"Error loading sheet: {e}")

    analysis = ask_gemini_agent(clean_message, records)
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

    return

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    user_id = event.source.user_id
    date_str = get_thai_time(event.timestamp)
    
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
