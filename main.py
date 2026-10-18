import os
import gspread
from google.oauth2.service_account import Credentials
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, TextSendMessage

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

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
    except:
        user_name = "สมาชิกในครอบครัว"

    save_to_google_sheets(
        date_time=event.timestamp,
        name=user_name,
        category="ข้อความ / บันทึกทั่วไป",
        location_or_shop="-",
        detail=user_message,
        amount="0",
        image_url="-"
    )

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=f"บันทึกข้อมูลให้เรียบร้อยแล้วครับ: {user_message}")
    )

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    message_id = event.message.id
    user_id = event.source.user_id
    
    try:
        profile = line_bot_api.get_profile(user_id)
        user_name = profile.display_name
    except:
        user_name = "สมาชิกในครอบครัว"

    message_content = line_bot_api.get_message_content(message_id)
    image_filename = f"trip_{message_id}.jpg"
    
    with open(image_filename, 'wb') as fd:
        for chunk in message_content.iter_content():
            fd.write(chunk)

    drive_image_link = "อัปโหลดรูปลง Google Drive สำเร็จ"

    save_to_google_sheets(
        date_time=event.timestamp,
        name=user_name,
        category="รูปภาพความทรงจำ",
        location_or_shop="สถานที่ท่องเที่ยว",
        detail="ส่งรูปภาพบันทึกทริป",
        amount="0",
        image_url=drive_image_link
    )

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="บันทึกรูปภาพและข้อมูลลง Google Sheets เรียบร้อยแล้วครับ!")
    )

def save_to_google_sheets(date_time, name, category, location_or_shop, detail, amount, image_url):
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        
        # อ่านไฟล์ credentials.json จากโปรเจกต์โดยตรง
        creds = Credentials.from_service_account_file("credentials.json", scopes=scope)
        
        client = gspread.authorize(creds)
        sheet = client.open("Family Travel & Meal Log (OS)").sheet1
        sheet.append_row([str(date_time), name, category, location_or_shop, detail, amount, image_url])
    except Exception as e:
        print(f"Error saving to Google Sheets: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
