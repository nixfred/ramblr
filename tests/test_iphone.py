"""B3-0: the Shortcut posts the recording with the bearer key; uploads need the key or the same origin."""
import plistlib


def test_shortcut_workflow_shape():
    from ramblr import iphone
    wf = iphone.workflow("k3y", "https://example.ts.net:8798/api/upload")
    ids = [a["WFWorkflowActionIdentifier"] for a in wf["WFWorkflowActions"]]
    assert ids == ["is.workflow.actions.recordaudio", "is.workflow.actions.downloadurl", "is.workflow.actions.notification"]
    post = wf["WFWorkflowActions"][1]["WFWorkflowActionParameters"]
    assert post["WFHTTPMethod"] == "POST" and post["WFHTTPBodyType"] == "File"
    assert post["WFRequestVariable"]["Value"]["OutputUUID"] == wf["WFWorkflowActions"][0]["WFWorkflowActionParameters"]["UUID"]
    hdrs = {i["WFKey"]["Value"]["string"]: i["WFValue"]["Value"]["string"] for i in post["WFHTTPHeaders"]["Value"]["WFDictionaryFieldValueItems"]}
    assert hdrs == {"Authorization": "Bearer k3y", "Content-Type": "audio/mp4"}
    plistlib.dumps(wf, fmt=plistlib.FMT_BINARY)          # serialises as a binary plist


def test_upload_key_created_in_tmp_config(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("RAMBLR_CONFIG", str(tmp_path / "cfg"))
    monkeypatch.delenv("RAMBLR_UPLOAD_KEY", raising=False)
    from ramblr import config, uploads
    importlib.reload(config); importlib.reload(uploads)
    k = uploads.ensure_key()
    f = tmp_path / "cfg" / "secrets.env"
    assert len(k) > 30 and f"RAMBLR_UPLOAD_KEY={k}" in f.read_text() and (f.stat().st_mode & 0o777) == 0o600
    config.UPLOAD_KEY = ""
    assert uploads.ensure_key() == k                      # reads the same key back, never makes a second one
