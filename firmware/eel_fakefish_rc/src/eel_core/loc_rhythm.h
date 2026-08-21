// loc_rhythm.h — L2 producer: WHEN the next localization pulse happens.
//
// The fitted resting rhythm of a real *Electrophorus*, as an online state update. One call
// per pulse, forever: `loc_rhythm_next_ipi_samp()` returns the interval to the next onset.
// `locgen.h` renders the pulse; this decides when. The two are deliberately separate, so a
// future de-novo surface can take the rhythm without the RC decode layer, and so this file
// can be tested against the Python reference without any notion of a waveform.
//
// WHAT THIS REPLACED, AND WHY. Until 2026-08-21 the interval came from a unit-mean lognormal
// draw around a set rate (rc_control.h's `rc_ipi_samples`, jitter knob = CV). That is a
// RENEWAL process: every interval is drawn independently, so consecutive log-intervals are
// uncorrelated at every lag. Real eels are not. In 99 010 measured resting intervals the
// log-interval autocorrelation is 0.55 at lag 1, 0.36 at lag 5 and still 0.21 at lag 20 — the
// fish has a discharge rate that WANDERS, over a few seconds and over a minute, and only part
// of each interval is fresh noise. That wander is the single thing a renewal process gets
// wrong, and it is what makes a recording sound like a fish instead of a random number
// generator. Everything below exists to reproduce it.
//
// THE MODEL, in one line:
//
//     z = randomness * (offset + x_fast + x_medium + x_slow + SD_WHITE * e);  IPI = Q(z) / tempo
//
// where Q is the measured interval quantile table (loc_model_params.h) and each x is an
// Ornstein-Uhlenbeck component that relaxes in WALL-CLOCK time — after waiting dt seconds,
// x <- a*x + sd*sqrt(1-a^2)*e with a = exp(-dt/tau). dt is the interval just emitted.
//
// FOUR THINGS IN HERE ARE MEASUREMENTS, NOT MODELLING CHOICES. Do not simplify them away:
//   1. The state relaxes in TIME, not in pulse count. After a 3 s silence the fish has
//      forgotten more than after three 100 ms intervals; refitting per-pulse costs 699 nats.
//   2. TWO timescales (3.2 s… — the shipped fast tau is 2.5 s, see loc_model_params.h — and
//      96 s), plus a slow one and a per-power-on offset. One is audibly not enough (619 nats).
//   3. The noise is PEAKED, not Gaussian: a two-scale mixture. A Gaussian of the same variance
//      gets the interval distribution and the autocorrelation right and still sounds wrong
//      (median CV2 0.60 against a measured 0.38).
//   4. LONG SILENCES ARE REAL. 1.5 % of resting intervals exceed 5 s and 0.6 % exceed 10 s;
//      the table's top knot is 32 s. The device WILL occasionally go quiet that long. That is
//      measured behaviour — the pulses bracketing a real 30 s silence are exactly as loud as
//      the rest of the recording, so the fish did not swim away, it stopped. Do not clamp it.
//
// THE BURST HAZARD IS DELIBERATELY NOT PORTED. The model also carries `interrupt()`: the
// fish's own decision to launch a fast run, about 1 in 49 resting pulses. This device does not
// implement it, and that is a protocol decision, not an omission. The RC surface runs a
// BLINDED trial design in which a marker's pulse count IS the trial record; a spontaneous
// burst would be an unmarked volley, and one landing inside a sham would destroy the
// no-stimulus control. Every volley this device emits is operator-requested. See CLAUDE.md.
//
// PURE. No Arduino, no globals, no allocation — the caller owns the LocRhythm. It does need
// <math.h> (expf/logf/sqrtf/sinf/cosf), unlike locgen.h. Safe to include from any number of
// translation units: everything here is `static inline` or `static const`, and all mutable
// state lives in the caller's struct (contrast out_hal.h, whose file-static shaper state makes
// it a one-TU header).
//
// COST, per interval — NOT per sample tick. Three expf, two Box-Muller pairs (2 logf, 2 sinf,
// 2 cosf), five sqrtf and eight PRNG words: about a dozen libm calls a few times a second,
// paid on the one tick that closes an interval. Comfortable on a Teensy 4.1. If a 3.5 ever
// proves tight on the bench, the spec sanctions two exact-in-context reductions (§7): drop to
// two components (the reference's own `n_components=2`, which folds `slow` into the offset and
// is correct for runs short against an hour), or replace the medium/slow `expf` with
// `1 - dt/tau`, which is accurate to better than 0.01 % at a typical interval. Neither is done
// here: they would put the firmware and the Python reference on different arithmetic, and the
// golden parity test below is worth more than the microseconds.
//
// Full model, provenance and every caveat: docs/LOCALIZATION_GENERATIVE_SPEC.md.
#pragma once
#include <stdint.h>
#include <math.h>
#include "loc_model_params.h"   // the FITTED tables (generated from data/loc_model_params.json)

// Defensive floor on the effective tempo. The rate knob never approaches it (the CH3 ladder
// bottoms out at LOC_RATE_MIN_HZ, a tempo of ~0.32), but tempo divides the interval, so a zero
// would silently produce an infinite one — a train that stops without any fault being raised.
#define LOC_TEMPO_MIN 0.01f

typedef struct {
  float    x[LOC_N_COMPONENTS];  // OU component values, in score units
  float    offset;               // per-power-on tempo offset, in score units
  float    pending_dt;           // seconds elapsed but not yet applied to x
  float    m;                    // last predicted score (post-randomness)
  float    tempo;                // rate / gain(randomness) — the effective time dilation
  float    s;                    // the randomness knob
  uint32_t rng;                  // xorshift32 state (never 0)
  float    spare;                // the second Box-Muller normal, kept for the next call
  uint8_t  have_spare;
} LocRhythm;

// ===== noise ==========================================================================
// A PRIVATE PRNG, not Arduino's random(). Two reasons. It keeps this header pure and
// host-testable, and it decouples the rhythm's draws from the ISR's other random() consumers
// — the blinded volley/sham draw, the polarity flip and the volley item pick. Sharing one
// stream would make the blinded trial sequence depend on how many localization pulses
// happened to be emitted first, which is exactly the kind of hidden coupling a blinded design
// must not have. Seed it from random() once at boot (see loc_rhythm_init).
static inline uint32_t loc_rng_next(LocRhythm* r) {
  uint32_t x = r->rng;
  x ^= x << 13; x ^= x >> 17; x ^= x << 5;
  r->rng = x;
  return x;
}
// Uniform in [0,1). Taken from the HIGH 24 bits: xorshift32's low bits are its weakest.
static inline float loc_rng_unit(LocRhythm* r) {
  return (float)(loc_rng_next(r) >> 8) * (1.0f / 16777216.0f);
}

// Standard normal, Box-Muller. Produces TWO per call and keeps the second, so the four normals
// an interval needs cost two logf/sinf/cosf triples rather than four. The polar method would
// save the trig but rejects ~21 % of draws, and an unbounded loop has no place in an ISR.
static inline float loc_rhythm_gauss(LocRhythm* r) {
  if (r->have_spare) { r->have_spare = 0; return r->spare; }
  float u1 = ((float)(loc_rng_next(r) >> 8) + 1.0f) * (1.0f / 16777216.0f);   // (0,1] — never 0
  float u2 = (float)(loc_rng_next(r) >> 8) * (1.0f / 16777216.0f);            // [0,1)
  float mag = sqrtf(-2.0f * logf(u1));
  float ang = 6.2831853071795862f * u2;
  r->spare = mag * sinf(ang);
  r->have_spare = 1;
  return mag * cosf(ang);
}

// Unit-variance noise with the MEASURED excess kurtosis: a two-scale Gaussian scale mixture.
// With probability LOC_MIX_P the draw is the narrow one — the fish holding its rate almost
// exactly — otherwise the wide one, the jump. One extra uniform and a branch, and it is the
// difference between a fish and a twitch (spec §4).
static inline float loc_rhythm_noise(LocRhythm* r) {
  float sd = (loc_rng_unit(r) < LOC_MIX_P) ? LOC_MIX_SD_LO : LOC_MIX_SD_HI;
  return sd * loc_rhythm_gauss(r);
}

// ===== the two tables =================================================================

// Interval quantile table: score -> seconds, linearly interpolated, CLAMPED at both ends.
//
// Indexed by arithmetic rather than a search, which is valid only because the knot grid is
// uniform — the codegen asserts that (gen_constants._uniform_step), so a re-drop that
// re-spaced it fails there rather than silently reading the wrong knot here.
//
// NOTE this differs deliberately from the spec's §7 listing, which clamps the index to 39.999
// and so returns a fraction short of the top knot at the clamp. The clamp is HIT on every long
// silence, so it is worth landing on the knot exactly, as the reference's np.interp does.
static inline float loc_rhythm_interval_for_score(float z) {
  float u = (z - LOC_Z_LO) * LOC_Z_SCALE;
  if (!(u > 0.0f)) u = 0.0f;                                   // also catches NaN
  if (u > (float)(LOC_N_KNOTS - 1)) u = (float)(LOC_N_KNOTS - 1);
  int i = (int)u;
  if (i > LOC_N_KNOTS - 2) i = LOC_N_KNOTS - 2;                // top edge -> f == 1
  float f = u - (float)i;
  return expf(LOC_LOG_IPI_KNOTS[i] + f * (LOC_LOG_IPI_KNOTS[i + 1] - LOC_LOG_IPI_KNOTS[i]));
}

// The randomness knob's tempo gain, so the TICK TEMPO stays where the rate knob put it as the
// score's spread changes. Same uniform-grid indexing, same codegen guarantee.
static inline float loc_rhythm_gain(float randomness) {
  if (!(randomness > 0.0f)) return LOC_KNOB_GAIN[0];
  if (randomness >= LOC_KNOB_MAX) return LOC_KNOB_GAIN[LOC_KNOB_N - 1];
  float u = randomness * LOC_KNOB_SCALE;
  int i = (int)u;
  if (i > LOC_KNOB_N - 2) i = LOC_KNOB_N - 2;
  float f = u - (float)i;
  return LOC_KNOB_GAIN[i] + f * (LOC_KNOB_GAIN[i + 1] - LOC_KNOB_GAIN[i]);
}

// ===== the knobs ======================================================================
// `rate` is a TEMPO MULTIPLIER, not a frequency: 1.0 is the measured eel, whose tick tempo is
// LOC_NOMINAL_TICK_HZ (3.15 Hz). loc_rhythm_rate_for_hz() converts a CH3 setting.
//
// Rate is a pure time dilation — it divides the intervals AND stretches the time constants
// with them, because that is what the data says a slower fish is: refitting the memory as
// tau * (own tempo)^gamma peaks sharply at gamma = 1 and beats gamma = 0 by 413 nats. Halving
// the rate with tau fixed instead would let the state relax between pulses, so the wander
// washes out and the device drifts back toward a renewal process exactly at the slow settings
// — which is where it matters most. One `tempo` carries both knobs' dilation, which is why the
// state ages in the same stretched clock the intervals are emitted in.
//
// Randomness scales the state score: 0 is a metronome at exactly the nominal tempo, 1 is the
// measured eel, useful to ~1.5. It does NOT blend toward a Poisson process — the lag-1
// autocorrelation stays ~0.52 across the whole range. It dials how MUCH the rate varies, not
// how it varies over time, so every setting still sounds like a fish.
static inline float loc_rhythm_rate_for_hz(float tick_hz) {
  return tick_hz * (1.0f / LOC_NOMINAL_TICK_HZ);
}
static inline void loc_rhythm_set_knobs(LocRhythm* r, float rate, float randomness) {
  if (!(randomness > 0.0f)) randomness = 0.0f;                 // also catches NaN
  if (randomness > LOC_KNOB_MAX) randomness = LOC_KNOB_MAX;
  r->s = randomness;
  float tempo = rate / loc_rhythm_gain(randomness);
  if (!(tempo > LOC_TEMPO_MIN)) tempo = LOC_TEMPO_MIN;         // also catches NaN
  r->tempo = tempo;
}

// ===== the update =====================================================================

// The core recurrence, with the four noise draws INJECTED. Split out so the host self-test can
// drive it from the same noise sequence as the Python reference and compare state for state —
// which is what makes "the C is the model" a gate rather than a comment. The device calls
// loc_rhythm_next_s() instead.
//
// n[0..LOC_N_COMPONENTS-1] drive the OU components, n[LOC_N_COMPONENTS] the white term. When
// no time has passed (only the very first call, before any interval exists) the components are
// left alone and their noise is unused — matching the reference, which draws inside that
// branch.
static inline float loc_rhythm_step(LocRhythm* r, const float* n) {
  float dt = r->pending_dt;
  float m = 0.0f;
  for (int i = 0; i < LOC_N_COMPONENTS; i++) {
    if (dt > 0.0f) {
      float a = expf(-(dt * r->tempo) / LOC_TAU_S[i]);
      r->x[i] = a * r->x[i] + LOC_SD[i] * sqrtf(1.0f - a * a) * n[i];
    }
    m += r->x[i];
  }
  r->m = r->s * (r->offset + m);
  float z = r->m + r->s * LOC_SD_WHITE * n[LOC_N_COMPONENTS];
  float ipi = loc_rhythm_interval_for_score(z) / r->tempo;
  r->pending_dt = ipi;   // the interval just emitted IS the next ageing step
  return ipi;
}

// Seconds until the next pulse, given how long it has actually been since the last one.
// THE ONLINE CALL.
//
// `elapsed_s` IS THE WHOLE POINT, and it is why this signature differs from the reference's.
// The state relaxes in wall-clock time, so the rhythm must be told about every second that
// passed — including the seconds it did not control. The reference offers an `advance(dt)`
// that accumulates on top of the interval it last returned, which suits a simulation loop
// where nothing interrupts. This device is interrupted constantly: a trial preempts the
// localization train PART-WAY through an interval, the marker and volley take their own time,
// and the throttle can leave the train off for minutes. Accumulating on top of an interval
// that was never realised would book time that did not pass and miss time that did.
//
// So the caller — which owns an exact 50 kHz sample clock — passes the true elapsed samples
// since the last localization onset, and this assigns rather than accumulates. One mechanism,
// one call site, and "the state ages by real time" becomes structural instead of depending on
// someone remembering to call advance() after every playback. Pass 0 for the first pulse of a
// power-on, where there is no previous one.
static inline float loc_rhythm_next_s(LocRhythm* r, float elapsed_s) {
  r->pending_dt = (elapsed_s > 0.0f) ? elapsed_s : 0.0f;
  float n[LOC_N_COMPONENTS + 1];
  for (int i = 0; i <= LOC_N_COMPONENTS; i++) n[i] = loc_rhythm_noise(r);
  return loc_rhythm_step(r, n);
}

// Interval in whole samples, clamped to the scheduler's refractory.
//
// The clamp is a SAFETY floor for locgen's no-overlap invariant, not part of the model: at
// tempo 1 the table's fastest interval is 27.9 ms (1394 samples at 50 kHz) against a
// 250-sample refractory, so it only ever engages if the rate knob is driven far past a real
// eel. It is applied to the RETURNED value only — `pending_dt` keeps the model's own unclamped
// interval, so clamping can never feed back into the state and bend the rhythm.
static inline uint32_t loc_rhythm_next_ipi_samp(LocRhythm* r, uint32_t elapsed_samp,
                                                uint32_t sample_rate_hz,
                                                uint32_t refractory_samp) {
  float inv = 1.0f / (float)sample_rate_hz;
  float samp = loc_rhythm_next_s(r, (float)elapsed_samp * inv) * (float)sample_rate_hz;
  if (!(samp > (float)refractory_samp)) return refractory_samp;   // also catches NaN
  return (uint32_t)(samp + 0.5f);
}

// A fresh fish at power-on, warmed up to its running distribution.
//
// BURN-IN IS NOT OPTIONAL. The components' stationary distribution is defined in continuous
// time while the model is observed at its own pulse times, which over-visits the fast side —
// the same asymmetry the interval table is calibrated for. A cold state therefore starts
// noticeably SLOW and takes tens of pulses to settle. LOC_BURN_IN discarded intervals cost
// microseconds once, at boot, and remove the transient.
//
// The drawn `offset` is individual variation, reproduced on purpose: each power-on sits at its
// own tempo, roughly +-25 % around the knob setting, and stays there — the ~1 h component and
// the per-deployment offset have not mixed inside any single run of a real recording either.
static inline void loc_rhythm_init(LocRhythm* r, uint32_t seed, float rate, float randomness) {
  r->rng        = seed ? seed : 0x1234567u;   // xorshift32 is absorbing at 0
  r->have_spare = 0;
  r->spare      = 0.0f;
  r->pending_dt = 0.0f;
  r->m          = 0.0f;
  loc_rhythm_set_knobs(r, rate, randomness);
  r->offset = LOC_SD_OFFSET * loc_rhythm_noise(r);
  for (int i = 0; i < LOC_N_COMPONENTS; i++) r->x[i] = LOC_SD[i] * loc_rhythm_noise(r);
  // The burn-in feeds each interval back as the next ageing step — the reference's own
  // `new_state`, which starts from pending_dt == 0 and lets the model run itself forward.
  float dt = 0.0f;
  for (int i = 0; i < LOC_BURN_IN; i++) dt = loc_rhythm_next_s(r, dt);
}
