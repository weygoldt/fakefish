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
// pin 23 (IN2 PWM). +phase drives A while B sits at the pedestal; -phase drives B while A sits at
// the pedestal (bipolar differential across the dipole). Idle = BOTH boards at the same duty, so
// the dipole sees 0 V either way: the DEAD-ZONE PEDESTAL while armed, a hard brake to GND while
// disarmed — never floating. Pins, carrier, the pedestal and the guards live in config.h.
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

// `qmax` is the largest duty code the caller may use. It is PWM_DUTY_MAX (255) when there is no
// pedestal and OUT_SIGNAL_DUTY_MAX (234) when there is, so that pedestal + signal still lands
// exactly on 255 at full scale. The residual is carried from the CLAMPED q on purpose — that is
// the anti-windup property that stops a clipped sample from bleeding error into the next one.
static inline int shape(int32_t mag, volatile int32_t* err, int qmax) {
  int32_t v = mag + *err;
  int32_t q = (v + 64) >> 7;          // round to nearest of 128 (full 8-bit: 32767 -> 255)
  if (q < 0) q = 0;
  if (q > qmax) q = qmax;
  *err = v - (q << 7);                // carry the residual into the next sample
  return (int)q;
}

// ===== The duty mapping (pure; no Arduino dependency) ======================
// One channel's signed-magnitude sample -> the duty code actually commanded, INCLUDING the
// dead-zone pedestal (config.h explains why it exists). Split out from out_write() so the whole
// mapping is host-testable — see firmware/eel_core/host_test/out_hal_selftest.cpp.
//
// Setting OUT_PEDESTAL_DUTY to 0 reduces this EXACTLY to the pre-pedestal behaviour by
// construction: the scale becomes mag*255/255 (the identity) and qmax becomes 255. That is the
// documented way to disable the feature.
static inline int32_t out_scale_to_headroom(int32_t mag) {
  return (mag * (int32_t)OUT_SIGNAL_DUTY_MAX) / (int32_t)PWM_DUTY_MAX;
}
static inline int out_duty_for(int32_t mag, volatile int32_t* err) {
  return OUT_PEDESTAL_DUTY + shape(out_scale_to_headroom(mag), err, OUT_SIGNAL_DUTY_MAX);
}

// ===== Armed state =========================================================
// ARMED means "a stimulus is going into the water right now, or may within this playback" — the
// bridges are held live at the pedestal so no sample can land in the driver's dead zone.
// DISARMED means "nothing is playing and nothing is scheduled" — both bridges braked hard to GND,
// zero dissipation, zero battery drain. A control surface (L3) arms at every playback/train start
// and disarms when it returns to idle; it is deliberately NOT armed permanently, because the
// pedestal costs ~0.45 W per channel in the first output-filter resistor while it is on.
static volatile bool s_out_armed = false;
static inline bool out_armed() { return s_out_armed; }

// The duty BOTH boards are held at while emitting nothing. Pure, so the armed -> idle-duty
// mapping is host-testable even though the analogWrite() that applies it is not.
static inline int out_idle_duty() { return s_out_armed ? OUT_PEDESTAL_DUTY : 0; }

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

// The far board is held at the PEDESTAL, not at zero — out_duty_for(0) == OUT_PEDESTAL_DUTY — so
// the braked side never sits in the driver's dead zone either. Both boards carry the same pedestal,
// so it is common mode and cancels across the dipole; only the difference reaches the water.
static inline void out_write(int16_t s) {
  int32_t mag_a = (s > 0) ?  s : 0;   // positive phase -> board A drives (OUT1), board B at pedestal
  int32_t mag_b = (s < 0) ? -s : 0;   // negative phase -> board B drives (OUT1), board A at pedestal
  drive_board(DRV_A_IN2_PIN, out_duty_for(mag_a, &err_a));
  drive_board(DRV_B_IN2_PIN, out_duty_for(mag_b, &err_b));
}

// Idle / silence: BOTH boards actively BRAKED to GND (IN1 high + IN2 high = duty 0), NOT coast. This
// is the explicit idle state — never "both inputs low" (that would float the electrodes Hi-Z).
// UNCONDITIONAL and ARMED-BLIND: prefer out_silence() (armed-aware) or out_disarm() (which also
// clears the armed flag). This stays as the primitive both are built on, and as an escape hatch
// for a surface that must guarantee 0 V regardless of state.
static inline void out_brake() {
  drive_board(DRV_A_IN2_PIN, 0);   // duty 0 -> IN2 high -> brake OUT to GND
  drive_board(DRV_B_IN2_PIN, 0);
}

// Emit NOTHING (differentially) AND zero the shaper. Use THIS (not bare out_brake()) at every
// gap/silence boundary: a shaper left holding residual error leaks stray duty into the silence.
//
// What "nothing" means depends on the armed state, and that is the whole point:
//   ARMED    -> both boards held at OUT_PEDESTAL_DUTY. Differentially zero, but the bridges stay
//               live, so the next pulse's flanks do not have to climb out of the dead zone.
//   DISARMED -> both boards braked hard to GND. Zero volts, zero dissipation, zero drain.
// Making this unconditional again would put a common-mode step at every zero-valued sample inside
// a playback — which is exactly what the pedestal exists to avoid.
static inline void out_silence() {
  err_a = 0; err_b = 0;
  const int p = out_idle_duty();
  drive_board(DRV_A_IN2_PIN, p);
  drive_board(DRV_B_IN2_PIN, p);
}

// ARM before anything reaches the water; DISARM on the way back to idle. An L3 surface calls
// out_arm() at the top of every playback/train start (in place of the out_silence() that used to
// be there) and out_disarm() when it returns to idle. Both zero the shaper, so they are drop-in
// replacements for out_silence() at those seams.
//
// The arm transition is a common-mode step of OUT_PEDESTAL_DUTY/PWM_DUTY_MAX of the rail on BOTH
// electrodes at once. It cancels differentially except through channel-to-channel component
// mismatch (~2 % of the step, i.e. well under 0.2 % of a full-scale pulse), and the output filter
// smooths it over ~100 us. It is placed at a playback boundary, never inside a pulse train.
static inline void out_arm()    { s_out_armed = true;  out_silence(); }
static inline void out_disarm() { s_out_armed = false; out_silence(); }

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
  out_disarm();                                          // idle: DISARMED, both electrodes braked to GND
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
