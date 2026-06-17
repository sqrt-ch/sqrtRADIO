# sqrtRADIO

**English** | [Deutsch](#deutsch)

---

## 🎙️ M3U Playlist Player — HLS, Icecast, MP3, and More

sqrtRADIO is a Python-based M3U playlist radio player featuring a polished retro 80s theme, advanced timeshift capabilities for DVR (Digital Video Recording) streams, and local buffering for Icecast/HTTP radio stations. It replicates and expands the functionality of [m3u.js](https://www.sqrt.ch/Radio/m3u) with a full-featured, hardware-inspired GUI.

### Features

- **M3U Playlist Support** – Load HLS, Icecast, MP3, AAC, and other audio streams from M3U playlists.
- **HLS/DVR Playback** – Play live HLS streams with optional timeshift and rewind capabilities.
- **Advanced Timeshift** – Rewind/fast-forward on streams that support DVR windows.
- **Client-Side Buffering** – Icecast streams can be rewound up to 10 minutes using a local ring buffer.
- **Preset Persistence & Customization** – Overwrite and save your own stream URLs directly to custom preset slots. Buttons persist locally across application restarts.
- **Clipboard & File Export** – Quickly copy the currently playing station URL or the entire loaded M3U playlist to your clipboard, or export playlists as files.
- **Keyboard Shortcuts & Toggle** – Full keyboard control for navigation and playback, which can be dynamically enabled or disabled via a GUI checkbox.
- **Volume & Balance Control** – Adjust left/right balance and volume in real-time.
- **Stream Recording** – Record any stream to MKV, MP3, AAC, OPUS, FLAC, OGG or other formats.
- **Bitrate Detection** – Detects codec (e.g. MP3, FLAC, AAC), sample rate (kHz), and bitrate (kbps) from FFmpeg.
- **Pause/Resume** – Mute and pause playback while maintaining stream state.

### Requirements

- **Python 3.10+**
- **FFmpeg** – [Download from ffmpeg.org](https://ffmpeg.org/download.html)
- Python packages: `requests`, `sounddevice`, `numpy`

### Installation

1. **Install FFmpeg**
   - **macOS**: `brew install ffmpeg`
   - **Ubuntu/Debian**: `sudo apt-get install ffmpeg`
   - **Windows**: Download from [ffmpeg.org](https://ffmpeg.org/download.html) and add to PATH

2. **Install Python Dependencies**
   ```bash
   pip install requests sounddevice numpy