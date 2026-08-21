// Teensy 4.1 fakefish stimulator — RC + panel controlled (L3 surface on the shared core).
//
// A dipole EOD playback stimulator on a catamaran airboat, driven by an RC transmitter (FlySky
// FS-i6X / FS-iA6B) over an optically-isolated 4-channel PC817 link (see rc_control.h) — AND,
// with the same binary and no transmitter, by three panel buttons for the bench (panel_control.h).
// Both input sources are OR-ed. It LIVE-GENERATES its stimuli over the mean-EOD waveform (EOD_HV):
//
//   CH3 throttle  -> localization ON/OFF (dead-band) + RATE 0..20 Hz (a continuous train)
//   CH4 trigger   -> one-shot: throw high = run ONE BLINDED TRIAL (the firmware draws volley
//                    or sham); throw low does NOTHING
//   CH5 pot       -> localization JITTER          CH6 pot -> AMPLITUDE (sets volley/max; loc = half)
//
// A VOLLEY and a SHAM both begin with a coded PULSE MARKER preamble (a short EOD burst, tagged by
// pulse count) so the recording can tell them apart; the volley then plays the discharge, the
// sham stays silent (the no-stimulus control) while the LED shows a distinct pattern.
//
// BLINDING. The operator throws "fire a trial" and the FIRMWARE decides which it is (see
// begin_marker + TRIAL_P_VOLLEY), so when and where they trigger cannot correlate with the trial
// type. The marker's pulse count still records the truth for analysis.
//
// NOTE this is the RC device's COUNT-CODED marker (2 pulses = volley, 4 = sham, at 100 Hz, same
// polarity). The SD device uses a different code — 6 pulses at 10 Hz with ALTERNATING polarity,
// for identification rather than trial type. See shared/stim_constants.json; do not unify them.
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
// PULSE LOGGING. Every pulse this device emits — localization, marker and volley alike — is
// logged to the SD card with its exact sample tick, one row per pulse, never summarised. That
// is what makes the localization train separable from real fish in a recording (nothing else
// can: it is built to look exactly like a cruising eel), and it is the on-device ground truth
// for the blinded trial draw. LOGGING IS A PRECONDITION FOR OUTPUT: with no working card this
// sketch does not stimulate at all, and shows a distinct inverse-blink on the LED. See
// src/eel_core/pulse_log.h and firmware/README.md.
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
#include "src/eel_core/out_hal.h"       // L1: out_begin/out_write/out_arm/out_disarm (+ AMP_DEBUG)
#include "src/eel_core/eel_stimuli.h"   // L2: EOD_HV[] + STIM_ITEMS[] (volley snippets)
#include "src/eel_core/eel_player.h"    // L2: shared overlap-add engine (marker + volley playback)
#include "src/eel_core/locgen.h"        // L2: live localization scheduler
#include "src/eel_core/pulse_log.h"     // L2: per-pulse SD event log (ISR pushes, loop() writes)
#include "panel_control.h"              // L3: three panel buttons + the LED feedback vocabulary
#include "rc_control.h"                 // L3: 4-channel RC decode + conditioning

// Supported parts: Teensy 4.1 (i.MX RT1062) and Teensy 3.5 (MK64FX512). The output stage's
// pinout is deliberately identical on both (see config.h), and config.h derives the PWM clock
// per part, so the same source builds for either — anything else has not been checked.
#if !defined(__IMXRT1062__) && !defined(__MK64FX512__)
#warning "eel_fakefish_rc targets the Teensy 4.1 or 3.5 — select one of those in Tools > Board"
#endif

static const float ISR_PERIOD_US = 1000000.0f / (float)SAMPLE_RATE_HZ;   // 20.0 us (float -> unambiguous begin())

IntervalTimer sampleClock;

// ===== published targets (loop() writes, ISR latches) =====================
static volatile bool     g_loc_enabled = false;    // CH3 throttle above dead-band OR panel loc toggle
static volatile uint32_t g_trig_seq    = 0;        // bumped once per trigger throw (one trial)
static volatile int      g_trig_kind   = RC_TRIG_NONE;  // RC_TRIG_RANDOM (lever) or an explicit
                                                        // RC_TRIG_VOLLEY / _SHAM (panel buttons)
static volatile float    g_rate_ipi_samp = 0.0f;   // mean localization IPI (samples) for the set rate
static volatile float    g_cv          = 0.0f;     // localization jitter (coefficient of variation)
static volatile float    g_volley_amp  = 0.0f;     // volley (max) amplitude 0..1 — the amplitude control sets THIS
static volatile float    g_loc_amp     = 0.0f;     // localization amplitude = volley / VOLLEY_AMP_RATIO (always half)
static volatile bool     g_link_up     = false;    // CH3 (throttle) delivering edges
static volatile bool     g_rc_ever     = false;    // RC has been present at least once
static volatile bool     g_playing     = false;    // ISR is emitting a playback (owns the LED then)
// LOGGING IS A PRECONDITION FOR OUTPUT. loop() publishes the log's health here and the ISR
// latches it: with no working log the device does not stimulate at all (see the block comment
// above onSampleTick). Starts false so a card that fails in setup() can never be raced by the
// first sample tick.
static volatile bool     g_log_ok      = false;    // the SD event log is healthy

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
//
// This item is BUILT AT RUNTIME (setup(), below) rather than exported, so it is outside the
// Python gate that holds every library item to eel_player's minimum onset spacing. A
// runtime-built item has to carry that check itself, here, or a marker faster than the engine
// can mix would drop pulses in the ISR — and the marker is the on-water ground truth for the
// blind. At 500 samples (100 Hz) against a 131-sample EOD it is nowhere near the limit; the
// assertion exists so that retuning the marker cannot quietly cross it.
static_assert(PULSE_MARKER_IPI_SAMP >= EEL_PLAYER_MIN_IPI_SAMP,
              "PULSE_MARKER_IPI_SAMP is tighter than eel_player can mix — raise the marker's "
              "IPI, or widen EEL_PLAYER_MIN_IPI_SAMP and pay for it on every ISR tick");
static uint16_t   g_marker_ipi[PULSE_MARKER_MAX_PULSES];
static StimItem   g_marker_item;

// ===== pulse log (ISR-owned counters; the FILE is owned by loop()) ========
static PulseLog   g_plog;                             // ring + open file (see pulse_log.h)
static PlogTick   g_tick64;                           // 64-bit sample counter FOR THE LOG.
                                                      // Separate from g_tick on purpose: g_tick
                                                      // must stay 32-bit for the LED's wrap
                                                      // arithmetic, but a log spanning >23.9 h
                                                      // needs a counter that does not wrap.
static uint16_t   g_trial_id     = 0;                 // monotonic trial counter (1-based)
static int8_t     g_volley_item  = PLOG_ABSENT_ITEM;  // library item drawn for the live volley
static uint32_t   g_anchor_left  = 1;                 // samples until the next ANCHOR row
static bool       g_link_seen    = false;             // last g_link_up the ISR turned into a row

// ----- LED (per-pulse flash from the KNOWN onset; never a |sample| threshold) --
static inline void led_flash() { digitalWriteFast(LED_PIN, HIGH); g_led_off_at = g_tick + RC_LED_FLASH_SAMP; }
static inline void led_service() { if ((int32_t)(g_tick - g_led_off_at) >= 0) digitalWriteFast(LED_PIN, LOW); }

// ----- pulse log (ISR side: fill + push; NEVER touches the card) ----------
// The ISR only ever fills a POD record and pushes it into the lock-free ring; loop() formats
// and writes. No snprintf, no String, no SD call, no Serial in here — see pulse_log.h.
//
// Every record carries the settings in force at that instant rather than relying on separate
// settings-change events. The data cost is negligible and it keeps each row interpretable on
// its own, which matters because the realistic field failure is a file TRUNCATED by power
// loss: with delta-encoded settings a torn head makes everything after it ambiguous.
static inline void log_fill(PlogRec* r, uint8_t ev) {
  plog_rec_init(r, ev, plog_tick_value(&g_tick64));
  r->master_m = plog_milli(g_volley_amp);
  r->cv_m     = plog_milli(g_cv);
  r->rate_ipi = (uint32_t)(g_rate_ipi_samp + 0.5f);
}
static inline void log_event(uint8_t ev) { PlogRec r; log_fill(&r, ev); plog_push(&g_plog, &r); }

// RC_TRIG_* -> the log's trial-kind character. Recording the REQUESTED kind alongside the
// resolved one is what separates a genuinely blinded trial (requested RANDOM from the lever)
// from a bench-forced one (an explicit panel button); with only the outcome they are
// indistinguishable, and a bench test would silently contaminate the trial set.
static inline uint8_t log_trig_code(int kind) {
  switch (kind) {
    case RC_TRIG_RANDOM: return PLOG_KIND_RANDOM;
    case RC_TRIG_VOLLEY: return PLOG_KIND_VOLLEY;
    case RC_TRIG_SHAM:   return PLOG_KIND_SHAM;
    default:             return PLOG_KIND_NONE;
  }
}

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
  out_arm();                                     // bridges live at the pedestal for the whole train
  locgen_reset(&loc, draw_loc_ipi(), g_loc_amp, rand_polarity());
  src = SRC_LOC;
  g_playing = true;
  log_event(PLOG_LOCON);
}
// A trigger (volley or sham) always starts with the coded marker; g_post_marker records what
// follows. One polarity is chosen here and shared by the marker + the volley it precedes.
//
// THE BLINDED DRAW HAPPENS HERE. The RC lever requests RC_TRIG_RANDOM ("run a trial") and this
// is where it becomes a volley or a sham — in the ISR, at the moment of playback. Two reasons
// it lives here rather than in loop(): random() is called from the ISR elsewhere (polarity,
// localization jitter) and must stay single-caller, and drawing at playback time means a
// request that is later discarded never consumes a draw. The panel's explicit VOLLEY/SHAM
// buttons pass through unchanged, so the bench stays deterministic.
static inline void begin_marker(int kind) {
  out_arm();                                     // ...and for the marker + whatever follows it
  if (src == SRC_LOC) log_event(PLOG_LOCOFF);   // a trial preempts the localization train
  const int requested = kind;                    // BEFORE the blinded draw — see log_trig_code
  if (kind == RC_TRIG_RANDOM)
    kind = (random(TRIAL_DRAW_RANGE) < TRIAL_VOLLEY_CUTOFF) ? RC_TRIG_VOLLEY : RC_TRIG_SHAM;
  g_playback_pol = rand_polarity();
  g_playback_volley_amp = g_volley_amp;   // latch volley amplitude at the throw, held for the volley
  g_marker_item.n = (uint16_t)((kind == RC_TRIG_VOLLEY) ? PULSE_MARKER_PULSES_VOLLEY : PULSE_MARKER_PULSES_SHAM);
  eel_player_start_item(&player, &g_marker_item, PULSE_MARKER_AMP, g_playback_pol);
  g_marker_onset = 0xFFFFFFFFu;
  g_post_marker = kind;
  g_volley_item = PLOG_ABSENT_ITEM;   // no item drawn yet; a sham never draws one at all
  g_trig_seen = g_trig_seq;   // consume the request (one playback per throw; later throws ignored)
  src = SRC_MARKER;
  g_playing = true;
  // THE ON-DEVICE GROUND TRUTH FOR THE BLIND. The marker's pulse count records the outcome in
  // the water; this row records it on the card. Two independent records of the same fact, by
  // design — and this one also captures what was REQUESTED, which the water cannot show.
  g_trial_id++;
  PlogRec r;
  log_fill(&r, PLOG_TRIAL);
  r.trial = g_trial_id;
  r.pol   = g_playback_pol;
  r.req   = log_trig_code(requested);
  r.res   = log_trig_code(kind);
  plog_push(&g_plog, &r);
}
static inline void begin_volley_burst() {
  out_arm();                                     // already armed by the marker; harmless and explicit
  uint8_t idx = (uint8_t)(RC_VOLLEY_ITEM_FIRST + (int)random(RC_VOLLEY_ITEM_COUNT));
  eel_player_start_item(&player, &STIM_ITEMS[idx], g_playback_volley_amp, g_playback_pol);   // latched amp + marker's polarity
  g_volley_onset = 0xFFFFFFFFu;
  // WHICH pattern fired is recoverable from NOWHERE else — not from the marker, not from the
  // settings, and from a recording only by matching the IPI sequence against all 18 candidates.
  g_volley_item = (int8_t)idx;
  src = SRC_VOLLEY;
}
static inline void begin_sham() {   // no water output — just the distinct LED pattern
  out_arm();                                     // a sham holds the pedestal exactly like a volley does
  g_sham_phase = 0;
  src = SRC_SHAM;
  // A sham emits NOTHING into the water, so without this row the trial is invisible in the log.
  PlogRec r;
  log_fill(&r, PLOG_SHAM);
  r.trial = g_trial_id;
  plog_push(&g_plog, &r);
}
static inline void go_idle() {
  out_disarm();                                  // nothing playing, nothing scheduled -> hard brake, no drain
  if (src == SRC_LOC) log_event(PLOG_LOCOFF);
  src = SRC_IDLE;
  g_playing = false;
}
// After a volley/sham completes: discard any throw made DURING it (ignored, not queued), then
// resume localization if the throttle still commands it, else idle.
static inline void resume_after_playback() {
  g_trig_seen = g_trig_seq;
  if (g_loc_enabled && g_log_ok) begin_loc();
  else go_idle();
}

// One 50 kHz sample tick. Single owner of the playback engine AND the sole producer of pulse-
// log records.
//
// BLOCK ON LOGGING FAILURE. With no working log this ISR emits nothing at all: an unlogged
// localization pulse is indistinguishable from a real fish, so putting one in the water
// silently poisons the recording. The gate is applied only where a playback would START
// (SRC_IDLE, and the silent gap / interval boundary of SRC_LOC) — never mid-playback: a
// marker, volley or sham already in flight always runs to completion, exactly as an RC link
// loss never aborts one. A truncated volley is not "no data", it is an artefact that looks
// like data.
static void onSampleTick() {
  g_tick++;
  plog_tick_advance(&g_tick64);

  // loop() publishes the RC link state; the ISR turns a CHANGE into a row ITSELF. That keeps
  // the ring strictly single-producer — loop() is the consumer and must never push — and it
  // stamps the row with an exact tick instead of one sampled at loop() rate.
  bool link_now = g_link_up;
  if (link_now != g_link_seen) {
    g_link_seen = link_now;
    PlogRec r;
    log_fill(&r, PLOG_LINK);
    r.val = link_now ? 1u : 0u;
    plog_push(&g_plog, &r);
  }

  // Periodic tick <-> RTC anchor, emitted unconditionally so it also proves the device was
  // ALIVE through a quiet stretch. rtc_get() is a couple of SNVS register reads, done once per
  // PULSELOG_ANCHOR_S (10 s) — comfortably inside the 20 us budget. Reading the clock HERE,
  // rather than having loop() publish it, is what makes the tick and the wall-clock an exact
  // pair rather than one up to a second stale.
  if (--g_anchor_left == 0) {
    g_anchor_left = PULSELOG_ANCHOR_SAMP;
    PlogRec r;
    log_fill(&r, PLOG_ANCHOR);
    r.val = (uint32_t)rtc_get();
    plog_push(&g_plog, &r);
  }

  switch (src) {
    case SRC_MARKER: {
      int16_t s;
      if (eel_player_next(&player, &s)) {
        if (s == 0) out_silence(); else out_write(s);
        if (player.last_onset != g_marker_onset) {
          g_marker_onset = player.last_onset;
          led_flash();
          // eel_player_next() has already advanced k past the pulse it just started, so the
          // pulse whose onset this is has index k-1. No item: the marker is a StimItem built
          // at runtime in setup(), not a library entry — so its item column stays EMPTY.
          PlogRec r;
          log_fill(&r, PLOG_MARKER);
          r.pulse = (uint16_t)(player.k - 1);
          r.trial = g_trial_id;
          r.pol   = g_playback_pol;
          r.amp_m = plog_milli(PULSE_MARKER_AMP);
          plog_push(&g_plog, &r);
        }
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
        if (player.last_onset != g_volley_onset) {
          g_volley_onset = player.last_onset;
          led_flash();
          // item + pulse index together let an analysis look up the EXPECTED IPI in the
          // library and check it against the logged tick deltas — confirming the engine
          // emitted what it was told to, and making a partial volley obvious rather than
          // looking like a short one.
          uint16_t k = (uint16_t)(player.k - 1);
          // amp_m is "the amplitude applied to THIS pulse", so it must include the item's
          // PER-PULSE envelope, which eel_player_next() applies on top of the global scale
          // (eel_player.cpp: `if (it->rel_amp) a *= rel_amp[k]/255`). Every volley item the RC
          // device can draw carries such a table, and since the envelope became the MEASURED one
          // those tables run 255 down to as little as 22 — so logging the global scale alone
          // would overstate the tail of a volley by up to ~12x, silently, in the file that is
          // meant to be the exact ground truth.
          float applied = g_playback_volley_amp;
          if (player.item && player.item->rel_amp)
            applied *= (float)player.item->rel_amp[k] * (1.0f / 255.0f);
          PlogRec r;
          log_fill(&r, PLOG_VOLLEY);
          r.item  = g_volley_item;
          r.pulse = k;
          r.trial = g_trial_id;
          r.pol   = g_playback_pol;
          r.amp_m = plog_milli(applied);
          plog_push(&g_plog, &r);
        }
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
        // A logging failure stops the train HERE, in the silent gap — a clean seam that
        // truncates nothing. It is checked first because it outranks both other reasons.
        if (!g_log_ok) { go_idle(); break; }
        if (trig_pending()) { begin_marker(g_trig_kind); break; }
        if (!g_loc_enabled) { go_idle(); break; }
      }
      int onset, boundary;
      int16_t s = locgen_tick(&loc, &onset, &boundary);
      if (s == 0) out_silence(); else out_write(s);
      if (onset) {
        led_flash();
        // THE REASON THIS FEATURE EXISTS. A localization pulse is deliberately built to look
        // exactly like a real cruising eel, so this row is the only thing that will ever
        // distinguish it from biology in a recording. No item: locgen synthesises the pulse
        // directly from EOD_HV with a drawn IPI, so no library item is involved.
        PlogRec r;
        log_fill(&r, PLOG_LOC);
        r.amp_m = plog_milli(loc.amp);
        r.pol   = loc.pol;
        plog_push(&g_plog, &r);
      }
      led_service();
      if (boundary) {
        if (!g_loc_enabled || !g_log_ok) { go_idle(); }
        else { loc.ipi = draw_loc_ipi(); loc.amp = g_loc_amp; }   // latch for the next pulse
      }
      break;
    }
    default: {  // SRC_IDLE
      out_silence();
      // No working log -> no stimulation. A throw made while blocked is DISCARDED, not
      // queued, matching how a throw made during a playback is ignored — otherwise a stale
      // request would fire without warning the moment the card recovered.
      if (!g_log_ok) { g_trig_seen = g_trig_seq; break; }
      if (trig_pending()) { begin_marker(g_trig_kind); }
      else if (g_loc_enabled) { begin_loc(); }
      // LED in idle is owned by loop() (dark, the no-RC-signal blink, or the log-fault
      // inverse-blink).
      break;
    }
  }
}

// L3 provenance appended to EVERY log file's header (including one opened by a mid-session
// recovery — it is stored as a hook, not called once). These are the surface-specific
// constants an analysis needs to interpret the rows: the marker code that tags a trial in the
// water, the range the volley item index is drawn from, and the blind probability.
static void log_header_hook(PulseLog* L) {
  plog_header_kv(L, "surface", 0);                 // 0 == eel_fakefish_rc
  plog_header_kv(L, "marker_ipi_samp", PULSE_MARKER_IPI_SAMP);
  plog_header_kv(L, "marker_pulses_volley", PULSE_MARKER_PULSES_VOLLEY);
  plog_header_kv(L, "marker_pulses_sham", PULSE_MARKER_PULSES_SHAM);
  plog_header_kv(L, "marker_amp_milli", plog_milli(PULSE_MARKER_AMP));
  plog_header_kv(L, "volley_item_first", RC_VOLLEY_ITEM_FIRST);
  plog_header_kv(L, "volley_item_count", RC_VOLLEY_ITEM_COUNT);
  plog_header_kv(L, "trial_p_volley_milli", plog_milli(TRIAL_P_VOLLEY));
  plog_header_kv(L, "loc_refractory_samp", LOC_REFRACTORY_SAMP);
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

  // Pulse log. Opened HERE — before the sample clock, and after the settings defaults so the
  // first rows carry real values. Opening at boot rather than lazily on the first event is
  // deliberate: it proves the card is WRITABLE (a bare SD.begin() mount probe does not), and
  // it moves the first write failure to boot, where a card swap is cheap, instead of to the
  // first trial you were waiting for. Until this succeeds g_log_ok stays false and the ISR
  // emits nothing.
  plog_tick_reset(&g_tick64);
  g_log_ok = plog_begin(&g_plog, log_header_hook);
  if (g_log_ok) {
    // Pushing straight from setup() is safe and is NOT an SPSC violation: the sample clock —
    // the ring's only other producer — has not been started yet.
    PlogRec r;
    log_fill(&r, PLOG_BOOT);
    r.val = (uint32_t)g_plog.index;
    plog_push(&g_plog, &r);
  }

  sampleClock.begin(onSampleTick, ISR_PERIOD_US);    // continuous 50 kHz clock (runs forever)
  sampleClock.priority(64);                          // above the RC pin ISRs (default 128): protect pulse timing
}

// A trigger request from RC or panel. Set the kind BEFORE bumping the seq so the ISR sees a
// consistent (kind, seq) pair. A held/bouncing stick can't re-fire (loop() edge-detects it).
// `kind` is RC_TRIG_RANDOM from the lever (resolved in the ISR) or an explicit kind from a
// panel button.
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

// "Logging failed, output is suppressed": an INVERTED blink — steady on with a brief dark
// notch. Deliberately unlike every other pattern in the vocabulary, which are all short
// flashes on a dark background, because a blocked device otherwise looks exactly like an idle
// one and this is the one signal that must not be misread from shore.
static inline void log_fault_blink(uint32_t now_ms) {
  uint32_t ph = now_ms % LOGFAULT_LED_PERIOD_MS;
  digitalWriteFast(LED_PIN, (ph < LOGFAULT_LED_DARK_MS) ? LOW : HIGH);
}

// loop() decodes the RC channels + panel and publishes targets; it never touches engine state.
void loop() {
  uint32_t now_ms = millis();

  // ----- pulse log: drain the ISR's ring to the card ------------------------------------
  // THE ONLY PLACE FILE I/O HAPPENS. Serviced first so the ring drains promptly. On a failure
  // plog_retry() re-mounts and opens a NEW indexed file (never reopening the interrupted one,
  // whose tail length is unknowable) and records the discontinuity as a GAP row. g_log_ok is
  // republished every pass; the ISR latches it and stops emitting at its next clean seam.
  //
  // An SD write can stall for 100 ms+, which delays the ~200 Hz RC decode below. That is
  // tolerable by design: presence detection is time-based against RC_ABSENCE_MS (500 ms), so a
  // stall delays the decode rather than faking a link loss, and PULSELOG_DRAIN_MAX bounds how
  // much one pass can take on.
  plog_service(&g_plog, now_ms);
  if (!plog_healthy(&g_plog)) plog_retry(&g_plog, now_ms);
  g_log_ok = plog_healthy(&g_plog);

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
      // CH4: one-shot trigger (only on a live channel). Throwing HIGH requests a BLINDED
      // trial — the ISR draws volley-vs-sham. Throwing low does nothing at all: the
      // operator cannot pick the trial type, so their timing and position cannot correlate
      // with it. The bench panel keeps explicit volley/sham buttons for testing.
      if (trig_present) {
        if (rc_trigger_step(&trig, u_trig, CH4_VOLLEY_THRESH, CH4_CENTER_LO, CH4_CENTER_HI))
          request_trigger(RC_TRIG_RANDOM);
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
  // A logging fault OUTRANKS the no-signal blink: it is the more severe condition, because it
  // is actively suppressing stimulation. There is never contention with the ISR's per-pulse
  // flash here — a logging fault means there is no playback to flash for, so g_playing is
  // false and this branch owns the LED outright.
  if (!g_playing) {
    if (!plog_healthy(&g_plog)) log_fault_blink(now_ms);
    else if (g_rc_ever && !g_link_up) no_signal_blink(now_ms);
    else digitalWriteFast(LED_PIN, LOW);
  }
}
