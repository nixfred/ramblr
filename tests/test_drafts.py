"""B3-13: drafts are made from a card, edited with history, approved or dismissed; there is no send path."""
import json

import pytest


@pytest.fixture
def env(monkeypatch):
    import importlib
    from ramblr import config, db, drafts, conversations
    for m in (db, conversations, drafts):
        importlib.reload(m)
    d = db.connect(config.DB_PATH)
    sid, _ = db.insert_segment(d, 1792760000, 3.0, "I'll email the contractor the measurements", None, "t", 1)
    d.execute("""INSERT INTO conversations(key, ts0, ts1, first_seg, last_seg, lines, status, card, created)
                 VALUES('conv:1', 1792760000, 1792760003, ?, ?, 1, 'closed', ?, 0)""",
              (sid, sid, json.dumps({"title": "Deck", "summary": "s", "action_items": [{"task": "Email measurements"}]})))
    monkeypatch.setattr(drafts.llm, "generate", lambda p, **k: (json.dumps({"drafts": [
        {"kind": "email", "to": "the contractor", "subject": "Deck measurements", "body": "Hi, here are the measurements.", "why": "action item"},
        {"kind": "bogus", "body": "kept as a note"}, {"kind": "email", "body": ""}]}), "test", "stub", 0, ""))
    return {"db": d, "dr": drafts}


def test_make_edit_approve_no_send(env):
    d, dr = env["db"], env["dr"]
    assert dr.maybe_auto(d, 1) == {"made": 2}
    assert dr.make_for_conversation(d, 1)["made"] == 0                  # no duplicates unless forced
    kinds = [r[0] for r in d.execute("SELECT kind FROM drafts ORDER BY id")]
    assert kinds == ["email", "note"]
    dr.update(d, 1, body="Hi, measurements attached.", status="approved")
    assert d.execute("SELECT status, body FROM drafts WHERE id=1").fetchone()[:] == ("approved", "Hi, measurements attached.")
    assert d.execute("SELECT COUNT(*) FROM draft_edits").fetchone()[0] == 1
    with pytest.raises(ValueError):
        dr.update(d, 1, status="sent")                                  # there is no 'sent' status: Ramblr never sends
    import ramblr.drafts as mod
    src = open(mod.__file__).read()
    assert "urlopen" not in src and "smtplib" not in src and "send_message" not in src
