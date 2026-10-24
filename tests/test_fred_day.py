"""B4-3 promises, B4-6 calendar parsing, B4-12 energy map, B4-13 ambient snapshots (test DB, no network)."""
import time
from datetime import datetime, timedelta

import pytest

ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:standup
DTSTART;TZID=America/New_York:{d}T093000
DTEND;TZID=America/New_York:{d}T094500
RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;COUNT=10
SUMMARY:Standup\\, team
END:VEVENT
BEGIN:VEVENT
UID:dentist
DTSTART:{d}T180000Z
DTEND:{d}T190000Z
SUMMARY:Dentist
LOCATION:Main St
END:VEVENT
END:VCALENDAR
"""


@pytest.fixture
def env(monkeypatch):
    import importlib
    from ramblr import config, db, calendar_feed, ambient, insights, me
    for m in (db, me, calendar_feed, ambient, insights):
        importlib.reload(m)
    return {"db": db.connect(config.DB_PATH), "dbm": db, "cal": calendar_feed, "amb": ambient, "ins": insights, "me": me}


def test_calendar_parse_recurring_and_tz(env):
    d0 = datetime.now() - timedelta(days=datetime.now().weekday())          # this Monday
    evs = env["cal"].parse(ICS.replace("{d}", d0.strftime("%Y%m%d")))
    st = [e for e in evs if e["uid"].startswith("standup@")]
    assert len(st) == 10 and all(datetime.fromtimestamp(e["start"]).weekday() < 5 for e in st)
    assert st[0]["title"] == "Standup, team" and st[0]["end"] - st[0]["start"] == 900
    den = [e for e in evs if e["uid"] == "dentist"][0]
    assert den["location"] == "Main St" and env["cal"].status().startswith("stub")


def test_promises_energy_ambient(env, monkeypatch):
    d, dbm = env["db"], env["dbm"]
    fred = dbm.new_speaker(d, "Fred Nix", 1)
    d.execute("UPDATE speakers SET confirmed=1 WHERE id=?", (fred,))
    assert env["me"].names(d) == {"fred nix", "fred"}
    sid, _ = dbm.insert_segment(d, time.time() - 600, 3, "Larry, I have an idea", None, "t", 1)
    from ramblr import ideas
    import importlib; importlib.reload(ideas)
    ideas.create(d, [sid], "range")
    e = env["ins"].energy_map(d, 7)
    assert e["total"] == 1 and sum(map(sum, e["grid"])) == 1
    monkeypatch.setattr(env["amb"], "window", lambda: ("kitty", "vim notes.md"))
    monkeypatch.setattr(env["amb"], "weather", lambda: None)
    s = env["amb"].snapshot(d, now=time.time())
    assert s["window"] == "kitty" and env["amb"].snapshot(d, now=time.time() + 10) is None     # every 5 min, not more
