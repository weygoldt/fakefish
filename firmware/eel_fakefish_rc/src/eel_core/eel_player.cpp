// Shared additive-mixer playback engine — see eel_player.h.
#include "eel_player.h"
#include <math.h>

// Round to nearest, ties to even, then saturate to the int16 range.
//
// WHY THIS IS NOT SIMPLY lrintf(). "Round to nearest, ties to even" IS a single ARM
// instruction — VCVTR.S32.F32 converts using the FPSCR rounding mode, whose reset default is
// exactly that and which Teensyduino never changes. But GCC will not emit it for lrintf() at
// any optimisation level, on either core, with or without -fno-math-errno / -fno-trapping-math
// / -funsafe-math-optimizations / -ffast-math (all checked, GCC 11.3). It calls newlib
// instead, and newlib's lrintf is a SOFTWARE routine: for our value range it takes the
// magic-number path, ~35 instructions with two round-trips of the value through the stack.
//
// That call sat on the EMIT path — not once per pulse, but on every one of the 50 000 ticks a
// second — which made it more expensive than the pulse mixing it follows. Hence the two lines
// of assembly. The saturation below is unchanged and still does the real clamping; only the
// float-to-integer conversion moved.
//
// It is equivalent for EVERY float, not merely the ones that occur: newlib's routine and
// VCVTR agree on all 2^32 bit patterns once saturated to +/-32767 (verified exhaustively —
// they take the same rounding for anything in range, and both saturate to the int32 limits,
// with NaN -> 0, outside it). Host builds keep lrintf, so the self-tests are unaffected and
// their samples do not move.
static inline int16_t clamp16(float v) {
#if defined(__ARM_FP)
  int32_t r;
  __asm__("vcvtr.s32.f32 %[f], %[f]\n\t"
          "vmov %[i], %[f]"
          : [i] "=r"(r), [f] "+t"(v));
#else
  const long r = lrintf(v);   // host (self-tests): same rule, no instruction for it
#endif
  if (r > 32767) return 32767;
  if (r < -32767) return -32767;
  return (int16_t)r;
}

void eel_player_start_windowed(EelPlayer* p, const StimItem* item, float amplitude,
                               int8_t polarity, uint32_t max_samples, uint8_t loop) {
  p->item = item;
  p->k = 0;
  p->t = 0;
  p->next_onset = (item->n > 0) ? item->ipi_samp[0] : 0;  // ipi_samp[0] == 0
  p->last_onset = 0;
  p->max_samples = max_samples;
  p->loop = loop;
  p->scale = amplitude * (float)polarity;
  p->n_act = 0;   // nothing sounding yet — O(1), and this runs inside the RC sample ISR
  p->active = (item->n > 0) ? 1 : 0;
}

void eel_player_start_item(EelPlayer* p, const StimItem* item, float amplitude, int8_t polarity) {
  eel_player_start_windowed(p, item, amplitude, polarity, 0, 0);  // once, unbounded
}

void eel_player_start(EelPlayer* p, uint8_t item_index, float amplitude, int8_t polarity) {
  eel_player_start_item(p, &STIM_ITEMS[item_index], amplitude, polarity);
}

int eel_player_next(EelPlayer* p, int16_t* out) {
  if (!p->active) return 0;
  const StimItem* it = p->item;

  // 1. RETIRE BEFORE ADMITTING. Every pulse lasts exactly EOD_HV_LEN samples and
  // onsets never decrease, so phases decrease along act[] and the finished ones are
  // always a PREFIX — drop them from the front and close the gap. Doing this first is
  // what makes EEL_MAX_ACTIVE_PULSES exactly ceil(EOD_HV_LEN / min IPI) instead of one
  // more: a pulse whose last sample was the previous tick hands its slot straight to a
  // pulse starting on this one.
  while (p->n_act > 0 && p->act[0].phase >= EOD_HV_LEN) {
    for (uint8_t i = 1; i < p->n_act; i++) p->act[i - 1] = p->act[i];
    p->n_act--;
  }

  // 2. Admit every pulse whose onset is the current sample (min IPI > 1 sample, so at
  // most one fires per tick, but the loop is safe either way). The per-pulse scale is
  // resolved here, once, and held for the pulse's whole life.
  //
  // The bounds test can only fail if an item was handed to this engine with onsets
  // closer together than EEL_PLAYER_MIN_IPI_SAMP, which the Python gate rules out for
  // library items and a static_assert rules out for each runtime-built item. It is here
  // so that a future violation is a missing pulse rather than a smashed stack — but a
  // dropped pulse is still an artefact that looks like data, so the assertions are the
  // real defence, not this test. The host self-test's oracle comparison would fail on
  // any drop, sample-for-sample.
  while (p->k < it->n && p->t == p->next_onset) {
    float a = p->scale;
    if (it->rel_amp) a *= (float)it->rel_amp[p->k] * (1.0f / 255.0f);
    if (p->n_act < EEL_MAX_ACTIVE_PULSES) {
      p->act[p->n_act].phase = 0;
      p->act[p->n_act].amp   = a;
      p->n_act++;
    }
    p->last_onset = p->t;
    p->k++;
    if (p->k < it->n) p->next_onset += it->ipi_samp[p->k];
  }

  // 3. Sum this tick's taps, OLDEST PULSE FIRST, and advance each phase. Starting from
  // 0.0f and taking the pulses in onset order reproduces the ring-buffer engine's
  // accumulation exactly — it zeroed each slot on emit, then received contributions in
  // onset order as each pulse stamped itself in — and that is what makes this rewrite
  // bit-identical rather than merely close.
  //
  // DO NOT REORDER THIS LOOP ON THE STRENGTH OF A GREEN HOST GATE. On the Teensy this
  // expression contracts to vfma.f32: ONE rounding, with the accumulator's earlier term
  // already rounded and the incoming product not, so the sum is order-dependent even with
  // only two terms. The host g++ build has no FMA — it multiplies then adds, two roundings
  // — and with two terms that is plain commutative, so summing newest-first passes
  // eel_player_selftest --verify with every sample identical. Measured under real FMA
  // semantics across the whole library, at four amplitudes and both polarities: the float
  // sum changes on 30 % of overlapping ticks, and 32 emitted samples move by 1 LSB. The
  // self-test cannot defend this ordering; this comment is the defence.
  //
  // (The same contraction is why the rewrite is safe: old and new both emit exactly one
  // vfma.f32 at -O2 for cortex-m7 and cortex-m4, in the same order, over the same values.)
  float acc = 0.0f;
  for (uint8_t i = 0; i < p->n_act; i++) {
    EelActivePulse* q = &p->act[i];
    acc += (float)EOD_HV[q->phase] * q->amp;
    q->phase++;
  }

  int16_t s = clamp16(acc);
  p->t++;

  if (p->max_samples && p->t >= p->max_samples) {
    // Playback window reached — hard stop (truncate a long train, or the loop target).
    p->active = 0;
  } else if (p->k >= it->n && p->t > p->last_onset + EOD_HV_LEN) {
    // Every pulse has been emitted and the last has fully played out.
    if (p->loop && it->n > 1) {
      // Loop: replay the item, its first pulse one inter-pulse interval after the
      // last one (ipi_samp[1] is a representative gap), so the seam keeps the cadence.
      p->next_onset = p->last_onset + it->ipi_samp[1];
      p->k = 0;
    } else {
      p->active = 0;  // item finished, not looping
    }
  }

  *out = s;
  return 1;
}
