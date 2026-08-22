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
#define DRV_A_IN2_PIN    22                // board A IN2 — 100 kHz PWM (4.1 FlexPWM4_0_A / 3.5 FTM0_CH0)
#define DRV_B_IN1_PIN    3                 // board B IN1 — held HIGH  -> DRV8871 B -> OUT1 -> electrode B
#define DRV_B_IN2_PIN    23                // board B IN2 — 100 kHz PWM (4.1 FlexPWM4_1_A / 3.5 FTM0_CH1)
//
// WHY 22/23. This pinout is deliberately one that works UNCHANGED on a Teensy 4.1 and a Teensy 3.5,
// so one wiring harness serves both boards. Three constraints pick it:
//   * PWM on BOTH parts. Pins 0/1 — which IN2 used until 2026-08-21 — are FlexPWM on the 4.1 but
//     have no FTM channel at all on the 3.5 (MK64FX512), so they cannot carry the carrier there.
//     Taking the two cores' PWM tables (cores/teensy4/pwm.c, cores/teensy3/pins_teensy.c) the pins
//     that are PWM on both are 2,3,4,5,6,7,8,9,10,14,22,23,29,36,37.
//   * NOT a QuadTimer on the 4.1. Pin 14 is QuadTimer3_2 there; every other candidate is FlexPWM,
//     which is what keeps the two boards' carriers uniform with each other and with their DC IN1s.
//   * Free on both control surfaces. 2..10 are taken (IN1s, the RC decode pins 4..7, the RC panel's
//     buttons on 9..11, the button surface's keys on 5..10), which leaves 22, 23, 29, 36, 37 — and
//     22/23 are the only adjacent pair on the top edge of both boards. It also hands hardware
//     Serial1 (RX1/TX1 on pins 0/1) back, which the old pinout was the sole claimant of.
//
// !! THE BUILT 4.1 BOARD MUST BE REWIRED: IN2 moves from pins 0/1 to 22/23 (two wires).
//
// AN UN-REWIRED BOARD IS NOT INERT. IT SITS IN PERMANENT FULL-RAIL DC DRIVE.
// (This warning used to say such a board would "put NOTHING in the water". That was wrong, and
// dangerously so — it reads as a harmless no-op. Field-confirmed 2026-08-21: it cooked the
// output filter's resistors within seconds of connecting the battery.)
//
// The mechanism: out_begin() holds IN1 HIGH and drives the carrier on 22/23. If the drivers'
// IN2 is wired anywhere else, that pin is left in the Teensy's power-on default — a floating
// input — and the DRV8871's internal pull-down on its logic inputs takes IN2 LOW. IN1 high +
// IN2 low is the DRV8871's DRIVE state, so both bridges hold OUT1 at the 36 V rail
// continuously, from the moment setup() runs. Not pulses: DC.
//
// It is also completely outside firmware control. Every command — including the log-fault
// gate that is supposed to suppress all output — goes to pins the drivers cannot hear, so
// nothing the firmware does will brake a bridge it is not wired to. A device in this state
// looks entirely healthy from the LED, and pulling the SD card changes nothing.
//
// CHECK IN1/IN2 ORIENTATION TOO. Swapping them survives the idle state — the firmware writes
// PWM_DUTY_MAX at silence, so a swapped pair still reads high/high and still brakes, which
// means no heat and no smoke to warn you. During a pulse, though, the bridge drives OUT2
// instead of OUT1, and the electrodes hang off OUT1. Swap BOTH bridges and the device is
// silent: both sides do the same thing, so it is pure common mode and nothing appears across
// the dipole. Swap only ONE and it is worse than silent — you get a real signal at INVERTED
// polarity on that side, which works, looks fine, and quietly puts a sign error in every
// recording. (Both failure modes field-confirmed on the same build, 2026-08-21.)
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
//     The source clock is a different thing on each part, so it is derived per part:
//       * Teensy 4.1 (i.MX RT1062): FlexPWM runs off F_BUS_ACTUAL = 150 MHz at the stock 600 MHz.
//         F_BUS_ACTUAL is a runtime variable there, not a constant expression, so the ceiling is
//         pinned as a documented constant and the 600 MHz assumption is asserted.
//       * Teensy 3.5 (MK64FX512): FTM runs off the bus clock, which Teensyduino DOES expose as the
//         compile-time F_BUS (kinetis.h) — 60 MHz at the stock 120 MHz CPU. 100 kHz x 256 =
//         25.6 MHz of that, so all 8 duty bits survive there too, with ~2.3x headroom instead of
//         ~5.9x. Reading F_BUS rather than pinning a number means an overclocked or underclocked
//         3.5 re-derives itself instead of silently dropping duty bits.
#if defined(__IMXRT1062__)
static constexpr float PWM_TIMER_CLOCK_HZ = 150000000.0f;  // FlexPWM source (F_BUS_ACTUAL @ 600 MHz)
static_assert(F_CPU == 600000000,
              "PWM_TIMER_CLOCK_HZ assumes F_BUS_ACTUAL = 150 MHz (Teensy 4.1 @ 600 MHz); re-derive it "
              "for another CPU speed (F_BUS_ACTUAL = F_CPU / ceil(F_CPU / 150e6))");
#elif defined(F_BUS)
static constexpr float PWM_TIMER_CLOCK_HZ = (float)F_BUS;   // Teensy 3.x: FTM is clocked from the bus
#else
static constexpr float PWM_TIMER_CLOCK_HZ = 150000000.0f;   // host g++ (self-tests): no F_CPU/F_BUS
#endif
static_assert(PWM_CARRIER_HZ * (float)(1u << PWM_BITS) <= PWM_TIMER_CLOCK_HZ,
              "PWM_CARRIER_HZ too high for full PWM_BITS duty resolution on this part's PWM clock");

// ===== The driver dead zone — measured, and NOT compensated ================
// THE PHYSICS IS REAL, THE FIX WAS WORSE THAN THE PROBLEM. Keep the first half, do not
// reintroduce the second.
//
// `analogWrite()` maps duty q to an IN2-LOW (= DRIVE) time of (q+1)/256 of the carrier period.
// At PWM_CARRIER_HZ = 100 kHz that period is 10 us, so duty 0 commands a 39 ns drive pulse,
// duty 4 commands 195 ns, duty 11 commands 469 ns. The DRV8871 needs a MINIMUM INPUT PULSE
// WIDTH to respond at all — datasheet SLVSCY9B, Recommended Operating Conditions footnote 1:
// "The voltages applied to the inputs should have at least 800 ns of pulse width to ensure
// detection. Typical devices require at least 400 ns." The bottom duty codes are therefore in a
// region the datasheet does not specify, and the flanks and negative pre-potential of every EOD
// sit there. That much is true and worth knowing.
//
// WHAT WAS TRIED (2026-08-21, commit 3af9f33, REVERTED 2026-08-22). Both bridges were held at
// OUT_PEDESTAL_DUTY = 21 — the datasheet-GUARANTEED 800 ns bound — whenever the device was armed
// to emit, with the signal scaled into the codes above it. The offset was identical on both
// electrodes, so on paper it was pure common mode and cancelled across the dipole.
//
// WHAT THE BENCH SAID (Teensy 3.5, pins 22/23, 36 V, the built 2x 220R/220nF filter). One
// variable, both directions, decisive:
//
//     pedestal 21 -> output-filter wiring runs HOT, and the signal in water is a cacophony
//     pedestal  0 -> clean pulses, no noise, and the amplitude pot works to the BOTTOM of its
//                    travel without cutting out — the very failure the pedestal was added to fix
//
// So the dead-zone shutoff does not reproduce on this hardware at all: these DRV8871s sit near
// the 400 ns TYPICAL figure, not the 800 ns guarantee. The pedestal was justified against
// worst-case silicon and an ear observation made on a DIFFERENT output path (Teensy 4.1, IN2 on
// pins 0/1 = FlexPWM1 X-channel). It was never verified on the path it shipped to.
//
// WHY IT WAS SO DESTRUCTIVE. Idle stopped being a hard brake. Both bridges switched a 36 V
// square continuously — through every silence of a localization train, which is >98 % of it —
// putting ~0.47 W per channel into the first filter resistor where there had been none, and
// leaving both electrodes at ~3.1 V DC plus carrier residue instead of at 0 V. The filter is
// series-R / shunt-C, so it passes that DC untouched, and firmware/README.md's own recording
// model is SINGLE-ENDED: "common mode" is not "silent" for anything listening in that water.
//
// DO NOT REINTRODUCE IT, AND DO NOT TRY A SMALLER ONE. The bench A/B proves the pedestal caused
// both symptoms; it does not prove which path carried the heat. Arithmetic says the first series
// resistor must dissipate 24-1100x more than the ground return at EVERY value of R1, so either
// R1 was the hot part, or current was bypassing R1 through the bridge — cross-conduction at the
// 867 ns detection knee, which is unspecified silicon behaviour rather than a spec violation.
// A smaller pedestal moves CLOSER to that knee, not further from it. Any future attempt has to
// solve the common-mode problem first (the electrodes must return to 0 V between pulses), not
// merely pick a gentler number.
//
// `OUT_PEDESTAL_DUTY`, `OUT_SIGNAL_DUTY_MAX`, `out_scale_to_headroom()`, `out_duty_for()`,
// `out_arm()`, `out_disarm()`, `out_armed()`, `out_idle_duty()` and `shape()`'s `qmax` parameter
// are **gone** — do not reintroduce those names. Idle is once again an unconditional brake to
// GND on both bridges (out_hal.h `out_silence()`), which is the state the hardware is known good
// in. AMP_DEBUG below is retained: it is the right way to measure a given board's real threshold,
// and it is independent of any compensation scheme.

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
