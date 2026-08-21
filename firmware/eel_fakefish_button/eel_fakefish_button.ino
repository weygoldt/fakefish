// Teensy 4.1 fakefish stimulator — 6-button SD-card player (L3 surface on the shared core).
//
// The fakefish is a HAND-HELD field stimulator: the operator holds it and plays stimuli at
// a wild fish through a dipole; only the electrodes go in the water. Six buttons pick the
// program, an LED shows what is playing. Each stimulus is a pre-rendered mono int16 @ 50 kHz
// WAV on the SD card, organised one directory per button (see button_control.h +
// src/eel_core/sd_player.h); all stimulus generation is offline Python
// (src/fakefish/build_sd_card.py). A press lists the button's directory, picks a WAV at
// random, and streams it — applying a per-press random polarity flip (anti-pitting) and the
// field-tunable MASTER_GAIN — to the shared output HAL.
//
// LAYERING. This sketch is a CONTROL SURFACE (L3) only:
//   L1  src/eel_core/out_hal.h + config.h   the DRV8871 complementary output stage
//   L2  src/eel_core/sd_player.h            SD WAV streaming runtime
// src/eel_core/ is a COMMITTED COPY of firmware/eel_core/, produced by firmware/sync_core.sh —
// that is what makes this sketch self-contained for the IDE / arduino-cli / rsync-to-bench.
// The core also carries eel_player/eel_stimuli (the overlap-add engine and the stimulus
// library); this surface does not use them — they are the SOURCE build_sd_card.py renders the
// card from — and they simply link out of this binary.
//
// OUTPUT STAGE — MIGRATED. This device now runs the SAME 36 V DRV8871 complementary stage as
// the RC unit (two single-bridge drivers, IN1 held HIGH on pins 2/3, IN2 PWM'd at the
// complement of the wanted duty on pins 22/23, 100 kHz carrier, braked-not-coasting idle). It
// previously drove pins 2/3 directly at a 585.9 kHz carrier for an open-circuit full scale of
// ~5.7 Vpp.
//
// >>> AMPLITUDE WARNING — READ BEFORE THE FIRST BENCH RUN <<<
// Full-scale int16 now maps to the ~36 V rail, so the levels baked into the WAVs (0.90 volley
// / 0.45 localization) mean roughly 32 V / 16 V instead of the old few volts — about a SIX-FOLD
// jump in output. That is the intended consequence of the 36 V decision, not a bug, but it
// means this firmware must NOT be run on the old direct-pin hardware, and the absolute level
// must be re-scoped and set with MASTER_GAIN below before it goes near a fish.
#include <IntervalTimer.h>
#include "src/eel_core/config.h"     // L1: output stage, sample clock, LED pin
#include "src/eel_core/out_hal.h"    // L1: out_begin/out_write/out_arm/out_disarm (+ AMP_DEBUG)
#include "src/eel_core/sd_player.h"  // L2: SD WAV streaming — parse, ring buffer, random pick
#include "button_control.h"          // L3: 6 program buttons (pins 5-10) + indicator LED

// Supported parts: Teensy 4.1 (i.MX RT1062) and Teensy 3.5 (MK64FX512). The output stage's
// pinout is deliberately identical on both (see config.h), and config.h derives the PWM clock
// per part, so the same source builds for either — anything else has not been checked.
#if !defined(__IMXRT1062__) && !defined(__MK64FX512__)
#warning "eel_fakefish_button targets the Teensy 4.1 or 3.5 — select one of those in Tools > Board"
#endif

static const int ISR_PERIOD_US = 1000000 / SAMPLE_RATE_HZ;  // 20 us @ 50 kHz (the card's WAV rate)

// ===== OUTPUT LEVEL — the one knob you tune ===============================
// Every stimulus WAV already carries its own ABSOLUTE level (the loc / volley / marker
// ratios are baked in by build_sd_card.py). MASTER_GAIN is a single overall trim applied to
// every streamed sample so you can raise or lower the whole device's output in the field
// without regenerating the card. 1.0 == the levels exactly as rendered; lower it if you are
// close to a recording electrode, raise it (up to 1.0) for reach.
//
// SET THIS ON A SCOPE FIRST. On the 36 V DRV8871 stage full scale is the rail, so 1.0 is a
// far hotter output than this device produced before the migration (see the amplitude warning
// at the top of this file). In water the source impedance divides against the load, so the
// delivered level is lower and conductivity-dependent — scope it on the bench.
// (Values above 1.0 only clip — the WAVs already use the full-scale headroom.)
static const float MASTER_GAIN = 1.0f;

IntervalTimer sampleClock;
static SdPlayer sdplayer;
volatile bool playing = false;

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
    out_disarm();    // playback over: brake BOTH bridges to GND, clear the shaper, drop the pedestal
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
  out_arm();       // bridges live at the pedestal (out of the driver dead zone) + shaper cleared
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
  // Output stage first: out_begin() holds both DRV IN1 HIGH, sets the 100 kHz carrier on both
  // IN2 and brakes to the idle state. That ORDER is load-bearing (see out_hal.h).
  out_begin();
#if AMP_DEBUG
  amp_debug_run();   // never returns: replaces normal operation with the scope-calibration routine
#endif
  randomSeed(analogRead(A0));
  button_control_begin();   // configure the 6 button pins + the LED
  sdp_begin(&sdplayer);     // mount the SD card (retried in loop() if absent)
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
  const int b = button_control_fired();   // ticks every button's debounce
  if (!playing && b != BTN_NONE) start_playback(b);
  if (playing) sdp_prefetch(&sdplayer);   // keep the ring ahead of the ISR
}
