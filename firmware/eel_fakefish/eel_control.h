// Playback CONTROL for the eel stimulus library: the operator interface.
//
// The fakefish is a HAND-HELD field stimulator — the operator holds it and only the
// electrodes go in the water. This file is what the operator touches: FOUR dedicated
// program buttons and an indicator LED, plus the pure logic that turns one press into a
// SESSION — an ordered list of segments (tone / silence / item) that the driver's 50 kHz
// sample-clock ISR walks. It knows nothing about PWM or the clock itself; the .ino wires
// it to the engine and out_write().
//
// Header-only (static inline + file-static storage): the sketch is one translation unit,
// so there is no ODR issue.
//
// The pure decision logic (debounce edge-detect, kind->class partition, session
// construction, marker generation, LED state) sits ABOVE the `#ifdef ARDUINO` line so it
// compiles and is unit-tested on a PC (firmware/host_test/eel_control_selftest). Only the
// GPIO / millis() / random() glue is Arduino-specific.
#pragma once
#include <stdint.h>
#include <math.h>          // cosf / lrintf for the sine-marker envelope
#include "eel_stimuli.h"   // StimItem, STIM_ITEMS, StimKind, STIM_SAMPLE_RATE_HZ, STIM_LEAD_GAP_SAMP

// ===== Wiring (edit these to your rig) =====================================
// FOUR program buttons, each: one leg to its pin (INPUT_PULLUP), the other to GND ->
// active-low. One press plays that program ONCE, start to finish. A press while a
// playback is running is IGNORED — playbacks are uninterruptible (see EelProgram).
//
// The LED is the operator's only feedback: SOLID through the 10 kHz marker tone, one blink
// per EOD pulse through the stimulus, dark otherwise. Pin 13 is the Teensy 4.1's built-in
// LED (and SPI SCK — unused here), so an external LED on pin 13 mirrors the on-board one.
// The LED is dry hand-held operator UI: it never enters the water and cannot cue the fish.
//
// These pins avoid the PWM output pins (2, 3) and A0 (== digital pin 14, randomSeed).
// None of 5-8 is tied to bootloader entry (the 4.1 uses a dedicated PROGRAM button).
#define BTN_A_PIN  5    // A: calibration            (10 s 10 kHz sine)
#define BTN_B_PIN  6    // B: localization           (marker -> gap -> localization train)
#define BTN_C_PIN  7    // C: volley                 (marker -> gap -> volley)
#define BTN_D_PIN  8    // D: localization -> volley (one marker, one session)
#define LED_PIN    13   // built-in + external indicator LED

#define BTN_DEBOUNCE_MS  25u   // press must be stable this long to register

// ===== The four programs ===================================================
// One button each, all one-shot: press once, the whole program plays, then the device
// returns to idle. There is no stop button and no mode latch — the button IS the program.
// The enum value is also the button index into BTN_PINS[].
typedef enum {
  PROG_CALIBRATION  = 0,   // A
  PROG_LOCALIZATION = 1,   // B
  PROG_VOLLEY       = 2,   // C
  PROG_LOC_VOLLEY   = 3,   // D
} EelProgram;
#define N_PROGRAMS  4
#define PROG_NONE   (-1)

// ===== Out-of-band 10 kHz sine MARKER (the anchor) =========================
// A pure ~10 kHz sine is the playback ANCHOR: it locates each playback in the recording
// (and its apparent frequency pins the recorder-vs-playback clock drift). It replaced the
// old 50 Hz eel-EOD calibration train — a metronomic "conspecific" waveform would perturb
// the animal, whereas 10 kHz sits well above the eel's electrosensory band (and off the
// whole local biological band, so a narrowband detector flags it with ~zero false
// positives). It also clears the ~5 kHz ceiling of otophysan hearing by an octave — the
// most conservative choice on both the electrosensory and acoustic channels. NOTE that
// out-of-band is an ASSUMPTION, not a measured fact — see firmware/README.md.
//
//   - CALIBRATION      = a MARKER_CAL_S sine. No item follows.
//   - every other program = a MARKER_LEADIN_S sine, then the item's per-item lead-in gap
//     (STIM_LEAD_GAP_SAMP), then the stimulus. Duration alone (10 s vs 1 s) tells the two
//     apart downstream, so keeping CAL and LEADIN distinct is load-bearing.
//
// 10 kHz = exactly 5 output samples/cycle at 50 kHz, so one signed-int16 unit-sine cycle
// is an EXACT lookup table (MARKER_LUT); |peak| is 31163, not 32767, because a 5-sample
// sine never lands on the 90-deg crest (its samples fall at 0/72/144/216/288 deg). The
// table SUMS to 0 (odd symmetry) -> zero net charge per cycle -> no DC / no electrolysis
// over the long cal. Emit is LUT[t % 5] * amplitude * a short raised-cosine on/off ramp
// (anti-click AND spectral purity: a hard-gated tone splatters energy down into the EOD
// band). The tone is generated OUTSIDE the overlap-add pulse engine (which stays
// byte-identical); the driver just feeds these samples to the same out_write().
//
// 10 kHz passes the 16.4 kHz differential RC at ~-1.4 dB (still well inside the corner)
// and is captured cleanly by the 48 kHz recorder (< the 24 kHz Nyquist). To move the
// frequency, keep 50000 / MARKER_FREQ_HZ an INTEGER (an exact LUT) that also divides both
// MARKER_LEADIN_SAMPLES and MARKER_CAL_SAMPLES: 5/10 kHz work, 6/8 kHz do NOT (fractional
// samples/cycle spreads quantisation harmonics into the band). See firmware/README.md.
#define MARKER_FREQ_HZ            10000u
#define MARKER_SAMPLES_PER_CYCLE  (STIM_SAMPLE_RATE_HZ / MARKER_FREQ_HZ)   // == 5
#define MARKER_LEADIN_S           1u
#define MARKER_CAL_S              10u
#define MARKER_LEADIN_SAMPLES     ((uint32_t)MARKER_LEADIN_S * STIM_SAMPLE_RATE_HZ)  // 50000
#define MARKER_CAL_SAMPLES        ((uint32_t)MARKER_CAL_S * STIM_SAMPLE_RATE_HZ)     // 500000
#define MARKER_RAMP_SAMPLES       100u   // ~2 ms raised-cosine on/off ramp (click + splatter guard)

// One exact cycle of a unit sine at 10 kHz / 50 kHz: round(32767 * sin(2*pi*i/5)).
// Sums to 0. Scaled by the (firmware) marker amplitude and the ramp envelope at emit.
static const int16_t MARKER_LUT[MARKER_SAMPLES_PER_CYCLE] = {
  0, 31163, 19260, -19260, -31163
};
static const float MARKER_PI = 3.14159265358979323846f;
static_assert(MARKER_SAMPLES_PER_CYCLE == 5,
              "MARKER_LUT is a 5-entry table; it assumes 10 kHz at a 50 kHz sample rate");

// Raised-cosine on/off amplitude envelope for a marker of `total` samples with a
// `ramp`-sample rise and fall: 0 at t==0, ramps to 1 over [0,ramp), holds 1, ramps back
// to ~0 over the last `ramp` samples. Steady state returns 1 with no transcendental (so
// the ISR only pays cosf during the ~2 ms ramps). Pure — host-tested.
static inline float marker_envelope(uint32_t t, uint32_t total, uint32_t ramp) {
  if (ramp == 0 || total < 2u * ramp) return 1.0f;  // no room to ramp -> flat
  if (t < ramp)
    return 0.5f * (1.0f - cosf(MARKER_PI * (float)t / (float)ramp));
  if (t >= total - ramp) {
    uint32_t u = total - t;   // ramp (at the seam) .. 1 (last sample)
    return 0.5f * (1.0f - cosf(MARKER_PI * (float)u / (float)ramp));
  }
  return 1.0f;
}

// One output sample of the sine marker at output-sample index `t` of a `total`-sample
// tone, at global `amplitude` (0..1), with a `ramp`-sample raised-cosine on/off ramp.
// = LUT[t % 5] * amplitude * envelope, clamped to +/-32767. Zero-start (LUT[0]==0).
// Pure — host-tested; the driver feeds the result to out_write().
static inline int16_t marker_sample(uint32_t t, uint32_t total, uint32_t ramp,
                                    float amplitude) {
  float v = amplitude * marker_envelope(t, total, ramp)
            * (float)MARKER_LUT[t % MARKER_SAMPLES_PER_CYCLE];
  long r = lrintf(v);
  if (r > 32767) r = 32767;
  if (r < -32767) r = -32767;
  return (int16_t)r;
}

// ===== Localization playback window ========================================
// Localization trains are stored FULL length (~60 s) in the library, but sitting through
// all of it is tedious. LOC_PLAYBACK_S is how many seconds one press of B actually plays:
//   - <= the stored train length: only the first LOC_PLAYBACK_S seconds play;
//   - >  the stored train length: the train LOOPS (seam keeps the cadence) to fill it.
// Volley and calibration are unaffected — they always play their item once in full.
#define LOC_PLAYBACK_S        20u
#define LOC_PLAYBACK_SAMPLES  ((uint32_t)LOC_PLAYBACK_S * STIM_SAMPLE_RATE_HZ)

// ===== Program D: localization -> volley ===================================
// D is the sense-then-strike motif: an eel localizes, then strikes. It plays a SHORT
// localization lead (not B's full LOC_PLAYBACK_S — a D trial should stay snappy so you
// get more probes per session), a brief silence, then the volley. Both halves ride under
// ONE marker and share ONE polarity: a real fish does not re-announce itself, nor flip its
// dipole, between sensing and striking.
//
// The inter-phase gap is short and DETERMINISTIC on purpose: a hunting eel accelerates
// into the strike rather than going quiet, and a firmware constant (rather than something
// baked into the library) lets downstream reconstruct the loc->volley lattice without
// regenerating eel_stimuli. Honest caveat: D steps amplitude 0.5x -> 1.0x and inserts a
// silence, where a real eel ramps rate and amplitude continuously — it is a sequence-level
// caricature of a hunt, not a replay of one.
#define D_LOC_PLAYBACK_S          5u
#define D_LOC_PLAYBACK_SAMPLES    ((uint32_t)D_LOC_PLAYBACK_S * STIM_SAMPLE_RATE_HZ)
#define D_INTERPHASE_GAP_MS       300u
#define D_INTERPHASE_GAP_SAMPLES  ((uint32_t)D_INTERPHASE_GAP_MS * STIM_SAMPLE_RATE_HZ / 1000u)

// ===== Indicator LED =======================================================
// SOLID through the marker tone; one blink per EOD pulse through a stimulus; dark in gaps
// and at rest. The blink is a RETRIGGERABLE one-shot: every pulse onset re-arms a
// LED_BLINK_SAMPLES countdown. Localization IPIs (100 ms - 1 s) far exceed the blink, so
// those pulses read as discrete, countable flashes; a volley's 2.5-3.3 ms IPIs re-arm the
// countdown before it expires, so a volley reads as one continuous glow. That merging is
// physical, not a bug — and the pin still toggles per pulse for a scope.
#define LED_BLINK_MS       6u
#define LED_BLINK_SAMPLES  ((uint32_t)LED_BLINK_MS * STIM_SAMPLE_RATE_HZ / 1000u)   // 300

// ===== Pure decision logic (no Arduino; host-tested) =======================

// Which class of library item a StimKind belongs to, or -1 for a kind that is neither.
// The volley class covers both volley-family kinds (real + synthetic); every library kind
// currently maps to a class, and the -1 default is a defensive fall-through.
typedef enum {
  ITEM_CLASS_VOLLEY       = 0,
  ITEM_CLASS_LOCALIZATION = 1,
} ItemClass;

static inline int item_class_for_kind(uint8_t kind) {
  if (kind == STIM_REAL_VOLLEY || kind == STIM_SYNTH_VOLLEY)
    return ITEM_CLASS_VOLLEY;
  if (kind == STIM_LOCALIZATION)
    return ITEM_CLASS_LOCALIZATION;
  return -1;  // unknown kind: not playable
}

// Partition STIM_ITEMS into the per-class index lists (call once at startup).
// `vol` / `loc` must each hold up to N_STIM_ITEMS bytes.
static inline void build_item_lists(uint8_t* vol, uint8_t* n_vol,
                                    uint8_t* loc, uint8_t* n_loc) {
  *n_vol = 0;
  *n_loc = 0;
  for (uint8_t i = 0; i < N_STIM_ITEMS; i++) {
    int c = item_class_for_kind(STIM_ITEMS[i].kind);
    if (c == ITEM_CLASS_VOLLEY)            vol[(*n_vol)++] = i;
    else if (c == ITEM_CLASS_LOCALIZATION) loc[(*n_loc)++] = i;
  }
}

// ===== Sessions = a list of segments =======================================
// One press builds a SESSION: an ordered list of segments the sample-clock ISR walks,
// emitting exactly one sample per tick. Three kinds cover every program:
//   SEG_TONE     the 10 kHz sine marker (driver-generated, marker_sample())
//   SEG_SILENCE  a silent gap           (driver-generated zeros)
//   SEG_ITEM     a library item         (played by the overlap-add engine)
// Only SEG_ITEM touches eel_player, so the engine stays byte-identical and the marker and
// gaps stay DRIVER-level. Expressing each program as DATA is what lets D chain
// loc -> gap -> volley without a single new ISR state.
typedef enum { SEG_TONE = 0, SEG_SILENCE = 1, SEG_ITEM = 2 } SegKind;

typedef struct {
  uint8_t  kind;        // SegKind
  uint32_t n_samples;   // TONE/SILENCE: samples to emit.
                        // ITEM: playback window (0 == play the item once, unbounded).
  int8_t   item_index;  // ITEM: index into STIM_ITEMS. Otherwise -1.
  uint8_t  loop;        // ITEM: 1 == loop the item to fill n_samples.
} Segment;
// An item's LEVEL is not stored here: it follows from the item's own kind via
// item_amplitude_for_kind(), so a localization is guaranteed to play at the localization
// level in every program that uses one. The tone's level is likewise a session property
// (marker_amplitude_for_program) — every session has exactly one tone.

#define MAX_SESSION_SEGS  5   // D is the longest: TONE, SILENCE, ITEM, SILENCE, ITEM

// Build the segment list for a program. `loc_idx` / `vol_idx` are the library items the
// caller drew (-1 when not available / not needed); the caller injects them, which keeps
// this pure and host-testable. Returns the segment count, or 0 if the program cannot be
// played (its item list is empty) — the driver then simply self-mutes.
//
// Every stimulus program opens with the SAME MARKER_LEADIN_SAMPLES tone; only CALIBRATION
// uses MARKER_CAL_SAMPLES. Duration alone distinguishes the two downstream, so that split
// is an invariant, not a detail.
static inline uint8_t build_session(EelProgram prog, int loc_idx, int vol_idx,
                                    Segment* segs) {
  uint8_t n = 0;

  // Calibration is the bare tone: no gap, no item.
  if (prog == PROG_CALIBRATION) {
    segs[n].kind = SEG_TONE;   segs[n].n_samples = MARKER_CAL_SAMPLES;
    segs[n].item_index = -1;   segs[n].loop = 0;
    n++;
    return n;
  }

  const int need_loc = (prog == PROG_LOCALIZATION || prog == PROG_LOC_VOLLEY);
  const int need_vol = (prog == PROG_VOLLEY       || prog == PROG_LOC_VOLLEY);
  if (need_loc && loc_idx < 0) return 0;   // nothing of that class in the library
  if (need_vol && vol_idx < 0) return 0;

  // The lead-in tone, then the FIRST item's fixed per-item gap (so the marker's recorded
  // offset plus that known gap locates the first pulse downstream).
  const int first_idx = need_loc ? loc_idx : vol_idx;
  segs[n].kind = SEG_TONE;      segs[n].n_samples = MARKER_LEADIN_SAMPLES;
  segs[n].item_index = -1;      segs[n].loop = 0;
  n++;
  segs[n].kind = SEG_SILENCE;   segs[n].n_samples = STIM_LEAD_GAP_SAMP[first_idx];
  segs[n].item_index = -1;      segs[n].loop = 0;
  n++;

  if (need_loc) {
    // B plays the full LOC_PLAYBACK window; D's localization is only a short
    // sense-before-strike lead, so a D trial stays short.
    segs[n].kind = SEG_ITEM;
    segs[n].n_samples = (prog == PROG_LOC_VOLLEY) ? D_LOC_PLAYBACK_SAMPLES
                                                  : LOC_PLAYBACK_SAMPLES;
    segs[n].item_index = (int8_t)loc_idx;
    segs[n].loop = 1;   // loop a short stored train to fill the window
    n++;
  }
  if (prog == PROG_LOC_VOLLEY) {
    segs[n].kind = SEG_SILENCE;  segs[n].n_samples = D_INTERPHASE_GAP_SAMPLES;
    segs[n].item_index = -1;     segs[n].loop = 0;
    n++;
  }
  if (need_vol) {
    segs[n].kind = SEG_ITEM;     segs[n].n_samples = 0;   // play once, in full
    segs[n].item_index = (int8_t)vol_idx;
    segs[n].loop = 0;
    n++;
  }
  return n;
}

// ===== LED state (pure) ====================================================

// True exactly once per new pulse onset. Driven by the engine's `last_onset` (set to the
// pulse's output-sample index on every pulse-add, and monotonic across a looped restart),
// NOT by the output sample: the EOD waveform is biphasic — it crosses zero between its
// lobes — so any |sample|-threshold detector would fire TWICE per pulse. Initialise *prev
// to LED_ONSET_NONE so an item's first pulse, which lands at last_onset == 0, still
// registers.
#define LED_ONSET_NONE  0xFFFFFFFFu
static inline bool onset_fired(uint32_t last_onset, uint32_t* prev) {
  if (last_onset == *prev) return false;
  *prev = last_onset;
  return true;
}

// One tick of the blink countdown: a new onset re-arms it to `blink_samples`; otherwise it
// decrements to (and saturates at) 0.
static inline uint32_t led_blink_step(uint32_t remaining, bool onset,
                                      uint32_t blink_samples) {
  if (onset) return blink_samples;
  return remaining ? remaining - 1u : 0u;
}

// The marker tone level for a program, given the driver's two absolute levels (which live
// in the .ino, like every other absolute output level). CALIBRATION is played with the
// electrodes held right against a recording electrode, so it takes the QUIET level — a
// full-scale tone at contact range clips the recorder's input amplifier. Every other
// program's lead-in has to be detectable at a distance, so it takes the LOUD one. The two
// answer opposite constraints, which is exactly why they are separate knobs.
static inline float marker_amplitude_for_program(EelProgram prog, float leadin_amp,
                                                 float cal_amp) {
  return (prog == PROG_CALIBRATION) ? cal_amp : leadin_amp;
}

// The output level for a library item, given the driver's two absolute item levels (which
// live in the .ino alongside the marker levels). An eel's localization discharge is
// genuinely lower-voltage at the source than its volley, so the two classes play at
// different levels — a perceptible loc<->volley contrast. Amplitude is a firmware concern
// by design (the recorded amplitude is distance-confounded and deliberately left out of
// the stimulus data), so the contrast lives here rather than in the library.
//
// Keying off the item's own KIND is what guarantees the localization level is the SAME
// whether it is played alone (B) or as the lead into a strike (D) — the two cannot drift
// apart, because both are STIM_LOCALIZATION items and there is only one knob.
static inline float item_amplitude_for_kind(uint8_t kind, float loc_amp, float volley_amp) {
  return (item_class_for_kind(kind) == ITEM_CLASS_LOCALIZATION) ? loc_amp : volley_amp;
}

// The LED level while the given segment kind is playing.
static inline bool led_on_for_segment(uint8_t seg_kind, uint32_t blink_remaining) {
  if (seg_kind == SEG_TONE) return true;                 // solid for the whole tone
  if (seg_kind == SEG_ITEM) return blink_remaining > 0u; // one blink per pulse
  return false;                                          // SEG_SILENCE: dark
}

// ===== Debounce (pure) =====================================================
// millis()-based debounce + falling-edge (press) detector. `raw_high` is the raw pin level
// read active-low-inverted (true == released/HIGH, false == pressed/LOW). Returns true
// exactly once per debounced press. Pure: `now_ms` is injected.
typedef struct {
  bool     last_stable;    // last debounced level (true == released)
  bool     last_read;      // last raw level seen (bounce tracker)
  uint32_t last_change_ms; // when the raw level last changed
} DebounceState;

static inline void debounce_init(DebounceState* d) {
  d->last_stable = true;   // pulled-up idle == released
  d->last_read = true;
  d->last_change_ms = 0;
}

static inline bool debounce_fell(DebounceState* d, bool raw_high,
                                 uint32_t now_ms, uint32_t debounce_ms) {
  if (raw_high != d->last_read) {   // raw changed -> restart the stability timer
    d->last_read = raw_high;
    d->last_change_ms = now_ms;
  }
  if ((now_ms - d->last_change_ms) >= debounce_ms && raw_high != d->last_stable) {
    d->last_stable = raw_high;      // level held stable long enough -> commit it
    if (!raw_high) return true;     // committed transition to pressed == fire
  }
  return false;
}

// ===== Arduino glue (GPIO / millis / random) ===============================
#ifdef ARDUINO
#include <Arduino.h>

static uint8_t  g_vol_idx[N_STIM_ITEMS];
static uint8_t  g_loc_idx[N_STIM_ITEMS];
static uint8_t  g_n_vol = 0, g_n_loc = 0;
static DebounceState g_btn[N_PROGRAMS];
static const uint8_t BTN_PINS[N_PROGRAMS] = { BTN_A_PIN, BTN_B_PIN, BTN_C_PIN, BTN_D_PIN };

// Configure the button + LED pins and precompute the per-class item lists.
// Call once from setup(), after the driver's own analog setup.
static inline void eel_control_begin() {
  for (uint8_t i = 0; i < N_PROGRAMS; i++) {
    pinMode(BTN_PINS[i], INPUT_PULLUP);
    debounce_init(&g_btn[i]);
  }
  pinMode(LED_PIN, OUTPUT);
  digitalWriteFast(LED_PIN, LOW);
  build_item_lists(g_vol_idx, &g_n_vol, g_loc_idx, &g_n_loc);
}

// The program whose button just fired, or PROG_NONE. Ticks EVERY button's debounce on
// every call (it never early-returns), so a held button cannot spurious-retrigger when a
// playback ends, and a press during a playback is consumed and ignored. If two buttons
// somehow commit in the same pass the lowest (A < B < C < D) wins — a harmless tie-break.
static inline int eel_control_program_fired() {
  const uint32_t now = millis();
  int fired = PROG_NONE;
  for (uint8_t i = 0; i < N_PROGRAMS; i++) {
    bool raw_high = (digitalRead(BTN_PINS[i]) != LOW);   // active-low: HIGH == released
    if (debounce_fell(&g_btn[i], raw_high, now, BTN_DEBOUNCE_MS) && fired == PROG_NONE)
      fired = (int)i;
  }
  return fired;
}

// Draw a random library item of each class, or -1 if the library has none.
// The index lets the driver look up both STIM_ITEMS[idx] and its lead-in gap
// STIM_LEAD_GAP_SAMP[idx].
static inline int eel_control_pick_localization() {
  return g_n_loc ? (int)g_loc_idx[random(g_n_loc)] : -1;
}
static inline int eel_control_pick_volley() {
  return g_n_vol ? (int)g_vol_idx[random(g_n_vol)] : -1;
}

#endif  // ARDUINO
