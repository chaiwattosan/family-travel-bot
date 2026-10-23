import os
import re
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
from linebot.exceptions import InvalidSignatureError, LineBotApiError
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

# [Train] ใช้ push_message ซึ่งนับเข้าโควตา LINE แผนฟรี (200 ข้อความ/เดือน) ทุกข้อความ
# เคยเผาโควตาหมดกลางเดือน (429 "You have reached your monthly limit" 23 ต.ค. 2026) จนตัวเตือนนัด (push เช่นกัน) ยิงไม่ได้
# ค่าเริ่มต้น = ปิด (log ยังลง Google Sheets ครบเหมือนเดิม) — เปิดเฉพาะตอนเทรน/ดีบักด้วย env TRAIN_PUSH_ENABLED=1 บน Render
TRAIN_PUSH_ENABLED = os.environ.get('TRAIN_PUSH_ENABLED', '0') == '1'

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ===== Bot B — ช่องสำรองสำหรับ push โดยเฉพาะ (เพิ่ม 23 ต.ค. 2026 ตามการตัดสินใจของนาย) =====
# ที่มา: โควตา push แผนฟรี (200 ข้อความ/เดือน/ช่อง) ของ Bot A หมดกลางเดือน ต.ค. 2026 จาก [Train] push
# Bot B = อีก Official Account (โควตาแยกอีก 200/เดือน) — หน้าที่เดียวคือ push แทน A เวลา A เจอ 429
# B ไม่ตอบแชต ไม่มีโฟลว์ของตัวเอง: webhook ของ B (/callback_b) แค่พิมพ์ Source id ลง log
# ⚠️ userId/groupId ของ LINE เป็น "รายช่อง" — id ของช่อง B ใช้แทนของ A ไม่ได้ ต้องอ่านจาก log [BotB]
# แล้วตั้ง env แยก: LINE_B_CHANNEL_ACCESS_TOKEN / LINE_B_CHANNEL_SECRET / FAMILY_GROUP_ID_B / ADMIN_USER_ID_B
# ไม่ตั้ง env ชุดนี้ = ระบบทำงานแบบเดิมด้วย A ช่องเดียว (failover ปิดเองอัตโนมัติ)
LINE_B_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_B_CHANNEL_ACCESS_TOKEN')
LINE_B_CHANNEL_SECRET = os.environ.get('LINE_B_CHANNEL_SECRET')
FAMILY_GROUP_ID_B = os.environ.get('FAMILY_GROUP_ID_B')
ADMIN_USER_ID_B = os.environ.get('ADMIN_USER_ID_B')
line_bot_api_b = LineBotApi(LINE_B_CHANNEL_ACCESS_TOKEN) if LINE_B_CHANNEL_ACCESS_TOKEN else None
handler_b = WebhookHandler(LINE_B_CHANNEL_SECRET) if LINE_B_CHANNEL_SECRET else None

if handler_b:
    @handler_b.default()
    def bot_b_log_only(event):
        # Bot B ไม่ประมวลผลอะไรเลย — แค่โชว์ id ของช่อง B ใน log ไว้ให้แอดมินเอาไปตั้ง env
        src = event.source
        print(f"[BotB] event={event.type} user:{getattr(src, 'user_id', None)} group:{getattr(src, 'group_id', None)} room:{getattr(src, 'room_id', None)}")

def push_with_failover(text, target_a, target_b):
    # ส่ง push ผ่าน Bot A ก่อน — ถ้าเจอโควตาเต็ม (429) สลับไป Bot B ให้อัตโนมัติ
    # target_b = id ปลายทางเดียวกันในช่อง B (คนละค่ากับของ A เสมอ) — คืนค่า 'A' / 'B' / None (ส่งไม่ได้ทั้งคู่)
    try:
        line_bot_api.push_message(target_a, TextSendMessage(text=text))
        return 'A'
    except LineBotApiError as e:
        if getattr(e, 'status_code', None) != 429:
            raise
        print("Push via Bot A hit monthly quota (429) — failing over to Bot B")
    if line_bot_api_b and target_b:
        line_bot_api_b.push_message(target_b, TextSendMessage(text=text))
        return 'B'
    return None

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
    if not TRAIN_PUSH_ENABLED:
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
            'description': remind_marker([int(m) for m in reminder_minutes_list[:5]]),
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

def remind_marker(minutes_list):
    # เครื่องหมายเลขเตือนที่บอทฝังท้าย description ของ event ที่ตัวเองสร้าง: [remind:1440,180]
    # เหตุผล: reminder overrides ที่ service account ตั้งตอน insert/patch ไม่ติดจริงบนปฏิทินครอบครัว
    # (เจอ 2 ครั้ง 22 ต.ค. — นัดสรุปจากบทสนทนา + เที่ยวบิน) ตัวเตือนจึงอ่านเลขจากเครื่องหมายนี้แทนเมื่อ event เป็น useDefault
    return "[remind:" + ",".join(str(int(m)) for m in minutes_list) + "]"

def parse_remind_marker(description):
    if not description:
        return None
    m = re.search(r'\[remind:([0-9,]+)\]', description)
    if not m:
        return None
    try:
        vals = [int(x) for x in m.group(1).split(',') if x.strip()]
        return vals[:5] or None
    except Exception:
        return None

def reminder_due(fire_time, now, grace_minutes=5):
    # ถึงเวลาเตือนหรือยัง: ยิงได้ตั้งแต่ fire_time ถึง fire_time + grace (เผื่อรอบเช็กคลาด/process เพิ่งตื่น)
    return fire_time <= now <= fire_time + timedelta(minutes=grace_minutes)

def background_calendar_reminder_worker():
    notified = set()
    # รอบแรกหลัง process เริ่ม/รีสตาร์ต: เช็กทันทีไม่ต้องรอ 60 วิ + เผื่อย้อนหลัง 15 นาที
    # (บทเรียน 22 ต.ค.: deploy รอบเย็นทับจังหวะเตือนเปิดไฟ 18:00 พอดี — เธรดเริ่มช้า หน้าต่างเดิม 5 นาทีเลยหลุดเงียบ ๆ)
    first_pass = True
    while True:
        try:
            if not first_pass:
                time.sleep(60)
            grace = 15 if first_pass else 5
            first_pass = False
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
                # ยกเว้น event ที่บอทสร้างเอง: อ่านเลขเตือนจากเครื่องหมาย [remind:...] ท้าย description แทน
                reminders = event.get('reminders', {})
                if reminders.get('useDefault', True):
                    offsets = parse_remind_marker(event.get('description', '')) or [30]
                else:
                    offsets = [r.get('minutes', 30) for r in reminders.get('overrides', [])] or [30]

                for minutes in offsets:
                    fire_time = start_dt - timedelta(minutes=minutes)
                    # ยิงเมื่อถึงเวลาเตือน (เผื่อย้อนหลังตาม grace — 5 นาทีปกติ, 15 นาทีเฉพาะรอบแรกหลังรีสตาร์ต)
                    if reminder_due(fire_time, now, grace):
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

                        # ปลายทางแยกตามช่อง (groupId/userId เป็นรายช่อง ใช้แทนกันไม่ได้):
                        # ส่งด้วย A ก่อน โควตา A เต็ม (429) ค่อยสลับไป B — ดู push_with_failover
                        if FAMILY_GROUP_ID:
                            target_a, target_b = FAMILY_GROUP_ID, FAMILY_GROUP_ID_B
                        else:
                            target_a, target_b = ADMIN_USER_ID, ADMIN_USER_ID_B
                        if target_a:
                            try:
                                used = push_with_failover(alert_text, target_a, target_b)
                                if used is None:
                                    print(f"Reminder push not delivered for {key} (Bot A quota exhausted, Bot B unavailable)")
                                elif used == 'B':
                                    print(f"Reminder for {key} sent via Bot B")
                            except Exception as e:
                                # push ไม่ผ่านก็ถือว่าพยายามกับ occurrence นี้แล้ว ไม่ retry ซ้ำทุก 60 วิจน log รก
                                print(f"Reminder push failed for {key}: {e}")

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

@app.route("/callback_b", methods=['POST'])
def callback_b():
    # Webhook ของ Bot B — ไม่ตอบแชต ประมวลผลอะไรทั้งนั้น (handler_b มีแค่ตัวพิมพ์ log [BotB])
    # ใช้ตอนติดตั้ง B: แอดมิน add เพื่อน/เชิญ B เข้ากลุ่ม แล้วอ่าน userId/groupId ของช่อง B จาก Render log
    if not handler_b:
        return 'Bot B not configured', 200
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler_b.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# ===== โหมดถาม-ตอบตารางนัด / วันพระ (เพิ่ม 21 ต.ค. 2569) =====
# ตอบคำถามเรื่องตารางจากปฏิทินครอบครัวจริง: วันพระตอบด้วยโค้ดล้วน ๆ (แม่น ไม่ใช้ AI)
# ส่วนคำถามนัดทั่วไป ส่งรายการนัดให้ Gemini ช่วยจับคู่ความหมาย (เช่น "ไป รพ." = "พบหมอ") แล้วเรียบเรียงคำตอบ
THAI_WEEKDAYS = ['วันจันทร์', 'วันอังคาร', 'วันพุธ', 'วันพฤหัสบดี', 'วันศุกร์', 'วันเสาร์', 'วันอาทิตย์']
THAI_MONTHS = ['', 'มกราคม', 'กุมภาพันธ์', 'มีนาคม', 'เมษายน', 'พฤษภาคม', 'มิถุนายน',
               'กรกฎาคม', 'สิงหาคม', 'กันยายน', 'ตุลาคม', 'พฤศจิกายน', 'ธันวาคม']
QA_CREATE_VERBS = ['ตั้งเตือน', 'ตั้งนัด', 'จองนัด', 'เพิ่มนัด', 'บันทึกนัด', 'ตั้งนัดหมาย', 'จองนัดหมาย', 'เพิ่มนัดหมาย', 'บันทึกนัดหมาย']
# หมายเหตุ: ห้ามใส่คำเปล่า ๆ อย่าง 'นัดหมาย' ในลิสต์นี้ — เคสจริง 23 ต.ค. 2026: คำถามในกลุ่ม "วันนี้มีนัดหมายอะไรไหม"
# โดนกันเป็นคำสั่งสร้างนัดเพราะมีคำว่า นัดหมาย ทั้งที่มีคำถาม 'ไหม' ชัด ๆ (ส่วนคำสั่งสร้างนัดที่ไม่มีคำถาม cue
# จะไหลไปโหมดสร้างนัดเองอยู่แล้ว เพราะ is_schedule_question บังคับว่าต้องมี question cue ถึงจะตอบ)
# สัญญาณคำถามแบบเข้ม (แก้ 21 ต.ค. 20:20): คิวอ่อน ๆ อย่าง "เหลือ/บ้าง/เดือนนี้" เคยทำให้ข้อความประกาศธรรมดาโดนตีเป็นคำถาม บอทเลยตอบซ้ำทั้งที่ไม่มีใครถาม
QA_QUESTION_CUES = ['ไหม', 'มั้ย', 'เมื่อไหร่', 'เมื่อไร', 'วันไหน', 'อะไรบ้าง', 'หรือเปล่า', 'กี่โมง', 'กี่วัน', 'เท่าไหร่', 'รู้ไหม']
# ข้อความประกาศ/ชวนสมาชิกส่งข้อมูล ไม่ใช่คำถามถึงบอท — ห้ามแย่งตอบ
QA_ANNOUNCE_CUES = ['แจ้งได้', 'แจ้งมา', 'แจ้งเข้ามา', 'ส่งมา', 'บอกมา', 'รวบรวม', 'ฝากแจ้ง']

def is_schedule_question(text):
    t = text.strip()
    # มีคำสั่งสร้างนัดชัด ๆ → ไม่แย่ง ให้ไหลไปโหมดสร้างนัดตามเดิม
    if any(v in t for v in QA_CREATE_VERBS):
        return False
    # ข้อความประกาศให้สมาชิกส่งข้อมูล ไม่ใช่คำถามถึงบอท
    if any(c in t for c in QA_ANNOUNCE_CUES):
        return False
    if 'วันพระ' in t and any(c in t for c in QA_QUESTION_CUES + ['ถัดไป', 'ครั้งหน้า', 'รอบหน้า']):
        return True
    if ('นัด' in t or 'ตาราง' in t) and any(c in t for c in QA_QUESTION_CUES):
        return True
    # คำขอตารางแบบไม่มีคำถาม cue ก็คือคำถามถึงบอท (เคสจริง 23 ต.ค. 2026: อาหนุ่ยพิมพ์
    # "ตารางงานพรุ่งนี้หน่อย" — ไม่มี ไหม/เมื่อไหร่/อะไรบ้าง บอทเลยเงียบ ไม่ตอบอะไรเลย)
    if 'ตาราง' in t and any(d in t for d in ['วันนี้', 'พรุ่งนี้', 'มะรืน', 'สัปดาห์', 'อาทิตย์', 'เดือน']):
        return True
    return False

def is_image_summary_request(text):
    # ขอให้สรุป/อ่าน "รูป" — บอทยังสรุปเนื้อหาจากรูปในแชตไม่ได้ (รูปถูกบันทึกเข้าชีตอย่างเดียว)
    # เคสจริง 23 ต.ค. 2026: "สรุปตารางงานจากรูปที่ส่งนี้ได้ไหม" มีคำว่า ตาราง+ไหม เลยโดน QA ปฏิทิน
    # แย่งไปตอบจากปฏิทินครอบครัวแทน ทั้งที่คนถามหมายถึงรูปตารางสอบที่เพิ่งส่ง — ต้องกันไว้ก่อนถึง QA
    # ยกเว้นงานถอดรูปเที่ยวบิน (ขั้น 0.8) ที่มีทริกเกอร์และโฟลว์ของตัวเอง
    t = text.strip()
    if any(w in t for w in ['เที่ยวบิน', 'ตั๋ว', 'ไฟลต์']):
        return False
    return ('สรุป' in t or 'อ่าน' in t) and any(w in t for w in ['รูป', 'ภาพ'])

def fmt_thai_date(dt):
    return f"{THAI_WEEKDAYS[dt.weekday()]}ที่ {dt.day} {THAI_MONTHS[dt.month]} {dt.year + 543}"

def fmt_thai_datetime(dt):
    return f"{fmt_thai_date(dt)} เวลา {dt.strftime('%H:%M')} น."

def fetch_upcoming_events(days=30, max_results=100):
    tz = pytz.timezone('Asia/Bangkok')
    now = datetime.now(tz)
    service = get_calendar_service()
    res = service.events().list(
        calendarId=FAMILY_CALENDAR_ID,
        timeMin=now.isoformat(),
        timeMax=(now + timedelta(days=days)).isoformat(),
        singleEvents=True,
        orderBy='startTime',
        maxResults=max_results,
    ).execute()
    out = []
    for e in res.get('items', []):
        s = e.get('start', {}).get('dateTime')
        if not s:
            continue  # ข้าม event แบบทั้งวัน (แบบเดียวกับตัวเตือน)
        out.append({
            'summary': e.get('summary', 'ไม่มีหัวข้อ'),
            'start': datetime.fromisoformat(s).astimezone(tz),
            'description': e.get('description', '') or '',
        })
    return out

def answer_wan_phra(text, events):
    tz = pytz.timezone('Asia/Bangkok')
    today = datetime.now(tz).date()
    wp = [e for e in events if 'วันพระ' in e['summary']]
    if not wp:
        return "ยังไม่มีข้อมูลวันพระในปฏิทินครอบครัวครับ"
    if 'เดือนหน้า' in text:
        first_next = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        rest = [e for e in wp if e['start'].year == first_next.year and e['start'].month == first_next.month]
        if not rest:
            return f"เดือน{THAI_MONTHS[first_next.month]} {first_next.year + 543} ไม่มีวันพระในปฏิทินครอบครัวครับ"
        lines = [f"🙏 วันพระในเดือน{THAI_MONTHS[first_next.month]} {first_next.year + 543}:"]
        for e in rest:
            lines.append(f"• {fmt_thai_date(e['start'])} — {e['summary']}")
        return "\n".join(lines)
    if 'เดือนนี้' in text:
        rest = [e for e in wp if e['start'].year == today.year and e['start'].month == today.month]
        if not rest:
            return f"เดือน{THAI_MONTHS[today.month]}นี้ไม่มีวันพระเหลือแล้วครับ"
        lines = [f"🙏 วันพระที่เหลือในเดือน{THAI_MONTHS[today.month]} {today.year + 543}:"]
        for e in rest:
            lines.append(f"• {fmt_thai_date(e['start'])} — {e['summary']}")
        return "\n".join(lines)
    nxt = wp[0]
    delta = (nxt['start'].date() - today).days
    when = "วันนี้เลยครับ" if delta == 0 else ("พรุ่งนี้ครับ" if delta == 1 else f"อีก {delta} วันครับ")
    return f"🙏 {nxt['summary']} ถัดไปคือ {fmt_thai_date(nxt['start'])} — {when}"

def schedule_window(text, now):
    # แปลงคำระบุช่วงเวลาในคำถามเป็นกรอบ [start, end) ตามขอบเขตปฏิทินจริง
    # (บทเรียน 22 ต.ค.: เดิมไม่มีกิ่ง "เดือนหน้า" คำถามเลยตกไปกรอบ default 14 วัน — นัดปลายเดือนถัดไปหลุดจากคำตอบหมด)
    # กิ่งรายวัน (เพิ่ม 23 ต.ค. 2026 เย็น): เดิมไม่มี วันนี้/พรุ่งนี้ เลย — คำถาม "พรุ่งนี้มีนัดอะไรไหม" ตกไปกรอบ
    # default 14 วัน แล้ว AI ต้องเดาเองว่ารายการไหนคือ "พรุ่งนี้" → ในกลุ่ม AI หยิบนัดวันจันทร์ที่ 26 มาตอบ
    # ทั้งที่พรุ่งนี้คือเสาร์ที่ 24 (ส่วน 1-1 ตอบถูกเพราะ AI เดาถูกครั้งนั้น) — วันต้องให้โค้ดกำหนด ไม่ใช่ให้ AI เดา
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if 'มะรืน' in text:
        start = today0 + timedelta(days=2)
        return start, start + timedelta(days=1)
    if 'พรุ่งนี้' in text:
        start = today0 + timedelta(days=1)
        return start, start + timedelta(days=1)
    if 'วันนี้' in text or 'คืนนี้' in text:
        return today0, today0 + timedelta(days=1)
    if 'เดือนหน้า' in text:
        first_next = (now.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (first_next + timedelta(days=32)).replace(day=1)
        return first_next, end
    if 'เดือนนี้' in text:
        end = (now.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return now, end
    if 'อาทิตย์หน้า' in text or 'สัปดาห์หน้า' in text:
        start = (now + timedelta(days=7 - now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=7)
    if 'อาทิตย์นี้' in text or 'สัปดาห์นี้' in text:
        end = (now + timedelta(days=7 - now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return now, end
    return now, now + timedelta(days=14)

def answer_schedule_question(text):
    tz = pytz.timezone('Asia/Bangkok')
    now = datetime.now(tz)
    start, end = schedule_window(text, now)
    # ดึง event ให้ครอบคลุมกรอบที่ถามจริง (เดือนหน้าอาจไกลถึง ~62 วัน) + เผื่อ maxResults กัน event กิจวัตรรายวันดันรายการจริงหลุด
    fetch_days = max(30, (end - now).days + 1)
    events = fetch_upcoming_events(fetch_days, max_results=250)
    # คำถามวันพระตอบด้วยโค้ดล้วน ๆ (แม่นยำ ไม่ต้องใช้ AI)
    if 'วันพระ' in text:
        return answer_wan_phra(text, events)
    if not events:
        return "ช่วงนี้ยังไม่มีนัดอะไรในปฏิทินครอบครัวเลยครับ"
    sel = [e for e in events if start <= e['start'] < end]
    if not sel:
        return "ช่วงที่ถาม ยังไม่มีนัดในปฏิทินครอบครัวครับ"
    # ยุบ event กิจวัตรที่ชื่อซ้ำกันเยอะ ๆ (เช่น เปิดไฟหน้าบ้านทุกวัน) ให้เหลือรายการเดียว กันคำตอบรก
    # ยกเว้น "วันพระ": ชื่อซ้ำกันทุกครั้งแต่เป็นคนละวันที่มีความหมาย — ยุบแล้วคำตอบจะเหลือแค่วันแรก
    # (เคสจริง 22 ต.ค.: ถามนัดเดือน พ.ย. คำตอบโผล่แค่วันพระ 3 กับ 24 พ.ย. เพราะ 9/17 โดนยุบ ส่วน 24 รอดเพราะชื่อต่างเป็น "วันพระ (วันลอยกระทง)")
    counts = {}
    for e in sel:
        counts[e['summary']] = counts.get(e['summary'], 0) + 1
    seen, listing_lines = set(), []
    for e in sel:
        dedupable = 'วันพระ' not in e['summary']
        if dedupable and e['summary'] in seen:
            continue
        seen.add(e['summary'])
        line = f"- {fmt_thai_datetime(e['start'])}: {e['summary']}"
        if e['description']:
            line += f" ({e['description']})"
        if dedupable and counts[e['summary']] > 1:
            line += f" [กิจวัตรซ้ำ {counts[e['summary']]} ครั้งในช่วงนี้]"
        listing_lines.append(line)
    listing = "\n".join(listing_lines)
    # ป้ายชื่อช่วงเวลาแบบ deterministic ให้ AI ใช้เรียกวันในคำตอบ — กัน AI เดาวัน/วันที่เอง
    # (เคส 23 ต.ค. 2026 เย็น: AI เรียกวันจันทร์ที่ 26 ว่า "พรุ่งนี้" ทั้งที่พรุ่งนี้คือเสาร์ที่ 24)
    if (end - start) == timedelta(days=1):
        window_label = fmt_thai_date(start)
    else:
        window_label = f"{fmt_thai_date(start)} ถึง {fmt_thai_date(end - timedelta(days=1))}"
    # ให้ AI ช่วยเรียบเรียงคำตอบจากรายการที่โค้ดคัดกรองตามช่วงเวลามาแล้ว — ตอบจากรายการนี้เท่านั้น
    # บทเรียนเคสจริง 23 ต.ค. 2026: คำถาม "วันนี้มีนัดหมายอะไรไหม" เคยโดน AI ตีความคำว่า "นัดหมาย" เป็นประเภทนัด
    # แล้วตอบว่าไม่มี ทั้งที่รายการมีนัดอยู่ (ส่วนประโยค "มีนัดอะไรไหม" ตอบถูก) — จึงต้อง (1) สั่งใน prompt ว่า
    # ห้ามคัดตามประเภท (2) temperature=0 กันคำตอบแกว่ง (3) มีตัวตรวจทานโค้ดจับคำตอบปฏิเสธทั้งที่รายการไม่ว่าง
    QA_DENIAL_PHRASES = ('ไม่มีนัดลักษณะนั้น', 'ไม่มีข้อมูลนัดหมาย', 'ไม่มีนัดในช่วง', 'ไม่มีนัดอะไร', 'ไม่มีแผนในช่วง')
    if gemini_client:
        for attempt in range(2):
            try:
                response = gemini_client.models.generate_content(
                    model=GEMINI_MODEL,
                    config=types.GenerateContentConfig(temperature=0),
                    contents=f"""
คำถามจากสมาชิกในครอบครัว: "{text}"
ช่วงเวลาที่คำถามหมายถึง (โค้ดกำหนดให้แล้ว ใช้เรียกวันตามนี้เป๊ะ ๆ ห้ามเดาวันเอง): {window_label}

รายการนัดในปฏิทินครอบครัว (เฉพาะช่วงที่เกี่ยวข้อง):
{listing}

ตอบคำถามเป็นภาษาไทย สั้น กระชับ เป็นกันเอง ใช้ข้อมูลจากรายการนัดข้างบนเท่านั้น ห้ามแต่งนัดที่ไม่มีในรายการเด็ดขาด
สำคัญ: คำว่า นัดหมาย / นัด / ตาราง / แผน ในคำถาม หมายถึงทุกรายการในรายการข้างบน โค้ดคัดตามช่วงเวลามาให้แล้ว
ห้ามคัดออกหรือปฏิเสธด้วยเหตุว่าไม่ตรงประเภทนัด — ถ้ารายการข้างบนมีรายการอยู่ ห้ามตอบว่าไม่มีเด็ดขาด ให้เล่าให้ครบทุกรายการ
เฉพาะกรณีที่ถามเจาะจงเรื่องใดเรื่องหนึ่ง (เช่น นัดหมอ) แล้วในรายการไม่มีเรื่องนั้นจริง ๆ ถึงตอบว่าไม่มีนัดลักษณะนั้นในช่วงนี้
ขึ้นต้นคำตอบด้วย 📅 และลงท้ายประโยคด้วย "ครับ"
""",
                )
                ans = (response.text or "").strip()
                if ans:
                    # ตัวตรวจทาน (โค้ดล้วน): รายการไม่ว่างแต่ AI ปฏิเสธภาพรวม = ไม่เชื่อ ใช้คำตอบสำรองจากโค้ดแทน
                    if any(p in ans for p in QA_DENIAL_PHRASES):
                        print(f"Schedule Q&A guard: AI denied despite {len(sel)} event(s) in window, using fallback")
                        break
                    return ans
            except Exception as e:
                print(f"Schedule Q&A Gemini attempt {attempt+1} error: {e}")
    return "📅 นัดที่จะถึง:\n" + listing

# ===== โหมดสรุปนัดจากบทสนทนา (เพิ่ม 22 ต.ค. 2569) =====
# แนวคิด: AI เสนอร่าง คนในกลุ่มยืนยันก่อนเสมอ — บอทไม่มีสิทธิ์ลงปฏิทินเองจากบทสนทนา
# ขั้นตอน: พิมพ์ทริกเกอร์ → บอทอ่าน log บทสนทนาวันนี้ใน Sheets → Gemini สรุปนัดล่าสุดที่ตกลงกันจบ
#         → โพสต์ร่างในกลุ่ม → ใครพิมพ์ "โอเค/เพิ่ม" ภายใน 10 นาที ถึงลงปฏิทินจริง
PLAN_TRIGGERS = ['สรุปนัด', 'สรุปบทสนทนา', 'นัดนี้ลงปฏิทิน', 'สรุปแผนให้หน่อย']
CONFIRM_WORDS = {'โอเค', 'ok', 'okay', 'เพิ่ม', 'ตกลง', 'ยืนยัน', 'ลงเลย', 'เอาเลย', 'ได้เลย'}
REJECT_WORDS = {'ยกเลิก', 'ไม่เอา', 'ไม่ใช่', 'ไม่ต้อง', 'อย่าเพิ่ง'}
PENDING_PLAN = {'data': None, 'expires': None, 'group_id': None}

def normalize_short(text):
    t = text.strip().lower()
    # ตัดคำลงท้ายออกเพียงชั้นเดียว (กันเคส "ได้เลยครับ" โดนตัดสองชั้นจนเหลือ "ได้")
    for suf in ['นะครับ', 'นะคะ', 'ครับ', 'ค่ะ', 'นะ', 'เลย', 'จ้า', 'ฮะ']:
        if t.endswith(suf):
            t = t[:-len(suf)].strip()
            break
    return t.strip('!~. ')

def is_confirm_word(text):
    n = normalize_short(text)
    return len(n) <= 10 and n in CONFIRM_WORDS

def is_reject_word(text):
    n = normalize_short(text)
    return len(n) <= 10 and n in REJECT_WORDS

def fetch_chat_context(max_rows=80):
    # อ่านบันทึกบทสนทนาจากแท็บ log (บอทเขียนไว้ทุกข้อความอยู่แล้ว) — ใช้แถวของวันนี้ก่อน
    try:
        sheet = get_sheet()
        rows = sheet.get_all_values()
    except Exception as e:
        print(f"Fetch chat context error: {e}")
        return ""
    tz = pytz.timezone('Asia/Bangkok')
    today = datetime.now(tz).strftime('%Y-%m-%d')
    valid = [r for r in rows if len(r) >= 5 and r[0][:1].isdigit() and r[4].strip()]
    todays = [r for r in valid if r[0].startswith(today)]
    chosen = todays[-max_rows:] if len(todays) >= 3 else valid[-max_rows:]
    return "\n".join(f"[{r[0]}] {r[1]}: {r[4]}" for r in chosen)

def summarize_plan_from_chat(context):
    if not gemini_client or not context:
        return None
    tz = pytz.timezone('Asia/Bangkok')
    now = datetime.now(tz)
    prompt = f"""วันนี้คือ {fmt_thai_datetime(now)} (วันที่ {now.strftime('%Y-%m-%d')} เวลา {now.strftime('%H:%M')})

ด้านล่างคือบันทึกบทสนทนาในกลุ่ม LINE ครอบครัว (แต่ละบรรทัด: [เวลา] ชื่อผู้ส่ง: เนื้อความที่ระบบสรุปไว้)
---
{context}
---
งานของคุณ: หา "แผนหรือนัดล่าสุดที่คนในกลุ่มตกลงกันจบแล้ว" จากบทสนทนานี้ (มีคนเสนอ แล้วมีคนตอบตกลง/โอเค/ได้เลย) — เลือกแผนที่ใหม่ที่สุดเพียงแผนเดียว

กฎ:
- ถ้าไม่เห็นแผนที่ตกลงกันชัดเจน ให้ตอบ {{"found": false}} เท่านั้น
- เวลาออกเดินทาง เช่น "ล้อหมุน 10:30" ให้ใช้เป็นเวลาเริ่มของนัด
- ถ้าไม่ระบุเวลาจบ ให้ end_time เป็น null
- แปลงวันที่เป็น YYYY-MM-DD โดยอิงวันปัจจุบันด้านบน ("พรุ่งนี้" = วันถัดไป)
- ห้ามแต่งรายละเอียดที่ไม่มีในบทสนทนา

ตอบเป็น JSON อย่างเดียวเท่านั้น รูปแบบ:
{{"found": true, "title": "ชื่อนัดสั้น ๆ", "date": "YYYY-MM-DD", "start_time": "HH:MM", "end_time": "HH:MM หรือ null", "location": "สถานที่ หรือ null", "details": "รายละเอียดสั้น ๆ", "participants": ["ชื่อผู้เกี่ยวข้อง"]}}"""
    for attempt in range(3):
        try:
            response = gemini_client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
            raw = response.text.strip()
            if raw.startswith('```'):
                raw = raw.strip('`')
                if raw.lower().startswith('json'):
                    raw = raw[4:].strip()
            plan = json.loads(raw)
            if not plan.get('found'):
                return None
            # ตรวจรูปแบบขั้นต่ำก่อนเชื่อ AI
            datetime.strptime(plan['date'], '%Y-%m-%d')
            datetime.strptime(plan['start_time'], '%H:%M')
            if plan.get('end_time'):
                datetime.strptime(plan['end_time'], '%H:%M')
            return plan
        except Exception as e:
            print(f"Plan summary attempt {attempt+1} error: {e}")
            time.sleep(2)
    return None

def add_plan_to_calendar(plan):
    try:
        service = get_calendar_service()
        tz = pytz.timezone('Asia/Bangkok')
        start_dt = tz.localize(datetime.strptime(f"{plan['date']}T{plan['start_time']}:00", '%Y-%m-%dT%H:%M:%S'))
        if plan.get('end_time'):
            end_dt = tz.localize(datetime.strptime(f"{plan['date']}T{plan['end_time']}:00", '%Y-%m-%dT%H:%M:%S'))
            if end_dt <= start_dt:
                end_dt = start_dt + timedelta(hours=2)
        else:
            end_dt = start_dt + timedelta(hours=2)
        desc_parts = []
        if plan.get('details'):
            desc_parts.append(plan['details'])
        if plan.get('participants'):
            desc_parts.append("ผู้เกี่ยวข้อง: " + ", ".join(plan['participants']))
        desc_parts.append("(นัดนี้บอทสรุปจากบทสนทนาในกลุ่ม และมีสมาชิกยืนยันก่อนลงปฏิทิน)")
        desc_parts.append(remind_marker([60]))
        event = {
            'summary': plan['title'],
            'description': "\n".join(desc_parts),
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Bangkok'},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Bangkok'},
            'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 60}]},
        }
        if plan.get('location'):
            event['location'] = plan['location']
        result = service.events().insert(calendarId=FAMILY_CALENDAR_ID, body=event).execute()
        # หมายเหตุจากเคสจริง 22 ต.ค.: override การเตือนบางครั้งไม่ติดตอน insert (อีเวนต์แรกได้ useDefault แทน)
        # → ย้ำด้วย patch อีกรอบหลังสร้าง เพื่อให้เตือนก่อน 60 นาทีตามที่ประกาศในกลุ่ม
        try:
            service.events().patch(
                calendarId=FAMILY_CALENDAR_ID, eventId=result['id'],
                body={'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 60}]}},
            ).execute()
        except Exception as e:
            print(f"Reminder patch error: {e}")
        return result.get('htmlLink')
    except Exception as e:
        print(f"Error adding plan to calendar: {e}")
        return None

def format_plan_draft(plan):
    tz = pytz.timezone('Asia/Bangkok')
    d = datetime.strptime(plan['date'], '%Y-%m-%d')
    end_txt = f"–{plan['end_time']}" if plan.get('end_time') else ""
    lines = [
        "📋 **บอทสรุปนัดจากบทสนทนาให้แบบนี้ครับ**",
        f"📌 {plan['title']}",
        f"🗓 {fmt_thai_date(tz.localize(d))} เวลา {plan['start_time']}{end_txt} น.",
    ]
    if plan.get('location'):
        lines.append(f"📍 {plan['location']}")
    if plan.get('details'):
        lines.append(f"📝 {plan['details']}")
    if plan.get('participants'):
        lines.append("👥 " + ", ".join(plan['participants']))
    lines.append("🔔 เตือนก่อน 1 ชั่วโมง")
    lines.append('👉 พิมพ์ "โอเค" หรือ "เพิ่ม" ภายใน 10 นาทีเพื่อลงปฏิทิน — พิมพ์ "ยกเลิก" ถ้าไม่เอา')
    return "\n".join(lines)

def handle_plan_flow(event, msg_text):
    # คืนค่า True = จัดการข้อความนี้แล้ว (ส่ง reply แล้ว) — แต่ยังให้ไหลไปลง log ตามปกติ
    source = event.source
    group_id = getattr(source, 'group_id', None) or getattr(source, 'room_id', None) or getattr(source, 'user_id', None)
    tz = pytz.timezone('Asia/Bangkok')
    now = datetime.now(tz)
    pending = PENDING_PLAN.get('data')
    if pending and PENDING_PLAN.get('expires'):
        if now > PENDING_PLAN['expires']:
            PENDING_PLAN['data'] = None
            pending = None
        elif PENDING_PLAN.get('group_id') == group_id:
            if is_reject_word(msg_text):
                PENDING_PLAN['data'] = None
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="รับทราบครับ ไม่ลงนัดนี้ลงปฏิทิน"))
                return True
            if is_confirm_word(msg_text):
                link = add_plan_to_calendar(pending)
                PENDING_PLAN['data'] = None
                if link:
                    d = tz.localize(datetime.strptime(pending['date'], '%Y-%m-%d'))
                    text = (f"📅 **ลงปฏิทินครอบครัวแล้วครับ!**\n"
                            f"📌 {pending['title']}\n"
                            f"🗓 {fmt_thai_date(d)} เวลา {pending['start_time']} น.\n"
                            f"🔔 บอทจะเตือนในกลุ่มก่อน 1 ชั่วโมงครับ")
                else:
                    text = "❌ ลงปฏิทินไม่สำเร็จ ลองพิมพ์ 'สรุปนัด' ให้บอททำร่างใหม่นะครับ"
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text))
                return True
            return False  # ข้อความอื่นระหว่างรอร่าง — ปล่อย flow ปกติ ร่างยังรอต่อ
    if any(t in msg_text for t in PLAN_TRIGGERS):
        context = fetch_chat_context()
        plan = summarize_plan_from_chat(context)
        if not plan:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text="🔍 เราดูบทสนทนาวันนี้แล้ว ยังไม่เจอนัดที่ตกลงกันจบแล้วครับ ถ้ามีแผน พิมพ์สั่งบอทแบบเดิม (เช่น 'นัดหมาย ...') ได้เลย"))
            return True
        PENDING_PLAN['data'] = plan
        PENDING_PLAN['expires'] = now + timedelta(minutes=10)
        PENDING_PLAN['group_id'] = group_id
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=format_plan_draft(plan)))
        return True
    return False

# ===== โหมดสรุปเที่ยวบินจากรูปแผนเดินทาง (เพิ่ม 22 ต.ค. 2569) =====
# แนวคิดเดียวกับโหมดสรุปนัด: AI ถอดข้อมูล เสนอร่าง คนในกลุ่มยืนยันก่อนลงปฏิทินเสมอ
# ขั้นตอน: ใครส่งรูปตั๋ว/แผนเดินทางเข้ากลุ่มไว้ก่อน → พิมพ์ทริกเกอร์ "สรุปเที่ยวบิน"
#         → บอทดึงรูปล่าสุดจาก log (ลิงก์ Cloudinary คอลัมน์ H) → Gemini ถอดเป็น JSON + ตรวจรูปแบบ
#         → โพสต์ร่างในกลุ่ม → ใครพิมพ์ "โอเค/เพิ่ม" ภายใน 10 นาที ถึงสร้างอีเวนต์เที่ยวบินจริง (ขาละ 1 อีเวนต์)
TRAVEL_TRIGGERS = ['สรุปเที่ยวบิน', 'สรุปการเดินทาง', 'สรุปแผนเดินทาง', 'สรุปตั๋ว', 'ถอดตั๋ว', 'ลงตารางเที่ยวบิน']
PENDING_TRAVEL = {'data': None, 'expires': None, 'group_id': None}
TRAVEL_REMINDERS = [1440, 180]  # เตือนล่วงหน้า 1 วัน + 3 ชั่วโมงก่อนออกเดินทาง

def find_latest_image_urls(limit=3):
    # หา URL รูปล่าสุดจากแท็บ log (คอลัมน์ H) เรียงจากใหม่สุด — ดูย้อนหลังหลายใบเผื่อรูปล่าสุดไม่ใช่เอกสารเดินทาง
    try:
        rows = get_sheet().get_all_values()
    except Exception as e:
        print(f"Find latest image error: {e}")
        return []
    urls = []
    for r in reversed(rows):
        if len(r) >= 8 and isinstance(r[7], str) and r[7].startswith('http'):
            urls.append(r[7])
            if len(urls) >= limit:
                break
    return urls

def download_image_bytes(url):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as e:
        print(f"Download image error: {e}")
        return None

def validate_itinerary(data):
    # ตรวจข้อมูลที่ AI ถอดมาก่อนเชื่อ: รูปแบบวันที่/เวลา + ช่วงวันที่สมเหตุสมผล + แปลงปี พ.ศ. ที่หลุดมา
    try:
        legs = data.get('legs') or []
        if not legs or len(legs) > 6:
            return None
        tz = pytz.timezone('Asia/Bangkok')
        today = datetime.now(tz).date()
        clean_legs = []
        for leg in legs:
            d = datetime.strptime(str(leg.get('date', '')), '%Y-%m-%d').date()
            if d.year > 2400:  # AI ส่งปี พ.ศ. หลุดมา → แปลงเป็น ค.ศ.
                d = d.replace(year=d.year - 543)
            datetime.strptime(str(leg.get('depart', '')), '%H:%M')
            datetime.strptime(str(leg.get('arrive', '')), '%H:%M')
            if not (leg.get('from_city') and leg.get('to_city')):
                return None
            if not (today - timedelta(days=1) <= d <= today + timedelta(days=370)):
                return None
            leg = dict(leg)
            leg['date'] = d.strftime('%Y-%m-%d')
            clean_legs.append(leg)
        passengers = [str(p).strip() for p in (data.get('passengers') or []) if str(p).strip()]
        return {
            'is_itinerary': True,
            'booking_ref': data.get('booking_ref') or None,
            'passengers': passengers[:6],
            'legs': clean_legs,
        }
    except Exception as e:
        print(f"Itinerary validate error: {e}")
        return None

def extract_itinerary_from_image(image_bytes):
    if not gemini_client or not image_bytes:
        return None
    prompt = """รูปนี้อาจเป็นเอกสารแผนการเดินทาง (ตั๋วเครื่องบิน/รถไฟ/รถทัวร์ หรือหน้ายืนยันการจองจาก Trip.com, Traveloka, สายการบิน ฯลฯ)

งานของคุณ: ถอดข้อมูลการเดินทางจากรูปให้ครบทุกเที่ยว (legs) เฉพาะที่เห็นในรูปจริง ๆ ห้ามเดาหรือแต่งเพิ่ม

กฎสำคัญ:
- ถ้ารูปไม่ใช่เอกสารการเดินทางเลย ให้ตอบ {"is_itinerary": false} เท่านั้น
- วันที่: ตอบเป็นปีคริสต์ศักราชรูปแบบ YYYY-MM-DD เสมอ — ถ้าในรูปเป็นปีพุทธศักราช (เช่น 2569) ให้ลบ 543 ก่อน (2569 → 2026)
- เวลา: รูปแบบ HH:MM (24 ชั่วโมง)
- ชื่อผู้โดยสาร: คัดลอกตามที่พิมพ์ในเอกสาร (ตัวพิมพ์ใหญ่ภาษาอังกฤษได้)
- ถ้าอ่านช่องไหนไม่ออก ให้ใส่ null แทนการเดา

ตอบเป็น JSON อย่างเดียวเท่านั้น รูปแบบ:
{"is_itinerary": true, "booking_ref": "เลขที่การจอง หรือ null", "passengers": ["ชื่อผู้โดยสาร"], "legs": [{"airline": "ชื่อสายการบิน/ผู้ให้บริการ หรือ null", "flight_no": "หมายเลขเที่ยวบิน/ขบวน หรือ null", "from_city": "เมืองต้นทาง", "from_airport": "สนามบิน/สถานีต้นทาง หรือ null", "to_city": "เมืองปลายทาง", "to_airport": "สนามบิน/สถานีปลายทาง หรือ null", "date": "YYYY-MM-DD", "depart": "HH:MM", "arrive": "HH:MM"}]}"""
    for attempt in range(3):
        try:
            response = gemini_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[prompt, types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")],
            )
            raw = response.text.strip()
            if raw.startswith('```'):
                raw = raw.strip('`')
                if raw.lower().startswith('json'):
                    raw = raw[4:].strip()
            data = json.loads(raw)
            if not data.get('is_itinerary'):
                return {'is_itinerary': False}
            valid = validate_itinerary(data)
            if valid:
                return valid
            print(f"Itinerary attempt {attempt+1}: ถอดได้แต่ข้อมูลไม่ผ่านการตรวจ ลองใหม่")
            time.sleep(1)
        except Exception as e:
            print(f"Itinerary extract attempt {attempt+1} error: {e}")
            time.sleep(2)
    return None

def add_flight_events(data):
    # สร้างอีเวนต์ปฏิทินขาละ 1 อีเวนต์ คืนจำนวนที่สร้างสำเร็จ
    try:
        service = get_calendar_service()
    except Exception as e:
        print(f"Flight calendar service error: {e}")
        return 0
    tz = pytz.timezone('Asia/Bangkok')
    overrides = [{'method': 'popup', 'minutes': m} for m in TRAVEL_REMINDERS]
    created = 0
    for leg in data['legs']:
        try:
            start_dt = tz.localize(datetime.strptime(f"{leg['date']}T{leg['depart']}:00", '%Y-%m-%dT%H:%M:%S'))
            end_dt = tz.localize(datetime.strptime(f"{leg['date']}T{leg['arrive']}:00", '%Y-%m-%dT%H:%M:%S'))
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)  # เที่ยวบินข้ามคืน: เวลาถึงเป็นของวันถัดไป
            code = (leg.get('flight_no') or '').strip()
            title = f"✈️ {code + ' ' if code else ''}{leg['from_city']} → {leg['to_city']}"
            desc = []
            route = leg['from_city']
            if leg.get('from_airport'):
                route += f" ({leg['from_airport']})"
            route += f" → {leg['to_city']}"
            if leg.get('to_airport'):
                route += f" ({leg['to_airport']})"
            desc.append(f"เส้นทาง: {route}")
            carrier = ' '.join(x for x in [(leg.get('airline') or '').strip(), code] if x)
            if carrier:
                desc.append(f"เที่ยวบิน: {carrier}")
            if data.get('passengers'):
                desc.append("ผู้โดยสาร: " + ", ".join(data['passengers']))
            if data.get('booking_ref'):
                desc.append(f"เลขที่การจอง: {data['booking_ref']}")
            desc.append("⏰ เผื่อเวลาเช็กอินเที่ยวบินในประเทศอย่างน้อย 1.5–2 ชั่วโมง")
            desc.append("(บอทถอดจากรูปแผนเดินทางในกลุ่ม และมีสมาชิกยืนยันก่อนลงปฏิทิน)")
            desc.append(remind_marker(TRAVEL_REMINDERS))
            event = {
                'summary': title,
                'description': "\n".join(desc),
                'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Bangkok'},
                'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Bangkok'},
                'reminders': {'useDefault': False, 'overrides': overrides},
            }
            if leg.get('from_airport'):
                event['location'] = leg['from_airport']
            result = service.events().insert(calendarId=FAMILY_CALENDAR_ID, body=event).execute()
            # ย้ำ reminder ด้วย patch (บทเรียนเดียวกับโหมดสรุปนัด: override ตอน insert บางครั้งไม่ติด)
            try:
                service.events().patch(
                    calendarId=FAMILY_CALENDAR_ID, eventId=result['id'],
                    body={'reminders': {'useDefault': False, 'overrides': overrides}},
                ).execute()
            except Exception as e:
                print(f"Flight reminder patch error: {e}")
            created += 1
        except Exception as e:
            print(f"Error adding flight event: {e}")
    return created

def format_travel_draft(data):
    tz = pytz.timezone('Asia/Bangkok')
    lines = ["✈️ **บอทถอดแผนเดินทางจากรูปให้แบบนี้ครับ**"]
    if data.get('passengers'):
        lines.append("👥 ผู้โดยสาร: " + ", ".join(data['passengers']))
    if data.get('booking_ref'):
        lines.append(f"🎫 เลขที่การจอง: {data['booking_ref']}")
    for i, leg in enumerate(data['legs'], 1):
        d = tz.localize(datetime.strptime(leg['date'], '%Y-%m-%d'))
        code = (leg.get('flight_no') or '').strip()
        lines.append(f"{i}) {code + ' ' if code else ''}{leg['from_city']} → {leg['to_city']}")
        lines.append(f"🗓 {fmt_thai_date(d)} เวลา {leg['depart']}–{leg['arrive']} น.")
    lines.append("🔔 เตือนล่วงหน้า 1 วัน และ 3 ชั่วโมงก่อนออกเดินทาง")
    lines.append('👉 พิมพ์ "โอเค" หรือ "เพิ่ม" ภายใน 10 นาทีเพื่อลงปฏิทิน — พิมพ์ "ยกเลิก" ถ้าไม่เอา')
    return "\n".join(lines)

def handle_travel_flow(event, msg_text):
    # คืนค่า True = จัดการข้อความนี้แล้ว (ส่ง reply แล้ว) — แต่ยังให้ไหลไปลง log ตามปกติ (แบบเดียวกับโหมดสรุปนัด)
    source = event.source
    group_id = getattr(source, 'group_id', None) or getattr(source, 'room_id', None) or getattr(source, 'user_id', None)
    tz = pytz.timezone('Asia/Bangkok')
    now = datetime.now(tz)
    pending = PENDING_TRAVEL.get('data')
    if pending and PENDING_TRAVEL.get('expires'):
        if now > PENDING_TRAVEL['expires']:
            PENDING_TRAVEL['data'] = None
            pending = None
        elif PENDING_TRAVEL.get('group_id') == group_id:
            if is_reject_word(msg_text):
                PENDING_TRAVEL['data'] = None
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="รับทราบครับ ไม่ลงแผนเดินทางนี้ลงปฏิทิน"))
                return True
            if is_confirm_word(msg_text):
                created = add_flight_events(pending)
                PENDING_TRAVEL['data'] = None
                if created:
                    first = pending['legs'][0]
                    d = tz.localize(datetime.strptime(first['date'], '%Y-%m-%d'))
                    text = (f"📅 **ลงปฏิทินครอบครัวแล้วครับ!**\n"
                            f"✈️ ทั้งหมด {created} เที่ยวบิน\n"
                            f"🗓 เที่ยวแรก: {fmt_thai_date(d)} เวลา {first['depart']} น.\n"
                            f"🔔 บอทจะเตือนในกลุ่มล่วงหน้า 1 วัน และ 3 ชั่วโมงก่อนบินครับ")
                else:
                    text = "❌ ลงปฏิทินไม่สำเร็จ ลองพิมพ์ 'สรุปเที่ยวบิน' ให้บอททำร่างใหม่นะครับ"
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text))
                return True
            return False  # ข้อความอื่นระหว่างรอร่าง — ปล่อย flow ปกติ ร่างยังรอต่อ
    if any(t in msg_text for t in TRAVEL_TRIGGERS):
        found = None
        for url in find_latest_image_urls(3):
            image_bytes = download_image_bytes(url)
            if not image_bytes:
                continue
            result = extract_itinerary_from_image(image_bytes)
            if result and result.get('is_itinerary'):
                found = result
                break
        if not found:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text="🔍 เราดูรูปล่าสุดในบันทึกแล้ว ยังไม่เจอแผนเดินทางที่ถอดได้ครับ — ส่งรูปตั๋ว/แผนเดินทางเข้ากลุ่มก่อน แล้วพิมพ์ 'สรุปเที่ยวบิน' อีกครั้งนะครับ"))
            return True
        PENDING_TRAVEL['data'] = found
        PENDING_TRAVEL['expires'] = now + timedelta(minutes=10)
        PENDING_TRAVEL['group_id'] = group_id
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=format_travel_draft(found)))
        return True
    return False

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

        # 0.5 ถาม-ตอบตารางนัด/วันพระ: ถ้าเป็นคำถามเรื่องตาราง ให้ตอบจากปฏิทินจริงก่อน
        # (ตอบแล้วข้ามโหมดสร้างนัด แต่ยังไหลไปบันทึก log + แจ้งแอดมินตามปกติ)
        qa_answer = None
        if is_image_summary_request(msg_text):
            # ขอสรุปจากรูป: ตอบตามจริงว่ายังทำไม่ได้ แทนที่จะหลุดไปตอบจากปฏิทิน (เคส 23 ต.ค. 2026)
            qa_answer = "ขออภัยครับ ตอนนี้บอทยังสรุปเนื้อหาจากรูปในแชตไม่ได้ครับ 📷 รูปถูกบันทึกเข้าชีตให้เรียบร้อยแล้ว ถ้าต้องการให้สรุปเป็นนัดหรือลงปฏิทิน พิมพ์รายละเอียดเป็นข้อความมาได้เลยครับ"
        elif is_schedule_question(msg_text):
            try:
                qa_answer = answer_schedule_question(msg_text)
            except Exception as e:
                print(f"Schedule Q&A error: {e}")
        if qa_answer:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=qa_answer))

        # 0.7 สรุปนัดจากบทสนทนา: ทริกเกอร์ขอร่างสรุป หรือคำยืนยัน/ปฏิเสธร่างที่รออยู่
        plan_handled = False
        if not qa_answer:
            try:
                plan_handled = handle_plan_flow(event, msg_text)
            except Exception as e:
                print(f"Plan flow error: {e}")

        # 0.8 สรุปเที่ยวบินจากรูปแผนเดินทาง: ทริกเกอร์ขอร่างถอดรูป หรือคำยืนยัน/ปฏิเสธร่างที่รออยู่
        travel_handled = False
        if not qa_answer and not plan_handled:
            try:
                travel_handled = handle_travel_flow(event, msg_text)
            except Exception as e:
                print(f"Travel flow error: {e}")

        # 1. ตรวจสอบนัดหมายปฏิทิน (ข้ามถ้าตอบคำถามไปแล้วในข้อ 0.5 หรือจัดการในข้อ 0.7/0.8)
        cal_intent = None if (qa_answer or plan_handled or travel_handled) else parse_calendar_intent(msg_text)
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

        # 3. ส่ง Log รายงานส่วนตัวไปหาแอดมินโดยตรง (push — เปิดเฉพาะเมื่อ TRAIN_PUSH_ENABLED=1 กันโควตาหมด)
        if ADMIN_USER_ID and TRAIN_PUSH_ENABLED:
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
