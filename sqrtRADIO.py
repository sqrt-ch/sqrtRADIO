#!/usr/bin/env python3
"""
sqrtRADIO: Python M3U Player with Claude AI
Replicates m3u.js (Martin Ambauen, https://www.sqrt.ch/Radio/m3u)

Dependencies:
    FFmpeg https://ffmpeg.org/
    pip install requests sounddevice numpy
    requests>=2.28
    sounddevice>=0.4
    numpy>=1.24

Usage:
    python sqrtRADIO_dark.py
"""

import tkinter as tk
from tkinter import scrolledtext, filedialog, simpledialog
import subprocess
import threading
import queue
from collections import deque
import shutil
import sys
import webbrowser
import re
import json
import os
from pathlib import Path

import numpy as np
import requests
import sounddevice as sd

if sys.platform == "win32":
    CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW
else:
    CREATE_NO_WINDOW = 0

# -- Audio constants -----------------------------------------------------------
RATE = 44100
CHANNELS = 2
DTYPE = np.int16
CHUNK = 2048  # samples; int16 stereo → 8 192 bytes per block

# -- Icecast ring-buffer constants --------------------------------------------
BLOCKS_PER_SEC = RATE / CHUNK  # ≈ 21.5 audio blocks per second
ICECAST_BUFFER_SECS = 615  # 10-minute client-side ring buffer
ICECAST_MAX_CHUNKS = int(ICECAST_BUFFER_SECS * BLOCKS_PER_SEC)

# -- Preset playlists ------------------------- -------------------------------
# These are the factory defaults, used to seed the presets file on first run.
# After that, PRESETS_FILE is the single source of truth and every button
# (including these) can be overwritten via the "Speichern" button.
DEFAULT_PRESETS = [
    ("Kultur", "https://www.sqrt.ch/Radio/kultur.m3u"),
    ("Langwelle", "https://www.sqrt.ch/Radio/langwelle.m3u"),
    ("HLS", "https://www.sqrt.ch/Radio/hls.m3u"),
    ("Permalink", "https://www.sqrt.ch/Radio/simple.m3u"),
    ("Bartók", "https://www.sqrt.ch/Radio/bartok.m3u"),
    (
        "GitHub HiQ",
        "https://raw.githubusercontent.com/Pulham/Internet-Radio-HQ-URL-playlists/main/Radio%20Stations.m3u",
    ),
]

# Negative = go back in time; positive = go forward toward live
SEEK_STEPS = [-600, -120, -30, -15, -5, +5, +15, +30]

# -- Preset persistence ----------------------------------------------------
def _app_dir() -> Path:
    """Directory the presets file lives in: a per-user config folder.

    Works the same whether run as a script or as a PyInstaller --onefile
    binary, and avoids two pitfalls:
    - sys._MEIPASS (temp extraction dir for onefile builds) is wiped on
      exit, so anything written there is silently lost.
    - the folder next to the executable may not be writable (e.g. an
      install under "Program Files" without admin rights).

    Windows : %APPDATA%\\sqrtRADIO
    macOS   : ~/Library/Application Support/sqrtRADIO
    Linux   : ~/.config/sqrtRADIO
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    app_dir = base / "sqrtRADIO"
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


PRESETS_FILE = _app_dir() / "sqrtRADIO_presets.json"
MAX_PRESETS = 8


# -------------------------------------------------------------------------------
# PCM ring-buffer  (Icecast rewind)
# -------------------------------------------------------------------------------


class PCMBuffer:
    """
    Thread-safe ring buffer for raw PCM blocks.
    One block = CHUNK samples × CHANNELS × 2 bytes (int16 stereo).
    Older blocks are silently dropped once maxlen is reached.
    """

    def __init__(self, maxlen: int):
        self._dq: deque[bytes] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self._dq.clear()

    def append(self, chunk: bytes) -> None:
        with self._lock:
            self._dq.append(chunk)

    def available(self) -> int:
        with self._lock:
            return len(self._dq)

    def tail(self, n: int) -> list[bytes]:
        """Return up to the last *n* chunks (oldest first)."""
        with self._lock:
            lst = list(self._dq)
        return lst[max(0, len(lst) - n) :]


# -------------------------------------------------------------------------------
# Player
# -------------------------------------------------------------------------------


class Player:
    """
    Decodes any stream (MP3, AAC, HLS, ...) via ffmpeg and plays it through
    sounddevice.  All heavy work runs in daemon threads so the GUI stays free.

    Uses a generation counter instead of a single stop-Event so that old
    reader/player threads reliably exit even when a new play() call has
    already cleared the event.  On Windows, proc.stdout is closed *before*
    proc.kill() so that the blocking read() in _reader unblocks immediately.
    """

    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()
        self._volume = 1.0
        self._balance = 0.0  # -1.0 = full left, 0.0 = center, +1.0 = full right
        self._gen = 0  # incremented on every stop/play cycle
        self._q: queue.Queue = queue.Queue(maxsize=100)
        # Icecast live-buffer & replay
        self._pcm_buffer: PCMBuffer | None = None
        self._replay_dq: deque[bytes] = deque()
        self._replay_lock = threading.Lock()

    # -- public --------------------------------------------------------------

    @property
    def volume(self) -> float:
        return self._volume

    @volume.setter
    def volume(self, v: float):
        self._volume = float(np.clip(v, 0.0, 1.0))

    @property
    def balance(self) -> float:
        return self._balance

    @balance.setter
    def balance(self, v: float):
        self._balance = float(np.clip(v, -1.0, 1.0))

    def play(
        self,
        url: str,
        seek_sec: int = 0,
        live_start_index: int | None = None,
        on_error=None,
        on_end=None,
        on_bitrate=None,
        hls_bw: int = 0,
        pcm_buffer: "PCMBuffer | None" = None,
    ):
        """
        Start playback.  Kills any previous stream first.

        live_start_index  -- HLS-only: start N segments from the end of the
                             manifest (negative int).  Preferred over seek_sec
                             for live/DVR HLS because ffmpeg maps it directly
                             to a segment boundary without decoding everything
                             in between.  Ignored when None.
        seek_sec          -- input-side -ss seek (VOD / fallback only).
        pcm_buffer        -- PCMBuffer instance for Icecast rewind; None to
                             disable buffering.
        on_bitrate        -- Callback for updating bitrate display dynamically.
        hls_bw            -- Extracted Bandwidth for HLS streams natively parsed.
        """
        self._kill_current()  # kill old process & advance generation
        gen = self._gen  # snapshot generation for new threads
        self._drain()
        # Attach ring-buffer and clear any stale replay chunks
        self._pcm_buffer = pcm_buffer
        with self._replay_lock:
            self._replay_dq.clear()

        t_r = threading.Thread(
            target=self._reader,
            args=(
                url,
                seek_sec,
                live_start_index,
                on_error,
                on_end,
                on_bitrate,
                hls_bw,
                gen,
            ),
            daemon=True,
        )
        t_p = threading.Thread(target=self._player, args=(on_error, gen), daemon=True)
        t_r.start()
        t_p.start()

    def stop(self):
        self._kill_current()
        self._drain()
        self.detach_buffer()
        with self._replay_lock:
            self._replay_dq.clear()

    # -- internals -----------------------------------------------------------

    def _kill_current(self):
        with self._lock:
            self._gen += 1
            if self._proc is not None:
                try:
                    self._proc.stdout.close()  # unblocks blocking read()
                except Exception:
                    pass
                try:
                    self._proc.stderr.close()  # unblocks blocking stderr read()
                except Exception:
                    pass
                try:
                    self._proc.kill()  # SIGKILL / TerminateProcess
                    self._proc.wait(timeout=3)
                except Exception:
                    pass
                self._proc = None

    def _drain(self):
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    def inject_replay(self, chunks_back: int) -> None:
        """
        Rewind playback by *chunks_back* PCM blocks using the ring-buffer.

        Drains the live queue, then loads the requested tail of the ring-buffer
        into *_replay_dq* so that *_player* consumes it before resuming live
        data from *_q*.  Pass 0 to cancel any active replay (return to live).
        """
        chunks: list[bytes] = (
            self._pcm_buffer.tail(chunks_back)
            if (self._pcm_buffer is not None and chunks_back > 0)
            else []
        )
        self._drain()
        with self._replay_lock:
            self._replay_dq = deque(chunks)

    def buf_available(self) -> int:
        """Chunks currently in the ring-buffer (0 when buffering is disabled)."""
        return self._pcm_buffer.available() if self._pcm_buffer is not None else 0

    def attach_buffer(self, buf: "PCMBuffer | None") -> None:
        self._pcm_buffer = buf

    def detach_buffer(self) -> None:
        if self._pcm_buffer is not None:
            self._pcm_buffer.clear()
        self._pcm_buffer = None

    def _reader(
        self,
        url: str,
        seek_sec: int,
        live_start_index: int | None,
        on_error,
        on_end,
        on_bitrate,
        hls_bw: int,
        gen: int,
    ):
        cmd = ["ffmpeg", "-hide_banner"]

        # Reconnect flags (ignored for local files, harmless)
        cmd += [
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
        ]

        # Segment-accurate HLS seek (VLC-style): jump directly to the
        # Nth-from-the-end segment without decoding frames in between.
        if live_start_index is not None:
            cmd += ["-live_start_index", str(live_start_index)]

        # Fallback: input-side -ss for VOD or non-HLS streams
        elif seek_sec:
            cmd += ["-ss", str(seek_sec)]

        cmd += [
            "-i",
            url,
            "-vn",  # drop video
            "-f",
            "s16le",
            "-ar",
            str(RATE),
            "-ac",
            str(CHANNELS),
            "pipe:1",
        ]

        bytes_per_block = CHUNK * CHANNELS * 2  # int16 = 2 bytes

        try:
            with self._lock:
                if self._gen != gen:
                    return  # superseded before we even started
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=CREATE_NO_WINDOW,
                )
            proc = self._proc

            # Start thread to parse metadata & bitrate from stderr stream dynamically
            if on_bitrate:

                def stderr_reader():
                    buf = ""
                    in_input = False
                    icy_br = ""

                    try:
                        while self._gen == gen:
                            try:
                                c = proc.stderr.read(1)
                                if not c:  # EOF
                                    break
                                c = c.decode("utf-8", errors="ignore")
                            except (OSError, ValueError):
                                break

                            if c == "\r" or c == "\n":
                                line = buf.strip()
                                if line.startswith("Input #"):
                                    in_input = True
                                elif line.startswith("Output #"):
                                    in_input = False

                                if in_input:
                                    if "icy-br" in line:
                                        m = re.search(r"icy-br\s*:\s*([\d.]+)", line)
                                        if m:
                                            icy_br = str(round(float(m.group(1))))

                                    # CODEC + SAMPLE RATE + BITRATE mapping
                                    if "Stream #" in line and "Audio:" in line:
                                        codec_match = re.search(
                                            r"Audio:\s*([a-zA-Z0-9_]+)", line
                                        )
                                        codec = (
                                            codec_match.group(1).upper()
                                            if codec_match
                                            else "AUDIO"
                                        )
                                        if "MP3" in codec:
                                            codec = "MP3"

                                        sr_match = re.search(r"(\d+)\s*Hz", line)
                                        sr = (
                                            f"{int(sr_match.group(1))/1000:g} kHz"
                                            if sr_match
                                            else ""
                                        )

                                        br_match = re.search(
                                            r"(\d+)\s*kb/s", line
                                        ) or re.search(r"(\d+)\s*kbps", line)
                                        br = (
                                            f"{br_match.group(1)} kbps"
                                            if br_match
                                            else ""
                                        )

                                        # Use the extracted bandwidth for HLS streams if missing
                                        if not br and hls_bw > 0:
                                            br = f"{round(hls_bw/1000)} kbps"

                                        # Ignore icy-br for VBR/lossless formats to prevent the 128 kbps nonsense
                                        if (
                                            not br
                                            and icy_br
                                            and codec not in ("FLAC", "ALAC", "WAV")
                                        ):
                                            br = f"{icy_br} kbps"

                                        parts = [p for p in (codec, sr, br) if p]
                                        if parts:
                                            on_bitrate(" • ".join(parts))
                                buf = ""
                            else:
                                buf += c
                    except Exception:
                        pass

                threading.Thread(target=stderr_reader, daemon=True).start()

            while self._gen == gen:
                try:
                    raw = proc.stdout.read(bytes_per_block)
                except Exception:
                    break
                if not raw:
                    break

                if self._pcm_buffer is not None:
                    self._pcm_buffer.append(raw)

                    with self._replay_lock:
                        is_replaying = len(self._replay_dq) > 0

                    if is_replaying:
                        continue

                try:
                    self._q.put(raw, timeout=2)
                except queue.Full:
                    pass  # drop old audio rather than block

            if on_end and self._gen == gen:
                on_end()

        except Exception as exc:
            if on_error and self._gen == gen:
                on_error(str(exc))

    def _player(self, on_error, gen: int):
        try:
            with sd.OutputStream(
                samplerate=RATE,
                channels=CHANNELS,
                dtype=DTYPE,
                blocksize=CHUNK,
            ) as stream:
                while self._gen == gen:
                    # Replay mode: drain replay deque before live queue
                    raw = None
                    with self._replay_lock:
                        if self._replay_dq:
                            raw = self._replay_dq.popleft()
                    if raw is None:
                        try:
                            raw = self._q.get(timeout=0.5)
                        except queue.Empty:
                            continue
                    pcm = np.frombuffer(raw, dtype=DTYPE).reshape(-1, CHANNELS).copy()
                    pcm = (pcm * self._volume).clip(-32768, 32767).astype(DTYPE)
                    b = self._balance
                    gain = np.array(
                        [min(1.0, 1.0 - b), min(1.0, 1.0 + b)], dtype=np.float32
                    )
                    pcm = (pcm * gain).clip(-32768, 32767).astype(DTYPE)
                    stream.write(pcm)
        except Exception as exc:
            if on_error and self._gen == gen:
                on_error(str(exc))


# -------------------------------------------------------------------------------
# Application
# -------------------------------------------------------------------------------


# -------------------------------------------------------------------------------
# Retro 80s Radio Theme
# -------------------------------------------------------------------------------

RETRO = {
    "bg": "#1c1c1c",  # cabinet dark charcoal
    "panel": "#242424",  # slightly lighter panel
    "display_bg": "#0a1a0a",  # dark green VFD background
    "display_fg": "#ff9900",  # amber LED text
    "display_dim": "#7a4400",  # dim amber (inactive)
    "btn_bg": "#333333",  # brushed metal button
    "btn_active": "#444444",
    "btn_fg": "#dddddd",  # button label
    "btn_accent": "#ff6600",  # orange accent (START etc.)
    "btn_accent_fg": "#ffffff",
    "btn_red": "#cc2200",
    "btn_red_fg": "#ffffff",
    "border": "#555555",  # panel border
    "label_fg": "#aaaaaa",  # dim label text
    "section_bg": "#1e1e1e",
    "status_fg": "#ffcc00",  # bright yellow status
    "seek_bg": "#2a2a2a",
    "font_mono": ("Courier", 9, "bold"),
    "font_display": ("Courier", 11, "bold"),
    "font_title": ("Courier", 14, "bold"),
    "font_label": ("Courier", 9),
    "font_small": ("Courier", 8),
}


def _retro_btn(parent, text, command, width=None, bg=None, fg=None, **kw):
    """Create a styled retro button."""
    b = tk.Button(
        parent,
        text=text,
        command=command,
        bg=bg or RETRO["btn_bg"],
        fg=fg or RETRO["btn_fg"],
        activebackground=RETRO["btn_active"],
        activeforeground=RETRO["btn_fg"],
        relief="raised",
        bd=3,
        font=RETRO["font_mono"],
        cursor="hand2",
        **({} if width is None else {"width": width}),
        **kw,
    )
    return b


def _retro_label(parent, text="", textvariable=None, fg=None, font=None, **kw):
    """Create a styled retro label."""
    kwargs = dict(
        bg=RETRO["panel"],
        fg=fg or RETRO["label_fg"],
        font=font or RETRO["font_label"],
    )
    if text:
        kwargs["text"] = text
    if textvariable:
        kwargs["textvariable"] = textvariable
    return tk.Label(parent, **kwargs, **kw)


def _retro_frame(parent, bg=None, **kw):
    return tk.Frame(parent, bg=bg or RETRO["panel"], **kw)


class App:

    def __init__(self, root: tk.Tk):
        self.root = root
        self.player = Player()

        # Playlist state (mirrors JS variables)
        self.m3u_arr: list[str] = []
        self.k: int = 0
        self.simple: bool = False  # "simple" = URL-per-line format
        self.history: list[str] = []  # lm3u equivalent

        # Playback state
        self._current_url = ""
        self._resolved_url = (
            ""  # best HLS variant URL; same as _current_url for non-adaptive streams
        )
        self._hls_bw = 0  # stores HLS bandwidth in bps for UI display
        self._seek_offset = 0  # seconds behind live edge (always ≥ 0)
        self._dvr_window = 0.0  # DVR window size in seconds; 0 = unknown/no DVR
        self._dvr_segments: list[float] = []  # cached #EXTINF durations from manifest
        self._volume = 1.0
        self._balance_val = 0.0

        # Icecast client-side ring-buffer
        self._is_icecast: bool = False
        self._pcm_buffer: PCMBuffer | None = None

        # Pause state
        self._paused = False
        self._pre_pause_vol = 1.0

        # Recording state
        self._rec_proc: subprocess.Popen | None = None

        # All preset buttons (factory + user-saved), single source of truth,
        # loaded from disk (seeded with DEFAULT_PRESETS on first run).
        self.presets: list[tuple[str, str]] = self._load_presets()

        root.title("sqrtRADIO — Vintage M3U Receiver")
        root.geometry("900x780")
        root.minsize(640, 560)
        root.configure(bg=RETRO["bg"])

        self._build_ui()
        self._bind_keys()

        self._seek_permitted = False
        self._start_seek_loop()

        # Load default playlist after UI is ready
        root.after(200, lambda: self._get_m3u("https://www.sqrt.ch/Radio/kultur.m3u"))

    # -- UI construction ------------------------------------------------------

    def _build_ui(self):
        R = RETRO
        P = dict(padx=8, pady=4)

        # ── CABINET TOP HEADER ──────────────────────────────────────────────
        header = _retro_frame(self.root, bg=R["bg"])
        header.pack(fill="x", padx=0, pady=0)

        tk.Label(
            header,
            text="◈  sqrtRADIO  ◈",
            font=R["font_title"],
            bg=R["bg"],
            fg=R["display_fg"],
            anchor="center",
        ).pack(side="left", padx=16, pady=6)

        tk.Label(
            header,
            text="M3U RADIO PLAYER",
            font=R["font_small"],
            bg=R["bg"],
            fg=R["display_dim"],
            anchor="center",
        ).pack(side="left", padx=4)

        # ── PRESET STATION BUTTONS ──────────────────────────────────────────
        sep1 = tk.Frame(self.root, bg=R["border"], height=2)
        sep1.pack(fill="x")

        preset_outer = _retro_frame(self.root, bg=R["bg"])
        preset_outer.pack(fill="x", padx=8, pady=(4, 2))

        tk.Label(
            preset_outer,
            text="PRESETS",
            font=R["font_small"],
            bg=R["bg"],
            fg=R["display_dim"],
        ).pack(side="left", padx=(4, 8))

        self._pr = _retro_frame(preset_outer, bg=R["bg"])
        self._pr.pack(side="left", fill="x")
        self._render_presets()

        self._btn_save_preset = _retro_btn(
            preset_outer,
            text="💾 Speichern",
            command=self._save_preset,
            padx=5,
            pady=2,
        )
        self._btn_save_preset.pack(side="left", padx=(8, 2), pady=2)

        # ── VFD DISPLAY PANEL ───────────────────────────────────────────────
        sep2 = tk.Frame(self.root, bg=R["border"], height=2)
        sep2.pack(fill="x")

        display_outer = _retro_frame(self.root, bg=R["bg"])
        display_outer.pack(fill="x", padx=8, pady=6)

        # VFD frame with inset border effect
        vfd_border = tk.Frame(display_outer, bg=R["border"], bd=0)
        vfd_border.pack(fill="x")
        vfd = tk.Frame(vfd_border, bg=R["display_bg"], bd=0)
        vfd.pack(fill="x", padx=2, pady=2)

        # Station name row
        name_row = tk.Frame(vfd, bg=R["display_bg"])
        name_row.pack(fill="x", padx=8, pady=(6, 1))

        tk.Label(
            name_row,
            text="STATION ",
            font=R["font_small"],
            bg=R["display_bg"],
            fg=R["display_dim"],
        ).pack(side="left")

        self._v_name = tk.StringVar(value="– KEIN SENDER –")
        tk.Label(
            name_row,
            textvariable=self._v_name,
            font=("Courier", 12, "bold"),
            bg=R["display_bg"],
            fg=R["display_fg"],
            anchor="w",
        ).pack(side="left", fill="x", expand=True)

        # URL text area (styled as VFD readout)
        url_row = tk.Frame(vfd, bg=R["display_bg"])
        url_row.pack(fill="x", padx=8, pady=(0, 6))

        tk.Label(
            url_row,
            text="URL ",
            font=R["font_small"],
            bg=R["display_bg"],
            fg=R["display_dim"],
        ).pack(side="left", anchor="n", pady=2)

        url_sub = tk.Frame(url_row, bg=R["display_bg"])
        url_sub.pack(side="left", fill="x", expand=True)

        self._url_box = tk.Text(
            url_sub,
            height=3,
            wrap="word",
            font=("Courier", 9),
            bg=R["display_bg"],
            fg=R["display_fg"],
            insertbackground=R["display_fg"],
            selectbackground=R["display_dim"],
            relief="flat",
            bd=0,
        )
        self._url_box.pack(fill="x")

        copy_url_btn = _retro_btn(
            url_sub, text="KOPIEREN", command=self._copy_url, padx=4, pady=1
        )
        copy_url_btn.pack(anchor="w", pady=2)

        # ── TRANSPORT CONTROLS ──────────────────────────────────────────────
        sep3 = tk.Frame(self.root, bg=R["border"], height=2)
        sep3.pack(fill="x")

        transport_panel = _retro_frame(self.root, bg=R["panel"])
        transport_panel.pack(fill="x", padx=8, pady=6)

        # Row 1: navigation + playback
        tr = _retro_frame(transport_panel, bg=R["panel"])
        tr.pack(fill="x", pady=2)

        tk.Label(
            tr,
            text="NAV",
            font=R["font_small"],
            bg=R["panel"],
            fg=R["display_dim"],
            width=4,
        ).pack(side="left", padx=(4, 2))

        nav_btns = [
            (">>", self.s_plus, 3),
            ("<<", self.s_minus, 3),
            ("UP", self.s_back, 3),
            ("|<", self.s_min, 3),
            (">|", self.s_max, 3),
        ]
        for label, cmd, w in nav_btns:
            _retro_btn(tr, text=label, command=cmd, width=w).pack(side="left", padx=1)

        # Separator
        tk.Frame(tr, bg=R["border"], width=2, height=28).pack(side="left", padx=6)

        tk.Label(
            tr, text="PLAY", font=R["font_small"], bg=R["panel"], fg=R["display_dim"]
        ).pack(side="left", padx=(0, 2))

        # START button — accent orange
        _retro_btn(
            tr,
            text="▶ START",
            command=self.tune,
            width=8,
            bg="#884400",
            fg="#ffffff",
        ).pack(side="left", padx=2)

        # Pause button
        self._btn_pause = _retro_btn(tr, text="II", command=self._toggle_pause, width=3)
        self._btn_pause.pack(side="left", padx=1)

        # Stop button
        _retro_btn(
            tr, text="■", command=self.s_stop, width=3, bg="#442200", fg="#ff6600"
        ).pack(side="left", padx=1)

        # TAB button
        _retro_btn(tr, text="TAB", command=self.on_tab, width=4).pack(
            side="left", padx=1
        )

        # Separator
        tk.Frame(tr, bg=R["border"], width=2, height=28).pack(side="left", padx=6)

        # Record button
        self._btn_rec = _retro_btn(
            tr,
            text="⏺ REC",
            command=self._toggle_record,
            width=7,
            bg="#440000",
            fg="#ff4444",
        )
        self._btn_rec.pack(side="left", padx=2)

        # Row 2: Volume + Balance
        vb_row = _retro_frame(transport_panel, bg=R["panel"])
        vb_row.pack(fill="x", pady=(2, 4))

        tk.Label(
            vb_row,
            text="VOL",
            font=R["font_small"],
            bg=R["panel"],
            fg=R["display_dim"],
            width=4,
        ).pack(side="left", padx=(4, 2))

        _retro_btn(
            vb_row,
            text="−",
            command=lambda: self._set_vol(self._volume - 0.05),
            width=2,
        ).pack(side="left", padx=1)

        self._v_vol = tk.StringVar(value="100 %")
        tk.Label(
            vb_row,
            textvariable=self._v_vol,
            width=6,
            bg=R["display_bg"],
            fg=R["display_fg"],
            font=R["font_mono"],
            relief="sunken",
            bd=1,
        ).pack(side="left", padx=2)

        _retro_btn(
            vb_row,
            text="+",
            command=lambda: self._set_vol(self._volume + 0.05),
            width=2,
        ).pack(side="left", padx=1)

        tk.Frame(vb_row, bg=R["border"], width=2, height=22).pack(side="left", padx=8)

        self._balance_val = 0.0
        tk.Label(
            vb_row, text="BAL", font=R["font_small"], bg=R["panel"], fg=R["display_dim"]
        ).pack(side="left", padx=(0, 2))

        _retro_btn(
            vb_row,
            text="L",
            command=lambda: self._set_bal(self._balance_val - 0.1),
            width=2,
        ).pack(side="left", padx=1)

        self._v_bal = tk.StringVar(value="C")
        tk.Label(
            vb_row,
            textvariable=self._v_bal,
            width=5,
            bg=R["display_bg"],
            fg=R["display_fg"],
            font=R["font_mono"],
            relief="sunken",
            bd=1,
        ).pack(side="left", padx=2)

        _retro_btn(
            vb_row,
            text="R",
            command=lambda: self._set_bal(self._balance_val + 0.1),
            width=2,
        ).pack(side="left", padx=1)

        # ── TIMESHIFT ROW ───────────────────────────────────────────────────
        sep4 = tk.Frame(self.root, bg=R["border"], height=2)
        sep4.pack(fill="x")

        seek_panel = _retro_frame(self.root, bg=R["seek_bg"])
        seek_panel.pack(fill="x", padx=8, pady=4)

        tk.Label(
            seek_panel,
            text="TIMESHIFT",
            font=R["font_small"],
            bg=R["seek_bg"],
            fg=R["display_dim"],
        ).pack(side="left", padx=(8, 6))

        self._seek_btns: list[tk.Button] = []
        for sec in SEEK_STEPS:
            label = f"{sec:+d}s"
            b = _retro_btn(
                seek_panel,
                text=label,
                command=lambda s=sec: self._seek(s),
                width=5,
                bg=R["seek_bg"],
            )
            b.config(state="disabled", fg=R["display_fg"])
            b.pack(side="left", padx=1, pady=3)
            self._seek_btns.append(b)

        tk.Frame(seek_panel, bg=R["border"], width=2, height=22).pack(
            side="left", padx=6
        )

        self._btn_live = _retro_btn(
            seek_panel,
            text="● LIVE",
            command=self._go_live,
            width=7,
            bg="#002200",
            fg="#00cc44",
        )
        self._btn_live.config(state="disabled")
        self._btn_live.pack(side="left", padx=4)
        self._seek_btns.append(self._btn_live)

        # ── STATUS BAR (LED readout) ─────────────────────────────────────────
        sep5 = tk.Frame(self.root, bg=R["border"], height=2)
        sep5.pack(fill="x")

        stat_outer = _retro_frame(self.root, bg=R["display_bg"])
        stat_outer.pack(fill="x", padx=8, pady=4)

        self._v_status = tk.StringVar(value="BEREIT.")
        tk.Label(
            stat_outer,
            textvariable=self._v_status,
            anchor="w",
            bg=R["display_bg"],
            fg=R["status_fg"],
            font=R["font_mono"],
        ).pack(side="left", fill="x", expand=True, padx=6)

        self._v_bitrate = tk.StringVar(value="")
        tk.Label(
            stat_outer,
            textvariable=self._v_bitrate,
            anchor="e",
            bg=R["display_bg"],
            fg=R["display_fg"],
            font=R["font_mono"],
        ).pack(side="right", padx=6)

        # ── KEYBOARD TOGGLE ──────────────────────────────────────────────────
        kb_frm = _retro_frame(self.root, bg=R["bg"])
        kb_frm.pack(fill="x", padx=8, pady=(2, 0))

        self._v_kb = tk.BooleanVar(value=True)
        tk.Checkbutton(
            kb_frm,
            text="TASTATUR",
            variable=self._v_kb,
            command=self._kb_toggle,
            bg=R["bg"],
            fg=R["label_fg"],
            selectcolor=R["panel"],
            activebackground=R["bg"],
            activeforeground=R["display_fg"],
            font=R["font_small"],
        ).pack(side="left")

        self._kb_hint = tk.Label(
            kb_frm,
            text="← ↑ ↓ →  Enter=START  q=■  p=⏸  TAB  r=⏺REC  u=up  1/+=Vol▲  −=Vol▼  b=Bal◀  n=Bal▶  Esc=OFF",
            font=R["font_small"],
            bg=R["bg"],
            fg=R["display_fg"],
        )
        self._kb_hint.pack(side="left", padx=6)

        self._url_box.bind(
            "<Tab>", lambda e: (e.widget.tk_focusNext().focus(), "break")[1]
        )

        # ── M3U DISPLAY ──────────────────────────────────────────────────────
        sep6 = tk.Frame(self.root, bg=R["border"], height=2)
        sep6.pack(fill="x", pady=(4, 0))

        m3u_outer = _retro_frame(self.root, bg=R["bg"])
        m3u_outer.pack(fill="both", expand=True, padx=8, pady=4)

        tk.Label(
            m3u_outer,
            text="M3U WIEDERGABELISTE",
            font=R["font_small"],
            bg=R["bg"],
            fg=R["display_dim"],
        ).pack(anchor="w", padx=4)

        m3u_inner = tk.Frame(m3u_outer, bg=R["border"], bd=0)
        m3u_inner.pack(fill="both", expand=True, padx=0, pady=2)

        self._m3u_box = scrolledtext.ScrolledText(
            m3u_inner,
            height=10,
            font=("Courier", 9),
            state="disabled",
            bg=R["display_bg"],
            fg="#88cc88",
            insertbackground=R["display_fg"],
            selectbackground=R["display_dim"],
            relief="flat",
            bd=4,
        )
        self._m3u_box.pack(fill="both", expand=True, padx=2, pady=2)

        btn_row = _retro_frame(m3u_outer, bg=R["bg"])
        btn_row.pack(anchor="w", pady=4)

        _retro_btn(btn_row, text="KOPIEREN", command=self._copy_m3u, padx=4).pack(
            side="left", padx=2
        )
        _retro_btn(btn_row, text="DATEI", command=self._save_m3u, padx=4).pack(
            side="left", padx=2
        )

    # -- M3U fetch & parse ----------------------------------------------------

    def _get_m3u(self, url: str):
        self._status(f"Lade: {url}")

        def fetch():
            try:
                r = requests.get(url, timeout=10)
                r.raise_for_status()
                self.root.after(0, lambda: self._parse_m3u(r.text, url))
            except Exception as exc:
                self.root.after(0, lambda: self._status(f"Fehler: {exc}"))

        threading.Thread(target=fetch, daemon=True).start()

    def _parse_m3u(self, text: str, url: str):
        """
        Mirrors the JS logic exactly:
          - if text contains commas  → split(','), simpleText=False, k=1
          - otherwise               → split('\n'),  simpleText=True,  k=0
        In standard M3U the comma separates #EXTINF:-1,<name>\n<url>
        so each element after [0] is "<name>\n<url>".
        """
        if "," in text:
            self.m3u_arr = text.split(",")
            self.simple = False
            self.k = 1
        else:
            self.m3u_arr = text.split("\n")
            self.simple = True
            self.k = 0

        self.history.append(url)
        self._write_text(preserve_scroll=False)  # fresh playlist → start at top
        self._status(
            f"Geladen · {len(self.m3u_arr) - (0 if self.simple else 1)} Einträge · {url}"
        )

    def _write_text(self, preserve_scroll: bool = True):
        """Update name label, URL field and M3U box from current index k."""
        if not self.m3u_arr:
            return
        entry = self.m3u_arr[self.k]

        if self.simple:
            self._v_name.set("Simple M3U")
            self._set_url(entry.strip())
        else:
            lines = entry.split("\n")
            self._v_name.set(lines[0].strip() if lines else "?")
            self._set_url(lines[1].strip() if len(lines) > 1 else "")
        self._render_m3u_box(preserve_scroll=preserve_scroll)

    # -- Navigation (mirrors JS sPlus / sMinus / sMin / sMax / sBack) ---------

    def s_plus(self):
        if not self.m3u_arr:
            return
        lo = 0 if self.simple else 1
        self.k = (self.k + 1) if self.k < len(self.m3u_arr) - 1 else lo
        self._write_text()

    def s_minus(self):
        if not self.m3u_arr:
            return
        lo = 0 if self.simple else 1
        self.k = (self.k - 1) if self.k > lo else len(self.m3u_arr) - 1
        self._write_text()

    def s_min(self):
        self.k = 0 if self.simple else 1
        self._write_text()

    def s_max(self):
        if self.m3u_arr:
            self.k = len(self.m3u_arr) - 1
            self._write_text()

    def s_back(self):
        """Go back one entry in the playlist history (mirrors sBack)."""
        if len(self.history) > 1:
            self.history.pop()
        if self.history:
            self._set_url(self.history[-1])

    def s_stop(self):
        self.player.stop()
        self._current_url = ""
        self._seek_offset = 0
        self._dvr_window = 0.0
        self._dvr_segments = []
        self._is_icecast = False
        self._pcm_buffer = None  # release ring-buffer memory
        # Reset pause and status/bitrate
        self._paused = False
        self._v_bitrate.set("")
        self._btn_pause.config(text="II")
        # Stop recording if active gracefully
        if self._rec_proc is not None:
            try:
                self._rec_proc.communicate(input=b"q", timeout=5)
            except Exception:
                self._rec_proc.terminate()
                self._rec_proc.wait(timeout=5)
            self._rec_proc = None
            self._btn_rec.config(text="⏺ REC", fg="#ff4444")
        self._status("■ Gestoppt.")
        self._set_seek_enabled(False)

    # -- Playback -------------------------------------------------------------

    def tune(self):
        """START button / Enter key — mirrors tuneM3U()."""
        url = self._get_url()
        if not url:
            return
        if ".m3u8" in url:
            self._play(url, hls=True)
        elif ".m3u" in url:
            self._get_m3u(url)
        elif ".pls" in url:
            self.on_tab()
        else:
            self._play(url, hls=False)

    def _play(self, url: str, hls: bool, seek_sec: int = 0):
        """
        Start playback.
        On a fresh (seek_sec == 0) HLS play, reset DVR state and probe asynchronously.
        On a seek-initiated play, leave DVR state alone — _seek manages it.
        For adaptive HLS (master playlist), the highest-bandwidth variant is
        resolved in a background thread before handing the URL to ffmpeg.
        """
        self._current_url = url
        self._v_bitrate.set("")
        label = url[:65] + ("…" if len(url) > 65 else "")
        self._status(f"▶  {label}")

        if seek_sec == 0:
            # Fresh station start (or return to live): reset everything
            self._seek_offset = 0
            self._dvr_window = 0.0
            self._dvr_segments = []
            self._hls_bw = 0
            self._resolved_url = url  # reset; thread below updates to best variant
            if hls:
                self._is_icecast = False
                self._pcm_buffer = None
                self._set_seek_enabled(False)
                self._check_dvr_async(url)
            else:
                # Icecast / plain HTTP stream: buffer from the start; enable seek immediately
                self._is_icecast = True
                if self._pcm_buffer is None:
                    self._pcm_buffer = PCMBuffer(ICECAST_MAX_CHUNKS)
                else:
                    self._pcm_buffer.clear()
                self._set_seek_enabled(True)

        if hls:
            # Resolve the highest-bandwidth variant off the GUI thread, then play
            _seek_sec = seek_sec

            def _start():
                resolved, bw = self._resolve_best_hls_variant(url)
                if url == self._current_url:  # still the active station
                    self._resolved_url = resolved
                    self._hls_bw = bw
                else:
                    resolved = url  # stale; fall back to original
                    bw = 0

                self.player.play(
                    resolved,
                    seek_sec=_seek_sec,
                    on_error=lambda e: self.root.after(
                        0, lambda: self._status(f"ERROR: {e}")
                    ),
                    on_bitrate=lambda b: self.root.after(
                        0, lambda: self._v_bitrate.set(b)
                    ),
                    hls_bw=bw,
                )
                buf = self._pcm_buffer
                if buf is not None:
                    self.player.attach_buffer(buf)

            threading.Thread(target=_start, daemon=True).start()
        else:
            self._resolved_url = url
            self._hls_bw = 0
            self.player.play(
                url,
                seek_sec=seek_sec,
                pcm_buffer=self._pcm_buffer,
                on_error=lambda e: self.root.after(
                    0, lambda: self._status(f"ERROR: {e}")
                ),
                on_bitrate=lambda b: self.root.after(0, lambda: self._v_bitrate.set(b)),
                hls_bw=0,
            )

    def on_tab(self):
        url = self._get_url()
        if url:
            webbrowser.open(url)

    # -- Timeshift seek -------------------------------------------------------

    def _probe_dvr(self, url: str) -> tuple[bool, float, list[float]]:
        """
        Determine whether an HLS stream has a DVR window.
        Returns (is_seekable, window_seconds, segment_durations).

        segment_durations is the list of all #EXTINF values found in the
        media playlist — used by _seek to compute live_start_index without
        a second network round-trip.

        Strategy
        --------
        1. Fetch the manifest and parse it directly (fast, no ffmpeg needed).
           - If it is a master playlist, follow the first variant.
           - #EXT-X-PLAYLIST-TYPE EVENT or VOD → DVR confirmed.
           - Sum all #EXTINF durations.  A sliding-window live stream keeps
             only ~3-6 segments (≈ 15-60 s); real DVR manifests list many
             more.  Threshold: > 90 s total, or an EVENT/VOD type tag.
        2. Fallback: ffprobe format=duration (handles non-manifest cases,
           e.g. plain MP3/AAC streams that never have a manifest at all).
        """
        # --- Method 1: manifest parsing ---
        try:
            r = requests.get(url, timeout=8)
            r.raise_for_status()
            text = r.text
            lines = [l.strip() for l in text.splitlines()]

            # Follow first variant if this is a master playlist
            if any(l.startswith("#EXT-X-STREAM-INF") for l in lines):
                base = url.rsplit("/", 1)[0] + "/"
                for line in lines:
                    if line and not line.startswith("#"):
                        variant = line if line.startswith("http") else base + line
                        try:
                            r2 = requests.get(variant, timeout=8)
                            r2.raise_for_status()
                            lines = [l.strip() for l in r2.text.splitlines()]
                        except Exception:
                            pass
                        break

            # Explicit DVR type tag
            has_dvr_tag = any(
                l in ("#EXT-X-PLAYLIST-TYPE:EVENT", "#EXT-X-PLAYLIST-TYPE:VOD")
                for l in lines
            )

            # Collect segment durations
            seg_durs: list[float] = []
            for line in lines:
                if line.startswith("#EXTINF:"):
                    try:
                        seg_durs.append(float(line[8:].split(",")[0]))
                    except ValueError:
                        pass

            total = sum(seg_durs)
            if has_dvr_tag or total > 90:
                return True, total, seg_durs

        except Exception:
            pass

        # --- Method 2: ffprobe fallback (no segment list available) ---
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "quiet",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=CREATE_NO_WINDOW,
            )
            val = result.stdout.strip()
            if val and val.lower() != "n/a":
                dur = float(val)
                if dur > 90:
                    return True, dur, []
        except Exception:
            pass

        return False, 0.0, []

    def _check_dvr_async(self, url: str):
        """
        Probe url in a background thread; enable seek buttons if DVR confirmed.
        Safe to call at any time — silently aborts if url is no longer active.
        """

        def _probe():
            is_dvr, dur, segs = self._probe_dvr(url)

            def _update():
                if url != self._current_url:
                    return  # stale — user switched station
                self._dvr_window = dur if is_dvr else 0.0
                self._dvr_segments = segs
                self._set_seek_enabled(is_dvr)
                if is_dvr:
                    n = len(segs)
                    self._status(
                        f"▶ DVR-Fenster erkannt: {dur:.0f} s "
                        f"({n} Segmente) — Timeshift aktiv"
                    )
                else:
                    self._pcm_buffer = PCMBuffer(ICECAST_MAX_CHUNKS)
                    self.player.attach_buffer(self._pcm_buffer)
                    self._set_seek_enabled(True)
                    self._status("▶ Kein DVR — Timeshift via Client-Puffer aktiv")

            self.root.after(0, _update)

        threading.Thread(target=_probe, daemon=True).start()

    def _resolve_best_hls_variant(self, url: str) -> tuple[str, int]:
        """
        If *url* is a HLS master playlist (#EXT-X-STREAM-INF present), return
        the variant URL with the highest BANDWIDTH value — exactly what VLC does.
        Returns the original *url* and `0` bandwidth if it's already a media playlist,
        not HLS, or if the request fails.
        """
        try:
            r = requests.get(url, timeout=8)
            r.raise_for_status()
            lines = [l.strip() for l in r.text.splitlines()]
            if not any(l.startswith("#EXT-X-STREAM-INF") for l in lines):
                return url, 0  # already a media playlist

            base = url.rsplit("/", 1)[0] + "/"
            best_bw, best_url = -1, url
            i = 0
            while i < len(lines):
                if lines[i].startswith("#EXT-X-STREAM-INF"):
                    bw = 0
                    for part in lines[i][len("#EXT-X-STREAM-INF:") :].split(","):
                        if part.startswith("BANDWIDTH="):
                            try:
                                bw = int(part.split("=", 1)[1])
                            except ValueError:
                                pass
                    j = i + 1
                    while j < len(lines) and (not lines[j] or lines[j].startswith("#")):
                        j += 1
                    if j < len(lines):
                        variant = (
                            lines[j] if lines[j].startswith("http") else base + lines[j]
                        )
                        if bw > best_bw:
                            best_bw, best_url = bw, variant
                    i = j + 1
                else:
                    i += 1
            return best_url, max(0, best_bw)
        except Exception:
            return url, 0

    def _seek(self, delta_sec: int):
        """
        Seek relative to the current live-edge offset — VLC-style.
        ...
        """
        if not self._current_url:
            return

        # Icecast streams use the client-side ring-buffer — no network probe needed
        if self._is_icecast or (self._pcm_buffer is not None and self._dvr_window == 0):
            self._seek_icecast(delta_sec)
            return

        new_offset = max(0, self._seek_offset - delta_sec)
        url = self._current_url  # snapshot before threading

        if new_offset == 0:
            self._seek_offset = 0
            self._status("▶ Live …")
            self.player.play(
                url,
                on_error=lambda e: self.root.after(
                    0, lambda: self._status(f"ERROR: {e}")
                ),
                on_bitrate=lambda b: self.root.after(0, lambda: self._v_bitrate.set(b)),
                hls_bw=self._hls_bw,
            )
            return

        self._status(f"⏳ Suche {new_offset} s hinter Live-Kante …")
        self._set_seek_enabled(False)  # prevent repeated clicks while probing

        # Use cached segment list if available, else probe
        cached_segs = self._dvr_segments if self._dvr_window > 0 else None

        def do_seek():
            if cached_segs is not None:
                is_dvr = True
                dvr_dur = self._dvr_window
                segs = cached_segs
            else:
                is_dvr, dvr_dur, segs = self._probe_dvr(url)

            def apply():
                if url != self._current_url:
                    return  # stale

                if not is_dvr:
                    self._seek_offset = 0
                    self._dvr_window = 0.0
                    self._dvr_segments = []
                    self._set_seek_enabled(False)
                    self._status("⚠ Kein DVR erkannt — Timeshift nicht möglich.")
                    return

                # Clamp offset to what's actually in the manifest
                behind = min(new_offset, max(0.0, dvr_dur - 2))

                # Walk from the live edge backward until we've covered
                # 'behind' seconds, counting segments from the end.
                if segs:
                    acc = 0.0
                    seg_idx = 0  # segments from end (0 = live edge)
                    for dur in reversed(segs):
                        acc += dur
                        seg_idx += 1
                        if acc >= behind:
                            break
                    live_start = -seg_idx  # negative = from end
                    avg_dur = sum(segs) / len(segs)
                else:
                    # No segment list from manifest (ffprobe fallback path):
                    # estimate using a typical 4-second segment duration
                    avg_dur = 4.0
                    seg_idx = max(1, round(behind / avg_dur))
                    live_start = -seg_idx

                self._seek_offset = int(behind)
                self._dvr_window = dvr_dur
                self._dvr_segments = segs
                self._set_seek_enabled(True)
                self._status(
                    f"◀ {behind:.0f} s hinter Live  "
                    f"(Segment -{seg_idx}, DVR: {dvr_dur:.0f} s)"
                )
                # Use the pre-resolved best-variant URL so ffmpeg doesn't fall
                # back to the lowest bitrate when replaying the master playlist.
                play_url = self._resolved_url if self._current_url == url else url
                self.player.play(
                    play_url,
                    live_start_index=live_start,
                    on_error=lambda e: self.root.after(
                        0, lambda: self._status(f"ERROR: {e}")
                    ),
                    on_bitrate=lambda b: self.root.after(
                        0, lambda: self._v_bitrate.set(b)
                    ),
                    hls_bw=self._hls_bw,
                )

            self.root.after(0, apply)

        threading.Thread(target=do_seek, daemon=True).start()

    def _seek_icecast(self, delta_sec: int) -> None:
        """
        Rewind / fast-forward an Icecast stream using the client-side ring-buffer.
        No network round-trip needed — audio was already captured locally while
        listening.  Calculates the new replay position, loads the buffer tail into
        the player's replay deque, and lets the player thread do the rest.
        """
        if self._pcm_buffer is None:
            return

        avail_chunks = self.player.buf_available()
        avail_secs = avail_chunks / BLOCKS_PER_SEC

        new_offset = max(0, self._seek_offset - delta_sec)
        new_offset = min(new_offset, int(avail_secs))  # clamp to buffered window
        chunks_back = int(new_offset * BLOCKS_PER_SEC)

        self._seek_offset = new_offset
        self.player.inject_replay(chunks_back)

        if new_offset == 0:
            self._status("▶ Live")
        else:
            self._status(
                f"◀ {new_offset} s hinter Live  "
                f"(Puffer: {int(avail_secs)} s / {ICECAST_BUFFER_SECS} s max)"
            )
        self._set_seek_enabled(True)

    def _go_live(self):
        """Jump back to the live edge (reset seek offset to 0)."""
        if not self._current_url:
            return
        self._seek_offset = 0
        if self._is_icecast or (self._pcm_buffer is not None and self._dvr_window == 0):
            # Clear replay deque — player falls through to live _q immediately
            self.player.inject_replay(0)
            self._status("▶ Live")
            self._set_seek_enabled(True)
        else:
            self._status("▶ Live …")
            self._play(self._current_url, hls=True, seek_sec=0)

    # -- Pause ----------------------------------------------------------------

    def _toggle_pause(self):
        if not self._paused:
            self._pre_pause_vol = self._volume
            self._set_vol(0.0)
            self._paused = True
            self._btn_pause.config(text="▶")
            self._status("⏸ Pausiert (Stummschaltung).")
        else:
            self._set_vol(self._pre_pause_vol)
            self._paused = False
            self._btn_pause.config(text="II")
            self._status("▶ Wiedergabe fortgesetzt.")

    # -- Recording ------------------------------------------------------------

    def _toggle_record(self):
        if self._rec_proc is not None:
            # Stop recording gracefully
            try:
                self._rec_proc.communicate(input=b"q", timeout=5)
            except Exception:
                self._rec_proc.terminate()
                self._rec_proc.wait(timeout=5)
            self._rec_proc = None
            self._btn_rec.config(text="⏺ REC", fg="#ff4444")
            self._status("Aufnahme gestoppt.")
        else:
            # Start recording
            url = self._current_url
            if not url:
                self._status("Kein Stream aktiv – erst START drücken.")
                return

            # .mkv is the most robust universal container for copying raw codecs (MP3, AAC, Ogg, etc.)
            path = filedialog.asksaveasfilename(
                title="Wähle das richtige Audioformat!",
                filetypes=[
                    ("Universal Audio (MKV)", "*.mkv"),
                    ("MP3 Stream", "*.mp3"),
                    ("MPEG-TS (HLS)", "*.ts"),
                    ("AAC Stream", "*.aac"),
                    ("M4A Stream", "*.m4a"),
                    ("OPUS Audio", "*.opus"),
                    ("FLAC Audio", "*.flac"),
                    ("OGG Audio", "*.ogg"),
                    ("Alle Dateien", "*"),
                ],
                defaultextension=".mkv",
                initialfile="sqrtRADIO.mkv",
            )
            if not path:
                return

            self._rec_path = path

            # Compute HLS seek args so recording starts at the timeshifted
            # position, not at the live edge.  Mirrors the logic in _seek.
            hls_seek_args: list[str] = []
            if self._seek_offset > 0:
                segs = self._dvr_segments
                behind = self._seek_offset
                if segs:
                    acc, seg_idx = 0.0, 0
                    for dur in reversed(segs):
                        acc += dur
                        seg_idx += 1
                        if acc >= behind:
                            break
                    hls_seek_args = ["-live_start_index", str(-seg_idx)]
                elif self._dvr_window > 0:
                    # Fallback when no segment list available
                    seek_from_start = max(0, int(self._dvr_window - behind))
                    hls_seek_args = ["-ss", str(seek_from_start)]

            # Use the pre-resolved best-variant URL so the recording captures the
            # highest available bitrate, not the lowest (ffmpeg default).
            rec_url = (
                self._resolved_url
                if self._resolved_url and self._current_url == url
                else url
            )
            cmd = [
                "ffmpeg",
                "-y",
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_delay_max",
                "5",
                *hls_seek_args,
                "-i",
                rec_url,
                "-vn",  # Drop video streams just in case
                "-c",
                "copy",  # Raw bit-exact stream copy
                path,
            ]
            try:
                self._rec_proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                )
                self._btn_rec.config(text="⏹ STOP", fg="#ffffff")
                self._status(f"⏺ Aufnahme läuft → {path}")
            except Exception as exc:
                self._status(f"Aufnahme-Fehler: {exc}")

    # -- Volume ---------------------------------------------------------------

    def _set_vol(self, v: float):
        self._volume = float(np.clip(v, 0.0, 1.0))
        self.player.volume = self._volume
        self._v_vol.set(f"{round(self._volume * 100)} %")

    # -- Balance --------------------------------------------------------------

    def _set_bal(self, v: float):
        self._balance_val = float(np.clip(v, -1.0, 1.0))
        self.player.balance = self._balance_val
        if abs(self._balance_val) < 0.05:
            self._v_bal.set("C")
        elif self._balance_val < 0:
            self._v_bal.set(f"L{abs(round(self._balance_val * 100))}")
        else:
            self._v_bal.set(f"R{round(self._balance_val * 100)}")

    # -- Clipboard / File -----------------------------------------------------

    def _copy_url(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self._get_url())

    def _export_m3u_text(self) -> str:
        """Reconstruct valid M3U/plain-URL text from self.m3u_arr.

        This is the single source of truth for anything the user downloads
        or copies — it must stay byte-for-byte playable in VLC etc.,
        independent of how the M3U box chooses to *display* the playlist.
        """
        if not self.m3u_arr:
            return ""
        if self.simple:
            return "\n".join(self.m3u_arr)
        return ",".join(self.m3u_arr)

    def _copy_m3u(self):
        content = self._export_m3u_text()
        self.root.clipboard_clear()
        self.root.clipboard_append(content)

    def _save_m3u(self):
        content = self._export_m3u_text()
        path = filedialog.asksaveasfilename(
            defaultextension=".m3u",
            filetypes=[("M3U playlist", "*.m3u"), ("All files", "*.*")],
            initialfile="sqrt.m3u",
        )
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            self._status(f"Gespeichert: {path}")

    # -- Presets (single JSON-backed source, factory + user-saved) ------------

    def _load_presets(self) -> list[tuple[str, str]]:
        """Load (label, url) presets from the JSON file; seed it on first run."""
        try:
            with open(PRESETS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            presets = [(item["label"], item["url"]) for item in data]
            if presets:
                return presets[-MAX_PRESETS:]
        except Exception:
            pass
        # First run / unreadable file: seed with factory defaults and persist.
        seeded = list(DEFAULT_PRESETS)[-MAX_PRESETS:]
        self.presets = seeded
        self._persist_presets()
        return seeded

    def _persist_presets(self):
        """Write the current presets list to disk as JSON."""
        try:
            data = [{"label": label, "url": url} for label, url in self.presets]
            with open(PRESETS_FILE, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        except Exception as exc:
            self._status(f"Fehler beim Speichern der Presets: {exc}")

    def _load_preset_url(self, label: str, url: str):
        """Preset button click: dispatch by URL type (mirrors tune()).

        .m3u8 is a live HLS manifest and must NOT go through _get_m3u/
        _parse_m3u, since that splits the text into a playlist — which is
        meaningless for a single HLS stream and corrupts the entry.

        For direct streams (HLS/.pls/raw), the station name comes from the
        button label since there's no playlist metadata to derive it from.
        For .m3u playlists, _get_m3u/_write_text fills in the real station
        name from the file itself, so the label is not used there.
        """
        if ".m3u8" in url:
            self._set_url(url)
            self._v_name.set(label)
            self._play(url, hls=True)
        elif ".m3u" in url:
            self._get_m3u(url)
        elif ".pls" in url:
            self._set_url(url)
            self._v_name.set(label)
            self.on_tab()
        else:
            self._set_url(url)
            self._v_name.set(label)
            self._play(url, hls=False)

    def _render_presets(self):
        """Rebuild the row of preset buttons from self.presets."""
        for child in self._pr.winfo_children():
            child.destroy()
        for label, url in self.presets:
            b = _retro_btn(
                self._pr,
                text=label,
                command=lambda l=label, u=url: self._load_preset_url(l, u),
                padx=5,
                pady=2,
            )
            b.pack(side="left", padx=2, pady=2)

    def _save_preset(self):
        """Save the current URL-box content as a new preset button.

        Asks the user for a label, then appends it to the presets row.
        Oldest entry is dropped once MAX_PRESETS is exceeded (FIFO) — this
        can eventually push out factory-default buttons too.
        """
        url = self._get_url()
        if not url:
            self._status("Kein Inhalt im URL-Feld zum Speichern.")
            return

        label = simpledialog.askstring(
            "Preset speichern", "Name für diesen Button:", parent=self.root
        )
        if not label:
            return
        label = label.strip()
        if not label:
            return

        self.presets.append((label, url))
        if len(self.presets) > MAX_PRESETS:
            self.presets.pop(0)  # drop oldest (could be a factory default)

        self._persist_presets()
        self._render_presets()
        self._status(f"Preset gespeichert: {label}")

    # -- Keyboard -------------------------------------------------------------

    def _kb_toggle(self):
        active = self._v_kb.get()
        self._kb_hint.config(fg=RETRO["display_fg"] if active else RETRO["display_dim"])

    def _on_click(self, event):
        if not isinstance(event.widget, tk.Text):
            self.root.focus_set()

    def _bind_keys(self):
        self.root.bind("<KeyPress>", self._on_key)
        self.root.bind("<Button-1>", self._on_click)

    def _on_key(self, event):
        if not self._v_kb.get():
            return
        # Let the user type in text fields without triggering transport
        if isinstance(event.widget, tk.Text):
            return
        k = event.keysym
        if k == "Return":
            self.tune()
        elif k == "Right":
            self.s_plus()
        elif k == "Left":
            self.s_minus()
        elif k == "Up":
            self.s_min()
        elif k == "Down":
            self.s_max()
        elif k == "Tab":
            self.on_tab()
        elif k.lower() == "u":
            self.s_back()
        elif k.lower() == "q":
            self.s_stop()
        elif k.lower() == "p":
            self._toggle_pause()
        elif k.lower() == "r":
            self._toggle_record()
        elif k in ("plus", "equal", "1"):
            self._set_vol(self._volume + 0.05)
        elif k in ("minus", "underscore"):
            self._set_vol(self._volume - 0.05)
        elif k.lower() == "b":
            self._set_bal(self._balance_val - 0.1)
        elif k.lower() == "n":
            self._set_bal(self._balance_val + 0.1)
        elif k == "Escape":
            self._v_kb.set(False)
            self._kb_toggle()

    # -- Helpers --------------------------------------------------------------

    def _set_url(self, text: str):
        self._url_box.config(state="normal")
        self._url_box.delete("1.0", "end")
        self._url_box.insert("1.0", text)

    def _get_url(self) -> str:
        return self._url_box.get("1.0", "end").strip()

    def _render_m3u_box(self, preserve_scroll: bool = True):
        """Render self.m3u_arr as a readable list with clickable entries.

        Purely a display concern — never used for copy/save (see
        _export_m3u_text). Both the station name and its URL are clickable
        and jump the player to that playlist entry, reusing the same
        type-aware dispatch as preset buttons (_load_preset_url) so
        .m3u8/.pls entries inside a playlist don't get mis-handled either.

        Rebuilding the box clears+reinserts all text, which would otherwise
        reset the scroll position to the top on every click — annoying on
        long playlists. preserve_scroll restores the prior scroll offset.
        """
        box = self._m3u_box
        scroll_pos = box.yview()[0] if preserve_scroll else 0.0

        box.config(state="normal")
        box.delete("1.0", "end")

        if not self.m3u_arr:
            box.config(state="disabled")
            return

        # Tag styling: configured once per box instance is fine to repeat,
        # tag_config is idempotent.
        box.tag_config("entry_name", foreground=RETRO["display_fg"], underline=True)
        box.tag_config(
            "entry_url",
            foreground="#88cc88",
            underline=True,
        )
        box.tag_config(
            "entry_current",
            background=RETRO["display_dim"],
        )
        for clickable_tag in ("entry_name", "entry_url"):
            box.tag_bind(
                clickable_tag, "<Enter>", lambda e: box.config(cursor="hand2")
            )
            box.tag_bind(clickable_tag, "<Leave>", lambda e: box.config(cursor=""))

        indices = range(1, len(self.m3u_arr)) if not self.simple else range(
            len(self.m3u_arr)
        )

        for idx in indices:
            entry = self.m3u_arr[idx]
            if self.simple:
                name = None
                url = entry.strip()
            else:
                lines = entry.split("\n")
                name = lines[0].strip() if lines else ""
                url = lines[1].strip() if len(lines) > 1 else ""
            if not url:
                continue

            line_start = box.index("end-1c")
            entry_tag = f"entry_{idx}"  # shared click target for name + url
            box.tag_bind(
                entry_tag,
                "<Button-1>",
                lambda e, i=idx: self._jump_to_m3u_entry(i),
            )

            if name:
                box.insert("end", f"{name}\n", ("entry_name", entry_tag))
            box.insert("end", f"  {url}\n", ("entry_url", entry_tag))

            if idx == self.k:
                line_end = box.index("end-1c")
                box.tag_add("entry_current", line_start, line_end)
                current_line_start = line_start

        box.config(state="disabled")
        if preserve_scroll and current_line_start is not None:
            box.see(current_line_start)
        elif preserve_scroll:
            box.yview_moveto(scroll_pos)

    def _jump_to_m3u_entry(self, idx: int):
        """Click on a URL inside the M3U box: play that playlist entry.

        Reuses _load_preset_url's type dispatch since a playlist entry can
        itself point at a nested .m3u/.m3u8/.pls, not just a raw stream.
        """
        if not self.m3u_arr or not (0 <= idx < len(self.m3u_arr)):
            return
        entry = self.m3u_arr[idx]
        if self.simple:
            url = entry.strip()
            label = "Simple M3U"
        else:
            lines = entry.split("\n")
            label = lines[0].strip() if lines else "?"
            url = lines[1].strip() if len(lines) > 1 else ""
        if not url:
            return
        self.k = idx
        self._load_preset_url(label, url)
        # If the entry is itself a nested .m3u, _get_m3u() fetches it async
        # and will replace self.m3u_arr + re-render on completion — doing it
        # again here right now would just redraw stale data. Only refresh
        # immediately for the synchronous play paths (direct stream/HLS/pls).
        if not (".m3u" in url and ".m3u8" not in url):
            self._render_m3u_box()

    def _set_seek_enabled(self, enabled: bool):
        self._seek_permitted = enabled
        if not enabled or not self._current_url:
            for b in self._seek_btns:
                b.config(state="disabled")
            return

        if self._is_icecast or (self._pcm_buffer is not None and self._dvr_window == 0):
            avail_chunks = self.player.buf_available()
            max_back = avail_chunks / BLOCKS_PER_SEC
        elif self._dvr_window > 0:
            max_back = self._dvr_window
        else:
            for b in self._seek_btns:
                b.config(state="disabled")
            return

        for i, sec in enumerate(SEEK_STEPS):
            b = self._seek_btns[i]
            if sec < 0:
                # Rewind button: only valid if target offset stays within available window history
                if self._seek_offset - sec <= max_back:
                    b.config(state="normal")
                else:
                    b.config(state="disabled")
            else:
                # Forward button: only valid if we are currently behind live edge
                if self._seek_offset > 0:
                    b.config(state="normal")
                else:
                    b.config(state="disabled")

        # LIVE button
        if self._seek_offset > 0:
            self._btn_live.config(state="normal")
        else:
            self._btn_live.config(state="disabled")

    def _start_seek_loop(self):
        if getattr(self, "_seek_permitted", False):
            self._set_seek_enabled(True)
        self.root.after(1000, self._start_seek_loop)

    def _status(self, msg: str):
        self._v_status.set(msg)


# -------------------------------------------------------------------------------
# Entry point
# -------------------------------------------------------------------------------


def main():
    if not shutil.which("ffmpeg"):
        print(
            "ERROR: ffmpeg not found on PATH.\n"
            "Install it from https://ffmpeg.org/download.html "
            "and make sure it is on your PATH.",
            file=sys.stderr,
        )
        sys.exit(1)

    if sys.platform == "win32":
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)

    root = tk.Tk()
    app = App(root)

    def on_closing():
        app.player.stop()
        if app._rec_proc:
            try:
                app._rec_proc.communicate(input=b"q", timeout=5)
            except Exception:
                try:
                    app._rec_proc.terminate()
                except Exception:
                    pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()