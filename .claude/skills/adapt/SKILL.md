---
name: adapt
description: Change LomboShrink's rules, thresholds, encoder settings or alerts without breaking it. Use when the user wants different behaviour, a new encoder, a new setting, or a different alert target.
---

# Adapt LomboShrink

1. **Change a rule** (what is skipped, what is accepted): edit `skip_reason` or `accept_problems` in `core.py`.
   Write the test first with plain probe data (`video_probe()` in `tests/conftest.py`), watch it fail, then
   change the code. Keep `core.py` free of network, disk, clock and subprocess. Never loosen an accept rule to
   make one file pass: the point is that a rejected file keeps its original.
2. **Change the quality or size bars:** these are settings (`CQ`, `MAX_CQ`, `MIN_SAVING_PCT`, `MIN_BPP`), not code.
3. **Add an encoder:** codec name in `ENCODER_CODECS`, arguments in `encoder_args`, the name in `ENCODER_CHOICES`
   (`config.py`) and `.env.example`, a test. Say in the README that it is unverified until run on that hardware.
4. **Add a setting:** read and validate it in `config.py` (the error must name the variable), add a commented
   line to `.env.example` (a test fails if they drift), and a test for a bad value in `tests/test_app.py`.
5. **Touch the swap:** read the checklist at the top of `swap.py`. Every new path needs a test in
   `tests/test_swap.py` that runs in `tmp_path` and proves the original is untouched when it refuses.
6. **Change alerts:** `notify.py` posts JSON to a webhook; for a different body, put a small bridge in front.
7. **Run:** `pre-commit run --all-files`, `pytest -q --cov`. Both must pass.
8. **Stay safe:** `DRY_RUN` still defaults to true, and `plan` and `swap` without `--apply` still change nothing.
