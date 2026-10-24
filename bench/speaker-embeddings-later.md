# Later: a stronger speaker-embedding model (not started)

**Why:** on the room mic every voice lands in a narrow cone with the current model (wespeaker ResNet34). During the
2026-10-24 call:
- the centroids of the two people on the call were at cos 0.92
- one hand-labelled line scored 0.912 vs 0.907 between them
- the median margin between them was 0.053, so the 0.10 naming margin is never met by single lines

Mean-centring inflated the margins without improving the ranking (about 15% wrong names), so it was rejected.
Better separation has to come from the embedding itself.

**What to benchmark** (read-only, like `tools/bench_whisper.py`):
- candidates: ECAPA-TDNN (SpeechBrain, ungated) and a larger wespeaker ResNet (ResNet152/221/293), CPU first, NPU if they convert
- clips: hand-labelled room lines (`segments.manual=1`) plus their 24 h audio, split by channel (in-room voice vs voice from speakers)
- metrics:
  - same-vs-different-person EER
  - margin distribution between two people on the same channel
  - leave-one-out nearest-centroid agreement with the hand labels
  - RTF and memory per embedding
- decision rule: switch only if margins clear 0.10 for most single lines at the same or lower error, with headroom
  on the CPU or NPU (the diarizer runs at nice 19, 1 thread)

**Status:** documented only (Fred, 2026-10-24: "item 5 goes into bench/ as a documented later item").
