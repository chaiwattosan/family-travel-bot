import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, TextSendMessage

app = Flask(__name__)

# ดึงค่าจาก Environment Variables ที่เราตั้งไว้บน Railway
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

    # ตอบกลับยืนยันการรับข้อความ
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=f"รับทราบครับคุณ {user_name}: {user_message}")
    )

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="ได้รับรูปภาพบรรยากาศทริปเรียบร้อยแล้วครับ! กำลังบันทึกเข้าอัลบัมให้ครับ")
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)