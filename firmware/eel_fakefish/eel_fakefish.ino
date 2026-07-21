// Teensy 4.1 SD-card player for the fakefish stimulator (the only board we ship).
//
// The fakefish is a HAND-HELD field stimulator: the operator holds it and plays stimuli at
// a wild fish through a dipole; only the electrodes go in the water. Six buttons pick the
// program, an LED shows what is playing. Each stimulus is a pre-rendered mono int16 @ 50 kHz
// WAV on the SD card, organised one directory per button (see eel_control.h + sd_player.h);
// all stimulus generation is offline Python (src/fakefish/build_sd_card.py). A press lists
// the button's directory, picks a WAV at random, and streams it — applying a per-press
// random polarity flip (anti-pitting) and the field-tunable MASTER_GAIN — to out_write().
//
// PWM + an RC low-pass IS a DAC. Pins 2 and 3 are on the same FlexPWM submodule
// (phase-aligned carriers): ch_A = max(+w,0), ch_B = max(-w,0), so the two unipolar PWM+RC
// channels form one BIPOLAR differential output across the dipole. Which channel leads
// encodes polarity (randomised per press). out_write() and its noise shaping are UNCHANGED
// from the flash-library firmware — only the sample SOURCE changed (SD stream, not the
// overlap-add engine).
//
// !!! HARDWARE (enabling): the bridging cap must be 22 nF, NOT 220 nF. !!!
//   Differential RC: pin 2 -> 220 ohm -> node A -> electrode A; pin 3 -> 220 ohm ->
//   node B -> electrode B; a SINGLE cap BRIDGES node A<->node B (floating, not GND).
//   440 ohm (two 220 in series) + the bridging cap -> differential corner:
//     22 nF  -> fc = 16.4 kHz: EOD passes and the 10 kHz marker passes at -1.4 dB. CORRECT.
//     220 nF -> fc = 1.6 kHz: low-passes the EOD itself. WRONG -- swap the cap.
//   V4A stainless, few volts, pulsed -> no DC-blocking cap; per-press polarity flip keeps
//   the net charge near zero over a session (see README).
//
// RESOLUTION: 8-bit duty at the 585.9 kHz carrier (150 MHz FlexPWM clock / 256). out_write()
// applies first-order error-feedback NOISE SHAPING per channel (~+3 effective in-band bits).
//
// Self-contained sketch: this .ino + sd_player.h + eel_control.h all live in this folder.
// eel_player.{h,cpp} + eel_stimuli.{h,cpp} also live here but are NOT used by the firmware
// any more — they are the stimulus-library SOURCE that build_sd_card.py renders into the WAV
// card. host_test/ is a subdir the Arduino build ignores.
#include <IntervalTimer.h>
#include "eel_control.h"   // 6 program buttons (pins 5-10) + indicator LED (pin 13)
#include "sd_player.h"     // SD WAV streaming: parse, ring buffer, per-button random pick

#if !defined(__IMXRT1062__)
#warning "eel_fakefish targets the Teensy 4.1 (i.MX RT1062) — select that board in Tools > Board"
#endif

static const int   PWM_A_PIN      = 2;              // FlexPWM (submodule-shared with pin 3) -> RC -> electrode A
static const int   PWM_B_PIN      = 3;              // FlexPWM -> RC -> electrode B
static const float PWM_CARRIER_HZ = 585937.5f;      // 150 MHz FlexPWM clock / 256 (8-bit)
static const int   PWM_BITS       = 8;
static const int   SAMPLE_RATE_HZ = 50000;          // the card's WAV rate; ISR period == 20 us
static const int   ISR_PERIOD_US  = 1000000 / SAMPLE_RATE_HZ;  // 20 us

// ===== OUTPUT LEVEL — the one knob you tune ===============================
// Every stimulus WAV already carries its own ABSOLUTE level (the loc / volley / marker
// ratios are baked in by build_sd_card.py). MASTER_GAIN is a single overall trim applied to
// every streamed sample so you can raise or lower the whole device's output in the field
// without regenerating the card. 1.0 == the levels exactly as rendered; lower it if you are
// close to a recording electrode, raise it (up to 1.0) for reach. The open-circuit full
// scale is ~5.7 Vpp differential; in water the 440 ohm source impedance divides against the
// load, so scope it on the bench. (Values above 1.0 only clip — the WAVs already use the
// full-scale headroom.)
static const float MASTER_GAIN = 1.0f;

IntervalTimer sampleClock;
static SdPlayer sdplayer;
volatile bool playing = false;

// Per-channel first-order error-feedback noise-shaping accumulators. Reset per playback in
// start_playback() so stale error can't glitch the newly-idle channel after a polarity flip.
static volatile int32_t err_a = 0, err_b = 0;

static inline int shape(int32_t mag, volatile int32_t* err) {
  int32_t v = mag + *err;
  int32_t q = (v + 64) >> 7;         // round to nearest of 128 (full 8-bit: 32767->255)
  if (q < 0) q = 0;
  if (q > 255) q = 255;
  *err = v - (q << 7);               // carry the residual into the next sample
  return (int)q;
}

// out_write() is the platform seam: a signed int16 -> two phase-locked PWM+RC channels with
// noise shaping. UNCHANGED from the flash-library firmware.
static inline void out_write(int16_t s) {
  int32_t mag_a = (s > 0) ? s : 0;   // sign selects the electrode == polarity
  int32_t mag_b = (s < 0) ? -s : 0;
  analogWrite(PWM_A_PIN, shape(mag_a, &err_a));
  analogWrite(PWM_B_PIN, shape(mag_b, &err_b));
}

// One sample-clock tick: pop one sample from the ring, scale by gain + this playback's
// polarity, and drive out_write(). The ISR NEVER touches the SD card — loop() refills the
// ring (single-producer/single-consumer). When the file is fully read and the ring drains,
// tear down here (this path returns WITHOUT reaching out_write(), so clear the LED explicitly
// or it sticks on).
static void onSampleTick() {
  int16_t s;
  if (sdplayer.ring.pop(&s)) {
    out_write(sdp_apply(s, MASTER_GAIN, sdplayer.polarity));
  } else if (sdp_finished(&sdplayer)) {
    sampleClock.end();
    analogWrite(PWM_A_PIN, 0);
    analogWrite(PWM_B_PIN, 0);
    digitalWriteFast(LED_PIN, LOW);
    playing = false;
    return;
  } else {
    out_write(0);   // buffer underrun (SD stall) — emit silence, keep the cadence
  }
  digitalWriteFast(LED_PIN, HIGH);   // solid while playing
}

// Start streaming a random WAV from button `b`'s directory. Self-mutes (no clock) if that
// directory has no playable WAV. One polarity per playback (randomised for anti-pitting).
static void start_playback(int b) {
  const int8_t pol = random(2) ? 1 : -1;
  err_a = 0;
  err_b = 0;
  if (!sdp_start(&sdplayer, BTN_DIRS[b], (int)random(1 << 30), pol, SAMPLE_RATE_HZ)) {
    // A failed press is usually a spare/empty button, but it can also mean the card was
    // pulled while idle. Re-probe: sdp_begin() re-runs SD.begin(), which flips card_ok false
    // only if the card is truly gone (a present card with a missing/empty dir re-mounts fine
    // and stays card_ok), so loop() then drops into the no-card recovery branch.
    sdp_begin(&sdplayer);
    return;
  }
  playing = true;
  sampleClock.begin(onSampleTick, ISR_PERIOD_US);   // start the clock LAST
}

// Slow "no card" error blink (~1 Hz) so the operator sees a missing/failed SD card.
static void error_blink() {
  static uint32_t t0 = 0;
  static bool on = false;
  uint32_t now = millis();
  if (now - t0 >= 500u) {
    t0 = now;
    on = !on;
    digitalWriteFast(LED_PIN, on ? HIGH : LOW);
  }
}

void setup() {
  analogWriteResolution(PWM_BITS);
  analogWriteFrequency(PWM_A_PIN, PWM_CARRIER_HZ);
  analogWriteFrequency(PWM_B_PIN, PWM_CARRIER_HZ);
  analogWrite(PWM_A_PIN, 0);
  analogWrite(PWM_B_PIN, 0);
  randomSeed(analogRead(A0));
  eel_control_begin();   // configure the 6 button pins + the LED
  sdp_begin(&sdplayer);  // mount the SD card (retried in loop() if absent)
}

// Six one-shot program buttons — the button IS the program. One press while idle picks a
// random WAV from that button's SD directory and streams it once, start to finish. A press
// during a playback is ignored (uninterruptible). Buttons are debounced every loop so a held
// button cannot spurious-retrigger at playback end.
//
//   A  /A  calibration    B  /B  localization   C  /C  volley
//   D  /D  loc -> volley   E  /E  (spare)         F  /F  song
void loop() {
  if (!sdplayer.card_ok) {
    // No card mounted — flash an error and keep retrying so a late insert recovers.
    error_blink();
    static uint32_t last_try = 0;
    if (millis() - last_try >= 1000u) {
      last_try = millis();
      if (sdp_begin(&sdplayer)) digitalWriteFast(LED_PIN, LOW);  // recovered -> clear the blink
    }
    return;
  }
  const int b = eel_control_button_fired();   // ticks every button's debounce
  if (!playing && b != BTN_NONE) start_playback(b);
  if (playing) sdp_prefetch(&sdplayer);       // keep the ring ahead of the ISR
}
