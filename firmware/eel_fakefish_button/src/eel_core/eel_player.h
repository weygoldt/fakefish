// Shared, device-agnostic playback engine for the eel stimulus library.
//
// This is the device-agnostic core of the playback path. It knows nothing about
// DACs, PWM, or polarity wiring — it turns a StimItem (from eel_stimuli.h) into a
// stream of signed int16 samples at STIM_SAMPLE_RATE_HZ. The driver calls
// eel_player_next() from its 50 kHz sample clock and hands the sample to its own
// out_write() (see eel_fakefish.ino). The seam is kept board-independent on
// purpose, so a second board's out_write() could drop in without touching this.
//
// Because the EOD waveform (EOD_HV_LEN samples) is LONGER than the shortest
// inter-pulse interval, consecutive pulses overlap and must be SUMMED — the
// engine is an additive mixer, not a fire-and-wait loop. It is the C twin of
// tools/export_teensy_stimuli.py :: reconstruct_item and is validated
// sample-for-sample against it (firmware/eel_core/host_test).
//
// HOW THE SUM IS SPREAD, AND WHY IT MATTERS. The obvious way to mix is to stamp
// the whole EOD into a ring buffer at each onset and then read one slot per tick.
// That is what this engine used to do, and it puts EOD_HV_LEN (131) float
// multiply-accumulates into the ONE tick where a pulse begins, leaving every other
// tick nearly free. Measured on real Teensyduino codegen (arm-none-eabi-g++ -O2),
// that burst is ~2100 instructions: 3.5-5.6 us on a Teensy 4.1's Cortex-M7 at
// 600 MHz — survivable inside the 20 us tick — but 17.5-28 us on a Cortex-M4F at
// 120 MHz, which OVERRUNS the tick period on every single pulse onset before the
// pulse log, the two analogWrite()s and the LED have run at all.
//
// So this engine does the opposite: it keeps a short list of the pulses currently
// SOUNDING (onset phase + amplitude) and sums their taps on EVERY tick, spread evenly,
// with no spike anywhere. Across the whole shipped library 86 % of onsets have one pulse
// sounding and 14 % have two, so the steady per-tick cost is 1-2 taps against the old
// engine's 131-tap instant — roughly what locgen already pays per tick.
//
// It is not even a trade: the per-tap cost FELL from 16 instructions to 11, because a
// phase counter per sounding pulse is cheaper than the ring's wraparound arithmetic
// (cmp/sub/ite/uxth every tap). Per pulse that is 2096 instructions down to 1441; per
// tick, worst case, 2096 down to 22. It also drops `float ring[131]` (524 B) to two
// 8-byte slots and removes a SECOND ISR spike: eel_player_start_*() no longer has to zero
// 131 floats, which mattered because the RC surface starts a marker and a volley from
// inside its sample ISR.
#pragma once
#include <stdint.h>
#include "eel_stimuli.h"

// ---- Engine capability: how tightly pulses may be spaced ------------------
// EEL_PLAYER_MIN_IPI_SAMP is the tightest onset spacing this engine will render.
// It fixes the slot count below, and therefore the ISR's worst-case per-tick cost.
//
// The shipped library's floor is SYNTH_MIN_IPI_SAMP = 100 samples (2.00 ms, set by
// the EOD's energy width — see src/fakefish/synthetic_volleys.py), and the tightest
// real volley is 132. This constant is deliberately pinned to that floor rather than
// derived from the generated library, so that tightening the library later cannot
// silently widen the ISR's per-tick work: it fails a test instead, and someone
// decides. The enforcement is in Python, because C cannot see the item tables at
// compile time — tests/test_export_teensy_stimuli.py checks every committed item's
// minimum gap against this value, parsing it straight out of this header.
//
// A RUNTIME-BUILT StimItem (one assembled in a sketch rather than exported, e.g. the
// RC surface's coded pulse marker) is NOT covered by that test. Every such item must
// static_assert its own spacing against EEL_PLAYER_MIN_IPI_SAMP where it is defined —
// see eel_fakefish_rc.ino. This is the same idiom as locgen.h's assertion that
// LOC_REFRACTORY_SAMP exceeds EOD_HV_LEN.
#define EEL_PLAYER_MIN_IPI_SAMP  100u

// How many pulses can be sounding at once: ceil(EOD_HV_LEN / min IPI). Exact, not a
// safety margin — the engine retires finished pulses BEFORE admitting new ones, so a
// pulse that ends on the tick another begins hands its slot straight over.
#define EEL_MAX_ACTIVE_PULSES \
  ((uint8_t)((EOD_HV_LEN + EEL_PLAYER_MIN_IPI_SAMP - 1u) / EEL_PLAYER_MIN_IPI_SAMP))

static_assert(EEL_PLAYER_MIN_IPI_SAMP > 0u,
              "EEL_PLAYER_MIN_IPI_SAMP is a divisor and sizes the slot array");
static_assert((EOD_HV_LEN + EEL_PLAYER_MIN_IPI_SAMP - 1u) / EEL_PLAYER_MIN_IPI_SAMP
                  == (unsigned)EEL_MAX_ACTIVE_PULSES,
              "slot count does not fit uint8_t — EEL_PLAYER_MIN_IPI_SAMP is far too small");

// One pulse that is currently sounding. `phase` counts samples since its onset, so
// it indexes EOD_HV directly; the pulse retires when phase reaches EOD_HV_LEN. `amp`
// is the whole scale for this pulse (global amplitude * polarity * the item's
// per-pulse envelope), resolved once at onset and then fixed — a mid-pulse amplitude
// change never alters a pulse already in flight.
typedef struct {
  uint16_t phase;
  float    amp;
} EelActivePulse;

typedef struct {
  const StimItem* item;
  uint16_t k;               // index of the next pulse to add
  uint32_t next_onset;      // sample index at which pulse k begins
  uint32_t t;               // current output sample index
  uint32_t last_onset;      // onset sample of the most recent pulse added
  uint32_t max_samples;     // stop after this many output samples (0 = play item once)
  uint8_t  loop;            // 1 = restart the item when it ends before max_samples
  float    scale;           // global amplitude * polarity (constant per playback)
  // Pulses currently sounding, OLDEST FIRST — kept compacted at the front, appended at
  // n_act, retired from index 0. THE ORDER IS LOAD-BEARING ON THE DEVICE, and the host
  // self-test cannot see it: see the note above the summation in eel_player.cpp before
  // touching this.
  EelActivePulse act[EEL_MAX_ACTIVE_PULSES];
  uint8_t  n_act;           // how many of act[] are sounding (NOT the playing flag)
  uint8_t  active;          // 1 while playing, 0 once finished
} EelPlayer;

// Start playing STIM_ITEMS[item_index] at a global amplitude (0..1) and constant
// polarity (+1 / -1). Amplitude and polarity are firmware choices, not data.
void eel_player_start(EelPlayer* p, uint8_t item_index, float amplitude, int8_t polarity);

// As above, but from a StimItem pointer rather than a table index. Lets a driver
// play a runtime-synthesised item (e.g. the constant-IPI calibration train in
// eel_control.h) through the same validated additive engine. eel_player_start()
// is a thin wrapper: eel_player_start_item(p, &STIM_ITEMS[i], ...). Plays the item
// once, in full (no window, no loop). A runtime-built item's onset spacing must be
// at least EEL_PLAYER_MIN_IPI_SAMP — assert that where the item is defined.
void eel_player_start_item(EelPlayer* p, const StimItem* item, float amplitude, int8_t polarity);

// As eel_player_start_item, but BOUNDS playback to max_samples output samples, and if
// loop!=0 RESTARTS the item (one inter-pulse gap after its last pulse) whenever it ends
// before max_samples is reached. Used for localization: play only the first N seconds
// of a long stored train (max_samples < train length), or loop a train to fill N
// seconds (max_samples > train length). max_samples==0 means unbounded (play once).
void eel_player_start_windowed(EelPlayer* p, const StimItem* item, float amplitude,
                               int8_t polarity, uint32_t max_samples, uint8_t loop);

// Pull the next sample. Returns 1 and writes *out (signed int16, clamped to
// +/-32767) while playing; returns 0 once the item has finished.
int eel_player_next(EelPlayer* p, int16_t* out);
