// button_control.h — L3 control surface: the hand-held SD player's SIX program buttons.
//
// The fakefish is a HAND-HELD field stimulator — the operator holds it and only the
// electrodes go in the water. This file is what the operator touches: six dedicated
// program buttons and an indicator LED. A press maps to one SD-card directory; the driver
// lists that directory, picks a WAV at random, and streams it (see src/eel_core/sd_player.h).
// All stimulus generation is offline Python (src/fakefish/build_sd_card.py) — a stimulus is
// simply a pre-rendered WAV on the card.
//
// The pure logic (debounce edge-detect, button->directory map) sits ABOVE the `#ifdef ARDUINO`
// line so it compiles and is unit-tested on a PC (host_test/button_control_selftest.cpp). Only
// the GPIO / millis() glue is Arduino-specific.
#pragma once
#include <stdint.h>
#include "src/eel_core/config.h"   // LED_PIN (L1: every device has an indicator LED)

// ===== Wiring (edit to your rig) ==========================================
// SIX program buttons, each: one leg to its pin (INPUT_PULLUP), the other to GND ->
// active-low. One press plays that program ONCE, start to finish; a press while a playback
// runs is IGNORED (playbacks are uninterruptible).
//
// Pins 5-10 avoid the four DRV8871 output pins (0,1 = IN2 PWM; 2,3 = IN1 held HIGH) and A0
// (== digital 14, randomSeed). The 4.1's SDIO card slot is on dedicated pads, so it does not
// collide. The pin map is asserted against the real HAL macros in the host self-test.
#define BTN_A_PIN  5    // A: calibration    -> /A
#define BTN_B_PIN  6    // B: localization   -> /B
#define BTN_C_PIN  7    // C: volley         -> /C
#define BTN_D_PIN  8    // D: loc -> volley  -> /D
#define BTN_E_PIN  9    // E: spare/unassigned -> /E (empty dir == self-mute)
#define BTN_F_PIN  10   // F: song           -> /F
#define N_BUTTONS  6
#define BTN_NONE   (-1)
#define BTN_DEBOUNCE_MS  25u

// The SD directory each button plays from (index == button). One directory per button; the
// driver picks a WAV inside it at random per press. To reassign a button, move WAVs between
// directories on the card — no reflash. A missing/empty directory simply self-mutes.
static const char* const BTN_DIRS[N_BUTTONS] = { "/A", "/B", "/C", "/D", "/E", "/F" };

// ===== Debounce (pure; host-tested) =======================================
// millis()-based debounce + falling-edge (press) detector. `raw_high` is the pin level read
// active-low-inverted (true == released/HIGH, false == pressed/LOW). Returns true exactly
// once per debounced press. Pure: `now_ms` is injected.
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

static DebounceState g_btn[N_BUTTONS];
static const uint8_t BTN_PINS[N_BUTTONS] = {
  BTN_A_PIN, BTN_B_PIN, BTN_C_PIN, BTN_D_PIN, BTN_E_PIN, BTN_F_PIN
};

// Configure the button + LED pins. Call once from setup().
static inline void button_control_begin() {
  for (uint8_t i = 0; i < N_BUTTONS; i++) {
    pinMode(BTN_PINS[i], INPUT_PULLUP);
    debounce_init(&g_btn[i]);
  }
  pinMode(LED_PIN, OUTPUT);
  digitalWriteFast(LED_PIN, LOW);
}

// The button that just fired, or BTN_NONE. Ticks EVERY button's debounce on every call (it
// never early-returns), so a held button cannot spurious-retrigger when a playback ends and
// a press during a playback is consumed and ignored. Lowest index wins a same-pass tie.
static inline int button_control_fired() {
  const uint32_t now = millis();
  int fired = BTN_NONE;
  for (uint8_t i = 0; i < N_BUTTONS; i++) {
    bool raw_high = (digitalRead(BTN_PINS[i]) != LOW);   // active-low: HIGH == released
    if (debounce_fell(&g_btn[i], raw_high, now, BTN_DEBOUNCE_MS) && fired == BTN_NONE)
      fired = (int)i;
  }
  return fired;
}

#endif  // ARDUINO
