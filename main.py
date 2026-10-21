import os
import json
import time
import hashlib
import base64
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
import pytz
import threading
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageMessage, StickerMessage

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from google import genai
from google.genai import types

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
FAMILY_GROUP_ID = os.environ.get('FAMILY_GROUP_ID')
FAMILY_CALENDAR_ID = os.environ.get('FAMILY_CALENDAR_ID', 'primary')

# ลิงก์เว็บแอปครัว (Apps Script) — ค่าเริ่มต้นเป็น /dev สำหรับทดสอบเอง
# พอ deploy เป็น /exec แล้ว ให้ตั้ง env KITCHEN_APP_URL ทับได้เลย ไม่ต้องแก้โค้ด
KITCHEN_APP_URL = os.environ.get('KITCHEN_APP_URL', 'https://script.google.com/macros/s/AKfycbyRe-pHPpJ_5q9tPDPyJJ3ZyVY-qycfRsdNcPR4dw/dev')
KITCHEN_KEYWORDS = {'ครัว', 'เปิดครัว', 'สต็อก', 'สต๊อก', 'stock', 'เมนู', 'สูตร', 'สูตรอาหาร', 'รายการซื้อ', 'สุขภาพ'}

# กำหนด User ID ส่วนตัวของคุณ Chaiwat สำหรับส่งรายงานแจ้งเตือนแอดมินโดยตรง
ADMIN_USER_ID = "U4408dd0ab71b56db6074273eb28651b0"

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

if GEMINI_API_KEY:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
else:
    gemini_client = None

# ชื่อโมเดล Gemini — เปลี่ยนรุ่นครั้งต่อไปแก้บรรทัดนี้บรรทัดเดียว (free tier: 3.5-flash-lite = 500 ครั้ง/วัน)
GEMINI_MODEL = 'gemini-3.5-flash-lite'

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
    for attempt in range(3):
        try:
            response = gemini_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=f"""
วิเคราะห์ข้อความนี้: "{text}"
จัดหมวดหมู่ให้อยู่ในประเภทใดประเภทหนึ่งต่อไปนี้เท่านั้น:
- อาหาร/เครื่องดื่ม
- ซื้อของ/ใช้จ่าย
- การเดินทาง
- บันทึกทั่วไป

ตอบกลับในรูปแบบ: [หมวดหมู่] | [ข้อความสรุปสั้นๆ]
""",
            )
            parts = response.text.split("|")
            if len(parts) == 2:
                return parts[0].strip(), parts[1].strip()
            return "บันทึกทั่วไป", text
        except Exception as e:
            print(f"Gemini attempt {attempt+1} error: {e}")
            time.sleep(2)
    return "บันทึกทั่วไป", text

def analyze_image_with_gemini(image_bytes):
    if not gemini_client:
        return "บันทึกทั่วไป", "ส่งรูปภาพ"
    for attempt in range(3):
        try:
            response = gemini_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[
                    """วิเคราะห์รูปภาพนี้ จัดหมวดหมู่ให้อยู่ในประเภทใดประเภทหนึ่งต่อไปนี้เท่านั้น:
- อาหาร/เครื่องดื่ม
- ซื้อของ/ใช้จ่าย
- การเดินทาง
- บันทึกทั่วไป

ตอบกลับในรูปแบบ: [หมวดหมู่] | [คำบรรยายรูปสั้นๆ ภาษาไทย]""",
                    types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                ],
            )
            parts = response.text.split("|")
            if len(parts) == 2:
                return parts[0].strip(), parts[1].strip()
            return "บันทึกทั่วไป", "ส่งรูปภาพ"
        except Exception as e:
            print(f"Gemini image attempt {attempt+1} error: {e}")
            time.sleep(2)
    return "บันทึกทั่วไป", "ส่งรูปภาพ"

def upload_image_to_cloudinary(image_bytes, public_id):
    # อัปโหลดแบบ signed ผ่าน API ของ Cloudinary โดยตรง (ใช้ไลบรารีมาตรฐานล้วน ไม่ต้องลงแพ็กเกจเพิ่ม)
    cloud_name = os.environ.get('CLOUDINARY_CLOUD_NAME')
    api_key = os.environ.get('CLOUDINARY_API_KEY')
    api_secret = os.environ.get('CLOUDINARY_API_SECRET')
    if not (cloud_name and api_key and api_secret):
        print("Cloudinary env vars missing")
        return None
    try:
        timestamp = str(int(time.time()))
        folder = 'family_bot'
        # ลายเซ็น Cloudinary: พารามิเตอร์เรียงตามตัวอักษร ต่อท้ายด้วย api_secret แล้ว sha1
        to_sign = f"folder={folder}&public_id={public_id}&timestamp={timestamp}{api_secret}"
        signature = hashlib.sha1(to_sign.encode('utf-8')).hexdigest()
        data_uri = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode('utf-8')
        payload = urllib.parse.urlencode({
            'file': data_uri,
            'api_key': api_key,
            'timestamp': timestamp,
            'signature': signature,
            'folder': folder,
            'public_id': public_id,
        }).encode('utf-8')
        req = urllib.request.Request(f"https://api.cloudinary.com/v1_1/{cloud_name}/image/upload", data=payload)
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode('utf-8'))
        return result.get('secure_url')
    except Exception as e:
        print(f"Cloudinary upload error: {e}")
        return None

def append_log_row(row):
    # บันทึกแถวลง Sheets (USER_ENTERED เพื่อให้สูตร =IMAGE() แสดงรูปได้)
    for attempt in range(3):
        try:
            sheet = get_sheet()
            sheet.append_row(row, value_input_option='USER_ENTERED')
            return True
        except Exception as e:
            print(f"Attempt {attempt+1} - Error saving to Google Sheets: {e}")
            time.sleep(1)
    return False

def notify_admin(sender_name, category, summary_text, thai_time, extra_url=None):
    if not ADMIN_USER_ID:
        return
    admin_msg = (
        f"📝 **[Train] แจ้งเตือนข้อความใหม่**\n\n"
        f"👤 ผู้ส่ง: {sender_name}\n"
        f"📁 หมวดหมู่: {category}\n"
        f"💬 ข้อความ: {summary_text}\n"
        f"⏰ เวลา: {thai_time}"
    )
    if extra_url:
        admin_msg += f"\n🔗 {extra_url}"
    try:
        line_bot_api.push_message(ADMIN_USER_ID, TextSendMessage(text=admin_msg))
    except Exception as e:
        print(f"Error sending admin log: {e}")

def parse_calendar_intent(text):
    if not gemini_client:
        return None
    now_thai_str = datetime.now(pytz.timezone('Asia/Bangkok')).strftime('%Y-%m-%d %H:%M:%S')
    for attempt in range(3):
        try:
            response = gemini_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=f"""
วิเคราะห์ข้อความนี้อย่างละเอียดว่าเป็น "คำสั่งหรือความต้องการให้นัดหมาย/บันทึกปฏิทินอย่างแท้จริง" หรือไม่: "{text}"

เวลาปัจจุบัน (ไทย): {now_thai_str} — ใช้เป็นจุดอ้างอิงแปลงคำว่า วันนี้/คืนนี้/พรุ่งนี้/มะรืนนี้ ให้เป็นวันที่จริง

กฎเหล็กขั้นเด็ดขาดในการตัดสินใจ (is_calendar ต้องเป็น true ก็ต่อเมื่อผ่านทุกข้อ):
1. ข้อความต้องระบุทั้ง "วัน" และ "เวลาที่เจาะจง" อย่างชัดเจน (ถ้าไม่มีเวลาที่เจาะจง ห้ามเดา ให้ตอบ false ทันที)
2. ข้อความนั้นต้องมีบริบทเชิง "การนัดหมาย, กำหนดการทางการ, หรือคำสั่งให้เตือน"
3. หากเป็นการพิมพ์พูดคุยสนทนาทั่วไป เล่าเรื่อง หรือแค่บอกแผนลอย ๆ โดยไม่มีคำสั่งใด ๆ ให้ถือว่าไม่ใช่ (is_calendar: false ทันที)

ข้อยกเว้นสำคัญ (ใช้แทนกฎข้อ 3): ถ้าข้อความมี "คำสั่งชัด" เช่น นัดหมาย / ตั้งเตือน / แจ้งเตือน / บันทึกนัด / เตือนด้วย ประกอบกับเวลาที่เจาะจงตามข้อ 1 ให้ถือว่าเป็นนัดหมายทันที (is_calendar: true) ไม่ว่ากิจกรรมจะเป็นเรื่องส่วนตัวหรือกิจวัตรประจำวันใด ๆ ก็ตาม เช่น เตรียมเข้านอน, ทานยา, ออกกำลังกาย เพราะผู้ใช้ตั้งใจสั่งให้ระบบเตือน

กติกาเรื่องเวลาเตือน (reminder_minutes — ตอบเป็นรายการตัวเลขหน่วยนาทีเสมอ):
- ไม่ระบุเวลาเตือนเลย ให้ใช้ [30]
- ระบุครั้งเดียว เช่น "เตือนก่อน 15 นาที" = [15], "เตือนก่อน 1 ชั่วโมง" = [60], "เตือนล่วงหน้า 1 วัน" = [1440]
- ระบุหลายครั้ง เช่น "เตือนล่วงหน้า 1 วัน และก่อน 30 นาที" ให้ใช้ [1440, 30] (สูงสุด 5 ค่า เรียงจากมากไปน้อย)

กติกาเรื่องการทำซ้ำ (recurrence):
- ไม่ทำซ้ำ ให้ใช้ null
- "ทุกวัน" = "DAILY", "ทุกสัปดาห์" = "WEEKLY", "ทุกเดือน" = "MONTHLY", "ทุกปี" (เช่น วันเกิด วันครบรอบ) = "YEARLY"
- ถ้าแบบรายสัปดาห์ระบุวัน เช่น "ทุกวันจันทร์กับพฤหัส" ให้ใส่ recurrence_days เป็น ["MO", "TH"] (จันทร์=MO, อังคาร=TU, พุธ=WE, พฤหัส=TH, ศุกร์=FR, เสาร์=SA, อาทิตย์=SU) ถ้าไม่ระบุให้เว้นเป็น []

ตอบกลับในรูปแบบ JSON เท่านั้น:
{{
  "is_calendar": true/false,
  "summary": "หัวข้อเรื่อง",
  "start_time": "YYYY-MM-DDTHH:MM:SS",
  "reminder_minutes": [30],
  "recurrence": null,
  "recurrence_days": []
}}
""",
            )
            text_resp = response.text.strip()
            if text_resp.startswith("```json"):
                text_resp = text_resp[7:-3].strip()
            return json.loads(text_resp)
        except Exception as e:
            print(f"Calendar intent parse attempt {attempt+1} error: {e}")
            time.sleep(2)
    return None

def add_to_calendar(summary, start_time_str, reminder_minutes_list=None, recurrence=None, recurrence_days=None):
    try:
        service = get_calendar_service()
        tz = pytz.timezone('Asia/Bangkok')
        start_dt = tz.localize(datetime.strptime(start_time_str, '%Y-%m-%dT%H:%M:%S'))
        end_dt = start_dt + timedelta(hours=1)

        if not reminder_minutes_list:
            reminder_minutes_list = [30]
        # รองรับกรณีโมเดลส่งมาเป็นตัวเลขเดี่ยวแทนรายการ
        if isinstance(reminder_minutes_list, (int, float)):
            reminder_minutes_list = [int(reminder_minutes_list)]
        overrides = [{'method': 'popup', 'minutes': int(m)} for m in reminder_minutes_list[:5]]

        event = {
            'summary': summary,
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Bangkok'},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Bangkok'},
            'reminders': {
                'useDefault': False,
                'overrides': overrides,
            },
        }

        # การทำซ้ำแบบ Google Calendar (RRULE) — รายวัน/สัปดาห์/เดือน/ปี
        if recurrence:
            rrule = f"RRULE:FREQ={recurrence}"
            if recurrence == 'WEEKLY' and recurrence_days:
                rrule += ";BYDAY=" + ",".join(recurrence_days)
            event['recurrence'] = [rrule]

        event_result = service.events().insert(calendarId=FAMILY_CALENDAR_ID, body=event).execute()
        return event_result.get('htmlLink')
    except Exception as e:
        print(f"Error adding to calendar: {e}")
        return None

def background_calendar_reminder_worker():
    notified = set()
    while True:
        try:
            time.sleep(60)
            service = get_calendar_service()
            tz = pytz.timezone('Asia/Bangkok')
            now = datetime.now(tz)
            # มองไปข้างหน้า 8 วัน เพื่อให้การเตือนล่วงหน้าแบบหลายวัน (เช่น 1 วัน) ยิงได้ทันเวลา
            horizon = now + timedelta(days=8)

            events_result = service.events().list(
                calendarId=FAMILY_CALENDAR_ID,
                timeMin=now.isoformat(),
                timeMax=horizon.isoformat(),
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            events = events_result.get('items', [])

            for event in events:
                start_str = event['start'].get('dateTime')
                if not start_str:
                    continue  # ข้าม event แบบทั้งวัน (ไม่มีเวลาเริ่ม)
                start_dt = datetime.fromisoformat(start_str).astimezone(tz)

                # อ่านเวลาเตือนจากตัว event เอง — ถ้า event ใช้ค่ามาตรฐานของปฏิทิน ให้ถือว่าเตือนก่อน 30 นาที
                reminders = event.get('reminders', {})
                if reminders.get('useDefault', True):
                    offsets = [30]
                else:
                    offsets = [r.get('minutes', 30) for r in reminders.get('overrides', [])] or [30]

                for minutes in offsets:
                    fire_time = start_dt - timedelta(minutes=minutes)
                    # ยิงเมื่อถึงเวลาเตือน (เผื่อย้อนหลัง 5 นาที กันรอบเช็กคลาดหรือเครื่องเพิ่งตื่น)
                    if fire_time <= now <= fire_time + timedelta(minutes=5):
                        key = f"{event['id']}|{start_str}|{minutes}"
                        if key in notified:
                            continue

                        if minutes >= 1440 and minutes % 1440 == 0:
                            lead = f"อีก {minutes // 1440} วัน"
                        elif minutes >= 60 and minutes % 60 == 0:
                            lead = f"อีก {minutes // 60} ชั่วโมง"
                        else:
                            lead = f"อีก {minutes} นาที"

                        summary = event.get('summary', 'ไม่มีหัวข้อ')
                        alert_text = (
                            f"⏰ **แจ้งเตือนกำหนดการ!**\n"
                            f"📌 เรื่อง: {summary}\n"
                            f"🕒 เริ่ม: {start_dt.strftime('%d/%m %H:%M น.')} ({lead})"
                        )

                        target_id = FAMILY_GROUP_ID if FAMILY_GROUP_ID else ADMIN_USER_ID
                        if target_id:
                            line_bot_api.push_message(target_id, TextSendMessage(text=alert_text))

                        notified.add(key)
        except Exception as e:
            print(f"Reminder worker error: {e}")

# เริ่มเธรดเตือนนัดหมายตั้งแต่ตอน import โมดูล — ต้องอยู่นอก if __name__
# ไม่งั้นตอนรันใต้ gunicorn บน Render เธรดนี้จะไม่เริ่มทำงานเลย
reminder_thread = threading.Thread(target=background_calendar_reminder_worker, daemon=True)
reminder_thread.start()

@app.route("/", methods=['GET'])
def index():
    # หน้าเช็กสถานะ: เปิด URL หลักในเบราว์เซอร์ หรือให้ UptimeRobot ping จะได้คำตอบ 200 OK
    return "Family Travel & Kitchen Bot is running!", 200

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
        msg_text = event.message.text

        # 0. คำสั่งเปิดแอปครัว: พิมพ์คีย์เวิร์ดสั้น ๆ บอทตอบลิงก์เว็บแอปทันที (ไม่ผ่าน AI ไม่ลง log ไม่ต้องดึงโปรไฟล์)
        if msg_text.strip().lower() in KITCHEN_KEYWORDS:
            if KITCHEN_APP_URL:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"🍳💚 **ครัวและสุขภาพ บ้านตั้งทอง**\nเปิดดูเมนู สูตรอาหาร สต็อกวัตถุดิบ และอัปเดตสุขภาพครอบครัวได้ที่นี่:\n{KITCHEN_APP_URL}")
                )
            return

        sender_name = get_user_display_name(user_id)
        # พิมพ์ source id ลง log — ใช้หา FAMILY_GROUP_ID จาก Render Logs (ส่งข้อความในกลุ่มแล้วดูค่า group:)
        print(f"Source -> user:{user_id} group:{getattr(event.source, 'group_id', None)} room:{getattr(event.source, 'room_id', None)}")
        timestamp = event.timestamp
        thai_time = get_thai_time(timestamp)

        # 1. ตรวจสอบนัดหมายปฏิทิน
        cal_intent = parse_calendar_intent(msg_text)
        if cal_intent and cal_intent.get("is_calendar"):
            summary = cal_intent.get("summary", "นัดหมายครอบครัว")
            start_time = cal_intent.get("start_time")
            reminder_list = cal_intent.get("reminder_minutes", [30])
            if isinstance(reminder_list, (int, float)):
                reminder_list = [int(reminder_list)]
            recurrence = cal_intent.get("recurrence")
            recurrence_days = cal_intent.get("recurrence_days") or []

            if start_time:
                res = add_to_calendar(summary, start_time, reminder_list, recurrence, recurrence_days)
                if res:
                    def fmt_lead(m):
                        if m >= 1440 and m % 1440 == 0:
                            return f"{m // 1440} วัน"
                        if m >= 60 and m % 60 == 0:
                            return f"{m // 60} ชั่วโมง"
                        return f"{m} นาที"
                    lead_text = ", ".join(fmt_lead(m) for m in reminder_list)
                    recur_labels = {'DAILY': 'ทุกวัน', 'WEEKLY': 'ทุกสัปดาห์', 'MONTHLY': 'ทุกเดือน', 'YEARLY': 'ทุกปี'}
                    recur_text = f"\n🔁 ทำซ้ำ: {recur_labels.get(recurrence, recurrence)}" if recurrence else ""
                    reply_text = (
                        f"📅 **บันทึกนัดหมายลงปฏิทินครอบครัวสำเร็จ!**\n"
                        f"📌 หัวข้อ: {summary}\n"
                        f"⏰ เริ่ม: {start_time}\n"
                        f"🔔 แจ้งเตือนล่วงหน้า: {lead_text}"
                        f"{recur_text}"
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

        # 3. ส่ง Log รายงานส่วนตัวไปหาแอดมินโดยตรง
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

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    try:
        user_id = event.source.user_id
        sender_name = get_user_display_name(user_id)
        thai_time = get_thai_time(event.timestamp)
        print(f"Source -> user:{user_id} group:{getattr(event.source, 'group_id', None)} room:{getattr(event.source, 'room_id', None)}")

        # 1) ดาวน์โหลดรูปจาก LINE
        content = line_bot_api.get_message_content(event.message.id)
        image_bytes = b"".join(chunk for chunk in content.iter_content())
        if not image_bytes:
            print("Empty image content")
            return

        # 2) อัปโหลดขึ้น Cloudinary + ให้ AI บรรยายรูป
        image_url = upload_image_to_cloudinary(image_bytes, f"img_{event.message.id}")
        category, summary_text = analyze_image_with_gemini(image_bytes)

        # 3) บันทึกลง Sheets รูปแบบเดียวกับ log ฉบับเดิม: คอลัมน์ G = รูปตัวอย่าง (=IMAGE), H = ลิงก์ Cloudinary
        image_formula = f'=IMAGE("{image_url}")' if image_url else ""
        append_log_row([thai_time, sender_name, category, "-", summary_text, 0, image_formula, image_url or ""])

        # 4) แจ้งแอดมิน (แนบลิงก์รูปให้กดดูได้)
        notify_admin(sender_name, category, f"📷 {summary_text}", thai_time, image_url)
    except Exception as e:
        print(f"Error handling image message: {e}")

@handler.add(MessageEvent, message=StickerMessage)
def handle_sticker_message(event):
    try:
        user_id = event.source.user_id
        sender_name = get_user_display_name(user_id)
        thai_time = get_thai_time(event.timestamp)
        print(f"Source -> user:{user_id} group:{getattr(event.source, 'group_id', None)} room:{getattr(event.source, 'room_id', None)}")

        sticker_id = event.message.sticker_id
        package_id = event.message.package_id
        # รูปประจำตัวสติ๊กเกอร์จาก CDN ของ LINE
        sticker_url = f"https://stickershop.line-scdn.net/stickershop/v1/sticker/{sticker_id}/android/sticker.png"
        summary_text = f"ส่งสติ๊กเกอร์ (package {package_id} / sticker {sticker_id})"

        append_log_row([thai_time, sender_name, "สติ๊กเกอร์", "-", summary_text, 0, f'=IMAGE("{sticker_url}")', sticker_url])
        notify_admin(sender_name, "สติ๊กเกอร์", f"😊 {summary_text}", thai_time, sticker_url)
    except Exception as e:
        print(f"Error handling sticker message: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
