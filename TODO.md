# TODO

Open items after the 3-layer merge. Everything here is **owner/bench work** or a decision that
needs a measurement — nothing in this file is blocking `make check`, which is green.

---

## 1. Hardware the button device still needs

The hand-held SD player **migrated off** its old direct-pin ~5.7 Vpp stage onto the shared
36 V DRV8871 stage. That hardware **has to be built** before the sketch can be bench-tested.

- [ ] Build the 36 V DRV8871 output stage for the hand-held unit (2× DRV8871, IN1 on pins 2/3
      held high, IN2 on pins 0/1, plus the per-channel 2-pole RC).
- [ ] **Do not flash `eel_fakefish_button` onto the old direct-pin hardware.** A discharge now
      reaches the **36 V rail** instead of the old stage's ~5.7 Vpp — still roughly a six-fold jump.
- [ ] Re-scope the absolute output and set `MASTER_GAIN` before the device goes near an animal.
      A discharge reaches **at most ~36 V**, the rail, because the eel EOD is monophasic: only one
      bridge ever drives while the other is braked to 0 V. (It is **not** ~72 Vpp — earlier
      revisions of these docs said so; that would need a biphasic signal driving both bridges in
      opposite directions at once, which never happens.) A `0.90` volley pulse is ~**32 V**, a
      `0.45` localization pulse ~**16 V**, open-circuit at nominal rail.

## 2. The 10 kHz marker — question closed, one bench check left

**Decided (2026-08-17): the SD device's 10 kHz sine marker was removed.** The level-budget
question that used to fill this section is closed *by construction*, not by measurement. Two
reasons: the output filter (2× 220 Ω / 220 nF per channel, composite −3 dB ≈ 1.23 kHz) attenuates
10 kHz by **−21.8 dB**, and everything the device puts in the water should be made of eel pulses.

**What replaced it.** A lead-in of **6 EOD pulses at a fixed 10 Hz** (IPI 5000 samples = 100 ms)
with **alternating polarity**, at level **0.5** — first onset to last onset 0.5 s — prepended to
every `/B`, `/C` and `/D` session. Its three properties:

- **alternation is the detection cue** — no eel alternates, and a localization train is
  single-polarity, so the pattern cannot be confused with biology or with the stimulus;
- **it survives the per-press random polarity flip**, which negates the whole WAV: the sign is
  unpredictable, the alternation is not. A detector must key on the *pattern*, never the sign;
- **the even pulse count makes it charge-balanced**, replacing the zero-sum property the sine LUT
  provided. The codegen asserts evenness.

Program **A** changed with it: a 10 s **single-polarity** eel-pulse train at 50 Hz, level 0.45 — a
plain reference signal, deliberately not a code. The playback structure is unchanged:
**[marker] → [per-item gap, 50–200 ms] → [stimulus]**. The new marker is barely touched by the
filter (eel pulses are sub-kHz; 99 % of `EOD_HV`'s energy is below 904 Hz), and because it now sits
in the same band as the stimulus, the marker-to-pulse **ratio** survives the water load exactly.

What is left:

- [ ] **Bench/field check: is the alternating burst actually detectable in a real recording?**
      Confirm the six pulses resolve individually at the grid and that the alternation is
      unambiguous against real fish. This is a *detection* question now rather than a level-budget
      one, and only a recording settles it.
- [ ] The code is **provisional and cheap to retune.** 6 pulses at 10 Hz are authored in
      `shared/stim_constants.json` (`sd_path.pulse_marker`); changing them is `make gen` +
      `fakefish-build-card`, and the card, the firmware header and the galleries then move
      together by construction. Keep the count **even** or the burst stops being charge-balanced.
- [ ] Cosmetic leftovers of the sine, in **byte-frozen** artifacts: `eel_stimuli.h` still calls the
      per-item gap a "sine-marker lead-in gap", and `data/stimuli_provenance.json` still carries a
      `lead_in_marker` block describing the 10 kHz anchor. The *gap itself* (50–200 ms, fixed per
      item at export) is unchanged and still live — only the wording is historical. Rewording
      either is a deliberate edit to a frozen contract; decide it, don't drift into it.

## 3. `FULLSCALE_PULSE_PEAK_MV` is a 3.3 V-era constant

`shared/stim_constants.json` carries `fullscale_pulse_peak_mv: 3313.0`, derived as
`3.3 V × 32767/32640` on the **retired** rail. On the 36 V stage every millivolt figure the
toolchain prints is therefore ~11× low: `fakefish-build-card`'s mV CLI, and the
`levels_nominal_mv` field written into each card's `manifest.json`.

- [ ] Scope the actual delivered full-scale voltage and re-derive the constant, then `make gen`.
- [ ] Decide what the constant *means* — open-circuit at the driver, or at the electrode after
      the filter? The current value does not say, and the two differ by the filter's response.
- [ ] Until then, prefer **fractions of full scale**, which are exact and rail-independent.

Fractions of full scale, the level *ratios*, and every WAV on the card are unaffected — this is
purely the human-readable mV annotation.

## 4. Verify the filter on the bench, not on paper

Every filter figure in `firmware/README.md` is analysis of the described network with ideal
components. The most likely reason a measurement disagrees:

- [ ] **Class-2 ceramic DC-bias derating.** Each 220 nF shunt sits at a large mean DC voltage on
      a single-ended 36 V rail. X7R/X5R (and far worse Y5V) parts can lose a large fraction of
      their nominal capacitance under tens of volts of bias, which pushes the real corner
      **above** 1.23 kHz, possibly well above. Check the parts' dielectric and voltage rating,
      and measure the assembled network.
- [ ] Measure the in-water response — the electrode load divides DC gain by
      `R_diff/(R_diff + 880)`, which is a bigger effect than anything the filter does to the EOD.
- [ ] Confirm idle really is braked (hard 0 V single-ended, no slow leak), and that duty → volts
      is linear to the rail. `AMP_DEBUG 1` in `eel_core/config.h` automates this sweep.

## 5. Pulse logging — bench verification and the aligner

The RC device now logs **every emitted pulse** to `/LOGS/PULSnnnn.CSV` and **refuses to
stimulate without a working card** (`CLAUDE.md` invariant 9, `firmware/README.md` → "Pulse
logging"). The pure logic is host-tested and the C↔Python format is pinned by a generated
golden, but none of it has touched real hardware.

Bench items:

- [ ] **Run it with a real card.** Confirm `/LOGS/` is created, the first file is `PULS0000.CSV`,
      and a second power-cycle produces `PULS0001.CSV` — never a reopened file.
- [ ] **Confirm no dropped records during a volley burst.** The worst case is ~400 Hz for ~1 s
      against a 512-record ring. Fire volleys back to back and check for `DROP` rows
      (`uv run fakefish-pulse-log info /path/PULS0000.CSV` reports them). If drops appear, the
      SD stall is longer than assumed — raise `PULSELOG_RING_SIZE`, do **not** silence the row.
- [ ] **Verify the block-always policy on hardware.** Boot with no card: the LED must show the
      inverse blink (steady on, brief dark notch) and **nothing** may reach the electrodes. Then
      pull the card mid-session: a volley in flight must finish, localization must stop at a
      pulse boundary, and re-inserting must open a *new* file carrying a `GAP` row.
- [ ] **Fit the RTC coin cell and verify it holds time across a power cycle.** Check
      `#rtc_valid=1` in the header and that `ANCHOR` rows carry a plausible clock. Without it the
      log is still fully usable — you lose absolute time, not relative timing — so this is a
      convenience, not a blocker.
- [ ] **Measure the `loop()` cost.** An SD write can stall 100 ms+, delaying the ~200 Hz RC
      decode. `RC_ABSENCE_MS` is 500 ms so a stall should not fake a link loss, but confirm the
      trigger still responds crisply while logging at 20 Hz.
- [ ] Check flash/RAM headroom now that `SD`/`SdFat` are linked into the RC binary (`make check`
      is a syntax check, not a link — only `arduino-cli` or the IDE will tell you).

Toolchain follow-up:

- [ ] **`fakefish-align-log` is not written.** The procedure is specified in full in
      `firmware/README.md` → "Aligning a log to a recording": detect pulses in the recording,
      cross-correlate against the logged train, fit **offset + drift rate** (a single global
      offset degrades in ~10 minutes at 40 ppm), and report a **match quality**. Deliberately
      deferred until there is a real recording to tune the detection thresholds against —
      written blind it would be plausible and unverifiable. The reader it will build on
      (`fakefish-pulse-log`) is shipped.
- [ ] **The button device could get logging for free.** `pulse_log.h` is L2 and is already
      synced into `eel_fakefish_button/src/eel_core/`; that sketch simply does not include it.
      It has a card mounted already. Not done now, on purpose — the button device's 36 V
      hardware does not exist yet (§1), so there is nothing to verify it on.

## 6. Future control surfaces (documented slots, deliberately unbuilt)

Adding one is a new folder under `firmware/`, not a fork — see "Adding a new surface" in
`firmware/README.md`.

- [ ] **Self-test surface** — a resurrected `fakefish-drv-hwtest`: exercise the HAL, the brake
      idle and the duty sweep without any stimulus library.
- [ ] **De-novo-synthesis handheld** — reuse `locgen` (already L2 core, exactly so this is
      possible) with a button surface instead of the RC decode layer.

## 7. Smaller follow-ups

- [ ] `simulate_firmware.py`'s remaining commands (`dac`, `analyze`) were written to argue for
      the 585.9 kHz / 1-pole chain. The filter model is now correct everywhere, but some of the
      surrounding *prose* still frames its conclusions against that old stage.
- [ ] The two surfaces each carry their own copy of the identical `DebounceState` /
      `debounce_fell` logic (`button_control.h`, `panel_control.h`). Harmless today — separate
      binaries — but it is the obvious next thing to lift into the core if a third surface
      appears.
- [ ] `MERGE_PLAN.md` is a completed historical record, not current guidance (it is written in
      the future tense and its §C9 "22 nF" figure was wrong — corrected in place). Delete it or
      move it to `docs/` at the owner's discretion.

## 8. Not done here, by design

- **Nothing in this repo has been flashed, scoped or field-tested.** Firmware is bench-owned;
  the gate proves it compiles and that the pure logic behaves, nothing more.
- `fakefish-rc/` is still on disk, read-only and unmodified. Keep it until **both** merged
  sketches are bench-verified; its history is preserved in its own `.git`.
