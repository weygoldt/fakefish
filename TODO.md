# TODO

Open items after the 3-layer merge. Everything here is **owner/bench work** or a decision that
needs a measurement — nothing in this file is blocking `make check`, which is green.

---

## 1. Hardware the button device still needs

The hand-held SD player **migrated off** its old direct-pin ~5.7 Vpp stage onto the shared
36 V DRV8871 stage. That hardware **has to be built** before the sketch can be bench-tested.

- [ ] Build the 36 V DRV8871 output stage for the hand-held unit (2× DRV8871, IN1 on pins 2/3
      held high, **IN2 on pins 22/23** — NOT 0/1, which this line said until 2026-08-21 and which
      have no FTM channel at all on a Teensy 3.5 — plus the per-channel output filter). Double-check
      IN1/IN2 orientation at each driver before first power-up: see the hazard note in
      `firmware/eel_core/config.h`, because both mis-wirings are silent on the LED and one of them
      holds the electrodes at full-rail DC. **Build the filter as the
      3-section network** in `firmware/README.md` → "A better network for the next build"
      (`220 Ω/150 nF → 110 Ω/100 nF → 110 Ω/100 nF`), not as a copy of the RC unit's 2-section
      one: same parts cost and same dissipation, 39 % less pulse distortion. See §4.
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

## 3b. Set `OUT_PEDESTAL_DUTY` from a measurement, not from the datasheet bound

The driver dead zone is **implemented and documented** (`firmware/README.md` → "The driver dead
zone"): both bridges now idle at `OUT_PEDESTAL_DUTY` while armed, so no sample is ever commanded
into the DRV8871's minimum-pulse-width gap. This removed a level-dependent distortion worth
3-32 % RMS shape error (and outright silence below ~1/16 amplitude) that was first noticed **by
ear** — the pulses changed character as the RC amplitude control came down.

The shipped value **21** is the datasheet-*guaranteed* 800 ns bound, chosen to be safe without
measuring. A typical part is fine at **11**, halving both costs (headroom 8.2 % → 4.3 %, armed
idle dissipation ~0.45 W → ~0.24 W per channel).

- [ ] **Measure the real threshold** with the `AMP_DEBUG` sweep walking the bottom of the range
      (`{ 0, 1, 2, 3, 4, 6, 8, 10, 12, 14, 16, 18, 21, 24, 32 }`). Find the lowest code that
      produces output *and* the lowest code from which output is linear; set `OUT_PEDESTAL_DUTY` a
      couple above the second. Procedure in `firmware/README.md`.
- [ ] While there, read the **duty→volts intercept**: `t_DEAD = 220 ns` is 2.2 % of the carrier
      period, worth up to 5.6 duty codes of systematic offset, and is not modelled anywhere.
- [ ] **Confirm disarmed idle is still hard 0 V** single-ended (it commands a 39 ns pulse the
      driver should ignore) and that battery drain with the lever down is unchanged.
- [ ] **Confirm by ear**: the pulse should keep its character all the way down the amplitude range
      instead of thinning out and vanishing.

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
- [ ] **A better 3-section network is designed and documented but NOT built** —
      `firmware/README.md` → "A better network for the next build". `220 Ω/150 nF → 110 Ω/100 nF →
      110 Ω/100 nF` per channel: same total series resistance, same dissipation, *more* carrier
      rejection (−60.7 vs −59.4 dB) and **39 % less pulse shape error** (2.93 % vs 4.77 %
      open-circuit). The present network's problem is that its two identical unbuffered sections
      load each other and split the poles 6.9:1, so it pays the passband droop of a 1.23 kHz filter
      and collects only the stopband of a 3.29 kHz one. **Build the hand-held unit's stage (§1)
      this way from the start** — it does not exist yet, so it costs nothing extra. Retrofitting
      the RC unit is optional and lower priority than §3b.
- [ ] **Measure the recorder's anti-alias response at 100 kHz.** Every carrier-rejection
      requirement rests on it and it is documented nowhere: with a 2-pole AA the requirement is
      −55 dB (the present network has 4.9 dB spare), with a 1-pole AA it is −69 dB and the present
      network is 10 dB short. Inject a 100 kHz tone at a grid electrode, look for the folded
      4 kHz line.

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

## 6. Volley model — replaced by a proper fit, 2026-08-21

**The intermediate calibration this section used to describe is gone.** It rested on the 41
volleys in `data/real_volley_population.npz` — tracker *fragments*, whose durations measured the
segmentation rather than the animal — and it said explicitly that full-dataset statistics would
supersede it. They have.

The volley model is now **vendored**, not fitted here: `src/fakefish/volley_model.py` +
`data/volley_model_params.json`, byte-identical copies from
`eeltracker/analyses/volley_dynamics/`, where it was fitted to the **200 strongest hunting
volleys** in the FLONA 2025 dataset (43 recordings, 16 sites) and validated by re-fitting
synthetic draws with the same estimator. The spec ships as
[`docs/VOLLEY_GENERATIVE_SPEC.md`](docs/VOLLEY_GENERATIVE_SPEC.md); CLAUDE.md invariant 10 is the
rule that keeps the copies copies.

What changed in the library:

| | before | now |
|---|---|---|
| synthetic volleys | 21, from a designed 0.1–4 s duration ladder | **100**, each an independent draw from the fitted joint distribution |
| duration (quartiles) | 0.20 / 0.65 / 2.18 s | **0.31 / 0.46 / 0.68 s** (real: 0.28 / 0.47 / 0.75) |
| pulses | 64 / 89 / 189 | **58 / 90 / 121** (real: 58 / 88 / 142) |
| rate texture | lognormal jitter, CV 0.166, around an exponential decay | rate **integrated** off `r_start·exp(-λf)` with per-volley CV2 from its ECDF + an OU wander |
| amplitude | linear ramp to a 0.8 floor, onset at a random point in the first third | the **measured** envelope: ~22 % decay, per-volley trend from its ECDF, 1.4 % pulse jitter |
| IPI floor | 2.5 ms (+ a 381 Hz "physics ceiling") | **2.00 ms**, set by the EOD's 1.92 ms *energy width*; no ceiling needed |
| localization level | volley / 2 | **volley / 4** |
| library | 34 items, 12.6 kB | **113 items, 37.4 kB** of a 131 kB budget |

Verified on the re-export: `EOD_HV`, the 7 real scenes, their lead gaps and all 8 localization
trains came back **byte-identical** — only the synthetic volleys moved. Synthesis QC reports
**no overlap-clip** at the 2.00 ms floor, and `scan` reproduced
`data/multifish_volley_candidates.csv` byte-for-byte.

### Why the localization level moved 2:1 → 4:1

Not cosmetic. The synthetic volleys now carry the *measured* within-volley amplitude envelope,
whose 5th-percentile tail reaches ~0.34 of a volley's own peak — so at 2:1 the end of a decaying
volley sat exactly on the localization level, which is the separation that ratio existed to
provide. The alternative was the retired `VOLLEY_DECAY_FLOOR = 0.8`, which flattened the measured
decay on 64 % of volleys. At 4:1 only **3 %** of volleys end below the localization level, and the
envelope ships intact. The absolute volley-to-resting step is *not* a measurement (spec §5) —
it is a knob, and this is that knob.

### What is still open

- [ ] **The long tail is a known gap.** The fitted duration distribution tops out around 2.3 s,
      but volleys in the field run much longer (~20 s by observation). That is a blind spot of
      the source analysis, not biology: a 25 s analysis window cannot contain a 20 s volley
      without right-censoring it, and censored bursts were **dropped rather than fitted**. The
      library therefore has nothing above ~2.3 s, where the old ladder reached 4 s. Deliberate —
      extrapolating a fitted distribution past its support would look like data and not be it.
      Decide whether the experiment needs long volleys represented, and if so, on what basis.
- [ ] **A fitted localization model.** The volley model deliberately does not cover localization
      (its §1.3), and its §2.3 between-volley rate is an explicit upper bound. So
      `LOC_SYNTH_RATES_HZ = [1, 2, 3, 5, 7, 10]` is still a *designed* ladder. The seam is marked
      in `synthetic_volleys.py`; a fitted model drops in the same way the volley one did.
- [ ] **The 2.00 ms IPI floor leaves a delta spike: 9.5 % of shipped intervals sit exactly on
      it**, where real volleys have a smooth tail down to ~2.1 ms. It is the right clamp —
      unclamped, 3 % of volleys overlap-clip, and the spec's §5 says the sub-2 ms intervals in
      the source data are spurious *extra detections* rather than extra EODs — but it removes
      them by stacking them on one value rather than redistributing them. Anyone analysing a
      recorded playback will see that spike. Decide whether it matters; redistributing (e.g.
      re-drawing a violating interval rather than clamping it) is the alternative.
- [ ] **The amplitude ECDF's tail is long, and the deep end is probably fish movement.** The
      trend is drawn per volley from the measured ECDF, so the deepest draw in the shipped
      population fades to **8 %** of its own peak and holds there; 3 % of volleys end below the
      localization level. Spec §1.4 warns that recorded amplitude is source amplitude ×
      distance attenuation and that a striking fish moves — a 92 % fade is far more likely the
      animal swimming away than its organ winding down. Kept because truncating a fitted tail
      by eye is how a model stops being one, but the RC device draws uniformly, so ~1 % of
      trials would deliver a volley that fades to near-silence. Worth a decision.
- [x] **`data/real_volley_population.npz` is now QC-only, and was regenerated 2026-08-21.** It
      is a *different* selection from the model's (this repo's single-fish criteria, on
      fragments), which is what makes it a useful independent cross-check in `compare` — it
      catches a wiring mistake, not a modelling one. Regenerating it inside this repo for the
      first time shrank it **41 → 29**: the imported file predated this repo's filters and
      carried 9 events peaking above `VOLLEY_MULTIFISH_PEAK_HZ` (two fish volleying together,
      rates adding) plus one below the 100 Hz floor. The 29 are a strict subset with the same
      distribution, so nothing downstream moved — the grey reference curve in `compare` is
      simply no longer contaminated. `fakefish-synth-volleys analyze` refreshes it (needs the
      recordings + `--group export`).

**Settled, not open:** program D's size. It renders every localization × volley pair, the only
quadratic part of the card, so the bigger volley pool took it from 208 WAVs to **840 (~544 MB)**.
The owner's cards are ≥ 64 GB, where that is under 1 % — so all-pairs stays the default and
nothing is capped. `build_sd_card` now just *reports* the count and size (it is the slow part of
a build); `--d-pairings N` remains for a smaller card.

**Kept from the old calibration, because it was the right lesson:** measure a peak as a
*sustained* rate (`sustained_peak_hz`), never as `1/min(IPI)` — an extreme-value statistic whose
expected value grows with the number of intervals drawn, so a bigger sample makes it *worse*.
`sustained_peak_hz` survives as QC on both the real and synthetic populations.

## 6b. Localization rhythm — fitted for the RC device, still a ladder on the card

**Done 2026-08-21:** the RC device's resting rhythm is now a model fitted to 99 010 measured
resting intervals, vendored from `eeltracker/analyses/localization_rhythm/` exactly as the volley
model was (CLAUDE.md invariant 11). It replaced a unit-mean lognormal draw, which is a *renewal*
process and therefore has zero log-interval autocorrelation at every lag where a real eel has
0.55 / 0.36 / 0.21 at lags 1 / 5 / 20.

**The SD path is done too.** `generate_localization` now draws from the model, and the library
was re-exported on 2026-08-21: `EOD_HV`, the 5 real volleys, both real localization exemplars and
all 100 synthetic volleys came back byte-identical, and exactly the 6 synthetic localization items
(107–112) moved. The library is at format **v4** (uint32 IPI, so multi-second silences fit) and
the packet is 58.7 kB of a 131 kB budget.

- [ ] **Rebuild the WAV card** with `fakefish-build-card` before the next outing — the committed
      card content predates all of this. Note program D's 5 s lead gets only ~2 pulses from the
      1 Hz rung now (a 1 Hz fish with the model's tail); pick a faster rung if that lead needs to
      be denser.
- [ ] **Bench:** confirm the ISR still closes an interval inside its 20 µs budget on the part you
      actually flash. The draw costs three `expf`, two Box–Muller pairs and eight PRNG words —
      once per *interval*, a few times a second, not per sample tick. Comfortable on a 4.1; a 3.5
      at 120 MHz is the one worth scoping. If it is tight, the spec sanctions two exact-in-context
      reductions (§7): drop to `n_components = 2`, or replace the medium/slow `expf` with
      `1 - dt/tau`. Neither is done in the firmware, because either would put the C and the Python
      reference on different arithmetic and cost the golden parity test.
- [ ] **Bench:** listen to / scope a few minutes of the resting train at randomness 1.0 and
      confirm the multi-second silences look right rather than alarming. 1.5 % of intervals exceed
      5 s and 0.6 % exceed 10 s — that is measured behaviour, not a fault, and the device is
      *supposed* to go quiet occasionally.
- [ ] **Field habit:** keep CH5 near 1.0. At 0 the train is an exact metronome, which degrades
      log↔recording alignment badly (§5 relies on the train's irregularity for a sharp
      cross-correlation peak).

---

## 7. Future control surfaces (documented slots, deliberately unbuilt)

Adding one is a new folder under `firmware/`, not a fork — see "Adding a new surface" in
`firmware/README.md`.

- [ ] **Self-test surface** — a resurrected `fakefish-drv-hwtest`: exercise the HAL, the brake
      idle and the duty sweep without any stimulus library.
- [ ] **De-novo-synthesis handheld** — reuse `locgen` (already L2 core, exactly so this is
      possible) with a button surface instead of the RC decode layer.

## 8. Smaller follow-ups

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

## 9. Not done here, by design

- **Nothing in this repo has been flashed, scoped or field-tested.** Firmware is bench-owned;
  the gate proves it compiles and that the pure logic behaves, nothing more.
- `fakefish-rc/` is still on disk, read-only and unmodified. Keep it until **both** merged
  sketches are bench-verified; its history is preserved in its own `.git`.
