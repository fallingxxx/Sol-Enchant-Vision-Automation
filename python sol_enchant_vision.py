import base64
import msvcrt
import os
import re
import socket
import subprocess
import sys
import threading
import time

import av
import cv2
import requests

try:
    import pytesseract

    # Windows Tesseract OCR executable
    TESSERACT_EXE = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(TESSERACT_EXE):
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_EXE

    OCR_AVAILABLE = True
except Exception:
    pytesseract = None
    OCR_AVAILABLE = False


# ============================================================
# CONFIG
# ============================================================

ADB_EXE = r"C:\Users\ramye\Downloads\scrcpy-win64-v4.1\scrcpy-win64-v4.1\adb.exe"

ADB_SERIAL = "R5CN3112WGP"

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
MODEL = "qwen2.5vl:3b"


REMOTE_SERVER_JAR = "/data/local/tmp/scrcpy-server-v4.1.jar"

SCRCPY_VERSION = "4.1"

FORWARD_PORT = 27183


# scrcpy video

VIDEO_MAX_SIZE = 720
VIDEO_MAX_FPS = 15
VIDEO_BIT_RATE = 2_000_000


# Vision coordinate system

VISION_WIDTH = 720
VISION_HEIGHT = 324


# Android original coordinate

ADB_WIDTH = 1600
ADB_HEIGHT = 720


# VLM

VISION_INTERVAL = 0.5
STATE_VLM_INTERVAL = 2.0

VLM_TIMEOUT = 120

VLM_RETRY_COUNT = 1

VLM_RETRY_DELAY = 0.5

# VLM request serialization / cooldown.
# The RTX 3050 4GB environment can become unstable when multiple
# image requests are issued back-to-back. Every VLM request is
# serialized and a minimum gap is enforced between requests.
VLM_REQUEST_LOCK = threading.Lock()
VLM_LAST_END_TIME = 0.0
VLM_MIN_GAP = 2.0
HOME_VERIFY_INTERVAL = 3.0
VLM_FAILURE_RESTART_THRESHOLD = 1
VLM_FAILURE_COOLDOWN = 8.0
VLM_RECOVERY_UNTIL = 0.0

# OCR primary detection. OCR is intentionally much cheaper than VLM.
OCR_ENABLED = True
OCR_INTERVAL = 0.5
OCR_MIN_TEXT_CONFIDENCE = 45
OCR_LANG = "kor+eng"


# state

STABLE_COUNT_REQUIRED = 3

MIN_CONFIDENCE = 0.55


VALID_STATES = {
    "NORMAL",
    "BATTLE",
    "MENU",
    "INVENTORY",
    "SHOP",
    "HOME",
    "UNKNOWN",
}


# inventory

INVENTORY_MIN_CONFIDENCE = 0.70


# target

ENABLE_TARGET_DETECTION = False

TARGET_MIN_CONFIDENCE = 0.60

TARGET_VERIFY_MIN_CONFIDENCE = 0.70

TARGET_RETRY_COUNT = 1


# safety

DRY_RUN = False

ENABLE_INVENTORY_ACTION = True
ENABLE_SHOP_ACTION = False
ENABLE_HOME_ACTION = False

# Game-native auto hunting. We do not detect or tap individual monsters.
ENABLE_AUTO_ACTION = True
AUTO_MIN_CONFIDENCE = 0.85
AUTO_CHECK_INTERVAL = 5.0
AUTO_OFF_CONFIRM_REQUIRED = 2

# Safe target diagnostic mode: detect and print coordinates, never tap.
TARGET_DIAGNOSTIC_ONLY = False
TARGET_DIAGNOSTIC_INTERVAL = 3.0



# ============================================================
# REGEX
# ============================================================


STATE_RE = re.compile(
    r"(NORMAL|BATTLE|MENU|INVENTORY|SHOP|HOME|UNKNOWN)"
    r"\s*[\|:]?\s*"
    r"(100(?:\.\d+)?|[0-9]{1,2}(?:\.\d+)?)"
    r"\s*%?",
    re.I,
)


YES_NO_RE = re.compile(
    r"(YES|NO)"
    r"\s*[\|:]?\s*"
    r"(100(?:\.\d+)?|[0-9]{1,2}(?:\.\d+)?)"
    r"\s*%?",
    re.I,
)


TARGET_RE = re.compile(
    r"TARGET"
    r"\s*[\|:]"
    r"\s*(\d+)"
    r"\s*[\|:]"
    r"\s*(\d+)"
    r"\s*[\|:]"
    r"(100(?:\.\d+)?|[0-9]{1,2}(?:\.\d+)?)"
    r"\s*%?"
    r"\s*[\|:]"
    r"\s*(.*)",
    re.I,
)


TARGET_N_RE = re.compile(
    r"TARGET([1-3])"
    r"\s*[\|:]"
    r"\s*(\d+)"
    r"\s*[\|:]"
    r"\s*(\d+)"
    r"\s*[\|:]"
    r"(100(?:\.\d+)?|[0-9]{1,2}(?:\.\d+)?)"
    r"\s*%?"
    r"\s*[\|:]"
    r"\s*(.*)",
    re.I,
)



# ============================================================
# ADB
# ============================================================


def adb_run(args, timeout=10):

    try:

        return subprocess.run(
            [
                ADB_EXE,
                "-s",
                ADB_SERIAL,
            ]
            + list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )

    except Exception as e:

        print("[ADB ERROR]", repr(e))

        return None



def check_adb():

    print("[ADB] checking device")

    result = adb_run(
        [
            "get-state"
        ],
        timeout=5,
    )


    if (
        result is None
        or result.returncode != 0
        or result.stdout.strip() != "device"
    ):

        raise RuntimeError(
            "ADB connection failed"
        )


    print(
        "[ADB] connected:",
        ADB_SERIAL
    )



# ============================================================
# Coordinate conversion
# ============================================================


def vision_to_adb(x, y):

    ax = round(
        x * ADB_WIDTH / VISION_WIDTH
    )

    ay = round(
        y * ADB_HEIGHT / VISION_HEIGHT
    )


    return (
        max(0, min(ADB_WIDTH - 1, ax)),
        max(0, min(ADB_HEIGHT - 1, ay)),
    )



def print_coordinate_bridge():

    print("[COORDINATE BRIDGE]")


    points = [
        (0,0),
        (
            VISION_WIDTH//2,
            VISION_HEIGHT//2
        ),
        (
            VISION_WIDTH-1,
            VISION_HEIGHT-1
        ),
    ]


    for x,y in points:

        ax,ay = vision_to_adb(x,y)

        print(
            f"Vision({x},{y}) -> ADB({ax},{ay})"
        )



# ============================================================
# Ollama VLM
# ============================================================


def ollama_chat(
    prompt,
    image=None,
    timeout=VLM_TIMEOUT
):

    message = {
        "role":"user",
        "content":prompt,
    }


    if image is not None:

        ok, encoded = cv2.imencode(
            ".jpg",
            image,
            [
                cv2.IMWRITE_JPEG_QUALITY,
                65
            ]
        )


        if not ok:

            raise RuntimeError(
                "jpeg encode failed"
            )


        message["images"] = [
            base64.b64encode(
                encoded.tobytes()
            ).decode("ascii")
        ]



    payload = {

        "model":MODEL,

        "messages":[message],

        "stream":False,

        "options":{
            "temperature":0.0,
            "num_predict":12,
            "repeat_penalty":1.15,
        }

    }


    global VLM_LAST_END_TIME

    # Only one image request may be active at a time.
    # Also leave a small cooldown after the previous request.
    with VLM_REQUEST_LOCK:

        now = time.time()
        wait = (
            VLM_MIN_GAP
            - (now - VLM_LAST_END_TIME)
        )

        if wait > 0:
            print(
                f"[VLM COOLDOWN] waiting {wait:.2f}s"
            )
            time.sleep(wait)

        for attempt in range(
            VLM_RETRY_COUNT + 1
        ):

            if time.time() < VLM_RECOVERY_UNTIL:
                remaining = VLM_RECOVERY_UNTIL - time.time()
                print(f"[VLM RECOVERY GATE] waiting {remaining:.2f}s")
                return {"message": {"content": ""}}

            try:

                r = requests.post(
                    OLLAMA_URL,
                    json=payload,
                    timeout=timeout,
                )



                if not r.ok:

                    print(
                        "[OLLAMA HTTP]",
                        r.status_code,
                        r.text[:1000]
                    )

                    r.raise_for_status()

                VLM_LAST_END_TIME = time.time()

                return r.json()


            except Exception as e:

                print(
                    "[OLLAMA ERROR]",
                    repr(e)
                )

                if attempt < VLM_RETRY_COUNT:

                    time.sleep(
                        VLM_RETRY_DELAY
                    )

        VLM_LAST_END_TIME = time.time()

        raise RuntimeError(
            "Ollama failed"
        )



def restart_vlm_after_repeated_failure():
    """Enter a short VLM recovery cooldown without crashing the worker."""
    global VLM_RECOVERY_UNTIL

    VLM_RECOVERY_UNTIL = time.time() + VLM_FAILURE_COOLDOWN

    print(
        f"[VLM RECOVERY] cooldown {VLM_FAILURE_COOLDOWN:.1f}s"
    )


def ollama_text(prompt):

    result = ollama_chat(
        prompt
    )

    return (
        result
        .get("message", {})
        .get("content","")
        .strip()
    )
# ============================================================
# SOCKET READER
# ============================================================


class SocketReader:

    def __init__(self, sock, initial_data=b""):

        self.sock = sock
        self.buffer = bytearray(initial_data)
        self.closed = False


    def read(self, size=-1):

        if self.closed:

            return b""


        if size < 0:

            size = 65536


        while len(self.buffer) < size:

            try:

                data = self.sock.recv(
                    65536
                )

            except socket.timeout:

                continue


            except Exception:

                self.closed = True
                break


            if not data:

                self.closed = True
                break


            self.buffer.extend(
                data
            )


        result = bytes(
            self.buffer[:size]
        )


        del self.buffer[:size]


        return result



    def close(self):

        self.closed = True





# ============================================================
# VISION CAPTURE
# ============================================================


class VisionCapture:


    def __init__(self):


        self.server_process = None

        self.sock = None

        self.reader = None

        self.container = None


        self.decode_thread = None


        self.stop_event = threading.Event()


        self.frame_lock = threading.Lock()


        self.latest_frame = None

        self.latest_frame_id = 0


        self.running = False



    # --------------------------------------------------------
    # scrcpy cleanup
    # --------------------------------------------------------


    def cleanup_server(self):

        print(
            "[SERVER] cleanup"
        )


        adb_run(
            [
                "shell",
                "pkill",
                "-f",
                "com.genymobile.scrcpy.Server"
            ],
            timeout=5
        )


        time.sleep(
            0.5
        )



    # --------------------------------------------------------
    # adb forward
    # --------------------------------------------------------


    def setup_forward(self):

        print(
            "[FORWARD] setup"
        )


        adb_run(
            [
                "forward",
                "--remove",
                f"tcp:{FORWARD_PORT}"
            ],
            timeout=5
        )


        result = adb_run(
            [
                "forward",
                f"tcp:{FORWARD_PORT}",
                "localabstract:scrcpy"
            ],
            timeout=5
        )


        if (
            result is None
            or result.returncode != 0
        ):

            raise RuntimeError(
                "adb forward failed"
            )


        print(
            "[FORWARD] OK"
        )



    # --------------------------------------------------------
    # start scrcpy server
    # --------------------------------------------------------


    def start_server(self):


        command = (

            f"CLASSPATH={REMOTE_SERVER_JAR} "

            f"app_process / "

            f"com.genymobile.scrcpy.Server "

            f"{SCRCPY_VERSION} "

            f"tunnel_forward=true "

            f"video=true "

            f"audio=false "

            f"turn_screen_off=true "

            f"control=false "

            f"cleanup=false "

            f"raw_stream=true "

            f"max_size={VIDEO_MAX_SIZE} "

            f"max_fps={VIDEO_MAX_FPS} "

            f"video_bit_rate={VIDEO_BIT_RATE} "

            f"video_encoder=c2.android.avc.encoder"

        )


        print(
            "[SERVER] starting"
        )


        self.server_process = subprocess.Popen(

            [
                ADB_EXE,
                "-s",
                ADB_SERIAL,
                "shell",
                command
            ],

            stdout=subprocess.DEVNULL,

            stderr=subprocess.DEVNULL,

            creationflags=subprocess.CREATE_NO_WINDOW

        )


        time.sleep(
            1
        )


        if self.server_process.poll() is not None:

            raise RuntimeError(
                "scrcpy server stopped"
            )




    # --------------------------------------------------------
    # connect raw h264
    # --------------------------------------------------------


    def connect_video(self):


        print(
            "[VIDEO] waiting"
        )


        deadline = time.time() + 10


        while time.time() < deadline:


            try:

                sock = socket.create_connection(

                    (
                        "127.0.0.1",
                        FORWARD_PORT
                    ),

                    timeout=3

                )


                sock.settimeout(1)


                first = sock.recv(
                    65536
                )


                if not first:

                    raise RuntimeError(
                        "empty stream"
                    )


                self.sock = sock


                print(
                    "[VIDEO] connected",
                    len(first),
                    "bytes"
                )


                return first



            except Exception as e:


                print(
                    "[VIDEO WAIT]",
                    e
                )


                time.sleep(
                    0.3
                )


        raise RuntimeError(
            "video connection failed"
        )




    # --------------------------------------------------------
    # decode thread
    # --------------------------------------------------------


    def decode_loop(self, initial):


        print(
            "[PYAV] decoder start"
        )


        try:


            self.reader = SocketReader(
                self.sock,
                initial
            )


            self.container = av.open(
                self.reader,
                format="h264"
            )


            stream = (
                self.container
                .streams
                .video[0]
            )


            print(
                "[PYAV] codec:",
                stream.codec_context.name
            )



            count = 0

            start = time.time()



            for frame in self.container.decode(
                video=0
            ):


                if self.stop_event.is_set():

                    break



                image = frame.to_ndarray(
                    format="bgr24"
                )



                image = cv2.resize(
                    image,
                    (
                        VISION_WIDTH,
                        VISION_HEIGHT
                    ),
                    interpolation=cv2.INTER_AREA
                )



                with self.frame_lock:


                    self.latest_frame = image.copy()

                    self.latest_frame_id += 1



                count += 1



                if count == 1:

                    print(
                        "[VISION] first frame"
                    )

                    cv2.imwrite(
                        "vision_debug.jpg",
                        image
                    )



                if count % 30 == 0:

                    fps = (
                        count /
                        max(
                            time.time()-start,
                            0.01
                        )
                    )

                    print(
                        f"[VISION] fps={fps:.2f}"
                    )



        except Exception as e:


            if not self.stop_event.is_set():

                print(
                    "[PYAV ERROR]",
                    repr(e)
                )


        finally:

            self.running = False




    # --------------------------------------------------------
    # start capture
    # --------------------------------------------------------


    def start(self):


        self.stop_event.clear()


        self.cleanup_server()

        self.setup_forward()

        self.start_server()


        first = self.connect_video()


        self.running = True


        self.decode_thread = threading.Thread(

            target=self.decode_loop,

            args=(first,),

            daemon=True

        )


        self.decode_thread.start()




    # --------------------------------------------------------
    # snapshot
    # --------------------------------------------------------


    def get_snapshot(self):


        with self.frame_lock:


            if self.latest_frame is None:

                return None,0



            return (
                self.latest_frame.copy(),
                self.latest_frame_id
            )




    # --------------------------------------------------------
    # stop
    # --------------------------------------------------------


    def stop(self):


        print(
            "[VISION] stopping"
        )


        self.stop_event.set()



        if self.reader:

            self.reader.close()



        if self.sock:

            try:

                self.sock.close()

            except:

                pass



        if self.decode_thread:

            self.decode_thread.join(
                timeout=3
            )



        if self.server_process:

            try:

                self.server_process.terminate()

            except:

                pass



        adb_run(
            [
                "forward",
                "--remove",
                f"tcp:{FORWARD_PORT}"
            ],
            timeout=5
        )


        self.running = False


        print(
            "[VISION] stopped"
        )
        
# ============================================================
# OCR SCREEN DETECTION
# ============================================================


OCR_STATE_KEYWORDS = {
    "SHOP": (
        "상점", "상인", "구매", "상품", "판매상"
    ),
    "INVENTORY": (
        "인벤토리", "가방", "장비", "아이템"
    ),
    "MENU": (
        "메뉴"
    ),
    "HOME": (
        "마을", "마을광장", "마을입구", "거점", "본거지"
    ),
}


def normalize_ocr_text(text):
    if not text:
        return ""
    return re.sub(r"\s+", "", str(text)).lower()


def ocr_screen(frame):
    """Read visible screen text only. Never invent a state from coordinates."""
    if not OCR_ENABLED or not OCR_AVAILABLE or frame is None:
        return []

    try:
        # Inventory and shop have a distinctive Korean title at the upper-left.
        # OCR that small region at higher scale because full-screen OCR at
        # 720x324 is too noisy to reliably read the title.
        h, w = frame.shape[:2]
        roi = frame[0:min(h, 150), 0:min(w, 380)]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, None, fx=4.0, fy=4.0, interpolation=cv2.INTER_CUBIC)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        processed = cv2.threshold(
            gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )[1]

        data = pytesseract.image_to_data(
            processed,
            lang=OCR_LANG,
            config="--psm 11",
            output_type=pytesseract.Output.DICT,
        )
        results = []
        for i, raw_text in enumerate(data.get("text", [])):
            text = str(raw_text).strip()
            if not text:
                continue
            try:
                conf = float(data["conf"][i])
            except Exception:
                conf = 0.0
            if conf < OCR_MIN_TEXT_CONFIDENCE:
                continue
            results.append({"text": text, "confidence": conf})

        return results

    except Exception as e:
        print("[OCR ERROR]", repr(e))
        return []


def detect_ui_title(frame):
    """Detect the small Korean UI title in the extreme upper-left corner."""
    if not OCR_ENABLED or not OCR_AVAILABLE or frame is None:
        return None, 0.0
    try:
        h, w = frame.shape[:2]

        # The title can shift slightly depending on the current game UI scale.
        # Try several small upper-left ROIs instead of relying on a single crop.
        rois = [
            frame[0:min(h, 48), 0:min(w, 145)],
            frame[0:min(h, 72), 0:min(w, 220)],
            frame[0:min(h, 96), 0:min(w, 300)],
        ]

        for roi in rois:
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            gray = cv2.createCLAHE(
                clipLimit=2.0,
                tileGridSize=(8, 8),
            ).apply(gray)

            enlarged = cv2.resize(
                gray,
                None,
                fx=6.0,
                fy=6.0,
                interpolation=cv2.INTER_CUBIC,
            )

            variants = [enlarged]
            for threshold in (135, 155, 175, 195, 215):
                variants.append(
                    cv2.threshold(
                        enlarged,
                        threshold,
                        255,
                        cv2.THRESH_BINARY,
                    )[1]
                )
                variants.append(
                    cv2.threshold(
                        enlarged,
                        threshold,
                        255,
                        cv2.THRESH_BINARY_INV,
                    )[1]
                )

            for image in variants:
                for psm in (7, 8, 13):
                    text = pytesseract.image_to_string(
                        image,
                        lang=OCR_LANG,
                        config="--psm "
                        + str(psm)
                        + " -c preserve_interword_spaces=0",
                    )
                    compact = normalize_ocr_text(text)

                    if "인벤토리" in compact:
                        return "INVENTORY", 0.99

                    if "상점" in compact or "상인" in compact:
                        return "SHOP", 0.99

        return None, 0.0
    except Exception as e:
        print("[UI TITLE OCR ERROR]", repr(e))
        return None, 0.0
def classify_ocr_state(ocr_results):
    """Return a state only when OCR finds a strong explicit keyword."""
    if not ocr_results:
        return None, 0.0, []

    texts = [item["text"] for item in ocr_results]
    compact = "".join(normalize_ocr_text(t) for t in texts)
    hits = []

    for state, keywords in OCR_STATE_KEYWORDS.items():
        for keyword in keywords:
            key = normalize_ocr_text(keyword)
            if key and key in compact:
                hits.append((state, keyword))

    # Shop wins over inventory because shop screens can contain item rows.
    for priority_state in ("SHOP", "INVENTORY", "MENU", "HOME"):
        matches = [h for h in hits if h[0] == priority_state]
        if matches:
            confidence = min(0.99, 0.80 + 0.05 * len(matches))
            return priority_state, confidence, matches

    return None, 0.0, hits


# ============================================================
# VLM STATE DETECTION
# ============================================================


STATE_PROMPT = """
Analyze this Sol Enchant screenshot and classify the CURRENT MAIN SCREEN.

Return the first line only in this exact format:

STATE|confidence

Allowed states:
NORMAL
BATTLE
MENU
INVENTORY
SHOP
HOME
UNKNOWN

Priority rules:
1. SHOP has priority over INVENTORY when a merchant/product interface is visible.
2. INVENTORY means the player's own inventory/equipment/item-management screen.
3. HOME means an actual town/base/home scene. Buildings, streets, NPCs,
   town facilities or a clearly recognizable town/base context should be visible.
4. "(안전)" means only a PK-disabled safe area. It is NOT proof of HOME.
5. BATTLE requires actual active combat evidence such as enemy combat UI,
   target/HP bars, damage numbers, attack effects or an active attack scene.
   A safe-area dungeon, hunting dungeon, field, or other PvE area where the
   character is merely present or moving is NOT BATTLE. If no active combat
   evidence is visible, prefer NORMAL.
6. MENU is a large general menu panel that is not inventory or shop.
7. NORMAL is ordinary field/gameplay without active combat.
8. UNKNOWN if the screenshot is genuinely ambiguous.
9. Do not infer BATTLE merely from a dungeon, monster-hunting location,
   "(안전)", minimap context, or the presence of game HUD elements.

Important:
- Do not output explanations before or after the STATE line.
- Do not confuse small HUD icons with MENU, SHOP or INVENTORY.
- Do not classify a safe field area as HOME only because "(안전)" is visible.
- If both shop-like and inventory-like elements appear, choose SHOP.
"""


INVENTORY_PROMPT = """

Is a real INVENTORY/EQUIPMENT screen open?

Return ONLY one line.

Format:

YES|0.95

or

NO|0.95

YES ONLY when the screen is a player's inventory/equipment/items
management screen.

IMPORTANT:
A merchant SHOP is NOT inventory.

If the screen contains product rows, product prices, currency,
BUY, SELL, PURCHASE, merchant/shop controls, or a merchant
product list, return NO.

A shop may also show many item icons. That still does NOT make it
inventory.

A normal gameplay HUD, battle screen, town/home screen, menu,
or merchant shop is NOT inventory.

The number must be a confidence value between 0 and 1.

Do not add explanations.
"""


TARGET_PROMPT = """
Find one safe clickable UI target.

Use image coordinates:

x:0-719
y:0-323


Return exactly:

TARGET|x|y|confidence|reason


If no target:

NONE


Never invent coordinates.
"""


INVENTORY_TARGET_PROMPT = """
The inventory screen is confirmed.

Find a CLOSE/BACK/EXIT button.

Return:

TARGET|x|y|confidence|reason


Only visible controls.

Do not choose items,
equipment slots,
icons,
or decoration.


If unavailable:

NONE
"""


SHOP_TARGET_PROMPT = """
The SHOP screen is confirmed.

Find one safe navigation control that exits the shop
or returns to normal gameplay.

Prefer CLOSE, BACK, EXIT, or a clearly labeled return control.

Do NOT select products.
Do NOT press BUY, SELL, PURCHASE, or item rows.

Return exactly:

TARGET|x|y|confidence|reason

If no safe navigation control is visible:

NONE
"""


AUTO_STATE_PROMPT = """
Determine the state of the game's AUTO / 자동사냥 control.

Return exactly ONE line.

If AUTO is visibly active:
ON|confidence

If AUTO is visibly inactive and the AUTO button itself is clearly visible:
OFF|x|y|confidence|reason

If the state cannot be determined safely:
UNKNOWN

Rules:
- Do not guess.
- ON means the game is already in automatic hunting mode.
- OFF means AUTO must be tapped to start automatic hunting.
- x,y must be the center of the AUTO button in the 720x324 image.
- Do not use minimap, potion, inventory, chat, skill buttons, monsters, character, or decorative icons.
- Confidence must be 0 to 1.
"""

AUTO_STATE_RE = re.compile(
    r"^ON\s*[|:]\s*(100(?:\.\d+)?|[0-9]{1,2}(?:\.\d+)?)\s*$",
    re.I,
)

AUTO_OFF_RE = re.compile(
    r"^OFF\\s*[|:]\\s*(\\d+)\\s*[|:]\\s*(\\d+)\\s*[|:]\\s*"
    r"(100(?:\\.\\d+)?|[0-9]{1,2}(?:\\.\\d+)?)\\s*[|:]\\s*(.*)$",
    re.I,
)


def detect_auto_state(frame):
    try:
        raw = ollama_text_with_image(
            AUTO_STATE_PROMPT,
            frame,
        )
    except Exception as e:
        print("[AUTO ERROR]", repr(e))
        return None

    raw = raw.strip()
    print("[AUTO RAW]", raw)

    m = AUTO_STATE_RE.match(raw)
    if m:
        confidence = float(m.group(1))
        if confidence > 1.0:
            confidence /= 100.0

        if confidence >= AUTO_MIN_CONFIDENCE:
            return {
                "state": "ON",
                "confidence": confidence,
            }

        print("[AUTO UNKNOWN] ON confidence too low")
        return None

    m = AUTO_OFF_RE.match(raw)
    if m:
        x = int(m.group(1))
        y = int(m.group(2))
        confidence = float(m.group(3))
        if confidence > 1.0:
            confidence /= 100.0

        if (
            0 <= x < VISION_WIDTH
            and 0 <= y < VISION_HEIGHT
            and confidence >= AUTO_MIN_CONFIDENCE
        ):
            ax, ay = vision_to_adb(x, y)
            return {
                "state": "OFF",
                "vision_x": x,
                "vision_y": y,
                "adb_x": ax,
                "adb_y": ay,
                "confidence": confidence,
                "reason": m.group(4).strip(),
            }

        print("[AUTO UNKNOWN] OFF result failed safety validation")
        return None

    print("[AUTO UNKNOWN] invalid response")
    return None


def is_vlm_failure(raw):
    """Return True only when the VLM response is genuinely unusable."""
    if raw is None:
        return True

    text = str(raw).strip()

    if not text:
        return True

    lowered = text.lower()

    # Known transport/model failure strings must never become a state.
    failure_tokens = (
        "error",
        "exception",
        "timeout",
        "failed",
        "failure",
    )

    if any(token in lowered for token in failure_tokens):
        return True

    # Use the same parser that owns state syntax and confidence handling.
    # This keeps validation consistent with parse_state(), including plain
    # responses such as "BATTLE" and confidence responses such as
    # "BATTLE|90%".
    state, confidence = parse_state(text)

    if state in VALID_STATES:
        return False

    # UNKNOWN is a valid, intentionally uncertain VLM result.
    if re.fullmatch(r"\s*UNKNOWN\s*", text, re.I):
        return False

    return True


def ollama_text_with_image(prompt, frame):
    result = ollama_chat(
        prompt,
        image=frame,
    )
    return (
        result
        .get("message", {})
        .get("content", "")
        .strip()
    )


def detect_state(frame):
    """Classify the current main screen with the state VLM."""
    try:
        raw = ollama_text_with_image(
            STATE_PROMPT,
            frame,
        )
        print("[STATE RAW]", raw)
        return raw
    except Exception as e:
        print("[STATE ERROR]", repr(e))
        return ""


STATE_RE = re.compile(
    r"^\s*(NORMAL|BATTLE|MENU|INVENTORY|SHOP|HOME|UNKNOWN)"
    r"\s*(?:[|:]\s*)?"
    r"(100(?:\.\d+)?|[0-9]{1,2}(?:\.\d+)?)?"
    r"\s*%?\s*$",
    re.I,
)


def parse_state(raw):
    """Parse the state VLM response without ever inventing a state."""
    if raw is None:
        return None, 0.0

    text = str(raw).strip()

    if not text:
        return None, 0.0

    match = STATE_RE.match(text)

    if not match:
        return None, 0.0

    state = match.group(1).upper()
    confidence_raw = match.group(2)

    if confidence_raw is None:
        # The model's STATE_PROMPT allows a confidence value, but tolerate
        # a plain state response conservatively rather than guessing high.
        confidence = 0.55 if state != "UNKNOWN" else 0.0
    else:
        confidence = float(confidence_raw)
        if confidence > 1.0:
            confidence /= 100.0

    if state == "UNKNOWN":
        return None, 0.0

    if not 0.0 <= confidence <= 1.0:
        return None, 0.0

    return state, confidence


# INVENTORY VERIFY
# ============================================================


def verify_inventory(frame):


    try:


        # Build a visual probe that preserves the full screen while
        # enlarging the tiny upper-left UI-title region. This addresses the
        # exact failure mode where the full VLM sees an item grid and calls
        # it SHOP, while the actual inventory title is too small to notice.
        probe = frame
        try:
            h, w = frame.shape[:2]
            title = frame[0:min(h, 64), 0:min(w, 180)]
            title = cv2.resize(
                title,
                None,
                fx=4.0,
                fy=4.0,
                interpolation=cv2.INTER_CUBIC,
            )
            canvas_h = max(h, title.shape[0])
            canvas_w = w + title.shape[1] + 8
            probe = cv2.copyMakeBorder(
                frame,
                0,
                canvas_h - h,
                0,
                title.shape[1] + 8,
                cv2.BORDER_CONSTANT,
                value=(0, 0, 0),
            )
            probe[0:title.shape[0], w + 8:w + 8 + title.shape[1]] = title
        except Exception as e:
            print("[INV PROBE ERROR]", e)

        result = ollama_chat(
            INVENTORY_PROMPT,
            image=probe
        )


        text = (
            result
            .get("message", {})
            .get("content","")
        )


        print(
            "[INV RAW]",
            text
        )

        if is_vlm_failure(text):
            print("[INV FAILURE] invalid/repeated model output")
            restart_vlm_after_repeated_failure()
            return None



        m = YES_NO_RE.search(
            text
        )


        if not m:

            return None



        yes = (
            m.group(1)
            .upper()
            ==
            "YES"
        )


        conf = float(
            m.group(2)
        )


        if conf > 1:

            conf /= 100



        accepted = (

            yes

            and

            conf >= INVENTORY_MIN_CONFIDENCE

        )


        print(
            "[INV]",
            accepted,
            conf
        )


        return accepted



    except Exception as e:


        print(
            "[INV ERROR]",
            e
        )


        return False




# ============================================================
# HOME VERIFY
# ============================================================


HOME_VERIFY_PROMPT = """
Is the player currently in the actual town/home/base area?

Return ONLY one line.

Format:

YES|0.95

or

NO|0.95

YES only when the overall screenshot visually matches an actual
town/home/base area.

IMPORTANT:
The upper-right minimap may display "(안전)".
"(안전)" means the current area is PK-disabled/a safe zone.
It does NOT mean the player is necessarily in a town.
Many towns normally show "(안전)", but other non-town safe areas
can also show it.

Therefore "(안전)" is supporting evidence only.
Do NOT return YES from "(안전)" alone.

Use additional visual context such as town buildings, streets,
NPCs, shops/buildings, or a clearly recognizable town/base scene.

NO when:
- active battle/combat is visible
- enemies/target combat UI are present
- the player is in the hunting/field area
- inventory/equipment is open
- a merchant shop is open
- a general menu is open
- only "(안전)" is visible without clear town/base context

A town screen is NOT the same as a generic safe zone.

If uncertain, return NO.

Do not add explanations.
"""



def verify_home(frame):

    try:
        result = ollama_chat(
            HOME_VERIFY_PROMPT,
            image=frame
        )

        text = (
            result
            .get("message", {})
            .get("content", "")
        )

        print("[HOME RAW]", text)

        m = YES_NO_RE.search(text)

        if not m:
            return None

        yes = m.group(1).upper() == "YES"

        conf = float(m.group(2))

        if conf > 1:
            conf /= 100

        accepted = (
            yes
            and conf >= INVENTORY_MIN_CONFIDENCE
        )

        print("[HOME]", accepted, conf)

        return accepted

    except Exception as e:
        print("[HOME ERROR]", e)
        return False


# ============================================================
# SHOP VERIFY
# ============================================================


SHOP_VERIFY_PROMPT = """

Is a real merchant SHOP interface open?

Return ONLY one line.

Format:

YES|0.95

or

NO|0.95

YES when there is a merchant shop/product interface with one or
more of these visible:
- product/item rows offered for sale
- prices or currency beside products
- BUY/SELL/PURCHASE controls
- merchant/shop title or shop-specific controls

NO for:
- player inventory/equipment
- battle
- ordinary gameplay
- town/home
- general menu

If uncertain, return NO.

Do not add explanations.
"""


def verify_shop(frame):

    try:
        result = ollama_chat(
            SHOP_VERIFY_PROMPT,
            image=frame
        )

        text = (
            result
            .get("message", {})
            .get("content", "")
        )

        print("[SHOP RAW]", text)

        if is_vlm_failure(text):
            print("[SHOP FAILURE] invalid/repeated model output")
            restart_vlm_after_repeated_failure()
            return None

        m = YES_NO_RE.search(text)

        if not m:
            return False

        yes = m.group(1).upper() == "YES"

        conf = float(m.group(2))

        if conf > 1:
            conf /= 100

        accepted = (
            yes
            and conf >= INVENTORY_MIN_CONFIDENCE
        )

        print("[SHOP]", accepted, conf)

        return accepted

    except Exception as e:
        print("[SHOP ERROR]", e)
        return False


# ============================================================


def detect_target(
    frame,
    inventory=False,
    state=None
):

    try:

        if inventory:
            prompt = INVENTORY_TARGET_PROMPT
        elif state == "SHOP":
            prompt = SHOP_TARGET_PROMPT
        elif state == "HOME":
            prompt = HOME_TARGET_PROMPT
        else:
            prompt = TARGET_PROMPT

        result = ollama_chat(
            prompt,
            image=frame
        )

        raw = (
            result
            .get("message", {})
            .get("content", "")
        )

        print(
            "[TARGET RAW]",
            raw
        )

        target = parse_target(raw)

        if target:
            print(
                "[TARGET FOUND]",
                target
            )
        else:
            print(
                "[TARGET NONE]"
            )

        return target

    except Exception as e:

        print(
            "[TARGET ERROR]",
            e
        )

        return None


# ============================================================
# STATE STABILIZER
# ============================================================


class StateStabilizer:


    def __init__(self):

        self.confirmed=None

        self.previous = None

        self.count = 0




    def update(
        self,
        state,
        confidence
    ):


        if confidence < MIN_CONFIDENCE:

            return (
                self.confirmed,
                False
            )



        if state == self.previous:

            self.count += 1


        else:

            self.previous = state

            self.count = 1




        if self.count >= STABLE_COUNT_REQUIRED:


            changed = (
                self.confirmed != state
            )


            self.confirmed = state


            self.count = 0



            if changed:

                print(
                    "[STATE CHANGE]",
                    self.confirmed
                )



            return (
                self.confirmed,
                changed
            )



        return (
            self.confirmed,
            False
        )


# ============================================================
# ACTION EXECUTOR
# ============================================================


class ActionExecutor:


    def tap(self, x, y):


        if DRY_RUN:

            print(
                f"[DRY RUN] tap({x},{y})"
            )

            return False



        result = adb_run(
            [
                "shell",
                "input",
                "tap",
                str(x),
                str(y)
            ],
            timeout=5
        )


        if (
            result is None
            or result.returncode != 0
        ):

            print(
                "[TAP ERROR]"
            )

            return False



        print(
            "[TAP]",
            x,
            y
        )


        return True




    def back(self):


        if DRY_RUN:

            print(
                "[DRY RUN] back"
            )

            return False



        result = adb_run(
            [
                "shell",
                "input",
                "keyevent",
                "4"
            ],
            timeout=5
        )


        if (
            result is None
            or result.returncode != 0
        ):

            print(
                "[BACK ERROR]"
            )

            return False



        print(
            "[BACK]"
        )


        return True





    def execute(
        self,
        state,
        target
    ):


        if target is None:

            print(
                "[ACTION] no target"
            )

            return



        print(
            "[ACTION]",
            state,
            target
        )


        if target.get("action") == "back":

            self.back()

            return



        if (
            "adb_x" in target
            and "adb_y" in target
        ):

            self.tap(
                target["adb_x"],
                target["adb_y"]
            )


# ============================================================
# ROUTER
# ============================================================


def route_action(state):


    if state == "INVENTORY":

        return "CLOSE_INVENTORY"



    if state == "SHOP":

        return "SHOP"



    if state == "HOME":

        return "HOME"



    return "NONE"





# ============================================================
# VLM WORKER
# ============================================================


class VLMWorker:


    def __init__(
        self,
        capture
    ):

        self.capture = capture

        self.stop_event = threading.Event()

        self.thread = None

        self.last_frame_id = 0

        self.last_home_verify_time = 0.0
        self.cached_home_result = None
        self.last_state_vlm_time = 0.0
        self.cached_state_raw = None
        self.vlm_failure_streak = 0
        self.inventory_title_override_count = 0
        self.last_ocr_time = 0.0
        self.cached_ocr_results = []
        self.ocr_available_logged = False
        self.last_target_diagnostic_time = 0.0
        self.last_auto_action_time = 0.0
        self.auto_off_confirm_count = 0

        self.stabilizer = StateStabilizer()

        self.executor = ActionExecutor()




    def start(self):

        self.thread = threading.Thread(

            target=self.run,

            daemon=True

        )

        self.thread.start()





    def run(self):


        print(
            "[WORKER] started"
        )



        while not self.stop_event.is_set():


            frame, frame_id = (
                self.capture.get_snapshot()
            )



            if frame is None:

                time.sleep(0.1)

                continue



            if frame_id == self.last_frame_id:

                time.sleep(0.1)

                continue



            self.last_frame_id = frame_id



            self.process(
                frame
            )



            time.sleep(
                VISION_INTERVAL
            )





    def process(
        self,
        frame
    ):


        now = time.time()

        # --------------------------------------------------------
        # OCR FIRST: explicit screen text is the cheap primary path.
        # VLM is used only when OCR cannot identify the state.
        # --------------------------------------------------------
        if OCR_ENABLED and OCR_AVAILABLE:
            if now - self.last_ocr_time >= OCR_INTERVAL:
                self.cached_ocr_results = ocr_screen(frame)
                self.last_ocr_time = now
                # Keep OCR output quiet unless it produces a real state hint.
            ocr_state, ocr_confidence, ocr_hits = classify_ocr_state(
                self.cached_ocr_results
            )

            title_state, title_confidence = detect_ui_title(frame)
            if title_state is not None:
                ocr_state = title_state
                ocr_confidence = title_confidence
                ocr_hits = [(title_state, "UI_TITLE")]
        else:
            ocr_state, ocr_confidence, ocr_hits = None, 0.0, []
            if not self.ocr_available_logged:
                print("[OCR] unavailable - VLM fallback remains active")
                self.ocr_available_logged = True

        if ocr_state is not None:
            state = ocr_state
            confidence = ocr_confidence
            print("[STATE OCR]", state, confidence, ocr_hits)
            raw = None

            # UI title OCR is deterministic evidence for inventory/shop.
            # SHOP is a deterministic OCR state: do not call the VLM inventory
            # verifier and do not return before the action router.
            if ocr_hits and any(hit[1] == "UI_TITLE" for hit in ocr_hits):
                self.cached_state_raw = None
                self.cached_state = state
                self.cached_state_confidence = confidence
                self.cached_state_time = now
                self.last_confirmed_state = state
                self.last_confirmed_confidence = confidence

                if state == "SHOP":
                    changed = self.stabilizer.confirmed != "SHOP"
                    self.stabilizer.confirmed = "SHOP"
                    self.stabilizer.previous = "SHOP"
                    self.stabilizer.count = 0
                    self.inventory_title_override_count = 0

                    print("[UI TITLE CONFIRMED] SHOP")

                    if changed:
                        print("[STATE CHANGE] SHOP")
                        print("[CONFIRMED] SHOP")
                        if ENABLE_SHOP_ACTION:
                            print("[ACTION] SHOP -> BACK")
                            self.executor.back()
                    else:
                        print("[CONFIRMED] SHOP")

                    return

                print("[UI TITLE CONFIRMED]", state)
                return state, confidence
        else:
            if (
                self.cached_state_raw is not None
                and now - self.last_state_vlm_time < STATE_VLM_INTERVAL
            ):
                raw = self.cached_state_raw
                print("[STATE CACHE]", raw)
            else:
                print("[STATE VLM FALLBACK] OCR found no explicit state text")
                raw = detect_state(frame)
                self.cached_state_raw = raw
                self.last_state_vlm_time = now

            if is_vlm_failure(raw):
                print(
                    "[VLM FAILURE]",
                    "invalid/repeated model output"
                )

                self.vlm_failure_streak += 1

                if self.vlm_failure_streak >= VLM_FAILURE_RESTART_THRESHOLD:
                    restart_vlm_after_repeated_failure()
                    self.vlm_failure_streak = 0

                # A broken VLM response must not erase a state that was
                # already confirmed by deterministic OCR / verification.
                # Keep the last confirmed state while the model recovers.
                if self.stabilizer.confirmed is not None:
                    print(
                        "[VLM FAILURE] retaining confirmed state",
                        self.stabilizer.confirmed
                    )
                    self.cached_state_raw = None
                    self.last_state_vlm_time = (
                        time.time() + VLM_FAILURE_COOLDOWN
                    )
                    print(
                        "[CONFIRMED]",
                        self.stabilizer.confirmed
                    )
                    return

                self.stabilizer.previous = None
                self.stabilizer.count = 0
                print("[CONFIRMED] None")
                self.cached_state_raw = None
                self.last_state_vlm_time = (
                    time.time() + VLM_FAILURE_COOLDOWN
                )
                return

            self.vlm_failure_streak = 0
            state, confidence = parse_state(raw)

            # Once SHOP has been explicitly confirmed, a generic VLM
            # INVENTORY classification must not replace it. The inventory
            # screen must provide its own explicit UI-title evidence before
            # the confirmed SHOP state can change.
            if (
                state == "INVENTORY"
                and self.stabilizer.confirmed == "SHOP"
                and not any(
                    hit[1] == "UI_TITLE" and hit[0] == "INVENTORY"
                    for hit in ocr_hits
                )
            ):
                print("[STATE RETAINED] SHOP -> ignoring generic INVENTORY VLM")
                state = "SHOP"
                confidence = 0.99


        if (
            state == "NORMAL"
            and self.stabilizer.confirmed == "HOME"
            and now - self.last_home_verify_time >= HOME_VERIFY_INTERVAL
        ):
            self.cached_home_result = None


        inventory_verified = False
        shop_verified = False
        home_verified = False

        # Game-native AUTO mode.
        # NEVER tap when AUTO is already ON or when the VLM result is uncertain.
        if (
            ENABLE_AUTO_ACTION
            and state in ("NORMAL", "BATTLE")
            and not TARGET_DIAGNOSTIC_ONLY
            and now - self.last_auto_action_time >= AUTO_CHECK_INTERVAL
        ):
            self.last_auto_action_time = now
            print("[AUTO TEST] checking AUTO ON/OFF state")

            auto_state = detect_auto_state(frame)

            if auto_state is None:
                self.auto_off_confirm_count = 0
                print("[AUTO ACTION] no safe state -> NO TAP")

            elif auto_state["state"] == "ON":
                self.auto_off_confirm_count = 0
                print(
                    f"[AUTO STATE] ON confidence={auto_state['confidence']:.2f}"
                )
                print("[AUTO ACTION] already ON -> NO TAP")

            elif auto_state["state"] == "OFF":
                self.auto_off_confirm_count += 1
                print(
                    f"[AUTO STATE] OFF confidence={auto_state['confidence']:.2f} "
                    f"confirm={self.auto_off_confirm_count}/{AUTO_OFF_CONFIRM_REQUIRED}"
                )

                if self.auto_off_confirm_count >= AUTO_OFF_CONFIRM_REQUIRED:
                    print(
                        f"[AUTO ACTION] OFF confirmed -> tap "
                        f"vision=({auto_state['vision_x']},{auto_state['vision_y']}) "
                        f"adb=({auto_state['adb_x']},{auto_state['adb_y']})"
                    )
                    self.executor.tap(
                        auto_state["adb_x"],
                        auto_state["adb_y"]
                    )
                    self.auto_off_confirm_count = 0
                else:
                    print("[AUTO ACTION] waiting for second OFF confirmation")


        # Safe live target test. Detection is allowed, tapping is not.
        if (
            TARGET_DIAGNOSTIC_ONLY
            and state in ("NORMAL", "BATTLE")
            and now - self.last_target_diagnostic_time >= TARGET_DIAGNOSTIC_INTERVAL
        ):
            self.last_target_diagnostic_time = now
            print("[TARGET TEST] detecting target - NO TAP")
            target = detect_target(
                frame,
                inventory=False,
                state=state
            )
            if target is None:
                print("[TARGET TEST] no target")
            else:
                print("[TARGET TEST] VISION -> ADB")
                print(
                    f"[TARGET TEST] vision=({target['vision_x']},{target['vision_y']}) "
                    f"adb=({target['adb_x']},{target['adb_y']}) "
                    f"confidence={target['confidence']:.2f} "
                    f"reason={target['reason']}"
                )
            return


        if state == "HOME":

            # Verify a new HOME candidate once. Do not repeatedly send
            # the same screenshot to the vision model.
            if self.stabilizer.confirmed != "HOME":
                home_verified = verify_home(frame)
                self.last_home_verify_time = time.time()
                self.cached_home_result = home_verified

                if not home_verified:
                    print("[HOME rejected]")
                    state = "NORMAL"
                    confidence = 0.8


        if state == "BATTLE":

            # Town/base scenes are often classified as BATTLE.
            # Re-check HOME only periodically and use the cached result
            # between checks to reduce VLM load.
            now = time.time()

            if (
                self.cached_home_result is None
                or now - self.last_home_verify_time >= HOME_VERIFY_INTERVAL
            ):
                home_verified = verify_home(frame)
                self.last_home_verify_time = now
                self.cached_home_result = home_verified
            else:
                home_verified = bool(self.cached_home_result)
                print("[HOME CACHE]", home_verified)

            if home_verified:
                print(
                    "[STATE OVERRIDE] BATTLE -> HOME"
                )
                state = "HOME"
                confidence = 0.95

            elif self.stabilizer.confirmed == "HOME":
                # Explicit HOME=NO must immediately release stale HOME.
                self.stabilizer.confirmed = None
                self.stabilizer.previous = None
                self.stabilizer.count = 0
                print("[STATE RESET] HOME -> BATTLE candidate")

        # Once a verified screen is no longer visually present,
        # the normal stabilizer is allowed to replace the old
        # confirmed SHOP/HOME state with the new state.
        if (
            self.stabilizer.confirmed in ("SHOP", "HOME")
            and state not in ("SHOP", "HOME", "INVENTORY")
            and not shop_verified
            and not home_verified
            and not inventory_verified
        ):

            self.stabilizer.previous = None
            self.stabilizer.count = 0


        if state == "INVENTORY":

            # Resolve the ambiguous item-grid case with both independent
            # verifiers. Inventory must not override an explicit SHOP title.
            # SHOP must not override a positively verified inventory screen.
            inventory_result = verify_inventory(frame)
            inventory_verified = bool(inventory_result)

            if inventory_result is None:

                # VLM failure is not a real NO. Never let a second VLM
                # request manufacture a contradictory state while the model
                # is recovering.
                if self.stabilizer.confirmed == "INVENTORY" or getattr(self, "last_confirmed_state", None) == "INVENTORY":
                    state = "INVENTORY"
                    confidence = 0.95
                else:
                    state = "NORMAL"
                    confidence = 0.5

            elif inventory_verified:

                # An explicit OCR SHOP title is stronger than the generic
                # inventory VLM response. Keep SHOP in that case.
                if state == "INVENTORY" and any(
                    hit[1] == "UI_TITLE" and hit[0] == "SHOP"
                    for hit in ocr_hits
                ):
                    shop_verified = verify_shop(frame)

                    if shop_verified:
                        print("[STATE OVERRIDE] INVENTORY -> SHOP")
                        state = "SHOP"
                        confidence = 0.99
                    else:
                        state = "INVENTORY"
                        confidence = 0.95
                else:
                    state = "INVENTORY"
                    confidence = 0.95

            else:

                if verify_shop(frame):
                    shop_verified = True
                    print("[STATE OVERRIDE] INVENTORY -> SHOP")
                    state = "SHOP"
                    confidence = 0.95
                else:
                    print("[INVENTORY rejected]")
                    state = "NORMAL"
                    confidence = 0.8



        if state == "SHOP":

            # An explicit SHOP title is deterministic evidence. Do not let
            # the generic inventory verifier overturn it. Only use the
            # inventory verifier when OCR did not explicitly identify SHOP.
            explicit_shop_title = any(
                hit[1] == "UI_TITLE" and hit[0] == "SHOP"
                for hit in ocr_hits
            )

            if not explicit_shop_title:
                inventory_candidate = verify_inventory(frame)

                if inventory_candidate is True:
                    inventory_verified = True
                    print("[STATE OVERRIDE] SHOP -> INVENTORY")
                    state = "INVENTORY"
                    confidence = 0.95

        if state == "SHOP":

            confirmed = "SHOP"

            changed = (
                self.stabilizer.confirmed
                !=
                "SHOP"
            )

            self.stabilizer.confirmed = "SHOP"
            self.stabilizer.previous = "SHOP"
            self.stabilizer.count = 0

            if changed:
                print(
                    "[STATE CHANGE]",
                    "SHOP"
                )

        elif state == "HOME" and home_verified:

            confirmed = "HOME"

            changed = (
                self.stabilizer.confirmed
                !=
                "HOME"
            )

            self.stabilizer.confirmed = "HOME"
            self.stabilizer.previous = "HOME"
            self.stabilizer.count = 0

            if changed:
                print(
                    "[STATE CHANGE]",
                    "HOME"
                )

        elif inventory_verified:

            confirmed = "INVENTORY"

            changed = (
                self.stabilizer.confirmed
                !=
                "INVENTORY"
            )

            self.stabilizer.confirmed = "INVENTORY"
            self.stabilizer.previous = "INVENTORY"
            self.stabilizer.count = 0

            if changed:

                print(
                    "[STATE CHANGE]",
                    "INVENTORY"
                )

        else:

            confirmed, changed = (
                self.stabilizer.update(
                    state,
                    confidence
                )
            )



        print(
            "[CONFIRMED]",
            confirmed
        )



        if not changed:

            return



        action = route_action(
            confirmed
        )



        if action == "NONE":

            return



        if (
            confirmed == "INVENTORY"
            and ENABLE_INVENTORY_ACTION
        ):

            print("[ACTION] INVENTORY -> BACK")

            self.executor.back()

            return

        if (
            confirmed == "SHOP"
            and ENABLE_SHOP_ACTION
        ):

            print("[ACTION] SHOP -> BACK")

            self.executor.back()

            return

        if (
            confirmed == "SHOP"
            and not ENABLE_SHOP_ACTION
        ):

            print("[ACTION] SHOP detected - no action")

            return

        if (
            confirmed == "HOME"
            and not ENABLE_HOME_ACTION
        ):

            print("[ACTION] HOME detected - no action")

            return



        if ENABLE_TARGET_DETECTION:
            target = detect_target(
                frame,
                inventory=(
                    confirmed
                    ==
                    "INVENTORY"
                ),
                state=confirmed
            )

            self.executor.execute(
                confirmed,
                target
            )





    def stop(self):


        self.stop_event.set()



        if self.thread:

            self.thread.join(
                timeout=3
            )






# ============================================================
# SINGLE IMAGE OCR TEST
# ============================================================


def run_ocr_test(capture):
    print("=" * 60)
    print("SOL ENCHANT - SINGLE IMAGE OCR TEST")
    print("=" * 60)

    if not OCR_AVAILABLE:
        print("[OCR TEST] pytesseract is not installed")
        print("[OCR TEST] Install: python -m pip install pytesseract")
        return False

    try:
        print("[OCR TEST] Tesseract:", pytesseract.get_tesseract_version())
    except Exception as e:
        print("[OCR TEST] Tesseract executable unavailable:", repr(e))
        return False

    deadline = time.time() + 15
    frame = None
    frame_id = 0

    while time.time() < deadline:
        frame, frame_id = capture.get_snapshot()
        if frame is not None:
            break
        time.sleep(0.1)

    if frame is None:
        print("[OCR TEST ERROR] no video frame received")
        return False

    results = ocr_screen(frame)
    state, confidence, hits = classify_ocr_state(results)

    print("[OCR TEST] frame_id=", frame_id)
    print("[OCR TEST TEXT]", " | ".join(x["text"] for x in results) or "<none>")
    print("[OCR TEST STATE]", state, confidence, hits)
    return True


# ============================================================
# SINGLE IMAGE VLM TEST
# ============================================================


def run_single_vlm_test(capture):


    print("=" * 60)

    print(
        "SOL ENCHANT - SINGLE IMAGE VLM TEST"
    )

    print("=" * 60)

    print(
        "ONE Android frame -> ONE qwen2.5vl:3b image request"
    )

    print()


    deadline = time.time() + 15

    frame = None

    frame_id = 0


    while time.time() < deadline:


        frame, frame_id = (
            capture.get_snapshot()
        )


        if frame is not None:

            break


        time.sleep(
            0.1
        )


    if frame is None:

        print(
            "[TEST ERROR] no video frame received"
        )

        return False


    print(
        "[TEST] frame received:",
        frame.shape,
        "frame_id=",
        frame_id
    )


    print(
        "[TEST] sending ONE VLM image request..."
    )


    try:


        raw = detect_state(
            frame
        )


        print(
            "[TEST STATE RAW]",
            raw
        )


        if is_vlm_failure(raw):

            print(
                "[TEST RESULT] VLM failure:",
                "repeated/invalid output"
            )

            return False


        state, confidence = parse_state(
            raw
        )


        print(
            "[TEST RESULT]",
            "state=",
            state,
            "confidence=",
            confidence
        )


        return True


    except Exception as e:


        print(
            "[TEST ERROR]",
            repr(e)
        )


        return False




# ============================================================
# MAIN
# ============================================================


def main():


    # Accept the diagnostic flag anywhere in the command line.
    # This avoids depending on argv[1] when PowerShell/launchers
    # add or reorder arguments.
    single_vlm_test = "--single-vlm-test" in sys.argv
    single_ocr_test = "--ocr-test" in sys.argv
    target_test = "--target-test" in sys.argv
    auto_test = "--auto-test" in sys.argv

    global TARGET_DIAGNOSTIC_ONLY
    TARGET_DIAGNOSTIC_ONLY = target_test

    if single_ocr_test:
        print("[MODE] ocr-test")
    elif single_vlm_test:
        print("[MODE] single-vlm-test")
    elif target_test:
        print("[MODE] target-test (NO TAP)")
    elif auto_test:
        print("[MODE] auto-test")
    else:
        print("[MODE] realtime")


    print("=" * 60)


    if single_ocr_test:
        print("SOL ENCHANT SINGLE IMAGE OCR TEST")
    elif single_vlm_test:
        print(
            "SOL ENCHANT SINGLE IMAGE VLM TEST"
        )
    else:

        print(
            "SOL ENCHANT REAL TIME VISION"
        )

    print("=" * 60)



    print(
        "Q = EXIT"
    )



    check_adb()



    print_coordinate_bridge()



    capture = VisionCapture()


    worker = VLMWorker(
        capture
    )



    try:


        capture.start()


        if single_ocr_test:
            run_ocr_test(capture)
            return

        if single_vlm_test:
            run_single_vlm_test(capture)
            return

        if auto_test:
            print("[AUTO TEST] start realtime AUTO-button detection")
            worker.start()
        else:
            worker.start()



        while True:


            if msvcrt.kbhit():


                key = msvcrt.getwch()


                if key.lower() == "q":

                    print(
                        "[MAIN] exit"
                    )

                    break



            time.sleep(
                0.05
            )



    except KeyboardInterrupt:


        print(
            "[MAIN] interrupt"
        )



    except Exception as e:


        print(
            "[MAIN ERROR]",
            repr(e)
        )



    finally:


        worker.stop()

        capture.stop()



        print(
            "[DONE]"
        )





if __name__ == "__main__":

    main()