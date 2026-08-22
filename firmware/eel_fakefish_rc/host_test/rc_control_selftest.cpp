// Host self-test for the pure RC logic (no Arduino): width->unit, quantise+hysteresis, the CH3
// throttle map (on/off + tick tempo), the CH4 one-shot trigger (edge-detect, re-arm, no
// boot-time fire), the CH5 randomness + CH6 amplitude maps, and the localization pulse
// renderer. The RHYTHM itself is not tested here — it moved to src/eel_core/loc_rhythm.h and has
// its own parity test against the Python reference (host_test/loc_rhythm_selftest.cpp); what is
// left in this file is the L3 job of turning two RC channels into that model's two knobs.
// Compile + run on a PC (needs EOD_HV from eel_stimuli):
//   g++ -std=c++17 firmware/eel_fakefish_rc/src/eel_core/eel_stimuli.cpp
//       firmware/eel_fakefish_rc/host_test/rc_control_selftest.cpp -lm -o /tmp/rc   (then run /tmp/rc)
#include <cstdio>
#include <cmath>
#include "../rc_control.h"

static int g_fail = 0;
#define CHECK(cond, msg) do { if (!(cond)) { printf("FAIL: %s\n", msg); g_fail++; } } while (0)

// shorthand for the configured CH4 thresholds
static int trig(RcTrigger* t, float u) {
  return rc_trigger_step(t, u, CH4_VOLLEY_THRESH, CH4_CENTER_LO, CH4_CENTER_HI);
}

static void test_unit() {
  RcCal c = {1000, 2000};
  CHECK(rc_unit(1000, c) == 0.0f, "unit min -> 0");
  CHECK(rc_unit(2000, c) == 1.0f, "unit max -> 1");
  CHECK(fabsf(rc_unit(1500, c) - 0.5f) < 1e-6f, "unit mid -> 0.5");
  CHECK(rc_unit(500, c) == 0.0f, "unit below-min clamps to 0");
  CHECK(rc_unit(2500, c) == 1.0f, "unit above-max clamps to 1");
}

static void test_quantize_hyst() {
  CHECK(rc_quantize_hyst(0.0f, 0, 16, 0.3f) == 0, "quant 0 -> level 0");
  CHECK(rc_quantize_hyst(1.0f, 0, 16, 0.3f) == 15, "quant 1 -> level 15");
  CHECK(rc_quantize_hyst(0.37f, 5, 16, 0.3f) == 5, "quant hysteresis holds prev");
  CHECK(rc_quantize_hyst(0.42f, 5, 16, 0.3f) == 6, "quant moves when clearly past");
}

static void test_throttle() {
  CHECK(rc_throttle_on(0.0f) == 0, "throttle bottom -> off");
  CHECK(rc_throttle_on(CH3_OFF_DEADBAND - 0.01f) == 0, "throttle in dead-band -> off");
  CHECK(rc_throttle_on(0.5f) == 1, "throttle up -> on");
  CHECK(CH3_ON_THRESH > CH3_OFF_DEADBAND, "the enable threshold must sit above the dead-band");

  // THE OFF ZONE IS A NOISE MARGIN, NOT A DRIFT BUDGET — and the distinction is the whole design.
  // It has to cover the jitter of a stationary stick around a zero that RcZero re-measures every
  // power-on and keeps ratcheting down; it does NOT have to cover the supply-dependent offset,
  // which measured ~200 us and which no threshold value can absorb without eating a fifth of the
  // travel. That was tried (0.15/0.22) and abandoned. Still check it in MICROSECONDS, because the
  // unit scale is a fiction of the calibration and jitter is not.
  const float span_us = (float)(RC_CAL_THROTTLE_MAX - RC_CAL_THROTTLE_MIN);
  CHECK(CH3_OFF_DEADBAND * span_us >= 50.0f,
        "the CH3 off zone is under 50 us wide — too tight even for stationary-stick jitter");
  CHECK(CH3_ON_THRESH < 0.25f,
        "the CH3 enable threshold is sized like a drift budget — RcZero handles drift, shrink it");
  CHECK(rc_throttle_frac(0.0f) == 0.0f, "throttle frac 0 at bottom");
  CHECK(fabsf(rc_throttle_frac(1.0f) - 1.0f) < 1e-6f, "throttle frac 1 at full");
  CHECK(fabsf(rc_rate_to_hz(0, RC_RATE_STEPS) - LOC_RATE_MIN_HZ) < 1e-4f, "rate lvl0 -> min Hz");
  CHECK(fabsf(rc_rate_to_hz(RC_RATE_STEPS - 1, RC_RATE_STEPS) - LOC_RATE_MAX_HZ) < 1e-4f, "rate top -> 20 Hz");
  // The ladder is a TICK TEMPO, and the model's rate knob is a multiplier of the measured
  // eel's own tempo — so a setting of LOC_NOMINAL_TICK_HZ must come out as exactly 1.0.
  // Getting this conversion wrong would scale every localization interval silently.
  CHECK(fabsf(rc_rate_to_tempo(0, RC_RATE_STEPS) - LOC_RATE_MIN_HZ / LOC_NOMINAL_TICK_HZ) < 1e-5f,
        "rate lvl0 -> tempo = min_hz / nominal");
  CHECK(fabsf(loc_rhythm_rate_for_hz(LOC_NOMINAL_TICK_HZ) - 1.0f) < 1e-6f,
        "the measured eel's tick tempo IS rate 1.0");

  // THE LADDER IS GEOMETRIC, and that is the point of it — a linear ladder put ~85 % of the
  // throttle above anything biological and made the slow end unreachable. Pin the shape:
  // every rung the same RATIO from the next.
  float r0 = rc_rate_to_hz(1, RC_RATE_STEPS) / rc_rate_to_hz(0, RC_RATE_STEPS);
  for (int i = 1; i + 1 < RC_RATE_STEPS; i++) {
    float ri = rc_rate_to_hz(i + 1, RC_RATE_STEPS) / rc_rate_to_hz(i, RC_RATE_STEPS);
    CHECK(fabsf(ri / r0 - 1.0f) < 1e-3f, "rate ladder rungs are not a constant ratio apart");
  }
  CHECK(r0 > 1.0f, "the rate ladder must increase");

  // The measured eel should land near mid-throttle, which is what makes the useful range
  // usable. Geometric mean of the endpoints, within one rung.
  float mid = rc_rate_to_hz((RC_RATE_STEPS - 1) / 2, RC_RATE_STEPS);
  CHECK(mid > LOC_NOMINAL_TICK_HZ / r0 && mid < LOC_NOMINAL_TICK_HZ * r0,
        "the measured eel's tempo should sit within one rung of mid-throttle");

  // ...and the low end must actually be controllable: the step at ~1 Hz is what the field
  // log said was impossible with the old 1 Hz-per-step linear ladder.
  for (int i = 0; i + 1 < RC_RATE_STEPS; i++) {
    float lo = rc_rate_to_hz(i, RC_RATE_STEPS), hi = rc_rate_to_hz(i + 1, RC_RATE_STEPS);
    if (lo <= 1.0f && hi >= 1.0f)
      CHECK(hi - lo < 0.25f, "the rung containing 1 Hz is coarser than 0.25 Hz");
  }
}

// ===== the session zero: one measured reference for all four channels =====
// The failure this exists to prevent is on record: the 2026-08-22 field log never once decoded the
// throttle below 877 us against a 705 us calibration, so "throttle down" could not mean off.
static void test_zero_capture() {
  RcZero z;
  rc_zero_reset(&z);
  CHECK(!rc_zero_armed(&z), "a fresh RcZero is not armed");
  CHECK(rc_zero_offset(&z, RC_CAL_THROTTLE_MIN) == 0, "an unarmed zero applies no offset");

  // Arming takes RC_ZERO_SETTLE_TICKS in-window readings, and lands on their MINIMUM.
  for (uint32_t i = 0; i < RC_ZERO_SETTLE_TICKS - 1; i++)
    CHECK(!rc_zero_step(&z, 900u + (i == 3 ? 40u : 0u)), "not armed before the settle window ends");
  CHECK(rc_zero_step(&z, 900u), "armed once the settle window completes");
  CHECK(z.zero_us == 900u, "the captured zero is the minimum over the settle window");

  // PROPERTY 1: it only ever ratchets DOWN. A higher reading must never raise it, or a throttle
  // pushed up during arming would redefine "off" as "somewhat open".
  rc_zero_step(&z, 1200u);
  CHECK(z.zero_us == 900u, "a higher reading must never raise the zero");
  // PROPERTY 2: it follows the pack down.
  rc_zero_step(&z, 850u);
  CHECK(z.zero_us == 850u, "a lower reading tracks the sagging pack");

  // PROPERTY 4: the plausibility window. Neither a failsafe frame nor a decode glitch may capture.
  rc_zero_step(&z, RC_ZERO_MIN_US - 1u);
  CHECK(z.zero_us == 850u, "a width below the window must not capture");
  rc_zero_step(&z, RC_ZERO_MAX_US + 1u);
  CHECK(z.zero_us == 850u, "a width above the window must not capture");
  CHECK(rc_zero_armed(&z), "an out-of-window reading must not disarm");

  // A first reading outside the window must not arm on its own, either.
  RcZero z2; rc_zero_reset(&z2);
  for (int i = 0; i < 50; i++) CHECK(!rc_zero_step(&z2, 3000u), "out-of-window traffic never arms");
  CHECK(z2.zero_us == 0, "...and never captures");
}

static void test_zero_applied() {
  // THE WHOLE POINT: a throttle at its mechanical stop must decode to 0.00 whatever the supply did
  // to the widths. Replay the two states this rig has actually been measured in.
  const uint32_t FLAT = 705u;   // 2026-08-07 calibration, nearly empty pack
  const uint32_t FULL = 905u;   // 2026-08-22 field log, fresh pack: ~200 us higher
  RcCal cal = RC_CAL[RC_IDX_THROTTLE];

  // Without the correction the fresh-pack rest decodes a fifth of the way up the stick — which is
  // exactly why the device could not be stopped from shore.
  CHECK(rc_unit(FULL, cal) > 0.15f, "uncorrected, a full-pack resting throttle decodes well above 0");

  RcZero z; rc_zero_reset(&z);
  for (uint32_t i = 0; i < RC_ZERO_SETTLE_TICKS; i++) rc_zero_step(&z, FULL);
  int32_t off = rc_zero_offset(&z, RC_CAL_THROTTLE_MIN);
  CHECK(off == (int32_t)FULL - (int32_t)RC_CAL_THROTTLE_MIN, "the offset is zero minus the calibrated min");
  CHECK(rc_unit_off(FULL, cal, off) == 0.0f, "a corrected resting throttle decodes to exactly 0");
  CHECK(rc_throttle_on(rc_unit_off(FULL, cal, off)) == 0, "...and therefore reads OFF");

  // Same stop on a flat pack, with a zero captured then: also exactly 0.
  RcZero zf; rc_zero_reset(&zf);
  for (uint32_t i = 0; i < RC_ZERO_SETTLE_TICKS; i++) rc_zero_step(&zf, FLAT);
  CHECK(rc_unit_off(FLAT, cal, rc_zero_offset(&zf, RC_CAL_THROTTLE_MIN)) == 0.0f,
        "a flat-pack resting throttle also decodes to exactly 0");

  // SPAN IS PRESERVED, not rescaled: the offset shifts both ends, so full travel still reads 1.0.
  CHECK(rc_unit_off(FULL + (cal.us_max - cal.us_min), cal, off) == 1.0f,
        "full travel above the zero still decodes to 1.0 — the offset shifts, it does not scale");

  // ONE ZERO CORRECTS FOUR CHANNELS. The offset belongs to the shared opto path, so the pots —
  // which the transmitter does not force anywhere — inherit the throttle's measurement.
  for (int i = 0; i < RC_N_CHANNELS; i++) {
    float rest = rc_unit_off(RC_CAL[i].us_min + off, RC_CAL[i], off);
    CHECK(rest == 0.0f, "every channel's calibrated minimum, shifted by the session offset, reads 0");
  }
  // ...and that is what makes randomness 0 (the metronome) reachable again: the same log showed
  // rand_m bottoming out at 200, never 0.
  float rnd_rest = rc_unit_off(RC_CAL[RC_IDX_RANDOM].us_min + off, RC_CAL[RC_IDX_RANDOM], off);
  CHECK(rc_randomness(rc_quantize_hyst(rnd_rest, 0, RC_RANDOM_STEPS, 0.0f), RC_RANDOM_STEPS) == 0.0f,
        "a corrected pot at rest reaches randomness 0, the metronome end of the knob");
}

static void test_throttle_gate() {
  const float on = 0.12f, off = 0.06f; const uint32_t deb = 30u;
  // A transient glitch (raw high but shorter than the debounce) never enables -> ZERO pulses at rest.
  uint32_t cnt = 0; bool st = false;
  for (int i = 0; i < 5; i++) st = rc_throttle_gate(0.9f, st, &cnt, on, off, deb);   // 5 huge-glitch ticks
  CHECK(!st, "gate: glitch shorter than debounce stays OFF");
  st = rc_throttle_gate(0.0f, st, &cnt, on, off, deb);
  CHECK(!st && cnt == 0, "gate: glitch end resets the debounce");
  // A sustained rise enables exactly at the debounce count.
  cnt = 0; st = false;
  for (uint32_t i = 0; i < deb - 1; i++) st = rc_throttle_gate(0.5f, st, &cnt, on, off, deb);
  CHECK(!st, "gate: not yet ON one tick before the debounce");
  st = rc_throttle_gate(0.5f, st, &cnt, on, off, deb);
  CHECK(st, "gate: ON after the full debounce");
  // Hysteresis: once ON, holding in the [off,on) band stays on; below off_deadband -> OFF immediately.
  st = rc_throttle_gate(0.08f, st, &cnt, on, off, deb);
  CHECK(st, "gate: hysteresis holds ON in the [off,on) band");
  st = rc_throttle_gate(0.03f, st, &cnt, on, off, deb);
  CHECK(!st && cnt == 0, "gate: drop below dead-band -> OFF now + reset");
  // Sitting in the [off,on) band while OFF never builds toward enable.
  cnt = 0; st = false;
  for (int i = 0; i < 100; i++) st = rc_throttle_gate(0.09f, st, &cnt, on, off, deb);
  CHECK(!st, "gate: sustained in the dead band (while OFF) never enables");
}

static void test_trigger() {
  RcTrigger t = {};
  CHECK(trig(&t, 0.5f) == 0, "trig: init centre, no fire");
  CHECK(trig(&t, 0.90f) == 1, "trig: throw high -> fire one trial");
  CHECK(trig(&t, 0.90f) == 0, "trig: hold high, no refire");
  CHECK(trig(&t, 0.65f) == 0, "trig: partial release (dead space), no fire");
  CHECK(trig(&t, 0.90f) == 0, "trig: re-throw without centring, no fire");
  CHECK(trig(&t, 0.50f) == 0, "trig: return to centre re-arms");
  CHECK(trig(&t, 0.95f) == 1, "trig: centre then high -> fires again");
}

// The LOW half of the axis is inert by design: the operator must not be able to choose the
// trial type (the firmware draws it), so throwing left can neither fire NOR disturb the arm.
static void test_trigger_low_throw_is_inert() {
  RcTrigger t = {};
  CHECK(trig(&t, 0.5f) == 0, "trig low: init centre");
  CHECK(trig(&t, 0.10f) == 0, "trig low: throw low -> NOTHING");
  CHECK(trig(&t, 0.10f) == 0, "trig low: hold low -> still nothing");
  CHECK(trig(&t, 0.00f) == 0, "trig low: full low -> still nothing");
  // ...and the arm survives it, so a high throw straight from low still fires without
  // needing to pass through centre first.
  CHECK(trig(&t, 0.90f) == 1, "trig low: low throw did not consume the arm");
  CHECK(trig(&t, 0.50f) == 0, "trig low: centre re-arms");
  CHECK(trig(&t, 0.10f) == 0, "trig low: low again -> nothing");
  CHECK(trig(&t, 0.90f) == 1, "trig low: still armed after another low throw");
}

static void test_trigger_boot() {
  // Cold boot with the CH4 axis ALREADY thrown must NOT fire until it returns to centre.
  RcTrigger t = {};
  CHECK(trig(&t, 1.0f) == 0, "trig boot: thrown at boot -> no fire");
  CHECK(trig(&t, 1.0f) == 0, "trig boot: held thrown -> no fire");
  CHECK(trig(&t, 0.5f) == 0, "trig boot: return to centre re-arms");
  CHECK(trig(&t, 1.0f) == 1, "trig boot: throw after centring -> fires");
}

static void test_amp_randomness() {
  CHECK(fabsf(rc_master_amp(0, RC_AMP_STEPS) - MASTER_AMP_MIN) < 1e-6f, "master lvl0 -> MIN");
  CHECK(fabsf(rc_master_amp(RC_AMP_STEPS - 1, RC_AMP_STEPS) - MASTER_AMP_MAX) < 1e-6f, "master top -> MAX");
  // The SHIPPED ratio, pinned as a literal on purpose. VOLLEY_AMP_RATIO is generated from
  // shared/stim_constants.json, so changing it there must also fail here — the same
  // two-file-edit discipline the pulse-log golden enforces. It moved 2 -> 4 on 2026-08-21
  // because the synthetic volleys now carry the measured amplitude envelope, whose tail
  // reaches ~0.34 of a volley's own peak; at 2:1 that tail sat on the localization level.
  CHECK(fabsf(VOLLEY_AMP_RATIO - 4.0f) < 1e-6f, "VOLLEY_AMP_RATIO is the shipped 4:1");
  // Localization is DERIVED from the volley, so the ratio must hold at EVERY pot position,
  // not just at full scale. The bug this pins against is the retired "loc = pot, volley =
  // 2x pot clamped to 1.0", where the two CONVERGED once the pot passed half.
  CHECK(fabsf(rc_loc_amp(1.0f) - 1.0f / VOLLEY_AMP_RATIO) < 1e-6f, "loc == volley / ratio at full scale");
  CHECK(fabsf(rc_loc_amp(0.4f) - 0.4f / VOLLEY_AMP_RATIO) < 1e-6f, "loc == volley / ratio off full scale");
  CHECK(rc_loc_amp(1.0f) > rc_loc_amp(0.4f), "loc tracks the volley, never converges to it");
  // CH5 is the fitted model's RANDOMNESS knob, not the retired jitter CV. ITS TWO ENDPOINTS ARE
  // THE POINT: hard down is a perfect metronome at the nominal tempo, hard up is EXACTLY 1.0 —
  // the measured eel, the model in undiluted control. Pinned as literals, both ends, because
  // every value in between is an interpolation between two things that mean something and a pot
  // that stopped somewhere else (it ran to 1.5 until 2026-08-22) is a dial with a region that
  // has no referent at all.
  CHECK(fabsf(rc_randomness(0, RC_RANDOM_STEPS) - 0.0f) < 1e-6f, "randomness lvl0 -> 0 (metronome)");
  CHECK(fabsf(rc_randomness(RC_RANDOM_STEPS - 1, RC_RANDOM_STEPS) - LOC_RANDOMNESS_MAX) < 1e-6f,
        "randomness top -> LOC_RANDOMNESS_MAX");
  CHECK(fabsf(LOC_RANDOMNESS_MAX - 1.0f) < 1e-6f,
        "the top of the CH5 pot must BE the measured eel (randomness 1.0), not past it");
  CHECK(LOC_RANDOMNESS_MAX <= LOC_KNOB_MAX, "the pot must not run past the shipped gain table");
  // ...and hard up must land on 1.0 exactly, not merely near it: the top rung IS the endpoint.
  CHECK(fabsf(rc_randomness(RC_RANDOM_STEPS - 1, RC_RANDOM_STEPS) - 1.0f) < 1e-6f,
        "the top pot step must land on randomness 1.0, the measured eel");
  // Monotone in between, so the dial reads left-to-right as "more like a fish".
  for (int i = 0; i + 1 < RC_RANDOM_STEPS; i++)
    CHECK(rc_randomness(i + 1, RC_RANDOM_STEPS) > rc_randomness(i, RC_RANDOM_STEPS),
          "the randomness pot must increase monotonically");
}

static void test_locgen() {
  LocGen g;
  locgen_reset(&g, 300u, 1.0f, 1);
  int onset, boundary, onsets = 0, boundaries = 0, first_boundary = -1, last_onset = -1;
  bool silence_ok = true;
  for (int t = 0; t < 300; t++) {
    int16_t s = locgen_tick(&g, &onset, &boundary);
    if (onset) { onsets++; last_onset = t; }
    if (boundary) { boundaries++; if (first_boundary < 0) first_boundary = t; }
    if (t >= EOD_HV_LEN && t < 299 && s != 0) silence_ok = false;
  }
  CHECK(onsets == 1 && last_onset == 0, "locgen: one onset at phase 0");
  CHECK(boundaries == 1 && first_boundary == 299, "locgen: boundary at end of interval");
  CHECK(silence_ok, "locgen: silent between pulse end and next onset");

  locgen_reset(&g, 300u, 1.0f, 1);
  int expect_next = 0; bool periodic = true;
  for (int t = 0; t < 3000; t++) {
    int16_t s = locgen_tick(&g, &onset, &boundary); (void)s;
    if (onset && t != expect_next) periodic = false;
    if (onset) expect_next = t + 300;
    if (boundary) g.ipi = 300u;   // a fixed interval: this tests the renderer, not the rhythm
  }
  CHECK(periodic, "locgen: a fixed interval gives an exactly periodic train");

  locgen_reset(&g, 300u, 0.5f, 1);
  int16_t peak = 0;
  for (int t = 0; t < EOD_HV_LEN; t++) { int o, b; int16_t s = locgen_tick(&g, &o, &b); if (s > peak) peak = s; }
  CHECK(peak > 0 && peak <= 16384, "locgen: amp 0.5 scales the EOD peak");
}

int main() {
  test_unit();
  test_quantize_hyst();
  test_throttle();
  test_throttle_gate();
  test_trigger();
  test_trigger_low_throw_is_inert();
  test_trigger_boot();
  test_zero_capture();
  test_zero_applied();
  test_amp_randomness();
  test_locgen();
  if (g_fail == 0) { printf("OK\n"); return 0; }
  printf("%d CHECK(s) FAILED\n", g_fail);
  return 1;
}
