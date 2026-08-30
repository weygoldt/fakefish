// Teensy 4.1 fakefish stimulator — RC + panel controlled (L3 surface on the shared core).
//
// A dipole EOD playback stimulator on a catamaran airboat, driven by an RC transmitter (FlySky
// FS-i6X / FS-iA6B) over an optically-isolated 4-channel PC817 link (see rc_control.h) — AND,
// with the same binary and no transmitter, by two panel buttons for the bench (panel_control.h).
// Both input sources are OR-ed. It LIVE-GENERATES its stimuli over the mean-EOD waveform (EOD_HV):
//
//   CH3 throttle  -> localization ON/OFF + TICK TEMPO 0.5..20 Hz, log ladder. At REST it is a
//                    MASTER off: it also clears the panel LOC latch, so throttle-down is zero pulses
//   CH4 trigger   -> one-shot: throw high = run ONE BLINDED TRIAL (the firmware draws the arm);
//                    throw low does NOTHING
//   CH5 pot       -> localization RANDOMNESS      CH6 pot -> AMPLITUDE (sets volley/max; loc = quarter)
//
// THREE ARMS (2026-08-24), each drawn with probability 1/3:
//
//   VOLLEY   — a library discharge. The treatment.
//   BASELINE — resting-rhythm pulses at localization amplitude: a fish is PRESENT and NOT
//              hunting. This arm is the reason the design changed. A volley-vs-nothing
//              experiment cannot separate "a hunting discharge happened" from "a conspecific is
//              here at all", and that is exactly the confound in questions like "do volleys
//              attract other eels" — the thing being tested and the thing being controlled for
//              arrive together. With three arms the contrast is nothing / a resting fish / a
//              hunting fish, and the middle one is what makes the outer two interpretable.
//   SILENCE  — nothing at all. What the two-arm design called a SHAM; the name survives in the
//              log's PLOG_SHAM because the quantity is identical.
//
// ALL THREE ARE THE SAME LENGTH, and it is drawn per trial from the volley library: every trial
// picks an item, VOLLEY plays it, and the other two hold for exactly its duration. So the arms
// match in duration BY CONSTRUCTION. Until 2026-08-24 the sham was instead as long as its own
// LED animation — a fixed 720 ms, longer than 80 % of the library's volleys — which meant the
// hole a control left in the localization train was systematically LONGER than the hole a volley
// left, and an analyst scoring gaps could have separated the arms without hearing a volley.
//
// THE BASELINE ARM RUNS ITS OWN RHYTHM (trial_rhythm), on its own fixed knobs, seeded
// independently. It has to: the arm exists to be a control for "the throttle is down and nothing
// is happening", which is precisely the state in which the ambient rhythm is not running. Its
// first pulse is anchored at trial onset — see begin_base() for why that is load-bearing and not
// a detail.
//
// BLINDING, END TO END. The operator throws "run a trial" and the FIRMWARE draws the arm (see
// begin_trial), so when and where they trigger cannot correlate with it. Since 2026-08-24
// NOTHING can force an arm — the panel's explicit VOLLEY/SHAM buttons became one blinded TRIAL
// button — and the LED shows ONE pattern for all three arms, so neither an observer on shore nor
// anyone scoring the GoPro afterwards can read the arm off the device. The PLOG_TRIAL row is the
// only unblinding key.
//
// THE MARKER IS GONE (2026-08-22). It was 2 pulses for a volley and 4 for a sham, at 100 Hz. Two
// reasons it went, and the second is the one that matters:
//   1. It is redundant. The log cannot be absent — no working log means no output at all — so the
//      trial and its outcome are already recorded, with a tick 20 us wide.
//   2. IT WAS NOT REDUNDANT FOR THE SHAM, IT WAS A STIMULUS. A sham is the no-stimulus control,
//      and it was emitting four eel pulses at 100 Hz before staying quiet. Whatever the animal did
//      next, "nothing happened" was not what it experienced. (The 2026-08-24 three-arm change is
//      the same argument one step further: it is not enough for the control to be silent, the
//      design also needs an arm in which a fish is present without hunting.)
// What this costs for ALIGNMENT: almost nothing. A volley is the anchor and a far better one than
// the marker was — 46..364 pulses at 300-400 Hz whose exact IPI sequence the log's `item` index
// looks up in the library, against the marker's two. A sham emits nothing, but its time comes from
// interpolating between volley anchors, and on both field logs volleys land every ~10 s (largest
// gap 26 s), over which 40 ppm of clock drift is about 1 ms. The only unplaceable session is one
// with no volley AND no localization — which put nothing in the water to place.
// firmware/README.md -> "Aligning a log to a recording" has the argument.
//
// THE SD DEVICE KEEPS ITS OWN MARKER, and that is not an inconsistency: it has no pulse log, so
// its 6-pulse alternating lead-in is the ONLY record that a playback happened. The premise that
// retired this one — "the session is recorded anyway" — is simply not true over there.
//
// LAYERING. This sketch is a CONTROL SURFACE (L3) only. It owns the input decode, the session
// state machine and the ISR that wires them together; it does NOT own the output stage or the
// sample producers:
//   L1  src/eel_core/out_hal.h + config.h   the DRV8871 complementary output stage (36 V, 100 kHz)
//   L2  src/eel_core/eel_player.{h,cpp}     overlap-add engine (volley playback)
//       src/eel_core/locgen.h               renders the localization pulse
//       src/eel_core/loc_rhythm.h           the FITTED resting rhythm — decides when
//       src/eel_core/eel_stimuli.{h,cpp}    the generated stimulus library
// src/eel_core/ is a COMMITTED COPY of firmware/eel_core/, produced by firmware/sync_core.sh —
// that is what makes this sketch self-contained for the IDE / arduino-cli / rsync-to-bench.
//
// THE RESTING RHYTHM IS A FITTED MODEL, NOT A RANDOM DRAW. Between trials this device ticks
// along like a resting eel, and since 2026-08-21 that means a model fitted to 99 010 measured
// resting intervals rather than the unit-mean lognormal it used before. The difference is not
// cosmetic: a lognormal is a RENEWAL process, so its consecutive log-intervals are uncorrelated
// at every lag, while a real eel's correlate 0.55 at lag 1 and still 0.21 at lag 20. The fish's
// discharge rate WANDERS, over a few seconds and over a minute, and reproducing that wander is
// the whole point — it is what makes a recording sound like a fish instead of a random number
// generator. See src/eel_core/loc_rhythm.h and docs/LOCALIZATION_GENERATIVE_SPEC.md.
//
// The model also carries a burst hazard — the fish's own decision to fire a fast run, about 1
// in 49 resting pulses. It is DELIBERATELY not wired up. This surface runs a blinded trial
// design in which the log is the trial record, so a spontaneous burst would be
// an unmarked volley in the water, and one landing inside a sham would destroy the no-stimulus
// control. Every volley this device emits is operator-requested.
//
// PULSE LOGGING. Every pulse this device emits — localization and volley alike — is
// logged to the SD card with its exact sample tick, one row per pulse, never summarised. That
// is what makes the localization train separable from real fish in a recording (nothing else
// can: it is built to look exactly like a cruising eel), and it is the on-device ground truth
// for the blinded trial draw. LOGGING IS A PRECONDITION FOR OUTPUT: with no working card this
// sketch does not stimulate at all, and shows a distinct inverse-blink on the LED. See
// src/eel_core/pulse_log.h and firmware/README.md.
//
// CONCURRENCY MODEL. The 50 kHz sample-clock ISR (onSampleTick) is the SINGLE OWNER of all
// playback-engine state (the localization scheduler + the shared EelPlayer used for the volley
// and the volley). loop() only decodes the RC/panel inputs and publishes target parameters +
// a trigger request through volatile words, LATCHED by the ISR at pulse boundaries. Each shared
// value is a single aligned 32-bit word (atomic on the M7), written by exactly one side. The
// sample clock is given higher priority than the RC pin ISRs so pulse OUTPUT timing is never
// perturbed by input capture.
#include <IntervalTimer.h>
#include "src/eel_core/config.h"        // L1: output stage, sample clock, LED pin
#include "src/eel_core/stim_levels.h"   // generated playback constants (TRIAL_*, PANEL_*)
#include "src/eel_core/out_hal.h"       // L1: out_begin/out_write/out_silence (+ AMP_DEBUG)
#include "src/eel_core/eel_stimuli.h"   // L2: EOD_HV[] + STIM_ITEMS[] (volley snippets)
#include "src/eel_core/eel_player.h"    // L2: shared overlap-add engine (volley playback)
#include "src/eel_core/locgen.h"        // L2: renders one localization pulse
#include "src/eel_core/loc_rhythm.h"    // L2: the fitted resting rhythm (when the next one is)
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
static volatile float    g_rate_tempo  = 0.0f;     // localization rate knob: a TEMPO MULTIPLIER
                                                   // (1.0 == the measured eel's 3.15 Hz tick)
static volatile uint32_t g_tick_ipi_samp = 0;      // the nominal MEDIAN IPI for that tempo, in
                                                   // samples — carried in the log so a row can be
                                                   // read back as a rate without the firmware
static volatile float    g_randomness  = 0.0f;     // localization randomness (0 = metronome, 1 = eel)
static volatile float    g_volley_amp  = 0.0f;     // volley (max) amplitude 0..1 — the amplitude control sets THIS
static volatile float    g_loc_amp     = 0.0f;     // AMBIENT train amplitude = volley / VOLLEY_AMP_RATIO
                                                   // (a QUARTER). Not the BASELINE arm: see g_base_amp.
static volatile bool     g_link_up     = false;    // CH3 (throttle) delivering edges
static volatile bool     g_rc_zeroed   = false;    // the session zero has been captured (RcZero)
static volatile bool     g_playing     = false;    // ISR is emitting a playback (owns the LED then)
// LOGGING IS A PRECONDITION FOR OUTPUT. loop() publishes the log's health here and the ISR
// latches it: with no working log the device does not stimulate at all (see the block comment
// above onSampleTick). Starts false so a card that fails in setup() can never be raced by the
// first sample tick.
static volatile bool     g_log_ok      = false;    // the SD event log is healthy

// The session zero (rc_control.h -> RcZero). Plain, not volatile: it is loop()-owned end to end —
// captured, held and applied entirely inside the decode path. The ISR never reads it, and never
// needs to: what reaches the ISR is the already-corrected knob values and g_rc_zeroed.
static RcZero g_zero;

// ===== the RAW decode, published for the log (pulse-log v3) ===============
// loop() decodes; the ISR is the ring's ONLY producer (invariant 9). So the raw widths and the
// captured zero are published here as volatile words and READ BY THE ISR when it fills a record
// — the same publish/latch idiom as g_randomness and g_tick_ipi_samp, and the same reason: a
// record must carry the state at the instant of the event, not at the instant loop() got round
// to formatting it.
//
// These are the NEAR end of the decode chain. Everything else the log carries about the controls
// is derived from them through a calibration and a quantiser, so without them a fault in the
// measurement can only be inferred backwards, one rung at a time, through the very constants
// under suspicion. That is exactly how the 2026-08-22 supply-offset fault had to be found.
static volatile uint16_t g_ch_us[RC_N_CHANNELS];   // raw width per channel, us (ABSENT_U16 = none yet)
static volatile uint16_t g_zero_us = PLOG_ABSENT_U16;  // the captured session zero, us

// ===== ISR-owned playback state ===========================================
// SRC_SHAM is the SILENCE arm. Its name is kept from the two-arm design, like the log's
// PLOG_SHAM: the quantity is unchanged and renaming a shipped token is churn.
enum Source { SRC_IDLE, SRC_LOC, SRC_VOLLEY, SRC_BASE, SRC_SHAM };
static volatile Source src = SRC_IDLE;
static EelPlayer  player;                           // volley engine (touched ONLY in the ISR)
static LocGen     loc;                               // localization pulse renderer (ISR only)
static LocRhythm  rhythm;                            // the fitted resting rhythm (ISR only)

// ===== the BASELINE arm's OWN generator ===================================
// A THIRD independent stream, and the independence is the point. The baseline arm has to work
// with live localization switched fully off — that is its whole purpose as a control, and it is
// exactly the state in which `rhythm` is not running at all. Sharing `rhythm` would also make
// the arm's pulse train depend on how long the ambient train had been ticking, and (worse) make
// the ambient train's relaxation state depend on how many baseline arms had fired. Neither is
// acceptable in a blinded design: the operator's rate knob would reach into the control
// condition. Same reasoning as loc_rhythm.h's own "the rhythm has its OWN PRNG".
//
// Its knobs are TRIAL_BASE_* — fixed constants, never CH3/CH5. See stim_constants.json.
//
// EVERY ARM GETS A FRESH, BURNED-IN STATE. The arm is one independent presentation of "a
// resting fish is present", so it must start from the ensemble of resting fish observed AT one
// of their own pulses — the arm anchors a pulse at t=0, so that is the state it is standing in.
//
// The first version aged ONE persistent rhythm by the wall-clock gap since the previous arm's
// last pulse (tens of seconds). That is a COLD START, and it is exactly the transient
// loc_rhythm_init's own comment warns about: the components' stationary law is defined in
// continuous time, while the model is observed at its PULSE times, which over-visits the fast
// side. Measured on the shipped C (N=200000): the score at a pulse of a free-running train has
// median -0.512; after 60 s with no pulses it has median -0.079. So every arm re-entered slow.
// The first interval's median went 0.32 s -> 0.51 s against a median arm length of 0.47 s, and
// ONE PULSE became the modal outcome — in exp2's field log, 11 of 13 arms carried only the
// anchor. A burn-in cannot go in the ISR (~320 intervals is ~4500 libm calls), so loop()
// pre-warms a spare and the ISR swaps it in: a single-slot SPSC handoff, the same publish/latch
// idiom this file already uses for g_trig_seq/g_trig_seen.
static LocGen     trial_loc;                         // baseline-arm pulse renderer (ISR only)
static LocRhythm  trial_rhythm;                      // baseline-arm rhythm (ISR only)
static LocRhythm  g_base_spare;                      // warmed by loop(), consumed by the ISR
static volatile uint8_t g_base_spare_ready = 0;      // 1 = loop() has a burned-in state waiting

// The burn-in length is TRIAL_BASE_BURN_IN, generated from shared/stim_constants.json — see
// there for the measured bias at each value and why it is not LOC_BURN_IN.
//
// loop()'s OWN seed stream for those warm-ups. Not random() — that is an ISR-only caller here
// (polarity, the item pick, the blinded arm draw) and must stay single-caller; and not one of
// the LocRhythm PRNGs, for invariant 11's reason. Seeded once in setup().
static uint32_t   g_base_seed_rng = 0;
static inline uint32_t base_seed_next() {
  uint32_t x = g_base_seed_rng;
  x ^= x << 13; x ^= x >> 17; x ^= x << 5;    // xorshift32, as in loc_rng_next
  g_base_seed_rng = x;
  return x;
}
// Draw a fresh resting fish, observed at one of its own pulses. ~300 us on a 4.1; loop() only.
static inline void base_warm_spare() {
  loc_rhythm_init(&g_base_spare, base_seed_next() | 1u,
                  loc_rhythm_rate_for_hz(TRIAL_BASE_TICK_HZ), TRIAL_BASE_RANDOMNESS);
  float dt = 0.0f;   // fed back exactly as loc_rhythm_init's own burn-in does
  for (int i = LOC_BURN_IN; i < TRIAL_BASE_BURN_IN; i++)
    dt = loc_rhythm_next_s(&g_base_spare, dt);
}
// Tick of the last localization ONSET, 64-bit so the difference below cannot wrap. The rhythm
// relaxes in WALL-CLOCK time, so every draw must be told how long it has actually been since
// the previous pulse — including time spent on a volley, or with the throttle at
// rest. Feeding back the interval the model last *scheduled* would be wrong twice over: a
// trial preempts the localization train part-way through an interval that is then never
// realised, and the time the trial itself took would go unbooked.
static uint64_t   g_loc_last_onset = 0;
static uint32_t   g_trig_seen = 0;                   // last consumed trigger seq (ISR only)
static int8_t     g_playback_pol = 1;                // polarity drawn per trial
static float      g_playback_volley_amp = 0.0f;      // volley amplitude latched at the throw (volley start)
static float      g_base_amp            = 0.0f;      // BASELINE arm amplitude: ONE pulse drawn from the
                                                     // trial's own item, held for every pulse of the arm.
                                                     // Drawn in begin_trial() for ALL THREE arms — see there.
static uint32_t   g_tick = 0;                        // monotonic sample counter (ISR only)
static uint32_t   g_led_off_at = 0;                  // g_tick at which the per-pulse flash ends
static uint32_t   g_volley_onset = 0;                // detect volley pulse onsets for the LED
static uint32_t   g_trial_phase = 0;                 // samples elapsed in the current trial —
                                                     // drives BOTH the silent arms' length and
                                                     // the ONE trial LED pattern (all arms)
static uint32_t   g_trial_dur_samp = 0;              // this trial's length, drawn from the
                                                     // library so all three arms match

// EVERY TRIAL LASTS AS LONG AS A VOLLEY, because every trial draws one. A volley's length is
// the sum of its item's ipi_samp table plus the last pulse's own width; the two silent arms
// then hold for exactly that many samples. The arms therefore match in duration BY
// CONSTRUCTION, not by a matched constant that could drift out of step with the library.
//
// The table is built once in setup() rather than summed in the ISR: an item runs to 457 pulses,
// and a 457-iteration loop inside begin_trial() would be ~8 us of a 20 us tick on the Teensy
// 3.5. 100 entries x 4 bytes = 400 bytes of RAM to make the trial start free.
static uint32_t   g_item_dur_samp[RC_VOLLEY_ITEM_COUNT];


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

// THE TRIAL LED IS IDENTICAL FOR ALL THREE ARMS, and that is a protocol requirement, not a
// simplification. Until 2026-08-24 a volley flashed once per emitted pulse and a sham ran a
// distinct 3-blink pattern, so the LED announced the arm — to anyone on shore, and to anyone
// scoring the GoPro footage afterwards. With a camera on every session that unblinds the
// behavioural scoring, which is the measurement the blinding exists to protect.
//
// So during a trial the LED is a plain square wave of a fixed period, driven by g_trial_phase,
// for the trial's whole length. It leaks the trial's DURATION — but duration is drawn from the
// same distribution for every arm, so it carries no information about which arm ran. The pulse
// log is the only unblinding key.
static inline void led_trial() {
  uint32_t period = TRIAL_LED_ON_SAMP + TRIAL_LED_OFF_SAMP;
  digitalWriteFast(LED_PIN,
                   ((g_trial_phase % period) < TRIAL_LED_ON_SAMP) ? HIGH : LOW);
}

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
  r->rand_m   = plog_milli(g_randomness);
  r->tick_ipi = g_tick_ipi_samp;
  for (int i = 0; i < RC_N_CHANNELS; i++) r->ch_us[i] = g_ch_us[i];
  r->zero_us  = g_zero_us;
}
static inline void log_event(uint8_t ev) { PlogRec r; log_fill(&r, ev); plog_push(&g_plog, &r); }

// RC_TRIG_* -> the log's trial-kind character. Recording the REQUESTED kind alongside the
// resolved one is what separates a genuinely blinded trial (requested RANDOM from the lever)
// from a bench-forced one (an explicit panel button); with only the outcome they are
// indistinguishable, and a bench test would silently contaminate the trial set.
static inline uint8_t log_trig_code(int kind) {
  switch (kind) {
    case RC_TRIG_RANDOM:   return PLOG_KIND_RANDOM;
    case RC_TRIG_VOLLEY:   return PLOG_KIND_VOLLEY;
    case RC_TRIG_BASELINE: return PLOG_KIND_BASELINE;
    case RC_TRIG_SILENCE:  return PLOG_KIND_SHAM;   // 'S' — the silence arm, name kept
    default:               return PLOG_KIND_NONE;
  }
}

// ----- random helpers (called ONLY from the ISR -> single-caller, no lock) --
static inline int8_t rand_polarity() { return random(2) ? (int8_t)1 : (int8_t)-1; }
// Beyond this the state has relaxed to its offset anyway (the slowest component's time
// constant is 62 minutes), and the cap is what keeps the elapsed sample count inside a uint32.
#define LOC_ELAPSED_CAP_SAMP ((uint32_t)SAMPLE_RATE_HZ * 3600u * 4u)   // 4 hours

// Samples since the last localization onset — the true elapsed time the rhythm ages by. At the
// first pulse of a power-on this is simply the uptime, which is the right answer: the fish was
// resting quietly before the throttle was pushed.
static inline uint32_t loc_elapsed_samp() {
  uint64_t now = plog_tick_value(&g_tick64);
  uint64_t d   = (now > g_loc_last_onset) ? (now - g_loc_last_onset) : 0;
  return (d > (uint64_t)LOC_ELAPSED_CAP_SAMP) ? LOC_ELAPSED_CAP_SAMP : (uint32_t)d;
}

// One interval from the fitted rhythm. The knobs are latched HERE, at the boundary, so a
// mid-interval control change never alters the pulse already in flight — the same rule locgen
// follows for ipi/amp. Setting them every draw costs one table lookup and one divide.
static inline uint32_t draw_loc_ipi() {
  loc_rhythm_set_knobs(&rhythm, g_rate_tempo, g_randomness);
  return loc_rhythm_next_ipi_samp(&rhythm, loc_elapsed_samp(),
                                  (uint32_t)SAMPLE_RATE_HZ, LOC_REFRACTORY_SAMP);
}
// The BASELINE arm's interval, from its OWN rhythm and its OWN fixed knobs. Deliberately not
// draw_loc_ipi(): that one latches g_rate_tempo/g_randomness, the live CH3/CH5 controls, and
// this arm must be identical whatever the operator left those at — including "off", which is
// the state the arm exists to be a control for.
//
// `elapsed_samp` is how long the state should age before drawing, and the caller decides:
//   * begin_base() passes 0 — the state was just swapped in from a burned-in spare, so it is
//     ALREADY standing at a pulse. Ageing it there is the bug this replaced.
//   * the boundary inside an arm passes the interval that just completed, which is exactly the
//     time since the previous onset. Within an arm the rhythm free-runs, as a real fish does.
static inline uint32_t draw_base_ipi(uint32_t elapsed_samp) {
  loc_rhythm_set_knobs(&trial_rhythm, loc_rhythm_rate_for_hz(TRIAL_BASE_TICK_HZ),
                       TRIAL_BASE_RANDOMNESS);
  return loc_rhythm_next_ipi_samp(&trial_rhythm, elapsed_samp, (uint32_t)SAMPLE_RATE_HZ,
                                  LOC_REFRACTORY_SAMP);
}
static inline bool trig_pending() { return g_trig_seq != g_trig_seen; }

// ----- ISR-side state transitions (single owner) --------------------------
static inline void begin_loc() {
  out_silence();                                 // brake both bridges + clear the shaper
  locgen_reset(&loc, draw_loc_ipi(), g_loc_amp, rand_polarity());
  src = SRC_LOC;
  g_playing = true;
  log_event(PLOG_LOCON);
}
static inline void begin_volley_burst() {
  out_silence();                                 // clean seam into the volley; harmless and explicit
  // The item was drawn in begin_trial(), for EVERY arm — see the note there.
  eel_player_start_item(&player, &STIM_ITEMS[(uint8_t)g_volley_item],
                        g_playback_volley_amp, g_playback_pol);   // latched amp + the trial's polarity
  g_volley_onset = 0xFFFFFFFFu;
  src = SRC_VOLLEY;
}
// The BASELINE arm: a fish that is present and NOT hunting, for exactly as long as the volley
// would have lasted. Its FIRST PULSE IS AT TRIAL ONSET — locgen_reset() leaves phase at 0, so
// the very next tick is an onset. That is the correct parallel to a volley (whose first pulse is
// also at t=0) and it is load-bearing besides: measured over the real arm-duration mix, an
// unanchored arm would be EMPTY in 27 % of trials, which would make the baseline and silence
// arms physically identical better than one time in four. Do not remove the anchor.
static inline void begin_base() {
  out_silence();                                 // clean seam into the arm
  // Take loop()'s burned-in fish if one is waiting. It always is: setup() warms the first and
  // loop() re-warms within a millisecond of each arm consuming one, against trials seconds
  // apart. If it somehow is not, carry on with the state in hand and draw with no ageing —
  // that is the old behaviour minus the cold start, so strictly better than blocking a trial,
  // which would put a hole in the blinded arm sequence.
  if (g_base_spare_ready) {
    trial_rhythm = g_base_spare;
    g_base_spare_ready = 0;                      // release the slot back to loop()
  }
  locgen_reset(&trial_loc, draw_base_ipi(0),     // 0: the state is already standing at a pulse
               g_base_amp,                          // amplitude-matched to the volley: one pulse of
               g_playback_pol);                     // this trial's item, drawn in begin_trial()
  src = SRC_BASE;
}
static inline void begin_sham() {   // the SILENCE arm — no water output at all
  out_silence();                                 // a silence arm emits nothing — 0 V, like every gap
  src = SRC_SHAM;
  // It emits NOTHING into the water, so without this row the trial is invisible in the log.
  PlogRec r;
  log_fill(&r, PLOG_SHAM);
  r.trial = g_trial_id;
  r.item  = g_volley_item;   // the item whose LENGTH this arm borrowed
  plog_push(&g_plog, &r);
}
// A trigger resolves to ONE OF THREE ARMS here and plays it immediately — there is no preamble
// (see "THE MARKER IS GONE" at the top of this file).
//
//   VOLLEY   — a library discharge (the treatment)
//   BASELINE — resting-rhythm pulses at localization amplitude: a fish is present and NOT
//              hunting. This arm is what separates "a discharge happened" from "a fish is there
//              at all", which a volley-vs-nothing design confounds — and that confound is fatal
//              to the questions this device exists to ask ("do volleys attract other eels?").
//   SILENCE  — nothing at all (what the two-arm design called a SHAM)
//
// EVERY ARM DRAWS AN ITEM, AND THAT IS WHAT SETS ITS LENGTH. The two silent arms hold for
// exactly the duration the drawn volley would have taken, so the arms are matched in duration BY
// CONSTRUCTION. The old sham was instead as long as its own LED animation — 720 ms, longer than
// 80 % of the library's volleys — so the gap it left in the localization train was systematically
// longer than the gap a volley left, and gap length alone leaked the arm.
//
// THE BLINDED DRAW HAPPENS HERE, in the ISR at the moment of playback. Two reasons it is not in
// loop(): random() is called from the ISR elsewhere (polarity, the item pick) and must stay
// single-caller, and drawing at playback time means a request that is later discarded never
// consumes a draw. Nothing can force an arm any more — the panel's explicit buttons became one
// blinded TRIAL button on 2026-08-24 — so `req` is always 'R' in new files.
//
// Neither rhythm is on this stream: the ambient train has its own generator and the baseline arm
// has a third (see loc_rhythm.h, and trial_rhythm above). Sharing any of them would make the
// blinded sequence depend on how many localization pulses happened to precede a throw, so the
// operator's rate knob would silently reach into which arms came out.
static inline void begin_trial(int kind) {
  out_silence();                                 // clean seam into whatever this resolves to
  if (src == SRC_LOC) log_event(PLOG_LOCOFF);   // a trial preempts the localization train
  const int requested = kind;                    // BEFORE the blinded draw — see log_trig_code
  if (kind == RC_TRIG_RANDOM) {
    long d = random(TRIAL_DRAW_RANGE);
    kind = (d < TRIAL_CUT_VOLLEY)   ? RC_TRIG_VOLLEY
         : (d < TRIAL_CUT_BASELINE) ? RC_TRIG_BASELINE
                                    : RC_TRIG_SILENCE;
  }
  g_playback_pol = rand_polarity();
  g_playback_volley_amp = g_volley_amp;   // latch volley amplitude at the throw, held for the trial
  // THE ITEM IS DRAWN FOR ALL THREE ARMS, not only for a volley. A volley plays it; the two
  // silent arms borrow only its LENGTH. Drawing unconditionally is also what keeps the draw
  // itself blind: if only a volley consumed an item, the library's RNG stream would advance at a
  // rate that depends on the arm sequence.
  //
  // WHICH pattern fired is recoverable from NOWHERE else — not from the settings, and from a
  // recording only by matching the IPI sequence against all 100 candidates.
  uint8_t idx = (uint8_t)(RC_VOLLEY_ITEM_FIRST + (int)random(RC_VOLLEY_ITEM_COUNT));
  g_volley_item    = (int8_t)idx;
  g_trial_dur_samp = g_item_dur_samp[idx - RC_VOLLEY_ITEM_FIRST];

  // THE BASELINE ARM'S AMPLITUDE, drawn HERE for exactly the reason the item is: unconditionally,
  // for all three arms. A draw that only one arm consumed would advance the RNG stream at a rate
  // depending on the arm sequence — the same leak the unconditional item draw exists to avoid.
  //
  // WHY ONE OF THE ITEM'S OWN PULSE AMPLITUDES. Volley and baseline were confounded by amplitude:
  // a volley runs at the master setting times the measured envelope (median 0.91 of master over
  // the library) while the arm sat at master/4 — an ~11 dB step that rides on top of the temporal
  // contrast the arm exists to isolate. Taking ONE pulse of the item this trial would have played
  // and holding it for the whole arm reproduces the volley's amplitude distribution BY
  // CONSTRUCTION, not by a matched constant the library could later drift away from; and the
  // value is inside [min, max] of that item's envelope by definition. Every drawable item carries
  // a rel_amp table, but the guard is free and mirrors eel_player.cpp.
  //
  // NOT rc_loc_amp(): that is the AMBIENT train's level (g_loc_amp), which deliberately stays at
  // volley/4. Amplitude reads as DISTANCE at these ranges, so the ambient fish stays further off
  // while both trial arms present the same nearer one.
  const StimItem* vit = &STIM_ITEMS[idx];
  g_base_amp = g_playback_volley_amp;
  if (vit->rel_amp) g_base_amp *= (float)vit->rel_amp[random(vit->n)] * (1.0f / 255.0f);

  g_trial_phase    = 0;
  g_trig_seen = g_trig_seq;   // consume the request (one playback per throw; later throws ignored)
  g_playing = true;

  // THE ONLY RECORD THIS TRIAL LEAVES, now that the water carries no code. It captures what was
  // REQUESTED alongside what was RESOLVED — the distinction between a genuinely blinded lever
  // throw and a bench-forced panel press, which nothing else can reconstruct.
  g_trial_id++;
  PlogRec r;
  log_fill(&r, PLOG_TRIAL);
  r.trial = g_trial_id;
  r.pol   = g_playback_pol;
  r.req   = log_trig_code(requested);
  r.res   = log_trig_code(kind);
  r.item  = g_volley_item;   // v4: populated for ALL THREE ARMS — it is what gives a silent
                             // arm a length. On a v3 file this column is empty, correctly:
                             // nothing was drawn for a sham there.
  plog_push(&g_plog, &r);

  if      (kind == RC_TRIG_VOLLEY)   begin_volley_burst();
  else if (kind == RC_TRIG_BASELINE) begin_base();
  else                               begin_sham();
}
static inline void go_idle() {
  out_silence();                                 // nothing playing, nothing scheduled -> hard brake, no drain
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
// volley or sham already in flight always runs to completion, exactly as an RC link
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
    case SRC_VOLLEY: {
      int16_t s;
      if (eel_player_next(&player, &s)) {
        if (s == 0) out_silence(); else out_write(s);
        if (player.last_onset != g_volley_onset) {
          g_volley_onset = player.last_onset;
          // NO per-pulse flash here any more. It would draw the volley's own pulse rate on the
          // LED, which is exactly the arm the blinding hides — see led_trial().
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
        led_trial();
        g_trial_phase++;
      } else {
        digitalWriteFast(LED_PIN, LOW);
        resume_after_playback();   // link loss NEVER aborts a volley — it runs to the end here
      }
      break;
    }
    // The BASELINE arm: VOLLEY-AMPLITUDE pulses from the trial's OWN rhythm, for exactly as long
    // as the drawn volley would have run. The level is g_base_amp — one pulse drawn from this
    // trial's item — so the arm now differs from a volley in TEMPORAL STRUCTURE ALONE.
    //
    // Its pulses are still logged as BASE, not LOC. That used to be because the two sat at the
    // same amplitude and nothing else could tell them apart; the reason is now the opposite (the
    // arm runs ~4x louder than the ambient train it interrupts), but the event code is still the
    // only thing that survives into an analysis, so it stays.
    //
    // Like the ambient train it stops at a PULSE BOUNDARY, never mid-EOD: a truncated pulse is
    // an artefact that looks like data. The arm therefore overruns its nominal length by at most
    // one EOD (2.62 ms) — which is why the length check is here and not on the raw phase.
    case SRC_BASE: {
      int onset = 0, boundary = 0;
      int16_t s = locgen_tick(&trial_loc, &onset, &boundary);
      if (s == 0) out_silence(); else out_write(s);
      if (onset) {
        PlogRec r;
        log_fill(&r, PLOG_BASE);
        r.trial = g_trial_id;
        r.item  = g_volley_item;   // the item whose LENGTH this arm borrowed
        r.pol   = g_playback_pol;
        r.amp_m = plog_milli(trial_loc.amp);
        plog_push(&g_plog, &r);
      }
      led_trial();
      g_trial_phase++;
      // Only ever ends in the silent gap between pulses.
      if (g_trial_phase >= g_trial_dur_samp && trial_loc.phase >= (uint32_t)EOD_HV_LEN) {
        digitalWriteFast(LED_PIN, LOW);
        resume_after_playback();
      } else if (boundary) {
        // Age by the interval that just completed — inside an arm the rhythm free-runs.
        locgen_reset(&trial_loc, draw_base_ipi(trial_loc.ipi), trial_loc.amp, trial_loc.pol);
      }
      break;
    }
    // The SILENCE arm: nothing in the water for the drawn duration. The LED runs the SAME
    // pattern as the other two arms — that is the whole point (see led_trial()).
    case SRC_SHAM: {
      out_silence();
      led_trial();
      g_trial_phase++;
      if (g_trial_phase >= g_trial_dur_samp) {
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
        if (trig_pending()) { begin_trial(g_trig_kind); break; }
        if (!g_loc_enabled) { go_idle(); break; }
      }
      int onset, boundary;
      int16_t s = locgen_tick(&loc, &onset, &boundary);
      if (s == 0) out_silence(); else out_write(s);
      if (onset) {
        led_flash();
        g_loc_last_onset = plog_tick_value(&g_tick64);   // the rhythm ages from HERE
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
      if (trig_pending()) { begin_trial(g_trig_kind); }
      else if (g_loc_enabled) { begin_loc(); }
      // LED in idle is owned by loop() (dark, the no-RC-signal blink, or the log-fault
      // inverse-blink).
      break;
    }
  }
}

// L3 provenance appended to EVERY log file's header (including one opened by a mid-session
// recovery — it is stored as a hook, not called once). These are the surface-specific
// constants an analysis needs to interpret the rows: the trial draw that decides a trial in the
// water, the range the volley item index is drawn from, and the arm weights.
static void log_header_hook(PulseLog* L) {
  plog_header_kv(L, "surface", 0);                 // 0 == eel_fakefish_rc
  plog_header_kv(L, "volley_item_first", RC_VOLLEY_ITEM_FIRST);
  plog_header_kv(L, "volley_item_count", RC_VOLLEY_ITEM_COUNT);
  // The three-arm draw (v4). These REPLACE v3's trial_p_volley_milli, which said "P(volley);
  // the rest are shams" — in a three-arm design that is not merely incomplete but wrong, since
  // two thirds of "the rest" is a fish quietly discharging rather than nothing at all.
  plog_header_kv(L, "trial_w_volley_milli",   TRIAL_W_VOLLEY_MILLI);
  plog_header_kv(L, "trial_w_baseline_milli", TRIAL_W_BASELINE_MILLI);
  plog_header_kv(L, "trial_w_silence_milli",  TRIAL_W_SILENCE_MILLI);
  // The BASELINE arm's own rhythm knobs — fixed constants, NOT the CH3/CH5 columns, so a log
  // cannot be read as if the arm had followed the live controls.
  plog_header_kv(L, "trial_base_tick_milli_hz",
                 (uint64_t)(TRIAL_BASE_TICK_HZ * 1000.0f + 0.5f));
  plog_header_kv(L, "trial_base_randomness_milli", plog_milli(TRIAL_BASE_RANDOMNESS));
  plog_header_kv(L, "loc_refractory_samp", LOC_REFRACTORY_SAMP);
  // The resting rhythm, so a log can be interpreted without the firmware source: which
  // model produced the localization intervals, and what the two knob columns mean.
  plog_header_kv(L, "loc_rhythm_fitted", 1);          // 0 == the retired lognormal draw
  plog_header_kv(L, "loc_nominal_tick_milli_hz", (uint64_t)(LOC_NOMINAL_TICK_HZ * 1000.0f + 0.5f));
  plog_header_kv(L, "loc_randomness_max_milli", plog_milli(LOC_RANDOMNESS_MAX));
  plog_header_kv(L, "loc_rate_anchor_median", 1);     // tick tempo held, not pulse dose
}

void setup() {
  // Output stage first: out_begin() holds both DRV IN1 HIGH, sets the 100 kHz carrier on both IN2
  // and brakes to the idle state. That ORDER is load-bearing (see out_hal.h) — never open-code it.
  out_begin();
#if AMP_DEBUG
  amp_debug_run();   // never returns: replaces normal operation with the scope-calibration routine
#endif
  randomSeed(analogRead(A0));                         // A0 == digital 14; unused elsewhere
  panel_begin();                                     // 2 panel buttons + the LED (pin 13)
  rc_begin();                                        // 4 RC pins + pin-change interrupts
  rc_zero_reset(&g_zero);                            // no session zero until the throttle supplies one
  for (int i = 0; i < RC_N_CHANNELS; i++) g_ch_us[i] = PLOG_ABSENT_U16;   // "never seen", not 0 us

  // Panel defaults so a bench unit (no transmitter) has a rate/randomness/amplitude.
  g_rate_tempo    = loc_rhythm_rate_for_hz(PANEL_RATE_HZ);
  g_tick_ipi_samp = (uint32_t)((float)SAMPLE_RATE_HZ / PANEL_RATE_HZ + 0.5f);
  g_randomness    = PANEL_RANDOMNESS;
  // The rhythm gets its OWN generator, seeded from random(), rather than sharing it. Sharing
  // would make the blinded volley/sham sequence depend on how many localization pulses happened
  // to be emitted first, which is precisely the kind of hidden coupling a blinded design must
  // not have. loc_rhythm_init also burns in LOC_BURN_IN intervals: a cold state starts
  // noticeably slow, because the components' stationary distribution is defined in continuous
  // time while the model is observed at its own pulse times.
  loc_rhythm_init(&rhythm, ((uint32_t)random(65536) << 16) ^ (uint32_t)random(65536),
                  g_rate_tempo, g_randomness);
  // The BASELINE arm's rhythm: a THIRD independent stream, separately seeded, on its own fixed
  // knobs. Same argument as above one level further out — if it shared `rhythm`, the control
  // condition would depend on how long the ambient train had been running, and the ambient
  // train's relaxation state would depend on how many baseline arms had fired.
  //
  // Every arm then gets a FRESH burned-in state, so this initial one only covers the window
  // before loop() first runs. Warm the spare here too, while the sample clock is still stopped:
  // that way the very first trial of a session is drawn from the same distribution as every
  // later one, rather than from whatever a cold init happened to leave behind.
  g_base_seed_rng = ((uint32_t)random(65536) << 16) ^ (uint32_t)random(65536);
  loc_rhythm_init(&trial_rhythm, base_seed_next() | 1u,
                  loc_rhythm_rate_for_hz(TRIAL_BASE_TICK_HZ), TRIAL_BASE_RANDOMNESS);
  base_warm_spare();
  g_base_spare_ready = 1;

  // Every trial's LENGTH comes from this table, whichever arm it resolves to. Summed once here
  // rather than in begin_trial(): an item runs to 457 pulses and the ISR has a 20 us budget.
  // ipi_samp[k] is the wait BEFORE pulse k (so [0] == 0); the item ends one EOD after the last
  // onset, which is what makes the silent arms exactly as long as the volley would have been.
  for (int i = 0; i < RC_VOLLEY_ITEM_COUNT; i++) {
    const StimItem* it = &STIM_ITEMS[RC_VOLLEY_ITEM_FIRST + i];
    uint32_t total = 0;
    for (uint16_t k = 0; k < it->n; k++) total += it->ipi_samp[k];
    g_item_dur_samp[i] = total + (uint32_t)EOD_HV_LEN;
  }

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

// Distinct "no RC signal" LED pattern (two quick blinks per second). loop()-owned, only while
// the ISR is not emitting a playback.
//
// SHOWN FROM BOOT, not only after a transmitter has been seen once. It used to be gated on
// g_rc_ever ("a bench unit that never had a transmitter stays dark"), which meant the one case
// where you most need to know — powered up, transmitter off or out of range, nothing has ever
// linked — was the one case that looked identical to a dead board. The bench cost is that a
// panel-only unit blinks this while idle, which is simply true: there is no link. Anything it
// actually does still overrides it, because the ISR owns the LED whenever something is playing.
static inline void no_signal_blink(uint32_t now_ms) {
  uint32_t ph = now_ms % NOSIG_LED_PERIOD_MS;
  bool on = (ph < NOSIG_BLINK_MS) ||
            (ph >= NOSIG_BLINK_SPACING_MS && ph < NOSIG_BLINK_SPACING_MS + NOSIG_BLINK_MS);
  digitalWriteFast(LED_PIN, on ? HIGH : LOW);
}

// "NOT ZEROED": link up, but the session zero has not been captured, so the RC path is inert.
// Three quick blinks — one more than the no-link pattern, because both mean "waiting on the RC
// side" and the count is what tells them apart. Normally invisible: the transmitter's own
// throttle interlock means the zero lands within ~100 ms of the link appearing.
static inline void not_zeroed_blink(uint32_t now_ms) {
  uint32_t ph = now_ms % NOTZERO_LED_PERIOD_MS;
  bool on = (ph < NOTZERO_BLINK_MS) ||
            (ph >= NOTZERO_BLINK_GAP_MS && ph < NOTZERO_BLINK_GAP_MS + NOTZERO_BLINK_MS) ||
            (ph >= 2u * NOTZERO_BLINK_GAP_MS && ph < 2u * NOTZERO_BLINK_GAP_MS + NOTZERO_BLINK_MS);
  digitalWriteFast(LED_PIN, on ? HIGH : LOW);
}

// "READY": logging healthy, RC link up, and localization OFF — one short blink every two
// seconds. The state that used to be a DARK LED, indistinguishable from a crashed board or a
// dead battery. It is also what makes "the throttle is down" readable from shore: pull the
// throttle to rest and the per-pulse flashing must give way to this heartbeat, promptly.
static inline void ready_blink(uint32_t now_ms) {
  digitalWriteFast(LED_PIN, (now_ms % READY_LED_PERIOD_MS) < READY_BLINK_MS ? HIGH : LOW);
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

  // ----- re-warm the BASELINE arm's next fish -------------------------------------------
  // Single-slot SPSC handoff, loop() the producer: it only ever writes g_base_spare while the
  // flag is clear, and the ISR only ever reads it while the flag is set. The flag is the
  // release, so it is written LAST here and cleared only after the copy there — the same
  // publish/latch idiom as g_trig_seq/g_trig_seen.
  //
  // Costs ~320 interval draws (a few hundred microseconds) once per baseline arm, in loop()
  // where it is free. It cannot go in the ISR: that is ~4500 libm calls against a 20 us tick.
  if (!g_base_spare_ready) {
    base_warm_spare();
    g_base_spare_ready = 1;
  }
  g_log_ok = plog_healthy(&g_plog);

  // ----- panel buttons (OR-ed with RC; every loop so a held button can't retrigger) -----
  static bool panel_loc_state = false;
  int loc_fell, trial_fell;
  panel_poll(&loc_fell, &trial_fell);
  if (loc_fell)   panel_loc_state = !panel_loc_state;
  // RC_TRIG_RANDOM, not an explicit arm: the panel is blinded like the lever (2026-08-24).
  if (trial_fell) request_trigger(RC_TRIG_RANDOM);

  // ----- RC decode, rate-limited to ~200 Hz -----
  static uint32_t last_decode_ms = 0;
  static bool     primed = false;
  static bool     rc_loc_state = false;
  static uint32_t thr_on_count = 0;   // consecutive above-CH3_ON_THRESH decode ticks (throttle-on debounce)
  static float    u_thr = 0.0f, u_trig = 0.5f, u_rnd = 0.0f, u_amp = 0.0f;
  static int      rate_level = 0, rnd_level = 0, amp_level = 0;
  static RcTrigger trig = {};
  static bool     trig_prev_present = false;

  if ((uint32_t)(now_ms - last_decode_ms) >= RC_DECODE_PERIOD_MS) {
    last_decode_ms = now_ms;

    bool thr_present  = rc_present_ms(RC_IDX_THROTTLE, now_ms);   // the primary / failsafe channel
    bool trig_present = rc_present_ms(RC_IDX_TRIGGER,  now_ms);
    bool rnd_present  = rc_present_ms(RC_IDX_RANDOM,   now_ms);
    bool amp_present  = rc_present_ms(RC_IDX_AMP,      now_ms);
    bool trig_acquired = trig_present && !trig_prev_present;   // CH4 absent -> present edge
    trig_prev_present = trig_present;
    g_link_up = thr_present;

    if (thr_present) {
      // THE SESSION ZERO, measured from the throttle and applied to ALL FOUR channels. The
      // transmitter refuses to transmit until the throttle is at its stop, so the first widths
      // that ever arrive carry a resting stick; that reading becomes the zero, at the battery
      // state actually in use. See rc_control.h -> "THE SESSION ZERO" for why a fixed calibration
      // cannot do this and why the pull-up that would have fixed it in hardware does not work on
      // this opto. Captured once per power-on and never reset by a link drop — re-capturing on
      // reacquisition would sample whatever position the stick was left in.
      const uint32_t thr_us = rc_get_width_us(RC_IDX_THROTTLE);
      const bool armed = rc_zero_step(&g_zero, thr_us) != 0;
      const int32_t zoff = rc_zero_offset(&g_zero, RC_CAL_THROTTLE_MIN);
      g_rc_zeroed = armed;

      // Publish the RAW decode for the log (v3). Clamped into the u16 column, with the absent
      // sentinel reserved: the glitch filter already bounds real widths to 400..3000 us, so this
      // only ever fires on a channel that has never been seen.
      for (int i = 0; i < RC_N_CHANNELS; i++) {
        uint32_t w = rc_get_width_us((uint8_t)i);
        g_ch_us[i] = (w == 0 || w >= PLOG_ABSENT_U16) ? PLOG_ABSENT_U16 : (uint16_t)w;
      }
      g_zero_us = g_zero.zero_us ? (uint16_t)g_zero.zero_us : PLOG_ABSENT_U16;

      float raw_thr  = rc_unit_off(thr_us,                          RC_CAL[RC_IDX_THROTTLE], zoff);
      float raw_trig = rc_unit_off(rc_get_width_us(RC_IDX_TRIGGER), RC_CAL[RC_IDX_TRIGGER],  zoff);
      float raw_rnd  = rc_unit_off(rc_get_width_us(RC_IDX_RANDOM),  RC_CAL[RC_IDX_RANDOM],   zoff);
      float raw_amp  = rc_unit_off(rc_get_width_us(RC_IDX_AMP),     RC_CAL[RC_IDX_AMP],      zoff);

      if (!primed) {
        // FIRST decode after (re)acquisition: SNAP filters to the live readings (no EMA ramp) and
        // re-init the trigger, so the true stick positions decode on tick 1 (no ramp lag, and the
        // trigger arms from the ACTUAL axis position -> no boot-time volley/sham).
        primed = true;
        u_thr = raw_thr;
        if (trig_present) u_trig = raw_trig;
        if (rnd_present)  u_rnd  = raw_rnd;
        if (amp_present)  u_amp  = raw_amp;
        trig = {};
        rate_level = rc_quantize_hyst(rc_throttle_frac(u_thr), rate_level, RC_RATE_STEPS,   0.0f);
        rnd_level  = rc_quantize_hyst(u_rnd,  rnd_level,  RC_RANDOM_STEPS, 0.0f);
        amp_level  = rc_quantize_hyst(u_amp,  amp_level,  RC_AMP_STEPS,    0.0f);
      } else {
        u_thr = rc_ema(u_thr, raw_thr, RC_EMA_ALPHA);
        if (trig_acquired) { u_trig = raw_trig; trig = {}; }   // CH4 re-acquired mid-run: snap + arm from truth
        else if (trig_present) u_trig = rc_ema(u_trig, raw_trig, RC_EMA_ALPHA);
        if (rnd_present)  u_rnd  = rc_ema(u_rnd,  raw_rnd,  RC_EMA_ALPHA);
        if (amp_present)  u_amp  = rc_ema(u_amp,  raw_amp,  RC_EMA_ALPHA);
        rate_level = rc_quantize_hyst(rc_throttle_frac(u_thr), rate_level, RC_RATE_STEPS,   RC_QUANT_HYST);
        rnd_level  = rc_quantize_hyst(u_rnd,  rnd_level,  RC_RANDOM_STEPS, RC_QUANT_HYST);
        amp_level  = rc_quantize_hyst(u_amp,  amp_level,  RC_AMP_STEPS,    RC_QUANT_HYST);
      }

      // CH3: localization on/off (DEBOUNCED + hysteretic, gated on the RAW reading so an RC noise glitch
      // can never fire a stray pulse at rest) + rate. Enable needs raw_thr >= CH3_ON_THRESH held for
      // CH3_ON_DEBOUNCE_TICKS ticks; disable is immediate below CH3_OFF_DEADBAND.
      // NOT ARMED -> THE RC PATH DOES NOT STIMULATE. Before the zero is known every threshold on
      // every channel is wrong by the same ~0.2 of travel, so this gates the trigger too, not just
      // localization: an ungated CH4 could fire from a stick that is not actually thrown. The knob
      // values above are still updated (harmless — nothing can play), and the bench panel is
      // deliberately untouched, because it has no transmitter to wait for.
      if (!armed) {
        rc_loc_state = false;
        thr_on_count = 0;
      } else {
        rc_loc_state = rc_throttle_gate(raw_thr, rc_loc_state, &thr_on_count,
                                        CH3_ON_THRESH, CH3_OFF_DEADBAND, CH3_ON_DEBOUNCE_TICKS);
      }
      // ...AND A THROTTLE AT REST IS A MASTER OFF, not merely one vote in the OR below.
      // g_loc_enabled is `rc_loc_state || panel_loc_state`, so a latched panel LOC toggle used
      // to make localization impossible to stop from the transmitter — which is the ONLY
      // control the operator still has once the boat is on the water, and the panel button is
      // easy to knock on while launching it. Clearing the latch here makes "throttle down"
      // mean zero pulses unconditionally.
      //
      // Gated on thr_present (this whole branch): with no transmitter at all the else branch
      // below runs instead and never touches the latch, so a bench unit driven from the panel
      // alone is unaffected.
      //
      // THE CONSEQUENCE, STATED SO IT IS NOT A SURPRISE: with a transmitter connected AND the
      // throttle at rest, the panel LOC button no longer latches — a press is cleared within one
      // decode tick. That is what "master off" means; a master off a button can override is not
      // one. Push the throttle to use the panel toggle, or run the bench unit with no receiver.
      if (raw_thr < CH3_OFF_DEADBAND) panel_loc_state = false;
      // CH3 rate. The ladder is a TICK TEMPO (1/median interval), so the tempo multiplier and
      // the nominal median IPI the log carries are two views of the same setting.
      float tick_hz   = rc_rate_to_hz(rate_level, RC_RATE_STEPS);
      g_rate_tempo    = loc_rhythm_rate_for_hz(tick_hz);
      g_tick_ipi_samp = (uint32_t)((float)SAMPLE_RATE_HZ / tick_hz + 0.5f);
      // CH5: randomness (pot; rnd_level held on CH5 loss -> g_randomness holds)
      g_randomness = rc_randomness(rnd_level, RC_RANDOM_STEPS);
      // CH6: amplitude pot -> VOLLEY (max) level; localization is derived as half the volley
      float amp = rc_master_amp(amp_level, RC_AMP_STEPS);
      g_volley_amp = amp;
      g_loc_amp    = rc_loc_amp(amp);
      // CH4: one-shot trigger (only on a live channel). Throwing HIGH requests a BLINDED
      // trial — the ISR draws volley-vs-sham. Throwing low does nothing at all: the
      // operator cannot pick the trial type, so their timing and position cannot correlate
      // with it. The bench panel keeps explicit volley/sham buttons for testing.
      if (trig_present && armed) {
        if (rc_trigger_step(&trig, u_trig, CH4_VOLLEY_THRESH, CH4_CENTER_LO, CH4_CENTER_HI))
          request_trigger(RC_TRIG_RANDOM);
      }
    } else {
      // CH3 (throttle) signal loss -> throttle zero -> localization OFF. The trigger never fires
      // without a live throw, so loss can NEVER start a volley/sham. Re-prime on reacquisition.
      primed = false;
      rc_loc_state = false;
      thr_on_count = 0;   // re-debounce the throttle-on when the channel is reacquired
      // The session zero is deliberately NOT reset here (rc_control.h, property 3): re-capturing
      // on reacquisition would sample whatever position the stick was left in, which is the one
      // case the transmitter's throttle interlock does not cover.
    }
    g_loc_enabled = rc_loc_state || panel_loc_state;
  }

  // ----- idle LED (loop owns the LED only while the ISR is not emitting a playback) -----
  // Severity order, most severe first. A logging fault OUTRANKS no-link because it is the one
  // condition actively SUPPRESSING stimulation; no-link outranks ready because a ready-looking
  // idle unit with no transmitter is a lie. There is never contention with the ISR's per-pulse
  // flash here — every branch below implies nothing is playing, so g_playing is false and this
  // owns the LED outright.
  //
  // THE FINAL BRANCH IS A BLINK, NOT DARKNESS. Every state the device can be in now has a
  // pattern (panel_control.h lists the whole vocabulary), so the only time the LED is dark is
  // the gap between localization pulses — which the ISR owns, because g_playing stays true
  // across a gap. A dark LED therefore means "running, mid-gap"; it never means "wedged".
  if (!g_playing) {
    if (!plog_healthy(&g_plog))      log_fault_blink(now_ms);
    else if (!g_link_up)             no_signal_blink(now_ms);
    else if (!g_rc_zeroed)           not_zeroed_blink(now_ms);
    else                             ready_blink(now_ms);
  }
}
