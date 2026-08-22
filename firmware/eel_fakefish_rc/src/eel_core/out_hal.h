// out_hal.h — L1 OUTPUT HAL: signed int16 waveform sample -> two DRV8871 half-bridges.
//
// This is the one place any fakefish surface touches the electrodes. A control surface (L3)
// decides WHAT to play; a sample producer (L2: eel_player, sd_player, locgen) decides the
// int16 sample values; this file turns one int16 into the two PWM duties that drive the water.
//
// OUTPUT STAGE — two DRV8871 single-bridge drivers (one per electrode) on a 36 V rail, each
// driven as a TRUE COMPLEMENTARY pair. Per board IN1 is held steadily HIGH and IN2 is PWM'd at
// the COMPLEMENT of the wanted duty, so the bridge alternates DRIVE <-> BRAKE and never
// coasts/floats: board A = pin 2 (IN1 HIGH) + pin 22 (IN2 PWM); board B = pin 3 (IN1 HIGH) +
// pin 23 (IN2 PWM). +phase drives A while B is braked to GND; -phase drives B while A is braked
// (bipolar differential across the dipole). Idle / between pulses = BOTH boards braked to GND
// (actively 0 V, NOT floating). Pins, carrier and guards live in config.h.
//
// SIGN == POLARITY. The sign of the sample selects which electrode drives, so flipping the sign
// of a whole playback flips the dipole — that is how both surfaces randomise polarity per
// playback (anti-pitting: the eel EOD is net-DC/monophasic, so always-same-polarity
// electrolysis would pit the electrodes).
//
// HEADER-ONLY ON PURPOSE. Everything here is `static inline` so each sketch compiles its own
// copy into its own binary — separate binaries, so there is no ODR problem, and no build-system
// wiring is needed beyond the sketch's bundled src/eel_core/. Include this from exactly ONE
// translation unit per sketch (the .ino): the noise-shaper accumulators below are file-static,
// so two including TUs would silently keep two divergent shaper states.
#pragma once
#include <stdint.h>
#include "config.h"   // DRV_* pins, PWM_CARRIER_HZ, PWM_BITS, PWM_DUTY_MAX, LED_PIN, AMP_DEBUG_*

// ===== Noise shaping (pure; no Arduino dependency) =========================
// Per-channel first-order error-feedback noise shaping (~+3 effective in-band bits). The shaper
// runs once per waveform SAMPLE (from the SAMPLE_RATE_HZ ISR), NOT per PWM carrier cycle — so the
// 100 kHz carrier does not touch it. Its quantization noise is high-passed toward the 25 kHz
// sample-Nyquist (above the <5 kHz signal band), where the output RC attenuates the upper part;
// that band is set by the sample rate, independent of the carrier.
static volatile int32_t err_a = 0, err_b = 0;

// The residual is carried from the CLAMPED q on purpose — that is the anti-windup property that
// stops a clipped sample from bleeding error into the next one.
//
// There is deliberately NO dead-zone compensation here: magnitude 0 maps to duty 0 (a hard brake),
// not to a bias. config.h -> "The driver dead zone" records the pedestal that was tried, what the
// bench measured, and why it must not come back.
static inline int shape(int32_t mag, volatile int32_t* err) {
  int32_t v = mag + *err;
  int32_t q = (v + 64) >> 7;          // round to nearest of 128 (full 8-bit: 32767 -> 255)
  if (q < 0) q = 0;
  if (q > (int32_t)PWM_DUTY_MAX) q = (int32_t)PWM_DUTY_MAX;
  *err = v - (q << 7);                // carry the residual into the next sample
  return (int)q;
}

// ===== The duty mapping (pure; no Arduino dependency) ======================
// One channel's signed-magnitude sample -> the duty code actually commanded. Split out from
// out_write() so the mapping is host-testable — see firmware/eel_core/host_test/out_hal_selftest.cpp.
static inline int out_duty_for(int32_t mag, volatile int32_t* err) {
  return shape(mag, err);
}

// ===== Arduino glue (analogWrite / GPIO) ===================================
#ifdef ARDUINO
#include <Arduino.h>

// Complementary drive of one board: IN1 is held HIGH (steady, in out_begin()); IN2 is PWM'd at the
// COMPLEMENT of the wanted output duty, so the bridge alternates DRIVE (IN2 low) <-> BRAKE (IN2 high)
// and NEVER coasts. duty 0 -> IN2 = PWM_DUTY_MAX -> brake (OUT actively at GND); duty PWM_DUTY_MAX ->
// IN2 = 0 -> drive (OUT at rail); average OUT = duty/PWM_DUTY_MAX * rail (linear). The DRV8871's
// internal dead-time makes the IN2 edges shoot-through-safe, so no software dead-time is needed.
static inline void drive_board(uint8_t in2_pin, int duty) {
  analogWrite(in2_pin, (int)PWM_DUTY_MAX - duty);
}

// The non-driving board is braked to GND (out_duty_for(0) == 0), so between the two electrodes the
// water sees exactly the driven side and nothing else. Both boards idle at the same 0 V, so there
// is no common-mode offset for a single-ended listener to pick up.
static inline void out_write(int16_t s) {
  int32_t mag_a = (s > 0) ?  s : 0;   // positive phase -> board A drives (OUT1), board B braked to GND
  int32_t mag_b = (s < 0) ? -s : 0;   // negative phase -> board B drives (OUT1), board A braked to GND
  drive_board(DRV_A_IN2_PIN, out_duty_for(mag_a, &err_a));
  drive_board(DRV_B_IN2_PIN, out_duty_for(mag_b, &err_b));
}

// Idle / silence: BOTH boards actively BRAKED to GND (IN1 high + IN2 high = duty 0), NOT coast. This
// is the explicit idle state — never "both inputs low" (that would float the electrodes Hi-Z).
// Prefer out_silence(), which also clears the shaper; this stays as the bare primitive.
static inline void out_brake() {
  drive_board(DRV_A_IN2_PIN, 0);   // duty 0 -> IN2 high -> brake OUT to GND
  drive_board(DRV_B_IN2_PIN, 0);
}

// Brake both boards AND zero the shaper. Use THIS (not bare out_brake()) at every gap/silence
// boundary: a shaper left holding residual error leaks stray duty into the silence.
//
// UNCONDITIONAL, and that is load-bearing. Between 2026-08-21 and 2026-08-22 this was armed-aware
// and held both bridges at a dead-zone pedestal while a playback or localization train was in
// flight. On the bench that ran the output filter hot and made the water a cacophony, because it
// meant the bridges switched a 36 V square through every silence instead of sitting at 0 V. Idle
// is 0 V, always. See config.h -> "The driver dead zone".
static inline void out_silence() {
  err_a = 0; err_b = 0;
  out_brake();
}

// Bring the output stage up. Call FIRST from setup(), before anything else can emit.
// ORDER IS LOAD-BEARING: hold both IN1 HIGH first so each bridge's high side is defined before its
// IN2 begins modulating it, THEN set the carrier, THEN brake to the idle state. Getting this order
// wrong glitches the electrodes at boot.
static inline void out_begin() {
  pinMode(DRV_A_IN1_PIN, OUTPUT); digitalWriteFast(DRV_A_IN1_PIN, HIGH);
  pinMode(DRV_B_IN1_PIN, OUTPUT); digitalWriteFast(DRV_B_IN1_PIN, HIGH);
  analogWriteResolution(PWM_BITS);
  analogWriteFrequency(DRV_A_IN2_PIN, PWM_CARRIER_HZ);   // 100 kHz carrier on each board's IN2
  analogWriteFrequency(DRV_B_IN2_PIN, PWM_CARRIER_HZ);
  out_silence();                                         // idle: both electrodes ACTIVELY braked to GND
}

// ===== Bench amplitude debug (compile flag AMP_DEBUG in config.h) ==========
#if AMP_DEBUG
// Commanded-duty -> intended DRV8871 state, for the Serial print (so the scope matches the firmware):
// duty 0 = fully BRAKED (OUT actively at GND); duty PWM_DUTY_MAX = fully DRIVEN (OUT at rail); in
// between = PWM alternating drive/brake at the proportional average.
static const char* amp_debug_state(int duty) {
  if (duty <= 0)                  return "BRAKE(0V)  ";
  if (duty >= (int)PWM_DUTY_MAX)  return "DRIVE(rail)";
  return "DRIVE/BRAKE";
}
// Drive one held (dutyA, dutyB) level through the SAME complementary HAL as normal play (IN1 held
// HIGH in amp_debug_run(); IN2 = PWM_DUTY_MAX - duty) with RAW duty (noise-shaper BYPASSED — isolates
// the commanded-duty -> output-volts transfer). LED-code it (blink `led_blinks` fast, then hold the
// LED SOLID on a clean known level), and print the commanded duty + the intended DRIVE/BRAKE state
// AND the actual IN2 duty per board, so a scope reading matches exactly what the firmware sent.
static void amp_debug_hold(const char* tag, int duty_a, int duty_b, uint32_t ms, int led_blinks) {
  for (int i = 0; i < led_blinks; i++) {
    digitalWriteFast(LED_PIN, HIGH); delay(60);
    digitalWriteFast(LED_PIN, LOW);  delay(60);
  }
  analogWrite(DRV_A_IN2_PIN, (int)PWM_DUTY_MAX - duty_a);   // IN1 held HIGH -> IN2 low=drive, high=brake
  analogWrite(DRV_B_IN2_PIN, (int)PWM_DUTY_MAX - duty_b);
  digitalWriteFast(LED_PIN, HIGH);   // solid == "level held — read the scope now"
  Serial.print("[AMP_DEBUG] ");  Serial.print(tag);
  Serial.print("  A: duty="); Serial.print(duty_a);
  Serial.print(" IN2=");      Serial.print((int)PWM_DUTY_MAX - duty_a);
  Serial.print(" ");          Serial.print(amp_debug_state(duty_a));
  Serial.print("   B: duty="); Serial.print(duty_b);
  Serial.print(" IN2=");      Serial.print((int)PWM_DUTY_MAX - duty_b);
  Serial.print(" ");          Serial.print(amp_debug_state(duty_b));
  Serial.print("   (A=");     Serial.print(100.0f * (float)duty_a / (float)PWM_DUTY_MAX, 1);
  Serial.print("%, B=");      Serial.print(100.0f * (float)duty_b / (float)PWM_DUTY_MAX, 1);
  Serial.println("% of full-scale)");
  delay(ms);
  digitalWriteFast(LED_PIN, LOW);
}
// Self-contained scope-calibration routine — NEVER returns (the normal sample-clock ISR is never
// started). Call it from setup() right after out_begin(), under `#if AMP_DEBUG`.
static void amp_debug_run() {
  // Stand alone: hold both IN1 HIGH and PWM both IN2 at the carrier (out_begin() already did this
  // before we were called; re-asserted so the debug path is self-contained), then loop forever.
  pinMode(DRV_A_IN1_PIN, OUTPUT); digitalWriteFast(DRV_A_IN1_PIN, HIGH);
  pinMode(DRV_B_IN1_PIN, OUTPUT); digitalWriteFast(DRV_B_IN1_PIN, HIGH);
  analogWriteResolution(PWM_BITS);
  analogWriteFrequency(DRV_A_IN2_PIN, PWM_CARRIER_HZ);
  analogWriteFrequency(DRV_B_IN2_PIN, PWM_CARRIER_HZ);
  pinMode(LED_PIN, OUTPUT);
  digitalWriteFast(LED_PIN, LOW);
  Serial.begin(AMP_DEBUG_SERIAL_BAUD);
  for (uint32_t t0 = millis(); !Serial && (uint32_t)(millis() - t0) < 2000u; ) { /* brief USB wait */ }
  static const int sweep[] = AMP_DEBUG_SWEEP_LEVELS;
  const int n_sweep = (int)(sizeof(sweep) / sizeof(sweep[0]));
  for (;;) {
    Serial.println();
    Serial.println("==== fakefish AMP_DEBUG — complementary drive (shaper bypassed, sample-ISR off) ====");
    Serial.println("IN1 held HIGH; IN2 = 255-duty. Probe electrode A & B single-ended to battery GND, and A-B.");
    Serial.println("duty 0 must read HARD 0 V (braked, not floating); 255 ~ full rail; SWEEP A = linear.");
    amp_debug_hold("ZERO       ", 0,   0,   AMP_DEBUG_HOLD_MS, 0);
    amp_debug_hold("FULLSCALE A", 255, 0,   AMP_DEBUG_FULL_MS, 1);
    amp_debug_hold("FULLSCALE B", 0,   255, AMP_DEBUG_FULL_MS, 2);
    Serial.println("---- duty sweep on channel A (B braked to GND) ----");
    for (int i = 0; i < n_sweep; i++)
      amp_debug_hold("SWEEP A    ", sweep[i], 0, AMP_DEBUG_HOLD_MS, i + 1);
    Serial.println("==== sweep complete — repeating ====");
  }
}
#endif  // AMP_DEBUG

#endif  // ARDUINO
