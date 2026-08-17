// Teensy 4.1 fakefish stimulator — RC + panel controlled (L3 surface on the shared core).
//
// A dipole EOD playback stimulator on a catamaran airboat, driven by an RC transmitter (FlySky
// FS-i6X / FS-iA6B) over an optically-isolated 4-channel PC817 link (see rc_control.h) — AND,
// with the same binary and no transmitter, by three panel buttons for the bench (panel_control.h).
// Both input sources are OR-ed. It LIVE-GENERATES its stimuli over the mean-EOD waveform (EOD_HV):
//
//   CH3 throttle  -> localization ON/OFF (dead-band) + RATE 0..20 Hz (a continuous train)
//   CH4 trigger   -> one-shot: throw high = VOLLEY, throw low = SHAM (marker, no water output)
//   CH5 pot       -> localization JITTER          CH6 pot -> AMPLITUDE (sets volley/max; loc = half)
//
// A VOLLEY and a SHAM both begin with a coded PULSE MARKER preamble (a short EOD burst, tagged by
// pulse count) so the recording can tell them apart; the volley then plays the discharge, the
// sham stays silent (the no-stimulus control) while the LED shows a distinct pattern. NOTE this
// is the PULSE-burst marker, a different mechanism from the SD device's 10 kHz sine tone — see
// shared/stim_constants.json.
//
// LAYERING. This sketch is a CONTROL SURFACE (L3) only. It owns the input decode, the session
// state machine and the ISR that wires them together; it does NOT own the output stage or the
// sample producers:
//   L1  src/eel_core/out_hal.h + config.h   the DRV8871 complementary output stage (36 V, 100 kHz)
//   L2  src/eel_core/eel_player.{h,cpp}     overlap-add engine (marker + volley)
//       src/eel_core/locgen.h               live localization scheduler
//       src/eel_core/eel_stimuli.{h,cpp}    the generated stimulus library
// src/eel_core/ is a COMMITTED COPY of firmware/eel_core/, produced by firmware/sync_core.sh —
// that is what makes this sketch self-contained for the IDE / arduino-cli / rsync-to-bench.
//
// CONCURRENCY MODEL. The 50 kHz sample-clock ISR (onSampleTick) is the SINGLE OWNER of all
// playback-engine state (the localization scheduler + the shared EelPlayer used for the marker
// and the volley). loop() only decodes the RC/panel inputs and publishes target parameters +
// a trigger request through volatile words, LATCHED by the ISR at pulse boundaries. Each shared
// value is a single aligned 32-bit word (atomic on the M7), written by exactly one side. The
// sample clock is given higher priority than the RC pin ISRs so pulse OUTPUT timing is never
// perturbed by input capture.
#include <IntervalTimer.h>
#include "src/eel_core/config.h"        // L1: output stage, sample clock, LED pin
#include "src/eel_core/stim_levels.h"   // generated playback constants (PULSE_MARKER_*, PANEL_*)
#include "src/eel_core/out_hal.h"       // L1: out_begin/out_write/out_silence (+ AMP_DEBUG)
#include "src/eel_core/eel_stimuli.h"   // L2: EOD_HV[] + STIM_ITEMS[] (volley snippets)
#include "src/eel_core/eel_player.h"    // L2: shared overlap-add engine (marker + volley playback)
#include "src/eel_core/locgen.h"        // L2: live localization scheduler
#include "panel_control.h"              // L3: three panel buttons + the LED feedback vocabulary
#include "rc_control.h"                 // L3: 4-channel RC decode + conditioning

#if !defined(__IMXRT1062__)
#warning "eel_fakefish_rc targets the Teensy 4.1 (i.MX RT1062) — select that board in Tools > Board"
#endif

static const float ISR_PERIOD_US = 1000000.0f / (float)SAMPLE_RATE_HZ;   // 20.0 us (float -> unambiguous begin())

IntervalTimer sampleClock;

// ===== published targets (loop() writes, ISR latches) =====================
static volatile bool     g_loc_enabled = false;    // CH3 throttle above dead-band OR panel loc toggle
static volatile uint32_t g_trig_seq    = 0;        // bumped once per trigger throw (volley/sham)
static volatile int      g_trig_kind   = RC_TRIG_NONE;  // which throw: RC_TRIG_VOLLEY / RC_TRIG_SHAM
static volatile float    g_rate_ipi_samp = 0.0f;   // mean localization IPI (samples) for the set rate
static volatile float    g_cv          = 0.0f;     // localization jitter (coefficient of variation)
static volatile float    g_volley_amp  = 0.0f;     // volley (max) amplitude 0..1 — the amplitude control sets THIS
static volatile float    g_loc_amp     = 0.0f;     // localization amplitude = volley / VOLLEY_AMP_RATIO (always half)
static volatile bool     g_link_up     = false;    // CH3 (throttle) delivering edges
static volatile bool     g_rc_ever     = false;    // RC has been present at least once
static volatile bool     g_playing     = false;    // ISR is emitting a playback (owns the LED then)

// ===== ISR-owned playback state ===========================================
enum Source { SRC_IDLE, SRC_LOC, SRC_MARKER, SRC_VOLLEY, SRC_SHAM };
static volatile Source src = SRC_IDLE;
static EelPlayer  player;                           // marker + volley engine (touched ONLY in the ISR)
static LocGen     loc;                               // localization scheduler (ISR only)
static uint32_t   g_trig_seen = 0;                   // last consumed trigger seq (ISR only)
static int        g_post_marker = RC_TRIG_NONE;      // what follows the marker: VOLLEY burst / SHAM
static int8_t     g_playback_pol = 1;                // polarity shared by a marker + its volley
static float      g_playback_volley_amp = 0.0f;      // volley amplitude latched at the throw (volley start)
static uint32_t   g_tick = 0;                        // monotonic sample counter (ISR only)
static uint32_t   g_led_off_at = 0;                  // g_tick at which the per-pulse flash ends
static uint32_t   g_marker_onset = 0;                // detect marker pulse onsets for the LED
static uint32_t   g_volley_onset = 0;                // detect volley pulse onsets for the LED
static uint32_t   g_sham_phase = 0;                  // drives the distinct SHAM LED pattern

// The marker StimItem: a short EOD burst at a fixed IPI, count = the volley/sham tag.
static uint16_t   g_marker_ipi[PULSE_MARKER_MAX_PULSES];
static StimItem   g_marker_item;

// ----- LED (per-pulse flash from the KNOWN onset; never a |sample| threshold) --
static inline void led_flash() { digitalWriteFast(LED_PIN, HIGH); g_led_off_at = g_tick + RC_LED_FLASH_SAMP; }
static inline void led_service() { if ((int32_t)(g_tick - g_led_off_at) >= 0) digitalWriteFast(LED_PIN, LOW); }

// ----- random helpers (called ONLY from the ISR -> single-caller, no lock) --
static inline int8_t rand_polarity() { return random(2) ? (int8_t)1 : (int8_t)-1; }
static inline uint32_t draw_loc_ipi() {
  float u1 = (float)(random(1, 1000001)) * 1e-6f;   // (0,1]
  float u2 = (float)(random(1000000)) * 1e-6f;
  float z  = rc_std_normal(u1, u2);
  return rc_ipi_samples(g_rate_ipi_samp, g_cv, z, LOC_REFRACTORY_SAMP);
}
static inline bool trig_pending() { return g_trig_seq != g_trig_seen; }

// ----- ISR-side state transitions (single owner) --------------------------
static inline void begin_loc() {
  out_silence();
  locgen_reset(&loc, draw_loc_ipi(), g_loc_amp, rand_polarity());
  src = SRC_LOC;
  g_playing = true;
}
// A trigger (volley or sham) always starts with the coded marker; g_post_marker records what
// follows. One polarity is chosen here and shared by the marker + the volley it precedes.
static inline void begin_marker(int kind) {
  out_silence();
  g_playback_pol = rand_polarity();
  g_playback_volley_amp = g_volley_amp;   // latch volley amplitude at the throw, held for the volley
  g_marker_item.n = (uint16_t)((kind == RC_TRIG_VOLLEY) ? PULSE_MARKER_PULSES_VOLLEY : PULSE_MARKER_PULSES_SHAM);
  eel_player_start_item(&player, &g_marker_item, PULSE_MARKER_AMP, g_playback_pol);
  g_marker_onset = 0xFFFFFFFFu;
  g_post_marker = kind;
  g_trig_seen = g_trig_seq;   // consume the request (one playback per throw; later throws ignored)
  src = SRC_MARKER;
  g_playing = true;
}
static inline void begin_volley_burst() {
  out_silence();
  uint8_t idx = (uint8_t)(RC_VOLLEY_ITEM_FIRST + (int)random(RC_VOLLEY_ITEM_COUNT));
  eel_player_start_item(&player, &STIM_ITEMS[idx], g_playback_volley_amp, g_playback_pol);   // latched amp + marker's polarity
  g_volley_onset = 0xFFFFFFFFu;
  src = SRC_VOLLEY;
}
static inline void begin_sham() {   // no water output — just the distinct LED pattern
  out_silence();
  g_sham_phase = 0;
  src = SRC_SHAM;
}
static inline void go_idle() {
  out_silence();
  src = SRC_IDLE;
  g_playing = false;
}
// After a volley/sham completes: discard any throw made DURING it (ignored, not queued), then
// resume localization if the throttle still commands it, else idle.
static inline void resume_after_playback() {
  g_trig_seen = g_trig_seq;
  if (g_loc_enabled) begin_loc();
  else go_idle();
}

// One 50 kHz sample tick. Single owner of the playback engine.
static void onSampleTick() {
  g_tick++;
  switch (src) {
    case SRC_MARKER: {
      int16_t s;
      if (eel_player_next(&player, &s)) {
        if (s == 0) out_silence(); else out_write(s);
        if (player.last_onset != g_marker_onset) { g_marker_onset = player.last_onset; led_flash(); }
        led_service();
      } else {
        if (g_post_marker == RC_TRIG_VOLLEY) begin_volley_burst();
        else begin_sham();   // RC_TRIG_SHAM
      }
      break;
    }
    case SRC_VOLLEY: {
      int16_t s;
      if (eel_player_next(&player, &s)) {
        if (s == 0) out_silence(); else out_write(s);
        if (player.last_onset != g_volley_onset) { g_volley_onset = player.last_onset; led_flash(); }
        led_service();
      } else {
        resume_after_playback();   // link loss NEVER aborts a volley — it runs to the end here
      }
      break;
    }
    case SRC_SHAM: {
      // No electrode output. A distinct blink pattern so a fired sham is visible from shore.
      out_silence();
      uint32_t period = SHAM_LED_ON_SAMP + SHAM_LED_OFF_SAMP;
      uint32_t total  = (uint32_t)SHAM_LED_BLINKS * period;
      if (g_sham_phase < total) {
        digitalWriteFast(LED_PIN, ((g_sham_phase % period) < SHAM_LED_ON_SAMP) ? HIGH : LOW);
        g_sham_phase++;
      } else {
        digitalWriteFast(LED_PIN, LOW);
        resume_after_playback();
      }
      break;
    }
    case SRC_LOC: {
      // In the silent gap (don't truncate a loc EOD mid-flight): honour a pending trigger OR a
      // localization-disable promptly rather than waiting for the interval boundary.
      if (loc.phase >= (uint32_t)EOD_HV_LEN) {
        if (trig_pending()) { begin_marker(g_trig_kind); break; }
        if (!g_loc_enabled) { go_idle(); break; }
      }
      int onset, boundary;
      int16_t s = locgen_tick(&loc, &onset, &boundary);
      if (s == 0) out_silence(); else out_write(s);
      if (onset) led_flash();
      led_service();
      if (boundary) {
        if (!g_loc_enabled) { go_idle(); }
        else { loc.ipi = draw_loc_ipi(); loc.amp = g_loc_amp; }   // latch for the next pulse
      }
      break;
    }
    default: {  // SRC_IDLE
      out_silence();
      if (trig_pending()) { begin_marker(g_trig_kind); }
      else if (g_loc_enabled) { begin_loc(); }
      // LED in idle is owned by loop() (dark, or the no-RC-signal blink).
      break;
    }
  }
}

void setup() {
  // Output stage first: out_begin() holds both DRV IN1 HIGH, sets the 100 kHz carrier on both IN2
  // and brakes to the idle state. That ORDER is load-bearing (see out_hal.h) — never open-code it.
  out_begin();
#if AMP_DEBUG
  amp_debug_run();   // never returns: replaces normal operation with the scope-calibration routine
#endif
  randomSeed(analogRead(A0));                         // A0 == digital 14; unused elsewhere
  panel_begin();                                     // 3 panel buttons + the LED (pin 13)
  rc_begin();                                        // 4 RC pins + pin-change interrupts

  // Build the coded marker: a fixed-IPI EOD burst (its pulse count is set per fire).
  g_marker_ipi[0] = 0;
  for (uint16_t i = 1; i < PULSE_MARKER_MAX_PULSES; i++) g_marker_ipi[i] = PULSE_MARKER_IPI_SAMP;
  g_marker_item.ipi_samp = g_marker_ipi;
  g_marker_item.rel_amp = NULL;
  g_marker_item.n = PULSE_MARKER_PULSES_VOLLEY;
  g_marker_item.kind = 0;
  g_marker_item.group = 0;

  // Panel defaults so a bench unit (no transmitter) has a rate/jitter/amplitude.
  g_rate_ipi_samp = (float)SAMPLE_RATE_HZ / PANEL_RATE_HZ;
  g_cv = PANEL_CV;
  g_volley_amp = PANEL_VOLLEY_AMP;
  g_loc_amp    = rc_loc_amp(PANEL_VOLLEY_AMP);   // localization = half the volley

  sampleClock.begin(onSampleTick, ISR_PERIOD_US);    // continuous 50 kHz clock (runs forever)
  sampleClock.priority(64);                          // above the RC pin ISRs (default 128): protect pulse timing
}

// A trigger request from RC or panel. Set the kind BEFORE bumping the seq so the ISR sees a
// consistent (kind, seq) pair. A held/bouncing stick can't re-fire (loop() edge-detects it).
static inline void request_trigger(int kind) { g_trig_kind = kind; g_trig_seq = g_trig_seq + 1; }

// Distinct "no RC signal" LED pattern (two quick blinks per second), shown only after RC was
// present at least once (a bench unit that never had a transmitter stays dark). loop()-owned,
// only while the ISR is not emitting a playback.
static inline void no_signal_blink(uint32_t now_ms) {
  uint32_t ph = now_ms % NOSIG_LED_PERIOD_MS;
  bool on = (ph < NOSIG_BLINK_MS) ||
            (ph >= NOSIG_BLINK_SPACING_MS && ph < NOSIG_BLINK_SPACING_MS + NOSIG_BLINK_MS);
  digitalWriteFast(LED_PIN, on ? HIGH : LOW);
}

// Per-channel presence by CHANGE-DETECTION on the ISR edge timestamp (rc_last_edge_us): present
// while the timestamp keeps advancing. Compares a ms clock read every call to a recently-updated
// change time, so it stays wrap-safe for ~49 days of sustained loss — a raw micros() diff against
// the FROZEN edge timestamp would wrap at ~71 min and momentarily read "present" again. loop()-
// owned static state; single caller.
static bool rc_present_ms(uint8_t i, uint32_t now_ms) {
  static uint32_t snap[RC_N_CHANNELS] = {0};
  static uint32_t change_ms[RC_N_CHANNELS] = {0};
  static uint8_t  seeded = 0;
  uint32_t s = rc_last_edge_us(i);
  if (!(seeded & (1u << i)) || s != snap[i]) {
    seeded |= (uint8_t)(1u << i);
    snap[i] = s;
    change_ms[i] = now_ms;
  }
  return s != 0 && (uint32_t)(now_ms - change_ms[i]) < RC_ABSENCE_MS;
}

// loop() decodes the RC channels + panel and publishes targets; it never touches engine state.
void loop() {
  // ----- panel buttons (OR-ed with RC; every loop so a held button can't retrigger) -----
  static bool panel_loc_state = false;
  int loc_fell, volley_fell, sham_fell;
  panel_poll(&loc_fell, &volley_fell, &sham_fell);
  if (loc_fell)    panel_loc_state = !panel_loc_state;
  if (volley_fell) request_trigger(RC_TRIG_VOLLEY);
  if (sham_fell)   request_trigger(RC_TRIG_SHAM);

  // ----- RC decode, rate-limited to ~200 Hz -----
  static uint32_t last_decode_ms = 0;
  static bool     primed = false;
  static bool     rc_loc_state = false;
  static uint32_t thr_on_count = 0;   // consecutive above-CH3_ON_THRESH decode ticks (throttle-on debounce)
  static float    u_thr = 0.0f, u_trig = 0.5f, u_jit = 0.0f, u_amp = 0.0f;
  static int      rate_level = 0, jit_level = 0, amp_level = 0;
  static RcTrigger trig = {};
  static bool     trig_prev_present = false;

  uint32_t now_ms = millis();
  if ((uint32_t)(now_ms - last_decode_ms) >= RC_DECODE_PERIOD_MS) {
    last_decode_ms = now_ms;

    bool thr_present  = rc_present_ms(RC_IDX_THROTTLE, now_ms);   // the primary / failsafe channel
    bool trig_present = rc_present_ms(RC_IDX_TRIGGER,  now_ms);
    bool jit_present  = rc_present_ms(RC_IDX_JITTER,   now_ms);
    bool amp_present  = rc_present_ms(RC_IDX_AMP,      now_ms);
    bool trig_acquired = trig_present && !trig_prev_present;   // CH4 absent -> present edge
    trig_prev_present = trig_present;
    g_link_up = thr_present;

    if (thr_present) {
      g_rc_ever = true;
      float raw_thr  = rc_unit(rc_get_width_us(RC_IDX_THROTTLE), RC_CAL[RC_IDX_THROTTLE]);
      float raw_trig = rc_unit(rc_get_width_us(RC_IDX_TRIGGER),  RC_CAL[RC_IDX_TRIGGER]);
      float raw_jit  = rc_unit(rc_get_width_us(RC_IDX_JITTER),   RC_CAL[RC_IDX_JITTER]);
      float raw_amp  = rc_unit(rc_get_width_us(RC_IDX_AMP),      RC_CAL[RC_IDX_AMP]);

      if (!primed) {
        // FIRST decode after (re)acquisition: SNAP filters to the live readings (no EMA ramp) and
        // re-init the trigger, so the true stick positions decode on tick 1 (no ramp lag, and the
        // trigger arms from the ACTUAL axis position -> no boot-time volley/sham).
        primed = true;
        u_thr = raw_thr;
        if (trig_present) u_trig = raw_trig;
        if (jit_present)  u_jit  = raw_jit;
        if (amp_present)  u_amp  = raw_amp;
        trig = {};
        rate_level = rc_quantize_hyst(rc_throttle_frac(u_thr), rate_level, RC_RATE_STEPS,   0.0f);
        jit_level  = rc_quantize_hyst(u_jit,  jit_level,  RC_JITTER_STEPS, 0.0f);
        amp_level  = rc_quantize_hyst(u_amp,  amp_level,  RC_AMP_STEPS,    0.0f);
      } else {
        u_thr = rc_ema(u_thr, raw_thr, RC_EMA_ALPHA);
        if (trig_acquired) { u_trig = raw_trig; trig = {}; }   // CH4 re-acquired mid-run: snap + arm from truth
        else if (trig_present) u_trig = rc_ema(u_trig, raw_trig, RC_EMA_ALPHA);
        if (jit_present)  u_jit  = rc_ema(u_jit,  raw_jit,  RC_EMA_ALPHA);
        if (amp_present)  u_amp  = rc_ema(u_amp,  raw_amp,  RC_EMA_ALPHA);
        rate_level = rc_quantize_hyst(rc_throttle_frac(u_thr), rate_level, RC_RATE_STEPS,   RC_QUANT_HYST);
        jit_level  = rc_quantize_hyst(u_jit,  jit_level,  RC_JITTER_STEPS, RC_QUANT_HYST);
        amp_level  = rc_quantize_hyst(u_amp,  amp_level,  RC_AMP_STEPS,    RC_QUANT_HYST);
      }

      // CH3: localization on/off (DEBOUNCED + hysteretic, gated on the RAW reading so an RC noise glitch
      // can never fire a stray pulse at rest) + rate. Enable needs raw_thr >= CH3_ON_THRESH held for
      // CH3_ON_DEBOUNCE_TICKS ticks; disable is immediate below CH3_OFF_DEADBAND.
      rc_loc_state = rc_throttle_gate(raw_thr, rc_loc_state, &thr_on_count,
                                      CH3_ON_THRESH, CH3_OFF_DEADBAND, CH3_ON_DEBOUNCE_TICKS);
      g_rate_ipi_samp = rc_rate_to_ipi_samp(rate_level, RC_RATE_STEPS);
      // CH5: jitter (pot; jit_level held on CH5 loss -> g_cv holds)
      g_cv = rc_jitter_cv(jit_level, RC_JITTER_STEPS);
      // CH6: amplitude pot -> VOLLEY (max) level; localization is derived as half the volley
      float amp = rc_master_amp(amp_level, RC_AMP_STEPS);
      g_volley_amp = amp;
      g_loc_amp    = rc_loc_amp(amp);
      // CH4: bidirectional one-shot trigger (only on a live channel)
      if (trig_present) {
        int fire = rc_trigger_step(&trig, u_trig, CH4_VOLLEY_THRESH, CH4_SHAM_THRESH,
                                   CH4_CENTER_LO, CH4_CENTER_HI);
        if (fire != RC_TRIG_NONE) request_trigger(fire);
      }
    } else {
      // CH3 (throttle) signal loss -> throttle zero -> localization OFF. The trigger never fires
      // without a live throw, so loss can NEVER start a volley/sham. Re-prime on reacquisition.
      primed = false;
      rc_loc_state = false;
      thr_on_count = 0;   // re-debounce the throttle-on when the channel is reacquired
    }
    g_loc_enabled = rc_loc_state || panel_loc_state;
  }

  // ----- idle LED (loop owns the LED only while the ISR is not emitting a playback) -----
  if (!g_playing) {
    if (g_rc_ever && !g_link_up) no_signal_blink(now_ms);
    else digitalWriteFast(LED_PIN, LOW);
  }
}
