// Host self-test for the pure RC logic (no Arduino): width->unit, quantise+hysteresis, the CH3
// throttle map (on/off + rate), the CH4 bidirectional one-shot trigger (volley/sham edge-detect,
// re-arm, no boot-time fire), the CH5 jitter + CH6 amplitude maps, the lognormal IPI draw
// (jitter=0 => exactly periodic; mean preserved; refractory + tail clamps), and the localization
// scheduler. Compile + run on a PC (needs EOD_HV from eel_stimuli):
//   g++ -std=c++17 -I firmware/eel_fakefish firmware/eel_fakefish/eel_stimuli.cpp
//       firmware/eel_fakefish/host_test/rc_control_selftest.cpp -lm -o /tmp/rc   (then run /tmp/rc)
#include <cstdio>
#include <cmath>
#include "../rc_control.h"

static int g_fail = 0;
#define CHECK(cond, msg) do { if (!(cond)) { printf("FAIL: %s\n", msg); g_fail++; } } while (0)

static uint32_t s_lcg = 12345u;
static float uni() { s_lcg = s_lcg * 1664525u + 1013904223u; return ((s_lcg >> 8) & 0xFFFFFF) * (1.0f / 16777216.0f); }

// shorthand for the configured CH4 thresholds
static int trig(RcTrigger* t, float u) {
  return rc_trigger_step(t, u, CH4_VOLLEY_THRESH, CH4_SHAM_THRESH, CH4_CENTER_LO, CH4_CENTER_HI);
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
  CHECK(rc_throttle_frac(0.0f) == 0.0f, "throttle frac 0 at bottom");
  CHECK(fabsf(rc_throttle_frac(1.0f) - 1.0f) < 1e-6f, "throttle frac 1 at full");
  CHECK(fabsf(rc_rate_to_ipi_samp(0, RC_RATE_STEPS) - (float)SAMPLE_RATE_HZ / LOC_RATE_MIN_HZ) < 1.0f, "rate lvl0 -> min Hz");
  CHECK(fabsf(rc_rate_to_ipi_samp(RC_RATE_STEPS - 1, RC_RATE_STEPS) - (float)SAMPLE_RATE_HZ / LOC_RATE_MAX_HZ) < 1.0f, "rate top -> 20 Hz");
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
  CHECK(trig(&t, 0.5f) == RC_TRIG_NONE, "trig: init centre, no fire");
  CHECK(trig(&t, 0.90f) == RC_TRIG_VOLLEY, "trig: throw high -> VOLLEY");
  CHECK(trig(&t, 0.90f) == RC_TRIG_NONE, "trig: hold high, no refire");
  CHECK(trig(&t, 0.65f) == RC_TRIG_NONE, "trig: partial release (dead space), no fire");
  CHECK(trig(&t, 0.90f) == RC_TRIG_NONE, "trig: re-throw without centring, no fire");
  CHECK(trig(&t, 0.50f) == RC_TRIG_NONE, "trig: return to centre re-arms");
  CHECK(trig(&t, 0.10f) == RC_TRIG_SHAM, "trig: throw low -> SHAM");
  CHECK(trig(&t, 0.10f) == RC_TRIG_NONE, "trig: hold low, no refire");
  trig(&t, 0.50f);
  CHECK(trig(&t, 0.95f) == RC_TRIG_VOLLEY, "trig: centre then high -> VOLLEY");
}

static void test_trigger_boot() {
  // Cold boot with the CH4 axis ALREADY thrown must NOT fire until it returns to centre.
  RcTrigger t = {};
  CHECK(trig(&t, 1.0f) == RC_TRIG_NONE, "trig boot: thrown at boot -> no fire");
  CHECK(trig(&t, 1.0f) == RC_TRIG_NONE, "trig boot: held thrown -> no fire");
  CHECK(trig(&t, 0.5f) == RC_TRIG_NONE, "trig boot: return to centre re-arms");
  CHECK(trig(&t, 1.0f) == RC_TRIG_VOLLEY, "trig boot: throw after centring -> fires");
}

static void test_amp_jitter() {
  CHECK(fabsf(rc_master_amp(0, RC_AMP_STEPS) - MASTER_AMP_MIN) < 1e-6f, "master lvl0 -> MIN");
  CHECK(fabsf(rc_master_amp(RC_AMP_STEPS - 1, RC_AMP_STEPS) - MASTER_AMP_MAX) < 1e-6f, "master top -> MAX");
  CHECK(fabsf(rc_loc_amp(1.0f) - 0.5f) < 1e-6f, "loc == half the volley at full scale");
  CHECK(fabsf(rc_loc_amp(0.4f) - 0.2f) < 1e-6f, "loc == volley / VOLLEY_AMP_RATIO");
  CHECK(fabsf(rc_jitter_cv(0, RC_JITTER_STEPS) - 0.0f) < 1e-6f, "jitter lvl0 -> 0 (perfectly even)");
  CHECK(fabsf(rc_jitter_cv(RC_JITTER_STEPS - 1, RC_JITTER_STEPS) - LOC_CV_MAX) < 1e-6f, "jitter top -> CV_MAX");
}

static void test_ipi() {
  CHECK(rc_ipi_samples(2500.0f, 0.0f, 3.7f, 250u) == 2500u, "ipi cv=0 exact (z=3.7)");
  CHECK(rc_ipi_samples(2500.0f, 0.0f, -2.0f, 250u) == 2500u, "ipi cv=0 exact (z=-2)");
  CHECK(rc_ipi_samples(100.0f, 0.0f, 0.0f, 250u) == 250u, "ipi clamps to refractory");
  CHECK(rc_ipi_samples(2500.0f, 0.8f, 6.0f, 250u) == (uint32_t)(2500.0f * RC_IPI_MAX_FACTOR + 0.5f),
        "ipi upper-clamped to mean*RC_IPI_MAX_FACTOR");
  double sum = 0; int n = 200000;
  for (int i = 0; i < n; i++) {
    float z = rc_std_normal(uni() * 0.999999f + 1e-6f, uni());
    sum += rc_ipi_samples(2500.0f, 0.5f, z, 250u);
  }
  CHECK(fabs(sum / n - 2500.0) < 60.0, "ipi cv=0.5 preserves mean (~2500)");
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
    if (boundary) g.ipi = rc_ipi_samples(300.0f, 0.0f, uni(), LOC_REFRACTORY_SAMP);
  }
  CHECK(periodic, "locgen: cv=0 train is exactly periodic");

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
  test_trigger_boot();
  test_amp_jitter();
  test_ipi();
  test_locgen();
  if (g_fail == 0) { printf("OK\n"); return 0; }
  printf("%d CHECK(s) FAILED\n", g_fail);
  return 1;
}
