import os
import datetime
import base64
from flask import Flask, request, Response, jsonify
from dotenv import load_dotenv

from twilio.twiml.voice_response import VoiceResponse, Dial
from twilio.rest import Client

from lead_selector import select_leads

import gspread
from google.oauth2.service_account import Credentials

from flask_cors import CORS

load_dotenv()
app = Flask(__name__)

# TODO: Change this in prod once nehes isreal gives their fucking domain 
CORS(app, origins="*")

# CORS(app, origins=[
#     "http://localhost:3000",
#     "https://the-actual-domain.com"
# ])

ACCOUNT_SID = os.getenv('ACCOUNT_SID')
AUTH_TOKEN = os.getenv('AUTH_TOKEN')
API_KEY_SID = os.getenv('API_KEY_SID')
API_KEY_SECRET = os.getenv('API_KEY_SECRET')
TWILIO_NUMBER = os.getenv('TWILIO_NUMBER')
GOOGLE_SHEET_ID = os.getenv('GOOGLE_SHEET_ID')
GOOGLE_API_JSON = os.getenv('GOOGLE_API_JSON')

CALLBACK_BASE = os.getenv('CALLBACK_BASE', 'https://t6d2lxxc1vjn.share.zrok.io/')
VOICE_ACCEPT_PATH = "/voice/accept"
VOICE_BUSY_PATH = "/voice/busy"

def log_request(name):
    print(f"\n====== Incoming Twilio Webhook ({name}) ======")
    print(f"Timestamp: {datetime.datetime.now()}")
    print(f"Remote Addr: {request.remote_addr}")
    print(f"Headers:\n{dict(request.headers)}")
    print(f"Form Data:\n{dict(request.form)}")
    print("Raw Body:\n", request.get_data(as_text=True))
    print("=====================================\n")

def gspread_client():
    creds = Credentials.from_service_account_file(
        GOOGLE_API_JSON, 
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    return gspread.authorize(creds)

def log_call_to_sheet(call_sid, agent_number, customer_number, status="initiated", duration=None):
    try:
        gc = gspread_client()
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        sheet = sh.worksheet("call_history")
        row = [
            call_sid,
            datetime.datetime.utcnow().isoformat(),
            agent_number,
            customer_number,
            status,
            duration if duration else ""
        ]
        sheet.append_row(row)
    except Exception as exc:
        import traceback
        print(f"[Google Sheets] Failed to log: {exc}")
        traceback.print_exc()

def update_sheet_status(call_sid, status, duration=None, agent_number="", customer_number=""):
    try:
        gc = gspread_client()
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        sheet = sh.worksheet("call_history")
        # Fetch all and look for row with this SID in col 1
        records = sheet.get_all_records()
        for idx, rec in enumerate(records):
            rec_sid = str(rec.get("call_sid") or rec.get("id") or "").strip()
            if rec_sid == str(call_sid).strip():
                # Found, update status and duration (columns 5 and 6: E and F)
                rownum = idx + 2  # because get_all_records skips header, sheet row is idx+2
                sheet.update_cell(rownum, 5, status)
                if duration is not None:
                    sheet.update_cell(rownum, 6, duration)
                print(f"Updated row for CallSid {call_sid}: status={status}, duration={duration}")
                return
        # Not found, insert new (using whatever was given; Twilio will always send 'To' and 'From')
        sheet.append_row([
            call_sid,
            datetime.datetime.utcnow().isoformat(),
            agent_number,
            customer_number,
            status,
            duration if duration else ""
        ])
        print(f"Inserted log for new CallSid {call_sid}")
    except Exception as exc:
        import traceback
        print(f"[Google Sheets] Failed to update: {exc}")
        traceback.print_exc()

@app.route("/triple_call", methods=['GET', 'POST'])
def triple_call_twiml():
    leads = select_leads()
    vr = VoiceResponse()
    dial = Dial()
    for n in leads:
        dial.number(n)
    vr.append(dial)
    return Response(str(vr), mimetype="text/xml")

@app.route("/trigger_triple_call", methods=['POST'])
def trigger_triple_call():
    data = request.get_json(force=True)
    agent_number = data.get("agent")
    if not agent_number:
        return jsonify({"error": "Please provide 'agent' phone number."}), 400
    twiml_url = request.url_root.rstrip("/") + "/triple_call"
    callback_url = request.url_root.rstrip("/") + "/twilio_callback"
    client = Client(ACCOUNT_SID, AUTH_TOKEN)
    call = client.calls.create(
        to=agent_number,
        from_=TWILIO_NUMBER,
        url=twiml_url,
        method="POST",
        status_callback=callback_url,
        status_callback_event=["initiated","ringing","answered","completed","busy","failed","no-answer","canceled"],
        status_callback_method="POST"
    )
    return jsonify({"call_sid": call.sid})

@app.route("/target_call", methods=['GET', 'POST'])
def target_call_twiml():
    numbers = None
    if request.is_json:
        numbers = request.get_json(force=True).get("numbers")
    elif request.form.get("numbers"):
        numbers = request.form.get("numbers").split(",")
    elif request.args.get("numbers"):
        numbers = request.args.get("numbers").split(",")
    if not numbers:
        return Response("<Response><Say>No numbers provided</Say></Response>", mimetype="text/xml")
    vr = VoiceResponse()
    dial = Dial()
    for n in numbers:
        dial.number(n)
    vr.append(dial)
    return Response(str(vr), mimetype="text/xml")

@app.route("/trigger_target_call", methods=['POST'])
def trigger_target_call():
    data = request.get_json(force=True)
    agent_number = data.get("agent")
    numbers = data.get("numbers")
    if not agent_number or not numbers or not isinstance(numbers, list):
        return jsonify({"error": "Please provide 'agent' and list of 'numbers'!"}), 400
    num_string = ",".join(numbers)
    twiml_url = request.url_root.rstrip("/") + f"/target_call?numbers={num_string}"
    callback_url = request.url_root.rstrip("/") + "/twilio_callback"
    client = Client(ACCOUNT_SID, AUTH_TOKEN)
    call = client.calls.create(
        to=agent_number,
        from_=TWILIO_NUMBER,
        url=twiml_url,
        method="POST",
        status_callback=callback_url,
        status_callback_event=["initiated","ringing","answered","completed","busy","failed","no-answer","canceled"],
        status_callback_method="POST"
    )
    return jsonify({"call_sid": call.sid, "numbers": numbers})

@app.route("/twilio_callback",  methods=['GET', 'POST'])
def twilio_callback():
    if request.method == "POST":
        form = request.form
        print("[Twilio Callback] POST data:", dict(form))
    else:
        form = request.args
        print("[Twilio Callback] GET data:", dict(form))
        
    call_sid = form.get("CallSid")
    call_status = form.get("CallStatus")
    from_number = form.get("From")
    to_number = form.get("To")
    duration = form.get("CallDuration")
    print(f"[Twilio Callback] SID: {call_sid} | Status: {call_status} | From: {from_number} | To: {to_number} | Duration: {duration}")
    update_sheet_status(call_sid, call_status, duration, from_number, to_number)
    return ("", 204)

@app.route("/call_history", methods=["GET"])
def get_call_history():
    gc = gspread_client()
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    sheet = sh.worksheet("call_history")
    records = sheet.get_all_records()
    # Ensure duration is always an int (if present)
    for rec in records:
        if "duration" in rec and isinstance(rec["duration"], str) and rec["duration"].isdigit():
            rec["duration"] = int(rec["duration"])
        elif "duration" in rec and rec["duration"] == "":
            rec["duration"] = 0
    return jsonify(records)

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)