import os
import json
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

# ดึงค่า Configuration จาก Environment Variables ของ Railway
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

# ตั้งค่า Cloudinary
cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME'),
    api_key=os.environ.get('CLOUDINARY_API_KEY'),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET')
)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ตั้งค่า Google GenAI Client
gemini_client = None
if GEMINI_API_KEY:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# ตั้งค่า Google Sheets
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
    """ส่งรูปภาพให้ Gemini ช่วยดูและระบุหมวดหมู่/คำอธิบาย"""
    if not gemini_client:
        return {"category": "รูปภาพความทรงจำ", "location": "-", "detail": "ส่งรูปภาพบันทึกทริป"}

    prompt = (
        "วิเคราะห์รูปภาพนี้สำหรับบันทึกครอบครัว ตอบเป็น JSON Object เท่านั้น:\n"
        "{\n"
        '  "category": "หมวดหมู่หลัก เช่น อาหาร, ที่พัก, สถานที่ท่องเที่ยว, ครอบครัว",\n'
        '  "location": "สถานที่หรือร้าน/ที่พัก (ถ้าไม่ทราบใส่ \'-\')",\n'
        '  "detail": "คำอธิบายสิ่งที่เห็นในรูป 1 ประโยค เช่น ทานชุดอาหารขันโตก หรือ ห้องนอนเตียงคู่"\n'
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
    """ใช้ Gemini วิเคราะห์เจตนาผู้ใช้และค้นหาข้อมูลจาก Sheet โดยห้ามเดา"""
    if not gemini_client:
        return {"action": "SAVE"}

    prompt = f"""
คุณคือ AI ผู้ช่วยอัจฉริยะของระบบครอบครัว (Family Bot)
หน้าที่ของคุณคือตรวจสอบข้อความของผู้ใช้ โดยอ้างอิงจากข้อมูลใน Google Sheet ที่ให้ไว้อย่างเคร่งครัด

ข้อมูลใน Google Sheet (เรียงตามลำดับแถว):
{json.dumps(sheet_data, ensure_ascii=False, indent=2)}

ข้อความจากผู้ใช้: "{user_message}"

กฎเหล็ก (Strict Rules):
1. ห้ามเดาหรือจินตนาการข้อมูลที่ไม่มีอยู่จริงในตารางเด็ดขาด
2. หากผู้ใช้ต้องการค้นหารูปภาพ (action: "FIND_IMAGE"):
   - ให้ตรวจคอลัมน์ category และ detail ว่าตรงกับสิ่งที่ผู้ใช้ระบุหรือไม่
   - หากพบ ให้ระบุ image_url และระบุ text_reply
   - หากในตาราง "ไม่มี" รูปที่ตรงกับหมวดหมู่หรือคำขอ ให้กำหนด "image_url": null และตอบใน text_reply เช่น "ยังไม่พบรูปภาพเกี่ยวกับ [สิ่งที่หา] ในระบบครับ"
3. หากผู้ใช้ขอให้สรุป (action: "SUMMARIZE"): สรุปเฉพาะสิ่งที่มีบันทึกไว้ในตารางเท่านั้น ห้ามแต่งเติม
4. หากเป็นการพูดคุยทั่วไปหรือบอกเล่าเพื่อจดบันทึก ให้เลือก action: "SAVE"

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
    except Exception as e:
        print(f"Error saving to Google Sheets: {e}")

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
    print(f"========== MY USER ID IS: {user_id} ==========") # สั่งพิมพ์รหัสโชว์ใน Logs ของ Railway ทันทีที่ส่งข้อความ
    
    user_message = event.message.text.strip()
    date_str = get_thai_time(event.timestamp)
    
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

    # ส่งให้ Gemini วิเคราะห์
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
