// rc_control.h — L3 control surface: 4-channel RC input for the boat unit.
//
// An ADDITIONAL input source OR-ed with the physical panel (panel_control.h), never a
// replacement. The same binary runs the bench unit (panel only) and the boat unit (RC), so the
// panel keeps working with no transmitter.
//
// The unit rides a catamaran airboat; a FlySky FS-i6X / FS-iA6B link drives it. The receiver
// sits in the OTHER hull on a galvanically-isolated power domain; the ONLY crossing is optical,
// through a 4-channel PC817 optocoupler module in THIS (stimulator) box. Four servo-PWM channels
// are decoded:
//
//   CH3  throttle stick (ratcheted)    -> localization ON/OFF (dead-band) + TICK TEMPO
//                                         (0.5..20 Hz, LOGARITHMIC — see rc_rate_to_hz)
//   CH4  right stick axis (self-centre)-> one-shot TRIGGER: throw high = VOLLEY, low = SHAM
//   CH5  pot                           -> localization RANDOMNESS (0 = metronome .. 1.0 = the eel)
//   CH6  pot                           -> AMPLITUDE (sets the volley/max; localization = volley/2)
//
// All PURE logic (width->unit, low-pass, quantise+hysteresis, throttle map, trigger
// edge-detect, the rate/randomness/amplitude knob maps) sits ABOVE the `#ifdef ARDUINO` line
// and host-tests off-device (host_test/rc_control_selftest.cpp). Only the pin-change ISR
// capture is Arduino-specific.
//
// THE INTERVAL DRAW NO LONGER LIVES HERE. Until 2026-08-21 this file owned the localization
// IPI: a unit-mean lognormal around the set rate, parameterised by a jitter CV
// (`rc_ipi_samples` + `rc_std_normal`, both now gone). That is a RENEWAL process — every
// interval drawn independently — and real eels are not: their log-intervals correlate 0.55
// at lag 1 and 0.21 at lag 20. The rhythm now comes from a model fitted to 99 010 measured
// resting intervals, in src/eel_core/loc_rhythm.h (L2). What is left here is the L3 job:
// turning two RC channels into that model's two knobs.
//
// WHAT LIVES WHERE. This file owns the RC-specific constants (pins, calibration, channel
// thresholds, conditioning, failsafe) — they used to sit in fakefish-rc's monolithic config.h,
// which mixed the output stage, the RC surface and the panel in one file. The output stage is now
// src/eel_core/config.h (L1, shared by every device), the playback/session constants are
// generated into src/eel_core/stim_levels.h from shared/stim_constants.json, and the panel +
// LED-feedback constants are in panel_control.h. The localization SCHEDULER moved to
// src/eel_core/locgen.h (L2) so a future de-novo surface can reuse it without the RC decode layer.
#pragma once
#include <stdint.h>
#include <math.h>
#include "src/eel_core/config.h"       // SAMPLE_RATE_HZ (L1 output stage / sample clock)
#include "src/eel_core/stim_levels.h"  // LOC_RATE_*, MASTER_AMP_*, VOLLEY_AMP_RATIO (generated)
#include "src/eel_core/locgen.h"       // LocGen + locgen_* (renders the localization pulse)
#include "src/eel_core/loc_rhythm.h"   // the FITTED resting rhythm — decides WHEN it happens

// ===== RC input pins (interrupt-capable; avoid 2,3 = DRV IN1, 13 LED, 14 A0) ======
// Pins 4-7 (not the original 5-8): pin 8 was a DEAD GPIO on the build Teensy 4.1 — a bench test
// (firmware/rc_input_test) showed its raw digitalRead never responded to any input, so every RC
// channel shifted down one pin. Plain knobs; restore 5-8 on a board with a working pin 8.
#define RC_PIN_THROTTLE  4                 // CH3
#define RC_PIN_TRIGGER   5                 // CH4
#define RC_PIN_RANDOM    6                 // CH5
#define RC_PIN_AMP       7                 // CH6
#define RC_N_CHANNELS    4
#define RC_IDX_THROTTLE  0                 // array indices (order matches RC_PINS[] below)
#define RC_IDX_TRIGGER   1
#define RC_IDX_RANDOM    2
#define RC_IDX_AMP       3

// ===== PC817 optocoupler decode (HY-M154 4-channel "817 Module") ===========
// Board = HY-M154, BARE-COLLECTOR output (only V1-V4 + G on the Teensy side; NO V+/pull-up pin).
// Wiring: receiver signal -> input Vx, receiver GND -> input-side G (isolation barrier); output
// Vx -> Teensy pin as INPUT_PULLUP (the internal pull-up IS the collector load), output G ->
// Teensy GND. NOTHING connects to the Teensy 3.3 V from this board. The PC817 INVERTS: the pin is
// pulled LOW while the RC servo pulse is HIGH, so the true 1000-2000 us width is the LOW duration
// -> measure the LOW level (below). If decoded widths jitter, add an external 1-2 kOhm pull-up per
// Vx to 3.3 V (the internal ~40k is weak against the PC817's slow turn-off); try INPUT_PULLUP first.
// Set RC_MEASURE_ACTIVE_HIGH to 1 ONLY for a non-inverting isolator.
#define RC_MEASURE_ACTIVE_HIGH 0

// ===== Calibration — bench-measured on the build unit (2026-08-07) ==========
// Pulse widths (us) at each channel's extremes AFTER the PC817, which offsets everything ~300 us
// LOW of nominal (full ~1000 us travel is preserved on every channel). Endpoints sit at the steady
// extremes so rc_unit()'s clamp saturates to exactly 0/1 at the mechanical stops. Sanity: CH4 rests
// ~1200 us -> unit ~0.50, dead-centre of the 0.40-0.60 trigger re-arm band. REDO on another rig:
// flash firmware/rc_input_test (or serial-print rc_get_width_us), hold each control low then high,
// paste { low_us, high_us } here.
#define RC_CAL_THROTTLE_MIN 705
#define RC_CAL_THROTTLE_MAX 1700
#define RC_CAL_TRIGGER_MIN  705
#define RC_CAL_TRIGGER_MAX  1700
#define RC_CAL_RANDOM_MIN   680
#define RC_CAL_RANDOM_MAX   1675
#define RC_CAL_AMP_MIN      730
#define RC_CAL_AMP_MAX      1725

// ===== CH3 throttle: localization on/off + rate ============================
// On/off is DEBOUNCED + HYSTERETIC so a resting throttle yields ZERO localization pulses even with RC
// noise: ENABLING requires the RAW throttle unit to stay >= CH3_ON_THRESH for CH3_ON_DEBOUNCE_TICKS
// consecutive decode ticks; DISABLING is immediate below CH3_OFF_DEADBAND (fail-safe). A transient noise
// glitch — however large — never sustains the debounce, so it can never fire a stray localization pulse.
// The .ino additionally treats a throttle below the dead-band as a MASTER off: it clears the panel LOC
// latch that is OR-ed with this gate, so throttle-down means zero pulses whatever the panel did.
//
// THE OFF ZONE IS SIZED IN MICROSECONDS, NOT IN PERCENT (widened 0.06/0.12 -> 0.15/0.22 on 2026-08-22).
// Against the ~995 us calibrated throttle span, 0.06 bought only ~60 us of margin below which the stick
// must read to shut localization down — and the PC817's error is DIRECTIONAL. The decoder measures the
// LOW duration at the pin, and the opto's slow turn-off lengthens it; anything that slows it further
// (a warm box, LED ageing, a weaker pull-up) grows every measured width, resting throttle included, and
// pushes it up through a 60 us dead-band. Then throttle-down stops meaning off and there is no way to
// stop the fish from shore. 0.15 is ~149 us of margin against a one-directional drift.
//
// It costs TRAVEL, NOT RANGE. rc_throttle_frac renormalises over the above-dead-band travel, so every
// rung keeps its value and the ladder still spans 0.5-20 Hz; only the stick length spent reaching them
// shrinks, from 94 % to 85 %. Cheap on a geometric ladder, where a rung is a ratio rather than a slice
// of travel.
#define CH3_OFF_DEADBAND  0.15f            // throttle unit below this -> localization OFF (immediate; resting = clean off)
#define CH3_ON_THRESH     0.22f            // must first EXCEED this (hysteresis margin above the dead-band) to enable
#define CH3_ON_DEBOUNCE_TICKS 30u          // ...and stay above it this many decode ticks (~150 ms at
                                           // RC_DECODE_PERIOD_MS 5 ms) before localization ENABLES. Raise if a
                                           // noise burst still leaks a pulse; lower for a snappier throttle-on.
#define RC_RATE_STEPS     20               // quantise rate (avoid Hz wander) ~1 Hz/step
                                           // (LOC_RATE_MIN_HZ / LOC_RATE_MAX_HZ come from stim_levels.h)
// NOTE the ladder is now a TICK TEMPO — one over the median interval, the number quoted as an
// eel's discharge rate — not an average pulse rate. A 5 Hz setting means the fish ticks at
// 5 Hz and delivers ~3.3 pulses/s, where the retired lognormal's 5 Hz meant 5 pulses/s. The
// device anchors the median because a heavy-tailed interval distribution cannot hold both,
// and the median is the one that keeps this Hz scale meaningful (spec section 5.3).

// ===== CH4 trigger: ONE-DIRECTIONAL one-shot (throw high = fire a BLINDED trial) =====
// Unit thresholds (1000us=0.0, 1500us=0.5, 2000us=1.0). Throw HIGH fires one trial; the axis
// must return to CENTER_LO..CENTER_HI to re-arm. Generous margins vs RC jitter.
//
// A LOW throw does NOTHING — deliberately. The trigger used to be bidirectional (high =
// volley, low = sham), which let the operator choose the trial type and therefore let their
// timing and position correlate with it. Now one throw means "run a trial" and the FIRMWARE
// draws volley-vs-sham (see TRIAL_P_VOLLEY, drawn in the ISR). The low half of the axis is
// inert: it cannot fire, and it does not even consume the arm.
#define CH4_VOLLEY_THRESH 0.70f            // ~1700 us — the fire threshold
#define CH4_CENTER_LO     0.40f            // re-arm zone (~1400 us)
#define CH4_CENTER_HI     0.60f            // re-arm zone (~1600 us)

// ===== CH5 pot: localization RANDOMNESS ====================================
// The fitted model's randomness knob, NOT the retired jitter CV. It scales the state score, and
// THE POT NOW SPANS EXACTLY THE TWO ENDPOINTS THAT MEAN SOMETHING: 0 is a perfect metronome at
// the nominal tempo, and full scale is 1.0 — the measured eel, the model in undiluted control of
// the temporal dynamics. Nothing in between is invented; the knob interpolates between a
// frequency-locked train and biology.
//
// It ran to 1.5 until 2026-08-22 (the shipped tables go to 2.0). That top third was a setting
// with no referent — MORE irregular than the fish the model was fitted to — and it was actively
// harmful in the field: rate is only controllable on average, and less so as randomness rises,
// so at 1.5 the realised rate over a two-minute window can sit several times off the commanded
// tempo. The 2026-08-22 field log has a run commanded at 4 Hz / randomness 1.5 that opened at
// 1940/259/133/211 ms, took a genuine 4.3 s silence, then collapsed to a near-constant 38 ms for
// fifty pulses. That is the model behaving correctly at a setting that should not have been on
// the dial. See firmware/README.md -> "Rate is only controllable on average".
//
// It does NOT blend toward a Poisson process: the lag-1 autocorrelation stays around 0.52
// across the whole range. It dials how MUCH the rate varies, leaving how it varies over time
// alone — an eel that is more or less variable, not an eel that is more or less random. That
// is why every setting still sounds like a fish.
//
// There is deliberately NO upper clamp on the interval any more. The retired
// RC_IPI_MAX_FACTOR bounded the lognormal's right tail at 4x the mean to avoid an "absurd
// gap"; the gaps are not absurd, they are measured. 1.5 % of real resting intervals exceed
// 5 s and 0.6 % exceed 10 s, and the pulses bracketing a real 30 s silence are exactly as
// loud as the rest of the recording — the fish did not swim away, it stopped. The interval
// table's own top knot (32 s) is the only bound, and it is the 99.98th percentile of the
// measurement rather than a number someone picked.
#define LOC_RANDOMNESS_MAX 1.0f            // top of the CH5 pot IS the measured eel (tables run to 2.0)
#define RC_RANDOM_STEPS   16               // quantise the randomness pot
                                           // (the minimum IPI, LOC_REFRACTORY_SAMP, comes from
                                           //  stim_levels.h, and is now only a safety floor)

// ===== CH6 pot: amplitude (sets the VOLLEY / max; localization is derived) ==
// The amplitude control (CH6 pot, or PANEL_VOLLEY_AMP on the bench) sets the VOLLEY amplitude — the
// LOUD discharge, the "max" — over MASTER_AMP_MIN..MAX (0..full rail). Localization is ALWAYS a fixed
// fraction BELOW it: loc = volley / VOLLEY_AMP_RATIO (a quarter at 4.0). Anchoring at the volley (not the
// loc) is deliberate: the old "loc = pot, volley = 2x pot clamped to 1.0" made loc and volley CONVERGE
// to full once the pot passed 0.5 (volley clamped while loc kept rising) — so localization jumped to
// max. Deriving loc FROM the volley keeps the 2:1 (volley:loc) ratio at EVERY pot position and needs
// no clamp (the volley never exceeds full scale because the pot IS the volley level).
// The ratio moved 2 -> 4 on 2026-08-21: synthetic volleys now carry the MEASURED amplitude
// envelope, whose tail reaches ~0.34 of the volley's own peak, and at 2:1 that tail landed on
// the localization level. See shared/stim_constants.json for the full reasoning.
// The levels themselves (MASTER_AMP_MIN/MAX, VOLLEY_AMP_RATIO) come from stim_levels.h.
#define RC_AMP_STEPS      16               // quantise amplitude (8-bit duty loses shape at low amp)

// ===== Volley snippet selection (from the stored library) ==================
// The synthetic volleys (kind 1, STIM_ITEMS indices 7..106) carry both the decaying-rate
// envelope and a per-pulse amplitude envelope. A volley random-picks one of these per fire.
// Pin FIRST + COUNT=1 for a reproducible volley.
//
// SINCE 2026-08-21 THE POOL IS A DRAWN POPULATION, NOT A DESIGNED LADDER. Every item is one
// independent draw from a generative model fitted to the 200 strongest hunting volleys in the
// FLONA 2025 dataset (43 recordings, 16 sites) — start rate, duration and decay drawn jointly
// so their correlations survive, then pulse times integrated off that rate curve with the
// measured per-volley regularity. So a uniform pick over the pool approximates drawing a real
// volley at random, and the pool is 100 items deep to make that approximation a good one:
// the count is a SAMPLING RESOLUTION, not a menu of hand-chosen variants.
//
//   duration        quartiles ~0.29 / 0.46 / 0.73 s
//   pulses          ~61 / 90 / 136
//   sustained peak  ~366 / 450 / 566 Hz
//
// The ceiling on this pool is NOT flash, it is the pulse log: pulse_log.h stores the library
// item in an int8_t (PLOG_ABSENT_ITEM = -1), so the whole library must stay under 128 items.
// At 100 volleys the highest index is 112. Growing further means changing that format and its
// golden file, not just this constant.
//
// The model, its fitted numbers and its caveats: docs/VOLLEY_GENERATIVE_SPEC.md.
// (Superseded: a log-spaced 0.1-4 s duration ladder of 21 items, set from field observation
// because the only volleys this repo could then see were truncated tracker fragments.)
#define RC_VOLLEY_ITEM_FIRST 7
#define RC_VOLLEY_ITEM_COUNT 100

// ===== Conditioning ========================================================
#define RC_QUANT_HYST     0.30f            // quantiser boundary hysteresis (fraction of a step)
// NOTE: RC_EMA_ALPHA is coupled to the CH4 centre band — a much smaller alpha would let a
// late-acquiring thrown trigger stick ramp THROUGH the centre band and arm. The acquire-snap in
// loop() (u_trig snapped on the CH4 present-edge) neutralises that; still keep alpha >= ~0.2.
#define RC_EMA_ALPHA      0.25f            // decoded-value low-pass per decode tick
#define RC_DECODE_PERIOD_MS 5u             // run the loop() decode at ~200 Hz
#define RC_GLITCH_MIN_US  400u             // accept a measured RC width only within [min,max] us:
#define RC_GLITCH_MAX_US  3000u            // rejects opto glitches AND the ~18 ms inter-frame gap

// ===== Failsafe ============================================================
// A channel with no edge for this long is "absent". On CH3 (throttle) absence -> throttle 0 ->
// localization off; the CH4 trigger never fires without a live throw, so signal loss can never
// start a volley/sham. Presence is tracked in ms (change-detection on the ISR edge timestamp),
// which stays wrap-safe for ~49 days of loss — a raw micros() diff would wrap at ~71 min.
#define RC_ABSENCE_MS     500u             // 500 ms

// Channel calibration (measured widths, us). Built from the constants above; index order
// matches RC_IDX_* / RC_PINS[].
typedef struct { uint16_t us_min, us_max; } RcCal;
static const RcCal RC_CAL[RC_N_CHANNELS] = {
  /* throttle */ { RC_CAL_THROTTLE_MIN, RC_CAL_THROTTLE_MAX },
  /* trigger  */ { RC_CAL_TRIGGER_MIN,  RC_CAL_TRIGGER_MAX  },
  /* random   */ { RC_CAL_RANDOM_MIN,   RC_CAL_RANDOM_MAX   },
  /* amp      */ { RC_CAL_AMP_MIN,      RC_CAL_AMP_MAX      },
};

// ===== Pure helpers (host-tested) =========================================
static inline float rc_clampf(float x, float lo, float hi) {
  return x < lo ? lo : (x > hi ? hi : x);
}

// Map a measured pulse width (us) to a unit value [0,1], clamped.
static inline float rc_unit(uint32_t width_us, RcCal cal) {
  float lo = (float)cal.us_min, hi = (float)cal.us_max;
  if (hi == lo) return 0.0f;
  return rc_clampf(((float)width_us - lo) / (hi - lo), 0.0f, 1.0f);
}

static inline float rc_ema(float prev, float x, float alpha) { return prev + alpha * (x - prev); }

// Quantise [0,1] to n_levels with boundary hysteresis (a reading must cross a step boundary by
// `hyst` of a step before the committed level changes). Returns the committed level [0,n-1].
static inline int rc_quantize_hyst(float x, int prev_level, int n_levels, float hyst) {
  if (n_levels <= 1) return 0;
  float step = 1.0f / (float)(n_levels - 1);
  int lvl = (int)(x / step + 0.5f);
  if (lvl < 0) lvl = 0;
  if (lvl > n_levels - 1) lvl = n_levels - 1;
  if (lvl != prev_level) {
    float boundary = ((float)prev_level + (lvl > prev_level ? 0.5f : -0.5f)) * step;
    float need = (lvl > prev_level ? boundary + hyst * step : boundary - hyst * step);
    if ((lvl > prev_level && x < need) || (lvl < prev_level && x > need)) return prev_level;
  }
  return lvl;
}

// ===== CH3 throttle: localization on/off + rate ============================
static inline int rc_throttle_on(float unit) { return unit >= CH3_OFF_DEADBAND ? 1 : 0; }
// Fraction 0..1 across the ABOVE-dead-band travel (0 at the dead-band edge, 1 at full throttle).
static inline float rc_throttle_frac(float unit) {
  if (unit < CH3_OFF_DEADBAND) return 0.0f;
  return rc_clampf((unit - CH3_OFF_DEADBAND) / (1.0f - CH3_OFF_DEADBAND), 0.0f, 1.0f);
}
// Debounced + hysteretic localization on/off from the RAW per-tick throttle unit. ENABLING requires
// `raw_unit` to stay >= on_thresh for `debounce_ticks` consecutive ticks; DISABLING is immediate when
// it drops below off_deadband (fail-safe). While already on and in the [off_deadband, on_thresh)
// hysteresis band it HOLDS on. A transient noise glitch never sustains the debounce, so a resting
// throttle yields ZERO pulses however large the glitch. `count` is the caller-owned run of consecutive
// above-on-threshold ticks (reset it when the channel is lost). debounce_ticks==0 -> no debounce.
static inline bool rc_throttle_gate(float raw_unit, bool prev_on, uint32_t* count,
                                    float on_thresh, float off_deadband, uint32_t debounce_ticks) {
  if (raw_unit < off_deadband) { *count = 0; return false; }   // released -> OFF now (fail-safe)
  if (prev_on) return true;                                     // already on -> hysteresis holds it
  if (raw_unit >= on_thresh) { if (*count < debounce_ticks) (*count)++; }  // sustained rise -> build
  else *count = 0;                                              // in [off,on) while off -> don't build
  return *count >= debounce_ticks;                              // ON only after a sustained rise
}
// Tick tempo (Hz) for a quantised rate level: a LOGARITHMIC ladder over the CH3 travel, so
// every rung is the same RATIO from its neighbours rather than the same number of Hz.
//
// It was linear 1-20 Hz until 2026-08-22. Rate is perceived and used multiplicatively -- the
// interesting question is always "twice as fast", never "one Hz faster" -- and a linear ladder
// spends its resolution in the wrong place: ~85 % of the throttle's travel sat above anything a
// real eel does (they tick at 3.15 Hz), while the entire biological range was squeezed into the
// bottom rungs. The slowest settings were worse than coarse, they were unreachable: 1 Hz landed
// inside the ~6 % of travel between CH3_OFF_DEADBAND and CH3_ON_THRESH, right against the edge
// where localization cuts out. A field log confirmed the operator never got below 4 Hz.
//
// Geometric spacing puts the resolution where the biology is. At 20 steps over 0.5-20 Hz each
// rung is ~21 % from the next, so the steps near 1 Hz are ~0.1 Hz instead of ~1 Hz, and the
// measured eel lands at almost exactly mid-throttle (sqrt(0.5 * 20) = 3.16 Hz). The endpoints
// are unchanged in meaning: level 0 is LOC_RATE_MIN_HZ and the top level is LOC_RATE_MAX_HZ.
//
// powf() is fine here: this runs in loop() at the RC decode rate, never in the sample ISR.
static inline float rc_rate_to_hz(int rate_level, int rate_steps) {
  float frac = (rate_steps > 1) ? (float)rate_level / (float)(rate_steps - 1) : 0.0f;
  return LOC_RATE_MIN_HZ * powf(LOC_RATE_MAX_HZ / LOC_RATE_MIN_HZ, frac);
}
// ...as the model's rate knob, which is a TEMPO MULTIPLIER (1.0 == the measured eel).
static inline float rc_rate_to_tempo(int rate_level, int rate_steps) {
  return loc_rhythm_rate_for_hz(rc_rate_to_hz(rate_level, rate_steps));
}

// ===== CH4 trigger: one-directional one-shot edge-detect ===================
// A self-centring axis. Throwing HIGH (>= fire_thr) fires exactly one trial; the axis must
// return to the centre band (center_lo..center_hi) to RE-ARM. It fires at most once per throw,
// never on a held/bouncing stick, and NEVER on the first read with the stick already thrown
// (armed starts true only if the first read is centred) -> no boot-time fire.
//
// The LOW half of the axis is completely inert: it cannot fire, and it does not consume the
// arm either, so throwing the wrong way costs nothing. The trial TYPE is not chosen here at
// all — the caller requests RC_TRIG_RANDOM and the firmware draws volley-vs-sham at playback
// time, which is what blinds the operator.
typedef enum {
  RC_TRIG_NONE   = 0,
  RC_TRIG_VOLLEY = 1,   // an explicit volley (the bench panel button)
  RC_TRIG_SHAM   = 2,   // an explicit sham   (the bench panel button)
  RC_TRIG_RANDOM = 3,   // "run a trial" — the ISR draws volley or sham (the RC lever)
} RcTrigKind;
typedef struct { uint8_t armed; uint8_t init; } RcTrigger;

// Returns 1 on a fresh high throw, else 0.
static inline int rc_trigger_step(RcTrigger* t, float unit,
                                  float fire_thr, float center_lo, float center_hi) {
  int thrown    = (unit >= fire_thr) ? 1 : 0;
  int at_center = (unit >= center_lo && unit <= center_hi) ? 1 : 0;
  if (!t->init) { t->init = 1; t->armed = at_center ? 1 : 0; return 0; }
  int fire = 0;
  if (t->armed && thrown) { fire = 1; t->armed = 0; }
  if (at_center) t->armed = 1;   // returned to centre -> re-arm for the next throw
  return fire;
}

// ===== CH5 randomness pot / CH6 amplitude pot ==============================
static inline float rc_randomness(int level, int steps) {
  float f = (steps > 1) ? (float)level / (float)(steps - 1) : 0.0f;
  return f * LOC_RANDOMNESS_MAX;
}
// The amplitude control maps to the VOLLEY (max) amplitude 0..1 — the loud discharge, the "max".
static inline float rc_master_amp(int level, int steps) {
  float f = (steps > 1) ? (float)level / (float)(steps - 1) : 0.0f;
  return MASTER_AMP_MIN + f * (MASTER_AMP_MAX - MASTER_AMP_MIN);
}
// Localization amplitude = a fixed fraction of the volley (half at VOLLEY_AMP_RATIO 2.0). Deriving loc
// FROM the volley keeps loc = volley/ratio at EVERY level — no clamp, no convergence-to-max.
static inline float rc_loc_amp(float volley) {
  return volley / VOLLEY_AMP_RATIO;
}

// ===== Arduino glue: 4-channel pulse-width capture on pin-change interrupts =====
// pulseIn() is FORBIDDEN (it blocks up to a full 20 ms frame). Each channel records micros() on
// every edge and computes the ACTIVE-level width; the sample clock is given higher priority so
// pulse OUTPUT timing is never perturbed by input capture.
#ifdef ARDUINO
#include <Arduino.h>

static const uint8_t RC_PINS[RC_N_CHANNELS] = { RC_PIN_THROTTLE, RC_PIN_TRIGGER, RC_PIN_RANDOM, RC_PIN_AMP };
static volatile uint32_t rc_edge_us[RC_N_CHANNELS];   // leading edge of the active level
static volatile uint32_t rc_width[RC_N_CHANNELS];     // last measured width (us)
static volatile uint32_t rc_seen_us[RC_N_CHANNELS];   // micros() of the last edge (absence detect)

static inline void rc_edge(uint8_t i) {
  uint32_t now = micros();
  rc_seen_us[i] = now | 1u;   // force non-zero: 0 is the "never seen" sentinel (rc_channel_present)
  bool level = (digitalReadFast(RC_PINS[i]) != LOW);
  // The PC817 inverts, so RC_MEASURE_ACTIVE_HIGH is 0 -> the active (servo-pulse) level is LOW at
  // the pin, and we measure its duration to recover the true 1000-2000 us width.
  bool active = RC_MEASURE_ACTIVE_HIGH ? level : !level;
  if (active) {
    rc_edge_us[i] = now;                 // leading edge of the active level
  } else {
    uint32_t w = now - rc_edge_us[i];    // trailing edge -> width
    if (w >= RC_GLITCH_MIN_US && w <= RC_GLITCH_MAX_US) rc_width[i] = w;   // reject glitches / frame gap
  }
}
static void rc_isr0() { rc_edge(0); }
static void rc_isr1() { rc_edge(1); }
static void rc_isr2() { rc_edge(2); }
static void rc_isr3() { rc_edge(3); }

static inline void rc_begin() {
  static void (*const isrs[RC_N_CHANNELS])() = { rc_isr0, rc_isr1, rc_isr2, rc_isr3 };
  for (int i = 0; i < RC_N_CHANNELS; i++) {
    pinMode(RC_PINS[i], INPUT_PULLUP);   // HY-M154 bare-collector board: the Teensy's internal
                                         // pull-up IS the collector load (no board pull-up; nothing to 3.3 V)
    rc_edge_us[i] = 0; rc_width[i] = 0; rc_seen_us[i] = 0;
    attachInterrupt(digitalPinToInterrupt(RC_PINS[i]), isrs[i], CHANGE);
  }
}

// Last measured width for channel i (us). 32-bit aligned read is atomic on the M7.
static inline uint32_t rc_get_width_us(uint8_t i) { return rc_width[i]; }
// Raw ISR edge timestamp for channel i (0 == never seen). Presence is derived in the .ino by
// CHANGE-DETECTION on this value against a ms clock (wrap-safe over ~49 days), rather than a raw
// micros() diff against this frozen timestamp (which would wrap at ~71 min of sustained loss).
static inline uint32_t rc_last_edge_us(uint8_t i) { return rc_seen_us[i]; }

#endif  // ARDUINO
