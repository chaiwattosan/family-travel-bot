# เทสต์แพตช์รอบ 29 (A–F) — รัน: python3 test_r29.py
# โค้ด production ต้องต่อ LINE/Google/Gemini จริง เทสต์นี้จึง stub โมดูลภายนอก
# ทั้งหมดก่อน import main แล้วเทสต์เฉพาะ "ฟังก์ชันล้วน" (pure) ของแต่ละข้อ +
# โฟลว์รอเวลา (ข้อ A) แบบจำลองด้วย state ปลอม — ส่วนที่ผูกกับ API จริง
# (สร้างอีเวนต์ใบส่งตัว, รวมรูปผ่าน webhook) เทสต์ตรงนี้ไม่ได้ ต้องเทสต์สดในกลุ่ม
import sys
import types as _types
from datetime import datetime
from unittest.mock import MagicMock

import pytz

# ---------- stubs (ต้องมาก่อน import main) ----------
_flask = _types.ModuleType('flask')

class _FakeFlask:
    def __init__(self, name):
        self.name = name

    def route(self, *a, **k):
        def deco(fn):
            return fn
        return deco

    def run(self, *a, **k):
        pass

_flask.Flask = _FakeFlask
_flask.request = MagicMock()
_flask.abort = MagicMock()
sys.modules['flask'] = _flask

_linebot = _types.ModuleType('linebot')

class _FakeLineBotApi:
    def __init__(self, token=None):
        self.token = token

    def reply_message(self, *a, **k):
        pass

    def push_message(self, *a, **k):
        pass

class _FakeHandler:
    def __init__(self, secret=None):
        pass

    def add(self, *a, **k):
        # ต้องคืน identity decorator ไม่งั้นฟังก์ชันที่ถูก decorate กลายเป็น MagicMock
        def deco(fn):
            return fn
        return deco

    def handle(self, *a, **k):
        pass

_linebot.LineBotApi = _FakeLineBotApi
_linebot.WebhookHandler = _FakeHandler
sys.modules['linebot'] = _linebot

_lb_exc = _types.ModuleType('linebot.exceptions')

class InvalidSignatureError(Exception):
    pass

class LineBotApiError(Exception):
    pass

_lb_exc.InvalidSignatureError = InvalidSignatureError
_lb_exc.LineBotApiError = LineBotApiError
sys.modules['linebot.exceptions'] = _lb_exc

_lb_models = _types.ModuleType('linebot.models')

class _Dummy:
    def __init__(self, *a, **k):
        pass

for _name in ('MessageEvent', 'TextMessage', 'TextSendMessage', 'ImageMessage',
              'StickerMessage', 'PostbackEvent', 'FlexSendMessage', 'BubbleContainer',
              'BoxComponent', 'TextComponent', 'ButtonComponent', 'PostbackAction',
              'SeparatorComponent'):
    setattr(_lb_models, _name, _Dummy)
sys.modules['linebot.models'] = _lb_models

sys.modules['gspread'] = MagicMock()

_google = _types.ModuleType('google')
_oauth2 = _types.ModuleType('google.oauth2')
_sa = _types.ModuleType('google.oauth2.service_account')
_sa.Credentials = MagicMock()
_google.oauth2 = _oauth2
_oauth2.service_account = _sa
sys.modules['google'] = _google
sys.modules['google.oauth2'] = _oauth2
sys.modules['google.oauth2.service_account'] = _sa

_gapi = _types.ModuleType('googleapiclient')
_gapi_discovery = _types.ModuleType('googleapiclient.discovery')

class _FakeEvents:
    def list(self, **k):
        return self

    def execute(self):
        return {'items': []}

class _FakeCalendarService:
    def events(self):
        return _FakeEvents()

_gapi_discovery.build = lambda *a, **k: _FakeCalendarService()
_gapi.discovery = _gapi_discovery
sys.modules['googleapiclient'] = _gapi
sys.modules['googleapiclient.discovery'] = _gapi_discovery

_genai = _types.ModuleType('google.genai')
_genai.Client = MagicMock()
_genai_types = _types.ModuleType('google.genai.types')
_genai.types = _genai_types
_google.genai = _genai
sys.modules['google.genai'] = _genai
sys.modules['google.genai.types'] = _genai_types

import main  # noqa: E402

TZ = pytz.timezone('Asia/Bangkok')
NOW = TZ.localize(datetime(2026, 11, 27, 9, 30))
SUTURE_MSG = 'นัดตัดไหม เลื่อนเป็นเสาร์ที่ 28 พฤศจิกายน 2569 เวลา 10:00'

failures = []

def check(name, cond):
    print(('PASS' if cond else 'FAIL'), '-', name)
    if not cond:
        failures.append(name)

# ---------- ข้อ A: ด่านคำใบ้ "ไหม" + ตัวแกะคำตอบเต็มประโยค ----------
check('A1 is_schedule_question(ข้อความตัดไหม) = False (ไม่โดน QA แย่ง)',
      main.is_schedule_question(SUTURE_MSG) is False)
check('A2 is_schedule_question("พรุ่งนี้มีนัดไหม") = True',
      main.is_schedule_question('พรุ่งนี้มีนัดไหม') is True)
check('A3 is_schedule_question("พรุ่งนี้มีนัดไหมครับ") = True (คำลงท้าย)',
      main.is_schedule_question('พรุ่งนี้มีนัดไหมครับ') is True)
check('A4 qa_cue_matches: "ไหม" กลางประโยค (นัดตัดไหมพรุ่งนี้) ไม่นับเป็นคำถาม',
      main.qa_cue_matches('นัดตัดไหมพรุ่งนี้', 'ไหม') is False)
check('A5 parse_full_appt_answer(ข้อความตัดไหม) = 2026-11-28 + 10:00',
      main.parse_full_appt_answer(SUTURE_MSG, NOW) == ('2026-11-28', (10, 0)))

# ---------- ข้อ A: โฟลว์รอเวลาแบบจำลอง (state ปลอม + ตัวลงปฏิทินปลอม) ----------
import json as _json  # noqa: E402

_pending_payload = _json.dumps({
    'appt': {'title': 'พาธงไท ตั้งทองไปตัดไหม', 'patient_name': 'ธงไท ตั้งทอง',
             'date': '2026-11-27', 'time': '08:00', 'time_is_default': True,
             'department': None, 'hospital': 'รพ.มหาราชนครเชียงใหม่', 'note': None,
             'steps': [], 'steps_title': None},
    'expires': (NOW + main.timedelta(minutes=30)).isoformat(),
}, ensure_ascii=False)
_captured = {}
main.read_state_value = lambda key: (True, _pending_payload) if key.endswith('G1') else (True, '')
main.write_state_value = lambda key, value: True
main.add_appt_to_calendar = lambda appt: _captured.update(appt) or 'https://calendar.example/x'

class _Src:
    group_id = 'G1'
    room_id = None
    user_id = 'U1'

class _Evt:
    source = _Src()
    reply_token = 'tok'

_reply = main.handle_pending_appt_time_answer(_Evt(), SUTURE_MSG, now=NOW, early=True)
check('A6 โฟลว์รอเวลา (early) รับคำตอบเต็มประโยคและลงนัดวันใหม่ 2026-11-28 10:00',
      _captured.get('date') == '2026-11-28' and _captured.get('time') == '10:00'
      and _captured.get('time_is_default') is False)
check('A7 ข้อความยืนยันบอกวัน/เวลาสุดท้ายชัด (28 พฤศจิกายน + 10:00)',
      bool(_reply) and '28 พฤศจิกายน' in _reply and '10:00' in _reply)

# ---------- ข้อ B: คีย์รวมเคส + การรวมข้อมูล ----------
occ_a = {'patient_name': 'ธงไท ตั้งทอง', 'date': '2026-12-08', 'time': '13:00',
         'time_is_default': False, 'department': 'คลินิกอาชีวเวชกรรม',
         'title': 'พาธงไท ตั้งทองไปคลินิกอาชีวเวชกรรม', 'hospital': 'รพ.มหาราช',
         'note': 'คิว 34', 'steps': [], 'steps_title': None}
occ_b = dict(occ_a)  # ใบเดียวกันส่งซ้ำ (สแกน + ภาพถ่าย) ถอดได้เหมือนกัน
check('B1 ใบอาชีวเวชกรรมซ้ำ = เคสเดียวกัน', main._appt_case_key_match(occ_a, occ_b) is True)
check('B2 คนละวัน = คนละเคส',
      main._appt_case_key_match(occ_a, dict(occ_a, date='2026-12-09')) is False)
check('B3 คนละผู้ป่วย = คนละเคส',
      main._appt_case_key_match(occ_a, dict(occ_a, patient_name='อากง')) is False)
steps_form = {'patient_name': 'ธงไท ตั้งทอง', 'date': '2026-12-08', 'time': '08:00',
              'time_is_default': True, 'department': 'คลินิกโรคระบบประสาท',
              'title': 'พาธงไท ตั้งทองไปคลินิกโรคระบบประสาท', 'hospital': 'รพ.มหาราช',
              'note': None, 'steps_title': 'ขั้นตอนวันนัด',
              'steps': ['ติดต่อ CSC ชั้น 1', 'ชำระเงิน', 'เจาะเลือด', 'ห้องตรวจ 21']}
neuro_slip = {'patient_name': 'ธงไท ตั้งทอง', 'date': '2026-12-08', 'time': '10:30',
              'time_is_default': False, 'department': 'คลินิกโรคระบบประสาท',
              'title': 'พาธงไท ตั้งทองไปคลินิกโรคระบบประสาท', 'hospital': 'รพ.มหาราช',
              'note': 'คิว 17', 'steps': [], 'steps_title': None}
check('B4 ใบขั้นตอนของนัดประสาท 8 ธ.ค. = เคสเดียวกับใบนัด',
      main._appt_case_key_match(neuro_slip, steps_form) is True)
_merged = main._merge_appt_fields(neuro_slip, steps_form)
check('B5 รวมแล้วได้ steps 4 ข้อ + เวลาจริง 10:30 ไม่ถูกค่าตั้งต้นทับ',
      len(_merged['steps']) == 4 and _merged['time'] == '10:30'
      and _merged['steps_title'] == 'ขั้นตอนวันนัด')
check('B6 รวมใบซ้ำเป๊ะ = ไม่เปลี่ยนอะไร (merged == เดิม)',
      main._merge_appt_fields(occ_a, occ_b) == occ_a)

# ---------- ข้อ D: แก้ร่างด้วยข้อความ ----------
check('D1 "ผู้ป่วย ธงไท ตั้งทอง" → ฟิลด์ patient_name',
      main.parse_appt_draft_edit('ผู้ป่วย ธงไท ตั้งทอง', NOW) == ('patient_name', 'ธงไท ตั้งทอง'))
_wrong = {'patient_name': 'ชลิไท ทั่วทอง', 'title': 'พาชลิไท ทั่วทองไปตรวจ EEG',
          'date': '2027-03-19', 'time': '08:30', 'time_is_default': False}
_fixed = main.apply_appt_draft_edit(_wrong, 'patient_name', 'ธงไท ตั้งทอง')
check('D2 แก้ผู้ป่วยแล้วหัวข้อถูกสร้างใหม่เป็นชื่อถูก',
      _fixed['patient_name'] == 'ธงไท ตั้งทอง' and 'ธงไท ตั้งทอง' in _fixed['title']
      and 'ชลิไท' not in _fixed['title'])
check('D3 ข้อความคุยเล่นไม่ใช่คำสั่งแก้', main.parse_appt_draft_edit('วันนี้อากาศดีจัง', NOW) is None)
check('D4 "เวลา 10:30" → ฟิลด์ time แบบมาตรฐาน',
      main.parse_appt_draft_edit('เวลา 10:30', NOW) == ('time', '10:30'))

# ---------- ข้อ F: ตัดสินข้าม/ส่ง push ชั้น 1440 ----------
ev_plain = {'summary': 'พาธงไท ตั้งทองไปคลินิกอาชีวเวชกรรม รพ.มหาราช',
            'description': 'ผู้ป่วย: ธงไท ตั้งทอง\nแผนก: คลินิกอาชีวเวชกรรม\n[remind:1440,30]'}
ev_eeg = {'summary': 'พาธงไท ตั้งทองไปตรวจ EEG รพ.มหาราช',
          'description': ('ผู้ป่วย: ธงไท ตั้งทอง\n'
                          'ขั้นตอนการเตรียมตัวตรวจคลื่นไฟฟ้าสมอง:\n'
                          '1. สระผมล่วงหน้า 1 วัน ห้ามใส่น้ำมัน/ครีม/สเปรย์\n'
                          '2. อดนอนคืนก่อนตรวจ\n[remind:1440,30]')}
check('F1 นัด รพ. ไม่มีเตรียมตัว → ข้าม push 1440 ได้เมื่อสรุปเช้าเล่าแล้ว',
      main.reminder_1day_hospital_skippable_by_greeting(ev_plain, 1440, [1440, 30]) is True)
check('F2 นัด EEG มีเตรียมตัว → ห้ามข้าม (ต้องส่ง push)',
      main.reminder_1day_hospital_skippable_by_greeting(ev_eeg, 1440, [1440, 30]) is False)
check('F3 ไม่ใช่นัด รพ. → ไม่เข้าเกณฑ์ข้ามแบบ รพ.',
      main.reminder_1day_hospital_skippable_by_greeting(
          {'summary': 'ไปเที่ยวสวนสัตว์', 'description': ''}, 1440, [1440, 30]) is False)
check('F4 ชั้นอื่น (30 นาที) ไม่เข้าเกณฑ์ฟังก์ชัน 1440',
      main.reminder_1day_hospital_skippable_by_greeting(ev_plain, 30, [1440, 30]) is False)
check('F5 event_has_advance_prep จับ EEG ได้ / นัดธรรมดาจับไม่ได้',
      main.event_has_advance_prep(ev_eeg) is True and main.event_has_advance_prep(ev_plain) is False)

# ---------- ข้อ E(4): ดึงรายละเอียดขึ้นโชว์ ----------
_desc = ('ผู้ป่วย: ธงไท ตั้งทอง\n🚗 เวลาออกบ้าน: 07:00 น.\n'
         'ขั้นตอนวันนัด:\n1. ติดต่อ CSC ชั้น 1 อาคารเฉลิมพระบารมี\n2. ชำระเงิน\n[remind:1440,30]')
_dl = main.event_detail_lines(_desc)
check('E1 event_detail_lines ได้บรรทัดออกบ้าน + ขั้นตอนแรก',
      len(_dl) == 2 and _dl[0].startswith('🚗') and 'CSC' in _dl[1])
check('E2 description ว่าง → ไม่มีบรรทัดเสริม', main.event_detail_lines('') == [])

print()
if failures:
    print(f'FAILED {len(failures)} เคส:', failures)
    sys.exit(1)
print('ALL TESTS PASSED')
