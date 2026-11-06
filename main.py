import os
# BOT VERSION: 2026-11-06-r18 — รอบ 18 (แก้บั๊กจากเคสจริง 5 พ.ย. 2569 สองจุด: (1) ทักทายเช้าส่งซ้ำ 2 รอบในวินาทีเดียวกัน เพราะแม่หน่อยส่งข้อความติดกัน 2 อันห่างกัน 2 วินาที แล้วธง "ทักไปแล้ว" ถูกตั้งหลังดึงข้อมูลอากาศเสร็จ — เปลี่ยนเป็นจองสิทธิ์ทักก่อนเริ่มดึงข้อมูล + ล็อกกันประมวลผลพร้อมกัน; (2) log วันเดียวกันมี Gemini ตอบ 503 UNAVAILABLE (โมเดลแน่น) เป็นช่วง ๆ ช่วงบ่าย–ค่ำ แต่ retry เดิมเว้นแค่ 2 วินาทีสั้นกว่า spike — เพิ่มตัวช่วยคำนวณเวลารอ: เจอ 503 ให้เว้น 5/12 วินาทีแทน)
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
from linebot.models import PostbackEvent, FlexSendMessage, BubbleContainer, BoxComponent, TextComponent, ButtonComponent, PostbackAction, SeparatorComponent

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

# ===== (เก็บกวาดรอบ 9, 31 ต.ค. 2569) โค้ดช่องสำรอง "Bot B" ถูกลบออกทั้งหมดแล้ว =====
# ที่มาเดิม: ต.ค. 2569 โควตา push ของ Bot A หมดกลางเดือน เลยเคยสร้าง Bot B เป็นอีก Official Account ไว้ push แทนตอน A เจอ 429
# แต่ข้อเท็จจริงที่ยืนยันจากเอกสาร LINE + เคสจริง 24 ต.ค. 2569: กลุ่มแชท 1 กลุ่มมี Official Account ได้ทีละ 1 ตัวเท่านั้น
# B จึงเข้ากลุ่มเดียวกับ A ไม่ได้เลย (ค้าง Pending ตลอด) — failover ข้าม OA ในกลุ่มเดียวกันทำไม่ได้โดยโครงสร้าง
# นายลบ Bot B ทิ้งทั้งหมดตั้งแต่ 24 ต.ค. ระบบเหลือ A ช่องเดียว โค้ดที่เหลืออยู่ (ตัวอ่าน env ของ B, ตัวส่ง failover,
# webhook ของ B) เป็น dead code ที่ไม่มีวันถูกเรียก จึงเก็บกวาดออกในรอบนี้ เหลือแต่คอมเมนต์นี้ไว้เล่าที่มา

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

PROFILE_CACHE = {}

def get_user_display_name(user_id, group_id=None):
    # ชื่อที่แสดงของสมาชิก — ลำดับ: แคชในหน่วยความจำ → โปรไฟล์สมาชิกกลุ่ม → โปรไฟล์เพื่อน → "Unknown User"
    # เคสจริง 21 ต.ค. 2569 (แพตช์รอบ 9): log มี get_profile ตอบ 404 ซ้ำ ๆ สำหรับสมาชิกอย่างน้อย 1 คน
    # เพราะสมาชิกกลุ่มไม่จำเป็นต้องแอดบอทเป็น "เพื่อน" — โปรไฟล์เพื่อน (get_profile) จึงไม่มีของคนกลุ่มนี้
    # วิธีที่ถูก: ขอ "โปรไฟล์สมาชิกกลุ่ม" (get_group_member_profile) ซึ่งใช้ได้กับสมาชิกกลุ่มทุกคน
    # และเลิก retry 3 รอบแบบเดิม: 404 แปลว่าไม่มีข้อมูล ไม่ใช่เครือข่ายสะดุด ยิ่ง retry ยิ่งช้าและ log รก
    # แคชชื่อไว้ในหน่วยความจำด้วย (ชื่อแทบไม่เปลี่ยน) ข้อความถัด ๆ ไปของคนเดิมจะไม่ยิง API ซ้ำ
    if not user_id:
        return "Unknown User"
    if user_id in PROFILE_CACHE:
        return PROFILE_CACHE[user_id]
    name = None
    if group_id:
        try:
            name = line_bot_api.get_group_member_profile(group_id, user_id).display_name
        except Exception as e:
            print(f"Group member profile lookup failed for {user_id}: {e}")
    if not name:
        try:
            name = line_bot_api.get_profile(user_id).display_name
        except Exception as e:
            print(f"Profile lookup failed for {user_id}: {e}")
    if name:
        PROFILE_CACHE[user_id] = name
        return name
    return "Unknown User"

def gemini_retry_delay(attempt, error):
    # รอบ 18 — เคสจริง 5 พ.ย. 2569: log มี Gemini ตอบ 503 UNAVAILABLE ("model is currently
    # experiencing high demand") ตอน 15:06, 18:22 และ 19:00–19:03 แต่ retry เดิมของทุกฟังก์ชัน
    # เว้นแค่ 2 วินาทีแล้วลองใหม่ทันที 3 รอบจบในไม่กี่วินาที — สั้นกว่าระยะที่ฝั่งโมเดลแน่นอยู่มาก
    # เลยพลาดทุกรอบทั้งที่รออีกนิดอาจผ่าน หลักการ: error แบบ "เซิร์ฟเวอร์แน่นชั่วคราว" (503)
    # ต้องเว้นจังหวะให้นานขึ้นแบบขั้นบันได (5 วิ แล้ว 12 วิ) ส่วน error อื่นคงรอ 2 วิเท่าเดิม
    # เพราะส่วนใหญ่ไม่ใช่เรื่องรอแล้วหาย (เช่น 404 หรือ JSON พัง รอไปก็ไม่ช่วย)
    if '503' in str(error) or 'UNAVAILABLE' in str(error):
        return (5, 12, 12)[min(attempt, 2)]
    return 2

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
            time.sleep(gemini_retry_delay(attempt, e))
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

# นัดล่าสุดที่บอทเพิ่งสร้างผ่านโหมดสร้างนัด — ใช้กับคำสั่งต่อเนื่อง "เพิ่มแจ้งเตือนล่วงหน้า..." (เพิ่ม 30 ต.ค. 2569)
LAST_EVENT = {'event_id': None, 'summary': None, 'start_time': None, 'reminders': [], 'scope': None, 'expires': None}

# ===== alias ชื่อคนในครอบครัว (เพิ่ม 31 ต.ค. 2569 ข้อ 1) =====
# เคสจริง 30 ต.ค.: ใบนัด รพ. ของอากงใช้ชื่อจริง "เทียนชัย ตั้งทอง" แต่คนในกลุ่มเรียก อากง / พ่อ / เชฟเทียน
# ถ้าไม่สอนบอท ชื่อเหล่านี้จะถูกมองเป็นคนละคน ทั้งตอนสร้างนัด (summary คนละชื่อ) และตอนถาม-ตอบตาราง
# วิธีใช้: ตารางนี้ป้อนเข้า prompt ของ AI ทุกจุดที่ตีความชื่อคน (สร้างนัด / ถาม-ตอบ / สรุปนัดจากบทสนทนา)
# ให้ AI ใช้ชื่อ canonical เขียน summary เสมอ — ไม่ใช้ substring match ในโค้ด deterministic เพราะชื่อเล่นสั้น ๆ
# เช่น "นุก" ชนกับคำว่า "สนุก" ได้ (เคสจริง: อากงพิมพ์ "สนุกดี" ในกลุ่ม) บริบทเป็นงานของ AI ไม่ใช่ของ regex
FAMILY_ALIASES = [
    ('อากง', ['อากง', 'พ่อ', 'เชฟเทียน', 'เทียนชัย ตั้งทอง', 'เทียนชัย']),
    ('อาหนุ่ย', ['อาหนุ่ย', 'หนุ่ย']),
    ('อานุก', ['อานุก', 'ช่างนุก']),
]
ALIAS_NOTE_FOR_AI = (
    "หมายเหตุชื่อคนในครอบครัว (ชื่อในกลุ่มเดียวกันคือคนเดียวกัน ห้ามมองเป็นคนละคน): "
    "อากง = พ่อ = เชฟเทียน = เทียนชัย = เทียนชัย ตั้งทอง; "
    "อาหนุ่ย = หนุ่ย; อานุก = ช่างนุก = นุก"
)
ALIAS_RULE_FOR_AI = (
    ALIAS_NOTE_FOR_AI
    + " — ถ้าข้อความเอ่ยถึงคนเหล่านี้ด้วยชื่อใดก็ตาม ให้ใช้ชื่อแรกของกลุ่ม (อากง / อาหนุ่ย / อานุก) ใน summary เสมอ"
)

# ตัวแทนชื่อแบบ deterministic สำหรับ "ชื่อเฉพาะยาว" ที่ไม่มีความกำกวม (เพิ่ม 31 ต.ค. 2569 รอบ 5)
# เหตุผล: เทสต์สด r4 prompt อย่างเดียวไม่พอ — Gemini ยังลอกชื่อจากประโยคผู้ใช้ตรง ๆ ("พานายเทียนชัย ตั้งทอง")
# ชื่อเต็ม/ชื่อเฉพาะแบบนี้แทนที่ด้วยโค้ดได้ปลอดภัย (เรียงยาวก่อนสั้น กันแทนที่ซ้อน) ส่วนชื่อสั้นกำกวมยังปล่อยให้ AI ตีบริบท
PERSON_NAME_REPLACEMENTS = [
    ('นายเทียนชัย ตั้งทอง', 'อากง'),
    ('คุณเทียนชัย ตั้งทอง', 'อากง'),
    ('เทียนชัย ตั้งทอง', 'อากง'),
    ('เชฟเทียน', 'อากง'),
    ('เทียนชัย', 'อากง'),
    ('ช่างนุก', 'อานุก'),
]

def normalize_person_names(text):
    if not text:
        return text
    out = text
    for old, new in PERSON_NAME_REPLACEMENTS:
        out = out.replace(old, new)
    return out

# ===== ความหมายเรื่องเวลาของนัด 2 แบบ (นายอธิบาย 31 ต.ค. 2569 รอบ 7) =====
# แบบที่ 1 นัดไป รพ.: หัวใจคือ "แจ้งเตือนล่วงหน้า 1 วัน" (ไว้เตรียมตัว/เอกสาร) ส่วนเวลาในใบนัดคือเวลาเจอหมอ
#   ไม่ใช่เวลาออกบ้าน — บ้านนี้ออกบ้าน 07:00 ไม่เกิน 08:00 สำหรับนัดเช้า ส่วนนัดบ่าย (ไม่บ่อย) ออกก่อนเที่ยง
# แบบที่ 2 นัดออกไปข้างนอกทั่วไป: หัวใจคือ "เวลาออกบ้าน" ที่ชัดเจน (เช่น 07:00/08:00 หรือไปห้าง/กินข้าวเที่ยง ~10:00–10:30)
#   ถ้าไม่ระบุ บอทถาม "ออกบ้านกี่โมง" ตามหลังได้เลย — นายบอกถามทุกครั้งก็ยังได้ ทุกคนในกลุ่มจะได้รู้พร้อมกัน
HOSPITAL_KEYWORDS = ['โรงพยาบาล', 'รพ.', 'หาหมอ', 'พบหมอ', 'พบแพทย์', 'หมอนัด', 'คลินิก', 'ทำฟัน', 'ทันต',
                     'เจาะเลือด', 'เอกซเรย์', 'x-ray', 'mri', 'ตรวจสุขภาพ', 'ฉีดวัคซีน', 'ใบนัด']

def is_hospital_appointment(*texts):
    joined = ' '.join(t for t in texts if t).lower()
    return any(k in joined for k in HOSPITAL_KEYWORDS)

def ensure_hospital_reminders(reminder_list, *texts):
    # นัด รพ. ต้องมีเตือนล่วงหน้า 1 วันเสมอ (ถ้าผู้ใช้ยังไม่ได้กำหนดไว้) — คืนรายการเรียงมากไปน้อย สูงสุด 5 ค่า
    if not is_hospital_appointment(*texts):
        return reminder_list
    return sorted(set([1440] + [int(m) for m in reminder_list]), reverse=True)[:5]

PENDING_DEPARTURE = {'event_id': None, 'summary': None, 'scope': None, 'expires': None}
DEPARTURE_QUESTION = ("🚗 นัดนี้ออกบ้านกันกี่โมงครับ? พิมพ์บอกสั้น ๆ ได้เลย (เช่น \"ออกบ้าน 7 โมง\") "
                      "บอทจะจดลงนัดให้ทุกคนเห็นครับ")
STAY_HOME_KEYWORDS = ['ทานยา', 'กินยา', 'เข้านอน', 'เปิดไฟ', 'ปิดไฟ', 'ออกกำลังกาย']

def should_ask_departure(summary, source_text, recurrence):
    # ไม่ถามกับนัดทำซ้ำ (กิจวัตร) และนัดที่ทำที่บ้าน ไม่ต้อง "ออกบ้าน" ไปไหน
    if recurrence:
        return False
    joined = f"{summary or ''} {source_text or ''}"
    if any(k in joined for k in STAY_HOME_KEYWORDS):
        return False
    return True

def remember_departure_question(summary, scope):
    if not LAST_EVENT.get('event_id'):
        return
    PENDING_DEPARTURE['event_id'] = LAST_EVENT['event_id']
    PENDING_DEPARTURE['summary'] = summary
    PENDING_DEPARTURE['scope'] = scope
    PENDING_DEPARTURE['expires'] = datetime.now(pytz.timezone('Asia/Bangkok')) + timedelta(minutes=10)

def parse_departure_time(text):
    # คำตอบเวลาออกบ้านสั้น ๆ เช่น "7 โมง" / "ออกบ้าน 7 โมงครึ่ง" / "ออกบ้านตอนสองทุ่ม" → (ชั่วโมง, นาที) หรือ None
    t = (text or '').strip()
    if len(t) > 30:
        return None
    if any(v in t for v in QA_CREATE_VERBS) or 'ไหม' in t or 'มั้ย' in t or '?' in t:
        return None
    if not any(w in t for w in ['โมง', 'ทุ่ม', 'ตี', ':', 'เที่ยง']):
        return None
    if 'ก่อนเที่ยง' in t:
        return None  # "ออกก่อนเที่ยง" เป็นช่วงเวลา ไม่ใช่เวลาตายตัว — ไม่ตีความเป็น 12:00 มั่ว ๆ
    seg = _normalize_thai_clock_words(t)
    half = 30 if 'ครึ่ง' in seg else 0
    m = re.search(r'(\d{1,2})[:.](\d{2})', seg)
    if m:
        return int(m.group(1)) % 24, int(m.group(2)) % 60
    m = re.search(r'บ่าย\s*(\d{1,2})', seg)
    if m:
        return 12 + (int(m.group(1)) % 12), half
    m = re.search(r'ตี\s*(\d{1,2})', seg)
    if m:
        return int(m.group(1)) % 12, half
    m = re.search(r'(\d{1,2})\s*ทุ่ม', seg)
    if m:
        return (18 + int(m.group(1))) % 24, half
    m = re.search(r'(\d{1,2})\s*โมง', seg)
    if m:
        h = int(m.group(1)) % 12
        if 'เย็น' in seg and h < 12:
            h += 12
        return h, half
    if 'เที่ยงคืน' in seg:
        return 0, 0
    if 'เที่ยง' in seg:
        return 12, 0
    return None

def handle_departure_answer(event, msg_text):
    # คืนข้อความตอบกลับ หรือ None ถ้าข้อความนี้ไม่ใช่คำตอบเวลาออกบ้านของนัดที่บอทถามค้างไว้
    parsed = parse_departure_time(msg_text)
    if not parsed:
        return None
    scope = qa_scope(event)
    now = datetime.now(pytz.timezone('Asia/Bangkok'))
    if (not PENDING_DEPARTURE.get('event_id') or PENDING_DEPARTURE.get('scope') != scope
            or not PENDING_DEPARTURE.get('expires') or now > PENDING_DEPARTURE['expires']):
        return None
    hh, mm = parsed
    try:
        service = get_calendar_service()
        ev = service.events().get(calendarId=FAMILY_CALENDAR_ID, eventId=PENDING_DEPARTURE['event_id']).execute()
        desc = ev.get('description', '') or ''
        line = f"🚗 เวลาออกบ้าน: {hh:02d}:{mm:02d} น."
        if line not in desc:
            new_desc = insert_before_remind_marker(desc, line)
            service.events().patch(
                calendarId=FAMILY_CALENDAR_ID,
                eventId=PENDING_DEPARTURE['event_id'],
                body={'description': new_desc},
            ).execute()
        summary = PENDING_DEPARTURE.get('summary', 'นัดหมาย')
        PENDING_DEPARTURE['event_id'] = None
        return f"🚗 รับทราบครับ นัด \"{summary}\" ออกบ้าน {hh:02d}:{mm:02d} น. นะครับ (จดลงปฏิทินให้แล้วครับ)"
    except Exception as e:
        print(f"Departure answer error: {e}")
        return None

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

กติกาเรื่องชื่อคนในครอบครัว: {ALIAS_RULE_FOR_AI}

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
            time.sleep(gemini_retry_delay(attempt, e))
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
        # จำนัดล่าสุดไว้ให้คำสั่งต่อเนื่อง "เพิ่มแจ้งเตือน..." (scope จะถูกผูกโดยผู้เรียกใน handle_text_message)
        LAST_EVENT['event_id'] = event_result.get('id')
        LAST_EVENT['summary'] = summary
        LAST_EVENT['start_time'] = start_time_str
        LAST_EVENT['reminders'] = [int(m) for m in reminder_minutes_list[:5]]
        LAST_EVENT['scope'] = None
        LAST_EVENT['expires'] = datetime.now(pytz.timezone('Asia/Bangkok')) + timedelta(minutes=15)
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

def parse_add_reminder_minutes(text):
    # คำสั่งต่อเนื่อง "เพิ่มแจ้งเตือนล่วงหน้า 1 วัน" — คืนจำนวนนาที หรือ None ถ้าไม่ใช่คำสั่งนี้ (เพิ่ม 30 ต.ค. 2569)
    # เคสจริง 30 ต.ค.: นายพิมพ์สั่งหลังบอทเพิ่งลงนัดอากง แต่บอทเงียบ เพราะข้อความนี้ไม่ใช่ทั้งคำถามตาราง
    # และไม่ใช่นัดใหม่ (ไม่มีวัน/เวลาเริ่มของตัวเอง) — ต้องมีตัวดักเฉพาะก่อนถึงโหมดสร้างนัด
    t = text.strip()
    if any(v in t for v in QA_CREATE_VERBS):
        return None
    # รับทั้ง "แจ้งเตือน" และ "แจ้งล่วงหน้า" (เคสเทสต์สด 21:03: นายพิมพ์ "เพิ่มแจ้งล่วงหน้า 1 วันให้ด้วย" ไม่มีคำว่า เตือน บอทเลยเงียบ)
    if ('เตือน' not in t and 'แจ้งล่วงหน้า' not in t) or ('เพิ่ม' not in t and 'ล่วงหน้า' not in t):
        return None
    m = re.search(r'(\d+)\s*วัน', t)
    if m:
        return int(m.group(1)) * 1440
    m = re.search(r'(\d+)\s*ชั่วโมง', t)
    if m:
        return int(m.group(1)) * 60
    m = re.search(r'(\d+)\s*นาที', t)
    if m:
        return int(m.group(1))
    return None

THAI_NUM_WORDS = {
    'สิบเอ็ด': 11, 'สิบสอง': 12, 'สิบ': 10, 'หนึ่ง': 1, 'สอง': 2, 'สาม': 3, 'สี่': 4,
    'ห้า': 5, 'หก': 6, 'เจ็ด': 7, 'แปด': 8, 'เก้า': 9,
}

def _normalize_thai_clock_words(seg):
    # แปลงเลขคำไทยในวลีบอกเวลาให้เป็นตัวเลขก่อน regex จับ (เพิ่ม 31 ต.ค. 2569 รอบ 5)
    # เคสจริงเทสต์สด r4: นายพิมพ์ "เพิ่มแจ้งเตือนวันนี้ตอนสองทุ่มให้ด้วย" — parser เดิมรับเฉพาะตัวเลข
    # จึงอ่าน "สองทุ่ม" ไม่ออก คำสั่งหลุดไปโหมดสร้างนัดจนเกิดนัดปลอมชื่อ "แจ้งเตือน" และทับ LAST_EVENT
    words = sorted(THAI_NUM_WORDS, key=len, reverse=True)
    alt = '|'.join(words)
    seg = re.sub(rf'(ตี\s*)({alt})', lambda m: f"ตี{THAI_NUM_WORDS[m.group(2)]}", seg)
    seg = re.sub(rf'(บ่าย\s*)({alt})', lambda m: f"บ่าย{THAI_NUM_WORDS[m.group(2)]}", seg)
    seg = re.sub(rf'({alt})(\s*ทุ่ม)', lambda m: f"{THAI_NUM_WORDS[m.group(1)]}ทุ่ม", seg)
    seg = re.sub(rf'({alt})(\s*โมง)', lambda m: f"{THAI_NUM_WORDS[m.group(1)]}โมง", seg)
    return seg

def parse_absolute_reminder_clock(text):
    # เวลาเตือนแบบตายตัว เช่น "แจ้งตอน 8 โมงเช้า" → คืน (ชั่วโมง, นาที) หรือ None (เพิ่ม 30 ต.ค. 2569 รอบ 2)
    # เคสจริงจากเทสต์สด 20:47: นายขอ "แจ้งเตือนล่วงหน้าขอให้แจ้งตอน 8 โมงเช้าน่ะ" บอทเงียบ เพราะตัวดักเดิม
    # รู้จักเฉพาะเตือนแบบระยะล่วงหน้า (X วัน/ชั่วโมง/นาที) ไม่รู้จักเวลาตายตัว
    # รั้วกันชน: ต้องมีคำว่า "เตือน" และ "ตอน" นำหน้าเวลา และต้องไม่ใช่ประโยคสร้างนัด (เวลาเริ่มงานของนัดใหม่
    # ก็มักตามหลังคำว่า "ตอน" เหมือนกัน เช่น "บันทึกนัด...ตอน 21:00" — ห้ามแย่ง)
    t = text.strip()
    if ('เตือน' not in t and 'แจ้ง' not in t) or 'ตอน' not in t:
        return None
    if any(v in t for v in QA_CREATE_VERBS):
        return None
    seg = _normalize_thai_clock_words(t.split('ตอน', 1)[1])
    half = 30 if 'ครึ่ง' in seg else 0
    m = re.search(r'(\d{1,2})[:.](\d{2})', seg)
    if m:
        return int(m.group(1)) % 24, int(m.group(2)) % 60
    m = re.search(r'บ่าย\s*(\d{1,2})\s*โมง', seg)
    if m:
        return 12 + (int(m.group(1)) % 12), half
    m = re.search(r'บ่าย\s*(\d{1,2})(?!\d)', seg)
    if m:
        return 12 + (int(m.group(1)) % 12), half
    m = re.search(r'ตี\s*(\d{1,2})', seg)
    if m:
        return int(m.group(1)) % 12, half
    m = re.search(r'(\d{1,2})\s*ทุ่ม', seg)
    if m:
        return (18 + int(m.group(1))) % 24, half
    m = re.search(r'(\d{1,2})\s*โมง', seg)
    if m:
        return int(m.group(1)) % 12, half
    if 'เที่ยงคืน' in seg:
        return 0, 0
    if 'เที่ยง' in seg:
        return 12, 0
    return None

def absolute_reminder_minutes(start_time_str, hh, mm, now):
    # แปลงเวลาเตือนตายตัวเป็น "นาทีก่อนเริ่มงาน": ใช้เวลานั้นในวันงานก่อน ถ้าเลยเวลาเริ่มงานไปแล้ว
    # (เช่น งานเริ่ม 07:00 แต่ขอเตือน 08:00) ให้ถอยไปวันก่อนหน้าแทน คืน None ถ้าเวลาที่ได้ผ่านไปแล้ว
    try:
        tz = pytz.timezone('Asia/Bangkok')
        start_dt = tz.localize(datetime.strptime(start_time_str, '%Y-%m-%dT%H:%M:%S'))
        cand = start_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand >= start_dt:
            cand -= timedelta(days=1)
        if cand <= now:
            return None
        return int((start_dt - cand).total_seconds() // 60)
    except Exception:
        return None

def looks_like_add_reminder(text):
    # ข้อความนี้ "หน้าตาเหมือนคำสั่งเพิ่มเตือน" ไหม — ใช้เป็นรั้วชั้นสุดท้าย (เพิ่ม 31 ต.ค. 2569 รอบ 5)
    # เคสจริง r4: "เพิ่มแจ้งเตือนวันนี้ตอนสองทุ่ม" parser อ่านไม่ออกแล้วคืน None ข้อความจึงหลุดไปโหมดสร้างนัด
    # Gemini เลยสร้างนัดปลอมชื่อ "แจ้งเตือน" เริ่มวันนี้ 20:00 และทับ LAST_EVENT ทำข้อมูลติดต่อตามไปผิดนัด
    # กฎใหม่: เหมือนคำสั่งเพิ่มเตือนแต่อ่านเวลาไม่ออก = ตอบขอให้พิมพ์ใหม่ ห้ามหลุดไปสร้างนัดเด็ดขาด
    t = (text or '').strip()
    if any(v in t for v in QA_CREATE_VERBS):
        return False
    if 'เพิ่ม' in t and ('เตือน' in t or 'แจ้ง' in t):
        return True
    return 'แจ้งล่วงหน้า' in t or 'เตือนล่วงหน้า' in t

def handle_add_reminder_request(event, msg_text):
    # คืนข้อความตอบกลับ หรือ None ถ้าข้อความนี้ไม่ใช่คำสั่งเพิ่มเวลาเตือน
    minutes = parse_add_reminder_minutes(msg_text)
    clock = parse_absolute_reminder_clock(msg_text)
    if minutes is None and clock is None:
        if looks_like_add_reminder(msg_text):
            return ("ขอโทษครับ อ่านเวลาเตือนที่ขอไม่ออกครับ 🙏 ลองพิมพ์แบบนี้ได้เลยครับ "
                    "เช่น \"เพิ่มแจ้งเตือนล่วงหน้า 1 วัน\" หรือ \"เพิ่มแจ้งเตือนตอน 20:00\" "
                    "(บอทไม่สร้างนัดใหม่จากประโยคนี้ให้นะครับ)")
        return None
    scope = qa_scope(event)
    now = datetime.now(pytz.timezone('Asia/Bangkok'))
    if (not LAST_EVENT.get('event_id') or LAST_EVENT.get('scope') != scope
            or not LAST_EVENT.get('expires') or now > LAST_EVENT['expires']):
        return "ขอโทษครับ ไม่แน่ใจว่าหมายถึงนัดไหนครับ รบกวนสร้างนัดใหม่พร้อมบอกเวลาเตือนในประโยคเดียวได้เลยครับ เช่น \"เตือนล่วงหน้า 1 วัน\""
    try:
        abs_note = None
        if clock is not None:
            # ระบุเวลาตายตัวมาชัด ๆ ให้ความสำคัญกว่าแบบระยะล่วงหน้า (เช่น "เตือนล่วงหน้า ขอแจ้งตอน 8 โมงเช้า")
            abs_minutes = absolute_reminder_minutes(LAST_EVENT.get('start_time'), clock[0], clock[1], now)
            if abs_minutes is None:
                return "ขอโทษครับ เวลาเตือนที่ขอคำนวณแล้วผ่านไปแล้วหรือไม่ทันเวลาเริ่มงานครับ ลองบอกเป็นเตือนล่วงหน้าแบบชั่วโมง/วันแทนนะครับ"
            minutes = abs_minutes
            abs_note = f"{clock[0]:02d}:{clock[1]:02d}"
        service = get_calendar_service()
        ev = service.events().get(calendarId=FAMILY_CALENDAR_ID, eventId=LAST_EVENT['event_id']).execute()
        desc = ev.get('description', '') or ''
        old = parse_remind_marker(desc) or LAST_EVENT.get('reminders') or [30]
        new_list = sorted(set(old + [minutes]), reverse=True)[:5]
        marker = remind_marker(new_list)
        if '[remind:' in desc:
            new_desc = re.sub(r'\[remind:[0-9,]+\]', marker, desc)
        else:
            new_desc = (desc + '\n' + marker).strip()
        overrides = [{'method': 'popup', 'minutes': int(m)} for m in new_list]
        # ตามกฎเหล็กของบ้านนี้: ตัวเลขเตือนจริงให้ยึด [remind:...] ใน description เป็นหลัก
        # (overrides ตอน patch ของ service account เคยไม่ติด) จึง patch ทั้งสองอย่างพร้อมกัน
        service.events().patch(
            calendarId=FAMILY_CALENDAR_ID,
            eventId=LAST_EVENT['event_id'],
            body={'description': new_desc, 'reminders': {'useDefault': False, 'overrides': overrides}},
        ).execute()
        LAST_EVENT['reminders'] = new_list

        def fmt_lead(m):
            if m >= 1440 and m % 1440 == 0:
                return f"{m // 1440} วัน"
            if m >= 60 and m % 60 == 0:
                return f"{m // 60} ชั่วโมง"
            return f"{m} นาที"

        lead_text = ", ".join(fmt_lead(m) for m in new_list)
        reply = f"🔔 เพิ่มแจ้งเตือนให้แล้วครับ\n📌 หัวข้อ: {LAST_EVENT.get('summary', 'นัดหมาย')}\n🔔 แจ้งเตือนล่วงหน้า: {lead_text}"
        if abs_note:
            reply += f"\n⏰ เวลาเตือนที่เพิ่ม: {abs_note} น."
        return reply
    except Exception as e:
        print(f"Add reminder error: {e}")
        return "❌ เพิ่มแจ้งเตือนไม่สำเร็จครับ ลองใหม่อีกครั้งนะครับ"

# ===== แนบข้อมูลติดต่อลงนัดล่าสุด (เพิ่ม 31 ต.ค. 2569 ข้อ 2) =====
# เคสจริง 30 ต.ค.: บอทลงนัด "ช่างตัดหญ้า/ตัดต้นไม้ 3 พ.ย. 09:30" จากข้อความของอาหนุ่ย
# แล้วอาหนุ่ยส่งข้อมูลติดต่อช่าง (เบอร์โทร + ลิงก์ Facebook) ตามมาในกลุ่ม แต่บอทไม่ได้เก็บเข้า description
# ของนัดนั้นเลย — คนเปิดปฏิทินทีหลังจะไม่เห็นช่องทางติดต่อช่าง ต้องกลับไปไล่หาในแชตเอง
# วิธีแก้แบบจำกัดขอบเขต (deterministic ล้วน ไม่ใช้ AI): เฉพาะข้อความที่มี "เบอร์โทร หรือ ลิงก์" ชัดเจน
# และมาภายในหน้าต่างเดียวกับคำสั่งต่อเนื่องอื่น ๆ (LAST_EVENT 15 นาที, scope กลุ่ม/ผู้ส่งเดียวกัน)
# ถึงจะผูกเป็นข้อมูลติดต่อของนัดล่าสุด — ข้อความเบอร์/ลิงก์ที่มาในเวลาปกติ (ไม่มีนัดสด) บอทจะไม่แตะ เงียบตามเดิม
PHONE_RE = re.compile(r'0\d{1,2}[-\s]?\d{3,4}[-\s]?\d{4}')
URL_RE = re.compile(r'(https?://\S+|www\.\S+|facebook\.com/\S+|fb\.me/\S+|line\.me/\S+)')

def extract_contact_info(text):
    # คืนข้อความต้นฉบับ (ตัดสั้น 300 ตัวอักษร) ถ้าเป็นข้อมูลติดต่อที่แนบได้ ไม่ใช่คืน None
    t = (text or '').strip()
    if not (PHONE_RE.search(t) or URL_RE.search(t)):
        return None
    # กันชนนัดใหม่: ข้อความที่มี "เวลา + ตัวชี้วัน/คำว่านัดหรือเตือน" คือประกาศนัดของตัวเอง (แบบที่ Gemini จับลงปฏิทิน)
    # ให้โหมดสร้างนัดจัดการ ไม่แย่งมาแนบกับนัดก่อนหน้า — ข้อมูลติดต่อตามนัดปกติจะมีแค่เบอร์/ลิงก์ ไม่มีเวลางานของตัวเอง
    if re.search(r'\d{1,2}[:.]\d{2}|\d{1,2}\s*โมง', t):
        day_or_cmd_cues = ['วันนี้', 'พรุ่งนี้', 'มะรืน', 'วันที่', 'นัด', 'เตือน',
                           'วันจันทร์', 'วันอังคาร', 'วันพุธ', 'วันพฤหัส', 'วันศุกร์', 'วันเสาร์', 'วันอาทิตย์']
        if any(c in t for c in day_or_cmd_cues) or any(v in t for v in QA_CREATE_VERBS):
            return None
    return t[:300]

def insert_before_remind_marker(desc, line):
    # description ของ event ที่บอทสร้างมี [remind:...] ปิดท้าย และตัวเตือนอ่านเลขเตือนจาก marker นี้
    # ข้อมูลใหม่จึงต้องแทรก "ก่อน" marker เสมอ — ต่อท้าย marker จะทำให้ marker ไม่ปิดท้ายตามกฎเหล็กเดิม
    desc = desc or ''
    if '[remind:' in desc:
        return re.sub(r'\[remind:[0-9,]+\]', lambda m: line + '\n' + m.group(0), desc, count=1)
    return (desc + '\n' + line).strip() if desc else line

def handle_contact_attach(event, msg_text):
    # คืนข้อความตอบกลับ หรือ None ถ้าข้อความนี้ไม่ใช่ข้อมูลติดต่อของนัดล่าสุด (บอทเงียบตามเดิม)
    contact = extract_contact_info(msg_text)
    if not contact:
        return None
    scope = qa_scope(event)
    now = datetime.now(pytz.timezone('Asia/Bangkok'))
    if (not LAST_EVENT.get('event_id') or LAST_EVENT.get('scope') != scope
            or not LAST_EVENT.get('expires') or now > LAST_EVENT['expires']):
        return None
    try:
        service = get_calendar_service()
        ev = service.events().get(calendarId=FAMILY_CALENDAR_ID, eventId=LAST_EVENT['event_id']).execute()
        desc = ev.get('description', '') or ''
        if contact in desc:
            return f"📞 ข้อมูลติดต่อนี้บันทึกอยู่ในนัด \"{LAST_EVENT.get('summary', 'นัดหมาย')}\" แล้วครับ"
        line = f"📞 ข้อมูลติดต่อ: {contact}"
        new_desc = insert_before_remind_marker(desc, line)
        service.events().patch(
            calendarId=FAMILY_CALENDAR_ID,
            eventId=LAST_EVENT['event_id'],
            body={'description': new_desc},
        ).execute()
        return (f"📞 บันทึกข้อมูลติดต่อลงนัดให้แล้วครับ\n"
                f"📌 หัวข้อ: {LAST_EVENT.get('summary', 'นัดหมาย')}\n"
                f"📞 {contact}")
    except Exception as e:
        print(f"Contact attach error: {e}")
        return "❌ บันทึกข้อมูลติดต่อไม่สำเร็จครับ ลองส่งใหม่อีกครั้งนะครับ"

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
                    # หน้าต่างยิง: ฐานคือ grace (5 นาทีปกติ, 15 นาทีรอบแรกหลังรีสตาร์ต) แต่เตือนที่ล่วงหน้านาน
                    # ต้องมีหน้าต่างตามเก็บ (catch-up) ยาวกว่านั้น — เคสจริง 2 พ.ย. 2569: เตือนวันพระล่วงหน้า
                    # 1 วัน กำหนดยิง 06:30 ไม่เข้ากลุ่มเลย (ปฏิทินเด้งเตือนนายเองตามปกติ ตัวอีเวนต์ตั้งเตือน
                    # 1440 นาทีถูกต้อง) สาเหตุที่เข้ากันได้กับหลักฐานที่สุดคือ worker/process ไม่ได้ทำงานใน
                    # หน้าต่างแคบ 06:30–06:35 (Render free หลับ/รีสตาร์ต) และโค้ดเดิมไม่มีการตามเก็บ —
                    # เลยเวลาหน้าต่างปุ๊บ เตือนนั้นหายเงียบถาวร ทั้งที่ส่งช้าไปหลายชั่วโมงก็ยังมีประโยชน์
                    # (วันพระ: ยังซื้อดอกไม้ทันภายในวันเดียวกัน) จึงขยาย: เตือน ≥1 วัน → ตามเก็บ 12 ชม.
                    # (ยังเหลือเวลาก่อนอีเวนต์จริงอีก 12 ชม.), เตือน ≥3 ชม. → 60 นาที, อื่น ๆ → อย่างน้อย 15 นาที
                    # ข้อแลกเปลี่ยนที่ยอมรับ: ถ้า process รีสตาร์ตภายในหน้าต่างตามเก็บหลังส่งไปแล้ว อาจส่งซ้ำ
                    # 1 ครั้ง เพราะ notified จำในหน่วยความจำ (แบบเดียวกับทักทายเช้ารอบ 10)
                    window = grace
                    if minutes >= 1440:
                        window = max(window, 12 * 60)
                    elif minutes >= 180:
                        window = max(window, 60)
                    else:
                        window = max(window, 15)
                    if reminder_due(fire_time, now, window):
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
                        # รอบ 17 (ฟีดแบ็กครอบครัว 3 พ.ย. 2569): อานุกบอกว่าบอทเตือนเยอะ นายจึงเคาะให้
                        # "วันพระ" กับ "น้ำดื่มมาส่ง" ไม่ต้อง push รายตัวอีก — สองเรื่องนี้ไม่มีจังหวะที่
                        # พลาดแล้วเสียหาย (ต่างจากเปิดไฟหน้าบ้าน/ล้อหมุนที่ต้องเป๊ะตามเวลา) และทักทายเช้า
                        # (reply ฟรี ไม่กินโควตา) สรุปให้ครบทั้ง "วันนี้ต้องทำ" และ "พรุ่งนี้ต้องเตรียม"
                        # อยู่แล้ว การ push ซ้ำคือตัวซ้ำที่ทำให้คนในบ้านรำคาญ — เคสจริงเช้า 3 พ.ย. 2569:
                        # push วันพระยิง 06:00 มาตามปกติแต่ยังพ่วงข้อความ "(ซื้อดอกไม้เตรียมไว้)" ใน
                        # วันพระจริง ยิ่งตอกย้ำความสับสนว่าวันไหนต้องซื้อ วันไหนคือวันพระ
                        # tradeoff ที่นายรับทราบตอนเคาะแบบ: ถ้าเช้าวันก่อนวันพระไม่มีใครทักในกลุ่มเลย
                        # บรรทัดเตรียมดอกไม้จะไม่โผล่ — บ้านนี้ทักเช้ากันทุกวัน นายเลยเลือกเก็บโควตาไว้
                        if 'วันพระ' in summary or 'น้ำดื่มมาส่ง' in summary:
                            notified.add(key)
                            continue
                        alert_text = (
                            f"⏰ **แจ้งเตือนกำหนดการ!**\n"
                            f"📌 เรื่อง: {summary}\n"
                            f"🕒 เริ่ม: {start_dt.strftime('%d/%m %H:%M น.')} ({lead})"
                        )

                        # ปลายทาง: กลุ่มครอบครัว (หรือแอดมินถ้าไม่ได้ตั้งกลุ่มไว้) — ระบบเหลือ Bot A ช่องเดียว
                        # (ช่องสำรองถูกลบไปแล้ว ดูคอมเมนต์เก็บกวาดรอบ 9 ต้นไฟล์) ถ้าเจอ 429 = โควตา push
                        # ของเดือนนั้นหมด ให้บันทึก log ไว้ชัด ๆ ไม่มีการสลับช่องอีก
                        target = FAMILY_GROUP_ID or ADMIN_USER_ID
                        if target:
                            try:
                                line_bot_api.push_message(target, TextSendMessage(text=alert_text))
                            except LineBotApiError as e:
                                if getattr(e, 'status_code', None) == 429:
                                    print(f"Reminder push blocked by monthly quota (429) for {key}")
                                else:
                                    # push ไม่ผ่านก็ถือว่าพยายามกับ occurrence นี้แล้ว ไม่ retry ซ้ำทุก 60 วิจน log รก
                                    print(f"Reminder push failed for {key}: {e}")
                            except Exception as e:
                                print(f"Reminder push failed for {key}: {e}")

                        notified.add(key)
        except Exception as e:
            print(f"Reminder worker error: {e}")

# เริ่มเธรดเตือนนัดหมายตั้งแต่ตอน import โมดูล — ต้องอยู่นอก if __name__
# ไม่งั้นตอนรันใต้ gunicorn บน Render เธรดนี้จะไม่เริ่มทำงานเลย
reminder_thread = threading.Thread(target=background_calendar_reminder_worker, daemon=True)
reminder_thread.start()

# ===== ข้อ 5 (รอบ 10): ทักทายเช้าคนแรกของวัน + พยากรณ์อากาศ/ฝุ่นสองบ้าน =====
# ที่มา: ทุกเช้าคนในบ้านตั้งทองทัก "สวัสดี" กันในกลุ่มเป็นข้อความแรก ๆ ของวันอยู่แล้ว (เช่น อากงทัก 08:05
# อาหนุ่ยตอบ 08:54 เมื่อ 31 ต.ค. 2569) นายอยากให้บอทใช้จังหวะนั้น — เงื่อนไขของนายคือ "รอข้อความแรกของวัน
# จากคนจริงก่อน" ไม่ยิง push เองตามเวลา เพราะบ้านนี้ไม่มีเวลาเริ่มวันตายตัวและ push กินโควตารายเดือน
# บอทจึงใช้ reply (ฟรี) แนบท้ายข้อความเช้าแรกที่ตัวเองไม่ได้ตอบอะไรอยู่แล้ว วันละครั้งเดียวต่อกลุ่ม
# ข้อมูลอากาศ: Open-Meteo (ฟรี ไม่ต้องใช้ API key) — ⚠️ บทเรียนตอนออกแบบ (เช็กข้อมูลจริง 31 ต.ค.):
# ค่า us_aqi ดิบจาก API ไม่สอดคล้องกับ PM2.5 ของมันเอง (ฝุ่นสูงสุด 28 µg/m³ แต่ AQI โผล่ 175)
# จึงรายงาน PM2.5 เป็น µg/m³ ตรง ๆ และจัดระดับด้วยเกณฑ์ไทย ไม่ใช้ค่าดัชนีสำเร็จรูปที่ไม่น่าเชื่อ
MORNING_GREETED = {'date': None, 'group': None}
# รอบ 18: ล็อกสำหรับจองสิทธิ์ทักทายเช้า — เคสจริง 5 พ.ย. 2569 ข้อความเช้า 2 อันมาติดกัน
# ห่างกัน 2 วินาทีถูกประมวลผลพร้อมกันสองเส้น ทั้งคู่เช็กธงก่อนอีกฝ่ายตั้งธงทัน (ธงเดิมตั้งหลัง
# ดึงอากาศเสร็จ ซึ่งกินเวลาหลายวินาที) เลยทักซ้ำ 2 รอบ ดู maybe_morning_greeting ด้านล่าง
MORNING_GREETING_LOCK = threading.Lock()
WEATHER_CITIES = [
    ('เชียงใหม่', 18.7883, 98.9853),
    ('อยุธยา', 14.3532, 100.5689),
]

def pm25_level_text(value):
    # ระดับฝุ่นตามเกณฑ์ไทย (ค่าเฉลี่ย 24 ชม.) — ใช้กับค่าสูงสุดของวันแบบหยาบ ๆ เพื่อคำแนะนำประจำวัน
    if value <= 25:
        return 'อากาศดี'
    if value <= 37.5:
        return 'ปานกลาง'
    if value <= 75:
        return 'เริ่มมีผลต่อสุขภาพ'
    return 'มีผลต่อสุขภาพ'

def fetch_json_url(url, timeout=8):
    # เคสจริงรอบ 12 (31 ต.ค. 2569): Open-Meteo ตอบ 429 ตั้งแต่คำขอแรกของวันบน Render — ไม่ใช่บอทยิงถี่
    # แต่ IP ขาออกของ Render free เป็น IP ร่วมของ datacenter ที่ผู้เช่ารายอื่นใช้โควตาต่อ IP จนเต็ม (บทเรียน
    # เดียวกับ YouTube จาก IP datacenter ที่โดนบล็อก) มาตรการในชั้นนี้: เจอ 429 ให้รอ 3 วิแล้วลองซ้ำ 1 ครั้ง
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'family-line-bot'})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(3)
                continue
            raise

WEATHER_CACHE = {}

def _weather_from_open_meteo(name, lat, lon):
    # แหล่งหลัก: Open-Meteo — มีครบ อุณหภูมิ/ฝน/PM2.5
    try:
        base = f"latitude={lat}&longitude={lon}&timezone=Asia%2FBangkok&forecast_days=1"
        fc = fetch_json_url(f"https://api.open-meteo.com/v1/forecast?{base}&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max")
        daily = fc.get('daily') or {}
        tmax = (daily.get('temperature_2m_max') or [None])[0]
        tmin = (daily.get('temperature_2m_min') or [None])[0]
        rain = (daily.get('precipitation_probability_max') or [None])[0]
        if tmax is None or tmin is None:
            return None
        line = f"🌡 {name} วันนี้ {tmin:.0f}–{tmax:.0f}°C"
        if rain is not None:
            line += f" · โอกาสฝน {rain:.0f}%"
            if rain >= 60:
                line += " ☔ พกร่มด้วยนะครับ"
        try:
            aq = fetch_json_url(f"https://air-quality-api.open-meteo.com/v1/air-quality?{base}&hourly=pm2_5")
            pm_values = [v for v in ((aq.get('hourly') or {}).get('pm2_5') or []) if v is not None]
            if pm_values:
                pm_max = max(pm_values)
                line += f"\n   ฝุ่น PM2.5 สูงสุด {pm_max:.0f} µg/m³ ({pm25_level_text(pm_max)})"
                if pm_max > 37.5:
                    line += " 😷 ออกนอกบ้านใส่หน้ากากนะครับ"
        except Exception as e:
            print(f"Air quality fetch failed for {name}: {e}")
        return line
    except Exception as e:
        print(f"Weather fetch failed for {name}: {e}")
        return None

def _weather_from_wttr(name, lat, lon):
    # แหล่งสำรอง: wttr.in (คนละโครงสร้างพื้นฐานกับ Open-Meteo โอกาสโดนบล็อกพร้อมกันต่ำ)
    # ข้อจำกัด: แหล่งนี้ไม่มีข้อมูลฝุ่น — ข้อความสำรองจึงมีเฉพาะอุณหภูมิกับฝน ไม่มีบรรทัด PM2.5
    try:
        data = fetch_json_url(f"https://wttr.in/{lat},{lon}?format=j1")
        days = data.get('weather') or []
        if not days:
            return None
        today = days[0]
        tmax = float(today['maxtempC'])
        tmin = float(today['mintempC'])
        line = f"🌡 {name} วันนี้ {tmin:.0f}–{tmax:.0f}°C"
        rains = []
        for h in today.get('hourly') or []:
            try:
                rains.append(int(h.get('chanceofrain') or 0))
            except (TypeError, ValueError):
                pass
        if rains:
            line += f" · โอกาสฝน {max(rains)}%"
            if max(rains) >= 60:
                line += " ☔ พกร่มด้วยนะครับ"
        return line
    except Exception as e:
        print(f"wttr.in fetch failed for {name}: {e}")
        return None

def fetch_city_weather(name, lat, lon):
    # จุดเรียกเดียวของทั้งทักทายเช้าและถามอากาศระหว่างวัน — ชั้นแคช + สลับแหล่ง (รอบ 12 จากเคส 429 บน Render):
    # แคช 60 นาทีทำให้คำถามซ้ำ ๆ ไม่ยิง API เลย และถ้าทุกแหล่งล่มพร้อมกัน ให้ใช้ค่าแคชเก่าของวันนั้นแทนตอบว่าไม่มีข้อมูล
    now = datetime.now(pytz.timezone('Asia/Bangkok'))
    cached = WEATHER_CACHE.get(name)
    if cached and (now - cached['ts']) < timedelta(minutes=60):
        return cached['text']
    text = _weather_from_open_meteo(name, lat, lon) or _weather_from_wttr(name, lat, lon)
    if text:
        WEATHER_CACHE[name] = {'text': text, 'ts': now}
        return text
    if cached:
        print(f"Weather sources down for {name} — serving stale cache from {cached['ts']}")
        return cached['text']
    return None

WEATHER_KEYWORDS = ('อากาศ', 'พยากรณ์', 'ฝุ่น', 'pm2.5', 'PM2.5')
WEATHER_QUESTION_CUES = ('ไหม', 'มั้ย', 'เท่าไร', 'เท่าไหร่', 'เป็นไง', 'ยังไง', 'กี่องศา', 'ฝนตก', '?', 'เช็ก', 'เช็ค', 'ดูให้', 'บอกหน่อย')

def is_weather_question(text):
    # ประตูคำถามอากาศแบบ deterministic (รอบ 11 — นายถามเพิ่มว่า "ถามพยากรณ์ล่าสุดตอนนั้นเลยได้ไหม ไม่ต้องรอรอบเช้า"):
    # ต้องมีคำเรื่องอากาศ/ฝุ่น และมีสัญญาณคำถาม หรือข้อความสั้นมาก (เช่นพิมพ์แค่ "อากาศ" / "ฝุ่นเชียงใหม่")
    # บทเรียนที่ระวัง: ประโยคบอกเล่าในวงคุยครอบครัว เช่น "วันนี้อากาศดีจังเลยเนอะ" ต้องไม่โดนบอทแทรกตอบ
    # ขอบเขตฉบับนี้: ตอบพยากรณ์ "วันนี้" เท่านั้น (พรุ่งนี้ยังไม่อยู่ในประตู จะได้ไม่ตอบผิดวัน)
    t = (text or '').strip()
    if not any(k in t for k in WEATHER_KEYWORDS):
        return False
    return len(t) <= 10 or any(c in t for c in WEATHER_QUESTION_CUES)

def tomorrow_prep_lines(now):
    # หัวข้อ "พรุ่งนี้ต้องเตรียม" ในทักทายเช้า (รอบ 17 — พัฒนาต่อจาก tomorrow_wan_phra_line ของรอบ 14):
    # ที่มาเดิมของวงนี้คือเคส 2 พ.ย. 2569 ที่ push เตือนวันพระล่วงหน้า 1 วันหลุดเงียบ ๆ ทักทายเช้า (reply ฟรี)
    # จึงรับหน้าที่เตือนเตรียมของแทนมาตั้งแต่รอบ 14 — รอบ 17 นายขยายจาก "เฉพาะวันพระ" เป็นรายการเตรียมตัว
    # ของพรุ่งนี้ทุกเรื่อง (ฟีดแบ็กครอบครัว 3 พ.ย. 2569: ให้สรุปเช้าอันเดียวบอกครบ ทั้งอากาศ/ฝุ่น สิ่งที่ต้อง
    # เตรียมวันนี้ และนัดของพรุ่งนี้) และนายสั่งแยกความหมายให้ชัด: บรรทัดในหัวข้อนี้แปลว่า "พรุ่งนี้มีเรื่องนี้
    # วันนี้คือวันเตรียม" เช่น พรุ่งนี้วันพระ = วันนี้ซื้อดอกไม้เตรียมไว้ ส่วนวันพระจริงจะไปโผล่ในหัวข้อ
    # "วันนี้ต้องทำ" ของเช้าวันนั้นแทน ไม่ปนกันอีก (เคสสับสนจริงจาก push เช้า 3 พ.ย. 2569)
    # กติกาเดียวกับสรุปวันนี้: ตัดงานประจำ "เปิดไฟหน้าบ้าน", แสดงสูงสุด 5 รายการ,
    # คืน [] เมื่อไม่มีอะไรต้องเตรียม/อ่านปฏิทินไม่ได้ (ห้ามทำให้ทักทายเช้าพัง)
    try:
        service = get_calendar_service()
        day_start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        res = service.events().list(
            calendarId=FAMILY_CALENDAR_ID,
            timeMin=day_start.isoformat(),
            timeMax=day_end.isoformat(),
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        items = []
        for e in res.get('items', []):
            summary = (e.get('summary') or '').strip()
            start_str = (e.get('start') or {}).get('dateTime')
            if not summary or not start_str:
                continue  # ข้ามอีเวนต์ทั้งวัน (ไม่มีเวลาเริ่ม)
            if 'เปิดไฟ' in summary:
                continue  # งานประจำทุกวัน ไม่ต้องเตรียมอะไร
            start_dt = datetime.fromisoformat(start_str).astimezone(now.tzinfo)
            hhmm = start_dt.strftime('%H:%M')
            if 'วันพระ' in summary:
                items.append("• พรุ่งนี้วันพระ — วันนี้ซื้อดอกไม้/ของไหว้เตรียมไว้ครับ")
            elif 'น้ำดื่มมาส่ง' in summary:
                items.append(f"• พรุ่งนี้น้ำดื่มมาส่ง {hhmm} น. — เตรียมถังเปล่าไว้ครับ")
            else:
                items.append(f"• พรุ่งนี้ {hhmm} น. {summary}")
        if len(items) > 5:
            extra = len(items) - 5
            items = items[:5] + [f"• …และอีก {extra} รายการครับ"]
        return items
    except Exception as e:
        print(f"Tomorrow prep check failed: {e}")
        return []

def today_schedule_lines(now):
    # สรุปกำหนดการ "วันนี้" แนบท้ายทักทายเช้า (รอบ 15 — นายเสนอเอง 2 พ.ย. 2569 หลังเห็น log เช้าวันเดียวกัน:
    # "ถ้ามีใครทักตอนเช้า ให้บอกสรุปหมายกำหนดการวันนี้ออกมาเลย แล้วพอถึงเวลาก็เตือนซ้ำตามปกติ")
    # หลักคิด: สองวงทำคนละหน้าที่ — สรุปเช้าไว้ "วางแผนวัน" (เตรียมของ/เตรียมตัวแต่เช้า) ส่วนเตือนตามเวลาเดิม
    # ไว้ "กันลืมจังหวะจริง" การเห็นซ้ำจึงเป็นเจตนา ไม่ใช่บั๊ก และวงเช้าเดินบน reply ที่พิสูจน์แล้วว่าเข้าชัวร์
    # กติกากรอง (กันสรุปกลายเป็นขยะจนคนเลิกอ่าน):
    # - ตัดงานประจำทุกวัน "เปิดไฟหน้าบ้าน 18:30" ออก — เคสจริงวันนี้ (2 พ.ย.) ปฏิทินมีแค่รายการนี้รายการเดียว
    #   ถ้าไม่ตัด ทักทายเช้าจะแถมสรุปที่ทุกคนรู้อยู่แล้วทุกวัน
    # - รายการที่จบไปแล้วก่อนเวลาทัก ไม่แสดง (เช็คจากเวลาสิ้นสุด) ยกเว้น "วันพระ" ซึ่งสำคัญทั้งวัน
    #   (วันพระเริ่ม 06:30 แต่คนทักเช้าแรกอาจมาหลังนั้น ยังต้องเห็น)
    # - แสดงสูงสุด 5 รายการ เกินนั้นสรุปเป็น "และอีก N รายการ" กันข้อความยาวเกิน
    # คืน [] เมื่อไม่มีอะไรต้องบอก/อ่านปฏิทินไม่ได้ — ผู้เรียกจะไม่เติมหัวข้อเลย (ห้ามทำให้ทักทายเช้าพัง)
    try:
        service = get_calendar_service()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        res = service.events().list(
            calendarId=FAMILY_CALENDAR_ID,
            timeMin=day_start.isoformat(),
            timeMax=day_end.isoformat(),
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        items = []
        for e in res.get('items', []):
            summary = (e.get('summary') or '').strip()
            start_str = (e.get('start') or {}).get('dateTime')
            if not summary or not start_str:
                continue  # ข้ามอีเวนต์ทั้งวัน (ไม่มีเวลาเริ่ม)
            if 'เปิดไฟ' in summary:
                continue  # งานประจำทุกวัน ไม่ต้องสรุป (เหตุผลในคอมเมนต์หัวฟังก์ชัน)
            start_dt = datetime.fromisoformat(start_str).astimezone(now.tzinfo)
            end_str = (e.get('end') or {}).get('dateTime')
            if end_str and 'วันพระ' not in summary:
                if datetime.fromisoformat(end_str).astimezone(now.tzinfo) <= now:
                    continue  # จบไปแล้วก่อนคนทักเช้า ไม่ต้องเล่าซ้ำ
            # รอบ 17: เขียนเป็น "สิ่งที่ต้องทำ" ไม่ใช่แค่ชื่ออีเวนต์ลอย ๆ — วันพระของวันนี้ต้องตัดท้าย
            # "(ซื้อดอกไม้เตรียมไว้)" ออก เพราะการซื้อเป็นงานของ "เมื่อวาน" ที่หัวข้อพรุ่งนี้ต้องเตรียม
            # บอกไปแล้ว (เคสจริงเช้า 3 พ.ย. 2569: push วันพระยังพ่วงประโยคซื้อดอกไม้ในวันพระจริง
            # คนอ่านเลยแยกไม่ออกว่าวันไหนซื้อ วันไหนไหว้) ส่วนน้ำดื่มมาส่งให้บอกการเตรียมถัง/เงินไว้เลย
            # เพราะอาหนุ่ยต้องเตรียมก่อนรถมาถึง (ประวัติส่งจริง: 7 ถัง 210 บาททุก 14 วัน)
            if 'วันพระ' in summary:
                clean = ' '.join(summary.replace('(ซื้อดอกไม้เตรียมไว้)', '').split())
                items.append(f"• {start_dt.strftime('%H:%M')} น. {clean} — วันนี้วันพระครับ")
            elif 'น้ำดื่มมาส่ง' in summary:
                items.append(f"• {start_dt.strftime('%H:%M')} น. {summary} — เตรียมถังเปล่ากับเงินไว้ครับ")
            else:
                items.append(f"• {start_dt.strftime('%H:%M')} น. {summary}")
        if len(items) > 5:
            extra = len(items) - 5
            items = items[:5] + [f"• …และอีก {extra} รายการครับ"]
        return items
    except Exception as e:
        print(f"Today schedule check failed: {e}")
        return []

def maybe_morning_greeting(event):
    # เรียกจากจุดที่แน่ใจว่าบอทยังไม่ได้ใช้ reply token ของข้อความนั้นเท่านั้น (ทุกโฟลว์ที่ตอบแล้วจะ return/ข้ามไปก่อน)
    # สถานะจำในหน่วยความจำ: ถ้าเครื่องรีสตาร์ตกลางเช้า อาจทักซ้ำได้อีกครั้งในเช้าเดียวกัน — ยอมรับในฉบับแรกนี้
    # รอบ 18 — เคสจริง 5 พ.ย. 2569 (บั๊กทักทายซ้ำ 2 รอบ): แม่หน่อยส่งข้อความเช้าติดกัน 2 อัน
    # ห่างกัน 2 วินาที (09:12:54 และ 09:12:56) log ขึ้น "Morning greeting sent" สองครั้งใน
    # วินาทีเดียวกัน (09:13:12) — ต้นเหตุคือโค้ดเดิมตั้งธง MORNING_GREETED "หลัง" ดึงข้อมูล
    # อากาศ+ตารางเสร็จ (งานที่กินเวลาหลายวินาที) ระหว่างนั้นข้อความที่สองซึ่งประมวลผลพร้อมกัน
    # อีกเส้นเข้ามาเช็กธงก็ยังเห็นเป็นค่าว่าง เลยผ่านด่านทั้งคู่ หลักแก้: "จองสิทธิ์ก่อนทำงาน"
    # คือล็อกแล้วตั้งธงทันทีตั้งแต่ต้นฟังก์ชัน ใครมาทีหลังเห็นธงแล้วถอยเลย ส่วนงานดึงอากาศ
    # ทำนอกล็อกตามปกติ (ไม่ต้องให้ใครรอ) — ถ้าสร้างข้อความหรือส่งไม่สำเร็จจริง ๆ ค่อยคืนสิทธิ์
    # ใน except ให้ข้อความถัดไปของเช้านั้นลองทักใหม่ได้ ไม่ใช่เสียสิทธิ์ทักไปทั้งวัน
    claimed = False
    today = None
    group_id = None
    try:
        group_id = getattr(event.source, 'group_id', None)
        if not group_id or (FAMILY_GROUP_ID and group_id != FAMILY_GROUP_ID):
            return
        now = datetime.now(pytz.timezone('Asia/Bangkok'))
        if not (5 <= now.hour < 11):
            return
        today = now.strftime('%Y-%m-%d')
        with MORNING_GREETING_LOCK:
            if MORNING_GREETED['date'] == today and MORNING_GREETED['group'] == group_id:
                return
            MORNING_GREETED['date'] = today
            MORNING_GREETED['group'] = group_id
            claimed = True
        lines = ["☀️ สวัสดีตอนเช้าครับทุกคน"]
        for name, lat, lon in WEATHER_CITIES:
            w = fetch_city_weather(name, lat, lon)
            if w:
                lines.append(w)
        # รอบ 15: แนบสรุปกำหนดการวันนี้ (นายเสนอ 2 พ.ย. — ดู today_schedule_lines ด้านบน)
        # ไม่มีรายการที่ต้องบอก = ไม่เติมหัวข้อเลย ทักทายจะได้สั้นเหมือนเดิม
        sched = today_schedule_lines(now)
        if sched:
            lines.append("📌 วันนี้ต้องทำครับ")
            lines.extend(sched)
        # รอบ 17: แนบหัวข้อ "พรุ่งนี้ต้องเตรียม" (แทนบรรทัดวันพระพรุ่งนี้เส้นเดียวของรอบ 14 —
        # ดู tomorrow_prep_lines ด้านบน) ไม่มีเรื่องต้องเตรียม = ไม่เติมหัวข้อเลย ทักทายจะได้สั้น
        prep = tomorrow_prep_lines(now)
        if prep:
            lines.append("🧺 พรุ่งนี้ต้องเตรียมครับ")
            lines.extend(prep)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="\n".join(lines)))
        print(f"Morning greeting sent for {today}")
    except Exception as e:
        print(f"Morning greeting error: {e}")
        if claimed:
            # คืนสิทธิ์ที่จองไว้ เพราะรอบนี้ส่งไม่สำเร็จ — ข้อความถัดไปในเช้าเดียวกันจะได้ลองใหม่
            with MORNING_GREETING_LOCK:
                if MORNING_GREETED['date'] == today and MORNING_GREETED['group'] == group_id:
                    MORNING_GREETED['date'] = None
                    MORNING_GREETED['group'] = None

# ===== รอบ 13: เมนูฟุตบอลไทยลีก 1 แบบ Flex ให้อากง =====
# ที่มา: อากงชอบดูบอลมาก เชียร์ 2 ทีมคือ อยุธยา ยูไนเต็ด กับ ลำพูน วอริเออร์ — นายออกแบบเอง (31 ต.ค. 2569):
# พิมพ์ "ฟุตบอล" → บอทส่งการ์ด Flex มี 4 ปุ่ม (โปรแกรมแข่ง / ผลการแข่ง / ทีมลำพูน / ทีมอยุธยา) กดปุ่มแล้วบอทตอบเป็นข้อความ
# ปุ่มใช้สัญญาณ postback (ไม่ใช่ข้อความ) เลยต้องมีตัวรับ PostbackEvent แยกต่างหากอยู่ด้านล่างไฟล์
# แหล่งข้อมูล: FotMob (ลีก id 8984) ดึงครั้งเดียวได้ครบทั้งฤดูกาล 240 นัด พร้อมสกอร์จริง — ตรวจแล้วว่า endpoint
# ไม่ต้องใช้ key และตอบไว ส่วน TheSportsDB/SofaScore ใช้ไม่ได้ (403 ทั้งคู่จาก IP ฝั่งเซิร์ฟเวอร์)
# ชั้นความระวังแบบเดียวกับอากาศ: แคช 15 นาที (กันยิงถี่+กันโดนจำกัด) และถ้าดึงไม่ได้ให้ตอบสุภาพ ไม่ทำให้โฟลว์อื่นพัง
# ขอบเขตฉบับแรกตามที่นายสั่ง "เอาแค่นี้ก่อน": ปุ่มลีก = โปรแกรม/ผล "รอบสัปดาห์นี้" (หน้าต่าง 7 วัน หลุดหน้าต่างใช้รอบแข่งแทน)
# ส่วนปุ่มทีม = ผลล่าสุด 3 นัด + โปรแกรมถัดไป 3 นัดของทีมนั้น — รออากงใช้จริงแล้วค่อยปรับตามที่เขาอยากได้
FOOTBALL_LEAGUE_ID = 8984
FOOTBALL_CACHE = {'ts': None, 'matches': [], 'table': []}
THAI_TEAM_NAMES = {
    'Ayutthaya United FC': 'อยุธยา ยูไนเต็ด',
    'Lamphun Warrior': 'ลำพูน วอริเออร์',
    'BG Pathum United': 'บีจี ปทุม ยูไนเต็ด',
    'Bangkok United': 'ทรู แบงค็อก ยูไนเต็ด',
    'Buriram United': 'บุรีรัมย์ ยูไนเต็ด',
    'Chiangrai United': 'เชียงราย ยูไนเต็ด',
    'Chonburi FC': 'ชลบุรี เอฟซี',
    'Pattani FC': 'ปัตตานี เอฟซี',
    'Port FC': 'การท่าเรือ เอฟซี',
    'Thai Port FC': 'การท่าเรือ เอฟซี',
    'Port MTI FC': 'การท่าเรือ เอฟซี',
    'Prachuap FC': 'พีที ประจวบ เอฟซี',
    'PT Prachuap FC': 'พีที ประจวบ เอฟซี',
    'Rasisalai United': 'ราษีไศล ยูไนเต็ด',
    'Ratchaburi FC': 'ราชบุรี เอฟซี',
    'Rayong FC': 'ระยอง เอฟซี',
    'Sisaket United': 'ศรีสะเกษ ยูไนเต็ด',
    'Sukhothai FC': 'สุโขทัย เอฟซี',
    'Uthai Thani FC': 'อุทัยธานี ฟอเรสท์',
    'Uthai Thani Forest': 'อุทัยธานี ฟอเรสท์',
}
FOOTBALL_TEAMS = {
    'lamphun': ('ลำพูน วอริเออร์', 'Lamphun'),
    'ayutthaya': ('อยุธยา ยูไนเต็ด', 'Ayutthaya'),
}
THAI_WEEKDAYS_SHORT = ['จ', 'อ', 'พ', 'พฤ', 'ศ', 'ส', 'อา']
THAI_MONTHS_SHORT = ['ม.ค.', 'ก.พ.', 'มี.ค.', 'เม.ย.', 'พ.ค.', 'มิ.ย.', 'ก.ค.', 'ส.ค.', 'ก.ย.', 'ต.ค.', 'พ.ย.', 'ธ.ค.']

def thai_team_name(en_name):
    return THAI_TEAM_NAMES.get(en_name, en_name)

def fetch_football_matches():
    # คืน list ของนัดทั้งฤดูกาลในรูป dict เรียบง่าย; แคช 15 นาที; ดึงไม่ได้และไม่มีแคชคืน []
    now = datetime.now(pytz.timezone('Asia/Bangkok'))
    if FOOTBALL_CACHE['ts'] and (now - FOOTBALL_CACHE['ts']) < timedelta(minutes=15) and FOOTBALL_CACHE['matches']:
        return FOOTBALL_CACHE['matches']
    try:
        url = f"https://www.fotmob.com/api/data/leagues?id={FOOTBALL_LEAGUE_ID}&season=2026/2027"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        raw = ((data.get('fixtures') or {}).get('allMatches')) or []
        tz = pytz.timezone('Asia/Bangkok')
        matches = []
        for m in raw:
            status = m.get('status') or {}
            utc = status.get('utcTime')
            if not utc:
                continue
            try:
                start = datetime.fromisoformat(utc.replace('Z', '+00:00')).astimezone(tz)
            except ValueError:
                continue
            matches.append({
                'round': int(m.get('round') or 0),
                'home': (m.get('home') or {}).get('name', ''),
                'away': (m.get('away') or {}).get('name', ''),
                'start': start,
                'finished': bool(status.get('finished')),
                'score': status.get('scoreStr') or '',
            })
        matches.sort(key=lambda x: x['start'])
        # รอบ 16: ตารางคะแนนอยู่ใน payload เดียวกันนี้อยู่แล้ว (data['table']) ไม่ต้องยิง API เพิ่ม —
        # แยก try ของตัวเองไว้ ถ้าฟอร์แมตตารางเปลี่ยน แมตช์ยังทำงานต่อได้ตามเดิม
        try:
            groups = data.get('table') or []
            rows = []
            if groups:
                all_rows = (((groups[0].get('data') or {}).get('table') or {}).get('all')) or []
                for r in all_rows:
                    rows.append({
                        'pos': int(r.get('idx') or 0),
                        'name': r.get('name', ''),
                        'played': int(r.get('played') or 0),
                        'wins': int(r.get('wins') or 0),
                        'draws': int(r.get('draws') or 0),
                        'losses': int(r.get('losses') or 0),
                        'scores': r.get('scoresStr') or '',
                        'gd': int(r.get('goalConDiff') or 0),
                        'pts': int(r.get('pts') or 0),
                    })
                rows.sort(key=lambda x: x['pos'])
            if rows:
                FOOTBALL_CACHE['table'] = rows
        except Exception as e:
            print(f"Football table parse failed: {e}")
        if matches:
            FOOTBALL_CACHE['ts'] = now
            FOOTBALL_CACHE['matches'] = matches
        return matches
    except Exception as e:
        print(f"Football fetch failed: {e}")
        return FOOTBALL_CACHE['matches']

def fmt_match_time(dt):
    return f"{THAI_WEEKDAYS_SHORT[dt.weekday()]} {dt.day} {THAI_MONTHS_SHORT[dt.month - 1]} {dt.strftime('%H:%M')}"

def fmt_fixture_line(m):
    return f"• {fmt_match_time(m['start'])} น. {thai_team_name(m['home'])} vs {thai_team_name(m['away'])}"

def fmt_result_line(m):
    score = m['score'].replace('-', '–').replace(' ', '')
    return f"• {thai_team_name(m['home'])} {score} {thai_team_name(m['away'])} ({fmt_match_time(m['start'])})"

def football_fixtures_text():
    # ปุ่ม "โปรแกรมแข่ง": คู่ที่ยังไม่แข่งภายใน 7 วันข้างหน้า (ทุกทีม) — ถ้าหน้าต่างว่าง (ช่วงพักลีก)
    # ให้ใช้ "รอบแข่งถัดไป" ทั้งรอบแทน จะได้ไม่ตอบว่าไม่มีโปรแกรมทั้งที่ยังมีแข่ง
    matches = fetch_football_matches()
    if not matches:
        return "ตอนนี้ดึงข้อมูลฟุตบอลไม่ได้ ลองอีกครั้งนะครับ 🙏"
    now = datetime.now(pytz.timezone('Asia/Bangkok'))
    upcoming = [m for m in matches if not m['finished'] and m['start'] >= now - timedelta(hours=3)]
    week = [m for m in upcoming if m['start'] <= now + timedelta(days=7)]
    if week:
        chosen, header = week, "⚽ โปรแกรมแข่งไทยลีก 1 สัปดาห์นี้"
    elif upcoming:
        next_round = upcoming[0]['round']
        chosen = [m for m in upcoming if m['round'] == next_round]
        header = f"⚽ โปรแกรมแข่งไทยลีก 1 นัดที่ {next_round}"
    else:
        return "ฤดูกาลนี้แข่งครบทุกนัดแล้วครับ 🏆"
    lines = [header] + [fmt_fixture_line(m) for m in chosen]
    return "\n".join(lines)

def football_results_text():
    # ปุ่ม "ผลการแข่ง": ผลที่จบแล้วภายใน 7 วันที่ผ่านมา — ถ้าไม่มี (ช่วงพัก) ใช้รอบล่าสุดที่มีผลแทน
    matches = fetch_football_matches()
    if not matches:
        return "ตอนนี้ดึงข้อมูลฟุตบอลไม่ได้ ลองอีกครั้งนะครับ 🙏"
    now = datetime.now(pytz.timezone('Asia/Bangkok'))
    finished = [m for m in matches if m['finished']]
    if not finished:
        return "ยังไม่มีผลการแข่งขันในฤดูกาลนี้ครับ"
    week = [m for m in finished if m['start'] >= now - timedelta(days=7)]
    if week:
        chosen, header = week, "🏁 ผลการแข่งไทยลีก 1 สัปดาห์นี้"
    else:
        last_round = finished[-1]['round']
        chosen = [m for m in finished if m['round'] == last_round]
        header = f"🏁 ผลการแข่งไทยลีก 1 นัดที่ {last_round}"
    lines = [header] + [fmt_result_line(m) for m in chosen]
    return "\n".join(lines)

def football_team_text(team_key):
    # ปุ่มทีม (ลำพูน/อยุธยา): ผลล่าสุด 3 นัด + โปรแกรมถัดไป 3 นัดของทีมนั้น
    label, needle = FOOTBALL_TEAMS[team_key]
    matches = fetch_football_matches()
    if not matches:
        return "ตอนนี้ดึงข้อมูลฟุตบอลไม่ได้ ลองอีกครั้งนะครับ 🙏"
    mine = [m for m in matches if needle in m['home'] or needle in m['away']]
    past = [m for m in mine if m['finished']][-3:]
    nxt = [m for m in mine if not m['finished']][:3]
    lines = [f"⚽ {label}"]
    if past:
        lines.append("ผลล่าสุด:")
        lines += [fmt_result_line(m) for m in past]
    if nxt:
        lines.append("โปรแกรมถัดไป:")
        lines += [fmt_fixture_line(m) for m in nxt]
    if not past and not nxt:
        lines.append("ยังไม่มีข้อมูลของทีมนี้ในฤดูกาลนี้ครับ")
    return "\n".join(lines)

def fetch_football_table():
    # ตารางคะแนนล่าสุด — ตัวดึงแมตช์ด้านบนพ่วงตารางจาก payload เดียวกันมาใส่แคชไว้แล้ว
    # เรียก fetch_football_matches() ก่อนเสมอเพื่อกระตุ้น/ใช้แคชชุดเดียวกัน (แคช 15 นาที)
    fetch_football_matches()
    return FOOTBALL_CACHE.get('table') or []

def football_table_text():
    # ปุ่ม "อันดับตาราง" (รอบ 16 — นายขอเพิ่ม 2 พ.ย. 2569 เอาใจอากง หลังเมื่อวาน (1 พ.ย.) ทีมที่อากงเชียร์
    # ชนะทั้งคู่: อยุธยาบุกชนะสุโขทัย 0–3, ลำพูนบุกชนะระยอง 0–4 อันดับของสองทีมขยับขึ้นชัดเจน —
    # อยุธยาขึ้นที่ 3 (13 แต้ม), ลำพูนที่ 8 (10 แต้ม) จากข้อมูล FotMob ณ เช้า 2 พ.ย.)
    # รูปแบบบรรทัด: อันดับ + ชื่อทีมไทย + แต้มเป็นตัวหลัก อ่านง่ายบนมือถือ ส่วนแข่ง/ชนะ-เสมอ-แพ้/ประตู
    # ใส่ย่อไว้ท้ายบรรทัดเดียวกัน; สองทีมของอากงติดสัญลักษณ์เดียวกับปุ่มทีมในการ์ด (⚔️/🐂) ให้กวาดตาเจอไว
    rows = fetch_football_table()
    if not rows:
        return "ตอนนี้ดึงอันดับตารางไม่ได้ ลองอีกครั้งนะครับ 🙏"
    lines = ["📊 อันดับตารางไทยลีก 1 (ล่าสุด)"]
    for r in rows:
        mark = ''
        if 'Ayutthaya' in r['name']:
            mark = ' ⚔️'
        elif 'Lamphun' in r['name']:
            mark = ' 🐂'
        lines.append(
            f"{r['pos']}. {thai_team_name(r['name'])}{mark} — {r['pts']} แต้ม "
            f"(แข่ง {r['played']}, ช{r['wins']} ส{r['draws']} พ{r['losses']}, ประตู {r['scores']}, ผลต่าง {r['gd']:+d})"
        )
    return "\n".join(lines)

def build_football_menu_flex():
    # การ์ด Flex ปุ่มตามที่นายออกแบบ (รอบ 13 = 4 ปุ่ม, รอบ 16 เพิ่มปุ่มที่ 5 "อันดับตาราง") —
    # ปุ่มส่ง postback กลับมาที่ตัวรับ PostbackEvent
    # (ข้อความในการ์ดต้องมี alt_text เสมอ ไม่งั้น LINE ปฏิเสธข้อความ Flex ทั้งอัน)
    def menu_button(label, data, color):
        return ButtonComponent(
            action=PostbackAction(label=label, data=data, display_text=label),
            style='primary', color=color, height='sm',
        )
    body = BoxComponent(layout='vertical', spacing='md', contents=[
        TextComponent(text='⚽ ฟุตบอลไทยลีก 1', weight='bold', size='lg'),
        TextComponent(text='เลือกดูข้อมูลได้เลยครับ', size='sm', color='#666666'),
        SeparatorComponent(),
        menu_button('📅 โปรแกรมแข่ง', 'football:fixtures', '#1DB446'),
        menu_button('🏁 ผลการแข่ง', 'football:results', '#1DB446'),
        menu_button('📊 อันดับตาราง', 'football:table', '#1DB446'),
        menu_button('🐂 ทีมลำพูน วอริเออร์', 'football:lamphun', '#0B6E4F'),
        menu_button('⚔️ ทีมอยุธยา ยูไนเต็ด', 'football:ayutthaya', '#0B6E4F'),
    ])
    bubble = BubbleContainer(body=body)
    return FlexSendMessage(alt_text='เมนูฟุตบอลไทยลีก 1', contents=bubble)

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
    # คำสั่งถอดใบนัดจากรูป (ขั้น 0.85) มีโฟลว์ของตัวเอง → ไม่แย่ง (เคสเทสต์สด 31 ต.ค.: "ช่วยทำนัดหมายจากรูปได้ไหม"
    # มีคำว่า นัด+ได้ไหม เลยโดน QA ตอบเป็นรายการนัดทั้งสัปดาห์แทนที่จะไปถอดรูป)
    if any(tr in t for tr in APPT_TRIGGERS):
        return False
    # ข้อความประกาศให้สมาชิกส่งข้อมูล ไม่ใช่คำถามถึงบอท
    if any(c in t for c in QA_ANNOUNCE_CUES):
        return False
    if 'วันพระ' in t and any(c in t for c in QA_QUESTION_CUES + ['ถัดไป', 'ครั้งหน้า', 'รอบหน้า']):
        return True
    if ('นัด' in t or 'ตาราง' in t or 'กำหนดการ' in t or 'แผน' in t) and any(c in t for c in QA_QUESTION_CUES):
        return True
    # คำขอตารางแบบไม่มีคำถาม cue ก็คือคำถามถึงบอท (เคสจริง 23 ต.ค. 2026: อาหนุ่ยพิมพ์
    # "ตารางงานพรุ่งนี้หน่อย" — ไม่มี ไหม/เมื่อไหร่/อะไรบ้าง บอทเลยเงียบ ไม่ตอบอะไรเลย)
    if ('ตาราง' in t or 'กำหนดการ' in t) and any(d in t for d in ['วันนี้', 'พรุ่งนี้', 'มะรืน', 'สัปดาห์', 'อาทิตย์', 'เดือน']):
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
    if any(w in t for w in ['ใบนัด', 'นัดจากรูป', 'นัดหมายจากรูป']):
        return False  # งานถอดรูปใบนัด (ขั้น 0.85) มีโฟลว์ของตัวเองเหมือนเที่ยวบิน
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

# ชื่อเดือนแบบเต็ม + ตัวย่อ — ใช้แปลงคำถามที่ระบุเดือนด้วยชื่อ (เพิ่ม 30 ต.ค. 2569)
# เคสจริง 30 ต.ค.: ถาม "วันพระเดือน พย มีวันไหนบ้าง" แต่โค้ดเดิมรู้จักแค่ เดือนนี้/เดือนหน้า
# เลยตกไปตอบแค่วันพระถัดไปวันเดียว ทั้งที่คำถามขอทั้งเดือน
THAI_MONTH_FULL = {
    'มกราคม': 1, 'กุมภาพันธ์': 2, 'มีนาคม': 3, 'เมษายน': 4, 'พฤษภาคม': 5, 'มิถุนายน': 6,
    'กรกฎาคม': 7, 'สิงหาคม': 8, 'กันยายน': 9, 'ตุลาคม': 10, 'พฤศจิกายน': 11, 'ธันวาคม': 12,
}
THAI_MONTH_DOTTED = {
    'ม.ค.': 1, 'ก.พ.': 2, 'มี.ค.': 3, 'เม.ย.': 4, 'พ.ค.': 5, 'มิ.ย.': 6,
    'ก.ค.': 7, 'ส.ค.': 8, 'ก.ย.': 9, 'ต.ค.': 10, 'พ.ย.': 11, 'ธ.ค.': 12,
}
THAI_MONTH_BARE = {
    'มค': 1, 'กพ': 2, 'มีค': 3, 'เมย': 4, 'พค': 5, 'มิย': 6,
    'กค': 7, 'สค': 8, 'กย': 9, 'ตค': 10, 'พย': 11, 'ธค': 12,
}

def parse_named_month(text, today):
    # หาเดือนที่ผู้ใช้ระบุด้วยชื่อในข้อความ → คืน (ปี ค.ศ., เดือน) หรือ None ถ้าไม่เจอชื่อเดือนชัด ๆ (ห้ามเดา)
    # ปี: ถ้าเดือนที่ระบุผ่านไปแล้วในปีนี้ ให้หมายถึงปีหน้า (เช่น ธ.ค. ถามถึง ม.ค.)
    m = re.search(r'เดือน\s*(\d{1,2})(?!\d)', text)
    if m:
        num = int(m.group(1))
        if 1 <= num <= 12:
            return (today.year + (1 if num < today.month else 0), num)
    for name in sorted(THAI_MONTH_FULL, key=len, reverse=True):
        if name in text:
            num = THAI_MONTH_FULL[name]
            return (today.year + (1 if num < today.month else 0), num)
    for name, num in THAI_MONTH_DOTTED.items():
        if name in text:
            return (today.year + (1 if num < today.month else 0), num)
    # ตัวย่อแบบไม่มีจุด (พย, ตค, ...) เสี่ยงชนคำอื่น เช่น พย ใน "พยายาม" — รับเฉพาะเมื่อตามหลังคำว่า "เดือน" มาติด ๆ
    # และตัวอักษรถัดไปต้องไม่ใช่ตัวอักษรไทย (กันชนคำที่ยาวกว่า)
    for name, num in THAI_MONTH_BARE.items():
        if re.search(r'เดือน\s*' + re.escape(name) + r'(?![\u0E00-\u0E7F])', text):
            return (today.year + (1 if num < today.month else 0), num)
    return None

# ชื่อวันในสัปดาห์ — ใช้แปลงคำถาม/คำถามต่อเนื่องที่ระบุวันด้วยชื่อ (เพิ่ม 30 ต.ค. 2569 รอบ 2)
# เคสจริงจากเทสต์สด 20:48: ถาม "กำหนดการพรุ่งนี้มีอะไรบ้าง" ตอบได้ แต่ถามต่อ "แล้ววันอาทิตย์หล่ะ" บอทเงียบ
# เพราะโค้ดไม่รู้จักชื่อวันเลย กฎ: วันชื่อนั้นคือวันที่จะถึงถัดไปของวันนั้น (รวมวันนี้ถ้าตรงวัน)
THAI_WEEKDAY_TO_NUM = {'วันอาทิตย์': 6, 'วันจันทร์': 0, 'วันอังคาร': 1, 'วันพุธ': 2, 'วันพฤหัสบดี': 3, 'วันศุกร์': 4, 'วันเสาร์': 5}

def parse_weekday_date(text, now):
    # คืน datetime 00:00 ของวันที่ตรงกับชื่อวันในข้อความ หรือ None ถ้าไม่มีชื่อวัน (ห้ามเดา)
    for name in sorted(THAI_WEEKDAY_TO_NUM, key=len, reverse=True):
        if name in text:
            delta = (THAI_WEEKDAY_TO_NUM[name] - now.weekday()) % 7
            return (now + timedelta(days=delta)).replace(hour=0, minute=0, second=0, microsecond=0)
    return None

def answer_wan_phra(text, events):
    tz = pytz.timezone('Asia/Bangkok')
    today = datetime.now(tz).date()
    wp = [e for e in events if 'วันพระ' in e['summary']]
    if not wp:
        return "ยังไม่มีข้อมูลวันพระในปฏิทินครอบครัวครับ"
    named = parse_named_month(text, today)
    if named:
        yy, mm = named
        rest = [e for e in wp if e['start'].year == yy and e['start'].month == mm]
        label = f"เดือน{THAI_MONTHS[mm]} {yy + 543}"
        if not rest:
            return f"{label} ไม่มีวันพระในปฏิทินครอบครัวครับ"
        header = f"🙏 วันพระที่เหลือใน{label}:" if (yy, mm) == (today.year, today.month) else f"🙏 วันพระใน{label}:"
        lines = [header]
        for e in rest:
            lines.append(f"• {fmt_thai_date(e['start'])} — {e['summary']}")
        return "\n".join(lines)
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
    named = parse_named_month(text, now)
    if named:
        yy, mm = named
        mstart = now.replace(year=yy, month=mm, day=1, hour=0, minute=0, second=0, microsecond=0)
        mend = (mstart + timedelta(days=32)).replace(day=1)
        # เดือนปัจจุบันเริ่มจากตอนนี้ ไม่ย้อนอดีต (แบบเดียวกับกิ่งเดือนนี้)
        return (now if (yy, mm) == (now.year, now.month) else mstart), mend
    wd = parse_weekday_date(text, now)
    if wd:
        return wd, wd + timedelta(days=1)
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
{ALIAS_NOTE_FOR_AI} — ถ้าคำถามเอ่ยถึงคนด้วยชื่อใดในกลุ่มเดียวกัน ให้ตอบจากรายการที่ใช้ชื่อใดก็ได้ในกลุ่มนั้น (เช่น ถาม "นัดอากง" ให้รวมรายการที่เขียนว่า เทียนชัย หรือ เชฟเทียน ด้วย)
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

# ===== บริบทถาม-ตอบต่อเนื่อง (เพิ่ม 30 ต.ค. 2569) =====
# เคสจริง 30 ต.ค.: อาหนุ่ยถาม "เดือนนี้มีวันพระอีกวันไหน" บอทตอบได้ แต่ถามต่อสั้น ๆ ว่า "เดือนพย ละ"
# บอทเงียบ เพราะประโยคต่อเนื่องไม่มีคำว่า วันพระ/นัด/ตาราง ให้ผ่านประตู QA และบอทไม่มีบริบทค้างอยู่เลย
# วิธีแก้แบบจำกัดขอบเขต: จำหัวข้อ QA ล่าสุดไว้ 5 นาที แยกตามกลุ่ม/ผู้ใช้ ถ้าข้อความถัดไปเป็นประโยคสั้น ๆ
# ที่พูดถึง "เดือน" (ชื่อเดือน/เดือนหน้า/เดือนนี้) ให้ถือว่าถามต่อเรื่องเดิม — รอบนี้รองรับเฉพาะหัวข้อวันพระ
# ซึ่งเป็นเคสจริงที่เจอ ส่วนหัวข้อตารางทั่วไปยังต้องถามแบบเต็มประโยคเหมือนเดิม (กันบอทเดาบริบทผิดเรื่อง)
LAST_QA = {'topic': None, 'scope': None, 'expires': None}
QA_FOLLOWUP_MINUTES = 5

def qa_scope(event):
    gid = getattr(event.source, 'group_id', None)
    return gid if gid else f"user:{event.source.user_id}"

def remember_qa(topic, scope):
    LAST_QA['topic'] = topic
    LAST_QA['scope'] = scope
    LAST_QA['expires'] = datetime.now(pytz.timezone('Asia/Bangkok')) + timedelta(minutes=QA_FOLLOWUP_MINUTES)

def qa_followup_question(text, scope):
    # ถ้าข้อความเป็นคำถามต่อเนื่องเรื่องวันพระ ให้ประกอบคำถามเต็มคืนมา ไม่ใช่ให้คืน None
    now = datetime.now(pytz.timezone('Asia/Bangkok'))
    if not LAST_QA.get('topic') or LAST_QA.get('scope') != scope:
        return None
    if not LAST_QA.get('expires') or now > LAST_QA['expires']:
        return None
    t = text.strip()
    if len(t) > 40:
        return None
    if any(v in t for v in QA_CREATE_VERBS) or is_image_summary_request(t):
        return None
    if LAST_QA['topic'] == 'wan_phra':
        if 'เดือน' not in t:
            return None
        if not (parse_named_month(t, now) or 'เดือนหน้า' in t or 'เดือนนี้' in t):
            return None
        return 'วันพระ' + t
    if LAST_QA['topic'] == 'schedule':
        # ถามต่อเนื่องเรื่องกำหนดการ (เพิ่มรอบ 2 จากเคส "แล้ววันอาทิตย์หล่ะ"): ประโยคสั้นที่อ้างถึงวัน
        # (ชื่อวัน/วันนี้/พรุ่งนี้/มะรืน) หรือเดือน ให้ผูกเข้ากรอบกำหนดการเดิม — วันให้โค้ดคำนวณเหมือนเดิม
        has_ref = (parse_weekday_date(t, now) is not None
                   or any(w in t for w in ['วันนี้', 'พรุ่งนี้', 'มะรืน'])
                   or (('เดือน' in t) and (parse_named_month(t, now) or 'เดือนหน้า' in t or 'เดือนนี้' in t)))
        if has_ref:
            return 'กำหนดการ' + t
    return None

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
- เรื่องชื่อคน: {ALIAS_RULE_FOR_AI} (ใช้กฎเดียวกันใน title และ participants)

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
        plan = dict(plan)
        plan['title'] = normalize_person_names(plan.get('title', ''))
        if plan.get('participants'):
            plan['participants'] = [normalize_person_names(p) for p in plan['participants']]
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
        # จำนัดล่าสุดไว้เหมือน add_to_calendar (เพิ่ม 31 ต.ค. 2569 ข้อ 2): คำสั่งต่อเนื่องหลังนัดแบบสรุปแผน
        # เช่น "เพิ่มแจ้งเตือน..." และข้อความข้อมูลติดต่อที่ส่งตามมา จะได้ผูกกับนัดนี้ได้ ไม่ใช่เฉพาะนัดที่สร้างจาก parse ตรง ๆ
        LAST_EVENT['event_id'] = result.get('id')
        LAST_EVENT['summary'] = plan['title']
        LAST_EVENT['start_time'] = f"{plan['date']}T{plan['start_time']}:00"
        LAST_EVENT['reminders'] = [60]
        LAST_EVENT['scope'] = None
        LAST_EVENT['expires'] = datetime.now(pytz.timezone('Asia/Bangkok')) + timedelta(minutes=15)
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
                    LAST_EVENT['scope'] = qa_scope(event)
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

# ===== โหมดใบนัดจากรูป (เพิ่ม 31 ต.ค. 2569 ข้อ 3) =====
# เคสจริง 30 ต.ค. 2569: อากงแคปหน้าจอแอป รพ. (นัด 12 พ.ย. 09:30 ห้องตรวจ 110) ลงกลุ่ม แต่วันนั้นบอทอ่านรูปได้
# แค่บรรยาย + ลงชีต นายต้องถอดข้อความพิมพ์ส่งบอทเอง — รอบนี้ให้บอทสกัดใบนัดจากรูปที่ส่งเข้ากลุ่มโดยตรง:
# รูปเข้า → AI ถอดเป็น JSON → โค้ด validation เข้ม (รูปแบบวันที่/เวลา + แปลง พ.ศ. + ช่วงวันที่) → เสนอ "ร่าง" ในกลุ่ม
# → คนพิมพ์ยืนยันก่อนเสมอ บอทไม่มีสิทธิ์ลงปฏิทินเองจากรูป (หลักเดียวกับโหมดสรุปนัด/เที่ยวบิน)
# รูปที่ไม่ผ่าน validation (รูปอาหาร/รูปทั่วไป) จะเงียบสนิท ไหลไป log ตามเดิม ไม่เสนอร่างกวนกลุ่ม
PENDING_APPT = {'data': None, 'expires': None, 'group_id': None}
APPT_TRIGGERS = ['ลงนัดจากรูป', 'สรุปใบนัด', 'อ่านใบนัด', 'ลงใบนัด', 'สรุปนัดจากรูป',
                 'ทำนัดจากรูป', 'ทำนัดหมายจากรูป', 'แกะนัดจากรูป', 'นัดหมายจากรูป', 'นัดจากรูป']
APPT_DEFAULT_TIME = '08:00'  # ใบนัด รพ. บางใบมีแค่วันไม่มีเวลา — บ้านนี้ออกบ้านไป รพ. กัน 7:00 ไม่เกิน 08:00
# จึงตั้ง 08:00 เป็นค่าตั้งต้น (นายอธิบาย 31 ต.ค.: นัด รพ. เน้น "แจ้งเตือนล่วงหน้า 1 วัน" มากกว่าเวลาเป๊ะ ๆ)
# และบอกในร่างชัด ๆ ว่าเป็นค่าตั้งต้น ไม่เงียบใส่เอง

def validate_appointment(data):
    # ตรวจข้อมูลที่ AI ถอดมาก่อนเชื่อ (แบบเดียวกับ validate_itinerary)
    try:
        if not data or not data.get('is_appointment'):
            return None
        tz = pytz.timezone('Asia/Bangkok')
        today = datetime.now(tz).date()
        d = datetime.strptime(str(data.get('date', '')), '%Y-%m-%d').date()
        if d.year > 2400:  # AI ส่งปี พ.ศ. หลุดมา → แปลงเป็น ค.ศ.
            d = d.replace(year=d.year - 543)
        if not (today - timedelta(days=1) <= d <= today + timedelta(days=370)):
            # กู้กรณี AI อ่านปี พ.ศ. แบบ 2 หลักในใบนัด ("12 พ.ย. 69") เป็น ค.ศ. 2069 (เคสเทสต์สด r7 31 ต.ค.:
            # ใบนัดอากงใบจริงโดนเงียบ เพราะวันที่หลุดออกนอกช่วงตรวจ) — เลข 2 หลัก "69" คือ พ.ศ. 2569 = ค.ศ. 2026
            # สูตรกู้: ปี ค.ศ. = (เลข 2 หลักท้าย) + 1957 จากนั้นเช็กช่วงอีกครั้ง ไม่เข้าช่วงก็ปฏิเสธตามเดิม
            d2 = None
            if 2000 <= d.year <= 2099:
                try:
                    d2 = d.replace(year=(d.year % 100) + 1957)
                except ValueError:
                    d2 = None
            if d2 and (today - timedelta(days=1) <= d2 <= today + timedelta(days=370)):
                d = d2
            else:
                return None
        raw_time = data.get('time')
        time_is_default = False
        if raw_time:
            datetime.strptime(str(raw_time), '%H:%M')
            t = str(raw_time)
        else:
            t = APPT_DEFAULT_TIME
            time_is_default = True
        patient = (str(data.get('patient_name') or '')).strip() or None
        dept = (str(data.get('department') or '')).strip() or None
        hospital = (str(data.get('hospital') or '')).strip() or None
        if not (patient or dept or hospital):
            return None
        title = (str(data.get('title') or '')).strip() or None
        if not title:
            title = dept or 'พบแพทย์ตามนัด'
        if patient and patient not in title and not title.startswith('พา'):
            # หัวข้อต้องบอกว่าเป็นนัดของใคร ไม่งั้นปฏิทินจะมีแต่นัดลอย ๆ ไม่รู้ของใคร
            # (ชื่อจะถูก normalize เป็นชื่อ canonical เช่น อากง ตอนแสดงร่าง/ลงปฏิทิน)
            title = f"พา{patient}ไป{title}"
        return {
            'is_appointment': True,
            'patient_name': patient,
            'title': title,
            'department': dept,
            'hospital': hospital,
            'date': d.strftime('%Y-%m-%d'),
            'time': t,
            'time_is_default': time_is_default,
            'note': (str(data.get('note') or '')).strip() or None,
        }
    except Exception as e:
        print(f"Appointment validate error: {e}")
        return None

def extract_appointment_from_image(image_bytes):
    if not gemini_client or not image_bytes:
        return None
    tz = pytz.timezone('Asia/Bangkok')
    today_str = datetime.now(tz).strftime('%Y-%m-%d')
    prompt = f"""รูปนี้อาจเป็น "ใบนัดหมายทางการแพทย์" เช่น ใบนัดโรงพยาบาล ภาพแคปหน้าจอแอปโรงพยาบาลที่มีนัดหมาย ใบนัดคลินิก/ทันตกรรม หรือบัตรนัด

งานของคุณ: ถอดข้อมูลนัดหมายจากรูปเฉพาะที่เห็นในรูปจริง ๆ ห้ามเดาหรือแต่งเพิ่ม

กฎสำคัญ:
- ถ้ารูปไม่ใช่ใบนัดหมายทางการแพทย์เลย ให้ตอบ {{"is_appointment": false}} เท่านั้น
- วันที่: ตอบเป็นปีคริสต์ศักราชรูปแบบ YYYY-MM-DD เสมอ — ถ้าในรูปเป็นปีพุทธศักราช (เช่น 2569) ให้ลบ 543 ก่อน (2569 → 2026) ถ้าระบุเป็นชื่อเดือนภาษาไทยหรือวันในสัปดาห์ ให้แปลงเป็นวันที่จริงโดยเทียบกับวันนี้ ({today_str})
- ปีในเอกสารไทยอาจเป็นเลข 2 หลัก เช่น "12 พ.ย. 69" หมายถึง พ.ศ. 2569 ให้ตอบ 2026-11-12 (ห้ามตอบปี 2069)
- รูปอาจเป็นภาพถ่ายจอมือถือหรือแคปหน้าจอที่มีแถบเมนู/ปุ่มของแอปปนอยู่ ให้สนใจเฉพาะเนื้อหาใบนัดในการ์ด/เอกสารในรูป
- เวลา: รูปแบบ HH:MM (24 ชั่วโมง) ถ้าในรูปไม่มีเวลา ให้ใส่ null ห้ามเดา
- ชื่อผู้ป่วย: คัดลอกตามที่พิมพ์ในเอกสาร
- ห้องตรวจ/เลขคิว/การเตรียมตัว/หมายเหตุอื่น: ใส่รวมใน note
- title: หัวข้อนัดสั้น ๆ ภาษาไทย เช่น "พบหมอเบาหวาน" หรือ "ตรวจ MRI"

ตอบเป็น JSON อย่างเดียวเท่านั้น รูปแบบ:
{{"is_appointment": true, "patient_name": "ชื่อผู้ป่วย หรือ null", "title": "หัวข้อสั้น ๆ หรือ null", "department": "แผนก/คลินิก หรือ null", "hospital": "ชื่อโรงพยาบาล/สถานพยาบาล หรือ null", "date": "YYYY-MM-DD หรือ null", "time": "HH:MM หรือ null", "note": "รายละเอียดอื่น หรือ null"}}"""
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
            if not data.get('is_appointment'):
                return {'is_appointment': False}
            valid = validate_appointment(data)
            if valid:
                return valid
            print(f"Appointment attempt {attempt+1}: ถอดได้แต่ข้อมูลไม่ผ่านการตรวจ ลองใหม่")
            time.sleep(1)
        except Exception as e:
            print(f"Appointment extract attempt {attempt+1} error: {e}")
            time.sleep(2)
    return None

def format_appt_draft(appt):
    tz = pytz.timezone('Asia/Bangkok')
    d = tz.localize(datetime.strptime(appt['date'], '%Y-%m-%d'))
    lines = ["📋 **บอทเห็นใบนัดในรูปนี้ครับ** เสนอร่างนัดให้ตรวจก่อนลงปฏิทิน:"]
    lines.append(f"📌 หัวข้อ: {normalize_person_names(appt['title'])}")
    if appt.get('patient_name'):
        lines.append(f"👤 ผู้ป่วย: {appt['patient_name']}")
    if appt.get('department'):
        lines.append(f"🏥 แผนก: {appt['department']}")
    if appt.get('hospital'):
        lines.append(f"📍 สถานที่: {appt['hospital']}")
    lines.append(f"🗓 {fmt_thai_date(d)} เวลา {appt['time']} น.")
    if appt.get('time_is_default'):
        lines.append("⏰ ในรูปไม่ระบุเวลา บอทใส่ 08:00 น. ให้ก่อนนะครับ (เวลาเตรียมออกบ้านไป รพ. ปกติของบ้านเรา — สั่งแก้เวลาได้หลังลงปฏิทิน)")
    if appt.get('note'):
        lines.append(f"📝 {appt['note']}")
    lines.append("พิมพ์ **'ตกลง'** เพื่อลงปฏิทินครอบครัว หรือ **'ไม่เอา'** เพื่อยกเลิก (ร่างรอ 10 นาทีครับ)")
    return "\n".join(lines)

def add_appt_to_calendar(appt):
    title = normalize_person_names(appt['title'])
    start_str = f"{appt['date']}T{appt['time']}:00"
    # ใบนัด = นัด รพ. เสมอ → เตือนล่วงหน้า 1 วัน (หัวใจของนัดแบบนี้) + 30 นาทีก่อนเวลาเจอหมอ
    link = add_to_calendar(title, start_str, [1440, 30])
    if not link:
        return None
    # เติมรายละเอียดจากใบนัดลง description ของอีเวนต์ (แทรกก่อน [remind:] ตามกฎเหล็กเรื่อง marker)
    details = []
    if appt.get('patient_name'):
        details.append(f"ผู้ป่วย: {appt['patient_name']}")
    if appt.get('department'):
        details.append(f"แผนก: {appt['department']}")
    if appt.get('hospital'):
        details.append(f"สถานที่: {appt['hospital']}")
    if appt.get('note'):
        details.append(f"หมายเหตุ: {appt['note']}")
    if appt.get('time_is_default'):
        details.append("เวลาในใบนัดไม่ระบุ — บอทตั้ง 08:00 น. ให้ก่อน")
    details.append("(นัดนี้บอทถอดจากรูปใบนัด และมีสมาชิกยืนยันก่อนลงปฏิทิน)")
    if LAST_EVENT.get('event_id'):
        try:
            service = get_calendar_service()
            ev = service.events().get(calendarId=FAMILY_CALENDAR_ID, eventId=LAST_EVENT['event_id']).execute()
            desc = ev.get('description', '') or ''
            new_desc = insert_before_remind_marker(desc, "\n".join(details))
            service.events().patch(
                calendarId=FAMILY_CALENDAR_ID,
                eventId=LAST_EVENT['event_id'],
                body={'description': new_desc},
            ).execute()
        except Exception as e:
            print(f"Appointment details patch error: {e}")
    return link

def handle_appt_flow(event, msg_text):
    # คืนค่า True = จัดการข้อความนี้แล้ว (โฟลว์เดียวกับ handle_travel_flow)
    source = event.source
    group_id = getattr(source, 'group_id', None) or getattr(source, 'room_id', None) or getattr(source, 'user_id', None)
    tz = pytz.timezone('Asia/Bangkok')
    now = datetime.now(tz)
    pending = PENDING_APPT.get('data')
    if pending and PENDING_APPT.get('expires'):
        if now > PENDING_APPT['expires']:
            PENDING_APPT['data'] = None
            pending = None
        elif PENDING_APPT.get('group_id') == group_id:
            if is_reject_word(msg_text):
                PENDING_APPT['data'] = None
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="รับทราบครับ ไม่ลงใบนัดนี้ลงปฏิทิน"))
                return True
            if is_confirm_word(msg_text):
                link = add_appt_to_calendar(pending)
                PENDING_APPT['data'] = None
                if link:
                    LAST_EVENT['scope'] = qa_scope(event)
                    remember_departure_question(normalize_person_names(pending['title']), qa_scope(event))
                    d = tz.localize(datetime.strptime(pending['date'], '%Y-%m-%d'))
                    text = (f"📅 **ลงปฏิทินครอบครัวแล้วครับ!**\n"
                            f"📌 {normalize_person_names(pending['title'])}\n"
                            f"🗓 {fmt_thai_date(d)} เวลา {pending['time']} น.\n"
                            f"🔔 แจ้งเตือนล่วงหน้า 1 วัน และ 30 นาทีครับ\n"
                            f"{DEPARTURE_QUESTION}")
                else:
                    text = "❌ ลงปฏิทินไม่สำเร็จ ลองพิมพ์ 'ลงนัดจากรูป' ให้บอททำร่างใหม่นะครับ"
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text))
                return True
            return False  # ข้อความอื่นระหว่างรอร่าง — ปล่อย flow ปกติ ร่างยังรอต่อ
    if any(t in msg_text for t in APPT_TRIGGERS):
        found = None
        for url in find_latest_image_urls(3):
            image_bytes = download_image_bytes(url)
            if not image_bytes:
                continue
            result = extract_appointment_from_image(image_bytes)
            if result and result.get('is_appointment'):
                found = result
                break
        if not found:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text="🔍 เราดูรูปล่าสุดในบันทึกแล้ว ยังไม่เจอใบนัดที่ถอดได้ครับ — ส่งรูปใบนัดเข้ากลุ่มก่อน แล้วพิมพ์ 'ลงนัดจากรูป' อีกครั้งนะครับ"))
            return True
        PENDING_APPT['data'] = found
        PENDING_APPT['expires'] = now + timedelta(minutes=10)
        PENDING_APPT['group_id'] = group_id
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=format_appt_draft(found)))
        return True
    return False

# ===== รอบ 17: @tag บอท = คุยกับบอทตรง ๆ =====
# เคสจริง 2 พ.ย. 2569: อาหนุ่ยพิมพ์ "@Bot A บ้านตั้งทอง วันพระเดือนนี้" ในกลุ่ม บอทเงียบสนิท เพราะ
# (1) โค้ดเดิมไม่เคยอ่านข้อมูล mention จาก LINE เลย และ (2) ประตู QA บังคับว่าต้องมีคำใบ้คำถาม
# (ไหม/วันไหน/...) ซึ่งประโยคของอาหนุ่ยไม่มี — นายต้องพิมพ์ซ้ำเป็น "วันพระเดือนนี้มีวันไหนบ้าง" บอทถึงตอบ
# หลักของรอบนี้ (นายเคาะ 3 พ.ย. 2569): คนที่ tag คือตั้งใจคุยกับบอทอยู่แล้ว สัญญาณ tag ใช้แทนรูปคำถามได้
# ส่วนข้อความที่ไม่ได้ tag ยังใช้ประตูเข้มแบบเดิมทุกอย่าง กันบอทแทรกกลางวงคุยของครอบครัว
# การตัดชื่อบอทออกจากข้อความตั้งแต่ต้นทางยังช่วยโฟลว์อื่นด้วย เช่น "@Bot A บอล" จะเหลือ "บอล" เข้าเมนูฟุตบอลพอดี
BOT_MENTION_MARKERS = ('@Bot A บ้านตั้งทอง', '@Bot A')

def split_bot_mention(event, text):
    # คืน (ถูกจ่าหน้าถึงบอทไหม, ข้อความที่ตัดชื่อบอทออกแล้ว) — อ่าน mention metadata แบบ defensive
    # (แต่ละเวอร์ชันของ SDK ตั้งชื่อฟิลด์ไม่เหมือนกัน) และสำรองด้วยตัวข้อความ "@Bot A ..." ที่ LINE แสดงจริง
    t = text or ''
    addressed = False
    try:
        mention = getattr(event.message, 'mention', None)
        for m in (getattr(mention, 'mentionees', None) or []):
            if getattr(m, 'is_self', False) or getattr(m, 'isSelf', False):
                addressed = True
    except Exception:
        pass
    for marker in BOT_MENTION_MARKERS:
        if marker in t:
            addressed = True
            t = t.replace(marker, ' ')
    return addressed, ' '.join(t.split())

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    try:
        user_id = event.source.user_id
        # รอบ 17: ตัดชื่อบอทออกจากข้อความที่ถูก tag มาตั้งแต่ต้นทาง และจำไว้ว่าข้อความนี้จ่าหน้าถึงบอท
        addressed_to_bot, msg_text = split_bot_mention(event, event.message.text)

        # 0. คำสั่งเปิดแอปครัว: พิมพ์คีย์เวิร์ดสั้น ๆ บอทตอบลิงก์เว็บแอปทันที (ไม่ผ่าน AI ไม่ลง log ไม่ต้องดึงโปรไฟล์)
        if msg_text.strip().lower() in KITCHEN_KEYWORDS:
            if KITCHEN_APP_URL:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"🍳💚 **ครัวและสุขภาพ บ้านตั้งทอง**\nเปิดดูเมนู สูตรอาหาร สต็อกวัตถุดิบ และอัปเดตสุขภาพครอบครัวได้ที่นี่:\n{KITCHEN_APP_URL}")
                )
            return

        sender_name = get_user_display_name(user_id, getattr(event.source, 'group_id', None))
        # พิมพ์ source id ลง log — ใช้หา FAMILY_GROUP_ID จาก Render Logs (ส่งข้อความในกลุ่มแล้วดูค่า group:)
        print(f"Source -> user:{user_id} group:{getattr(event.source, 'group_id', None)} room:{getattr(event.source, 'room_id', None)}")
        timestamp = event.timestamp
        thai_time = get_thai_time(timestamp)

        # 0.45 เมนูฟุตบอลไทยลีก (รอบ 13): พิมพ์ "ฟุตบอล" หรือ "บอล" สั้น ๆ -> ส่งการ์ด Flex 4 ปุ่มให้กดเลือก
        # วางไว้ก่อน QA เพราะนี่คือการ์ดเมนู ไม่ใช่คำถามตาราง — เงื่อนไขเป็นคำสั้น ๆ ตรงตัวเท่านั้น
        # กันบอทแทรกกลางประโยคคุยเรื่องบอลทั่วไปของครอบครัว (เช่น "วันนี้บอลแข่งกี่โมง" จะไม่โดนอันนี้)
        if msg_text.strip() in ('ฟุตบอล', 'บอล', 'เมนูฟุตบอล', 'ดูบอล'):
            line_bot_api.reply_message(event.reply_token, build_football_menu_flex())
            return

        # รอบ 16: พิมพ์ "อันดับตาราง" ตรง ๆ = ตอบตารางคะแนนเลย ไม่ต้องผ่านการ์ด
        # ต้องวางไว้ตรงนี้ (ก่อนขั้น 0.5 QA) เพราะคำว่า "ตาราง" เป็นคีย์เวิร์ดของโหมดถาม-ตอบตารางนัด —
        # ถ้าหลุดไปถึง QA บอทจะตอบเรื่องนัดหมายแทนอันดับฟุตบอล (เงื่อนไขตรงตัวสั้น ๆ แบบเดียวกับเมนูบอล
        # ประโยคคุยทั่วไปที่มีคำว่าอันดับตารางปนอยู่จะไม่โดนอันนี้)
        if msg_text.strip() in ('อันดับตาราง', 'ตารางคะแนน', 'อันดับบอล', 'ตารางบอล'):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=football_table_text()))
            return

        # 0.5 ถาม-ตอบตารางนัด/วันพระ: ถ้าเป็นคำถามเรื่องตาราง ให้ตอบจากปฏิทินจริงก่อน
        # (ตอบแล้วข้ามโหมดสร้างนัด แต่ยังไหลไปบันทึก log + แจ้งแอดมินตามปกติ)
        qa_answer = None
        follow_q = None
        if addressed_to_bot and not msg_text:
            # tag บอทมาเปล่า ๆ ไม่มีข้อความตามหลัง — ตอบเมนูสั้น ๆ ว่าถามอะไรได้บ้าง แทนที่จะเงียบใส่
            qa_answer = "ครับ เรียกบอทได้เลยครับ 🙏 ถามสั้น ๆ ได้เลย เช่น 'วันพระเดือนนี้' 'ตารางวันนี้' 'อากาศวันนี้' หรือพิมพ์ 'ฟุตบอล' เพื่อดูโปรแกรมบอลครับ"
        elif addressed_to_bot and 'รูป' not in msg_text and not any(v in msg_text for v in QA_CREATE_VERBS + ['ฝากนัด', 'ลงนัด', 'สร้างนัด', 'ทำนัด']) and ('วันพระ' in msg_text or any(k in msg_text for k in ('ตาราง', 'กำหนดการ', 'แผน', 'นัด'))):
            # รอบ 17: ถูก tag = คุยกับบอทตรง ๆ ไม่ต้องรอคำใบ้คำถาม (เคสอาหนุ่ย 2 พ.ย. 2569 ข้างบน)
            # กันไว้ 2 ชั้น: ข้อความขอสรุป "รูป" มีโฟลว์เฉพาะของมัน (บทเรียน 23 ต.ค.) และคำสั่งสร้างนัด
            # ชัด ๆ ต้องไหลไปโฟลว์สร้างนัดตามเดิม ไม่ใช่โดนแย่งมาตอบเป็นรายการนัด
            try:
                qa_answer = answer_schedule_question(msg_text)
            except Exception as e:
                print(f"Schedule Q&A (mention) error: {e}")
        elif is_image_summary_request(msg_text):
            # ขอสรุปจากรูป: ตอบตามจริงว่ายังทำไม่ได้ แทนที่จะหลุดไปตอบจากปฏิทิน (เคส 23 ต.ค. 2026)
            qa_answer = "ขออภัยครับ ตอนนี้บอทยังสรุปเนื้อหาจากรูปในแชตไม่ได้ครับ 📷 รูปถูกบันทึกเข้าชีตให้เรียบร้อยแล้ว ถ้าต้องการให้สรุปเป็นนัดหรือลงปฏิทิน พิมพ์รายละเอียดเป็นข้อความมาได้เลยครับ"
        elif is_schedule_question(msg_text):
            try:
                qa_answer = answer_schedule_question(msg_text)
            except Exception as e:
                print(f"Schedule Q&A error: {e}")
        if not qa_answer:
            # คำถามต่อเนื่องสั้น ๆ (เช่น "เดือนพย ละ" หลังเพิ่งถามเรื่องวันพระ) — ผูกกับหัวข้อ QA ล่าสุด
            follow_q = qa_followup_question(msg_text, qa_scope(event))
            if follow_q:
                try:
                    qa_answer = answer_schedule_question(follow_q)
                except Exception as e:
                    print(f"Schedule Q&A follow-up error: {e}")
        if qa_answer:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=qa_answer))
            remember_qa('wan_phra' if 'วันพระ' in (follow_q or msg_text) else 'schedule', qa_scope(event))

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

        # 0.85 โหมดใบนัดจากรูป: คำยืนยัน/ปฏิเสธร่างใบนัดที่เสนอไว้ หรือทริกเกอร์ให้ถอดรูปล่าสุดเป็นนัด (เพิ่ม 31 ต.ค. 2569 ข้อ 3)
        appt_handled = False
        if not qa_answer and not plan_handled and not travel_handled:
            try:
                appt_handled = handle_appt_flow(event, msg_text)
            except Exception as e:
                print(f"Appointment flow error: {e}")

        # 0.87 ถามพยากรณ์อากาศ/ฝุ่น "ล่าสุดตอนนั้น" ได้ตลอดวัน (รอบ 11) — ใช้ตัวดึงอากาศชุดเดียวกับทักทายเช้า
        # แต่กำกับเวลาที่ถามไว้ในหัวข้อความ กันสับสนว่าเป็นค่าของรอบเช้า; ระบุชื่อเมืองเดียวก็ตอบเมืองนั้นเมืองเดียว
        # รอบ 17: ถ้าถูก tag มาด้วย แค่มีคำเรื่องอากาศ/ฝุ่นก็ตอบเลย ไม่ต้องมีคำใบ้คำถาม (หลัก tag เดียวกับ QA)
        if not qa_answer and not plan_handled and not travel_handled and not appt_handled and (is_weather_question(msg_text) or (addressed_to_bot and any(k in msg_text for k in WEATHER_KEYWORDS))):
            now_str = datetime.now(pytz.timezone('Asia/Bangkok')).strftime('%H:%M')
            asked_cities = [c for c in WEATHER_CITIES if c[0] in msg_text] or WEATHER_CITIES
            wlines = [f"🌤 พยากรณ์ล่าสุด (เวลา {now_str} น.)"]
            got_weather = False
            for cname, clat, clon in asked_cities:
                w = fetch_city_weather(cname, clat, clon)
                if w:
                    wlines.append(w)
                    got_weather = True
            if not got_weather:
                wlines = ["ตอนนี้ดึงข้อมูลอากาศไม่ได้ ลองถามอีกครั้งนะครับ 🙏"]
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="\n".join(wlines)))
            return

        # 0.9 คำสั่งต่อเนื่อง "เพิ่มแจ้งเตือนล่วงหน้า..." สำหรับนัดล่าสุดที่เพิ่งสร้าง (เพิ่ม 30 ต.ค. 2569)
        if not qa_answer and not plan_handled and not travel_handled and not appt_handled:
            try:
                reminder_reply = handle_add_reminder_request(event, msg_text)
            except Exception as e:
                print(f"Add reminder flow error: {e}")
                reminder_reply = None
            if reminder_reply:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reminder_reply))
                return

        # 0.95 แนบข้อมูลติดต่อ (เบอร์/ลิงก์) ลงนัดล่าสุดที่เพิ่งสร้าง (เพิ่ม 31 ต.ค. 2569 ข้อ 2)
        if not qa_answer and not plan_handled and not travel_handled and not appt_handled:
            try:
                contact_reply = handle_contact_attach(event, msg_text)
            except Exception as e:
                print(f"Contact attach flow error: {e}")
                contact_reply = None
            if contact_reply:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=contact_reply))
                return

        # 0.97 คำตอบ "ออกบ้านกี่โมง" สำหรับนัดที่บอทเพิ่งถามไว้ตอนสร้างนัด (เพิ่ม 31 ต.ค. 2569 รอบ 7)
        if not qa_answer and not plan_handled and not travel_handled and not appt_handled:
            try:
                departure_reply = handle_departure_answer(event, msg_text)
            except Exception as e:
                print(f"Departure answer flow error: {e}")
                departure_reply = None
            if departure_reply:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=departure_reply))
                return

        # 1. ตรวจสอบนัดหมายปฏิทิน (ข้ามถ้าตอบคำถามไปแล้วในข้อ 0.5 หรือจัดการในข้อ 0.7/0.8)
        cal_intent = None if (qa_answer or plan_handled or travel_handled or appt_handled) else parse_calendar_intent(msg_text)
        if cal_intent and cal_intent.get("is_calendar"):
            summary = normalize_person_names(cal_intent.get("summary", "นัดหมายครอบครัว"))
            start_time = cal_intent.get("start_time")
            reminder_list = cal_intent.get("reminder_minutes", [30])
            if isinstance(reminder_list, (int, float)):
                reminder_list = [int(reminder_list)]
            # นัด รพ. ที่พิมพ์สั่งตรง ๆ ก็ใช้กฎเดียวกัน: เติมเตือนล่วงหน้า 1 วันให้เสมอ (รอบ 7)
            reminder_list = ensure_hospital_reminders(reminder_list, msg_text, summary)
            recurrence = cal_intent.get("recurrence")
            recurrence_days = cal_intent.get("recurrence_days") or []

            if start_time:
                res = add_to_calendar(summary, start_time, reminder_list, recurrence, recurrence_days)
                if res:
                    LAST_EVENT['scope'] = qa_scope(event)
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
                    if should_ask_departure(summary, msg_text, recurrence):
                        remember_departure_question(summary, qa_scope(event))
                        reply_text += f"\n{DEPARTURE_QUESTION}"
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

        # ข้อ 5: มาถึงจุดนี้แปลว่าข้อความนี้บอทไม่ได้ตอบอะไรเลย (ทุกโฟลว์ที่ตอบจะ return ออกไปก่อน)
        # ถ้าเป็นข้อความแรกของเช้านี้ในกลุ่มครอบครัว ใช้ reply token ที่ยังว่างอยู่ทักทาย + บอกอากาศ
        maybe_morning_greeting(event)

    except Exception as e:
        print(f"Error handling message: {e}")

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    try:
        user_id = event.source.user_id
        sender_name = get_user_display_name(user_id, getattr(event.source, 'group_id', None))
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

        # 3.5) โหมดใบนัด: รูปที่ส่งเข้ามา ลองสกัดใบนัด — ผ่าน validation เข้มเท่านั้นถึงเสนอร่าง
        # (เดิมล็อกเฉพาะกลุ่มที่ group_id ตรง FAMILY_GROUP_ID เป๊ะ ๆ — เทสต์สด 31 ต.ค. นายเทสต์ในแชตที่ group_id
        # ไม่ตรงเงื่อนไขนั้น บอทเลยเงียบสนิททั้งที่ฟังก์ชันสกัดพร้อมทำงาน รอบนี้เปิดทุกแชต (กลุ่ม/เดี่ยว) เพราะ
        # โฟลว์นี้ปลอดภัยในตัวเอง: ต้องผ่าน validation + มีคนพิมพ์ยืนยันก่อนลงปฏิทินเสมอ ร่างผูกกับ scope ของแชตนั้น)
        slip_replied = False
        try:
            appt_scope = (getattr(event.source, 'group_id', None) or getattr(event.source, 'room_id', None)
                          or getattr(event.source, 'user_id', None))
            if appt_scope:
                appt = extract_appointment_from_image(image_bytes)
                print(f"Appointment extract: is_appointment={bool(appt and appt.get('is_appointment'))}")
                if appt and appt.get('is_appointment'):
                    PENDING_APPT['data'] = appt
                    PENDING_APPT['expires'] = datetime.now(pytz.timezone('Asia/Bangkok')) + timedelta(minutes=10)
                    PENDING_APPT['group_id'] = appt_scope
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=format_appt_draft(appt)))
                    slip_replied = True
        except Exception as e:
            print(f"Appointment slip extract error: {e}")

        # 4) แจ้งแอดมิน (แนบลิงก์รูปให้กดดูได้)
        notify_admin(sender_name, category, f"📷 {summary_text}", thai_time, image_url)

        # ข้อ 5: ถ้ารอบนี้ยังไม่ได้ใช้ reply token (ไม่ได้เสนอร่างใบนัด) ใช้ทักทายเช้าได้
        if not slip_replied:
            maybe_morning_greeting(event)
    except Exception as e:
        print(f"Error handling image message: {e}")

@handler.add(MessageEvent, message=StickerMessage)
def handle_sticker_message(event):
    try:
        user_id = event.source.user_id
        sender_name = get_user_display_name(user_id, getattr(event.source, 'group_id', None))
        thai_time = get_thai_time(event.timestamp)
        print(f"Source -> user:{user_id} group:{getattr(event.source, 'group_id', None)} room:{getattr(event.source, 'room_id', None)}")

        sticker_id = event.message.sticker_id
        package_id = event.message.package_id
        # รูปประจำตัวสติ๊กเกอร์จาก CDN ของ LINE
        sticker_url = f"https://stickershop.line-scdn.net/stickershop/v1/sticker/{sticker_id}/android/sticker.png"
        summary_text = f"ส่งสติ๊กเกอร์ (package {package_id} / sticker {sticker_id})"

        append_log_row([thai_time, sender_name, "สติ๊กเกอร์", "-", summary_text, 0, f'=IMAGE("{sticker_url}")', sticker_url])
        notify_admin(sender_name, "สติ๊กเกอร์", f"😊 {summary_text}", thai_time, sticker_url)
        # ข้อ 5: สติ๊กเกอร์ไม่เคยใช้ reply token เลย เป็นตัวกระตุ้นทักทายเช้าได้เหมือนข้อความคุยเล่น
        maybe_morning_greeting(event)
    except Exception as e:
        print(f"Error handling sticker message: {e}")

@handler.add(PostbackEvent)
def handle_postback(event):
    # ตัวรับสัญญาณ "ปุ่มกด" (postback) — เกิดใหม่พร้อมเมนูฟุตบอลรอบ 13 (ก่อนหน้านี้บอทไม่เคยใช้ปุ่มเลย)
    # ปุ่มแต่ละปุ่มฝังค่า data ไว้ตอนสร้างการ์ด (เช่น football:fixtures) พอกด LINE จะส่งค่านั้นกลับมาที่นี่
    # ชุดปุ่มในอนาคตให้เพิ่มแขนงโดยดูคำขึ้นต้นของ data เป็นหลัก จะได้ไม่ชนกัน
    try:
        data = (event.postback.data or '') if getattr(event, 'postback', None) else ''
        print(f"Postback received: {data}")
        if data.startswith('football:'):
            mode = data.split(':', 1)[1]
            if mode == 'fixtures':
                text = football_fixtures_text()
            elif mode == 'results':
                text = football_results_text()
            elif mode == 'table':
                text = football_table_text()
            elif mode in FOOTBALL_TEAMS:
                text = football_team_text(mode)
            else:
                return
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text))
    except Exception as e:
        print(f"Postback handling error: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
