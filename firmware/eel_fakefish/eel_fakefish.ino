// Teensy 4.1 output driver for the eel stimulus library (the only board we ship).
//
// The fakefish is a HAND-HELD field stimulator: the operator holds it and plays eel
// stimuli at a wild eel through a dipole; only the electrodes go in the water. Four
// buttons pick the program, an LED shows what is playing. See eel_control.h.
//
// PWM + an RC low-pass IS a DAC. The 4.1 plays back the FULL mean EOD waveform —
// both phases, not an approximation — through the shared device-agnostic engine;
// only this driver's out_write() is board-specific. Pins 2 and 3 are
// on the same FlexPWM submodule (phase-aligned carriers): ch_A = max(+w,0),
// ch_B = max(-w,0), so the two unipolar PWM+RC channels form one BIPOLAR
// differential output across the dipole. Which channel leads encodes polarity
// (randomised between playbacks). The 4.1 is NOT restricted to monophasic pulses.
//
// !!! HARDWARE (enabling): the bridging cap must be 22 nF, NOT 220 nF. !!!
//   Differential RC: pin 2 -> 220 ohm -> node A -> electrode A; pin 3 -> 220 ohm ->
//   node B -> electrode B; a SINGLE cap BRIDGES node A<->node B (floating, not GND).
//   The two 220 add (440 ohm) in series with the bridging cap -> differential
//   corner fc = 1/(2*pi*440*C):
//     22 nF  -> fc = 16.4 kHz: EOD passes (flat <2 kHz, edge intact) and the 10 kHz
//               marker passes at -1.4 dB; 586 kHz carrier down -31 dB. CORRECT.
//     220 nF -> fc = 1.6 kHz: low-passes the EOD itself. WRONG -- swap the cap.
//   Differential source impedance = 440 ohm (bridging), less water-loading than
//   caps-to-GND. The bridging cap filters DIFFERENTIAL carrier only; if the recorder
//   is single-ended / water shares ground, add ~2.2-4.7 nF each node->GND too.
//   V4A stainless, few volts, pulsed -> no DC-blocking cap (see README).
//
// RESOLUTION: 8-bit duty at the 585.9 kHz carrier (150 MHz FlexPWM clock / 256).
// out_write() applies first-order error-feedback NOISE SHAPING per channel to
// exploit the ~10x oversampling (50 kHz updates vs a <5 kHz signal), recovering
// ~+3 effective bits in-band so the low-voltage pulses (~5 duty codes) render
// cleanly. 50 kHz is adequate (100 kHz gives no meaningful in-band gain — the
// residual is RC/quantization-limited, not sample-limited). Quantified in
// tools/simulate_firmware.py. Carrier residual after the RC ~6% pp; if that must
// drop, RAISE the carrier (7-bit/1.17 MHz), do NOT add a 2nd RC pole.
//
// Self-contained sketch: this .ino + eel_player.{h,cpp} + eel_control.h +
// eel_stimuli.{h,cpp} all live in this folder, so opening firmware/eel_fakefish/
// in the Arduino IDE compiles with zero file copying. host_test/ is a subdir the
// Arduino build ignores (not src/), so the g++ self-tests ride along untouched.
#include <IntervalTimer.h>
#include "eel_player.h"
#include "eel_control.h"   // 4 program buttons (pins 5-8) + indicator LED (pin 13)

// Board guard: __IMXRT1062__ is defined only for the i.MX RT1062 (Teensy 4.0/4.1).
// The PWM+RC output stage below assumes the 4.1's FlexPWM; a wrong-board build still
// compiles but won't drive the dipole correctly — so warn loudly at compile time.
#if !defined(__IMXRT1062__)
#warning "eel_fakefish targets the Teensy 4.1 (i.MX RT1062) — select that board in Tools > Board"
#endif

static const int PWM_A_PIN = 2;    // FlexPWM (submodule-shared with pin 3) -> RC -> electrode A
static const int PWM_B_PIN = 3;    // FlexPWM -> RC -> electrode B
static const float PWM_CARRIER_HZ = 585937.5f;  // 150 MHz FlexPWM clock / 256 (8-bit)
static const int PWM_BITS = 8;

// ===== OUTPUT LEVELS — the four knobs you tune ============================
// Every signal the device emits has its OWN absolute level on a 0..1 scale. They are
// independent on purpose: each answers a different physical constraint, and none of them
// scales any other, so what you set is what you get. The scale maps to the drive across
// the electrodes as (each phase drives one PWM rail; the sine LUT peaks at 31163/32767):
//
//     V_peak ~= 3.3 V * amplitude * LUT_peak/32640     (~3.15 V * amplitude for the sine)
//
// The output is BIPOLAR DIFFERENTIAL, so peak-to-peak is TWICE that — full scale is
// ~5.7 Vpp, NOT 3.3 V. The 16.4 kHz RC costs ~1.4 dB at the 10 kHz marker and is nearly
// flat over the EOD band (<2 kHz). These are OPEN-CIRCUIT figures: in water the 440 ohm
// source impedance divides against the
// electrode impedance, so the delivered level is lower and conductivity-dependent —
// scope it on the bench. A rough guide (sine; a pulse peaks at 32767 so ~5% hotter):
//     0.90 -> ~2.83 V peak / 5.67 Vpp
//     0.45 -> ~1.42 V peak / 2.84 Vpp
//     0.32 -> ~1.01 V peak / 2.02 Vpp
//     0.25 -> ~0.79 V peak / 1.58 Vpp
// Resolution is not a worry down here: 0.25 still peaks at duty code 61 of 255, and the
// noise shaper hands back ~3 more effective bits on top.

// VOLLEY items (C, and D's strike): the loud one. A hunting eel's strike is its
// highest-voltage discharge, so this is the reference level everything else sits under.
static const float VOLLEY_AMPLITUDE = 0.90f;

// LOCALIZATION items — used by BOTH B (localization alone) and D's lead-in localization,
// so the sensing discharge is identical however you reach it. An eel's localization
// discharge is genuinely lower-voltage at the source than its volley, and this contrast is
// the whole reason the two are separate: keep it BELOW VOLLEY_AMPLITUDE or a "localization"
// will read as a strike. Amplitude is a firmware concern by design — the recorded amplitude
// is distance-confounded, so it is deliberately left out of the stimulus library.
static const float LOC_AMPLITUDE = 0.45f;

// The 1 s sine LEAD-IN marker before every stimulus (B/C/D). Decoupled from the item
// levels: the marker serves recorded detection SNR, not behavioural realism, so a
// localization's lead-in is not obliged to be as quiet as the localization itself. It has
// to be detectable at whatever range you are playing from — raise it for reach, lower it
// if you are close enough to a recording electrode to clip that channel. (CALIBRATION does
// NOT use this — see MARKER_CAL_AMPLITUDE.)
static const float MARKER_AMPLITUDE = 0.25f;

// The 10 s CALIBRATION tone (A) — deliberately the quietest. Calibration is run with the
// stimulus electrodes held right next to a recording electrode, so the recorder sees the
// tone at essentially zero distance and a full-scale tone SATURATES its input amplifier.
// This and MARKER_AMPLITUDE answer OPPOSITE constraints — the lead-in must carry at range
// (loud), the calibration must not clip a recorder in contact with it (quiet) — which is
// why they are separate knobs. Cutting it costs nothing: the SNR case for a loud marker is
// a far-field argument, and at contact range there is SNR to burn.
static const float MARKER_CAL_AMPLITUDE = 0.25f;

IntervalTimer sampleClock;
volatile EelPlayer player;
volatile bool playing = false;

// ---- Session state --------------------------------------------------------
// A SESSION is one button press: an ordered segment list (tone -> gap -> item ...) that
// the sample clock walks, one sample per tick, to completion. begin_session() fills
// sess_segs[] COMPLETELY before starting the clock, and loop() only builds a new session
// while !playing, so the ISR is the sole reader of a list that never changes under it.
static Segment sess_segs[MAX_SESSION_SEGS];
static volatile uint8_t  sess_n_segs = 0;
static volatile uint8_t  seg_idx = 0;       // segment the ISR is currently emitting
static volatile uint32_t seg_t = 0;         // sample index within a TONE/SILENCE segment
static volatile int8_t   sess_polarity = 1; // ONE polarity per session (D's loc+volley share it)
static volatile float    sess_tone_amp = 0.0f;  // this session's marker level (cal is quieter)
static volatile uint32_t led_remaining = 0;            // blink countdown, in samples
static volatile uint32_t led_prev_onset = LED_ONSET_NONE;

// Per-channel first-order error-feedback noise-shaping accumulators (see header).
// Reset per session in begin_session() so stale error can't glitch the newly-idle
// channel after a polarity flip.
static volatile int32_t err_a = 0, err_b = 0;

static inline int shape(int32_t mag, volatile int32_t* err) {
  int32_t v = mag + *err;
  int32_t q = (v + 64) >> 7;         // round to nearest of 128 (full 8-bit: 32767->255)
  if (q < 0) q = 0;
  if (q > 255) q = 255;
  *err = v - (q << 7);               // carry the residual into the next sample
  return (int)q;
}

// out_write() is the platform seam: the shared engine hands it a signed int16;
// this (4.1) mapping splits the sign across two phase-locked PWM+RC channels with
// noise shaping.
static inline void out_write(int16_t s) {
  int32_t mag_a = (s > 0) ? s : 0;   // sign selects the electrode == polarity
  int32_t mag_b = (s < 0) ? -s : 0;
  analogWrite(PWM_A_PIN, shape(mag_a, &err_a));
  analogWrite(PWM_B_PIN, shape(mag_b, &err_b));
}

// Arm the engine for segment `i` (an ITEM). Called on ENTERING an item segment — including
// D's volley at the loc->volley seam, since two items cannot share one EelPlayer. Costs one
// ring clear (EOD_HV_LEN floats) once per item, far inside the 20 us tick budget.
static inline void arm_item(uint8_t i) {
  const Segment* sg = &sess_segs[i];
  const StimItem* it = &STIM_ITEMS[sg->item_index];
  // The level follows the item's own kind, so a localization plays at LOC_AMPLITUDE
  // whether it is program B or D's lead-in — they cannot drift apart.
  const float amp = item_amplitude_for_kind(it->kind, LOC_AMPLITUDE, VOLLEY_AMPLITUDE);
  eel_player_start_windowed((EelPlayer*)&player, it, amp, sess_polarity,
                            sg->n_samples, sg->loop);
  led_prev_onset = LED_ONSET_NONE;   // so the item's first pulse (last_onset == 0) blinks
  led_remaining = 0;
}

// Advance to the next segment, arming the engine if that segment is an item.
static inline void advance_segment() {
  seg_idx++;
  seg_t = 0;
  if (seg_idx < sess_n_segs && sess_segs[seg_idx].kind == SEG_ITEM) arm_item(seg_idx);
}

// One sample-clock tick: walk the session's segments, emitting exactly one sample.
// Empty segments (a zero-length gap, a finished item) are skipped within the same tick by
// the loop, so no dead ticks. The LED rides along: solid through a tone, one blink per
// pulse through an item, dark in a gap.
static void onSampleTick() {
  int16_t s = 0;
  bool led = false;
  for (;;) {
    if (seg_idx >= sess_n_segs) {
      // Session finished — tear down. This path returns WITHOUT reaching out_write(), so
      // the LED must be cleared explicitly here or it would stick on.
      sampleClock.end();
      analogWrite(PWM_A_PIN, 0);
      analogWrite(PWM_B_PIN, 0);
      digitalWriteFast(LED_PIN, LOW);
      playing = false;
      return;
    }
    const Segment* sg = &sess_segs[seg_idx];

    if (sg->kind == SEG_TONE) {
      if (seg_t < sg->n_samples) {
        s = marker_sample(seg_t, sg->n_samples, MARKER_RAMP_SAMPLES, sess_tone_amp);
        seg_t++;
        led = led_on_for_segment(SEG_TONE, 0);
        break;
      }
      advance_segment();
      continue;
    }

    if (sg->kind == SEG_SILENCE) {
      if (seg_t < sg->n_samples) {
        seg_t++;
        s = 0;
        led = led_on_for_segment(SEG_SILENCE, 0);
        break;
      }
      advance_segment();
      continue;
    }

    // SEG_ITEM — the engine owns the sample; the LED follows its pulse onsets.
    if (eel_player_next((EelPlayer*)&player, &s)) {
      bool onset = onset_fired(player.last_onset, (uint32_t*)&led_prev_onset);
      led_remaining = led_blink_step(led_remaining, onset, LED_BLINK_SAMPLES);
      led = led_on_for_segment(SEG_ITEM, led_remaining);
      break;
    }
    advance_segment();
  }
  out_write(s);
  digitalWriteFast(LED_PIN, led ? HIGH : LOW);
}

// Start a session for `prog`: draw the item(s) and ONE shared polarity, build the segment
// list, reset the noise-shaping accumulators (so leftover per-channel error can't glitch
// the newly-idle channel / inject DC at marker onset), then start the 50 kHz clock LAST —
// so the ISR can never observe a half-built list.
static void begin_session(EelProgram prog) {
  const int loc_idx = (prog == PROG_LOCALIZATION || prog == PROG_LOC_VOLLEY)
                        ? eel_control_pick_localization() : -1;
  const int vol_idx = (prog == PROG_VOLLEY || prog == PROG_LOC_VOLLEY)
                        ? eel_control_pick_volley() : -1;
  const uint8_t n = build_session(prog, loc_idx, vol_idx, sess_segs);
  if (n == 0) return;   // the library has no item of a class this program needs — self-mute

  sess_n_segs = n;
  sess_polarity = random(2) ? 1 : -1;   // per session; D's loc and volley share the one draw
  // Calibration is played against a recording electrode, so its tone is the quiet one.
  sess_tone_amp = marker_amplitude_for_program(prog, MARKER_AMPLITUDE, MARKER_CAL_AMPLITUDE);
  err_a = 0;
  err_b = 0;
  seg_idx = 0;
  seg_t = 0;
  led_remaining = 0;
  led_prev_onset = LED_ONSET_NONE;
  if (sess_segs[0].kind == SEG_ITEM) arm_item(0);   // (every program currently opens on a tone)
  playing = true;
  sampleClock.begin(onSampleTick, 20);   // 20 us period == STIM_SAMPLE_RATE_HZ (50 kHz)
}

void setup() {
  analogWriteResolution(PWM_BITS);
  analogWriteFrequency(PWM_A_PIN, PWM_CARRIER_HZ);
  analogWriteFrequency(PWM_B_PIN, PWM_CARRIER_HZ);
  analogWrite(PWM_A_PIN, 0);
  analogWrite(PWM_B_PIN, 0);
  randomSeed(analogRead(A0));
  eel_control_begin();   // configure the 4 button pins + the LED, build the item lists
}

// Four one-shot program buttons — the button IS the program (no mode switch to latch, no
// stop toggle). One press while idle plays that program once, start to finish:
//   A  CALIBRATION   -> the bare 10 s 10 kHz sine, at the QUIET MARKER_CAL_AMPLITUDE (it is
//                       played against a recording electrode) — a bench level/electrode
//                       check + the long tone that measures the recorder/playback clock drift
//   B  LOCALIZATION  -> a 1 s 10 kHz sine lead-in, the item's fixed per-item gap, then a
//                       random localization item, bounded to LOC_PLAYBACK_S, at
//                       LOC_AMPLITUDE
//   C  VOLLEY        -> the same lead-in + gap, then a random volley item, in full at
//                       VOLLEY_AMPLITUDE
//   D  LOC -> VOLLEY -> ONE lead-in + gap, a SHORT localization (D_LOC_PLAYBACK_S), a
//                       brief silence (D_INTERPHASE_GAP_MS), then a volley — the
//                       sense-then-strike motif, loc and volley sharing one polarity
// A press WHILE a session runs is IGNORED: playbacks are uninterruptible and always run to
// completion, so a marker in the recording is always followed by the pulses it announced.
// The lead-in marks every playback so downstream can locate it and pattern-match the known
// IPI sequence. Item polarity is randomised per session (the symmetric marker has none).
void loop() {
  const int prog = eel_control_program_fired();   // ticks every button's debounce
  if (prog != PROG_NONE && !playing) begin_session((EelProgram)prog);
}
