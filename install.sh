#!/usr/bin/env bash
# Ramblr one-command installer (B4-17).
#   ./install.sh --check     report what is there and what is missing; changes nothing
#   ./install.sh             project venv + Python deps + models + config skeleton (no system packages, no sudo)
#   ./install.sh --enable    the above, then install and start the systemd --user units
# System packages are NEVER installed by this script: it prints the exact command for you to run yourself.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DATA="${RAMBLR_DATA:-$HOME/.local/share/ramblr}"
CONF="${RAMBLR_CONFIG:-$HOME/.config/ramblr}"
MODELS="$DATA/models"
MODE="${1:-install}"
ok=0; warn=0; missing=()

say()  { printf '  %-28s %s\n' "$1" "$2"; }
good() { say "$1" "ok  $2"; ok=$((ok+1)); }
bad()  { say "$1" "MISSING  $2"; warn=$((warn+1)); [ -n "${3:-}" ] && missing+=("$3"); }

echo "Ramblr installer ($MODE)"
echo "Checks:"
[ "$(uname -s)" = "Linux" ] && good "Linux" "$(uname -r)" || bad "Linux" "Ramblr targets Linux (PipeWire, systemd --user)"
command -v uv >/dev/null && good "uv" "$(uv --version 2>/dev/null)" || bad "uv" "Python manager" "uv"
command -v ffmpeg >/dev/null && good "ffmpeg" "audio decode/encode" || bad "ffmpeg" "needed for uploads and the 24 h store" "ffmpeg"
command -v parec >/dev/null && good "parec" "PipeWire/Pulse capture" || bad "parec" "mic capture" "pipewire-pulse (or pulseaudio-utils)"
command -v pactl >/dev/null && good "pactl" "mic listing" || bad "pactl" "mic listing" "pipewire-pulse (or pulseaudio-utils)"
command -v systemctl >/dev/null && good "systemd" "user services" || bad "systemd" "needed for the services"
command -v gpg >/dev/null && good "gpg" "encrypted backups" || bad "gpg" "optional: encrypted backups" "gnupg"
command -v tailscale >/dev/null && good "tailscale" "private access" || bad "tailscale" "optional: reach the site from your devices" "tailscale"
command -v fpcalc >/dev/null && good "fpcalc" "music ID" || bad "fpcalc" "optional: song titles" "chromaprint"
if [ -e /dev/accel/accel0 ]; then
  [ -r /dev/accel/accel0 ] && [ -w /dev/accel/accel0 ] && good "NPU device" "/dev/accel/accel0" || bad "NPU access" "/dev/accel/accel0 exists but this user cannot open it (udev rule needed)"
else
  bad "NPU device" "no /dev/accel/accel0: Whisper and search will run on the CPU (set RAMBLR_WHISPER_DEVICE=CPU)"
fi
if command -v pactl >/dev/null; then
  mics=$(pactl list sources short 2>/dev/null | awk -F'\t' '$2 !~ /\.monitor$/ {print $2}')
  n=$(printf '%s\n' "$mics" | grep -c . || true)
  [ "$n" -gt 0 ] && good "microphones" "$n found (pick one on /setup)" || bad "microphones" "no input source found"
fi

if [ ${#missing[@]} -gt 0 ]; then
  echo
  echo "System packages are yours to install (this script never runs sudo). On Arch:"
  echo "  sudo pacman -S --needed $(printf '%s ' "${missing[@]}" | sed 's/(or pulseaudio-utils)//; s/pipewire-pulse (or /pipewire-pulse /')"
  echo "On Debian/Ubuntu the names are similar (pulseaudio-utils, ffmpeg, gnupg, chromaprint-tools)."
fi
[ "$MODE" = "--check" ] && { echo; echo "$ok ok, $warn missing or optional. Nothing was changed."; exit 0; }

echo
echo "Install (project-local only):"
command -v uv >/dev/null || { echo "  uv is required first: see https://docs.astral.sh/uv/"; exit 1; }
cd "$HERE"
[ -d .venv ] || uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt >/dev/null && say "python deps" "installed into $HERE/.venv"
mkdir -p "$MODELS" "$CONF" && chmod 700 "$CONF"
fetch() { [ -s "$MODELS/$1" ] && say "$1" "present" || { curl -fsSL -o "$MODELS/$1.part" "$2" && mv "$MODELS/$1.part" "$MODELS/$1" && say "$1" "downloaded"; } || say "$1" "download FAILED ($2)"; }
fetch silero_vad.onnx "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"   # same source as the live install
fetch wespeaker_en_voxceleb_resnet34_LM.onnx "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx"
fetch yamnet.onnx "https://huggingface.co/audiomagic/yamnet-onnx/resolve/main/yamnet.onnx"
fetch yamnet_class_map.csv "https://huggingface.co/audiomagic/yamnet-onnx/resolve/main/yamnet_class_map.csv"
WH="${RAMBLR_WHISPER:-$HOME/.local/share/intel-npu/models/whisper-base.en}"
if [ -d "$WH" ]; then say "whisper model" "present ($WH)"; else
  say "whisper model" "not found: download an OpenVINO Whisper (e.g. OpenVINO/whisper-base.en-fp16-ov on Hugging Face) and set RAMBLR_WHISPER in $CONF/config.env"; fi
[ -d "$MODELS/lcm-dreamshaper-v7-int8-ov" ] && say "panel art model" "present" || \
  say "panel art model" "optional: storyboard sketches need OpenVINO/LCM_Dreamshaper_v7-int8-ov (Hugging Face, MIT, ~1 GB) in $MODELS/lcm-dreamshaper-v7-int8-ov"
if [ ! -f "$CONF/config.env" ]; then
  umask 077
  { echo "# Ramblr local config (not in git). Pick the mic on /setup; this is only the starting default."
    echo "RAMBLR_SOURCE=$(pactl get-default-source 2>/dev/null)"; } > "$CONF/config.env"
  say "config.env" "created (mic = the system default for now)"
else say "config.env" "kept as is"; fi
[ -f "$CONF/secrets.env" ] || { umask 077; echo "# Ramblr secrets (never in git). See README for each variable." > "$CONF/secrets.env"; say "secrets.env" "created empty (chmod 600)"; }

if [ "$MODE" = "--enable" ]; then
  mkdir -p "$HOME/.config/systemd/user"
  for u in "$HERE"/systemd/*.service "$HERE"/systemd/*.timer; do
    sed "s|%h/Projects/ramblr|$HERE|g" "$u" > "$HOME/.config/systemd/user/$(basename "$u")"
  done
  systemctl --user daemon-reload
  systemctl --user enable --now ramblr-web ramblr-capture ramblr-diarize ramblr-ideas ramblr-mics ramblr-router ramblr-nightly.timer ramblr-retention.timer
  say "services" "installed and started"
  echo "Open http://127.0.0.1:${RAMBLR_PORT:-8798}/setup for the first-run wizard."
else
  echo "Run ./install.sh --enable to install and start the services, then open http://127.0.0.1:8798/setup"
fi
