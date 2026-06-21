# sqrtRADIO

**English** | [Deutsch](#deutsch)

---

## M3U Playlist Player — HLS, Icecast, MP3, and More

sqrtRADIO is a Python-based M3U playlist radio player with a retro 80s hardware-style GUI, timeshift for DVR streams, and local buffering for Icecast/HTTP stations. It replicates and extends [m3u.js](https://www.sqrt.ch/Radio/m3u).

### Features

- **M3U Playlist Support** – Load HLS, Icecast, MP3, AAC, and other audio streams from M3U playlists.
- **Nested Playlists** – If a playlist entry points to another `.m3u` file, sqrtRADIO loads it as a new playlist instead of playing it as a stream.
- **HLS/DVR Playback** – Play live HLS streams with timeshift and rewind.
- **Client-Side Buffering** – Icecast streams can be rewound up to 10 minutes using a local ring buffer.
- **Preset Persistence** – Save stream URLs to preset slots; they persist across restarts.
- **Clipboard & File Export** – Copy the current station URL or the loaded M3U playlist to clipboard, or export it as a file.
- **Keyboard Shortcuts** – Keyboard control for navigation and playback, can be toggled via a GUI checkbox.
- **Volume & Balance Control** – Adjust left/right balance and volume in real time.
- **Stream Recording** – Record a stream to MKV, MP3, AAC, OPUS, FLAC, OGG, or other formats.
- **Bitrate Detection** – Detects codec (e.g. MP3, FLAC, AAC), sample rate (kHz), and bitrate (kbps) via FFmpeg.
- **Pause/Resume** – Mute and pause playback while keeping stream state.

### Requirements

- **Python 3.10+**
- **FFmpeg** – [Download from ffmpeg.org](https://ffmpeg.org/download.html)
- Python packages: `sounddevice` (all other dependencies are Python standard library)

### Installation

1. **Install FFmpeg**
   - **macOS**: `brew install ffmpeg`
   - **Ubuntu/Debian**: `sudo apt-get install ffmpeg`
   - **Windows**: Download from [ffmpeg.org](https://ffmpeg.org/download.html) and add to PATH

2. **Install Python Dependencies**
   ```bash
   pip install sounddevice
   ```

3. **Run sqrtRADIO**
   ```bash
   python sqrtRADIO.py
   ```

### Usage

#### Basic Playback
1. Select a preset playlist or load your own M3U file.
2. Click **▶ START** to play the selected station.
3. Use arrow keys (← →) or navigation buttons (`<<`, `>>`) to browse through stations.
4. Click **■** (or press **Q**) to stop playback.

#### Managing Presets
1. Enter or modify a stream URL in the VFD display's **URL** text box.
2. Click the **💾 Speichern** button next to the presets row.
3. Enter a custom name for your new preset slot when prompted. Your configuration will automatically save to disk.

#### Keyboard Controls
Toggle the **TASTATUR** checkbox on or off to manage global shortcuts:

| Key | Action |
|-----|--------|
| ← / → | Previous/Next Station |
| ↑ / ↓ | First/Last Station |
| Enter | START playback |
| Q | Stop (■) |
| P | Pause/Resume (⏸) |
| TAB | Open URL in browser |
| U | Go back in history (⇑) |
| R | Toggle Recording (⏺) |
| 1 / + | Volume up |
| − | Volume down |
| B | Balance left |
| N | Balance right |
| Esc | Disable keyboard control |

#### Timeshift (DVR Streams)
- Use the **Timeshift** buttons to rewind/fast-forward in supported streams.
- **-600 s** to **-5 s**: Rewind buttons.
- **+5 s** to **+600 s**: Fast-forward buttons.
- **LIVE**: Return to the live edge.

#### Recording
1. Click **⏺ REC** while a stream is playing.
2. Choose your preferred audio format (MKV, MP3, AAC, etc.) and save location.
3. Click **⏹ STOP** to end recording.

### Technical Details

#### Preset Persistence & Storage
Custom presets are stored as a JSON file named `sqrtRADIO_presets.json`. On the first launch, it is seeded with factory defaults (Kultur, Langwelle, HLS, etc.). Subsequent modifications are saved directly to the user-specific config directory:
- **Windows**: `%APPDATA%\sqrtRADIO`
- **macOS**: `~/Library/Application Support/sqrtRADIO`
- **Linux**: `~/.config/sqrtRADIO`
*Note: A maximum of 8 presets are supported, utilizing a FIFO (First-In, First-Out) queue mechanism once exceeded.*

#### HLS Adaptive Streaming
sqrtRADIO automatically selects the **highest-bandwidth variant** from master playlists, ensuring the best audio quality (similar to VLC behavior).

#### DVR Detection
The player automatically detects DVR capabilities by:
1. Parsing HLS manifests for `#EXT-X-PLAYLIST-TYPE:EVENT` or `#EXT-X-PLAYLIST-TYPE:VOD` tags.
2. Checking total segment duration (>90 seconds indicates DVR).
3. Falling back to `ffprobe` for non-HLS streams.

#### Nested Playlists
When a playlist entry or the URL field itself ends in `.m3u` (not `.m3u8`), `tune()` calls `_get_m3u()` on it instead of `_play()` — the new playlist replaces the current one rather than being played as an audio stream.

#### Icecast Rewind
Non-HLS streams (Icecast, HTTP MP3, etc.) are buffered locally in a 10-minute ring buffer (`PCMBuffer`), allowing seamless rewind without network latency.

#### Recording
Streams are recorded at full bitrate using FFmpeg's `-c copy` option (no re-encoding), preserving original quality.

### License

MIT License – See [LICENSE](LICENSE) file for details.

---

<img src="sqrtRADIO.webp" width="800" alt="sqrtRADIO Screenshot">

<a name="deutsch"></a>

# sqrtRADIO

[English](#m3u-playlist-player--hls-icecast-mp3-and-more) | **Deutsch**

---

## M3U-Wiedergabelisten Abspieler — HLS, Icecast, MP3 und mehr

sqrtRADIO ist ein Python-basierter M3U-Wiedergabelist-Radioplayer mit Retro-80er-Hardware-GUI, Zeitversatz (Timeshift) für DVR-Streams und lokalem Puffer für Icecast/HTTP-Radiostationen. Es repliziert und erweitert [m3u.js](https://www.sqrt.ch/Radio/m3u).

### Funktionen

- **M3U-Wiedergabelisten-Unterstützung** – Laden Sie HLS-, Icecast-, MP3-, AAC- und andere Audioströme aus M3U-Wiedergabelisten.
- **Verschachtelte Playlists** – Zeigt ein Eintrag selbst auf eine weitere `.m3u`-Datei, lädt sqrtRADIO diese als neue Playlist, statt sie als Stream abzuspielen.
- **HLS/DVR-Wiedergabe** – Spielen Sie Live-HLS-Streams mit Zeitversatz und Rücklauf ab.
- **Client-seitiger Puffer** – Icecast-Streams können bis zu 10 Minuten mit einem lokalen Ringpuffer zurückgespult werden.
- **Voreinstellungen-Speicherung** – Speichern Sie Stream-URLs auf Preset-Schaltflächen; sie bleiben nach Programmneustarts erhalten.
- **Zwischenablage & Datei-Export** – Kopieren Sie die aktive Stations-URL oder die geladene M3U-Wiedergabeliste in die Zwischenablage, oder exportieren Sie sie als Datei.
- **Tastaturkürzel** – Tastatursteuerung für Navigation und Wiedergabe, über ein Kontrollkästchen umschaltbar.
- **Lautstärke- und Balancesteuerung** – Passen Sie Balance (L/R) und Lautstärke in Echtzeit an.
- **Stream-Aufnahme** – Zeichnen Sie einen Stream in den Formaten MKV, MP3, AAC, OPUS, FLAC, OGG oder anderen auf.
- **Datenrate-Anzeige** – Bestimmt Kodec (z.B. MP3, FLAC, AAC), Abtastrate (kHz) und Bitrate (kbps) über FFmpeg.
- **Pause/Fortsetzen** – Pausieren (Stummschalten) Sie die Wiedergabe, der Stream-Status bleibt erhalten.

### Anforderungen

- **Python 3.10+**
- **FFmpeg** – [Herunterladen von ffmpeg.org](https://ffmpeg.org/download.html)
- Python-Pakete: `sounddevice` (alle übrigen Abhängigkeiten gehören zur Python-Standardbibliothek)

### Installation

1. **FFmpeg installieren**
   - **macOS**: `brew install ffmpeg`
   - **Ubuntu/Debian**: `sudo apt-get install ffmpeg`
   - **Windows**: Von [ffmpeg.org](https://ffmpeg.org/download.html) herunterladen und zu PATH hinzufügen

2. **Python-Abhängigkeiten installieren**
   ```bash
   pip install sounddevice
   ```

3. **sqrtRADIO ausführen**
   ```bash
   python sqrtRADIO.py
   ```

### Bedienung

#### Grundwiedergabe
1. Wählen Sie eine Wiedergabeliste oder laden Sie Ihre eigene M3U-Datei.
2. Klicken Sie auf **▶ START**, um die gewählte Station abzuspielen.
3. Verwenden Sie die Pfeiltasten (← →) oder die Navigationstasten (`<<`, `>>`) zum Wechseln der Stationen.
4. Klicken Sie auf **■** (oder drücken Sie **Q**), um die Wiedergabe zu stoppen.

#### Presets verwalten und speichern
1. Tragen Sie eine Stream-URL direkt in das **URL**-Textfeld des VFD-Displays ein.
2. Klicken Sie auf die Schaltfläche **💾 Speichern** neben der Preset-Leiste.
3. Geben Sie im Dialogfenster einen Namen für das neue Preset ein. Der Button wird erzeugt und die Konfiguration auf der Festplatte gesichert.

#### Tastatursteuerung
Aktivieren oder deaktivieren Sie die Tastaturshortcuts über das Kontrollkästchen **TASTATUR**:

| Taste | Aktion |
|-------|--------|
| ← / → | Vorherige/Nächste Station |
| ↑ / ↓ | Erste/Letzte Station |
| Enter | START-Wiedergabe |
| Q | Stopp (■) |
| P | Pause/Fortsetzen (⏸) |
| TAB | URL im Browser öffnen |
| U | Im Verlauf zurückgehen (⇑) |
| R | Aufnahme umschalten (⏺) |
| 1 / + | Lautstärke erhöhen |
| − | Lautstärke verringern |
| B | Balance nach links |
| N | Balance nach rechts |
| Esc | Tastatursteuerung deaktivieren |

#### Zeitversatz (DVR-Streams)
- Verwenden Sie die **Timeshift**-Schaltflächen zum Zurückspulen/Vorspulen in unterstützten Streams.
- **-600 s** bis **-5 s**: Rücklauf-Schaltflächen.
- **+5 s** bis **+600 s**: Vorlauf-Schaltflächen.
- **LIVE**: Zurück zum Live-Rand.

#### Aufnahme
1. Klicken Sie auf **⏺ REC**, während ein Stream läuft.
2. Wählen Sie Ihr bevorzugtes Audioformat (MKV, MP3, AAC, etc.) und den Speicherort.
3. Klicken Sie auf **⏹ STOP**, um die Aufnahme zu beenden.

### Technische Details

#### Voreinstellungen-Persistenz & Speicherort
Eigene Presets werden in der JSON-Datei `sqrtRADIO_presets.json` hinterlegt. Beim ersten Start wird diese automatisch mit den Werkseinstellungen (Kultur, Langwelle, HLS etc.) generiert. Alle darauffolgenden Änderungen werden dauerhaft im benutzerspezifischen Konfigurationsordner gesichert:
- **Windows**: `%APPDATA%\sqrtRADIO`
- **macOS**: `~/Library/Application Support/sqrtRADIO`
- **Linux**: `~/.config/sqrtRADIO`
*Hinweis: Es werden maximal 8 Presets unterstützt. Wird dieses Limit überschritten, greift ein FIFO-Verfahren (First-In, First-Out), bei dem das älteste Preset überschrieben wird.*

#### HLS-Adaptive Übertragung
sqrtRADIO wählt automatisch die **Variante mit der höchsten Bitrate** aus Master-Wiedergabelisten, um die beste Audioqualität zu gewährleisten (ähnlich wie VLC).

#### DVR-Erkennung
Der Player erkennt DVR-Funktionen automatisch durch:
1. Analyse von HLS-Manifesten auf `#EXT-X-PLAYLIST-TYPE:EVENT` oder `#EXT-X-PLAYLIST-TYPE:VOD` Tags.
2. Überprüfung der Gesamtsegmentdauer (>90 Sekunden zeigt DVR an).
3. Fallback zu `ffprobe` für Nicht-HLS-Streams.

#### Verschachtelte Playlists
Endet ein Playlist-Eintrag oder der Inhalt des URL-Felds selbst auf `.m3u` (nicht `.m3u8`), ruft `tune()` `_get_m3u()` statt `_play()` auf — die neue Playlist ersetzt die aktuelle, statt als Audiostream abgespielt zu werden.

#### Icecast-Rücklauf
Nicht-HLS-Streams (Icecast, HTTP MP3, etc.) werden lokal in einem 10-Minuten-Ringpuffer (`PCMBuffer`) gepuffert, was nahtloses Zurückspulen ohne Netzwerklatenzen ermöglicht.

#### Aufnahme
Streams werden mit vollständiger Bitrate mit FFmpegs `-c copy`-Option aufgezeichnet (keine erneute Kodierung), um die ursprüngliche Qualität zu bewahren.

### Lizenz

MIT-Lizenz – Weitere Informationen finden Sie in der Datei [LICENSE](LICENSE).