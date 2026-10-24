"""Who is Fred, for the per-speaker wake word (B4-5) and "What did I promise" (B4-3).

Fred's voices are the speakers he confirmed with "Confirm this is me". Until he has confirmed one, the named
speakers whose name starts with "Fred" stand in. RAMBLR_WAKE_SPEAKERS (comma-separated names) overrides both.
Only these voices can trigger "Larry, ..." (ideas and voice routing); a line whose voice is not identified yet
waits for the diarizer (up to WAIT seconds), then counts as not Fred."""
import os

WAIT = float(os.environ.get("RAMBLR_WAKE_WAIT", 60))


def speaker_ids(db):
    names = [n.strip().lower() for n in os.environ.get("RAMBLR_WAKE_SPEAKERS", "").split(",") if n.strip()]
    if names:
        ph = ",".join("?" * len(names))
        return {r[0] for r in db.execute(f"SELECT id FROM speakers WHERE lower(name) IN ({ph}) AND merged_into IS NULL", names)}
    ids = {r[0] for r in db.execute("SELECT id FROM speakers WHERE confirmed=1 AND merged_into IS NULL")}
    if ids:
        return ids
    return {r[0] for r in db.execute("SELECT id FROM speakers WHERE named=1 AND merged_into IS NULL AND lower(name) LIKE 'fred%'")}


def names(db):
    ids = speaker_ids(db)
    if not ids:
        return {"fred"}
    ph = ",".join("?" * len(ids))
    out = {r[0].lower() for r in db.execute(f"SELECT name FROM speakers WHERE id IN ({ph})", list(ids))}
    out |= {n.split()[0] for n in out}            # "Fred Nix" also answers to "Fred"
    return out


def gate(db, row, now):
    """'yes' (Fred said it), 'no' (someone else), or 'wait' (voice not identified yet, still inside WAIT)."""
    if row["source"] in ("iPhone",):
        return "yes"                               # Fred's own phone recording
    if row["diar"] == 0 and not row["manual"] and now - (row["ts"] + row["dur"]) < WAIT:
        return "wait"
    return "yes" if row["speaker_id"] in speaker_ids(db) else "no"
