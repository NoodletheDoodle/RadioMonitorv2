import csv
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date
from pathlib import Path


def load_dotenv():
    if not ".env" in os.listdir(os.curdir):
        return
    with open(".env") as f:
        while (line:= f.readline().strip()):
            if line.startswith('#'):
                continue
            (var, val) = line.split('=', maxsplit=1)
            val = val.strip('"')
            os.environ[var] = val


load_dotenv()

SCRIPT_DIR = Path(__file__).parent
KEYCLOAK_TOKEN_URL = f"https://{os.environ['BASE_KEYCLOAK_URL']}/realms/odst/protocol/openid-connect/token"

def get_token(username, password, client_id=os.environ['CLIENT_ID'], client_secret=os.environ['CLIENT_SECRET']):
    data = urllib.parse.urlencode({
        "grant_type": "password",
        "client_id": client_id,
        "client_secret": client_secret,
        "username": username,
        "password": password,
    }).encode()
    req = urllib.request.Request(KEYCLOAK_TOKEN_URL, data=data)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["access_token"]

def create_event(evolution_id, event_type_id, description, severity=None, timestamp=None, longitude=None, latitude=None, altitude=None, metadata=None):
    url = f"https://{os.environ['BASE_AARDVARK_URL']}/api/v1/evolutions/{evolution_id}/events"

    payload = {
        "event_type_id": event_type_id,
        "description": description
    }

    if severity is not None:
        payload["severity"] = severity
    if timestamp is not None:
        payload["timestamp"] = timestamp
    if longitude is not None:
        payload["longitude"] = longitude
    if latitude is not None:
        payload["latitude"] = latitude
    if altitude is not None:
        payload["altitude"] = altitude
    if metadata is not None:
        payload["metadata"] = metadata

    token = get_token(os.environ['USERNAME'], os.environ['PASSWORD'])
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            class R:
                status_code = resp.status
                text = raw.decode()
                def json(self): return json.loads(raw)
            return R()
    except urllib.error.HTTPError as e:
        print(f"An error occurred: {e}")
        print(f"  Response body: {e.read().decode()}")
        return None
    except urllib.error.URLError as e:
        print(f"An error occurred: {e}")
        return None


def get(endpoint, params=None):
    token = get_token(os.environ['USERNAME'], os.environ['PASSWORD'])
    url = f"https://{os.environ['BASE_AARDVARK_URL']}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            class R:
                status_code = resp.status
                def json(self): return json.loads(raw)
            return R()
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        print(f"An error occurred: {e}")
        return None


def add_event_file(event_id, file_path):
    token = get_token(os.environ['USERNAME'], os.environ['PASSWORD'])
    url = f"https://{os.environ['BASE_AARDVARK_URL']}/api/v1/events/{event_id}/files"
    boundary = uuid.uuid4().hex
    filename = Path(file_path).name
    with open(file_path, "rb") as f:
        file_data = f.read()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: audio/x-wav\r\n\r\n"
    ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            class R:
                status_code = resp.status
                text = raw.decode()
            return R()
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        print(f"An error occurred: {e}")
        return None


if __name__ == '__main__':

    evolution_id = os.environ['EVOLUTION_ID']
    event_type_id = os.environ['EVENT_ID']

    # Setup here to look for the current date in the directory
    #today = date.today().strftime("%Y-%m-%d")
    today = date.today().strftime("2026-04-01")
    logs_dir = SCRIPT_DIR / "logs" / today

    if not logs_dir.exists():
        print(f"No logs directory found for today: {logs_dir}")
    else:
        # START: iterate through the directory to find csv path of the todays date
        for csv_path in sorted(logs_dir.glob("*.csv")):
            print(f"\nProcessing {csv_path.name}")
            # START: iterate through each row
            with open(csv_path, newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    time_str = row["local_start_time"][:5]  # HH:MM
                    timestamp = f"{today}T{time_str}"
                    description = "" # left blank for transcription to go into description in aardvark
                    metadata = {
                        "sender_ip": row["sender_ip"],
                        "unique_id": row["unique_id"],
                        "wav_filename": row["wav_filename"],
                        "relative_start": row["relative_start"],
                        "relative_end": row["relative_end"],
                        "local_end_time": row["local_end_time"],
                        "duration_seconds": row["duration_seconds"],
                    }
                    # START: Looks at the wave_filename column in the csv for each row, and POSTS the respective .wav file
                    response = create_event(evolution_id, event_type_id, description, timestamp=timestamp, metadata=json.dumps(metadata))
                    if response:
                        print(f"Created event for row {row['unique_id']}: {response.status_code}")
                        event_id = response.json()["data"]["event"]["id"]
                        wav_path = SCRIPT_DIR / "recordings" / today / row["channel_name"] / row["wav_filename"]
                        file_response = add_event_file(event_id, wav_path)
                        if file_response:
                            print(f"  Uploaded {row['wav_filename']}: {file_response.status_code}")
                        else:
                            print(f"  Failed to upload {row['wav_filename']}")
                    else:
                        print(f"Failed to create event for row {row['unique_id']}")
