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
// engine is an overlap-add mixer over a ring buffer, not a fire-and-wait loop.
// It is the C twin of tools/export_teensy_stimuli.py :: reconstruct_item and is
// validated sample-for-sample against it (firmware/host_test).
#pragma once
#include <stdint.h>
#include "eel_stimuli.h"

typedef struct {
  const StimItem* item;
  uint16_t k;               // index of the next pulse to add
  uint32_t next_onset;      // sample index at which pulse k begins
  uint32_t t;               // current output sample index
  uint32_t last_onset;      // onset sample of the most recent pulse added
  uint32_t max_samples;     // stop after this many output samples (0 = play item once)
  uint8_t  loop;            // 1 = restart the item when it ends before max_samples
  float    scale;           // global amplitude * polarity (constant per playback)
  float    ring[EOD_HV_LEN];// overlap-add accumulator (float; FPU on both Teensys)
  uint16_t ring_pos;        // current head of the ring
  uint8_t  active;          // 1 while playing, 0 once finished
} EelPlayer;

// Start playing STIM_ITEMS[item_index] at a global amplitude (0..1) and constant
// polarity (+1 / -1). Amplitude and polarity are firmware choices, not data.
void eel_player_start(EelPlayer* p, uint8_t item_index, float amplitude, int8_t polarity);

// As above, but from a StimItem pointer rather than a table index. Lets a driver
// play a runtime-synthesised item (e.g. the constant-IPI calibration train in
// eel_control.h) through the same validated overlap-add engine. eel_player_start()
// is a thin wrapper: eel_player_start_item(p, &STIM_ITEMS[i], ...). Plays the item
// once, in full (no window, no loop).
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
