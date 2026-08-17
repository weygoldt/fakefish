# TODO

Open items after the 3-layer merge. Everything here is **owner/bench work** or a decision that
needs a measurement — nothing in this file is blocking `make check`, which is green.

---

## 1. Hardware the button device still needs

The hand-held SD player **migrated off** its old direct-pin ~5.7 Vpp stage onto the shared
36 V DRV8871 stage. That hardware **has to be built** before the sketch can be bench-tested.

- [ ] Build the 36 V DRV8871 output stage for the hand-held unit (2× DRV8871, IN1 on pins 2/3
      held high, IN2 on pins 0/1, plus the per-channel 2-pole RC).
- [ ] **Do not flash `eel_fakefish_button` onto the old direct-pin hardware.** Full scale is now
      ~72 Vpp differential instead of ~5.7 Vpp.
- [ ] Re-scope the absolute output and set `MASTER_GAIN` before the device goes near an animal.
      A `0.90` volley pulse is ~30 V peak per electrode at nominal rail, open-circuit.

## 2. The 10 kHz marker level budget — the one genuinely open question

The output filter (2× 220 Ω / 220 nF per channel, composite −3 dB ≈ 1.23 kHz) attenuates the
SD device's 10 kHz sine anchor by **−21.8 dB**, against −1.4 dB under the 1-pole ~16 kHz filter
the docs used to describe. Whether that still closes depends on the detector, and the two
framings genuinely disagree:

- by **energy** (the narrowband spectral-peak detector this design assumes) it closes
  comfortably — a 1 s lead-in is only 4.1 dB below a *single* volley EOD pulse, and the 10 s
  calibration tone is 5.9 dB above one, before ~44–54 dB of processing gain;
- by **peak amplitude** (a threshold detector) the marker-to-pulse ratio degrades by 21.3 dB.

- [ ] **Bench check: does the marker still show up in a real recording at usable SNR?** This is
      the deciding measurement; the analysis cannot settle it.
- [ ] It should get *relatively* better in water — 10 kHz is nearly load-invariant while the EOD
      band is divided down by the load. Confirm in situ rather than open-circuit.
- [ ] If it does need fixing, note the constraints:
      - raising the marker **level** means editing `shared/stim_constants.json` and re-running
        `make gen` — the card and the galleries then move together, by construction;
      - changing the **frequency** additionally breaks the detector contract frozen in
        `data/stimuli_provenance.json` (`lead_in_marker.nominal_freq_hz = 10000`) against every
        recording already made;
      - the only alternative anchor satisfying the existing whole-cycles rule is **6250 Hz**
        (50000/8), worth ~5.9 dB. 8333 Hz does *not* qualify — 50000/6 is not an integer.

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

## 5. Future control surfaces (documented slots, deliberately unbuilt)

Adding one is a new folder under `firmware/`, not a fork — see "Adding a new surface" in
`firmware/README.md`.

- [ ] **Self-test surface** — a resurrected `fakefish-drv-hwtest`: exercise the HAL, the brake
      idle and the duty sweep without any stimulus library.
- [ ] **De-novo-synthesis handheld** — reuse `locgen` (already L2 core, exactly so this is
      possible) with a button surface instead of the RC decode layer.

## 6. Smaller follow-ups

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

## 7. Not done here, by design

- **Nothing in this repo has been flashed, scoped or field-tested.** Firmware is bench-owned;
  the gate proves it compiles and that the pure logic behaves, nothing more.
- `fakefish-rc/` is still on disk, read-only and unmodified. Keep it until **both** merged
  sketches are bench-verified; its history is preserved in its own `.git`.
