// out_hal_selftest — host self-test for the L1 duty mapping and the driver dead-zone pedestal.
//
// Covers the PURE half of out_hal.h: the noise shaper, the headroom scaling, the pedestal offset
// and the armed -> idle-duty mapping. The analogWrite() half needs Arduino and is covered by the
// group-3 Teensy syntax compile instead.
//
// THE PROPERTY THIS FILE EXISTS TO PROTECT (config.h explains the physics): no channel may ever be
// commanded to a duty inside (0, OUT_PEDESTAL_DUTY), because those codes ask the DRV8871 for a
// drive pulse shorter than its minimum detectable input width and it may ignore them outright. A
// regression there is invisible on a scope trace of a full-amplitude pulse and only shows up as
// level-dependent distortion at low amplitude — exactly the failure this feature was added to fix.
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
// 1. The constants themselves, and the dead-zone arithmetic they encode.
// ---------------------------------------------------------------------------
static void test_constants() {
  check(OUT_PEDESTAL_DUTY >= 0, "pedestal is non-negative");
  check(OUT_SIGNAL_DUTY_MAX == (int)PWM_DUTY_MAX - OUT_PEDESTAL_DUTY, "signal headroom = 255 - pedestal");
  check(OUT_PEDESTAL_DUTY + OUT_SIGNAL_DUTY_MAX == (int)PWM_DUTY_MAX,
        "pedestal + headroom lands exactly on full scale (no clamp at the top)");

  // analogWrite maps duty q to an IN2-LOW (DRIVE) time of (q+1)/256 of the carrier period. This is
  // the arithmetic the whole feature rests on, so pin it here: at 100 kHz, duty 0 is a 39 ns pulse.
  const double period_ns = 1e9 / (double)PWM_CARRIER_HZ;
  const double on0_ns    = period_ns * 1.0 / 256.0;
  check(std::fabs(period_ns - 10000.0) < 1e-6, "carrier period is 10 us at 100 kHz");
  check(on0_ns > 38.0 && on0_ns < 40.0, "duty 0 commands a ~39 ns drive pulse");
  if (OUT_PEDESTAL_DUTY > 0) {
    const double on_ped_ns = period_ns * (OUT_PEDESTAL_DUTY + 1) / 256.0;
    check(on_ped_ns >= 400.0,
          "the pedestal clears the DRV8871's 400 ns TYPICAL input-detection window");
  }
}

// ---------------------------------------------------------------------------
// 2. Endpoints: silence is the pedestal, full scale is still the rail.
// ---------------------------------------------------------------------------
static void test_endpoints() {
  volatile int32_t e = 0;
  check(out_duty_for(0, &e) == OUT_PEDESTAL_DUTY, "magnitude 0 -> the pedestal, never 0 duty");
  e = 0;
  const int full = out_duty_for(32767, &e);
  checkf(full == (int)PWM_DUTY_MAX, "full scale -> %ld, expected %ld", (long)full, (long)PWM_DUTY_MAX);

  // A zero-magnitude run must STAY at the pedestal — a shaper that drifted would leak stray duty
  // into every silent gap, which is the bug out_silence() zeroing the error exists to prevent.
  e = 0;
  for (int i = 0; i < 5000; i++)
    check(out_duty_for(0, &e) == OUT_PEDESTAL_DUTY, "a long silence never leaves the pedestal");
}

// ---------------------------------------------------------------------------
// 3. THE INVARIANT: no commanded duty ever lands inside the dead zone.
// ---------------------------------------------------------------------------
static void test_no_dead_zone_exhaustive() {
  // Every magnitude, with shaper state carried across the sweep so the dither is exercised.
  volatile int32_t e = 0;
  for (int32_t mag = 0; mag <= 32767; mag++) {
    const int q = out_duty_for(mag, &e);
    check(q >= 0 && q <= (int)PWM_DUTY_MAX, "duty stays inside 0..255");
    check(q == 0 || q >= OUT_PEDESTAL_DUTY, "duty is never inside (0, pedestal)");
  }
  // ...and again with the sweep descending, which is where a shaper carrying negative error
  // would be most likely to undershoot into the gap.
  e = 0;
  for (int32_t mag = 32767; mag >= 0; mag--) {
    const int q = out_duty_for(mag, &e);
    check(q == 0 || q >= OUT_PEDESTAL_DUTY, "duty is never inside (0, pedestal), descending");
  }
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
    // The ideal SATURATES at full scale — see test_top_of_scale_saturates() for why that is
    // pre-existing behaviour and not something the pedestal introduced.
    double ideal = OUT_PEDESTAL_DUTY
                 + (double)((mag * (int32_t)OUT_SIGNAL_DUTY_MAX) / (int32_t)PWM_DUTY_MAX) / 128.0;
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
// to anything below full scale is (qmax * 128) - 64. Everything above it saturates. This is NOT a
// consequence of the pedestal: the pre-pedestal mapping saturated from magnitude 32576 upward
// (255*128 - 64), i.e. the top 0.58 % of the int16 range was already flat, which is where the
// 32640 in `fullscale_pulse_peak_mv`'s derivation comes from. Pinned here so a future change to
// the scaling cannot quietly move it.
static void test_top_of_scale_saturates() {
  const int32_t scaled_knee = (int32_t)OUT_SIGNAL_DUTY_MAX * 128 - 64;
  // Smallest raw magnitude whose scaled value exceeds the knee -> the first saturating input.
  int32_t first_sat = -1;
  for (int32_t mag = 0; mag <= 32767; mag++) {
    if (out_scale_to_headroom(mag) > scaled_knee) { first_sat = mag; break; }
  }
  check(first_sat > 0, "saturation knee exists inside the int16 range");
  check(first_sat > 32000, "saturation only affects the very top of the range");
  volatile int32_t e = 0;
  check(out_duty_for(first_sat, &e) == (int)PWM_DUTY_MAX, "at the knee the output is full scale");
  const double lost_pct = 100.0 * (32767.0 - (double)first_sat) / 32767.0;
  if (lost_pct > 1.0) {
    std::printf("FAIL: %.2f%% of the int16 range saturates (was 0.58%% before the pedestal)\n", lost_pct);
    failures++;
  }
}

// ---------------------------------------------------------------------------
// 5. The real EOD, at the amplitudes the devices actually use.
// ---------------------------------------------------------------------------
static void test_real_eod_sweep() {
  // Levels: full scale, the 0.90 volley, the 0.50 marker, the 0.45 localization, and two settings
  // below anything the amplitude control can select — the region where the dead zone used to
  // silence the device entirely.
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
      check(qa == 0 || qa >= OUT_PEDESTAL_DUTY, "EOD sweep: board A never in the dead zone");
      check(qb == 0 || qb >= OUT_PEDESTAL_DUTY, "EOD sweep: board B never in the dead zone");
      check(qa <= (int)PWM_DUTY_MAX && qb <= (int)PWM_DUTY_MAX, "EOD sweep: duty never overflows");
      // The two boards are sign-split: they are never both driven above the pedestal at once.
      check(qa <= OUT_PEDESTAL_DUTY || qb <= OUT_PEDESTAL_DUTY,
            "EOD sweep: only one board drives at a time (sign split)");
      if (qa > OUT_PEDESTAL_DUTY || qb > OUT_PEDESTAL_DUTY) any_output = true;
    }
    // Even the smallest level must still put SOMETHING in the water. Before the pedestal, a level
    // this low produced nothing at all on a guaranteed-spec part.
    check(any_output, "EOD sweep: every level still produces output");
  }
}

// ---------------------------------------------------------------------------
// 6. Armed state -> idle duty.
// ---------------------------------------------------------------------------
static void test_armed_idle_duty() {
  s_out_armed = false;
  check(!out_armed(), "starts disarmed");
  check(out_idle_duty() == 0, "disarmed idle is a HARD BRAKE (duty 0) — no dissipation, no drain");
  s_out_armed = true;
  check(out_armed(), "armed flag reads back");
  check(out_idle_duty() == OUT_PEDESTAL_DUTY, "armed idle holds the pedestal on both boards");
  s_out_armed = false;   // leave it as found
}

// ---------------------------------------------------------------------------
// 7. The shaper's own contract, independent of the pedestal.
// ---------------------------------------------------------------------------
static void test_shaper_contract() {
  volatile int32_t e = 0;
  // Documented mapping at qmax 255: round to nearest of 128, so 32767 -> 256 -> clamped to 255.
  check(shape(0, &e, 255) == 0, "shape(0) == 0");
  e = 0; check(shape(128, &e, 255) == 1, "shape(128) == 1");
  e = 0; check(shape(64,  &e, 255) == 1, "shape(64) rounds up (round-to-nearest of 128)");
  e = 0; check(shape(63,  &e, 255) == 0, "shape(63) rounds down");
  e = 0; check(shape(32767, &e, 255) == 255, "shape(32767) clamps to 255");
  // Anti-windup: the residual is taken from the CLAMPED q, so a clipped sample must not bleed a
  // huge error into the next one.
  e = 0; (void)shape(32767, &e, 255);
  check(e > -32768 && e < 32768, "clipping does not blow up the carried error");
  // The ceiling is honoured.
  e = 0; check(shape(32767, &e, OUT_SIGNAL_DUTY_MAX) == OUT_SIGNAL_DUTY_MAX, "shape honours qmax");
}

int main() {
  test_constants();
  test_endpoints();
  test_no_dead_zone_exhaustive();
  test_shaper_mean_accuracy();
  test_top_of_scale_saturates();
  test_real_eod_sweep();
  test_armed_idle_duty();
  test_shaper_contract();
  if (failures) { std::printf("%d failure(s)\n", failures); return 1; }
  std::printf("OK\n");
  return 0;
}
