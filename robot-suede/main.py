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
import numpy as np

HOST = "127.0.0.1"
PORT = 5000

robot = None
marker_map = {"0": {"x": 12.0, "y": 11.0, "z": 0.0, "yaw": math.pi / 4}}


def send(msg):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((HOST, PORT))
        s.sendall(("fromDoodlebotAILive|" + msg + "\n").encode())

        while True:
            data = s.recv(1024)

            if not data:
                raise RuntimeError("Connection closed")

            if b"ms" in data:
                return


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


@dataclass
class Pose:
    x: float
    y: float
    headingDegrees: float = 0.0


@dataclass
class ArucoMarker:
    id: int
    position: Point
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
    exitPath: list[DrawingCommand] = field(default_factory=list)


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


def setup_aruco_client(robot_name, marker_map, marker_size_m):
    global robot
    robot = robot_name
    print("SETTING UP ARUCO", robot_name)
    response = requests.post(
        f"http://{robot_name}.direct.mitlivinglab.org:8001/aruco/setup",
        json={
            "robot_name": robot_name,
            "marker_map": marker_map,
            "marker_size_m": marker_size_m,
        },
    )
    print("Status:", response.status_code)
    print("Text:", response.text)


def get_robot_canvas_position(pose: dict) -> dict[str, float]:
    """
    Returns (canvas_x, canvas_y) in cm.

    x = lateral position along the marker face
    y = distance along the canvas away from the marker
        (projected onto ground plane, ignoring camera height)
    """
    # x/z are the ground-plane axes; y is vertical (camera mount height)
    camera_height = pose["y"]  # vertical offset — use to correct ground projection

    canvas_x = pose["x"] * 1000  # lateral, cm
    canvas_y = np.sqrt(pose["z"] ** 2 - pose["y"] ** 2) * 1000  # ground-plane depth, cm

    return {"x": canvas_x, "y": canvas_y}


def estimate_pose() -> Pose | None:
    try:
        resp = requests.get(
            f"http://{robot}.direct.mitlivinglab.org:8001/aruco/position", timeout=1.0
        )
        data = resp.json()

        print(data)
        print("angle: ", data["yaw"] * 180 / math.pi)

        if data:
            # canvas_position = get_robot_canvas_position(data)
            # print(canvas_position)
            # print(marker_map)
            # print(data["marker_id"])
            # print(marker_map[str(data["marker_id"])])
            # print(marker_map[str(data["marker_id"])]["yaw"])

            # adjacent = data["z"] * math.cos(marker_map[str(data["marker_id"])]["yaw"])
            # opposite = data["z"] * math.sin(marker_map[str(data["marker_id"])]["yaw"])

            # print(float(data["yaw"] * 180 / math.pi))
            # print(adjacent * 1000)
            # print(opposite * 1000)
            return Pose(
                x=float(data["x"]),
                y=float(data["y"]),
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

    # canvas-style coordinate system (same as your TS)
    target_heading = math.atan2(-dy, dx)

    turn = normalize_angle(target_heading - math.radians(current.headingDegrees))
    distance = math.hypot(dx, dy)

    # Build commands exactly like your TS goToPoint()
    arc_cmd = ArcCommand(
        radius=0,
        degrees=math.degrees(turn),  # convert radians → degrees for Arduino protocol
    )
    # distanceCm = distance / 10
    # steps = distanceCm * CM_TO_STEPS

    line_cmd = LineCommand(distance=distance, penDown=False)

    # Execute through your existing pipeline
    execute_commands([arc_cmd, line_cmd])


def send_command(cmd: str) -> None:
    print(f"Sending: {cmd}")
    send(cmd)


CM_TO_STEPS = 7.16 * 16


def execute_commands(commands: list[DrawingCommand]) -> None:
    """Issue line/spin/arc commands to the drive + pen, in order."""
    currentPen = 1
    for cmd in commands:
        if isinstance(cmd, ArcCommand):
            if currentPen == 0:
                send_command(f"u,45")
                currentPen = 1
            send_command(f"t,{cmd.radius},{cmd.degrees}")

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
            send_command(f"t,0,{cmd.degrees}")


# --------------------------------------------------------------------------- #
# Server client — the real "robot talking to the server" half.
# --------------------------------------------------------------------------- #


class ServerClient:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._session = requests.Session()

    def _url(self, path: str) -> str:
        return f"{self._config.server_url.rstrip('/')}{path}"

    def fetch_markers(self) -> list[ArucoMarker]:
        """``GET /api/robots/markers`` — the Locate step's known marker layout."""
        resp = self._session.get(self._url("/api/robots/markers"), timeout=10)
        resp.raise_for_status()
        return [
            ArucoMarker(
                id=m["id"],
                position=Point(x=m["position"]["x"], y=m["position"]["y"]),
                sizeMm=m.get("sizeMm"),
            )
            for m in resp.json()["markers"]
        ]

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
        return DrawJob(
            jobId=body["jobId"],
            navigateTo=Pose(
                x=body["navigateTo"]["x"],
                y=body["navigateTo"]["y"],
                headingDegrees=body["navigateTo"].get("headingDegrees", 0.0),
            ),
            commands=[_parse_command(c) for c in body["commands"]],
            exitPath=[_parse_command(c) for c in body.get("exitPath", [])],
        )


# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #


# def locate(client: ServerClient) -> Pose:
#     """Locate self via aruco code detection (README: ``Locate``)."""
#     # markers = client.fetch_markers()
#     # return estimate_pose(markers)
#     return estimate_pose()


def run(config: Config) -> None:
    """Run the Locate → Poll → Draw loop forever."""
    client = ServerClient(config)
    print(f"[{config.name}] starting; server = {config.server_url}")

    while True:
        # --- Locate ---------------------------------------------------------
        pose = estimate_pose()
        while not pose:
            execute_commands([SpinCommand(degrees=10)])
            pose = estimate_pose()

        # --- Poll -----------------------------------------------------------
        job: Optional[DrawJob] = None
        while job is None:
            job = client.check_in("ready", pose)
            if job is None:
                time.sleep(config.poll_interval_seconds)

        # --- Draw -----------------------------------------------------------
        print(f"[{config.name}] drawing {job.jobId} ({len(job.commands)} commands)")
        print(job.navigateTo)
        navigate_to(job.navigateTo, pose)
        execute_commands(job.commands)
        execute_commands(job.exitPath)
        # loop back to Locate


if __name__ == "__main__":

    import argparse

    parser = argparse.ArgumentParser(description="Doodlebot client")
    parser.add_argument("--name", required=True, help="this bot's unique name")
    parser.add_argument(
        "--server", default="https://doodlebot.media.mit.edu", help="server base URL"
    )
    args = parser.parse_args()
    setup_aruco_client(args.name, marker_map, 0.08)

    run(Config(name=args.name, server_url=args.server))
