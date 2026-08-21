// config.h — L1 OUTPUT-STAGE / HAL configuration, shared by every fakefish surface.
//
// This is the layer-1 half of what used to be one monolithic config.h in fakefish-rc: the
// output stage, the sample clock and the indicator LED — everything that is a property of the
// BOARD rather than of a control surface. Surface-specific constants (RC pin map, panel pins,
// button map, thresholds) live in that surface's own sketch folder; the playback/session
// constants live in shared/stim_constants.json and reach C through the generated stim_levels.h.
//
// Every device in this repo runs this same output stage. Edit here + re-flash to tune.
#pragma once
#include <stdint.h>
#include "eel_stimuli.h"   // STIM_SAMPLE_RATE_HZ, EOD_HV_LEN

// ===== Output stage / sample clock =========================================
// Output stage: two DRV8871 single-bridge drivers (one per electrode) on a 36 V rail. Each board is
// driven as a TRUE COMPLEMENTARY pair so the bridge always ACTIVELY drives its electrode — DRIVE on
// the PWM's driven phase, BRAKE (both outputs pulled to GND) on the other — and NEVER coasts. (The
// old wiring grounded IN2 and PWM'd IN1 alone: the PWM low phase left the bridge Hi-Z / floating,
// which showed on the scope as a two-stage pulse decay (fast driven decay handing off to a slow
// leak-through-water tail — no real EOD does that) and as un-filterable single-ended bleedthrough.
// Actively braking to GND instead of coasting fixes both.) See firmware/README.md "Output stage".
//
// Per board IN1 is held steadily HIGH and IN2 is PWM'd at the COMPLEMENT of the wanted duty. The
// electrode sits on OUT1, and OUT1 only goes high when IN1=1 (DRV8871 truth table: IN1=1,IN2=0 =
// drive OUT1 HIGH; IN1=1,IN2=1 = brake OUT1 to GND), so IN1 MUST be the held-high input and IN2 the
// modulator. Mapping: IN2 = PWM_DUTY_MAX - duty. duty 0 -> IN2 HIGH -> brake (OUT actively at GND);
// duty PWM_DUTY_MAX -> IN2 LOW -> drive (OUT at rail); average OUT = duty/PWM_DUTY_MAX * rail (linear).
// Idle / between pulses = duty 0 on BOTH boards = both electrodes actively braked to GND (not float).
#define DRV_A_IN1_PIN    2                 // board A IN1 — held HIGH  -> DRV8871 A -> OUT1 -> electrode A
#define DRV_A_IN2_PIN    0                 // board A IN2 — 100 kHz PWM (FlexPWM1_1)   [was: IN2 tied to GND]
#define DRV_B_IN1_PIN    3                 // board B IN1 — held HIGH  -> DRV8871 B -> OUT1 -> electrode B
#define DRV_B_IN2_PIN    1                 // board B IN2 — 100 kHz PWM (FlexPWM1_0)   [was: IN2 tied to GND]
// IN2 on pins 0/1 (not 9/10): both are true FlexPWM (uniform with each board's own DC IN1), and 9/10
// are taken by control surfaces (the RC panel's LOC/VOLLEY buttons; the button surface's E/F keys).
// Pins 0/1 are the only thing this firmware takes off the otherwise unused hardware Serial1 (RX1/TX1).
// The DRV8871's internal dead-time makes IN1/IN2 transitions shoot-through-safe, so NO software
// dead-time is needed (we only ever toggle IN2 with IN1 held high).
// PWM carrier: 100 kHz (raised from 50 kHz with the DRV8871 upgrade; the DRV8871 switches cleanly
// to ~100 kHz). Why 100 kHz specifically -- the recording grid is SINGLE-ENDED (no common-mode
// rejection downstream), so put the carrier where our own output filter rejects it best and where
// it aliases least:
//   * Rejection: the per-channel output filter is a 2-pole passive RC (2x 220 ohm / 220 nF, see
//     firmware/README.md) with a composite -3 dB at ~1.23 kHz, so 100 kHz sits ~81x above the
//     corner and lands at -59.4 dB. At 50 kHz it would be -47.4 dB. Suppression AT THE SOURCE
//     matters because the single-ended grid cannot clean up residual carrier.
//   * Aliasing into the 48 kHz grid: 50 kHz folds to ~2 kHz (in-band, bad); 100 kHz folds to
//     ~4 kHz -- higher, and the filter has already attenuated it far more before it gets there.
// (Historical note: this constant was chosen when the output filter was a 1-pole ~16 kHz
// differential RC, on the reasoning that 100 kHz was "~6x the corner" for ~16 dB of rejection.
// The filter has since been rebuilt as the 2-pole network above, which makes the SAME choice
// better justified, not worse -- but do not repeat the old ~6x/~16 kHz arithmetic, it is wrong.)
// This is the PWM CARRIER, independent of the 50 kHz waveform SAMPLE_RATE_HZ below.
#define PWM_CARRIER_HZ   100000.0f         // (do NOT revert to the 585937.5 Hz Teensy default)
#define PWM_BITS         8                 // DRV8871 holds duty fine at 100 kHz / 8-bit (see static_assert below)
#define PWM_DUTY_MAX     ((1u << PWM_BITS) - 1u)   // 255 — full-scale 8-bit duty (IN2 = PWM_DUTY_MAX - duty)
#define SAMPLE_RATE_HZ   STIM_SAMPLE_RATE_HZ   // 50000; ISR period == 20 us (waveform sample clock, != carrier)

// ---- Carrier guards: fail LOUDLY at compile time on a bad future bump ----------------------
// (1) The DRV8871 switches cleanly only to ~100 kHz; do not exceed that.
static_assert(PWM_CARRIER_HZ <= 100000.0f,
              "PWM_CARRIER_HZ exceeds the DRV8871's ~100 kHz clean-switching ceiling");
// (2) Full PWM_BITS duty resolution needs a FlexPWM counter modulo of >= 2^PWM_BITS, i.e.
//     PWM_CARRIER_HZ * 2^PWM_BITS <= the FlexPWM source clock. On a stock Teensy 4.1 @ 600 MHz that
//     clock is F_BUS_ACTUAL = 150 MHz, so the 8-bit ceiling is 150e6/256 = 585937.5 Hz (the core's
//     own default carrier). At 100 kHz we use 100000*256 = 25.6 MHz of it (~5.9x headroom), so all
//     8 duty bits survive. A future bump past this ceiling fails the build instead of silently
//     dropping duty bits and degrading the low-voltage localization pulses. (F_BUS_ACTUAL is a
//     runtime variable on Teensy 4.x, not a constant expression, so the ceiling is pinned as a
//     documented constant here rather than read from it.)
static constexpr float PWM_TIMER_CLOCK_HZ = 150000000.0f;  // Teensy 4.1 FlexPWM source (F_BUS_ACTUAL @ 600 MHz)
#ifdef F_CPU  // Teensy build only (host g++ has no F_CPU); pin the 150 MHz assumption to the 600 MHz CPU.
static_assert(F_CPU == 600000000,
              "PWM_TIMER_CLOCK_HZ assumes F_BUS_ACTUAL = 150 MHz (Teensy 4.1 @ 600 MHz); re-derive it "
              "for another CPU speed (F_BUS_ACTUAL = F_CPU / ceil(F_CPU / 150e6))");
#endif
static_assert(PWM_CARRIER_HZ * (float)(1u << PWM_BITS) <= PWM_TIMER_CLOCK_HZ,
              "PWM_CARRIER_HZ too high for full PWM_BITS resolution on Teensy 4.1 (FlexPWM @ 150 MHz)");

// ===== Driver dead-zone pedestal ===========================================
// WHY THIS EXISTS. `analogWrite()` maps duty q to an IN2-LOW (= DRIVE) time of (q+1)/256 of the
// carrier period. At PWM_CARRIER_HZ = 100 kHz that period is 10 us, so duty 0 commands a 39 ns
// drive pulse, duty 4 commands 195 ns, duty 11 commands 469 ns. The DRV8871 needs a MINIMUM INPUT
// PULSE WIDTH to respond at all — datasheet SLVSCY9B, Recommended Operating Conditions footnote 1:
// "The voltages applied to the inputs should have at least 800 ns of pulse width to ensure
// detection. Typical devices require at least 400 ns." Duty codes below ~11 (typical part) or
// ~21 (guaranteed) are therefore UNRELIABLE — the bridge may ignore them outright.
//
// The consequence is not a small linearity error, it is a LEVEL-DEPENDENT one. The flanks and the
// negative pre-potential of every EOD sit at low duty, so as the amplitude control comes down a
// growing fraction of each pulse falls under the threshold and is silently deleted: the pulse is
// hollowed out from the bottom while its PEAK still arrives at the right height. Simulated
// delivered-vs-ideal RMS shape error with the bridge dropping sub-threshold pulses (q_min = 21):
//
//     amp 0.90 -> 3.3 %   0.45 -> 6.9 %   0.25 -> 12.9 %   0.125 -> 32 %   0.0625 -> SILENT
//
// That is larger than everything the output filter does to the pulse, and unlike the filter it
// VARIES WITH THE AMPLITUDE SETTING — which breaks the assumption that the marker / localization /
// volley level RATIOS survive the chain. It is audible: the pulse changes character as the pot
// comes down, then stops entirely, which is how it was first noticed.
//
// THE FIX. Hold BOTH boards at a small non-zero duty whenever the device is armed to emit, and
// ride the signal on top of it. The offset is IDENTICAL on both electrodes, so it is pure common
// mode and cancels in the water (the field is driven by V_A - V_B); no channel is ever commanded
// below OUT_PEDESTAL_DUTY, so nothing enters the dead zone at any amplitude. Same simulation, with
// the pedestal: 0.90 -> 0.04 %, 0.45 -> 0.07 %, 0.25 -> 0.11 %, 0.125 -> 0.28 %, 0.0625 -> 0.51 %.
//
// WHAT IT COSTS.
//   1. HEADROOM. The signal is scaled into the OUT_SIGNAL_DUTY_MAX codes left above the pedestal,
//      so full scale drops by OUT_PEDESTAL_DUTY/PWM_DUTY_MAX — 8.2 % (0.74 dB) at 21. That is less
//      than one click of the RC amplitude control (0.60 dB at mid-scale).
//   2. POWER. While armed the bridges switch continuously, so the first output-filter resistor
//      dissipates V_rail^2 * d(1-d) / R1 ~ 0.45 W per channel even BETWEEN pulses. That is why the
//      pedestal is ARMED rather than permanent — see out_arm()/out_disarm() in out_hal.h. With
//      nothing playing and localization off the firmware disarms, commanding duty 0 on both boards
//      = hard brake = zero dissipation and zero battery drain, exactly as before this feature.
//   3. IDLE IS NO LONGER HARD 0 V SINGLE-ENDED *WHILE ARMED*. It is +OUT_PEDESTAL_DUTY/PWM_DUTY_MAX
//      of the rail, common mode on both electrodes, differentially zero. The star ground is not in
//      the water, so that pedestal has no return path through the water. Disarmed idle is still a
//      hard brake. Do not "simplify" out_silence() back to an unconditional brake — that would put
//      a common-mode step at every zero-valued sample inside a playback.
//
// TUNING THIS NUMBER IS A BENCH JOB AND IT HAS NOT BEEN DONE. 21 is the datasheet-GUARANTEED
// 800 ns bound, chosen because it is safe on any device without measuring. A typical part would be
// fine at 11, which would halve both the headroom and the power cost. Setting it to 0 disables the
// feature entirely and restores the previous behaviour exactly. See firmware/README.md ->
// "The driver dead zone" for the AMP_DEBUG sweep that measures the real threshold on a given board.
#define OUT_PEDESTAL_DUTY     21
#define OUT_SIGNAL_DUTY_MAX   ((int)PWM_DUTY_MAX - OUT_PEDESTAL_DUTY)   // 234 — codes left for signal
static_assert(OUT_PEDESTAL_DUTY >= 0 && OUT_PEDESTAL_DUTY < (int)PWM_DUTY_MAX / 2,
              "OUT_PEDESTAL_DUTY must leave most of the duty range for signal (0 disables it)");

// ===== Indicator LED =======================================================
// Shared by every surface (the Teensy 4.1 built-in LED, plus an external one on the same pin).
// What it MEANS is surface-specific — the button device holds it solid while a WAV streams, the
// RC device flashes it per pulse and blinks patterns for sham / no-signal.
#define LED_PIN           13               // built-in + external indicator LED

// ===== Bench amplitude debug (compile-time; OFF for normal operation) ========
// Set AMP_DEBUG to 1 and re-flash to REPLACE all normal playback with a self-contained
// scope-calibration routine (amp_debug_run() in out_hal.h). The sample-clock ISR is NEVER
// started (setup() runs the debug loop and never returns), so there is zero pulse-timing
// ambiguity. It drives each board through the SAME complementary HAL as normal play — IN1 held
// HIGH, IN2 = PWM_DUTY_MAX - duty — with RAW commanded duty (the noise-shaper is BYPASSED), and
// prints, over USB Serial @ AMP_DEBUG_SERIAL_BAUD, the commanded duty + the intended DRIVE/BRAKE
// state per board — so a scope reading can be matched to the number the firmware THINKS it sent.
// Sequence, looping forever:
//   (0) ZERO        A=0    B=0    ~1 s   LED off          both braked: single-ended A & B must read HARD 0 V
//   (1) FULLSCALE A A=255  B=0    ~2 s   LED solid        A driven to rail, B braked to GND (diff A-B ~= rail)
//   (2) FULLSCALE B A=0    B=255  ~2 s   LED solid        the other driver / electrode (symmetry)
//   (3) SWEEP A     A=step B=0    ~1 s ea LED blinks(#+1) commanded duty -> volts LINEAR? 255 -> full rail?
// Decisive readings now that IN2 is driven complementary (coast is gone): (0) both channels must sit
// HARD at 0 V (braked, not floating/leaking); (1)/(2) full-scale must reach ~full rail; SWEEP A must
// be a straight line duty->volts. A residual shortfall on a real PULSE is then just the amplitude
// setting (loc/volley amp < 1 -> duty < 255). See firmware/AMPLITUDE_DEBUG.md.
#define AMP_DEBUG              0
#define AMP_DEBUG_HOLD_MS      1000u    // hold per sweep level / zero
#define AMP_DEBUG_FULL_MS      2000u    // hold per full-scale DC level
#define AMP_DEBUG_SERIAL_BAUD  115200u
#define AMP_DEBUG_SWEEP_LEVELS { 0, 32, 64, 96, 128, 160, 192, 224, 255 }  // 8-bit duty steps incl. 0 & 255
