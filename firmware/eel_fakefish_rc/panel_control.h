// panel_control.h — L3 control surface: the RC unit's physical panel + LED feedback.
//
// A bench input source OR-ed with the RC layer (rc_control.h). The unit is bench-testable with
// NO transmitter: the panel alone drives the same localization / volley / sham state machine.
//
// THREE momentary buttons (mirroring the RC actions):
//   LOC toggle (pin 9)  — toggle the localization train on/off
//   VOLLEY     (pin 10) — fire one volley (marker + discharge)
//   SHAM       (pin 11) — fire one sham   (marker + LED, no water output)
//
// This file also owns the LED FEEDBACK vocabulary (per-pulse flash, the distinct sham pattern,
// the no-signal blink) — the LED is a panel output, and what it MEANS is surface-specific. The
// LED_PIN itself is L1 (src/eel_core/config.h): every device in the repo has one.
//
// The pure logic (millis() debounce + press edge-detect) sits ABOVE the `#ifdef ARDUINO` line so
// it host-tests on a PC (host_test/panel_control_selftest.cpp).
//
// The bench defaults for rate / jitter / amplitude (PANEL_RATE_HZ, PANEL_CV, PANEL_VOLLEY_AMP)
// are NOT here — they are playback constants, generated into src/eel_core/stim_levels.h from
// shared/stim_constants.json.
#pragma once
#include <stdint.h>
#include "src/eel_core/config.h"   // LED_PIN (L1: every device has an indicator LED)

// ===== Panel wiring ========================================================
// Each button: one leg to its pin (INPUT_PULLUP), the other to GND -> active-low.
// Pins 9-11 avoid the DRV pins (0-3), the RC pins (4-7), the LED (13) and A0 (== digital 14).
#define PANEL_LOC_PIN     9                // toggle localization
#define PANEL_VOLLEY_PIN  10               // fire one volley
#define PANEL_SHAM_PIN    11               // fire one sham
#define PANEL_DEBOUNCE_MS 25u

// ===== LED feedback ========================================================
// Sample-unit durations are counted by the sample-clock ISR; ms ones by loop()'s millis().
#define RC_LED_FLASH_SAMP 300u             // 6 ms per-pulse flash @ 50 kHz
#define SHAM_LED_BLINKS   3u               // distinct "sham fired" pattern (no water output)
#define SHAM_LED_ON_SAMP  6000u            // 120 ms on
#define SHAM_LED_OFF_SAMP 6000u            // 120 ms off
#define NOSIG_LED_PERIOD_MS 1000u          // "no RC signal" double-blink period
#define NOSIG_BLINK_MS      60u            // each blink length within the period
#define NOSIG_BLINK_SPACING_MS 150u        // start of the 2nd blink within the period

// ===== Debounce (pure; host-tested) =======================================
// millis()-based debounce + falling-edge (press) detector. `raw_high` is the pin level read
// active-low-inverted (true == released/HIGH, false == pressed/LOW). Returns true exactly once
// per debounced press. Pure: `now_ms` is injected.
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

// ===== Arduino glue (GPIO / millis) =======================================
#ifdef ARDUINO
#include <Arduino.h>

static DebounceState g_panel_loc, g_panel_volley, g_panel_sham;

// Configure the panel button pins + the LED. Call once from setup().
static inline void panel_begin() {
  pinMode(PANEL_LOC_PIN,    INPUT_PULLUP);
  pinMode(PANEL_VOLLEY_PIN, INPUT_PULLUP);
  pinMode(PANEL_SHAM_PIN,   INPUT_PULLUP);
  debounce_init(&g_panel_loc);
  debounce_init(&g_panel_volley);
  debounce_init(&g_panel_sham);
  pinMode(LED_PIN, OUTPUT);
  digitalWriteFast(LED_PIN, LOW);
}

// Tick all three debouncers. *_fell are set to 1 on a debounced press. Ticks every call (never
// early-returns) so a held button can't spurious-retrigger.
static inline void panel_poll(int* loc_fell, int* volley_fell, int* sham_fell) {
  const uint32_t now = millis();
  bool loc_hi    = (digitalRead(PANEL_LOC_PIN)    != LOW);
  bool volley_hi = (digitalRead(PANEL_VOLLEY_PIN) != LOW);
  bool sham_hi   = (digitalRead(PANEL_SHAM_PIN)   != LOW);
  *loc_fell    = debounce_fell(&g_panel_loc,    loc_hi,    now, PANEL_DEBOUNCE_MS) ? 1 : 0;
  *volley_fell = debounce_fell(&g_panel_volley, volley_hi, now, PANEL_DEBOUNCE_MS) ? 1 : 0;
  *sham_fell   = debounce_fell(&g_panel_sham,   sham_hi,   now, PANEL_DEBOUNCE_MS) ? 1 : 0;
}

#endif  // ARDUINO
