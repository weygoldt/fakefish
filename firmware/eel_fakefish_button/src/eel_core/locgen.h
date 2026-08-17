// locgen.h — L2 sample producer: the LIVE localization pulse scheduler.
//
// Emits ONE EOD_HV pulse (scaled by the latched amplitude + polarity), then silence, until
// the next onset `ipi` samples after this one — i.e. a continuous, rate- and jitter-controlled
// localization train synthesised on-device, sample by sample.
//
// WHY THIS IS NOT THE OVERLAP-ADD ENGINE. Localization runs at <= 20 Hz and the minimum IPI
// (LOC_REFRACTORY_SAMP) is longer than one EOD, so localization pulses can NEVER overlap. That
// invariant (static_assert'd below) is what lets this be a trivial phase counter instead of
// eel_player's overlap-add machinery. A volley, whose pulses DO out-run the EOD length, must
// use eel_player.
//
// PARAMETERS ARE LATCHED BY THE CALLER at each onset, so a mid-pulse control change never
// alters the pulse already in flight: locgen_tick reports `boundary` when an interval
// completes, and the caller writes the next ipi/amp then.
//
// Extracted from fakefish-rc's rc_control.h (the scheduler + its clamp helper) so that a
// future de-novo-synthesis surface can reuse it without dragging in the RC decode layer. Pure:
// no Arduino, no math.h, no floating-point transcendentals — host-testable as-is.
#pragma once
#include <stdint.h>
#include "eel_stimuli.h"   // EOD_HV[] / EOD_HV_LEN — the mean-EOD waveform
#include "stim_levels.h"   // LOC_REFRACTORY_SAMP (generated from shared/stim_constants.json)

// The single-pulse scheduler is only correct while pulses cannot overlap.
static_assert(LOC_REFRACTORY_SAMP > (uint32_t)EOD_HV_LEN,
              "LOC_REFRACTORY_SAMP must exceed EOD_HV_LEN or localization pulses would overlap "
              "— use the overlap-add engine (eel_player) instead of locgen");

// Round half away from zero, saturating to the int16 range. (eel_player.cpp has its own
// lrintf-based clamp16 with subtly different rounding; they are deliberately NOT unified —
// changing either would change the samples a shipped device emits.)
static inline int16_t locgen_clamp16(float v) {
  long r = (long)(v < 0 ? v - 0.5f : v + 0.5f);
  if (r > 32767) return 32767;
  if (r < -32767) return -32767;
  return (int16_t)r;
}

typedef struct {
  uint32_t phase;   // samples since the current pulse's onset (0 == onset sample)
  uint32_t ipi;     // interval to the next onset (samples), latched at onset
  float    amp;     // localization amplitude 0..1, latched at onset
  int8_t   pol;     // +/-1, latched at enable (held for the whole enabled run)
} LocGen;

static inline void locgen_reset(LocGen* g, uint32_t ipi, float amp, int8_t pol) {
  g->phase = 0; g->ipi = ipi ? ipi : LOC_REFRACTORY_SAMP; g->amp = amp; g->pol = pol;
}

// Advance one sample tick. Returns the signed int16 sample for THIS tick. *onset is 1 when this
// sample is a pulse onset (phase 0). *boundary is 1 when the interval just completed (phase
// wrapped) -> the caller latches ipi/amp for the next pulse.
static inline int16_t locgen_tick(LocGen* g, int* onset, int* boundary) {
  *onset = (g->phase == 0) ? 1 : 0;
  int16_t s = (g->phase < (uint32_t)EOD_HV_LEN)
              ? locgen_clamp16((float)EOD_HV[g->phase] * g->amp * (float)g->pol)
              : 0;
  g->phase++;
  *boundary = 0;
  if (g->phase >= g->ipi) { g->phase = 0; *boundary = 1; }
  return s;
}
