import os
import json
import uuid
from datetime import datetime
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
    'https://www.googleapis.com/auth/drive'
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

def get_thai_time(timestamp_ms):
    tz = pytz.timezone('Asia/Bangkok')
    dt = datetime.fromtimestamp(timestamp_ms / 1000.0, tz)
    return dt.strftime('%Y-%m-%d %H:%M:%S')

def analyze_image_with_gemini(image_path):
    if not gemini_client:
        return {"category": "รูปภาพความทรงจำ", "location": "-", "detail": "ส่งรูปภาพบันทึกทริป"}

    prompt = (
        "วิเคราะห์รูปภาพนี้สำหรับบันทึกครอบครัว ตอบเป็น JSON Object เท่านั้น:\n"
        "{\n"
        '  "category": "หมวดหมู่หลัก เช่น อาหาร, ที่พัก, สถานที่ท่องเที่ยว, ครอบครัว",\n'
        '  "location": "สถานที่หรือร้าน/ที่พัก (ถ้าไม่ทราบใส่ \'-\')",\n'
        '  "detail": "คำอธิบายสิ่งที่เห็นในรูป โดยระบุบุคคลในภาพ เช่น คุณพ่อ หรือ อากง (ถ้ามี) และกิจกรรมที่ทำ"\n'
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
        return {"category": "รูปภาพความทรงจำ", "location": "-", "detail": "ส่งรูปภาพบันทึกทริป"}

def ask_gemini_agent(user_message, sheet_data):
    if not gemini_client:
        return {"action": "SAVE"}

    prompt = f"""
คุณคือ AI ผู้ช่วยอัจฉริยะของระบบครอบครัว (Family Bot)
หน้าที่ของคุณคือตรวจสอบข้อความของผู้ใช้ โดยอ้างอิงจากข้อมูลใน Google Sheet ที่ให้ไว้อย่างเคร่งครัด

ข้อมูลใน Google Sheet (เรียงตามลำดับแถว โดยคอลัมน์แรกคือ วันที่และเวลาที่โพสต์):
{json.dumps(sheet_data, ensure_ascii=False, indent=2)}

ข้อความจากผู้ใช้: "{user_message}"

กฎเหล็กสำหรับการค้นหา (Strict Search Rules):
1. คำว่า "อากง" และ "คุณพ่อ" หมายถึงบุคคลเดียวกัน ให้เชื่อมโยงความหมายนี้ในการค้นหาข้อมูลเสมอ
2. ให้ยึด "เวลาที่ลงรูปในตาราง" (คอลัมน์แรก) เป็นเกณฑ์หลักในการเทียบเคียงมื้ออาหาร:
   - อาหารเช้า / มื้อเช้า: ให้เลือกภาพที่บันทึกในช่วงเช้า (เช่น 06:00 - 11:00 น.) ของวันที่สอบถาม
   - อาหารเที่ยง / มื้อเที่ยง: ให้เลือกภาพในช่วงเที่ยง (11:00 - 14:00 น.)
   - อาหารเย็น / มื้อเย็น: ให้เลือกภาพช่วงเย็น (17:00 น. เป็นต้นไป)
3. หากพบรูปที่ตรงกัน ให้ดึง image_url ออกมา และในการเขียน text_reply **ต้องระบุเวลาที่โพสต์รูปนั้นกำกับด้วยเสมอ** (เช่น "จากบันทึกเวลา 10:55 น. คือรูป...") เพื่อให้คนถามเข้าใจตรงกัน
4. หากในตาราง "ไม่มี" รูปที่สอดคล้อง ให้กำหนด "image_url": null และตอบใน text_reply เช่น "ยังไม่พบรูปภาพหรือข้อมูลเกี่ยวกับช่วงเวลานี้ในระบบครับ"
5. หากเป็นการพูดคุยทั่วไป ให้เลือก action: "SAVE" และไม่ต้องตอบกลับในแชทกลุ่ม

ตอบกลับเป็น JSON Object เท่านั้น:
{{
  "action": "FIND_IMAGE" หรือ "SUMMARIZE" หรือ "SAVE",
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

def save_to_google_sheets(date_str, user_name, category, location, detail, image_url):
    try:
        sheet = get_sheet()
        preview_formula = f'=IMAGE("{image_url}")' if image_url else ""
        row = [date_str, user_name, category, location, detail, "", preview_formula, image_url]
        sheet.append_row(row)
        print("Successfully saved to Google Sheets!")

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
                [
                    TextSendMessage(text=notification_text),
                    ImageSendMessage(original_content_url=image_url, preview_image_url=image_url)
                ]
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
    
    if "@Bot" in user_message:
        user_message = user_message.replace("@Bot", "").strip()

    try:
        profile = line_bot_api.get_profile(user_id)
        user_name = profile.display_name
    except Exception:
        user_name = "สมาชิกในครอบครัว"

    try:
        sheet = get_sheet()
        records = sheet.get_all_values()
    except Exception as e:
        records = []
        print(f"Error loading sheet: {e}")

    analysis = ask_gemini_agent(user_message, records)
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
        reply_txt = analysis.get("text_reply", "สรุปข้อมูลครอบครัว")
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
    
    try:
        profile = line_bot_api.get_profile(user_id)
        user_name = profile.display_name
    except Exception:
        user_name = "สมาชิกในครอบครัว"

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
    category = analysis.get("category", "รูปภาพความทรงจำ")
    location = analysis.get("location", "-")
    detail = analysis.get("detail", "บันทึกภาพถ่ายครอบครัว")

    try:
        save_to_google_sheets(date_str, user_name, category, location, detail, image_url)
    except Exception as e:
        print(f"Failed to save and notify: {e}")
        return

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
