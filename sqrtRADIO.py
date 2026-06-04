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
    python sqrtRADIO.py
"""

import tkinter as tk
from tkinter import scrolledtext, filedialog
import subprocess
import threading
import queue
from collections import deque
import shutil
import sys
import webbrowser

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
PRESETS = [
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
            args=(url, seek_sec, live_start_index, on_error, on_end, gen),
            daemon=True,
        )
        t_p = threading.Thread(target=self._player, args=(on_error, gen), daemon=True)
        t_r.start()
        t_p.start()

    def stop(self):
        self._kill_current()
        self._drain()

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

    def _reader(
        self,
        url: str,
        seek_sec: int,
        live_start_index: int | None,
        on_error,
        on_end,
        gen: int,
    ):
        cmd = ["ffmpeg"]

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
        # This is what every serious HLS player does internally.
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
            "-loglevel",
            "quiet",
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
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                )
            proc = self._proc

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

        root.title("sqrtRADIO")
        root.geometry("740x700")
        root.minsize(620, 520)

        self._build_ui()
        self._bind_keys()

        self._seek_permitted = False
        self._start_seek_loop()

        # Load default playlist after UI is ready
        root.after(200, lambda: self._get_m3u("https://www.sqrt.ch/Radio/ch.m3u"))

    # -- UI construction ------------------------------------------------------

    def _build_ui(self):
        P = dict(padx=6, pady=3)

        # -- Title --
        tk.Label(
            self.root,
            text="sqrtRADIO — M3U-Playlist Empfänger",
            font=(None, 13, "bold"),
            anchor="w",
        ).pack(fill="x", **P)
        tk.Label(
            self.root,
            text="Bitte eine Wiedergabeliste wählen und navigieren.",
            anchor="w",
        ).pack(fill="x", padx=6)

        # -- Preset buttons --
        pr = tk.Frame(self.root)
        pr.pack(fill="x", **P)
        for label, url in PRESETS:
            tk.Button(pr, text=label, command=lambda u=url: self._get_m3u(u)).pack(
                side="left", padx=2, pady=1
            )

        # -- Station name --
        self._v_name = tk.StringVar(value="–")
        tk.Label(
            self.root,
            textvariable=self._v_name,
            font=(None, 10, "bold"),
            anchor="w",
            fg="#003080",
        ).pack(fill="x", padx=6, pady=(4, 0))

        # -- URL display / edit field --
        url_frm = tk.Frame(self.root)
        url_frm.pack(fill="x", **P)
        self._url_box = tk.Text(url_frm, height=3, wrap="word", font=("Courier", 9))
        self._url_box.pack(fill="x")
        tk.Button(url_frm, text="Kopieren", command=self._copy_url).pack(
            anchor="w", pady=2
        )

        # -- Transport row --
        tr = tk.Frame(self.root)
        tr.pack(fill="x", **P)

        transport_left = [
            ("⇒", self.s_plus),
            ("⇐", self.s_minus),
            ("⇑", self.s_back),
            ("START", self.tune),
            ("⇙", self.s_min),
            ("⇘", self.s_max),
        ]
        for label, cmd in transport_left:
            w = 6 if label == "START" else 3
            tk.Button(tr, text=label, width=w, command=cmd).pack(side="left", padx=1)

        # Pause button (next to stop)
        self._btn_pause = tk.Button(tr, text="⏸", width=3, command=self._toggle_pause)
        self._btn_pause.pack(side="left", padx=1)

        # Stop button
        tk.Button(tr, text="■", width=3, command=self.s_stop).pack(side="left", padx=1)

        tk.Button(tr, text="TAB", width=3, command=self.on_tab).pack(
            side="left", padx=1
        )

        # Record button
        self._btn_rec = tk.Button(
            tr, text="⏺ REC", width=7, fg="red", command=self._toggle_record
        )
        self._btn_rec.pack(side="left", padx=(8, 1))

        # -- Volume controls on the same row --
        tk.Label(tr, text="Vol:").pack(side="left", padx=(10, 1))
        tk.Button(
            tr, text="−", width=2, command=lambda: self._set_vol(self._volume - 0.05)
        ).pack(side="left")
        self._v_vol = tk.StringVar(value="100%")
        tk.Label(tr, textvariable=self._v_vol, width=5).pack(side="left")
        tk.Button(
            tr, text="+", width=2, command=lambda: self._set_vol(self._volume + 0.05)
        ).pack(side="left")

        # -- Balance --
        self._balance_val = 0.0
        tk.Label(tr, text="Bal:").pack(side="left", padx=(10, 1))
        tk.Button(
            tr,
            text="L",
            width=2,
            command=lambda: self._set_bal(self._balance_val - 0.1),
        ).pack(side="left")
        self._v_bal = tk.StringVar(value="C")
        tk.Label(tr, textvariable=self._v_bal, width=4).pack(side="left")
        tk.Button(
            tr,
            text="R",
            width=2,
            command=lambda: self._set_bal(self._balance_val + 0.1),
        ).pack(side="left")

        # -- Seek / timeshift row --
        sr = tk.Frame(self.root)
        sr.pack(fill="x", **P)
        tk.Label(sr, text="Timeshift:").pack(side="left", padx=(30, 6))
        self._seek_btns: list[tk.Button] = []
        for sec in SEEK_STEPS:
            label = f"{sec:+d} s"
            b = tk.Button(
                sr, text=label, state="disabled", command=lambda s=sec: self._seek(s)
            )
            b.pack(side="left", padx=1)
            self._seek_btns.append(b)
        # LIVE button – resets to live edge
        self._btn_live = tk.Button(
            sr, text="LIVE", state="disabled", fg="red", command=self._go_live
        )
        self._btn_live.pack(side="left", padx=(12, 1))
        self._seek_btns.append(self._btn_live)

        # -- Status bar --
        self._v_status = tk.StringVar(value="Bereit.")
        tk.Label(
            self.root,
            textvariable=self._v_status,
            anchor="w",
            fg="#003080",
            font=(None, 10),
        ).pack(fill="x", padx=6)

        # -- Keyboard toggle --
        kb_frm = tk.Frame(self.root)
        kb_frm.pack(fill="x", **P)
        self._v_kb = tk.BooleanVar(value=True)
        tk.Checkbutton(
            kb_frm,
            text="Tastatursteuerung",
            variable=self._v_kb,
            command=self._kb_toggle,
        ).pack(side="left")
        self._kb_hint = tk.Label(
            kb_frm,
            text="← ↑ ↓ →  Enter = START  q = ■  p = ⏸ TAB = Tab  r = ⏺ REC\n  u = ⇑  1/+ = Vol▲  − = Vol▼ b = Bal◀ n = Bal▶ Esc = OFF",
            font=(None, 9),
            fg="#003080",
        )
        self._kb_hint.pack(side="left", padx=6)
        self._url_box.bind(
            "<Tab>", lambda e: (e.widget.tk_focusNext().focus(), "break")[1]
        )

        # -- M3U display --
        m3u_frm = tk.LabelFrame(self.root, text="M3U-Wiedergabeliste")
        m3u_frm.pack(fill="both", expand=True, **P)
        self._m3u_box = scrolledtext.ScrolledText(
            m3u_frm, height=12, font=("Courier", 9), state="disabled"
        )
        self._m3u_box.pack(fill="both", expand=True)
        btn_row = tk.Frame(m3u_frm)
        btn_row.pack(anchor="w", pady=2)
        tk.Button(btn_row, text="Kopieren", command=self._copy_m3u).pack(
            side="left", padx=2
        )
        tk.Button(btn_row, text="Datei", command=self._save_m3u).pack(
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
        self._write_text()
        self._status(
            f"Geladen · {len(self.m3u_arr) - (0 if self.simple else 1)} Einträge · {url}"
        )

    def _write_text(self):
        """Update name label, URL field and M3U box from current index k."""
        if not self.m3u_arr:
            return
        entry = self.m3u_arr[self.k]

        if self.simple:
            self._v_name.set("Simple M3U")
            self._set_url(entry.strip())
            self._set_m3u_box("\n".join(self.m3u_arr))
        else:
            lines = entry.split("\n")
            self._v_name.set(lines[0].strip() if lines else "?")
            self._set_url(lines[1].strip() if len(lines) > 1 else "")
            self._set_m3u_box(",".join(self.m3u_arr))

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
        # Reset pause
        self._paused = False
        self._btn_pause.config(text="⏸")
        # Stop recording if active gracefully
        if self._rec_proc is not None:
            try:
                self._rec_proc.communicate(input=b"q", timeout=5)
            except Exception:
                self._rec_proc.terminate()
                self._rec_proc.wait(timeout=5)
            self._rec_proc = None
            self._btn_rec.config(text="⏺ REC", fg="red")
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
        label = url[:65] + ("…" if len(url) > 65 else "")
        self._status(f"▶  {label}")

        if seek_sec == 0:
            # Fresh station start (or return to live): reset everything
            self._seek_offset = 0
            self._dvr_window = 0.0
            self._dvr_segments = []
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
                resolved = self._resolve_best_hls_variant(url)
                if url == self._current_url:  # still the active station
                    self._resolved_url = resolved
                else:
                    resolved = url  # stale; fall back to original
                self.player.play(
                    resolved,
                    seek_sec=_seek_sec,
                    on_error=lambda e: self.root.after(
                        0, lambda: self._status(f"ERROR: {e}")
                    ),
                )
                buf = self._pcm_buffer
                if buf is not None:
                    self.player.attach_buffer(buf)

            threading.Thread(target=_start, daemon=True).start()
        else:
            self._resolved_url = url
            self.player.play(
                url,
                seek_sec=seek_sec,
                pcm_buffer=self._pcm_buffer,
                on_error=lambda e: self.root.after(
                    0, lambda: self._status(f"ERROR: {e}")
                ),
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

    def _resolve_best_hls_variant(self, url: str) -> str:
        """
        If *url* is a HLS master playlist (#EXT-X-STREAM-INF present), return
        the variant URL with the highest BANDWIDTH value — exactly what VLC does.
        Returns *url* unchanged if it is already a media playlist, not HLS, or
        if the request fails.
        """
        try:
            r = requests.get(url, timeout=8)
            r.raise_for_status()
            lines = [l.strip() for l in r.text.splitlines()]
            if not any(l.startswith("#EXT-X-STREAM-INF") for l in lines):
                return url  # already a media playlist
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
            return best_url
        except Exception:
            return url

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

                # --- VLC-style: find segment index, not byte position ---
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
            self._btn_pause.config(text="⏸")
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
            self._btn_rec.config(text="⏺ REC", fg="red")
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

            # -c copy ensures the stream is dumped raw without any re-encoding.
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
                self._btn_rec.config(text="⏹ STOP", fg="darkred")
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

    def _copy_m3u(self):
        content = self._m3u_box.get("1.0", "end")
        self.root.clipboard_clear()
        self.root.clipboard_append(content)

    def _save_m3u(self):
        content = self._m3u_box.get("1.0", "end")
        path = filedialog.asksaveasfilename(
            defaultextension=".m3u",
            filetypes=[("M3U playlist", "*.m3u"), ("All files", "*.*")],
            initialfile="sqrt.m3u",
        )
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            self._status(f"Gespeichert: {path}")

    # -- Keyboard -------------------------------------------------------------

    def _kb_toggle(self):
        active = self._v_kb.get()
        self._kb_hint.config(fg="#003080" if active else "grey")

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

    def _set_m3u_box(self, text: str):
        self._m3u_box.config(state="normal")
        self._m3u_box.delete("1.0", "end")
        self._m3u_box.insert("1.0", text)
        self._m3u_box.config(state="disabled")

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
