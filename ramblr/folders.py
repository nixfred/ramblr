"""Auto-sorted folders (B3-11). Conversation cards are filed when they are written (the card prompt picks one of the
existing folders or names a new short one); ideas without a folder are filed by one batch call in the nightly pass.
Fred can move anything; a manual move is never overridden. Folders and placements are never deleted (an item that
moves just points at another folder)."""
import json
import time

from . import config, llm, storyboard

MAX_FOLDERS = 40


def names(db):
    return [r[0] for r in db.execute("SELECT name FROM folders ORDER BY name")]


def ensure(db, name):
    name = " ".join(str(name).split())[:40].strip(" .")
    if not name:
        return None
    r = db.execute("SELECT id FROM folders WHERE lower(name)=lower(?)", (name,)).fetchone()
    if r:
        return r[0]
    if db.execute("SELECT COUNT(*) FROM folders").fetchone()[0] >= MAX_FOLDERS:
        r = db.execute("SELECT id FROM folders WHERE name='Other'").fetchone()
        return r[0] if r else db.execute("INSERT INTO folders(name, created) VALUES('Other', ?)", (time.time(),)).lastrowid
    return db.execute("INSERT INTO folders(name, created) VALUES(?, ?)", (name, time.time())).lastrowid


def place(db, item_type, item_id, folder_name, source="auto"):
    """File an item. Auto placements never replace a manual one."""
    cur = db.execute("SELECT source FROM folder_items WHERE item_type=? AND item_id=?", (item_type, item_id)).fetchone()
    if cur and cur["source"] == "manual" and source == "auto":
        return False
    fid = ensure(db, folder_name)
    if not fid:
        return False
    db.execute("""INSERT INTO folder_items(item_type, item_id, folder_id, source, created) VALUES(?,?,?,?,?)
                  ON CONFLICT(item_type, item_id) DO UPDATE SET folder_id=excluded.folder_id, source=excluded.source, created=excluded.created""",
               (item_type, item_id, fid, source, time.time()))
    return True


PROMPT = """Sort these ideas into folders by their main subject area. Reuse an existing folder only if the idea is clearly
about that same subject; otherwise name a short new one (1 to 3 words, like "Ramblr", "House", "Pets", "Money", "Work"), never a near-synonym of an existing folder. Return ONE JSON object only: {"placements": [{"id": 1, "folder": "..."}]}
EXISTING FOLDERS: {folders}
IDEAS:
{items}
"""


def file_unsorted(db):
    rows = db.execute("""SELECT i.id, i.title FROM ideas i WHERE i.status!='dismissed'
                         AND NOT EXISTS (SELECT 1 FROM folder_items f WHERE f.item_type='idea' AND f.item_id=i.id) ORDER BY i.id LIMIT 60""").fetchall()
    if not rows:
        return {"filed": 0}
    items = []
    for r in rows:
        sb = db.execute("SELECT body FROM storyboards WHERE idea_id=? AND status='ok' ORDER BY version DESC LIMIT 1", (r["id"],)).fetchone()
        pitch = json.loads(sb["body"]).get("pitch", "") if sb else ""
        items.append(f'{r["id"]}: {r["title"]} {("- " + pitch) if pitch else ""}')
    text, gen, model, secs, note = llm.generate(PROMPT.replace("{folders}", ", ".join(names(db)) or "(none yet)").replace("{items}", "\n".join(items)),
                                                mode=config.NIGHTLY_GEN, claude_model=config.NIGHTLY_CLAUDE_MODEL, timeout=config.NIGHTLY_TIMEOUT)
    valid = {r["id"] for r in rows}
    n = 0
    for p in storyboard.parse_json(text).get("placements", []):
        try:
            iid = int(p.get("id"))
        except (TypeError, ValueError):
            continue
        if iid in valid and place(db, "idea", iid, p.get("folder", "")):
            n += 1
    return {"filed": n, "model": model}


def listing(db):
    out = []
    for f in db.execute("SELECT * FROM folders ORDER BY name").fetchall():
        items = []
        for it in db.execute("SELECT * FROM folder_items WHERE folder_id=? ORDER BY created DESC", (f["id"],)):
            if it["item_type"] == "conversation":
                c = db.execute("SELECT id, ts0, first_seg, card FROM conversations WHERE id=?", (it["item_id"],)).fetchone()
                if c and c["card"] and c["card"] != "{}":
                    card = json.loads(c["card"])
                    items.append({"type": "conversation", "id": c["id"], "title": card.get("title", ""), "summary": card.get("summary", ""),
                                  "ts": c["ts0"], "first_seg": c["first_seg"], "source": it["source"]})
            elif it["item_type"] == "idea":
                i = db.execute("SELECT id, title, status, created FROM ideas WHERE id=?", (it["item_id"],)).fetchone()
                if i:
                    items.append({"type": "idea", "id": i["id"], "title": i["title"], "status": i["status"], "ts": i["created"], "source": it["source"]})
        out.append({"id": f["id"], "name": f["name"], "items": items, "count": len(items)})
    return [f for f in out if f["count"]]
