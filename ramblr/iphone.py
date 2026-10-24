"""Build and sign the Ramblr iOS Shortcut (B3-0): Record Audio -> POST it to /api/upload with the bearer key ->
"Sent to Ramblr". iOS only imports signed shortcuts, and signing needs macOS, so the plist is signed on fnix
(`shortcuts sign --mode anyone`) over ssh. The file carries the upload key, so it lives in
~/.local/share/ramblr/iphone (mode 700/600), is served only on the tailnet, and the temp copies on the Mac are
removed right after signing.

  python -m ramblr.iphone build      write + sign + fetch ~/.local/share/ramblr/iphone/ramblr.shortcut
"""
import os
import plistlib
import shlex
import subprocess
import sys
import uuid

from . import config, uploads

SIGN_HOST = os.environ.get("RAMBLR_SIGN_HOST", "fnix")
OUT_DIR = config.DATA / "iphone"


def _tok(s):
    return {"Value": {"string": s, "attachmentsByRange": {}}, "WFSerializationType": "WFTextTokenString"}


def _item(k, v):
    return {"WFItemType": 0, "WFKey": _tok(k), "WFValue": _tok(v)}


def workflow(key, url):
    rec, post = str(uuid.uuid4()).upper(), str(uuid.uuid4()).upper()
    return {
        "WFWorkflowClientVersion": "2607.0.2", "WFWorkflowMinimumClientVersion": 900, "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": 431817727, "WFWorkflowIconGlyphNumber": 59446},
        "WFWorkflowImportQuestions": [], "WFWorkflowTypes": [], "WFWorkflowInputContentItemClasses": [],
        "WFWorkflowHasShortcutInputVariables": False,
        "WFWorkflowActions": [
            {"WFWorkflowActionIdentifier": "is.workflow.actions.recordaudio",
             "WFWorkflowActionParameters": {"UUID": rec, "WFRecordingStart": "Immediately", "WFRecordingEnd": "On Tap",
                                            "WFRecordingCompression": "Normal"}},
            {"WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
             "WFWorkflowActionParameters": {
                 "UUID": post, "WFURL": url, "WFHTTPMethod": "POST", "WFHTTPBodyType": "File", "ShowHeaders": True,
                 "WFRequestVariable": {"Value": {"OutputUUID": rec, "Type": "ActionOutput", "OutputName": "Recorded Audio"},
                                       "WFSerializationType": "WFTextTokenAttachment"},
                 "WFHTTPHeaders": {"Value": {"WFDictionaryFieldValueItems": [_item("Authorization", "Bearer " + key),
                                                                             _item("Content-Type", "audio/mp4")]},
                                   "WFSerializationType": "WFDictionaryFieldValue"}}},
            {"WFWorkflowActionIdentifier": "is.workflow.actions.notification",
             "WFWorkflowActionParameters": {"WFNotificationActionTitle": "Ramblr", "WFNotificationActionBody": "Sent to Ramblr"}},
        ]}


def build():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(OUT_DIR, 0o700)
    unsigned = OUT_DIR / "ramblr-unsigned.shortcut"
    signed = OUT_DIR / "ramblr.shortcut"
    old = os.umask(0o077)
    try:
        with open(unsigned, "wb") as f:
            plistlib.dump(workflow(uploads.ensure_key(), config.TAILNET_URL + "/api/upload?name=iphone.m4a"), f, fmt=plistlib.FMT_BINARY)
    finally:
        os.umask(old)
    ssh = ["ssh", "-o", "BatchMode=yes", SIGN_HOST]
    tmp = subprocess.run(ssh + ["mktemp -d /tmp/ramblr-sign.XXXX"], capture_output=True, text=True, check=True).stdout.strip()
    q = shlex.quote(tmp)
    try:
        subprocess.run(["scp", "-q", "-o", "BatchMode=yes", str(unsigned), f"{SIGN_HOST}:{tmp}/in.shortcut"], check=True)
        r = subprocess.run(ssh + [f"cd {q} && shortcuts sign --mode anyone --input in.shortcut --output out.shortcut"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("shortcuts sign failed: " + (r.stderr or r.stdout)[-300:])
        subprocess.run(["scp", "-q", "-o", "BatchMode=yes", f"{SIGN_HOST}:{tmp}/out.shortcut", str(signed)], check=True)
        os.chmod(signed, 0o600)
    finally:
        # our own temp files on the Mac (they carry the key) and the local unsigned copy
        subprocess.run(ssh + [f"rm -f {q}/in.shortcut {q}/out.shortcut && rmdir {q}"], capture_output=True)
        unsigned.unlink(missing_ok=True)
    with open(signed, "rb") as f:
        magic = f.read(4)
    if magic != b"AEA1":
        raise RuntimeError("the signed file is not in Apple's signed format")
    return {"path": str(signed), "bytes": signed.stat().st_size, "signed_on": SIGN_HOST}


if __name__ == "__main__":
    if sys.argv[1:2] == ["build"]:
        print(build())
    else:
        print(__doc__)
        sys.exit(2)
