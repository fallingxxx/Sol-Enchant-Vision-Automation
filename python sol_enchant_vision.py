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

            f"turn_screen_off=false "

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


# AUTO button is a fixed game UI control near the right-center edge.
# AUTO ON/OFF is NOT determined from a single VLM frame.
# The game shows a rotating/glowing ring when AUTO is active and
# the same ring becomes stationary when AUTO is inactive.
# AUTO control is the small circular AUTO button above/right of the
# main joystick. The latest auto_button_grid.jpg diagnostic showed the
# previous candidate (628,176) was too far right, so the fixed fallback
# target was moved to the visible button center at approximately (592,164).
AUTO_ROI_X1 = 555
AUTO_ROI_Y1 = 125
AUTO_ROI_X2 = 625
AUTO_ROI_Y2 = 200

# Safe tap point confirmed from the latest auto_button_grid.jpg diagnostic.
# The previous candidate (628,176) was visibly too far to the right.
# The corrected center is approximately (592,176) in the 720x324 vision frame.
AUTO_TAP_VISION_X = 592
AUTO_TAP_VISION_Y = 176

AUTO_MOTION_THRESHOLD = 3.0
AUTO_MOTION_MIN_ACTIVE_PIXELS = 0.02
AUTO_MOTION_HISTORY_REQUIRED = 2
AUTO_OFF_CONFIRM_REQUIRED = 2
AUTO_ON_CONFIRM_REQUIRED = 2
AUTO_ON_VERIFY_TIMEOUT = 5.0
AUTO_CHECK_INTERVAL = 0.5

# Explicit real-device AUTO tap test.
# This mode performs exactly one real ADB tap at the fixed AUTO control
# coordinate after the video stream is confirmed alive.
AUTO_TAP_TEST_TIMEOUT = 10.0

def measure_auto_motion(frame):
    """
    Measure temporal visual change in the fixed AUTO control region.

    This is deliberately independent of VLM. A single frame cannot tell
    whether the ring is rotating; consecutive frames are required.
    """
    try:
        roi = frame[
            AUTO_ROI_Y1:AUTO_ROI_Y2,
            AUTO_ROI_X1:AUTO_ROI_X2
        ]

        if roi is None or roi.size == 0:
            return None

        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)

        return gray

    except Exception as e:
        print("[AUTO CV ERROR]", repr(e))
        return None


def detect_auto_state(frame):
    """
    Detect AUTO state from temporal motion of the ring.

    ON  = the ring is visibly changing/rotating across frames.
    OFF = the ring is stationary across frames.
    UNKNOWN = insufficient temporal evidence.

    No VLM request is made here.
    """
    current = measure_auto_motion(frame)

    if current is None:
        return None

    history = getattr(detect_auto_state, "_history", [])
    history.append(current)

    if len(history) > AUTO_MOTION_HISTORY_REQUIRED:
        history.pop(0)

    detect_auto_state._history = history

    if len(history) < AUTO_MOTION_HISTORY_REQUIRED:
        print(            f"[AUTO CV] collecting frames "
            f"{len(history)}/{AUTO_MOTION_HISTORY_REQUIRED}"
        )
        return None

    differences = []

    for previous, latest in zip(history[:-1], history[1:]):
        diff = cv2.absdiff(previous, latest)

        # Ignore tiny compression/noise changes.
        active_pixels = float(
            (diff >= 8).mean()
        )

        mean_change = float(
            diff.mean()
        )

        differences.append(
            (mean_change, active_pixels)
        )

    mean_change = sum(x[0] for x in differences) / len(differences)
    active_ratio = sum(x[1] for x in differences) / len(differences)

    moving = (
        mean_change >= AUTO_MOTION_THRESHOLD
        and active_ratio >= AUTO_MOTION_MIN_ACTIVE_PIXELS
    )

    confidence = min(
        0.99,
        max(
            0.0,
            (
                mean_change / max(AUTO_MOTION_THRESHOLD * 3.0, 1.0)
            )
        )
    )

    if moving:
        confidence = max(confidence, 0.85)

        print(
            f"[AUTO CV] MOVING mean={mean_change:.2f} "
            f"active={active_ratio:.3f} confidence={confidence:.2f}"
        )

        return {
            "state": "ON",
            "confidence": confidence,
        }

    confidence = max(
        0.85,
        1.0 - min(
            1.0,
            mean_change / max(AUTO_MOTION_THRESHOLD * 2.0, 1.0)
        )
    )

    ax, ay = vision_to_adb(
        AUTO_TAP_VISION_X,
        AUTO_TAP_VISION_Y,
    )

    print(
        f"[AUTO CV] STATIONARY mean={mean_change:.2f} "
        f"active={active_ratio:.3f} confidence={confidence:.2f}"
    )

    return {
        "state": "OFF",
        "vision_x": AUTO_TAP_VISION_X,
        "vision_y": AUTO_TAP_VISION_Y,
        "adb_x": ax,
        "adb_y": ay,
        "confidence": confidence,
    }

def find_auto_text_target(frame):
    """Locate the visible AUTO label with OCR and return a vision-space center."""
    if not OCR_ENABLED or not OCR_AVAILABLE or frame is None:
        return None

    try:
        x1, y1, x2, y2 = 560, 70, 720, 250
        roi = frame[y1:y2, x1:x2]
        if roi is None or roi.size == 0:
            return None

        scale = 4.0
        enlarged = cv2.resize(
            roi, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC
        )
        data = pytesseract.image_to_data(
            enlarged,
            lang=OCR_LANG,
            config="--psm 11",
            output_type=pytesseract.Output.DICT,
        )

        for n, raw in enumerate(data.get("text", [])):
            text_value = re.sub(r"[^A-Za-z]", "", str(raw).strip()).upper()
            if text_value != "AUTO":
                continue
            try:
                conf = float(data["conf"][n])
            except Exception:
                conf = 0.0
            if conf < OCR_MIN_TEXT_CONFIDENCE:
                continue

            bx = float(data["left"][n])
            by = float(data["top"][n])
            bw = float(data["width"][n])
            bh = float(data["height"][n])
            vx = x1 + (bx + bw / 2.0) / scale
            vy = y1 + (by + bh / 2.0) / scale
            print(
                f"[AUTO OCR] AUTO found confidence={conf:.0f} "
                f"vision=({vx:.1f},{vy:.1f})"
            )
            return vx, vy, conf

    except Exception as e:
        print("[AUTO OCR ERROR]", repr(e))

    return None


def run_auto_tap_test(capture, worker):
    """
    Real AUTO-button verification.

    Safety:
    - Requires the AUTO button to be detected as OFF before tapping.
    - Sends exactly one real ADB tap.
    - Then verifies that the visual AUTO animation becomes MOVING/ON.
    - Never retaps automatically.
    """
    print("=" * 60)
    print("SOL ENCHANT - REAL AUTO TAP + VERIFY TEST")
    print("=" * 60)
    print("[AUTO TAP TEST] IMPORTANT: AUTO must be OFF before this test")
    print("[AUTO TAP TEST] waiting for video frame")

    deadline = time.time() + AUTO_TAP_TEST_TIMEOUT
    last_frame_id = -1
    before_state = None

    while time.time() < deadline:
        frame, frame_id = capture.get_snapshot()

        if frame is None or frame_id == last_frame_id:
            time.sleep(0.05)
            continue

        last_frame_id = frame_id
        state = detect_auto_state(frame)

        if state is not None:
            print(
                f"[AUTO BEFORE] {state['state']} "
                f"confidence={state['confidence']:.2f}"
            )

            if state["state"] == "ON":
                print("[AUTO TAP TEST] ABORT -> AUTO is already ON")
                print("[AUTO TAP TEST] Turn AUTO OFF manually, then rerun.")
                detect_auto_state._history = []
                return False

            if state["state"] == "OFF":
                before_state = state
                break

    if before_state is None:
        print("[AUTO TAP TEST] ABORT -> could not safely confirm AUTO OFF")
        detect_auto_state._history = []
        return False

    auto_target = find_auto_text_target(frame)
    if auto_target is not None:
        tap_vx, tap_vy, _ = auto_target
    else:
        tap_vx, tap_vy = AUTO_TAP_VISION_X, AUTO_TAP_VISION_Y
        print(
            f"[AUTO OCR] AUTO text not found -> fallback vision="
            f"({tap_vx},{tap_vy})"
        )

    adb_x, adb_y = vision_to_adb(tap_vx, tap_vy)

    print(
        f"[AUTO TAP TEST] OFF confirmed -> "
        f"REAL TAP vision=({tap_vx:.1f},{tap_vy:.1f})"
    )
    print(
        f"[AUTO TAP TEST] ADB TAP -> ({adb_x},{adb_y})"
    )

    success = worker.executor.tap(adb_x, adb_y)

    if not success:
        print("[AUTO TAP TEST] FAILED -> ADB tap command failed")
        detect_auto_state._history = []
        return False

    print("[AUTO TAP TEST] ADB command succeeded")
    print("[AUTO TAP TEST] verifying actual AUTO visual transition...")

    # Do not let the pre-tap OFF frames influence the post-tap result.
    detect_auto_state._history = []

    verify_deadline = time.time() + AUTO_ON_VERIFY_TIMEOUT
    last_frame_id = -1

    while time.time() < verify_deadline:
        frame, frame_id = capture.get_snapshot()

        if frame is None or frame_id == last_frame_id:
            time.sleep(0.05)
            continue

        last_frame_id = frame_id
        state = detect_auto_state(frame)

        if state is None:
            continue

        print(
            f"[AUTO AFTER] {state['state']} "
            f"confidence={state['confidence']:.2f}"
        )

        if state["state"] == "ON":
            print("[AUTO TAP TEST] VERIFIED -> AUTO is actually ON")
            detect_auto_state._history = []
            return True

    print("[AUTO TAP TEST] VERIFY FAILED -> AUTO did not become ON")
    print("[AUTO TAP TEST] No second tap was attempted.")
    detect_auto_state._history = []
    return False


def run_auto_touch_test(capture, worker):
    """
    Real AUTO touch-duration diagnostic.

    Safety:
    - Requires AUTO to be visually classified as OFF first.
    - Sends exactly one 150 ms touch press at the known AUTO coordinate.
    - Does not retap automatically.
    - Verifies whether the existing visual detector sees a transition.
    """
    print("=" * 60)
    print("SOL ENCHANT - REAL AUTO TOUCH-PRESS TEST")
    print("=" * 60)
    print("[AUTO TOUCH TEST] IMPORTANT: AUTO must be OFF before this test")
    print("[AUTO TOUCH TEST] waiting for video frame")

    deadline = time.time() + AUTO_TAP_TEST_TIMEOUT
    last_frame_id = -1
    before_state = None
    frame = None

    while time.time() < deadline:
        frame, frame_id = capture.get_snapshot()

        if frame is None or frame_id == last_frame_id:
            time.sleep(0.05)
            continue

        last_frame_id = frame_id
        state = detect_auto_state(frame)

        if state is None:
            continue

        print(
            f"[AUTO BEFORE] {state['state']} "
            f"confidence={state['confidence']:.2f}"
        )

        if state["state"] == "ON":
            print("[AUTO TOUCH TEST] ABORT -> AUTO is already ON")
            print("[AUTO TOUCH TEST] Turn AUTO OFF manually, then rerun.")
            detect_auto_state._history = []
            return False

        if state["state"] == "OFF":
            before_state = state
            break

    if before_state is None:
        print("[AUTO TOUCH TEST] ABORT -> could not safely confirm AUTO OFF")
        detect_auto_state._history = []
        return False

    auto_target = find_auto_text_target(frame)
    if auto_target is not None:
        tap_vx, tap_vy, _ = auto_target
    else:
        tap_vx, tap_vy = AUTO_TAP_VISION_X, AUTO_TAP_VISION_Y
        print(
            f"[AUTO OCR] AUTO text not found -> fallback vision="
            f"({tap_vx},{tap_vy})"
        )

    adb_x, adb_y = vision_to_adb(tap_vx, tap_vy)

    print(
        f"[AUTO TOUCH TEST] OFF confirmed -> "
        f"PRESS vision=({tap_vx:.1f},{tap_vy:.1f})"
    )
    print(
        f"[AUTO TOUCH TEST] ADB PRESS -> ({adb_x},{adb_y}) duration=150ms"
    )

    success = worker.executor.press(adb_x, adb_y, 150)

    if not success:
        print("[AUTO TOUCH TEST] FAILED -> ADB press command failed")
        detect_auto_state._history = []
        return False

    print("[AUTO TOUCH TEST] ADB command succeeded")
    print("[AUTO TOUCH TEST] verifying actual AUTO visual transition...")

    detect_auto_state._history = []

    verify_deadline = time.time() + AUTO_ON_VERIFY_TIMEOUT
    last_frame_id = -1

    while time.time() < verify_deadline:
        frame, frame_id = capture.get_snapshot()

        if frame is None or frame_id == last_frame_id:
            time.sleep(0.05)
            continue

        last_frame_id = frame_id
        state = detect_auto_state(frame)

        if state is None:
            continue

        print(
            f"[AUTO AFTER] {state['state']} "
            f"confidence={state['confidence']:.2f}"
        )

        if state["state"] == "ON":
            print("[AUTO TOUCH TEST] VERIFIED -> AUTO is actually ON")
            detect_auto_state._history = []
            return True

    print("[AUTO TOUCH TEST] VERIFY FAILED -> AUTO did not become ON")
    print("[AUTO TOUCH TEST] No second touch was attempted.")
    detect_auto_state._history = []
    return False


def run_auto_force_tap_test(capture, worker):
    """
    One-shot real AUTO tap test.

    This is deliberately independent of:
      - AUTO OFF detection
      - OCR
      - VLM
      - motion classification

    Purpose:
      Verify whether the verified coordinate bridge reaches the actual
      AUTO control and whether the game visibly changes after one tap.

    Safety:
      - exactly ONE real ADB tap
      - NO automatic retap
      - saves before/after frames for inspection
    """
    print("=" * 60)
    print("SOL ENCHANT - ONE-SHOT REAL AUTO TAP TEST")
    print("=" * 60)
    print("[AUTO FORCE TAP] NO OCR / NO VLM / NO OFF DETECTION")
    print("[AUTO FORCE TAP] exactly ONE real ADB tap will be sent")

    deadline = time.time() + AUTO_TAP_TEST_TIMEOUT
    last_frame_id = -1
    before_frame = None
    before_id = 0

    while time.time() < deadline:
        frame, frame_id = capture.get_snapshot()

        if frame is None or frame_id == last_frame_id:
            time.sleep(0.05)
            continue

        before_frame = frame
        before_id = frame_id
        break

    if before_frame is None:
        print("[AUTO FORCE TAP] ERROR -> no video frame received")
        return False

    tap_vx = AUTO_TAP_VISION_X
    tap_vy = AUTO_TAP_VISION_Y
    adb_x, adb_y = vision_to_adb(tap_vx, tap_vy)

    print(
        f"[AUTO FORCE TAP] BEFORE frame_id={before_id} "
        f"shape={before_frame.shape}"
    )
    print(
        f"[AUTO FORCE TAP] VISION=({tap_vx},{tap_vy}) "
        f"-> ADB=({adb_x},{adb_y})"
    )

    before_path = "auto_force_before.jpg"
    cv2.imwrite(before_path, before_frame)
    print("[AUTO FORCE TAP] saved ->", before_path)

    print("[AUTO FORCE TAP] SENDING EXACTLY ONE REAL ADB TAP")
    success = worker.executor.tap(adb_x, adb_y)

    if not success:
        print("[AUTO FORCE TAP] FAILED -> ADB tap command failed")
        return False

    print("[AUTO FORCE TAP] TAP COMMAND SUCCEEDED")
    print("[AUTO FORCE TAP] NO SECOND TAP WILL BE SENT")
    print("[AUTO FORCE TAP] waiting 2.0s for the game to react...")

    after_deadline = time.time() + 2.0
    after_frame = None
    after_id = before_id

    while time.time() < after_deadline:
        frame, frame_id = capture.get_snapshot()

        if frame is None or frame_id <= before_id:
            time.sleep(0.05)
            continue

        after_frame = frame
        after_id = frame_id
        time.sleep(0.15)
        latest, latest_id = capture.get_snapshot()
        if latest is not None and latest_id > after_id:
            after_frame = latest
            after_id = latest_id
        break

    if after_frame is None:
        print("[AUTO FORCE TAP] ERROR -> no post-tap video frame received")
        return False

    after_path = "auto_force_after.jpg"
    cv2.imwrite(after_path, after_frame)
    print("[AUTO FORCE TAP] saved ->", after_path)

    # Report raw visual change in the AUTO ROI only.
    x1 = max(0, AUTO_ROI_X1)
    y1 = max(0, AUTO_ROI_Y1)
    x2 = min(after_frame.shape[1], AUTO_ROI_X2)
    y2 = min(after_frame.shape[0], AUTO_ROI_Y2)

    before_roi = before_frame[y1:y2, x1:x2]
    after_roi = after_frame[y1:y2, x1:x2]

    if before_roi.size and after_roi.size:
        before_gray = cv2.cvtColor(before_roi, cv2.COLOR_BGR2GRAY)
        after_gray = cv2.cvtColor(after_roi, cv2.COLOR_BGR2GRAY)
        diff = cv2.absdiff(before_gray, after_gray)
        mean_diff = float(diff.mean())
        changed_ratio = float((diff > 8).mean())

        print(
            f"[AUTO FORCE TAP] ROI change mean={mean_diff:.2f} "
            f"changed_pixels={changed_ratio:.3f}"
        )
    else:
        print("[AUTO FORCE TAP] ROI change unavailable")

    print(
        "[AUTO FORCE TAP] COMPLETE -> inspect the game and "
        "auto_force_before.jpg / auto_force_after.jpg"
    )
    return True


def run_auto_button_grid_diagnostic(capture):
    """
    No-touch AUTO button geometry diagnostic.
    Captures one live frame and creates a dense coordinate grid over the
    current AUTO ROI. Every grid point is labelled with both Vision and
    ADB coordinates. No input event is sent.
    """
    print("=" * 60)
    print("SOL ENCHANT - AUTO BUTTON GRID DIAGNOSTIC")
    print("=" * 60)
    print("[AUTO GRID] NO TOUCH WILL BE SENT")

    deadline = time.time() + AUTO_TAP_TEST_TIMEOUT
    last_frame_id = -1
    frame = None
    frame_id = 0

    while time.time() < deadline:
        frame, frame_id = capture.get_snapshot()

        if frame is None or frame_id == last_frame_id:
            time.sleep(0.05)
            continue

        last_frame_id = frame_id
        break

    if frame is None:
        print("[AUTO GRID] ERROR -> no video frame received")
        return False

    output = frame.copy()

    # Use the full current AUTO ROI, but inset slightly so labels/markers
    # remain inside the actual screen.
    x1 = max(0, AUTO_ROI_X1)
    y1 = max(0, AUTO_ROI_Y1)
    x2 = min(output.shape[1] - 1, AUTO_ROI_X2)
    y2 = min(output.shape[0] - 1, AUTO_ROI_Y2)

    cv2.rectangle(output, (x1, y1), (x2, y2), (0, 255, 255), 2)

    # 5 columns x 5 rows gives enough precision without making the image
    # unreadable. Include the current fixed candidate as a separate marker.
    xs = [x1 + round((x2 - x1) * i / 4) for i in range(5)]
    ys = [y1 + round((y2 - y1) * i / 4) for i in range(5)]

    print(f"[AUTO GRID] frame={frame.shape} frame_id={frame_id}")
    print(f"[AUTO GRID] ROI vision=({x1},{y1})-({x2},{y2})")
    print("[AUTO GRID] candidate coordinates:")

    for row, vy in enumerate(ys):
        row_values = []
        for col, vx in enumerate(xs):
            ax, ay = vision_to_adb(vx, vy)
            row_values.append(f"G{row+1}{col+1}=V({vx},{vy})/A({ax},{ay})")

            cv2.drawMarker(
                output,
                (vx, vy),
                (255, 255, 255),
                cv2.MARKER_CROSS,
                12,
                1,
            )

            label_y = min(output.shape[0] - 4, vy + 16)
            cv2.putText(
                output,
                f"G{row+1}{col+1} {ax},{ay}",
                (max(2, vx - 28), max(12, label_y)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.30,
                (255, 255, 255),
                1,
                cv2.LINE_AA,
            )

        print("  " + " | ".join(row_values))

    # Highlight the current candidate in red so it is obvious which point
    # was just tested by --auto-force-tap-test.
    fx = int(round(AUTO_TAP_VISION_X))
    fy = int(round(AUTO_TAP_VISION_Y))
    fax, fay = vision_to_adb(AUTO_TAP_VISION_X, AUTO_TAP_VISION_Y)

    cv2.drawMarker(
        output,
        (fx, fy),
        (0, 0, 255),
        cv2.MARKER_TILTED_CROSS,
        28,
        3,
    )
    cv2.putText(
        output,
        f"CURRENT ({fx},{fy}) -> ADB ({fax},{fay})",
        (max(5, x1), max(18, y1 - 8)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (0, 0, 255),
        2,
        cv2.LINE_AA,
    )

    # Always save beside this Python script, not in the caller's
    # current working directory. This makes the diagnostic artifact
    # location deterministic.
    output_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(output_dir, "auto_button_grid.jpg")

    try:
        saved = cv2.imwrite(path, output)
    except Exception as e:
        print("[AUTO GRID] IMAGE SAVE ERROR:", repr(e))
        return False

    print("[AUTO GRID] save_result =", saved)
    print("[AUTO GRID] saved ->", path)
    print("[AUTO GRID] file_exists ->", os.path.isfile(path))
    if os.path.isfile(path):
        print("[AUTO GRID] file_size ->", os.path.getsize(path), "bytes")
    print("[AUTO GRID] NO TOUCH WAS SENT")
    return bool(saved)


def run_auto_diagnostic(capture):
    """
    Passive AUTO-button geometry diagnostic.

    This mode NEVER sends a touch event.
    It captures one live frame, marks:
      - the AUTO motion ROI
      - the fixed AUTO tap point
      - the corresponding ADB coordinate
      - the OCR-detected AUTO text target, when available
    and writes a full-frame + AUTO crop image for inspection.
    """
    print("=" * 60)
    print("SOL ENCHANT - PASSIVE AUTO GEOMETRY DIAGNOSTIC")
    print("=" * 60)
    print("[AUTO DIAGNOSTIC] NO TOUCH WILL BE SENT")

    deadline = time.time() + AUTO_TAP_TEST_TIMEOUT
    last_frame_id = -1
    frame = None
    frame_id = 0

    while time.time() < deadline:
        frame, frame_id = capture.get_snapshot()

        if frame is None or frame_id == last_frame_id:
            time.sleep(0.05)
            continue

        last_frame_id = frame_id
        break

    if frame is None:
        print("[AUTO DIAGNOSTIC] ERROR -> no video frame received")
        return False

    print(
        "[AUTO DIAGNOSTIC] frame:",
        frame.shape,
        "frame_id=",
        frame_id,
    )

    output = frame.copy()

    # Draw the exact CV motion ROI.
    cv2.rectangle(
        output,
        (AUTO_ROI_X1, AUTO_ROI_Y1),
        (AUTO_ROI_X2, AUTO_ROI_Y2),
        (0, 255, 255),
        2,
    )

    # Draw the fixed fallback point.
    fixed_x = int(round(AUTO_TAP_VISION_X))
    fixed_y = int(round(AUTO_TAP_VISION_Y))
    cv2.drawMarker(
        output,
        (fixed_x, fixed_y),
        (0, 255, 0),
        cv2.MARKER_CROSS,
        24,
        3,
    )

    fixed_adb_x, fixed_adb_y = vision_to_adb(
        AUTO_TAP_VISION_X,
        AUTO_TAP_VISION_Y,
    )

    cv2.putText(
        output,
        f"FIXED VISION ({fixed_x},{fixed_y}) -> ADB ({fixed_adb_x},{fixed_adb_y})",
        (max(5, AUTO_ROI_X1 - 5), max(25, AUTO_ROI_Y1 - 10)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (0, 255, 0),
        1,
        cv2.LINE_AA,
    )

    # Try OCR independently, but never use its result to touch anything.
    auto_target = find_auto_text_target(frame)

    if auto_target is not None:
        tap_vx, tap_vy, conf = auto_target
        ocr_adb_x, ocr_adb_y = vision_to_adb(tap_vx, tap_vy)

        ox = int(round(tap_vx))
        oy = int(round(tap_vy))

        cv2.drawMarker(
            output,
            (ox, oy),
            (255, 0, 255),
            cv2.MARKER_TILTED_CROSS,
            24,
            3,
        )

        cv2.putText(
            output,
            f"OCR AUTO ({tap_vx:.1f},{tap_vy:.1f}) conf={conf:.0f} -> ADB ({ocr_adb_x},{ocr_adb_y})",
            (max(5, AUTO_ROI_X1 - 5), min(output.shape[0] - 8, AUTO_ROI_Y2 + 24)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            (255, 0, 255),
            1,
            cv2.LINE_AA,
        )

        print(
            f"[AUTO DIAGNOSTIC] OCR target vision=({tap_vx:.1f},{tap_vy:.1f}) "
            f"ADB=({ocr_adb_x},{ocr_adb_y}) confidence={conf:.0f}"
        )
    else:
        print("[AUTO DIAGNOSTIC] OCR target: NOT FOUND")
        cv2.putText(
            output,
            "OCR AUTO: NOT FOUND",
            (max(5, AUTO_ROI_X1 - 5), min(output.shape[0] - 8, AUTO_ROI_Y2 + 24)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            (0, 0, 255),
            1,
            cv2.LINE_AA,
        )

    full_path = "auto_debug.jpg"
    crop_path = "auto_debug_crop.jpg"

    cv2.imwrite(full_path, output)

    crop_x1 = max(0, AUTO_ROI_X1 - 70)
    crop_y1 = max(0, AUTO_ROI_Y1 - 70)
    crop_x2 = min(output.shape[1], AUTO_ROI_X2 + 20)
    crop_y2 = min(output.shape[0], AUTO_ROI_Y2 + 50)

    cv2.imwrite(
        crop_path,
        output[crop_y1:crop_y2, crop_x1:crop_x2],
    )

    print(f"[AUTO DIAGNOSTIC] saved -> {full_path}")
    print(f"[AUTO DIAGNOSTIC] saved -> {crop_path}")
    print(
        f"[AUTO DIAGNOSTIC] fixed vision=({fixed_x},{fixed_y}) "
        f"ADB=({fixed_adb_x},{fixed_adb_y})"
    )
    print("[AUTO DIAGNOSTIC] COMPLETE -> no touch was attempted.")

    return True


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
            accepted,            conf
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





    def press(self, x, y, duration_ms=150):
        """
        Send one deliberate touch press using ADB input swipe with identical
        start/end coordinates. This is a diagnostic alternative to input tap
        for games that are sensitive to very-short touch events.
        """
        if DRY_RUN:
            print(f"[DRY RUN] press({x},{y},{duration_ms}ms)")
            return False

        result = adb_run(
            [
                "shell",
                "input",
                "swipe",
                str(x),
                str(y),
                str(x),
                str(y),
                str(duration_ms),
            ],
            timeout=5,
        )

        if result is None or result.returncode != 0:
            print("[PRESS ERROR]")
            return False

        print("[PRESS]", x, y, f"{duration_ms}ms")
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
            print("[ACTION] no target")
            return False

        print("[ACTION]", state, target)

        if target.get("action") == "back":
            return self.back()

        if "adb_x" in target and "adb_y" in target:
            return self.tap(target["adb_x"], target["adb_y"])

        print("[ACTION] invalid target")
        return False


# ============================================================
# ROUTER / WORKER / MAIN
# ============================================================

HOME_TARGET_PROMPT = """
The HOME/town screen is confirmed.
Find one safe navigation control that returns to normal gameplay.
Prefer CLOSE, BACK, EXIT, or a clearly labeled return control.
Do not select NPCs, shops, items, or decoration.
Return exactly:
TARGET|x|y|confidence|reason
or
NONE
"""


class VLMWorker:
    def __init__(self, capture):
        self.capture = capture
        self.stop_event = threading.Event()
        self.thread = None
        self.last_frame_id = 0
        self.stabilizer = StateStabilizer()
        self.executor = ActionExecutor()

    def start(self):
        self.thread = threading.Thread(
            target=self.run,
            daemon=True
        )
        self.thread.start()

    def run(self):
        print("[WORKER] started")

        while not self.stop_event.is_set():
            frame, frame_id = self.capture.get_snapshot()

            if frame is None:
                time.sleep(0.1)
                continue

            if frame_id == self.last_frame_id:
                time.sleep(0.1)
                continue

            self.last_frame_id = frame_id

            try:
                self.process(frame)
            except Exception as e:
                print("[WORKER ERROR]", repr(e))

            time.sleep(VISION_INTERVAL)

    def process(self, frame):
        raw = detect_state(frame)
        state, confidence = parse_state(raw)

        print("[STATE PARSED]", state, confidence)

        if state == "INVENTORY":
            verified = verify_inventory(frame)

            if verified is False:
                print("[INVENTORY] rejected")
                state = "NORMAL"
                confidence = 0.80

            elif verified is None:
                print("[INVENTORY] verification unavailable")
                return

        confirmed, changed = self.stabilizer.update(
            state,
            confidence
        )

        print("[CONFIRMED]", confirmed)

        if not changed:
            return

        action = route_action(confirmed)

        if action == "NONE":
            return

        target = detect_target(
            frame,
            inventory=(confirmed == "INVENTORY"),
            state=confirmed
        )

        self.executor.execute(
            confirmed,
            target
        )

    def stop(self):
        self.stop_event.set()

        if self.thread:
            self.thread.join(timeout=3)


def _get_live_frame(capture, timeout=10.0):
    deadline = time.time() + timeout
    last_frame_id = -1

    while time.time() < deadline:
        frame, frame_id = capture.get_snapshot()

        if frame is not None and frame_id != last_frame_id:
            return frame, frame_id

        last_frame_id = frame_id
        time.sleep(0.05)

    return None, 0


def _save_debug_frame(frame, filename):
    if frame is None:
        return None

    output_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(output_dir, filename)

    try:
        if cv2.imwrite(path, frame):
            print("[DEBUG] saved ->", path)
            return path
    except Exception as e:
        print("[DEBUG SAVE ERROR]", repr(e))

    return None


def run_auto_force_tap_test(capture, executor):
    print("=" * 60)
    print("SOL ENCHANT - ONE-SHOT REAL AUTO TAP TEST")
    print("=" * 60)
    print("[AUTO FORCE TAP] NO OCR / NO VLM / NO RETRY")

    frame, frame_id = _get_live_frame(
        capture,
        timeout=AUTO_TAP_TEST_TIMEOUT
    )

    if frame is None:
        print("[AUTO FORCE TAP] ERROR -> no video frame")
        return False

    _save_debug_frame(frame, "auto_force_before.jpg")

    vx = AUTO_TAP_VISION_X
    vy = AUTO_TAP_VISION_Y
    ax, ay = vision_to_adb(vx, vy)

    print(
        f"[AUTO FORCE TAP] Vision=({vx},{vy}) -> ADB=({ax},{ay})"
    )

    success = executor.tap(ax, ay)

    if not success:
        print("[AUTO FORCE TAP] FAILED -> ADB tap failed")
        return False

    print("[AUTO FORCE TAP] SUCCESS -> exactly one tap sent")

    after_deadline = time.time() + 2.0
    after_frame = None

    while time.time() < after_deadline:
        candidate, candidate_id = capture.get_snapshot()

        if candidate is not None and candidate_id > frame_id:
            after_frame = candidate
            break

        time.sleep(0.05)

    if after_frame is not None:
        _save_debug_frame(after_frame, "auto_force_after.jpg")

    print("[AUTO FORCE TAP] NO SECOND TAP WILL BE SENT")
    return True


def run_auto_tap_test(capture, executor):
    print("=" * 60)
    print("SOL ENCHANT - REAL AUTO TAP TEST")
    print("=" * 60)
    print("[AUTO TAP TEST] fixed coordinate only; OCR cannot override it")

    detect_auto_state._history = []

    deadline = time.time() + AUTO_TAP_TEST_TIMEOUT
    last_frame_id = -1

    while time.time() < deadline:
        frame, frame_id = capture.get_snapshot()

        if frame is None or frame_id == last_frame_id:
            time.sleep(0.05)
            continue

        last_frame_id = frame_id
        result = detect_auto_state(frame)

        if result is None:
            continue

        print(
            "[AUTO BEFORE]",
            result["state"],
            f"confidence={result['confidence']:.2f}"
        )

        if result["state"] == "ON":
            print("[AUTO TAP TEST] ABORT -> AUTO is already ON")
            detect_auto_state._history = []
            return False

        if result["state"] == "OFF":
            break
    else:
        print("[AUTO TAP TEST] ABORT -> could not confirm AUTO OFF")
        detect_auto_state._history = []
        return False

    ax, ay = vision_to_adb(
        AUTO_TAP_VISION_X,
        AUTO_TAP_VISION_Y
    )

    print(
        "[AUTO TAP TEST] FIXED TARGET:",
        f"Vision=({AUTO_TAP_VISION_X},{AUTO_TAP_VISION_Y})",
        f"ADB=({ax},{ay})"
    )

    if not executor.tap(ax, ay):
        detect_auto_state._history = []
        return False

    print("[AUTO TAP TEST] ADB TAP succeeded")
    detect_auto_state._history = []

    verify_deadline = time.time() + AUTO_ON_VERIFY_TIMEOUT
    last_frame_id = -1

    while time.time() < verify_deadline:
        frame, frame_id = capture.get_snapshot()

        if frame is None or frame_id == last_frame_id:
            time.sleep(0.05)
            continue

        last_frame_id = frame_id
        result = detect_auto_state(frame)

        if result is None:
            continue

        print(
            "[AUTO AFTER]",
            result["state"],
            f"confidence={result['confidence']:.2f}"
        )

        if result["state"] == "ON":
            print("[AUTO TAP TEST] VERIFIED -> AUTO ON")
            detect_auto_state._history = []
            return True

    print("[AUTO TAP TEST] VERIFY FAILED -> no second tap attempted")
    detect_auto_state._history = []
    return False


def run_auto_touch_test(capture, executor):
    print("=" * 60)
    print("SOL ENCHANT - REAL AUTO TOUCH-PRESS TEST")
    print("=" * 60)

    ax, ay = vision_to_adb(
        AUTO_TAP_VISION_X,
        AUTO_TAP_VISION_Y
    )

    print(
        "[AUTO TOUCH TEST] FIXED TARGET:",
        f"Vision=({AUTO_TAP_VISION_X},{AUTO_TAP_VISION_Y})",
        f"ADB=({ax},{ay})"
    )

    success = executor.press(
        ax,
        ay,
        150
    )

    print(
        "[AUTO TOUCH TEST]",
        "SUCCESS" if success else "FAILED"
    )

    return success


def run_auto_button_grid_diagnostic(capture):
    print("=" * 60)
    print("SOL ENCHANT - AUTO BUTTON GRID DIAGNOSTIC")
    print("=" * 60)
    print("[AUTO GRID] NO TOUCH WILL BE SENT")

    frame, frame_id = _get_live_frame(
        capture,
        timeout=AUTO_TAP_TEST_TIMEOUT
    )

    if frame is None:
        print("[AUTO GRID] ERROR -> no video frame")
        return False

    output = frame.copy()

    x1 = max(0, min(output.shape[1] - 1, AUTO_ROI_X1))
    y1 = max(0, min(output.shape[0] - 1, AUTO_ROI_Y1))
    x2 = max(x1, min(output.shape[1] - 1, AUTO_ROI_X2))
    y2 = max(y1, min(output.shape[0] - 1, AUTO_ROI_Y2))

    cv2.rectangle(
        output,
        (x1, y1),
        (x2, y2),
        (0, 255, 255),
        2
    )

    xs = [x1, round((x1 + x2) / 2), x2]
    ys = [y1, round((y1 + y2) / 2), y2]

    for row, vy in enumerate(ys, 1):
        for col, vx in enumerate(xs, 1):
            ax, ay = vision_to_adb(vx, vy)

            cv2.drawMarker(
                output,
                (vx, vy),
                (255, 255, 255),
                cv2.MARKER_CROSS,
                12,
                1
            )

            cv2.putText(
                output,
                f"G{row}{col} {ax},{ay}",
                (
                    max(2, vx - 30),
                    min(output.shape[0] - 4, vy + 15)
                ),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.30,
                (255, 255, 255),
                1,
                cv2.LINE_AA
            )

    fx = int(round(AUTO_TAP_VISION_X))
    fy = int(round(AUTO_TAP_VISION_Y))
    fax, fay = vision_to_adb(
        AUTO_TAP_VISION_X,
        AUTO_TAP_VISION_Y
    )

    cv2.drawMarker(
        output,
        (fx, fy),
        (0, 0, 255),
        cv2.MARKER_TILTED_CROSS,
        28,
        3
    )

    cv2.putText(
        output,
        f"CURRENT ({fx},{fy}) -> ADB ({fax},{fay})",
        (5, 18),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.45,
        (0, 0, 255),
        1,
        cv2.LINE_AA
    )

    output_dir = os.path.dirname(
        os.path.abspath(__file__)
    )
    path = os.path.join(
        output_dir,
        "auto_button_grid.jpg"
    )

    saved = cv2.imwrite(
        path,
        output
    )

    print(
        "[AUTO GRID]",
        f"Vision=({fx},{fy})",
        f"ADB=({fax},{fay})",
        "saved=",
        saved,
        path
    )

    return bool(saved)


def run_auto_diagnostic(capture):
    return run_auto_button_grid_diagnostic(capture)


def main():
    args = set(sys.argv[1:])

    print("=" * 60)
    print("SOL ENCHANT VISION AUTOMATION")
    print("=" * 60)

    check_adb()
    print_coordinate_bridge()

    capture = VisionCapture()
    worker = VLMWorker(capture)

    try:
        capture.start()

        if (
            "--auto-button-grid" in args
            or "--auto-diagnostic" in args
        ):
            return run_auto_button_grid_diagnostic(capture)

        if "--auto-force-tap-test" in args:
            return run_auto_force_tap_test(
                capture,
                worker.executor
            )

        if "--auto-touch-test" in args:
            return run_auto_touch_test(
                capture,
                worker.executor
            )

        if "--auto-tap-test" in args:
            return run_auto_tap_test(
                capture,
                worker.executor
            )

        worker.start()

        print("Q = EXIT")

        while True:
            if msvcrt.kbhit():
                key = msvcrt.getwch()

                if key.lower() == "q":
                    print("[MAIN] exit")
                    break

            time.sleep(0.05)

    except KeyboardInterrupt:
        print("[MAIN] interrupt")

    except Exception as e:
        print("[MAIN ERROR]", repr(e))

    finally:
        worker.stop()
        capture.stop()
        print("[DONE]")


if __name__ == "__main__":
    main()
