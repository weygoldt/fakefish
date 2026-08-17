// Shared additive-mixer playback engine — see eel_player.h.
#include "eel_player.h"
#include <math.h>

static inline int16_t clamp16(float v) {
  long r = lrintf(v);
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
  p->ring_pos = 0;
  for (uint16_t i = 0; i < EOD_HV_LEN; i++) p->ring[i] = 0.0f;
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

  // Add every pulse whose onset is the current sample (min IPI > 1 sample, so at
  // most one fires per tick, but the loop is safe either way). Overlap-add the
  // scaled waveform into the ring; overlapping pulses accumulate.
  while (p->k < it->n && p->t == p->next_onset) {
    float a = p->scale;
    if (it->rel_amp) a *= (float)it->rel_amp[p->k] * (1.0f / 255.0f);
    for (uint16_t j = 0; j < EOD_HV_LEN; j++) {
      uint16_t idx = p->ring_pos + j;
      if (idx >= EOD_HV_LEN) idx -= EOD_HV_LEN;
      p->ring[idx] += (float)EOD_HV[j] * a;
    }
    p->last_onset = p->t;
    p->k++;
    if (p->k < it->n) p->next_onset += it->ipi_samp[p->k];
  }

  // Emit and clear the current ring slot, then advance.
  int16_t s = clamp16(p->ring[p->ring_pos]);
  p->ring[p->ring_pos] = 0.0f;
  p->ring_pos++;
  if (p->ring_pos >= EOD_HV_LEN) p->ring_pos = 0;
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
