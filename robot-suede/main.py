"""Doodlebot client — the module deployed onto each robot.

This is the *bot* half of the protocol whose server half lives in
``server-suede/robots.py``. It implements the **Locate → Poll → Draw** state
machine from the repo README:

    [*] --> Locate            # find self via aruco marker detection
    Locate --> Poll           # ask the server for a drawing (~1s)
    Poll --> Poll             # nothing yet, wait ~1s
    Poll --> Draw             # drawing received
    Draw --> Locate           # repeat
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal, Optional, Union
import socket
import math
import requests
from websockets.sync.client import connect
import numpy as np

HOST = "127.0.0.1"
BLE_PORT = 5000

robot = None
marker_map = None


def send(msg):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((HOST, BLE_PORT))
        s.sendall(("fromDoodlebotAILive|" + msg + "\n").encode())

        while True:
            data = s.recv(1024)

            if not data:
                raise RuntimeError("Connection closed")

            if b"ms" in data:
                return


WEBSOCKET_PORT = 8765


def display(cmd):
    print(hostname)
    uri = f"ws://{hostname}.direct.mitlivinglab.org/api/v1/command"

    with connect(uri) as ws:
        ws.send(cmd)
        return ws.recv()


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    name: str
    server_url: str = "https://doodlebot.media.mit.edu"
    poll_interval_seconds: float = 1.0


# --------------------------------------------------------------------------- #
# Wire types — mirror server-suede/robots.py.
#
# The two modules are deployed independently (separate git subrepos), so these
# are kept as a deliberately small, hand-maintained mirror of the server's
# pydantic models rather than a shared import.
# --------------------------------------------------------------------------- #


@dataclass
class Point:
    x: float
    y: float


Stroke = list[Point]


@dataclass
class Pose:
    x: float
    y: float
    headingDegrees: float = 0.0


@dataclass
class ArucoMarker:
    id: int
    position: Point
    angle: float
    sizeMm: Optional[float] = None


@dataclass
class LineCommand:
    distance: float
    penDown: bool
    kind: Literal["line"] = "line"


@dataclass
class SpinCommand:
    degrees: float
    kind: Literal["spin"] = "spin"


@dataclass
class ArcCommand:
    radius: float
    degrees: float
    kind: Literal["arc"] = "arc"


DrawingCommand = Union[LineCommand, SpinCommand, ArcCommand]


@dataclass
class DrawJob:
    jobId: str
    navigateTo: Pose
    commands: list[DrawingCommand]
    exitPose: Optional[Pose] = None


def _parse_command(raw: dict) -> DrawingCommand:
    kind = raw["kind"]
    if kind == "line":
        return LineCommand(distance=raw["distance"], penDown=raw["penDown"])
    if kind == "spin":
        return SpinCommand(degrees=raw["degrees"])
    if kind == "arc":
        return ArcCommand(radius=raw["radius"], degrees=raw["degrees"])
    raise ValueError(f"Unknown drawing command kind: {kind!r}")


# --------------------------------------------------------------------------- #
# Hardware / vision stubs — implemented when the module is flashed to a bot.
# --------------------------------------------------------------------------- #


@dataclass
class CameraFrame:
    """An opaque captured image (e.g. a numpy array on real hardware)."""

    width: int
    height: int
    pixels: object


@dataclass
class DetectedMarker:
    """An aruco marker seen in a frame, with its pixel-space corners."""

    id: int
    corners: list[Point]


def wait_for_server(host=HOST, port=BLE_PORT, timeout=60, interval=0.5):
    start = time.time()

    while True:
        try:
            with socket.create_connection((host, port), timeout=2):
                print(f"Server is available on {host}:{port}")
                return
        except OSError:
            if time.time() - start > timeout:
                raise TimeoutError(f"Timed out waiting for {host}:{port}")
            time.sleep(interval)


def wait_for_aruco_setup(robot_name, timeout=60, interval=1.0):
    url = f"http://{robot_name}.direct.mitlivinglab.org:8001/aruco/setup"

    start = time.time()

    while True:
        try:
            # Try a GET first (safe probe)
            r = requests.get(url, timeout=2)

            # Any non-5xx response means server is up
            if r.status_code < 500:
                print("ArUco endpoint is ready.")
                return True

        except requests.RequestException:
            pass

        if time.time() - start > timeout:
            raise TimeoutError(f"Timed out waiting for {url}")

        time.sleep(interval)


def setup_aruco_client(robot_name, marker_map):
    response = requests.post(
        f"http://{robot_name}.direct.mitlivinglab.org:8001/aruco/setup",
        json={"robot_name": robot_name, "marker_map": marker_map},
    )
    print("Status:", response.status_code)


def estimate_pose() -> Pose | None:

    try:
        resp = requests.get(
            f"http://{robot}.direct.mitlivinglab.org:8001/aruco/position", timeout=1.0
        )
        data = resp.json()

        print(data)
        angle = float(data["yaw"] * 180 / math.pi)
        print(angle)

        camera_offset_mm = 51.0
        rad = math.radians(angle)
        true_x = float(data["x"]) - math.cos(rad) * camera_offset_mm
        true_y = float(data["y"]) - math.sin(rad) * camera_offset_mm

        if data:
            return Pose(
                x=true_x,
                y=true_y,
                headingDegrees=float(data["yaw"] * 180 / math.pi),
            )
        return None
    except Exception as error:
        return None


def normalize_angle(a: float) -> float:
    while a > math.pi:
        a -= 2 * math.pi
    while a < -math.pi:
        a += 2 * math.pi
    return a


def navigate_to(target: Pose, current: Pose) -> None:
    """Drive the bot from current to target (pen up)."""

    dx = target.x - current.x
    dy = target.y - current.y

    print("naigating")
    print(target)
    print(current)

    # canvas-style coordinate system (same as your TS)
    target_heading = math.atan2(dy, dx)

    turn1 = normalize_angle(target_heading - math.radians(current.headingDegrees))
    distance = math.hypot(dx, dy)

    # Build commands exactly like your TS goToPoint()
    arc_cmd1 = ArcCommand(
        radius=0,
        degrees=math.degrees(turn1),  # convert radians → degrees for Arduino protocol
    )
    newAngle = current.headingDegrees + math.degrees(turn1)
    turn2 = normalize_angle(
        math.radians(newAngle) - math.radians(target.headingDegrees)
    )
    arc_cmd2 = ArcCommand(
        radius=0,
        degrees=math.degrees(
            -1 * turn2
        ),  # convert radians → degrees for Arduino protocol
    )
    # distanceCm = distance / 10
    # steps = distanceCm * CM_TO_STEPS

    line_cmd = LineCommand(distance=distance, penDown=False)

    # Execute through your existing pipeline
    execute_commands([arc_cmd1, line_cmd, arc_cmd2])
    print(arc_cmd1)
    print(line_cmd)
    print(arc_cmd2)


def send_command(cmd: str) -> None:
    print(f"Sending: {cmd}")
    send(cmd)


CM_TO_STEPS = 7.16 * 16


def estimate_final_pose(commands, start_pose):
    """
    Replay commands without moving the robot.
    Returns final x, y, yaw.
    """

    x = start_pose.x
    y = start_pose.y
    yaw = start_pose.headingDegrees * math.pi / 180

    for cmd in commands:

        if isinstance(cmd, LineCommand):
            if cmd.penDown:
                # Move forward in current heading
                distance = cmd.distance / 10  # same conversion as your code if needed

                x += distance * math.cos(yaw)
                y += distance * math.sin(yaw)

            else:
                # Pen-up movement is still movement
                distance = cmd.distance / 10

                x += distance * math.cos(yaw)
                y += distance * math.sin(yaw)

        elif isinstance(cmd, SpinCommand):
            yaw += math.radians(cmd.degrees)

        elif isinstance(cmd, ArcCommand):
            # Arc math
            radius = cmd.radius

            angle = math.radians(cmd.degrees)

            # center of rotation
            cx = x - radius * math.sin(yaw)
            cy = y + radius * math.cos(yaw)

            yaw += angle

            x = cx + radius * math.sin(yaw)
            y = cy - radius * math.cos(yaw)

    return Pose(
        x=x,
        y=y,
        headingDegrees=yaw * 180 / math.pi,
    )


def execute_commands(commands: list[DrawingCommand]) -> None:
    """Issue line/spin/arc commands to the drive + pen, in order."""
    currentPen = 1
    for cmd in commands:
        if isinstance(cmd, ArcCommand):
            if currentPen == 0:
                send_command(f"u,45")
                currentPen = 1
            radiusInch = 0.0393701 * cmd.radius
            send_command(f"t,{radiusInch},{-1*cmd.degrees}")

        elif isinstance(cmd, LineCommand):
            if cmd.penDown:
                if currentPen == 0:
                    send_command(f"u,45")
                    currentPen = 1
            else:
                if currentPen == 1:
                    send_command(f"u,0")
                    currentPen = 0
            print("before")
            print(cmd.distance)
            distanceCm = cmd.distance / 10
            steps = distanceCm * CM_TO_STEPS
            send_command(f"m,{round(steps)},{round(steps)},2000,2000")

        elif isinstance(cmd, SpinCommand):
            send_command(f"t,0,{-1*cmd.degrees}")


# --------------------------------------------------------------------------- #
# Server client — the real "robot talking to the server" half.
# --------------------------------------------------------------------------- #


class ServerClient:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._session = requests.Session()

    def _url(self, path: str) -> str:
        return f"{self._config.server_url.rstrip('/')}{path}"

    def fetch_markers(self) -> dict[str, dict[str, float]]:
        """GET /api/robots/markers — the Locate step's known marker layout."""

        try:
            resp = self._session.get(
                self._url(f"/api/robots/markers?robot={robot}"),
                timeout=10,
            )
            resp.raise_for_status()

            markers = resp.json()["markers"]
            print(markers)

            return {
                str(m["id"]): {
                    "x": m["position"]["x"],
                    "y": m["position"]["y"],
                    "z": 0,
                    "yaw": m["yawRadians"],
                    "size": m["sizeMm"] / 1000,
                }
                for m in markers
            }

        except requests.exceptions.RequestException as e:
            print(f"Failed to fetch markers: {e}")
            return {}

        except (KeyError, ValueError) as e:
            print(f"Invalid marker response: {e}")
            return {}

    def check_in(
        self,
        status: Literal["locating", "ready", "drawing"],
        pose: Pose,
    ) -> Optional[DrawJob]:
        """``POST /api/robots/checkin`` — returns a job to draw, or ``None``.

        The server owns the canvas/occupancy model, so the bot only reports where
        it is; placement (region, rotation, offset) comes back inside the job.
        """
        payload = {
            "name": self._config.name,
            "status": status,
            "pose": {
                "x": pose.x,
                "y": pose.y,
                "headingDegrees": pose.headingDegrees,
            },
        }
        resp = self._session.post(
            self._url("/api/robots/checkin"), json=payload, timeout=10
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("action") != "draw":
            return None

        print("GO")
        print(body["navigateTo"])
        print("exit path")
        print(body.get("exitPath", []))

        return DrawJob(
            jobId=body["jobId"],
            navigateTo=Pose(
                x=body["navigateTo"]["x"],
                y=body["navigateTo"]["y"],
                headingDegrees=body["navigateTo"].get("headingDegrees", 0.0),
            ),
            commands=[_parse_command(c) for c in body["commands"]],
            exitPose=body.get("exitPose", None),
        )


# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #


# def locate(client: ServerClient) -> Pose:
#     """Locate self via aruco code detection (README: ``Locate``)."""
#     # markers = client.fetch_markers()
#     # return estimate_pose(markers)
#     return estimate_pose()


def run(client: ServerClient) -> None:
    """Run the Locate → Poll → Draw loop forever."""

    alternate = True
    while True:
        # --- Locate ---------------------------------------------------------
        pose = None
        job: Optional[DrawJob] = None

        while pose is None or job is None:

            marker_map = client.fetch_markers()

            setup_aruco_client(hostname, marker_map)

            # Try to localize
            new_pose = estimate_pose()
            if new_pose is not None:
                pose = new_pose
            else:
                execute_commands([SpinCommand(degrees=10)])
                if alternate:
                    print("anger")
                    display("(d,a)")
                    alternate = False
                else:
                    print("love")
                    display("(d,O)")
                    alternate = True

            # Poll for a job if we have a pose
            if pose is not None:
                new_job = client.check_in("ready", pose)
                if new_job is not None:
                    job = new_job

            if job is None:
                time.sleep(config.poll_interval_seconds)

        # --- Draw -----------------------------------------------------------
        print(f"[{config.name}] drawing {job.jobId} ({len(job.commands)} commands)")
        print(job.navigateTo)
        navigate_to(job.navigateTo, pose)
        execute_commands(job.commands)
        new_pose = estimate_final_pose(job.commands, job.navigateTo)
        print("new pose", new_pose)
        if job.exitPose:
            navigate_to(job.exitPose, new_pose)


if __name__ == "__main__":

    import argparse

    hostname = socket.gethostname()
    print("hostname", hostname)
    parser = argparse.ArgumentParser(description="Doodlebot client")
    parser.add_argument(
        "--server", default="https://doodlebot.media.mit.edu", help="server base URL"
    )
    wait_for_server()
    wait_for_aruco_setup(hostname, 300)
    args = parser.parse_args()
    config = Config(name=hostname, server_url=args.server)
    client = ServerClient(config)
    print(f"[{config.name}] starting; server = {config.server_url}")

    robot = hostname
    marker_map = client.fetch_markers()

    setup_aruco_client(hostname, marker_map)
    display("(d,h)")
    run(client)
