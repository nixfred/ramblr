"""B4-15: when Claude is unreachable the local model is used, but only if the GPU rule allows it."""
import pytest


def test_offline_fallback(monkeypatch):
    import importlib
    from ramblr import llm, storyboard
    importlib.reload(llm)
    monkeypatch.setattr(storyboard, "gen_claude", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network down")))
    monkeypatch.setattr(storyboard, "gen_ollama", lambda *a, **k: ('{"ok": 1}', "gemma3:12b"))
    monkeypatch.setattr(llm, "ollama_ok", lambda: (True, "idle"))
    text, gen, model, secs, note = llm.generate("x", mode="claude")
    assert gen == "ollama-offline" and "claude failed" in note
    monkeypatch.setattr(llm, "ollama_ok", lambda: (False, "GPU busy (95%), course bot has priority"))
    with pytest.raises(llm.Unavailable):
        llm.generate("x", mode="claude")
