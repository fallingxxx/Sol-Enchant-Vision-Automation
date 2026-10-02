import base64
import msvcrt
import re
import socket
import subprocess
import threading
import time

import av
import cv2
import requests


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

VISION_INTERVAL = 1.0

VLM_TIMEOUT = 120

VLM_RETRY_COUNT = 1

VLM_RETRY_DELAY = 0.5


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

ENABLE_TARGET_DETECTION = True

TARGET_MIN_CONFIDENCE = 0.60

TARGET_VERIFY_MIN_CONFIDENCE = 0.70

TARGET_RETRY_COUNT = 1


# safety

DRY_RUN = False

ENABLE_INVENTORY_ACTION = True



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
            "num_predict":128,
        }

    }


    for attempt in range(
        VLM_RETRY_COUNT + 1
    ):

        try:

            r = requests.post(
                OLLAMA_URL,
                json=payload,
                timeout=timeout,
            )



            r.raise_for_status()

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


    raise RuntimeError(
        "Ollama failed"
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
# VLM STATE DETECTION
# ============================================================


STATE_PROMPT = """
Analyze this Sol Enchant screenshot.

Return exactly:

STATE|confidence


Allowed states:

NORMAL
BATTLE
MENU
INVENTORY
SHOP
HOME
UNKNOWN


Rules:

NORMAL:
ordinary gameplay screen.

BATTLE:
active combat.

MENU:
large general menu panel.

INVENTORY:
large inventory/equipment/item management panel.

SHOP:
merchant shop interface with product rows,
prices, currency and buy/sell controls.

HOME:
actual home/base/town interface.

UNKNOWN:
uncertain.


Do not classify small icons as MENU,
SHOP or INVENTORY.
"""


INVENTORY_PROMPT = """

Is a real inventory/equipment/items panel open?

Return ONLY one line.

Format:

YES|0.95

or

NO|0.95


Rules:

YES means a real inventory/equipment/items management panel is visible.

NO means normal gameplay or another screen.

The number must be a confidence value between 0 and 1.

Do not write the word confidence.

Do not add explanations.

A normal gameplay HUD is NOT inventory.

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



# ============================================================
# STATE
# ============================================================


def detect_state(frame):

    try:

        result = ollama_chat(
            STATE_PROMPT,
            image=frame
        )


        return (
            result
            .get("message", {})
            .get("content","")
            .strip()
        )


    except Exception as e:

        print(
            "[STATE ERROR]",
            e
        )

        return "UNKNOWN|0"





def parse_state(raw):
    if not raw:
        return "UNKNOWN", 0.0

    text = raw.upper()

    detected = None

    for state_name in VALID_STATES:
        if state_name in text:
            detected = state_name
            break

    if detected is None:
        return "UNKNOWN", 0.0

    confidence = 0.8

    nums = re.findall(r"\d+(?:\.\d+)?", text)

    if nums:
        value = float(nums[-1])

        if value > 1:
            confidence = value / 100.0
        else:
            confidence = value

    return detected, confidence





# ============================================================
# INVENTORY VERIFY
# ============================================================


def verify_inventory(frame):


    try:


        result = ollama_chat(
            INVENTORY_PROMPT,
            image=frame
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



        m = YES_NO_RE.search(
            text
        )


        if not m:

            return False



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
# TARGET PARSER
# ============================================================


def parse_target(raw):

    if not raw:
        return None

    text = raw.upper()

    # BACK ACTION
    if "BACK" in text:
        return {
            "action": "back",
            "reason": "model requested back"
        }

    if "NONE" in text:
        return None

    m = TARGET_RE.search(raw)

    if not m:
        return None

    x = int(m.group(1))
    y = int(m.group(2))
    conf = float(m.group(3))

    if conf > 1:
        conf /= 100

    reason = m.group(4).strip()

    if not (
        0 <= x < VISION_WIDTH
        and
        0 <= y < VISION_HEIGHT
    ):
        print("[TARGET] invalid coordinate")
        return None

    if conf < TARGET_MIN_CONFIDENCE:
        return None

    ax, ay = vision_to_adb(x, y)

    return {
        "vision_x": x,
        "vision_y": y,
        "adb_x": ax,
        "adb_y": ay,
        "confidence": conf,
        "reason": reason,
    }


def detect_target(
    frame,
    inventory=False
):

    try:

        prompt = (
            INVENTORY_TARGET_PROMPT
            if inventory
            else TARGET_PROMPT
        )

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


        raw = detect_state(
            frame
        )


        print(
            "[STATE RAW]",
            raw
        )



        state, confidence = parse_state(
            raw
        )



        if state == "INVENTORY":


            if not verify_inventory(frame):

                print(
                    "[INVENTORY rejected]"
                )

                state = "NORMAL"

                confidence = 0.8



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



        target = detect_target(
            frame,
            inventory=(
                confirmed
                ==
                "INVENTORY"
            )
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
# MAIN
# ============================================================


def main():


    print("=" * 60)

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