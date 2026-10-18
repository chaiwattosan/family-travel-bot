import os
import json
import gspread
import cloudinary
import cloudinary.uploader
from datetime import datetime, timezone, timedelta
from google import genai
from google.genai import types
from PIL import Image
from google.oauth2.service_account import Credentials
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, ImageMessage, 
    TextSendMessage, ImageSendMessage
)

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

# ตั้งค่า Cloudinary
cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME'),
    api_key=os.environ.get('CLOUDINARY_API_KEY'),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET'),
    secure=True
)

# ตั้งค่า Gemini Client
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

def get_thai_time(timestamp_ms):
    """แปลง Unix timestamp (ms) จาก LINE เป็นเวลาไทย YYYY-MM-DD HH:MM:SS"""
    utc_dt = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
    thai_dt = utc_dt + timedelta(hours=7)
    return thai_dt.strftime('%Y-%m-%d %H:%M:%S')

def get_google_credentials():
    creds_json = json.loads(os.environ.get('GOOGLE_CREDENTIALS'))
    if "private_key" in creds_json:
        creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
    return Credentials.from_service_account_info(creds_json, scopes=SCOPES)

def get_sheet():
    creds = get_google_credentials()
    client = gspread.authorize(creds)
    return client.open("Family Travel & Meal Log (OS)").sheet1

def analyze_image_with_gemini(image_path):
    """ให้ Gemini ดูรูปภาพแล้วระบุหมวดหมู่และคำอธิบายภาษาไทยอัตโนมัติ"""
    if not gemini_client:
        return {"category": "รูปภาพความทรงจำ", "location": "-", "detail": "ส่งรูปภาพบันทึกทริป"}

    prompt = """
คุณคือ AI ผู้ช่วยบันทึกข้อมูลภาพถ่ายของครอบครัว
ช่วยดูรูปภาพนี้ แล้วระบุข้อมูลลงในรูปแบบ JSON Object:
{
  "category": "หมวดหมู่หลัก เช่น อาหาร, ที่พัก, สถานที่ท่องเที่ยว, ครอบครัว, ซื้อของ",
  "location": "สถานที่หรือประเภทร้าน/ที่พักที่ปรากฏในรูป (ถ้าไม่แน่ใจให้ใส่ '-')",
  "detail": "คำอธิบายสิ่งที่เห็นในรูปสั้นๆ 1 ประโยค เช่น ทานส้มตำ ไก่ย่าง หรือ ห้องนอนเตียงคู่ หรือ ถ่ายรูปครอบครัว"
}
ตอบเฉพาะ JSON เท่านั้น
"""
    try:
        pil_img = Image.open(image_path)
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[prompt, pil_img],
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"Error analyzing image with Gemini: {e}")
        return {"category": "รูปภาพความทรงจำ", "location": "-", "detail": "ส่งรูปภาพบันทึกทริป"}

def ask_gemini_agent(user_message, sheet_data):
    """ใช้ Gemini วิเคราะห์เจตนาผู้ใช้และค้นหาข้อมูลจาก Sheet"""
    if not gemini_client:
        return {"action": "SAVE"}

    prompt = f"""
คุณคือ AI ผู้ช่วยอัจฉริยะของระบบครอบครัว (Family Bot)
หน้าที่ของคุณคือวิเคราะห์ข้อความของผู้ใช้ โดยอ้างอิงจากข้อมูลใน Google Sheet ที่แนบมานี้

ข้อมูลใน Google Sheet (เรียงตามลำดับแถว):
{json.dumps(sheet_data, ensure_ascii=False, indent=2)}

ข้อความจากผู้ใช้: "{user_message}"

ให้ตอบกลับในรูปแบบ JSON Object เท่านั้น โดยมีโครงสร้างดังนี้:
{{
  "action": "FIND_IMAGE" หรือ "SUMMARIZE" หรือ "SAVE",
  "image_url": "URL รูปภาพจากตารางคอลัมน์ H ที่ตรงกับสิ่งที่ผู้ใช้หามากที่สุด (ถ้าไม่พบให้ใส่ null)",
  "text_reply": "ข้อความตอบกลับผู้ใช้อย่างสุภาพ กระชับ และเป็นธรรมชาติ"
}}

แนวทางการตัดสินใจ:
1. หากผู้ใช้ต้องการดูรูปภาพ, ค้นหารูปภาพ (เช่น รูปอาหาร, รูปที่พัก, รูปเที่ยว, รูปล่าสุด) ให้เลือก action: "FIND_IMAGE" และเลือก image_url จากคอลัมน์ H ที่ตรงที่สุด
2. หากผู้ใช้ต้องการให้สรุป, สรุปกิจกรรม, สรุปรายวัน หรือถามข้อมูลในตาราง ให้เลือก action: "SUMMARIZE" และเขียนสรุปใน text_reply
3. หากผู้ใช้พิมพ์บันทึกทั่วไป ไม่ได้ถาม ให้เลือก action: "SAVE"
"""
    try:
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"Error querying Gemini: {e}")
        return {"action": "SAVE"}

@app.route("/", methods=['GET'])
def index():
    return "Family Life & Care OS - LINE Bot Server is running!"

@app.route("/webhook", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    user_message = event.message.text.strip()
    user_id = event.source.user_id
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

    analysis = ask_gemini_agent(user_message, records)
    action = analysis.get("action", "SAVE")

    if action == "FIND_IMAGE":
        found_url = analysis.get("image_url")
        reply_txt = analysis.get("text_reply", "ส่งรูปภาพให้แล้วครับ")
        if found_url and found_url.startswith("https://res.cloudinary.com"):
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
                TextSendMessage(text="ไม่พบรูปภาพที่ตรงกับที่ค้นหาในระบบครับ")
            )
            return

    elif action == "SUMMARIZE":
        reply_txt = analysis.get("text_reply", "ไม่มีข้อมูลสำหรับสรุปครับ")
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_txt)
        )
        return

    # บันทึกข้อความทั่วไป
    save_to_google_sheets(
        date_time=date_str,
        name=user_name,
        category="ข้อความ / บันทึกทั่วไป",
        location_or_shop="-",
        detail=user_message,
        amount="0",
        image_preview="-",
        image_link="-"
    )

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=f"บันทึกข้อมูลเรียบร้อยครับ ({date_str})\nข้อความ: {user_message}")
    )

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    message_id = event.message.id
    user_id = event.source.user_id
    date_str = get_thai_time(event.timestamp)
    
    try:
        profile = line_bot_api.get_profile(user_id)
        user_name = profile.display_name
    except Exception:
        user_name = "สมาชิกในครอบครัว"

    message_content = line_bot_api.get_message_content(message_id)
    image_filename = f"trip_{message_id}.jpg"
    
    with open(image_filename, 'wb') as fd:
        for chunk in message_content.iter_content():
            fd.write(chunk)

    try:
        # ให้ Gemini ดูรูปและสร้างคำบรรยายอัตโนมัติ
        ai_meta = analyze_image_with_gemini(image_filename)
        img_category = ai_meta.get("category", "รูปภาพความทรงจำ")
        img_location = ai_meta.get("location", "-")
        img_detail = ai_meta.get("detail", "บันทึกรูปภาพ")

        # อัปโหลดรูปขึ้น Cloudinary
        upload_result = cloudinary.uploader.upload(
            image_filename,
            folder="family_bot",
            public_id=f"trip_{message_id}"
        )
        image_url = upload_result.get('secure_url')
        image_preview_formula = f'=IMAGE("{image_url}")'
    except Exception as e:
        print(f"Error handling image: {e}")
        img_category = "รูปภาพความทรงจำ"
        img_location = "-"
        img_detail = "ส่งรูปภาพบันทึกทริป"
        image_preview_formula = "เกิดข้อผิดพลาดในการอัปโหลดรูป"
        image_url = "-"
    finally:
        if os.path.exists(image_filename):
            os.remove(image_filename)

    save_to_google_sheets(
        date_time=date_str,
        name=user_name,
        category=img_category,
        location_or_shop=img_location,
        detail=img_detail,
        amount="0",
        image_preview=image_preview_formula,
        image_link=image_url
    )

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=f"บันทึกรูปภาพเรียบร้อยครับ ({date_str})\nหมวดหมู่: {img_category}\nรายละเอียด: {img_detail}")
    )

def save_to_google_sheets(date_time, name, category, location_or_shop, detail, amount, image_preview, image_link):
    try:
        sheet = get_sheet()
        sheet.append_row([
            str(date_time),
            name,
            category,
            location_or_shop,
            detail,
            amount,
            image_preview,
            image_link
        ], value_input_option='USER_ENTERED')
        print("Successfully saved to Google Sheets!")
    except Exception as e:
        print(f"Error saving to Google Sheets: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
