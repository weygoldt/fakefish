// Host harness for the device-agnostic playback engine. TWO MODES:
//
//   <item_index> [amplitude] [polarity]   DUMPER — stream one item's samples to stdout,
//                                         for diffing against the Python reference
//                                         (src/fakefish/export_teensy_stimuli.py ::
//                                         reconstruct_item). Prints samples, never "OK".
//   --verify                              ASSERTION SUITE — prints "OK" or dies.
//
// WHY THE ASSERTION SUITE EXISTS. The dumper alone cannot fail a gate: check.sh can only
// check that it builds, exits 0 and produces output, so a change that silently altered
// every sample would sail through and only a human running a before/after diff would catch
// it. eel_player renders the pulse timing of a blinded field experiment; that is too thin a
// net. --verify runs the engine against an ORACLE — the ring-buffer overlap-add engine that
// eel_player used until it was rewritten as a per-tick sum — and requires the two to agree
// sample-for-sample over the whole library, both polarities, several amplitudes, and the
// windowed/looping paths the dumper never reaches.
//
// The oracle is frozen on purpose. It is a second, independent implementation of the same
// contract, exactly as reconstruct_item is on the Python side, and it is the reason the
// rewrite could be proven bit-identical rather than merely close. Do not "refactor" it to
// share code with eel_player.cpp — that would make it agree by construction and prove
// nothing.
//
// ONE THING THIS SUITE CANNOT SEE. It runs on a PC, where `acc += e * a` is a multiply then
// an add. The Teensy contracts the same expression to a fused vfma.f32 with a single
// rounding, and under FMA the sum of two taps depends on which one is added first. Reversing
// the summation order in eel_player.cpp therefore passes here with every sample identical
// while moving 32 of the library's samples by 1 LSB on the real device. A green run of this
// suite is not a licence to reorder that loop — see the note above the summation there.
//
//   g++ -I firmware/eel_core firmware/eel_core/eel_player.cpp
//       firmware/eel_core/eel_stimuli.cpp
//       firmware/eel_core/host_test/eel_player_selftest.cpp -lm -o /tmp/selftest
//   /tmp/selftest --verify
//   /tmp/selftest <item_index> [amplitude] [polarity]
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include "eel_player.h"

// ===== Oracle: the ring-buffer engine eel_player replaced ==================
// Verbatim algorithm of the pre-rewrite eel_player: stamp the whole EOD into a ring
// buffer at each onset, then emit and clear one slot per tick.

typedef struct {
  const StimItem* item;
  uint16_t k;
  uint32_t next_onset;
  uint32_t t;
  uint32_t last_onset;
  uint32_t max_samples;
  uint8_t  loop;
  float    scale;
  float    ring[EOD_HV_LEN];
  uint16_t ring_pos;
  uint8_t  active;
} Oracle;

static inline int16_t oracle_clamp16(float v) {
  long r = lrintf(v);
  if (r > 32767) return 32767;
  if (r < -32767) return -32767;
  return (int16_t)r;
}

static void oracle_start(Oracle* p, const StimItem* item, float amplitude, int8_t polarity,
                         uint32_t max_samples, uint8_t loop) {
  p->item = item;
  p->k = 0;
  p->t = 0;
  p->next_onset = (item->n > 0) ? item->ipi_samp[0] : 0;
  p->last_onset = 0;
  p->max_samples = max_samples;
  p->loop = loop;
  p->scale = amplitude * (float)polarity;
  p->ring_pos = 0;
  for (uint16_t i = 0; i < EOD_HV_LEN; i++) p->ring[i] = 0.0f;
  p->active = (item->n > 0) ? 1 : 0;
}

static int oracle_next(Oracle* p, int16_t* out) {
  if (!p->active) return 0;
  const StimItem* it = p->item;

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

  int16_t s = oracle_clamp16(p->ring[p->ring_pos]);
  p->ring[p->ring_pos] = 0.0f;
  p->ring_pos++;
  if (p->ring_pos >= EOD_HV_LEN) p->ring_pos = 0;
  p->t++;

  if (p->max_samples && p->t >= p->max_samples) {
    p->active = 0;
  } else if (p->k >= it->n && p->t > p->last_onset + EOD_HV_LEN) {
    if (p->loop && it->n > 1) {
      p->next_onset = p->last_onset + it->ipi_samp[1];
      p->k = 0;
    } else {
      p->active = 0;
    }
  }

  *out = s;
  return 1;
}

// ===== assertions =========================================================

static int g_fail = 0;
static uint8_t g_high_water = 0;   // most pulses ever sounding at once, across every case
static unsigned long g_samples = 0;

#define CHECK(cond, ...)                                                      \
  do {                                                                        \
    if (!(cond)) {                                                            \
      fprintf(stderr, "FAIL %s:%d: ", __FILE__, __LINE__);                     \
      fprintf(stderr, __VA_ARGS__);                                           \
      fprintf(stderr, "\n");                                                  \
      if (++g_fail > 20) { fprintf(stderr, "too many failures\n"); exit(1); } \
      return;                                                                 \
    }                                                                         \
  } while (0)

// Run engine and oracle in lockstep over one playback and require exact agreement.
// Also re-derives the pulse onsets independently from ipi_samp and checks the fields the
// RC ISR reads (player.k, player.last_onset) against them — that surface logs every volley
// pulse as `player.k - 1`, so their meaning is part of the engine's contract, not an
// implementation detail.
static void verify_case(const StimItem* it, int idx, float amp, int pol,
                        uint32_t max_samples, uint8_t loop) {
  const char* what = loop ? "looped" : (max_samples ? "windowed" : "once");
  EelPlayer p;
  Oracle o;
  eel_player_start_windowed(&p, it, amp, (int8_t)pol, max_samples, loop);
  oracle_start(&o, it, amp, (int8_t)pol, max_samples, loop);

  uint32_t expect_onset = 0;   // independent cumulative sum of ipi_samp
  uint16_t expect_k = 0;
  uint32_t last_seen_onset = 0xFFFFFFFFu;
  uint32_t t = 0;
  // Onsets whose pulses are still sounding, tracked here so the expected count comes from
  // OUTSIDE the engine. Note it is no use asserting p.n_act <= EEL_MAX_ACTIVE_PULSES: the
  // engine's admit guard makes that true by construction even while it drops pulses on the
  // floor. 8 slots is four times what the library can reach.
  uint32_t sounding[8];
  unsigned n_sounding = 0;

  // Runaway guard, derived from the item rather than a wall-clock constant. It used to be
  // a flat 60 s with the note "no case here is anywhere near that" — which stopped being
  // true when the localization items started carrying the fitted rhythm's multi-second
  // silences and grew past 80 s. A bound computed from the item's own cumulative IPI
  // cannot go stale the next time the library changes shape.
  uint32_t item_span = EOD_HV_LEN;
  for (uint16_t i = 0; i < it->n; i++) item_span += it->ipi_samp[i];
  const uint32_t runaway_cap =
      (max_samples ? max_samples : item_span) * 2u + 4u * STIM_SAMPLE_RATE_HZ;

  for (;;) {
    int16_t a = 0, b = 0;
    int ra = eel_player_next(&p, &a);
    int rb = oracle_next(&o, &b);
    CHECK(ra == rb, "item %d %s amp=%g pol=%d: engine stopped at t=%u, oracle at t=%u",
          idx, what, (double)amp, pol, (unsigned)p.t, (unsigned)o.t);
    if (!ra) break;
    if (p.n_act > g_high_water) g_high_water = p.n_act;

    // The RC ISR detects an onset as "last_onset changed" and logs the pulse as k-1.
    if (p.last_onset != last_seen_onset) {
      last_seen_onset = p.last_onset;
      CHECK(n_sounding < sizeof(sounding) / sizeof(sounding[0]),
            "item %d %s: more than %u pulses sound at once at t=%u — the test's own tracker "
            "is too small, which means the item is far tighter than any engine here allows",
            idx, what, (unsigned)(sizeof(sounding) / sizeof(sounding[0])), (unsigned)t);
      sounding[n_sounding++] = p.last_onset;
      if (!loop) {   // a loop restart replays indices, so the running sum no longer applies
        CHECK(p.last_onset == expect_onset,
              "item %d %s: onset %u reported at %u, cumulative ipi_samp says %u",
              idx, what, (unsigned)expect_k, (unsigned)p.last_onset, (unsigned)expect_onset);
        CHECK(p.k > 0 && (uint16_t)(p.k - 1) == expect_k,
              "item %d %s: k-1 == %u at onset %u, expected %u",
              idx, what, (unsigned)(p.k - 1), (unsigned)p.last_onset, (unsigned)expect_k);
        expect_k++;
        if (expect_k < it->n) expect_onset += it->ipi_samp[expect_k];
      }
    }
    // How many pulses SHOULD be sounding on this tick, derived from the onsets alone: a
    // pulse contributes for exactly EOD_HV_LEN samples from its onset, so retiring the
    // expired ones leaves the expected count as the list's length. Checked before the
    // samples are compared, because it names the fault ("a pulse was dropped") where a
    // sample mismatch only reports the symptom.
    while (n_sounding > 0 && t - sounding[0] >= (uint32_t)EOD_HV_LEN) {
      for (unsigned i = 1; i < n_sounding; i++) sounding[i - 1] = sounding[i];
      n_sounding--;
    }
    const unsigned expect_sounding = n_sounding;
    CHECK(expect_sounding <= EEL_MAX_ACTIVE_PULSES,
          "item %d %s: %u pulses sound together at t=%u, but the engine has %u slot(s) — "
          "this item's onsets are tighter than EEL_PLAYER_MIN_IPI_SAMP",
          idx, what, expect_sounding, (unsigned)t, (unsigned)EEL_MAX_ACTIVE_PULSES);
    CHECK(p.n_act == expect_sounding,
          "item %d %s: engine has %u pulse(s) sounding at t=%u, the onsets say %u — "
          "a pulse was DROPPED or retired early",
          idx, what, (unsigned)p.n_act, (unsigned)t, expect_sounding);

    CHECK(a == b, "item %d %s amp=%g pol=%d: sample mismatch at t=%u: engine %d, oracle %d",
          idx, what, (double)amp, pol, (unsigned)t, a, b);

    // Engine and oracle must also agree on the bookkeeping, not just the samples.
    CHECK(p.k == o.k && p.last_onset == o.last_onset,
          "item %d %s: bookkeeping diverged at t=%u (k %u/%u, last_onset %u/%u)",
          idx, what, (unsigned)t, (unsigned)p.k, (unsigned)o.k,
          (unsigned)p.last_onset, (unsigned)o.last_onset);
    t++;
    g_samples++;
    if (t > runaway_cap) {
      CHECK(0, "item %d %s: playback did not terminate within %u samples (%.1f s)",
            idx, what, (unsigned)runaway_cap, (double)runaway_cap / STIM_SAMPLE_RATE_HZ);
    }
  }
  if (!loop && max_samples == 0) {
    CHECK(p.k == it->n, "item %d %s: ended after %u of %u pulses",
          idx, what, (unsigned)p.k, (unsigned)it->n);
  }
}

static int run_verify(void) {
  // Amplitudes: full scale, the two levels the devices actually use, and one that is not a
  // tidy binary fraction (so a rounding difference would show up rather than cancel).
  static const float AMPS[] = {1.0f, 0.9f, 0.45f, 0.3137f};
  static const int POLS[] = {1, -1};

  // The whole library, both polarities, every amplitude — played once, unbounded.
  for (int i = 0; i < N_STIM_ITEMS; i++)
    for (unsigned ai = 0; ai < sizeof(AMPS) / sizeof(AMPS[0]); ai++)
      for (unsigned pi = 0; pi < 2; pi++)
        verify_case(&STIM_ITEMS[i], i, AMPS[ai], POLS[pi], 0, 0);

  // The windowed and looping paths, which the dumper never reaches: they are how the SD
  // card renders localization (truncate a long train, or loop a short one to fill N
  // seconds). Exercise a truncation that lands mid-pulse, one that lands mid-gap, and a
  // loop that has to re-seam several times.
  int loc = -1, volley = -1;
  for (int i = 0; i < N_STIM_ITEMS; i++) {
    if (loc < 0 && STIM_ITEMS[i].kind == STIM_LOCALIZATION) loc = i;
    if (volley < 0 && STIM_ITEMS[i].kind == STIM_SYNTH_VOLLEY) volley = i;
  }
  if (loc < 0 || volley < 0) {
    fprintf(stderr, "FAIL: library has no localization and/or synth-volley item\n");
    return 1;
  }
  static const uint32_t WINDOWS[] = {1u, 65u, 131u, 132u, 1000u, 50000u, 250000u};
  for (unsigned wi = 0; wi < sizeof(WINDOWS) / sizeof(WINDOWS[0]); wi++) {
    for (unsigned pi = 0; pi < 2; pi++) {
      verify_case(&STIM_ITEMS[loc], loc, 0.45f, POLS[pi], WINDOWS[wi], 0);
      verify_case(&STIM_ITEMS[loc], loc, 0.45f, POLS[pi], WINDOWS[wi], 1);
      verify_case(&STIM_ITEMS[volley], volley, 0.9f, POLS[pi], WINDOWS[wi], 0);
    }
  }

  if (g_fail) {
    fprintf(stderr, "%d failure(s)\n", g_fail);
    return 1;
  }
  // The slot array is sized ceil(EOD_HV_LEN / EEL_PLAYER_MIN_IPI_SAMP). If the library never
  // actually reaches that, the engine is carrying a slot it does not need — report it rather
  // than let it rot, but do not fail: a runtime-built item is allowed to be tighter than
  // anything exported.
  if (g_high_water != EEL_MAX_ACTIVE_PULSES)
    fprintf(stderr, "note: %u slot(s) used of %u across %lu samples\n",
            (unsigned)g_high_water, (unsigned)EEL_MAX_ACTIVE_PULSES, g_samples);
  printf("OK\n");
  return 0;
}

int main(int argc, char** argv) {
  if (argc > 1 && strcmp(argv[1], "--verify") == 0) return run_verify();

  int idx = argc > 1 ? atoi(argv[1]) : 0;
  float amp = argc > 2 ? (float)atof(argv[2]) : 1.0f;
  int pol = argc > 3 ? atoi(argv[3]) : 1;
  if (idx < 0 || idx >= N_STIM_ITEMS) { fprintf(stderr, "bad index\n"); return 1; }
  EelPlayer p;
  eel_player_start(&p, (uint8_t)idx, amp, (int8_t)pol);
  int16_t s;
  while (eel_player_next(&p, &s)) printf("%d\n", s);
  return 0;
}
