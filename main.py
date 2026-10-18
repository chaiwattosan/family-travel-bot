import os
import json
import gspread
import cloudinary
import cloudinary.uploader
from google.oauth2.service_account import Credentials
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, TextSendMessage

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')

# ตั้งค่า Cloudinary จาก Environment Variables
cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME'),
    api_key=os.environ.get('CLOUDINARY_API_KEY'),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET'),
    secure=True
)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

def get_google_credentials():
    creds_json = json.loads(os.environ.get('GOOGLE_CREDENTIALS'))
    if "private_key" in creds_json:
        creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
    return Credentials.from_service_account_info(creds_json, scopes=SCOPES)

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
    user_message = event.message.text
    user_id = event.source.user_id
    
    try:
        profile = line_bot_api.get_profile(user_id)
        user_name = profile.display_name
    except Exception:
        user_name = "สมาชิกในครอบครัว"

    save_to_google_sheets(
        date_time=event.timestamp,
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
        TextSendMessage(text=f"บันทึกข้อมูลเรียบร้อยครับ: {user_message}")
    )

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    message_id = event.message.id
    user_id = event.source.user_id
    
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
        # อัปโหลดรูปขึ้น Cloudinary พร้อมจัดเก็บในโฟลเดอร์ family_bot
        upload_result = cloudinary.uploader.upload(
            image_filename,
            folder="family_bot",
            public_id=f"trip_{message_id}"
        )
        image_url = upload_result.get('secure_url')
        image_preview_formula = f'=IMAGE("{image_url}")'
    except Exception as e:
        print(f"Error uploading to Cloudinary: {e}")
        image_preview_formula = "เกิดข้อผิดพลาดในการอัปโหลดรูป"
        image_url = "-"
    finally:
        if os.path.exists(image_filename):
            os.remove(image_filename)

    save_to_google_sheets(
        date_time=event.timestamp,
        name=user_name,
        category="รูปภาพความทรงจำ",
        location_or_shop="สถานที่ท่องเที่ยว",
        detail="ส่งรูปภาพบันทึกทริป",
        amount="0",
        image_preview=image_preview_formula,
        image_link=image_url
    )

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="บันทึกรูปภาพและจัดเก็บลง Google Sheets ให้เรียบร้อยแล้วครับ!")
    )

def save_to_google_sheets(date_time, name, category, location_or_shop, detail, amount, image_preview, image_link):
    try:
        creds = get_google_credentials()
        client = gspread.authorize(creds)
        sheet = client.open("Family Travel & Meal Log (OS)").sheet1
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
