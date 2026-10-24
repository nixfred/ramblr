"""Ramblr paths and tunables. Everything is overridable by environment for tests."""
import os
from pathlib import Path

HOME = Path.home()
DATA = Path(os.environ.get("RAMBLR_DATA", HOME / ".local/share/ramblr"))
DB_PATH = Path(os.environ.get("RAMBLR_DB", DATA / "ears.db"))
MODELS = Path(os.environ.get("RAMBLR_MODELS", DATA / "models"))
# Audio spool: tmpfs only (XDG_RUNTIME_DIR is /run/user/<uid>, mode 700, RAM-backed).
SPOOL = Path(os.environ.get("RAMBLR_SPOOL",
                            Path(os.environ.get("XDG_RUNTIME_DIR", "/dev/shm")) / "ramblr"))
NOTIFY_SOCK = Path(os.environ.get("RAMBLR_SOCK", Path(os.environ.get("XDG_RUNTIME_DIR", "/dev/shm")) / "ramblr-notify.sock"))
SPOOL_MAX_BYTES = int(os.environ.get("RAMBLR_SPOOL_MAX", 300 * 1024 * 1024))

NPU_ROOT = HOME / ".local/share/intel-npu"
WHISPER_DIR = Path(os.environ.get("RAMBLR_WHISPER", NPU_ROOT / "models/whisper-base.en"))
WHISPER_DEVICE = os.environ.get("RAMBLR_WHISPER_DEVICE", "NPU")
WHISPER_CACHE = Path(os.environ.get("RAMBLR_WHISPER_CACHE", DATA / "cache" / "whisper"))
WHISPER_FALLBACK = Path(os.environ.get("RAMBLR_WHISPER_FALLBACK", NPU_ROOT / "models/whisper-base.en"))  # if the chosen one breaks
ASR_FALLBACK_FILE = Path(os.environ.get("RAMBLR_ASR_FALLBACK_FILE", DATA / "asr-fallback.json"))
WATCHDOG_FILE = Path(os.environ.get("RAMBLR_WATCHDOG_FILE", DATA / "watchdog.json"))
WATCHDOG_MINUTES = float(os.environ.get("RAMBLR_WATCHDOG_MINUTES", 10))   # speech this long with no line = broken
ASR_MAX_TOKENS = int(os.environ.get("RAMBLR_ASR_MAX_TOKENS", 448))   # distil-large-v3 on the NPU needs <= 440 (static KV cache)

# Mic node name lives in ~/.config/ramblr/config.env (RAMBLR_SOURCE), not in git. Unset = PipeWire default source.
SOURCE = os.environ.get("RAMBLR_SOURCE", "@DEFAULT_SOURCE@")
RATE = 16000

VAD_MODEL = MODELS / "silero_vad.onnx"
SPK_MODEL = MODELS / os.environ.get("RAMBLR_SPK_MODEL", "wespeaker_en_voxceleb_resnet34_LM.onnx")
SPK_MODEL_NAME = "wespeaker_en_voxceleb_resnet34_LM"

# VAD
VAD_THRESHOLD = float(os.environ.get("RAMBLR_VAD_THRESHOLD", 0.5))
VAD_MIN_SILENCE = float(os.environ.get("RAMBLR_VAD_MIN_SILENCE", 0.7))
VAD_MIN_SPEECH = float(os.environ.get("RAMBLR_VAD_MIN_SPEECH", 0.3))
VAD_MAX_SPEECH = float(os.environ.get("RAMBLR_VAD_MAX_SPEECH", 25.0))
VAD_LOOKBACK = float(os.environ.get("RAMBLR_VAD_LOOKBACK", 8.0))  # second look before each onset (wake word under music); 0 = off
VAD_PREROLL = float(os.environ.get("RAMBLR_VAD_PREROLL", 0.4))   # audio kept before each VAD onset (first-word clipping fix)

# Diarization
SPK_MATCH = float(os.environ.get("RAMBLR_SPK_MATCH", 0.65))    # cosine, join an existing voice
SPK_UPDATE = float(os.environ.get("RAMBLR_SPK_UPDATE", 0.85))      # cosine, an auto line may refine a NAMED voiceprint ...
SPK_UPDATE_MARGIN = float(os.environ.get("RAMBLR_SPK_UPDATE_MARGIN", 0.15))   # ... only this far ahead of every other voice
SPK_LEARN_SECONDS = float(os.environ.get("RAMBLR_SPK_LEARN_SECONDS", 2.0))    # ... and only from a line this long
# a line is given a NAMED person only with this similarity AND this lead over every other voice (2026-10-24 calibration
# on the manual tags: leave-one-out margins +0.03..+0.15, so 0.10 keeps doubtful lines unnamed instead of wrong)
SPK_NAMED_MATCH = float(os.environ.get("RAMBLR_SPK_NAMED_MATCH", 0.90))   # 0.80 let TV anchors and kids onto a 2-line Jordan print
SPK_MARGIN = float(os.environ.get("RAMBLR_SPK_MARGIN", 0.10))
# 2026-10-24 call: every voice on the room mic sits in a narrow cone (the two voices on the 2026-10-24 call: centroids cos 0.92), so thin prints
# are unsafe to name from. A named print names lines on its own only from this many samples; until then lines go to
# a cluster and the merge suggestion (cluster >= MERGE_MIN_LINES, closest named print by >= MERGE_MARGIN) asks Fred.
SPK_NAMED_MIN_N = int(os.environ.get("RAMBLR_SPK_NAMED_MIN_N", 8))
SPK_AMBIGUOUS_MARGIN = float(os.environ.get("RAMBLR_SPK_AMBIGUOUS_MARGIN", 0.02))   # a hand sample this close to another person
AMBIGUOUS_WEIGHT = float(os.environ.get("RAMBLR_AMBIGUOUS_WEIGHT", 0.5))           # ... counts 0.5 instead of MANUAL_WEIGHT
MERGE_MIN_LINES = int(os.environ.get("RAMBLR_MERGE_MIN_LINES", 10))
MERGE_MARGIN = float(os.environ.get("RAMBLR_MERGE_MARGIN", 0.04))
MERGE_FLOOR = float(os.environ.get("RAMBLR_MERGE_FLOOR", 0.80))
SPK_MIN_SECONDS = float(os.environ.get("RAMBLR_SPK_MIN_SECONDS", 1.2))
DIAR_INTERVAL = float(os.environ.get("RAMBLR_DIAR_INTERVAL", 5))
DIAR_BATCH = int(os.environ.get("RAMBLR_DIAR_BATCH", 40))

CONFIG_DIR = Path(os.environ.get("RAMBLR_CONFIG", HOME / ".config/ramblr"))

# DO IT webhook (phase 2 ideas). URL and key come only from ~/.config/ramblr/secrets.env; never logged.
DOIT_WEBHOOK_URL = os.environ.get("RAMBLR_DOIT_WEBHOOK_URL", "")
DOIT_WEBHOOK_KEY = os.environ.get("RAMBLR_DOIT_WEBHOOK_KEY", "")
DOIT_KEY_HEADER = os.environ.get("RAMBLR_DOIT_KEY_HEADER", "Authorization")
DOIT_KEY_FORMAT = os.environ.get("RAMBLR_DOIT_KEY_FORMAT", "Bearer {key}")

# Speaker reassign propagation (cosine vs the person's centroid). Basis, measured 2026-10-24 on 30 diarized test
# segments with known voices: segment-to-centroid same voice 0.851-0.955, cross voice <= 0.703; pairwise same voice
# >= 0.639, cross voice <= 0.718. Auto at 0.78 keeps ~0.07 margin on both sides; 0.65-0.78 is flagged for a click.
PROP_HIGH = float(os.environ.get("RAMBLR_PROP_HIGH", 0.78))        # auto-apply
PROP_LOW = float(os.environ.get("RAMBLR_PROP_LOW", 0.65))          # below this: leave alone; between: flag
PROP_CLUSTER = float(os.environ.get("RAMBLR_PROP_CLUSTER", 0.65))  # same unnamed diarizer cluster + this: auto
PROP_MARGIN = float(os.environ.get("RAMBLR_PROP_MARGIN", 0.05))    # must beat a named current speaker by this
MANUAL_WEIGHT = float(os.environ.get("RAMBLR_MANUAL_WEIGHT", 2.0))  # a hand-corrected line counts double in a voiceprint

# Ideas and storyboards
IDEAS_SOCK = Path(os.environ.get("RAMBLR_IDEAS_SOCK", Path(os.environ.get("XDG_RUNTIME_DIR", "/dev/shm")) / "ramblr-ideas.sock"))
IDEA_QUIET = float(os.environ.get("RAMBLR_IDEA_QUIET", 600))          # s after the last line of an idea session before auto-build
IDEA_GAP = float(os.environ.get("RAMBLR_IDEA_GAP", 60))               # s of silence that ends a spoken idea
IDEA_TRIGGER = os.environ.get("RAMBLR_IDEA_TRIGGER",
    r"\blarry\b.{0,60}\b(idea|i(?:'d| would| want| wanna)\s+(?:like\s+)?to\s+(?:work on|build|make|create|design)|what if)\b"
    r"|\b(?:i have|i've got|i got|here'?s) an idea\b")
IDEA_END = os.environ.get("RAMBLR_IDEA_END", r"\b(that'?s it|that is it|that'?s all|that is all)\b")
# Storyboard runtime generator: auto | ollama | claude. See README for the measured comparison.
SB_GEN = os.environ.get("RAMBLR_STORYBOARD_GEN", "claude")
SB_OLLAMA_MODEL = os.environ.get("RAMBLR_STORYBOARD_OLLAMA_MODEL", "gemma3:12b")
SB_OLLAMA_URL = os.environ.get("RAMBLR_OLLAMA_URL", "http://127.0.0.1:11434")
SB_OLLAMA_MAX_GPU = float(os.environ.get("RAMBLR_STORYBOARD_MAX_GPU_PCT", 40))   # defer local runs above this GPU load
SB_CLAUDE_MODEL = os.environ.get("RAMBLR_STORYBOARD_CLAUDE_MODEL", "claude-sonnet-5-5")
SB_CLAUDE_CWD = Path(os.environ.get("RAMBLR_STORYBOARD_CWD", DATA / "gen"))
SB_TIMEOUT = int(os.environ.get("RAMBLR_STORYBOARD_TIMEOUT", 240))

# Media detection (B2-1): YAMNet on the NPU, CPU at nice 19 as the fallback, never the GPU
MEDIA_MODEL = Path(os.environ.get("RAMBLR_MEDIA_MODEL", MODELS / "yamnet.onnx"))
MEDIA_ENABLED = os.environ.get("RAMBLR_MEDIA", "1") != "0"
MEDIA_MIN_RMS = float(os.environ.get("RAMBLR_MEDIA_MIN_RMS", 0.0025))   # quieter windows are not classified or stored

# B4-18 storyboard panel art: LCM Dreamshaper v7 int8 (OpenVINO) on the NPU, CPU at nice 19 as the fallback, never the GPU
PANEL_ART = os.environ.get("RAMBLR_PANEL_ART", "1") != "0"
PANEL_MODEL = Path(os.environ.get("RAMBLR_PANEL_MODEL", MODELS / "lcm-dreamshaper-v7-int8-ov"))
PANEL_DEVICE = os.environ.get("RAMBLR_PANEL_DEVICE", "NPU")
PANEL_CACHE = Path(os.environ.get("RAMBLR_PANEL_CACHE", DATA / "cache" / "panelart"))
PANEL_SIZE = int(os.environ.get("RAMBLR_PANEL_SIZE", 384))

# B2-2 music ID: AcoustID key lives only in ~/.config/ramblr/secrets.env (never printed). Empty = stub.
ACOUSTID_KEY = os.environ.get("RAMBLR_ACOUSTID_KEY", "")
MUSIC_LOOKUP = os.environ.get("RAMBLR_MUSIC_LOOKUP", "0") == "1"   # song naming stays a stub until Fred's key checks out (6:55 AM)
MUSIC_MIN = float(os.environ.get("RAMBLR_MUSIC_MIN", 0.6))       # window music score that counts as a song playing

# B3-0 iPhone uploads (tailnet only). The key lives only in secrets.env; it is generated there if missing, never printed.
UPLOAD_KEY = os.environ.get("RAMBLR_UPLOAD_KEY", "")
UPLOAD_MAX = int(os.environ.get("RAMBLR_UPLOAD_MAX", 200 * 1024 * 1024))
UPLOAD_TYPES = {"audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/m4a": "m4a", "audio/aac": "m4a", "audio/x-caf": "caf",
                "audio/caf": "caf", "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/vnd.wave": "wav"}
TAILNET_URL = os.environ.get("RAMBLR_TAILNET_URL", "")   # e.g. https://<host>.<tailnet>.ts.net:8798, set in config.env

# B4-19 extra mics over the tailnet: the web relays authenticated streams to ramblr-mics over this 0600 socket
MICS_SOCK = Path(os.environ.get("RAMBLR_MICS_SOCK", Path(os.environ.get("XDG_RUNTIME_DIR", "/dev/shm")) / "ramblr-mics.sock"))
MIC_KEY = os.environ.get("RAMBLR_MIC_KEY", "")
CALL_KEY = os.environ.get("RAMBLR_CALL_KEY", "")         # call heartbeat from another machine (fnix), own key like the mic key
CALL_TTL_MAX = float(os.environ.get("RAMBLR_CALL_TTL_MAX", 180))   # a heartbeat keeps call mode on for at most this long
CALL_SWITCH_HOURS = float(os.environ.get("RAMBLR_CALL_SWITCH_HOURS", 4))   # the site/ctl switch turns itself off after this

# B3-14 voice router on gus (Fred 7:01 AM): 127.0.0.1 + the tailnet IP only, its own log DB (never deleted)
ROUTER_PORT = int(os.environ.get("RAMBLR_ROUTER_PORT", 8783))
ROUTER_DB = Path(os.environ.get("RAMBLR_ROUTER_DB", DATA / "router.db"))

# B3-15 rolling 24 h raw-audio store (compressed). Writing on by default; deleting is ramblr.retention's job, dry run
# first. The cap only stops writing; it never deletes.
AUDIO24_DIR = Path(os.environ.get("RAMBLR_AUDIO24_DIR", DATA / "audio-24h"))
AUDIO24_ENABLED = os.environ.get("RAMBLR_AUDIO24", "1") != "0"
AUDIO24_BITRATE = os.environ.get("RAMBLR_AUDIO24_BITRATE", "10k")   # ~100 MB per 24 h with ogg overhead
AUDIO24_CAP = int(os.environ.get("RAMBLR_AUDIO24_CAP_MB", 2048)) * 2**20

# B4-14 family archive: a separate DB file; while the switch is on, new room lines go only there
FAMILY_DB = Path(os.environ.get("RAMBLR_FAMILY_DB", DATA / "family.db"))

# Runtime state shared by capture, the site, the hotkeys and the bar widget (mute, chosen mic). Outside git.
STATE_FILE = Path(os.environ.get("RAMBLR_STATE", CONFIG_DIR / "state.json"))

# Nightly batch (summary + action items + idea backstop) and ask-your-transcript.
# Generator order: local Ollama only while the GPU is idle (course bot first), then the logged-in Claude CLI.
NIGHTLY_GEN = os.environ.get("RAMBLR_NIGHTLY_GEN", "auto")                    # auto | ollama | claude
NIGHTLY_CLAUDE_MODEL = os.environ.get("RAMBLR_NIGHTLY_CLAUDE_MODEL", "claude-haiku-5-5")
NIGHTLY_FALLBACK_MODEL = os.environ.get("RAMBLR_NIGHTLY_FALLBACK_MODEL", "claude-sonnet-5-5")
NIGHTLY_BLOCK_CHARS = int(os.environ.get("RAMBLR_NIGHTLY_BLOCK_CHARS", 40000))
NIGHTLY_TIMEOUT = int(os.environ.get("RAMBLR_NIGHTLY_TIMEOUT", 300))

EMBED_MODEL_DIR = Path(os.environ.get("RAMBLR_EMBED_MODEL_DIR", HOME / ".local/share/larry-npu/models/nomic-embed-text-v1.5-official"))
EMBED_DEVICE = os.environ.get("RAMBLR_EMBED_DEVICE", "NPU")
EMBED_CACHE = Path(os.environ.get("RAMBLR_EMBED_CACHE", DATA / "cache" / "embed"))
EMBED_IDLE_UNLOAD = float(os.environ.get("RAMBLR_EMBED_IDLE_UNLOAD", 600))    # web frees the model after this idle time
CHUNK_GAP = float(os.environ.get("RAMBLR_CHUNK_GAP", 90))                     # s of silence that starts a new chunk
CHUNK_CHARS = int(os.environ.get("RAMBLR_CHUNK_CHARS", 700))
CHUNK_SETTLE = float(os.environ.get("RAMBLR_CHUNK_SETTLE", 300))              # only lines older than this get indexed
OLLAMA_LEASH = int(os.environ.get("RAMBLR_OLLAMA_LEASH", 60))             # s before a local call fails over to Claude
OFFLINE_LEASH = int(os.environ.get("RAMBLR_OFFLINE_LEASH", 240))           # s for the local model when Claude is unreachable
ASK_GEN = os.environ.get("RAMBLR_ASK_GEN", "auto")
ASK_CLAUDE_MODEL = os.environ.get("RAMBLR_ASK_CLAUDE_MODEL", "claude-sonnet-5-5")
ASK_TIMEOUT = int(os.environ.get("RAMBLR_ASK_TIMEOUT", 120))

# Nightly encrypted backup to whi. Public-key gpg: whi only ever holds ciphertext, the secret key stays on gus.
BACKUP_HOST = os.environ.get("RAMBLR_BACKUP_HOST", "whi")
BACKUP_DIR = os.environ.get("RAMBLR_BACKUP_DIR", "backups/ramblr")             # relative to the remote home
BACKUP_GNUPG = Path(os.environ.get("RAMBLR_BACKUP_GNUPG", CONFIG_DIR / "backup-gnupg"))
BACKUP_RECIPIENT = os.environ.get("RAMBLR_BACKUP_RECIPIENT", "ramblr-backup@gus")

WEB_HOST = "127.0.0.1"          # never 0.0.0.0
WEB_PORT = int(os.environ.get("RAMBLR_PORT", 8798))
