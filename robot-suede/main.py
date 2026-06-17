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

HOST = "127.0.0.1"
PORT = 5000


def send(msg):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((HOST, PORT))
        s.sendall(("fromDoodlebotAILive|" + msg + "\n").encode())


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


def estimate_pose(known: list[ArucoMarker]) -> Pose:
    """Solve for the bot's pose in the global frame from seen vs. known markers."""
    raise NotImplementedError("pose estimation is not implemented on this stub")


def navigate_to(target: Pose, current: Pose) -> None:
    """Drive the bot from ``current`` to ``target`` (pen up)."""
    raise NotImplementedError("navigation is not implemented on this stub")


def send_command(cmd: str) -> None:
    print(f"Sending: {cmd}")
    send(cmd)


def execute_commands(commands: list[DrawingCommand]) -> None:
    """Issue line/spin/arc commands to the drive + pen, in order."""
    for cmd in commands:
        if isinstance(cmd, ArcCommand):
            send_command(f"(t,{cmd.radius},{cmd.degrees})")

        elif isinstance(cmd, LineCommand):
            pen = 1 if cmd.penDown else 0
            send_command(f"(m,{cmd.distance},{cmd.distance},2000,2000)")

        elif isinstance(cmd, SpinCommand):
            send_command(f"(t,0,{cmd.degrees})")


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


def locate(client: ServerClient) -> Pose:
    """Locate self via aruco code detection (README: ``Locate``)."""
    markers = client.fetch_markers()
    return estimate_pose(markers)


def run(config: Config) -> None:
    """Run the Locate → Poll → Draw loop forever."""
    client = ServerClient(config)
    print(f"[{config.name}] starting; server = {config.server_url}")

    while True:
        # --- Locate ---------------------------------------------------------
        pose = locate(client)

        # --- Poll -----------------------------------------------------------
        job: Optional[DrawJob] = None
        while job is None:
            job = client.check_in("ready", pose)
            if job is None:
                time.sleep(config.poll_interval_seconds)

        # --- Draw -----------------------------------------------------------
        print(f"[{config.name}] drawing {job.jobId} ({len(job.commands)} commands)")
        navigate_to(job.navigateTo, pose)
        execute_commands(job.commands)
        execute_commands(job.exitPath)
        # loop back to Locate


if __name__ == "__main__":
    execute_commands(
        [
            ArcCommand(radius=100, degrees=90),
        ]
    )
    # import argparse

    # parser = argparse.ArgumentParser(description="Doodlebot client")
    # parser.add_argument("--name", required=True, help="this bot's unique name")
    # parser.add_argument(
    #     "--server", default="https://doodlebot.media.mit.edu", help="server base URL"
    # )
    # args = parser.parse_args()

    # run(Config(name=args.name, server_url=args.server))
