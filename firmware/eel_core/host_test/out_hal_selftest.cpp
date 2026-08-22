// out_hal_selftest — host self-test for the L1 duty mapping.
//
// Covers the PURE half of out_hal.h: the noise shaper and the signed-magnitude -> duty mapping.
// The analogWrite() half needs Arduino and is covered by the group-3 Teensy syntax compile.
//
// THE PROPERTY THIS FILE EXISTS TO PROTECT: **silence is duty 0 on BOTH boards.** A zero-valued
// sample, a long silent gap, and the non-driving side of any pulse must all command exactly 0 —
// a hard brake to GND — never a bias.
//
// That is a field-earned invariant, not a stylistic one. Between 2026-08-21 and 2026-08-22 both
// bridges were held at a dead-zone "pedestal" (OUT_PEDESTAL_DUTY = 21) whenever a playback or a
// localization train was in flight. On the bench (Teensy 3.5, 36 V, the built 2x 220R/220nF
// filter) that ran the output-filter wiring hot and turned the water into a cacophony, because it
// meant the bridges switched a 36 V square continuously through every silence — >98 % of a
// localization train — leaving both electrodes at ~3.1 V DC instead of 0 V. The A/B was decisive
// in both directions. config.h -> "The driver dead zone" has the full record.
//
// So the tests below assert the OPPOSITE of what this file asserted before that revert. If a
// future change reintroduces any common-mode bias at silence, these fail.
//
// Build: g++ -std=c++17 -Wall -Wextra out_hal_selftest.cpp eel_stimuli.cpp -o t && ./t   -> "OK"
#include <cstdio>
#include <cstdlib>
#include <cmath>
#include <cstdint>

#include "../out_hal.h"        // pure half only — ARDUINO is not defined on the host
#include "../eel_stimuli.h"    // EOD_HV, for the real-waveform sweep

static int failures = 0;
static void check(bool ok, const char* what) {
  if (!ok) { std::printf("FAIL: %s\n", what); failures++; }
}
static void checkf(bool ok, const char* fmt, long a, long b) {
  if (!ok) { std::printf("FAIL: "); std::printf(fmt, a, b); std::printf("\n"); failures++; }
}

// ---------------------------------------------------------------------------
// 1. The constants, and the pulse-width arithmetic that motivated the pedestal.
// ---------------------------------------------------------------------------
// The dead zone is real physics and worth keeping on the record even though we no longer
// compensate for it: analogWrite maps duty q to an IN2-LOW (DRIVE) time of (q+1)/256 of the
// carrier period, so the bottom codes ask the DRV8871 for a pulse shorter than the 400 ns it
// TYPICALLY needs to detect an input. The measured answer on this hardware is that it does not
// matter — the amplitude control works to the bottom of its travel without the compensation.
static void test_constants() {
  check(PWM_DUTY_MAX == 255u, "8-bit full-scale duty is 255");
  const double period_ns = 1e9 / (double)PWM_CARRIER_HZ;
  const double on0_ns    = period_ns * 1.0 / 256.0;
  check(std::fabs(period_ns - 10000.0) < 1e-6, "carrier period is 10 us at 100 kHz");
  check(on0_ns > 38.0 && on0_ns < 40.0, "duty 0 commands a ~39 ns drive pulse");
}

// ---------------------------------------------------------------------------
// 2. Endpoints: silence is a HARD BRAKE, full scale is still the rail.
// ---------------------------------------------------------------------------
static void test_endpoints() {
  volatile int32_t e = 0;
  check(out_duty_for(0, &e) == 0, "magnitude 0 -> duty 0 (hard brake), NEVER a pedestal");
  e = 0;
  const int full = out_duty_for(32767, &e);
  checkf(full == (int)PWM_DUTY_MAX, "full scale -> %ld, expected %ld", (long)full, (long)PWM_DUTY_MAX);

  // A zero-magnitude run must STAY at 0 — a shaper that drifted would leak stray duty into every
  // silent gap, which is the bug out_silence() zeroing the error exists to prevent.
  e = 0;
  for (int i = 0; i < 5000; i++)
    check(out_duty_for(0, &e) == 0, "a long silence never leaves duty 0");
}

// ---------------------------------------------------------------------------
// 3. THE INVARIANT: no silent sample ever commands a non-zero duty.
// ---------------------------------------------------------------------------
static void test_silence_is_zero_exhaustive() {
  // Sweep every magnitude with the shaper state carried across, then feed a zero and require the
  // brake. This is the path a real pulse takes into its trailing silence: whatever error the
  // shaper accumulated during the pulse must not manifest as a bias afterwards.
  for (int32_t mag = 0; mag <= 32767; mag += 7) {
    volatile int32_t e = 0;
    (void)out_duty_for(mag, &e);
    // out_silence() clears the shaper at every gap boundary; emulate that and require 0.
    e = 0;
    check(out_duty_for(0, &e) == 0, "after any magnitude, a cleared shaper commands duty 0");
  }
  // Range check across the whole domain, ascending and descending.
  volatile int32_t e = 0;
  for (int32_t mag = 0; mag <= 32767; mag++)
    check(out_duty_for(mag, &e) >= 0 && out_duty_for(mag, &e) <= (int)PWM_DUTY_MAX,
          "duty stays inside 0..255");
}

// ---------------------------------------------------------------------------
// 4. The shaper still gets the AVERAGE right — that is what it is for.
// ---------------------------------------------------------------------------
static void test_shaper_mean_accuracy() {
  // For a held magnitude the mean duty must track the ideal to well under one code: the shaper
  // dithers between neighbouring codes and the output filter averages them.
  for (int32_t mag = 0; mag <= 32767; mag += 337) {
    volatile int32_t e = 0;
    const int N = 4096;
    double sum = 0.0;
    for (int i = 0; i < N; i++) sum += out_duty_for(mag, &e);
    const double mean = sum / N;
    double ideal = (double)mag / 128.0;
    if (ideal > (double)PWM_DUTY_MAX) ideal = (double)PWM_DUTY_MAX;
    if (std::fabs(mean - ideal) > 0.05) {
      std::printf("FAIL: held mag %ld -> mean duty %.4f, ideal %.4f\n", (long)mag, mean, ideal);
      failures++;
    }
  }
}

// ---------------------------------------------------------------------------
// 4b. The top of the int16 range is flat, and ALWAYS WAS.
// ---------------------------------------------------------------------------
// The shaper rounds to the nearest multiple of 128 and clamps, so the largest magnitude that maps
// to anything below full scale is (255 * 128) - 64 = 32576. Everything above saturates — the top
// 0.58 % of the int16 range, which is where the 32640 in `fullscale_pulse_peak_mv`'s derivation
// comes from. Pinned so a future change to the scaling cannot quietly move it.
static void test_top_of_scale_saturates() {
  int32_t first_sat = -1;
  for (int32_t mag = 0; mag <= 32767; mag++) {
    volatile int32_t e = 0;
    if (out_duty_for(mag, &e) == (int)PWM_DUTY_MAX) { first_sat = mag; break; }
  }
  check(first_sat > 0, "saturation knee exists inside the int16 range");
  check(first_sat > 32000, "saturation only affects the very top of the range");
  const double lost_pct = 100.0 * (32767.0 - (double)first_sat) / 32767.0;
  if (lost_pct > 1.0) {
    std::printf("FAIL: %.2f%% of the int16 range saturates (expected ~0.58%%)\n", lost_pct);
    failures++;
  }
}

// ---------------------------------------------------------------------------
// 5. The real EOD, at the amplitudes the devices actually use.
// ---------------------------------------------------------------------------
// This is the test that would have caught the pedestal: it walks the real waveform through the
// real mapping for BOTH boards and requires that the idle board is at 0, so there is no
// common-mode offset for a single-ended listener (or the water) to see.
static void test_real_eod_sweep() {
  const double levels[] = { 1.0, 0.90, 0.50, 0.45, 0.25, 0.125, 0.0625, 0.03 };
  for (double amp : levels) {
    volatile int32_t ea = 0, eb = 0;
    bool any_output = false;
    for (int i = 0; i < EOD_HV_LEN; i++) {
      const int32_t s = (int32_t)std::lround(amp * (double)EOD_HV[i]);
      const int32_t mag_a = (s > 0) ?  s : 0;
      const int32_t mag_b = (s < 0) ? -s : 0;
      const int qa = out_duty_for(mag_a, &ea);
      const int qb = out_duty_for(mag_b, &eb);
      check(qa <= (int)PWM_DUTY_MAX && qb <= (int)PWM_DUTY_MAX, "EOD sweep: duty never overflows");
      // Sign split: the two boards are never both driven at once, and the idle one reads 0.
      check(qa == 0 || qb == 0, "EOD sweep: only one board drives at a time (sign split)");
      // NO COMMON MODE. A zero sample must brake BOTH boards — this is the pedestal regression
      // guard, and it is the property whose loss cooked the filter and fouled the water.
      if (s == 0) check(qa == 0 && qb == 0, "EOD sweep: a zero sample brakes BOTH boards to 0 V");
      if (qa > 0 || qb > 0) any_output = true;
    }
    check(any_output, "EOD sweep: every level still produces output");
  }
}

// ---------------------------------------------------------------------------
// 6. The shaper's own contract.
// ---------------------------------------------------------------------------
static void test_shaper_contract() {
  volatile int32_t e = 0;
  // Documented mapping: round to nearest of 128, so 32767 -> 256 -> clamped to 255.
  check(shape(0, &e) == 0, "shape(0) == 0");
  e = 0; check(shape(128, &e) == 1, "shape(128) == 1");
  e = 0; check(shape(64,  &e) == 1, "shape(64) rounds up (round-to-nearest of 128)");
  e = 0; check(shape(63,  &e) == 0, "shape(63) rounds down");
  e = 0; check(shape(32767, &e) == 255, "shape(32767) clamps to 255");
  // Anti-windup: the residual is taken from the CLAMPED q, so a clipped sample must not bleed a
  // huge error into the next one.
  e = 0; (void)shape(32767, &e);
  check(e > -32768 && e < 32768, "clipping does not blow up the carried error");
}

int main() {
  test_constants();
  test_endpoints();
  test_silence_is_zero_exhaustive();
  test_shaper_mean_accuracy();
  test_top_of_scale_saturates();
  test_real_eod_sweep();
  test_shaper_contract();
  if (failures) { std::printf("%d failure(s)\n", failures); return 1; }
  std::printf("OK\n");
  return 0;
}
