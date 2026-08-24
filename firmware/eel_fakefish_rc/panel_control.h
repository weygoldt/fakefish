// panel_control.h — L3 control surface: the RC unit's physical panel + LED feedback.
//
// A bench input source OR-ed with the RC layer (rc_control.h). The unit is bench-testable with
// NO transmitter: the panel alone drives the same localization / trial state machine.
//
// TWO momentary buttons (mirroring the RC actions):
//   LOC toggle (pin 9)  — toggle the localization train on/off
//   TRIAL      (pin 10) — run ONE BLINDED TRIAL; the ISR draws volley / baseline / silence
//
// THE PANEL IS BLIND TOO, since 2026-08-24. It used to have explicit VOLLEY (pin 10) and SHAM
// (pin 11) buttons so the bench stayed deterministic. Both playback devices now run from the
// blinded RC surface, so a deterministic bench path buys nothing and costs the one thing that
// matters: an operator who can force an arm can, even unintentionally, correlate the arm with
// when and where they fired. One button, one blinded draw, on every surface. Pin 11 is retired
// rather than reassigned — an unused input is easier to reason about than a repurposed one, and
// the board is already built.
//
// Scoping a volley on the bench therefore means pressing TRIAL until one comes out (p = 1/3).
// For output-stage work use AMP_DEBUG 1 (config.h), which replaces playback entirely and does
// not involve the trial machinery at all.
//
// This file also owns the LED FEEDBACK vocabulary (per-pulse flash, the arm-blind trial pattern,
// the no-link double-blink, the ready heartbeat, the log-fault inverse blink) — the LED is a
// panel output, and what it MEANS is surface-specific. The LED_PIN itself is L1
// (src/eel_core/config.h): every device in the repo has one. Every duration in that vocabulary
// is sized so a 30 fps camera can resolve it; see the block comment on it below.
//
// The pure logic (millis() debounce + press edge-detect) sits ABOVE the `#ifdef ARDUINO` line so
// it host-tests on a PC (host_test/panel_control_selftest.cpp).
//
// The bench defaults for tempo / randomness / amplitude (PANEL_RATE_HZ, PANEL_RANDOMNESS,
// PANEL_VOLLEY_AMP) are NOT here — they are playback constants, generated into
// src/eel_core/stim_levels.h from shared/stim_constants.json.
#pragma once
#include <stdint.h>
#include "src/eel_core/config.h"   // LED_PIN (L1: every device has an indicator LED)

// ===== Panel wiring ========================================================
// Each button: one leg to its pin (INPUT_PULLUP), the other to GND -> active-low.
// Pins 9-10 avoid the DRV pins (0-3), the RC pins (4-7), the LED (13) and A0 (== digital 14).
// Pin 11 carried the SHAM button until 2026-08-24 and is now unused — see the header note.
#define PANEL_LOC_PIN     9                // toggle localization
#define PANEL_TRIAL_PIN   10               // run one BLINDED trial (the ISR draws the arm)
#define PANEL_DEBOUNCE_MS 25u

// ===== LED feedback — THE WHOLE VOCABULARY ================================
// Sample-unit durations are counted by the sample-clock ISR; ms ones by loop()'s millis().
//
// The LED is the only thing an operator can read from shore, and the only thing a CAMERA can
// read at all. Two rules shape every number below; both were added on 2026-08-22 after a field
// session, and both are enforced by static_assert at the bottom of this block.
//
// RULE 1 — EVERY ON-TIME AND EVERY OFF-TIME IS AT LEAST TWO VIDEO FRAMES.
// A 30 fps camera exposes for at most 33.3 ms per frame and usually far less. A flash shorter
// than one frame period can land entirely in the inter-frame gap and never appear at all; a
// flash of exactly one frame period always appears but only ever PARTIALLY exposed, so how
// bright it looks depends on where it happened to fall. Two frame periods (66.7 ms) is the
// shortest pulse that guarantees at least one fully-exposed frame, which is what turns a flash
// from "maybe visible" into a usable timing mark. LED_MIN_VISIBLE_MS is that floor, rounded up.
// The per-pulse flash was 6 ms until this rule existed — invisible on video by a factor of ten.
//
// RULE 2 — THE LED IS NEVER DARK EXCEPT WHEN THE FISH IS DELIBERATELY QUIET.
// Every state the device can be in has a pattern, so the state is always readable from the
// blinks alone. The ONLY silence is the gap between localization pulses (which the model makes
// multi-second on purpose — see loc_rhythm.h). A dark LED therefore means "running, mid-gap",
// never "wedged". The full vocabulary, in loop()'s arbitration order:
//
//   LOG FAULT   inverse: ~750 ms ON, 250 ms dark, 1 s period   output is SUPPRESSED (no card)
//   NO RC LINK  two 80 ms blinks in a 1 s period               waiting for a transmitter
//   NOT ZEROED  three 80 ms blinks in a 1 s period             link up, waiting for a throttle zero
//   READY       one 80 ms blink in a 2 s period                healthy, armed, localization OFF
//   RUNNING     one 70 ms flash per emitted pulse              the AMBIENT localization train
//   TRIAL       120 ms on / 120 ms off, for the trial's run    a trial is running — WHICH ARM
//                                                              IS NOT SHOWN, deliberately
//
// THE TRIAL PATTERN IS THE SAME FOR ALL THREE ARMS. Until 2026-08-24 a volley flashed per pulse
// (so you could read its rate off the LED) and a sham had its own 3-blink pattern, which meant
// the device announced the arm to anyone on shore AND to anyone scoring the GoPro afterwards.
// A blinded draw that is then displayed is not blinded. The per-pulse RUNNING flash therefore
// belongs to the ambient train only; during a trial the LED says "a trial is running" and
// nothing else. It does leak the trial's DURATION, which is harmless: every arm draws its
// duration from the same distribution, so duration carries no information about the arm.
//
// LOG FAULT is the only INVERTED pattern in the set, deliberately: it is the one condition
// that is actively preventing stimulation, and a blocked device otherwise looks exactly like an
// idle one. It outranks NO RC LINK in loop()'s arbitration for the same reason. There is no
// contention with the ISR's per-pulse flash, because a logging fault means there is no playback
// to flash for.
//
// ONE KNOWN AMBIGUITY, and it is the price of Rule 1: above a tick tempo of ~14 Hz the interval
// is shorter than the flash, so consecutive RUNNING flashes merge into a steady ON. That is
// still distinguishable from LOG FAULT — which has a quarter-second dark notch every second —
// but not from a hypothetical steady-on state, so do not add one. Below ~14 Hz, which is every
// biologically meaningful setting (a real eel ticks at 3.15 Hz), the pulses resolve cleanly.
#define LED_MIN_VISIBLE_MS 70u             // 2 frames at 30 fps (66.7 ms), rounded up

#define RC_LED_FLASH_SAMP 3500u            // 70 ms per-pulse flash @ 50 kHz (was 6 ms: invisible on video)
// The ARM-BLIND trial pattern. No blink COUNT any more: the count was what encoded the arm, and
// it also fixed the sham's length at 3 x 240 ms = 720 ms — longer than 80 % of the library's
// volleys, so the silence arm left a systematically longer hole in the localization train than a
// volley did. The pattern now simply runs for whatever length the trial drew.
#define TRIAL_LED_ON_SAMP  6000u           // 120 ms on
#define TRIAL_LED_OFF_SAMP 6000u           // 120 ms off

// "NO RC LINK": two quick blinks per second. Shown from BOOT, not only after a transmitter has
// been seen once — a unit powered up with its transmitter off used to sit completely dark,
// which is indistinguishable from a dead unit at exactly the moment you most want to know.
// A bench unit driven from the panel alone therefore blinks this whenever it is idle, which is
// honest: there really is no link. Its own activity still overrides it (the ISR owns the LED
// while anything is playing).
#define NOSIG_LED_PERIOD_MS    1000u       // "no RC signal" double-blink period
#define NOSIG_BLINK_MS           80u       // each blink length within the period
#define NOSIG_BLINK_SPACING_MS  240u       // start of the 2nd blink within the period

// "LOGGING FAILED — output is suppressed". INVERTED duty: steady ON with a dark notch.
// The notch is 250 ms rather than the 100 ms it was until 2026-08-22: 100 ms reads as a flicker
// on the water and, at 3 frames, was marginal on video.
#define LOGFAULT_LED_PERIOD_MS 1000u       // full inverse-blink period
#define LOGFAULT_LED_DARK_MS    250u       // the dark notch within that period

// "NOT ZEROED": the link is up but the session zero has not been captured yet, so the RC path
// cannot stimulate (rc_control.h -> RcZero). One more blink than the no-link pattern, deliberately:
// both mean "waiting on the RC side", and the count says which. In normal use this is nearly
// invisible — the transmitter will not transmit until the throttle is at its stop, so the zero is
// captured within ~100 ms of the link coming up. Seeing it persist means something abnormal: a
// receiver running failsafe frames with the transmitter off, or a resting width outside
// RC_ZERO_MIN_US..MAX_US, which is a broken opto path rather than a stick position.
#define NOTZERO_LED_PERIOD_MS  1000u       // "waiting for the throttle zero" triple-blink period
#define NOTZERO_BLINK_MS         80u       // each blink within the period
#define NOTZERO_BLINK_GAP_MS    160u       // blink pitch: blinks start at 0, 160, 320 ms

// "READY": healthy, armed, and localization is OFF (throttle down / panel toggle off). One
// short blink every two seconds — sparse enough to read instantly against the no-link
// double-blink, and the thing that makes "throttle down" visibly different from "wedged".
#define READY_LED_PERIOD_MS    2000u       // slow single-blink heartbeat period
#define READY_BLINK_MS           80u       // the blink within that period

// RULE 1, enforced. Every interval the vocabulary asks a camera to resolve — each ON and each
// OFF — must clear two frames. Checked here rather than in the .ino so the host self-test
// (panel_control_selftest) sees it too.
static_assert(RC_LED_FLASH_SAMP * 1000u / SAMPLE_RATE_HZ >= LED_MIN_VISIBLE_MS,
              "the per-pulse flash is shorter than 2 video frames — a 30 fps camera may miss it");
static_assert(TRIAL_LED_ON_SAMP * 1000u / SAMPLE_RATE_HZ >= LED_MIN_VISIBLE_MS &&
              TRIAL_LED_OFF_SAMP * 1000u / SAMPLE_RATE_HZ >= LED_MIN_VISIBLE_MS,
              "the trial pattern's on/off times are shorter than 2 video frames");
static_assert(NOSIG_BLINK_MS >= LED_MIN_VISIBLE_MS,
              "the no-signal blink is shorter than 2 video frames");
static_assert(NOSIG_BLINK_SPACING_MS >= NOSIG_BLINK_MS + LED_MIN_VISIBLE_MS,
              "the two no-signal blinks are not separated by 2 visible frames of dark");
static_assert(NOSIG_LED_PERIOD_MS >= NOSIG_BLINK_SPACING_MS + NOSIG_BLINK_MS + LED_MIN_VISIBLE_MS,
              "the no-signal period leaves no visible dark gap before it repeats");
static_assert(LOGFAULT_LED_DARK_MS >= LED_MIN_VISIBLE_MS &&
              LOGFAULT_LED_PERIOD_MS - LOGFAULT_LED_DARK_MS >= LED_MIN_VISIBLE_MS,
              "the log-fault notch (or its lit part) is shorter than 2 video frames");
static_assert(READY_BLINK_MS >= LED_MIN_VISIBLE_MS &&
              READY_LED_PERIOD_MS - READY_BLINK_MS >= LED_MIN_VISIBLE_MS,
              "the ready heartbeat is shorter than 2 video frames");
static_assert(NOTZERO_BLINK_MS >= LED_MIN_VISIBLE_MS &&
              NOTZERO_BLINK_GAP_MS >= NOTZERO_BLINK_MS + LED_MIN_VISIBLE_MS,
              "the not-zeroed blinks are shorter than 2 video frames, or too close to separate");
static_assert(NOTZERO_LED_PERIOD_MS >= 2u * NOTZERO_BLINK_GAP_MS + NOTZERO_BLINK_MS + LED_MIN_VISIBLE_MS,
              "the not-zeroed period leaves no visible dark gap before it repeats");
// The patterns must stay TELLABLE APART, not just visible: READY is one blink per period and
// NO LINK is two, so their periods must differ or the eye has only the blink count to go on.
static_assert(READY_LED_PERIOD_MS >= 2u * NOSIG_LED_PERIOD_MS,
              "the ready heartbeat and the no-link blink run at too similar a rate to tell apart");

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

static DebounceState g_panel_loc, g_panel_trial;

// Configure the panel button pins + the LED. Call once from setup().
static inline void panel_begin() {
  pinMode(PANEL_LOC_PIN,   INPUT_PULLUP);
  pinMode(PANEL_TRIAL_PIN, INPUT_PULLUP);
  debounce_init(&g_panel_loc);
  debounce_init(&g_panel_trial);
  pinMode(LED_PIN, OUTPUT);
  digitalWriteFast(LED_PIN, LOW);
}

// Tick both debouncers. *_fell are set to 1 on a debounced press. Ticks every call (never
// early-returns) so a held button can't spurious-retrigger.
//
// *trial_fell means "run ONE BLINDED TRIAL", not "run a volley" — the caller passes
// RC_TRIG_RANDOM and the ISR draws the arm. There is deliberately no way to ask for a
// particular arm from here; see the header note.
static inline void panel_poll(int* loc_fell, int* trial_fell) {
  const uint32_t now = millis();
  bool loc_hi   = (digitalRead(PANEL_LOC_PIN)   != LOW);
  bool trial_hi = (digitalRead(PANEL_TRIAL_PIN) != LOW);
  *loc_fell   = debounce_fell(&g_panel_loc,   loc_hi,   now, PANEL_DEBOUNCE_MS) ? 1 : 0;
  *trial_fell = debounce_fell(&g_panel_trial, trial_hi, now, PANEL_DEBOUNCE_MS) ? 1 : 0;
}

#endif  // ARDUINO
