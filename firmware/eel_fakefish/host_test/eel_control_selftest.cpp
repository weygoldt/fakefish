// Host harness for the device-agnostic CONTROL logic (eel_control.h): compiles the
// pure decision code on a PC (ARDUINO undefined -> the GPIO/millis/random glue is
// skipped) and asserts it. Complements eel_player_selftest.cpp (the engine diff).
//
//   g++ -I firmware/eel_fakefish firmware/eel_fakefish/eel_player.cpp \
//       firmware/eel_fakefish/eel_stimuli.cpp \
//       firmware/eel_fakefish/host_test/eel_control_selftest.cpp -lm -o /tmp/control_selftest
//   /tmp/control_selftest        # prints "OK" and exits 0, or aborts on failure
#include <cstdio>
#include <cstdlib>
#include <cmath>
#include <vector>
#include "eel_control.h"
#include "eel_player.h"

static int failures = 0;
#define CHECK(cond) do { if (!(cond)) { \
  fprintf(stderr, "FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); failures++; } } while (0)

static void test_item_class_for_kind() {
  CHECK(item_class_for_kind(STIM_REAL_VOLLEY)  == ITEM_CLASS_VOLLEY);
  CHECK(item_class_for_kind(STIM_SYNTH_VOLLEY) == ITEM_CLASS_VOLLEY);
  CHECK(item_class_for_kind(STIM_LOCALIZATION) == ITEM_CLASS_LOCALIZATION);
  CHECK(item_class_for_kind(99)                == -1);   // unknown kind: no class
}

static void test_build_item_lists() {
  uint8_t vol[N_STIM_ITEMS], loc[N_STIM_ITEMS], n_vol = 0, n_loc = 0;
  build_item_lists(vol, &n_vol, loc, &n_loc);

  // Every listed index really is a member of that class; lists are disjoint.
  for (uint8_t i = 0; i < n_vol; i++)
    CHECK(item_class_for_kind(STIM_ITEMS[vol[i]].kind) == ITEM_CLASS_VOLLEY);
  for (uint8_t i = 0; i < n_loc; i++)
    CHECK(item_class_for_kind(STIM_ITEMS[loc[i]].kind) == ITEM_CLASS_LOCALIZATION);

  // Counts match a direct scan; every library item maps to a class.
  int exp_vol = 0, exp_loc = 0, exp_none = 0;
  for (uint8_t i = 0; i < N_STIM_ITEMS; i++) {
    int c = item_class_for_kind(STIM_ITEMS[i].kind);
    if (c == ITEM_CLASS_VOLLEY) exp_vol++;
    else if (c == ITEM_CLASS_LOCALIZATION) exp_loc++;
    else exp_none++;
  }
  CHECK(n_vol == exp_vol);
  CHECK(n_loc == exp_loc);
  CHECK(exp_none == 0);                                    // no non-class items
  CHECK((int)n_vol + (int)n_loc == N_STIM_ITEMS);
  CHECK(n_vol > 0 && n_loc > 0);   // both programs must have something to play
}

// ---- Session construction (one press -> a segment list) -------------------

// Pick a real localization and a real volley index out of the library to build with.
static void some_indices(int* loc_idx, int* vol_idx) {
  uint8_t vol[N_STIM_ITEMS], loc[N_STIM_ITEMS], n_vol = 0, n_loc = 0;
  build_item_lists(vol, &n_vol, loc, &n_loc);
  *loc_idx = n_loc ? (int)loc[0] : -1;
  *vol_idx = n_vol ? (int)vol[0] : -1;
}

// A: the bare 10 s calibration tone — one segment, no gap, no item.
static void test_session_calibration() {
  Segment segs[MAX_SESSION_SEGS];
  uint8_t n = build_session(PROG_CALIBRATION, -1, -1, segs);
  CHECK(n == 1);
  CHECK(segs[0].kind == SEG_TONE);
  CHECK(segs[0].n_samples == MARKER_CAL_SAMPLES);
  CHECK(segs[0].item_index == -1);
  // Calibration needs no item, so it builds even with an empty library.
  CHECK(build_session(PROG_CALIBRATION, -1, -1, segs) == 1);
}

// B: lead-in tone -> that item's fixed gap -> the localization, windowed + looped at the
// reduced amplitude.
static void test_session_localization() {
  int loc_idx, vol_idx;
  some_indices(&loc_idx, &vol_idx);
  Segment segs[MAX_SESSION_SEGS];
  uint8_t n = build_session(PROG_LOCALIZATION, loc_idx, -1, segs);
  CHECK(n == 3);
  CHECK(segs[0].kind == SEG_TONE && segs[0].n_samples == MARKER_LEADIN_SAMPLES);
  CHECK(segs[1].kind == SEG_SILENCE && segs[1].n_samples == STIM_LEAD_GAP_SAMP[loc_idx]);
  CHECK(segs[2].kind == SEG_ITEM);
  CHECK(segs[2].item_index == loc_idx);
  CHECK(segs[2].n_samples == LOC_PLAYBACK_SAMPLES);   // bounded to the playback window
  CHECK(segs[2].loop == 1);                           // and looped to fill it
  CHECK(item_amplitude_for_kind(STIM_ITEMS[segs[2].item_index].kind, 0.45f, 0.90f) == 0.45f);
}

// C: lead-in tone -> gap -> the volley, once, in full, at full scale.
static void test_session_volley() {
  int loc_idx, vol_idx;
  some_indices(&loc_idx, &vol_idx);
  Segment segs[MAX_SESSION_SEGS];
  uint8_t n = build_session(PROG_VOLLEY, -1, vol_idx, segs);
  CHECK(n == 3);
  CHECK(segs[0].kind == SEG_TONE && segs[0].n_samples == MARKER_LEADIN_SAMPLES);
  CHECK(segs[1].kind == SEG_SILENCE && segs[1].n_samples == STIM_LEAD_GAP_SAMP[vol_idx]);
  CHECK(segs[2].kind == SEG_ITEM);
  CHECK(segs[2].item_index == vol_idx);
  CHECK(segs[2].n_samples == 0);        // unbounded == play the item once, in full
  CHECK(segs[2].loop == 0);
  CHECK(item_amplitude_for_kind(STIM_ITEMS[segs[2].item_index].kind, 0.45f, 0.90f) == 0.90f);
}

// D: ONE lead-in -> gap -> a SHORT localization -> a brief silence -> the volley.
static void test_session_loc_volley() {
  int loc_idx, vol_idx;
  some_indices(&loc_idx, &vol_idx);
  Segment segs[MAX_SESSION_SEGS];
  uint8_t n = build_session(PROG_LOC_VOLLEY, loc_idx, vol_idx, segs);
  CHECK(n == 5);
  CHECK(n <= MAX_SESSION_SEGS);
  CHECK(segs[0].kind == SEG_TONE && segs[0].n_samples == MARKER_LEADIN_SAMPLES);
  CHECK(segs[1].kind == SEG_SILENCE && segs[1].n_samples == STIM_LEAD_GAP_SAMP[loc_idx]);
  CHECK(segs[2].kind == SEG_ITEM && segs[2].item_index == loc_idx);
  CHECK(segs[2].n_samples == D_LOC_PLAYBACK_SAMPLES);   // the SHORT lead, not B's window
  CHECK(segs[2].n_samples < LOC_PLAYBACK_SAMPLES);
  CHECK(segs[2].loop == 1);
  CHECK(item_amplitude_for_kind(STIM_ITEMS[segs[2].item_index].kind, 0.45f, 0.90f) == 0.45f);
  CHECK(segs[3].kind == SEG_SILENCE && segs[3].n_samples == D_INTERPHASE_GAP_SAMPLES);
  CHECK(segs[4].kind == SEG_ITEM && segs[4].item_index == vol_idx);
  CHECK(segs[4].n_samples == 0);
  CHECK(segs[4].loop == 0);
  CHECK(item_amplitude_for_kind(STIM_ITEMS[segs[4].item_index].kind, 0.45f, 0.90f) == 0.90f);
  // Exactly one tone anchors the whole sense->strike sequence (no second announcement).
  int tones = 0;
  for (uint8_t i = 0; i < n; i++) if (segs[i].kind == SEG_TONE) tones++;
  CHECK(tones == 1);
}

// A program whose class has no library item self-mutes rather than playing something wrong.
static void test_session_missing_item() {
  Segment segs[MAX_SESSION_SEGS];
  int loc_idx, vol_idx;
  some_indices(&loc_idx, &vol_idx);
  CHECK(build_session(PROG_LOCALIZATION, -1, -1, segs) == 0);
  CHECK(build_session(PROG_VOLLEY, -1, -1, segs) == 0);
  CHECK(build_session(PROG_LOC_VOLLEY, -1, vol_idx, segs) == 0);   // no localization
  CHECK(build_session(PROG_LOC_VOLLEY, loc_idx, -1, segs) == 0);   // no volley
}

// Duration alone distinguishes the calibration tone from a playback lead-in downstream, so
// the two lengths must stay distinct and every stimulus program must open on the lead-in.
static void test_marker_duration_invariant() {
  int loc_idx, vol_idx;
  some_indices(&loc_idx, &vol_idx);
  Segment segs[MAX_SESSION_SEGS];
  CHECK(MARKER_LEADIN_SAMPLES != MARKER_CAL_SAMPLES);

  build_session(PROG_CALIBRATION, -1, -1, segs);
  CHECK(segs[0].n_samples == MARKER_CAL_SAMPLES);

  const EelProgram stim[3] = { PROG_LOCALIZATION, PROG_VOLLEY, PROG_LOC_VOLLEY };
  for (int i = 0; i < 3; i++) {
    uint8_t n = build_session(stim[i], loc_idx, vol_idx, segs);
    CHECK(n > 0);
    CHECK(segs[0].kind == SEG_TONE);
    CHECK(segs[0].n_samples == MARKER_LEADIN_SAMPLES);   // never the calibration length
    CHECK(segs[0].n_samples != MARKER_CAL_SAMPLES);
  }
}

// Only CALIBRATION takes the quiet tone level (its electrodes sit against a recording
// electrode, where a full-scale tone clips the recorder); every lead-in stays loud so it
// carries at a distance.
static void test_marker_amplitude_for_program() {
  const float loud = 0.90f, quiet = 0.25f;
  CHECK(marker_amplitude_for_program(PROG_CALIBRATION,  loud, quiet) == quiet);
  CHECK(marker_amplitude_for_program(PROG_LOCALIZATION, loud, quiet) == loud);
  CHECK(marker_amplitude_for_program(PROG_VOLLEY,       loud, quiet) == loud);
  CHECK(marker_amplitude_for_program(PROG_LOC_VOLLEY,   loud, quiet) == loud);
}

// An item's level follows its own KIND, which is what makes ONE localization knob serve
// BOTH B (localization alone) and D's lead-in localization — they cannot drift apart.
static void test_item_amplitude_for_kind() {
  const float loc = 0.45f, vol = 0.90f;
  CHECK(item_amplitude_for_kind(STIM_LOCALIZATION,  loc, vol) == loc);
  CHECK(item_amplitude_for_kind(STIM_REAL_VOLLEY,   loc, vol) == vol);
  CHECK(item_amplitude_for_kind(STIM_SYNTH_VOLLEY,  loc, vol) == vol);   // both volley kinds

  // The load-bearing property: B's localization and D's localization resolve to the SAME
  // level, because both are STIM_LOCALIZATION items read through the same one knob.
  int loc_idx, vol_idx;
  some_indices(&loc_idx, &vol_idx);
  Segment b[MAX_SESSION_SEGS], d[MAX_SESSION_SEGS];
  build_session(PROG_LOCALIZATION, loc_idx, -1, b);
  build_session(PROG_LOC_VOLLEY, loc_idx, vol_idx, d);
  const float b_loc = item_amplitude_for_kind(STIM_ITEMS[b[2].item_index].kind, loc, vol);
  const float d_loc = item_amplitude_for_kind(STIM_ITEMS[d[2].item_index].kind, loc, vol);
  CHECK(b_loc == d_loc);
  CHECK(b_loc == loc);
  // ...and D's strike is still the loud one, so the contrast survives inside one session.
  CHECK(item_amplitude_for_kind(STIM_ITEMS[d[4].item_index].kind, loc, vol) == vol);
  CHECK(loc < vol);   // a "localization" must never read as a strike
}

// The quiet calibration level must still leave the sine plenty of PWM resolution: the
// driver's shape() maps a sample to an 8-bit duty code, and the whole point of the marker
// is a clean tone. At 0.25 the peak lands near code 61 of 255 (before noise shaping hands
// back ~3 more effective bits), which is far from the ~5-code regime that motivated the
// shaper in the first place.
static void test_marker_cal_level_keeps_resolution() {
  const float quiet = 0.25f;
  // Scan one whole cycle in the STEADY region (past the ramp, where the envelope is 1) for
  // the positive peak — sampling a fixed index would land inside the ramp and read ~0.
  int16_t peak = 0;
  for (uint32_t t = MARKER_RAMP_SAMPLES; t < MARKER_RAMP_SAMPLES + MARKER_SAMPLES_PER_CYCLE; t++) {
    int16_t v = marker_sample(t, MARKER_CAL_SAMPLES, MARKER_RAMP_SAMPLES, quiet);
    if (v > peak) peak = v;
  }
  const int duty = (peak + 64) >> 7;                 // shape(), sans the error feedback
  CHECK(peak > 0);
  CHECK(duty > 32);                                  // >5 bits of swing on the positive lobe
  CHECK(duty < 255);                                 // and nowhere near the rail
  // The tone is still zero-symmetric at the quiet level (no DC into the electrodes).
  long dc = 0;
  for (uint32_t t = MARKER_RAMP_SAMPLES; t < MARKER_RAMP_SAMPLES + 1000; t++)
    dc += marker_sample(t, MARKER_CAL_SAMPLES, MARKER_RAMP_SAMPLES, quiet);
  CHECK(std::labs(dc) < 100);
}

// ---- Indicator LED --------------------------------------------------------

// onset_fired: one shot per new onset. The sentinel init is what lets an item's FIRST
// pulse — which lands at last_onset == 0 — register at all.
static void test_onset_fired() {
  uint32_t prev = LED_ONSET_NONE;
  CHECK(onset_fired(0, &prev) == true);    // first pulse of an item (last_onset == 0)
  CHECK(onset_fired(0, &prev) == false);   // same onset held: no repeat
  CHECK(onset_fired(0, &prev) == false);
  CHECK(onset_fired(1500, &prev) == true); // next pulse
  CHECK(onset_fired(1500, &prev) == false);
  CHECK(onset_fired(3000, &prev) == true);
  // A looped restart keeps last_onset monotonic, so it still reads as a fresh onset.
  CHECK(onset_fired(9999, &prev) == true);
}

// led_blink_step: an onset arms the countdown, which then decrements and saturates at 0.
static void test_led_blink_step() {
  CHECK(led_blink_step(0, true, 300) == 300);    // onset arms
  CHECK(led_blink_step(300, false, 300) == 299); // then ticks down
  CHECK(led_blink_step(1, false, 300) == 0);
  CHECK(led_blink_step(0, false, 300) == 0);     // saturates (no underflow)
  CHECK(led_blink_step(50, true, 300) == 300);   // a retrigger RE-ARMS, never accumulates
}

static void test_led_on_for_segment() {
  CHECK(led_on_for_segment(SEG_TONE, 0) == true);      // solid for the whole tone
  CHECK(led_on_for_segment(SEG_TONE, 123) == true);
  CHECK(led_on_for_segment(SEG_SILENCE, 0) == false);  // dark in a gap
  CHECK(led_on_for_segment(SEG_SILENCE, 123) == false);// even with a stale countdown
  CHECK(led_on_for_segment(SEG_ITEM, 0) == false);     // between pulses
  CHECK(led_on_for_segment(SEG_ITEM, 1) == true);      // during a blink
}

// Drive the blink logic with realistic onset trains: a slow localization must produce
// DISCRETE countable flashes, a fast volley one CONTINUOUS glow (its IPI is shorter than
// the blink, so each onset re-arms before the countdown expires).
static void test_led_blink_rates() {
  // Localization at ~7 Hz: IPI ~7143 samples >> LED_BLINK_SAMPLES -> it must go dark between.
  const uint32_t loc_ipi = 7143;
  uint32_t remaining = 0;
  int loc_edges = 0;
  bool was_on = false;
  for (uint32_t t = 0; t < loc_ipi * 4; t++) {
    bool onset = (t % loc_ipi) == 0;
    remaining = led_blink_step(remaining, onset, LED_BLINK_SAMPLES);
    bool on = led_on_for_segment(SEG_ITEM, remaining);
    if (on && !was_on) loc_edges++;
    was_on = on;
  }
  CHECK(loc_edges == 4);          // one discrete flash per pulse
  CHECK(LED_BLINK_SAMPLES < loc_ipi);

  // Volley at ~350 Hz: IPI ~143 samples < LED_BLINK_SAMPLES -> continuously lit after onset.
  const uint32_t vol_ipi = 143;
  remaining = 0;
  was_on = false;
  int vol_edges = 0;
  int off_ticks = 0;
  for (uint32_t t = 0; t < vol_ipi * 10; t++) {
    bool onset = (t % vol_ipi) == 0;
    remaining = led_blink_step(remaining, onset, LED_BLINK_SAMPLES);
    bool on = led_on_for_segment(SEG_ITEM, remaining);
    if (on && !was_on) vol_edges++;
    if (!on) off_ticks++;
    was_on = on;
  }
  CHECK(vol_edges == 1);          // lights once and stays lit — a merged glow
  CHECK(off_ticks == 0);          // never goes dark mid-volley
  CHECK(LED_BLINK_SAMPLES > vol_ipi);
}

// ---- 10 kHz sine marker (the anchor tone) --------------------------------

// The lookup table is exactly one unit-sine cycle at MARKER_FREQ_HZ / 50 kHz and sums to
// zero (odd symmetry -> zero net charge / no DC over the long calibration tone).
static void test_marker_lut() {
  CHECK(MARKER_SAMPLES_PER_CYCLE == 5);   // 10 kHz at 50 kHz
  CHECK(STIM_SAMPLE_RATE_HZ % MARKER_FREQ_HZ == 0);   // exact -> integer samples/cycle
  long sum = 0;
  for (uint32_t i = 0; i < MARKER_SAMPLES_PER_CYCLE; i++) {
    long expect = lround(32767.0 * sin(2.0 * M_PI * (double)i / MARKER_SAMPLES_PER_CYCLE));
    CHECK(MARKER_LUT[i] == expect);   // table == round(32767*sin), element for element
    sum += MARKER_LUT[i];
  }
  CHECK(sum == 0);                    // zero-DC per cycle
  CHECK(MARKER_LUT[0] == 0);          // zero-start
}

// The lead-in (1 s) and calibration (10 s) tones are integer numbers of cycles, so each
// ends exactly at phase 0 (no offset click, clean drift-frequency fit).
static void test_marker_integer_cycles() {
  CHECK(MARKER_LEADIN_SAMPLES % MARKER_SAMPLES_PER_CYCLE == 0);
  CHECK(MARKER_CAL_SAMPLES % MARKER_SAMPLES_PER_CYCLE == 0);
  CHECK(MARKER_LEADIN_SAMPLES == 50000u);
  CHECK(MARKER_CAL_SAMPLES == 500000u);
}

// marker_sample: zero-start, steady-state == amplitude*LUT (envelope 1), and the onset
// ramp rises monotonically from 0 with no discontinuity.
static void test_marker_sample() {
  const uint32_t total = MARKER_LEADIN_SAMPLES;
  const uint32_t ramp = MARKER_RAMP_SAMPLES;
  const float amp = 0.90f;

  CHECK(marker_sample(0, total, ramp, amp) == 0);        // zero-start (LUT[0]==0 & env 0)
  CHECK(marker_envelope(0, total, ramp) == 0.0f);

  // steady region: envelope is exactly 1, so the sample is the scaled LUT value.
  for (uint32_t t = ramp; t < ramp + MARKER_SAMPLES_PER_CYCLE; t++) {
    CHECK(marker_envelope(t, total, ramp) == 1.0f);
    long expect = lround(amp * (float)MARKER_LUT[t % MARKER_SAMPLES_PER_CYCLE]);
    CHECK(marker_sample(t, total, ramp, amp) == (int16_t)expect);
  }

  // onset ramp is monotonic non-decreasing 0 -> 1 over [0, ramp].
  float prev = -1.0f;
  for (uint32_t t = 0; t <= ramp; t++) {
    float e = marker_envelope(t, total, ramp);
    CHECK(e >= prev - 1e-6f);
    CHECK(e >= 0.0f && e <= 1.0f);
    prev = e;
  }
  // offset ramps back down to ~0 at the final sample (click-free tail).
  CHECK(marker_envelope(total - 1, total, ramp) < 0.05f);
}

// Goertzel magnitude^2 of a real signal at frequency f (Hz), sample rate fs.
static double goertzel_power(const std::vector<int16_t>& x, double f, double fs) {
  double w = 2.0 * M_PI * f / fs;
  double coeff = 2.0 * cos(w);
  double s1 = 0.0, s2 = 0.0;
  for (int16_t v : x) {
    double s = (double)v + coeff * s1 - s2;
    s2 = s1;
    s1 = s;
  }
  return s2 * s2 + s1 * s1 - coeff * s1 * s2;
}

// Spectral purity: the generated marker is a single line at MARKER_FREQ_HZ — its power
// dwarfs power at off-band frequencies (a pure tone, not a broadband transient). Keyed off
// MARKER_FREQ_HZ so it stays honest if the marker frequency ever moves again.
static void test_marker_spectral_purity() {
  const uint32_t total = MARKER_LEADIN_SAMPLES;   // 1 s of exact cycles
  const float amp = 0.90f;   // full scale, to stress purity (independent of the driver knob)
  const double fs = STIM_SAMPLE_RATE_HZ;
  const double fline = (double)MARKER_FREQ_HZ;
  std::vector<int16_t> tone;
  tone.reserve(total);
  for (uint32_t t = 0; t < total; t++)
    tone.push_back(marker_sample(t, total, MARKER_RAMP_SAMPLES, amp));

  double pline = goertzel_power(tone, fline, fs);
  // Off-line probes: below the line, above it (still < Nyquist), and deep in the EOD band.
  double p_lo   = goertzel_power(tone, fline * 0.6, fs);
  double p_hi   = goertzel_power(tone, fline * 1.4, fs);
  double p_band = goertzel_power(tone, 60.0, fs);   // low-band (EOD region)
  CHECK(pline > 1e4 * p_lo);
  CHECK(pline > 1e4 * p_hi);
  CHECK(pline > 1e4 * p_band);

  // near-zero net DC over the whole tone (odd-symmetric LUT + symmetric ramps).
  long dc = 0;
  for (int16_t v : tone) dc += v;
  CHECK(std::labs(dc) < 100);   // << the ~28000 peak amplitude
}

// Feed a bouncy press/release into the debouncer and assert exactly one fire per
// debounced press, at the falling (press) edge, no repeats while held, none on
// release. raw_high == true means released (pulled up); false means pressed.
static void test_debounce() {
  DebounceState d;
  debounce_init(&d);
  const uint32_t DB = BTN_DEBOUNCE_MS;
  int fires = 0;

  // idle released for a while: no fire
  for (uint32_t t = 0; t < 50; t += 5) fires += debounce_fell(&d, true, t, DB);
  CHECK(fires == 0);

  // press with contact bounce (< DB apart) then settle LOW
  debounce_fell(&d, false, 100, DB);   // first dip
  debounce_fell(&d, true,  105, DB);   // bounce back
  debounce_fell(&d, false, 110, DB);   // dip again, now settles
  int early = debounce_fell(&d, false, 110 + DB - 1, DB);  // still inside window
  CHECK(early == 0);                                       // must NOT fire early
  int fire = debounce_fell(&d, false, 110 + DB + 1, DB);   // stable past window
  CHECK(fire == 1);                                        // fires exactly here

  // held down: no repeat fire
  int held = 0;
  for (uint32_t t = 200; t < 400; t += 10) held += debounce_fell(&d, false, t, DB);
  CHECK(held == 0);

  // release (bounce then settle HIGH): no fire on release
  int rel = 0;
  rel += debounce_fell(&d, true,  500, DB);
  rel += debounce_fell(&d, false, 505, DB);
  rel += debounce_fell(&d, true,  510, DB);
  for (uint32_t t = 510; t < 600; t += 10) rel += debounce_fell(&d, true, t, DB);
  CHECK(rel == 0);

  // a second clean press fires again
  debounce_fell(&d, false, 700, DB);
  int fire2 = debounce_fell(&d, false, 700 + DB + 1, DB);
  CHECK(fire2 == 1);
}

// Four independent buttons: each debounces on its own state, and pressing one never
// fires another (the driver ticks all four every loop).
static void test_debounce_independent_buttons() {
  DebounceState btn[N_PROGRAMS];
  for (int i = 0; i < N_PROGRAMS; i++) debounce_init(&btn[i]);
  const uint32_t DB = BTN_DEBOUNCE_MS;

  // Press button C (index 2) only; tick every button as the driver does.
  int fires[N_PROGRAMS] = {0, 0, 0, 0};
  for (uint32_t t = 0; t <= 2 * DB; t += 5)
    for (int i = 0; i < N_PROGRAMS; i++)
      fires[i] += debounce_fell(&btn[i], /*raw_high=*/(i != 2), t, DB);

  CHECK(fires[2] == 1);   // only C fired...
  CHECK(fires[0] == 0);
  CHECK(fires[1] == 0);
  CHECK(fires[3] == 0);   // ...and it fired exactly once
}

// The localization playback window: a bounded max-sample count truncates a long
// train, and the loop flag replays a short train to fill the window.
static void test_playback_window() {
  const uint16_t N = 5;
  uint16_t ipi[N] = {0, 1000, 1000, 1000, 1000};  // 5 pulses, 1000-sample IPI
  StimItem it;
  it.ipi_samp = ipi; it.rel_amp = NULL; it.n = N;
  it.kind = STIM_LOCALIZATION; it.group = 0;
  const uint32_t once = (uint32_t)(N - 1) * 1000 + EOD_HV_LEN + 1;  // full-play length

  EelPlayer p; int16_t s; uint32_t n;

  // no window -> plays once, full length
  eel_player_start_windowed(&p, &it, 1.0f, 1, 0, 0);
  n = 0; while (eel_player_next(&p, &s)) n++;
  CHECK(n == once);

  // window SHORTER than the train -> truncates at exactly max_samples
  eel_player_start_windowed(&p, &it, 1.0f, 1, 2500, 1);
  n = 0; while (eel_player_next(&p, &s)) n++;
  CHECK(n == 2500);

  // window LONGER than the train + loop -> loops to fill exactly max_samples
  eel_player_start_windowed(&p, &it, 1.0f, 1, 10000, 1);
  n = 0; while (eel_player_next(&p, &s)) n++;
  CHECK(n == 10000);
  CHECK(once < 10000);   // one pass is shorter -> it must have looped

  // window longer than the train WITHOUT loop -> plays once and stops early
  eel_player_start_windowed(&p, &it, 1.0f, 1, 10000, 0);
  n = 0; while (eel_player_next(&p, &s)) n++;
  CHECK(n == once);
}

// End-to-end over the ENGINE: an item segment's onsets, read through last_onset exactly as
// the ISR does, blink once per pulse — and never twice, which an |sample|-threshold
// detector would do at the biphasic EOD's internal zero crossing.
static void test_led_tracks_engine_onsets() {
  const uint16_t N = 6;
  uint16_t ipi[N] = {0, 8000, 8000, 8000, 8000, 8000};  // 6 pulses, well-separated
  StimItem it;
  it.ipi_samp = ipi; it.rel_amp = NULL; it.n = N;
  it.kind = STIM_LOCALIZATION; it.group = 0;

  EelPlayer p;
  eel_player_start_windowed(&p, &it, 1.0f, 1, 0, 0);
  uint32_t prev_onset = LED_ONSET_NONE, remaining = 0;
  int onsets = 0, rising = 0;
  bool was_on = false;
  int16_t s;
  while (eel_player_next(&p, &s)) {
    bool onset = onset_fired(p.last_onset, &prev_onset);
    if (onset) onsets++;
    remaining = led_blink_step(remaining, onset, LED_BLINK_SAMPLES);
    bool on = led_on_for_segment(SEG_ITEM, remaining);
    if (on && !was_on) rising++;
    was_on = on;
  }
  CHECK(onsets == N);   // exactly one onset per pulse, including the first at t == 0
  CHECK(rising == N);   // and exactly one LED flash per pulse — no biphasic double-blink
}

// Walk a WHOLE session exactly as onSampleTick() does — tone, silence, item, silence,
// item — and assert the composite Button D really is one 1 s tone, a gap, a SHORT
// localization, a brief silence, then the volley, with the LED solid / dark / blinking in
// the right places. The segment-list test above can only show the PLAN; this shows that
// walking it emits the right audio, that each window is honoured, and above all that the
// loc->volley SEAM genuinely re-arms the engine and plays the volley (two items cannot
// share one EelPlayer, so that re-arm is the one new thing D does).
static void test_session_walk_loc_volley() {
  int loc_idx, vol_idx;
  some_indices(&loc_idx, &vol_idx);
  Segment segs[MAX_SESSION_SEGS];
  const uint8_t n = build_session(PROG_LOC_VOLLEY, loc_idx, vol_idx, segs);
  CHECK(n == 5);

  EelPlayer p;
  uint32_t prev_onset = LED_ONSET_NONE, remaining = 0;
  uint32_t emitted[MAX_SESSION_SEGS] = {0, 0, 0, 0, 0};
  uint32_t nonzero[MAX_SESSION_SEGS] = {0, 0, 0, 0, 0};
  uint32_t led_on[MAX_SESSION_SEGS]  = {0, 0, 0, 0, 0};

  for (uint8_t i = 0; i < n; i++) {
    if (segs[i].kind == SEG_ITEM) {          // entering an item arms the engine (as the ISR does)
      eel_player_start_windowed(&p, &STIM_ITEMS[segs[i].item_index],
                                item_amplitude_for_kind(STIM_ITEMS[segs[i].item_index].kind,
                                                        0.45f, 0.90f),
                                1, segs[i].n_samples, segs[i].loop);
      prev_onset = LED_ONSET_NONE;
      remaining = 0;
    }
    for (;;) {
      int16_t s;
      bool led;
      if (segs[i].kind == SEG_TONE) {
        if (emitted[i] >= segs[i].n_samples) break;
        s = marker_sample(emitted[i], segs[i].n_samples, MARKER_RAMP_SAMPLES, 0.90f);
        led = led_on_for_segment(SEG_TONE, 0);
      } else if (segs[i].kind == SEG_SILENCE) {
        if (emitted[i] >= segs[i].n_samples) break;
        s = 0;
        led = led_on_for_segment(SEG_SILENCE, 0);
      } else {
        if (!eel_player_next(&p, &s)) break;
        bool onset = onset_fired(p.last_onset, &prev_onset);
        remaining = led_blink_step(remaining, onset, LED_BLINK_SAMPLES);
        led = led_on_for_segment(SEG_ITEM, remaining);
      }
      emitted[i]++;
      if (s != 0) nonzero[i]++;
      if (led) led_on[i]++;
    }
  }

  // 0: the 1 s lead-in tone — full length, really a tone, LED solid throughout.
  CHECK(emitted[0] == MARKER_LEADIN_SAMPLES);
  CHECK(nonzero[0] > MARKER_LEADIN_SAMPLES / 2);   // a real tone, not silence
  CHECK(led_on[0] == MARKER_LEADIN_SAMPLES);       // solid for the WHOLE tone (no 10 kHz flicker)

  // 1: the item's fixed lead-in gap — exactly silent, LED dark.
  CHECK(emitted[1] == STIM_LEAD_GAP_SAMP[loc_idx]);
  CHECK(nonzero[1] == 0);
  CHECK(led_on[1] == 0);

  // 2: the SHORT localization — stops exactly at D's window, LED blinks discretely.
  CHECK(emitted[2] == D_LOC_PLAYBACK_SAMPLES);
  CHECK(nonzero[2] > 0);
  CHECK(led_on[2] > 0);
  CHECK(led_on[2] < emitted[2]);                   // discrete flashes, not a solid glow

  // 3: the inter-phase silence — exactly silent, LED dark.
  CHECK(emitted[3] == D_INTERPHASE_GAP_SAMPLES);
  CHECK(nonzero[3] == 0);
  CHECK(led_on[3] == 0);

  // 4: the volley — proof the seam re-armed the engine and it actually played.
  CHECK(emitted[4] > 0);
  CHECK(nonzero[4] > 0);
  CHECK(led_on[4] > 0);
}

int main() {
  test_item_class_for_kind();
  test_build_item_lists();
  test_session_calibration();
  test_session_localization();
  test_session_volley();
  test_session_loc_volley();
  test_session_missing_item();
  test_session_walk_loc_volley();
  test_marker_duration_invariant();
  test_marker_amplitude_for_program();
  test_item_amplitude_for_kind();
  test_marker_cal_level_keeps_resolution();
  test_onset_fired();
  test_led_blink_step();
  test_led_on_for_segment();
  test_led_blink_rates();
  test_marker_lut();
  test_marker_integer_cycles();
  test_marker_sample();
  test_marker_spectral_purity();
  test_debounce();
  test_debounce_independent_buttons();
  test_playback_window();
  test_led_tracks_engine_onsets();
  if (failures) { fprintf(stderr, "%d CHECK(s) failed\n", failures); return 1; }
  printf("OK\n");
  return 0;
}
