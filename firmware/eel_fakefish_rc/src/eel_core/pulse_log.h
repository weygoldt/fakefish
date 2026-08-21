// pulse_log.h — L2 sample-side companion: per-pulse EVENT LOGGING to the SD card.
//
// WHY. The RC device live-synthesises its stimuli, and its localization train is built to be
// indistinguishable from a real cruising eel — single-polarity EOD pulses at 0.5..20 Hz with
// lognormal jitter. Volley/sham TRIALS are identifiable in a recording (the count-coded pulse
// marker tags them), but a localization pulse is not. Without a log, every analysis of a
// recording made during playback must treat an unknown subset of pulses as possibly ours.
// This file records, on the card, the exact sample tick of EVERY pulse the device emits.
//
// It is also the on-device GROUND TRUTH for the blinded trigger: the firmware draws
// volley-vs-sham in the ISR, so the marker in the water and this log are two independent
// records of the same fact. That redundancy is deliberate.
//
// THE MIRROR IMAGE OF sd_player.h. There the ISR pops samples that loop() read from the card;
// here the ISR PUSHES event records that loop() writes to the card. The rule is the same and
// it is load-bearing: THE ISR NEVER TOUCHES THE SD CARD. It fills a lock-free SPSC ring with
// plain POD records; all formatting and all file I/O happen in loop().
//
// SINGLE PRODUCER, SINGLE CONSUMER — and the producer is the ISR, ALONE. loop() must never
// push. Events that originate in loop() (e.g. RC link state) are published as a volatile word
// and WATCHED by the ISR, which pushes the record itself; that is the same publish/latch idiom
// the sketch already uses for the trigger (g_trig_seq / g_trig_seen). It keeps the ring
// strictly single-producer and gives the record an exact tick for free.
//
// TIME BASE. Records are stamped with a 64-bit sample counter, not the RTC. The sample clock
// IS the output's time base (20 us resolution, and by construction the same clock that placed
// the pulse); the Teensy RTC has 1 s resolution. Absolute time is recovered by interpolating
// between periodic ANCHOR records, which pair a tick with an RTC reading. That makes the whole
// design RTC-OPTIONAL: with no coin cell you lose the absolute anchor and keep exact relative
// timing, and nothing else changes.
//
// The 64-bit counter is SEPARATE from the sketch's uint32_t g_tick on purpose — the LED code
// relies on 32-bit wrap arithmetic ((int32_t)(g_tick - g_led_off_at) >= 0) and widening it
// would silently break that idiom. This one is ISR-owned and never read from loop(), so it
// needs no locking at all.
//
// PURE / GLUE SPLIT, as in sd_player.h: the ring, the record, the CSV formatting, the file
// name/index chooser and the number formatting are ABOVE the `#ifdef ARDUINO` line, so they
// compile and are unit-tested on a PC (firmware/eel_core/host_test/pulse_log_selftest).
//
// See firmware/README.md ("Pulse logging") for the file format and the alignment procedure.
#pragma once
#include <stdint.h>
#include <stddef.h>
#include "eel_stimuli.h"   // STIM_SAMPLE_RATE_HZ — the one place the sample rate is defined

// Bumped only for a BREAKING change to the row schema or the header keys. The Python reader
// (src/fakefish/pulse_log.py) refuses a version it does not know.
//
// v2 (2026-08-21) renamed two localization columns when the resting rhythm became a fitted
// model rather than a lognormal draw: `cv_m` -> `rand_m` and `rate_ipi` -> `tick_ipi`. Both
// carry a genuinely different quantity, so renaming was the point. `cv_m` held a coefficient
// of variation and `rand_m` holds the model's randomness knob (0 = metronome, 1 = the measured
// eel, up to 1.5); `rate_ipi` held the MEAN interval the rate knob asked for, `tick_ipi` holds
// the MEDIAN one, and those differ by about a factor of two on a heavy-tailed distribution.
// Reusing either name would have left every v1-era analysis silently misreading a v2 file —
// which is exactly what a format version is for.
#define PULSELOG_FORMAT_VERSION 2

// ===== Event types ====================================================================
// One row per event. PULSE rows (LOC / MARKER / VOLLEY) are one per emitted pulse, ALWAYS —
// never summarised, never collapsed. The rest are context.
enum PlogEvent {
  PLOG_BOOT   = 0,  // first row of a file: the device came up (val = file index)
  PLOG_LOC,         // one localization pulse
  PLOG_MARKER,      // one pulse of the count-coded trial marker
  PLOG_VOLLEY,      // one pulse of a volley
  PLOG_TRIAL,       // a trial began: req = what was asked for, res = what the ISR drew
  PLOG_SHAM,        // the sham fired — NO water output, so without this row it is invisible
  PLOG_LOCON,       // the localization train started
  PLOG_LOCOFF,      // the localization train stopped (disabled, log fault, or trial preempt)
  PLOG_LINK,        // RC link state changed (val: 1 = acquired, 0 = lost)
  PLOG_ANCHOR,      // periodic tick <-> RTC anchor + liveness proof (val = RTC unix seconds)
  PLOG_DROP,        // val records were LOST to a full ring immediately before this tick
  PLOG_GAP          // logging was interrupted; val = index of the file that was cut short
};

// Trial-kind codes, stored as their literal CSV characters.
//   'R' == RANDOM: the RC lever asked for a BLINDED trial and the ISR drew the outcome.
//   'V' / 'S'     : an explicit panel-button request (bench), or the resolved outcome.
// Recording BOTH the requested and the resolved kind is what distinguishes a genuinely
// blinded trial from a bench-forced one; with only the outcome they are indistinguishable.
#define PLOG_KIND_NONE   0
#define PLOG_KIND_RANDOM 'R'
#define PLOG_KIND_VOLLEY 'V'
#define PLOG_KIND_SHAM   'S'

// ===== "Absent" sentinels =============================================================
// Every absent field renders as an EMPTY CSV column, never as a number.
//
// THE ITEM SENTINEL MUST NOT BE 0, AND MUST NOT REACH THE FILE AS -1.
// STIM_ITEMS[0] is a real recorded volley, so a 0 default would silently attribute every
// localization and marker pulse to it. -1 is safe in C (no wrap-around indexing) but is NOT
// safe to write out: in Python STIM_ITEMS[-1] does not raise, it quietly returns the LAST
// item. So -1 is the in-memory sentinel and the CSV column is left EMPTY, which pandas reads
// as NaN and which can never be used as an index by accident.
#define PLOG_ABSENT_ITEM ((int8_t)-1)
#define PLOG_ABSENT_U16  ((uint16_t)0xFFFFu)
#define PLOG_ABSENT_U32  ((uint32_t)0xFFFFFFFFu)
// Used only by rows loop() writes itself (the GAP row), which cannot read the ISR-owned tick.
// A literal 0 would be indistinguishable from BOOT's legitimate tick 0.
#define PLOG_ABSENT_TICK ((uint64_t)0xFFFFFFFFFFFFFFFFull)

// An RTC reading below this is treated as NOT SET (no coin cell, or never programmed).
// 1600000000 == 2020-09-13; the device did not exist before that.
#define PLOG_RTC_MIN_VALID 1600000000u
static inline int plog_rtc_valid(uint32_t unix_s) { return unix_s >= PLOG_RTC_MIN_VALID; }

// ===== The record (pure POD — this is what the ISR pushes) ============================
// Kept small and trivially copyable: the ISR fills one on its stack and pushes it. No
// snprintf, no String, no SD call, no Serial, nothing unbounded ever happens in the ISR.
//
// Settings are stored as INTEGERS (milli-units, and raw samples) rather than floats. That
// removes float formatting from loop(), makes rows exact and byte-reproducible, and avoids
// the locale hazard of "%f" — which under a comma-decimal locale emits "0,5" and silently
// breaks the CSV column structure.
typedef struct {
  uint64_t tick;      // 64-bit sample counter at the event (20 us resolution)
  uint32_t tick_ipi;  // nominal MEDIAN localization IPI for the set tempo, whole samples
                      // (ABSENT_U32 = n/a). Median, not mean: the rate knob anchors the
                      // tick tempo, and the two differ ~2x on this distribution.
  uint32_t val;       // event-specific integer, see PlogEvent (ABSENT_U32 = n/a)
  uint16_t pulse;     // index of this pulse WITHIN its item, 0-based (ABSENT_U16 = n/a)
  uint16_t trial;     // trial id, 1-based (0 = n/a)
  uint16_t amp_m;     // amplitude applied to THIS pulse x1000 (ABSENT_U16 = n/a)
  uint16_t master_m;  // master (volley) amplitude setting x1000 (ABSENT_U16 = n/a)
  uint16_t rand_m;    // localization RANDOMNESS knob x1000 (ABSENT_U16 = n/a). 0 = a
                      // metronome, 1000 = the measured eel. Not a jitter amount.
  int8_t   item;      // library item index (ABSENT_ITEM = n/a -> EMPTY column)
  int8_t   pol;       // playback polarity +1 / -1 (0 = n/a)
  uint8_t  ev;        // PlogEvent
  uint8_t  req;       // requested trial kind (PLOG_KIND_*; 0 = n/a)
  uint8_t  res;       // resolved  trial kind (PLOG_KIND_*; 0 = n/a)
} PlogRec;

// Reset every field to "absent" and stamp the event + tick. ALWAYS start from this — a
// hand-initialised record risks leaving a stale field that reads as real data.
static inline void plog_rec_init(PlogRec* r, uint8_t ev, uint64_t tick) {
  r->tick     = tick;
  r->tick_ipi = PLOG_ABSENT_U32;
  r->val      = PLOG_ABSENT_U32;
  r->pulse    = PLOG_ABSENT_U16;
  r->trial    = 0;
  r->amp_m    = PLOG_ABSENT_U16;
  r->master_m = PLOG_ABSENT_U16;
  r->rand_m   = PLOG_ABSENT_U16;
  r->item     = PLOG_ABSENT_ITEM;
  r->pol      = 0;
  r->ev       = ev;
  r->req      = PLOG_KIND_NONE;
  r->res      = PLOG_KIND_NONE;
}

// A 0..1-ish control value as milli-units. Clamps to 65534 so it can never collide with the
// ABSENT_U16 sentinel, and maps NaN/negative to 0 (the `!(x > 0)` test catches NaN).
static inline uint16_t plog_milli(float x) {
  if (!(x > 0.0f)) return 0;
  long v = (long)(x * 1000.0f + 0.5f);
  if (v > 65534) v = 65534;
  return (uint16_t)v;
}

// ===== 64-bit sample tick (pure; ISR-owned) ===========================================
// Split into two 32-bit halves so the increment is a single word write in the common case.
// It is NEVER read from loop() — every record is stamped by the ISR that pushes it — so there
// is no torn-read hazard and no lock is required. (That is exactly why loop()-originated
// events are routed through the ISR rather than pushed directly.)
typedef struct { uint32_t lo, hi; } PlogTick;

static inline void plog_tick_reset(PlogTick* t) { t->lo = 0; t->hi = 0; }
static inline void plog_tick_advance(PlogTick* t) { if (++t->lo == 0u) t->hi++; }
static inline uint64_t plog_tick_value(const PlogTick* t) {
  return ((uint64_t)t->hi << 32) | (uint64_t)t->lo;
}

// ===== Lock-free SPSC ring (pure) =====================================================
// Same discipline as SdpRing in sd_player.h: free-running 32-bit head/tail masked on access,
// SIZE a power of two, each side touching only its own counter. Producer = the sample-clock
// ISR; consumer = loop(). Safe without a lock on the M7 (single core, aligned 32-bit word
// access is atomic, and the ISR preempts loop() rather than running concurrently).
//
// OVERFLOW IS RECORDED, NEVER SILENT. If the ring fills — realistically only when an SD
// block-erase stalls loop() during a volley burst — the ISR counts the losses and the NEXT
// successful push emits a PLOG_DROP row carrying the count. An experiment log that quietly
// omits pulses turns into wrong analysis; one that admits a gap turns into a caveat.
template <uint32_t SIZE>
struct PlogRing {
  static_assert((SIZE & (SIZE - 1)) == 0, "PlogRing SIZE must be a power of two");
  PlogRec           buf[SIZE];
  volatile uint32_t head;           // next write slot (producer / ISR)
  volatile uint32_t tail;           // next read slot  (consumer / loop)
  uint32_t          pending_drops;  // PRODUCER-ONLY: records lost since the last DROP row
  uint32_t          total_drops;    // PRODUCER-ONLY: lifetime count, for diagnostics

  void reset() { head = 0; tail = 0; pending_drops = 0; total_drops = 0; }
  uint32_t available() const { return head - tail; }
  uint32_t space() const { return SIZE - (head - tail); }
  bool empty() const { return head == tail; }

  // Unchecked store + publish. The barrier orders the data write BEFORE the head increment,
  // so a preempting consumer can never see head advance past a not-yet-written slot.
  void raw_push(const PlogRec* r) {
    buf[head & (SIZE - 1)] = *r;
    asm volatile("" ::: "memory");   // release: data store before index publish
    head = head + 1;
  }

  // Producer: push one record. Returns false when the record was dropped (ring full).
  // When drops are pending it reserves TWO slots so the DROP row and the record that
  // follows it land together — the DROP row carries the tick of that following record, i.e.
  // "n records were lost immediately before this tick".
  bool push(const PlogRec* r) {
    if (pending_drops) {
      if (space() < 2) { pending_drops++; total_drops++; return false; }
      PlogRec d;
      plog_rec_init(&d, (uint8_t)PLOG_DROP, r->tick);
      d.val = pending_drops;
      pending_drops = 0;
      raw_push(&d);
      raw_push(r);
      return true;
    }
    if (space() == 0) { pending_drops = 1; total_drops++; return false; }
    raw_push(r);
    return true;
  }

  // Consumer: pop one record into *out. Returns false when empty.
  //
  // NOTE THE SECOND BARRIER — this is where PlogRing must DIVERGE from SdpRing, because the
  // roles are inverted. In sd_player.h the consumer is the ISR, which its producer (loop())
  // cannot preempt, so its pop() needs no release barrier. Here the consumer is loop() and the
  // 50 kHz ISR preempts it at will. `buf` is non-volatile while `tail` is volatile, and the
  // compiler may move non-volatile accesses across a volatile one: a PlogRec is 32 bytes, so
  // the copy is a multi-instruction sequence with real freedom to sink below the tail store.
  // (Verified: arm-none-eabi-g++ 11.3.1 at -O2 — the Teensy 4.1 IDE default — does exactly
  // that, splitting the field loads either side of the publish.) Publishing tail early tells
  // the producer this slot is free while it is still being read, and push() would then overwrite
  // it mid-copy, emitting a row spliced from TWO events — with no DROP marker, because the push
  // succeeded. A fabricated pulse at a fabricated tick, in the file whose entire purpose is
  // ground truth. sd_player's single int16 load left no such window; a 32-byte record does.
  bool pop(PlogRec* out) {
    if (head == tail) return false;
    asm volatile("" ::: "memory");   // acquire: index check before data read
    *out = buf[tail & (SIZE - 1)];
    asm volatile("" ::: "memory");   // release: record copy COMPLETE before the slot is freed
    tail = tail + 1;
    return true;
  }
};

// ===== Bounded text emitter (pure) ====================================================
// Appends without ever writing past `cap`, while still counting the FULL length that would
// have been produced — so a caller can detect truncation instead of silently shipping a
// half-row. Deliberately not snprintf: "%llu" is not reliably available in a size-optimised
// embedded libc, and hand-rolling keeps the pure layer dependency-free and host-testable.
typedef struct { char* p; size_t cap; size_t len; } PlogBuf;

static inline void plog_buf_init(PlogBuf* b, char* p, size_t cap) { b->p = p; b->cap = cap; b->len = 0; }
static inline int  plog_buf_truncated(const PlogBuf* b) { return b->len >= b->cap; }
static inline void plog_put_ch(PlogBuf* b, char c) { if (b->len < b->cap) b->p[b->len] = c; b->len++; }
static inline void plog_put_str(PlogBuf* b, const char* s) { while (*s) plog_put_ch(b, *s++); }
static inline void plog_put_u64(PlogBuf* b, uint64_t v) {
  char t[20];
  size_t n = 0;
  do { t[n++] = (char)('0' + (int)(v % 10u)); v /= 10u; } while (v);
  while (n) plog_put_ch(b, t[--n]);
}
static inline void plog_put_i32(PlogBuf* b, int32_t v) {
  if (v < 0) { plog_put_ch(b, '-'); plog_put_u64(b, (uint64_t)(-(int64_t)v)); }
  else plog_put_u64(b, (uint64_t)v);
}
// NUL-terminate if there is room; returns the full (untruncated) length.
static inline size_t plog_buf_finish(PlogBuf* b) {
  if (b->len < b->cap) b->p[b->len] = '\0';
  else if (b->cap) b->p[b->cap - 1] = '\0';
  return b->len;
}

// ===== CSV rendering (pure) ===========================================================
// The column order is the file's contract; see firmware/README.md and the Python reader.
#define PLOG_COLUMNS "seq,tick,event,item,pulse,trial,pol,amp_m,master_m,rand_m,tick_ipi,val,req,res"
#define PLOG_ROW_MAX 128u   // longest possible row incl. '\n' and NUL, with headroom

static inline const char* plog_event_name(uint8_t ev) {
  switch (ev) {
    case PLOG_BOOT:   return "BOOT";
    case PLOG_LOC:    return "LOC";
    case PLOG_MARKER: return "MARKER";
    case PLOG_VOLLEY: return "VOLLEY";
    case PLOG_TRIAL:  return "TRIAL";
    case PLOG_SHAM:   return "SHAM";
    case PLOG_LOCON:  return "LOCON";
    case PLOG_LOCOFF: return "LOCOFF";
    case PLOG_LINK:   return "LINK";
    case PLOG_ANCHOR: return "ANCHOR";
    case PLOG_DROP:   return "DROP";
    case PLOG_GAP:    return "GAP";
    default:          return "?";
  }
}

// Render one record as a CSV row INCLUDING the trailing newline. Absent fields are written
// as empty columns. Returns the full length (>= cap means it was truncated and MUST NOT be
// written to the file).
static inline size_t plog_format_row(const PlogRec* r, uint32_t seq, char* out, size_t cap) {
  PlogBuf b;
  plog_buf_init(&b, out, cap);
  plog_put_u64(&b, seq);                                            // seq
  plog_put_ch(&b, ',');  if (r->tick != PLOG_ABSENT_TICK) plog_put_u64(&b, r->tick);   // tick
  plog_put_ch(&b, ',');  plog_put_str(&b, plog_event_name(r->ev));  // event
  // item: EMPTY when absent — never the -1 sentinel (see PLOG_ABSENT_ITEM).
  plog_put_ch(&b, ',');  if (r->item != PLOG_ABSENT_ITEM)    plog_put_i32(&b, r->item);
  plog_put_ch(&b, ',');  if (r->pulse != PLOG_ABSENT_U16)    plog_put_u64(&b, r->pulse);
  plog_put_ch(&b, ',');  if (r->trial != 0)                  plog_put_u64(&b, r->trial);
  plog_put_ch(&b, ',');  if (r->pol   != 0)                  plog_put_i32(&b, r->pol);
  plog_put_ch(&b, ',');  if (r->amp_m    != PLOG_ABSENT_U16) plog_put_u64(&b, r->amp_m);
  plog_put_ch(&b, ',');  if (r->master_m != PLOG_ABSENT_U16) plog_put_u64(&b, r->master_m);
  plog_put_ch(&b, ',');  if (r->rand_m   != PLOG_ABSENT_U16) plog_put_u64(&b, r->rand_m);
  plog_put_ch(&b, ',');  if (r->tick_ipi != PLOG_ABSENT_U32) plog_put_u64(&b, r->tick_ipi);
  plog_put_ch(&b, ',');  if (r->val      != PLOG_ABSENT_U32) plog_put_u64(&b, r->val);
  plog_put_ch(&b, ',');  if (r->req != PLOG_KIND_NONE)       plog_put_ch(&b, (char)r->req);
  plog_put_ch(&b, ',');  if (r->res != PLOG_KIND_NONE)       plog_put_ch(&b, (char)r->res);
  plog_put_ch(&b, '\n');
  return plog_buf_finish(&b);
}

// Render one "#key=value" header line (with newline). The header block is a flat list of
// these, so the Python reader parses it generically and an added key never breaks an old
// reader.
static inline size_t plog_format_kv(const char* key, uint64_t val, char* out, size_t cap) {
  PlogBuf b;
  plog_buf_init(&b, out, cap);
  plog_put_ch(&b, '#');
  plog_put_str(&b, key);
  plog_put_ch(&b, '=');
  plog_put_u64(&b, val);
  plog_put_ch(&b, '\n');
  return plog_buf_finish(&b);
}

// ===== Header block (pure) ============================================================
// The provenance stamped at the top of every file: enough to tie the rows back to the code
// AND the stimulus library that produced them. A log that cannot be tied to its origin is
// worth much less a year later, and this repo already treats the library as a byte-frozen
// contract with recorded provenance.
//
// It is emitted through a SINK rather than written directly so the key list lives in exactly
// ONE place and is host-testable: the firmware passes a sink that writes to the SD file, the
// self-test passes one that writes to stdout. That is what lets the committed golden log
// (tests/data/pulse_log_golden.csv) be produced by the real firmware code path rather than by
// a hand-copied duplicate that could silently drift from it.
//
// There is deliberately NO git commit hash. It would change on every commit, so no generated
// file carrying it could ever pass this repo's "generated files are in sync" gate.
// __DATE__ " " __TIME__ identifies a binary just as well and needs no codegen.
typedef struct {
  uint32_t    format_version;
  uint32_t    file_index;
  uint32_t    sample_rate_hz;
  uint32_t    rtc_unix;               // RTC at file open; 0/small == not set (see plog_rtc_valid)
  uint32_t    boot_rtc_unix;          // RTC at the FIRST file of this power-cycle
  uint32_t    stim_format_version;
  uint32_t    n_stim_items;
  uint32_t    eod_hv_len;
  uint32_t    eod_net_integral_x1000; // cheap fingerprint of the EOD waveform itself
  uint32_t    anchor_period_samp;
  uint32_t    ring_size;
  const char* build;                  // __DATE__ " " __TIME__
} PlogHeader;

typedef void (*PlogSink)(void* ctx, const char* s, size_t n);

static inline void plog_emit_kv(PlogSink sink, void* ctx, const char* key, uint64_t val) {
  char line[96];
  size_t n = plog_format_kv(key, val, line, sizeof(line));
  if (n < sizeof(line)) sink(ctx, line, n);
}

static inline void plog_emit_header(const PlogHeader* h, PlogSink sink, void* ctx) {
  sink(ctx, "#fakefish-pulse-log\n", sizeof("#fakefish-pulse-log\n") - 1);
  plog_emit_kv(sink, ctx, "format_version",         h->format_version);
  plog_emit_kv(sink, ctx, "file_index",             h->file_index);
  plog_emit_kv(sink, ctx, "sample_rate_hz",         h->sample_rate_hz);
  plog_emit_kv(sink, ctx, "rtc_unix",               h->rtc_unix);
  plog_emit_kv(sink, ctx, "rtc_valid",              plog_rtc_valid(h->rtc_unix) ? 1u : 0u);
  plog_emit_kv(sink, ctx, "boot_rtc_unix",          h->boot_rtc_unix);
  plog_emit_kv(sink, ctx, "stim_format_version",    h->stim_format_version);
  plog_emit_kv(sink, ctx, "n_stim_items",           h->n_stim_items);
  plog_emit_kv(sink, ctx, "eod_hv_len",             h->eod_hv_len);
  plog_emit_kv(sink, ctx, "eod_net_integral_x1000", h->eod_net_integral_x1000);
  plog_emit_kv(sink, ctx, "anchor_period_samp",     h->anchor_period_samp);
  plog_emit_kv(sink, ctx, "ring_size",              h->ring_size);
  {
    char line[96];
    PlogBuf b;
    plog_buf_init(&b, line, sizeof(line));
    plog_put_str(&b, "#build=");
    plog_put_str(&b, h->build ? h->build : "?");
    plog_put_ch(&b, '\n');
    size_t n = plog_buf_finish(&b);
    if (n < sizeof(line)) sink(ctx, line, n);
  }
}

// The column line, emitted twice on purpose: once commented (so a naive "strip all #" reader
// still sees a clean CSV) and once bare (so pandas.read_csv(comment='#') finds a real header).
static inline void plog_emit_columns(PlogSink sink, void* ctx) {
  sink(ctx, "#" PLOG_COLUMNS "\n", sizeof("#" PLOG_COLUMNS "\n") - 1);
  sink(ctx, PLOG_COLUMNS "\n", sizeof(PLOG_COLUMNS "\n") - 1);
}

// ===== File naming / index choice (pure) ==============================================
// NEVER OVERWRITE. Files are indexed, not dated, precisely BECAUSE the RTC may reset: with a
// dead coin cell every boot would produce the same date-based name and collide. The RTC time
// is recorded INSIDE the file instead (the header block and the ANCHOR rows).
//
// Names are 8.3-safe ("PULS0000.CSV" is exactly 8+3) so they survive any FAT tooling, and the
// files live in a subdirectory because a FAT32 ROOT directory is capped at 512 entries.
#define PLOG_DIR        "/LOGS"
#define PLOG_NAME_BYTES 13u     // "PULS0000.CSV" + NUL
#define PLOG_INDEX_MAX  9999

// Write "PULSnnnn.CSV" for 0 <= idx <= PLOG_INDEX_MAX into out[PLOG_NAME_BYTES].
static inline void plog_name_for_index(int idx, char* out) {
  if (idx < 0) idx = 0;
  if (idx > PLOG_INDEX_MAX) idx = PLOG_INDEX_MAX;
  out[0] = 'P'; out[1] = 'U'; out[2] = 'L'; out[3] = 'S';
  out[4] = (char)('0' + (idx / 1000) % 10);
  out[5] = (char)('0' + (idx / 100) % 10);
  out[6] = (char)('0' + (idx / 10) % 10);
  out[7] = (char)('0' + idx % 10);
  out[8] = '.'; out[9] = 'C'; out[10] = 'S'; out[11] = 'V'; out[12] = '\0';
}

static inline char plog_upper(char c) { return (c >= 'a' && c <= 'z') ? (char)(c - 32) : c; }

// Parse "PULSnnnn.CSV" (either case) back to its index; -1 if `nm` is not a log file name.
// Used by BOTH the directory scan and the tests, so naming and parsing cannot drift apart.
static inline int plog_index_from_name(const char* nm) {
  if (nm == 0) return -1;
  size_t L = 0;
  while (nm[L]) L++;
  if (L != 12) return -1;
  if (plog_upper(nm[0]) != 'P' || plog_upper(nm[1]) != 'U'
      || plog_upper(nm[2]) != 'L' || plog_upper(nm[3]) != 'S') return -1;
  if (nm[8] != '.' || plog_upper(nm[9]) != 'C'
      || plog_upper(nm[10]) != 'S' || plog_upper(nm[11]) != 'V') return -1;
  int idx = 0;
  for (int i = 4; i < 8; i++) {
    if (nm[i] < '0' || nm[i] > '9') return -1;
    idx = idx * 10 + (nm[i] - '0');
  }
  return idx;
}

// The next index to open, given the HIGHEST index already on the card (-1 == none).
// Highest+1, NOT lowest-free: reusing a gap would break the "index orders the sessions"
// property. Returns -1 when the name space is exhausted, which the caller must treat as a
// LOGGING FAILURE (the device then refuses to stimulate) rather than wrapping — wrapping
// would overwrite, and never-overwrite is the whole point.
static inline int plog_next_index(int highest) {
  if (highest < 0) return 0;
  if (highest >= PLOG_INDEX_MAX) return -1;
  return highest + 1;
}

// ===== Anchor cadence =================================================================
// One ANCHOR row every PULSELOG_ANCHOR_S seconds, unconditionally. It does three jobs:
// it pairs a tick with an RTC reading so absolute time can be interpolated at full sample
// resolution; it PROVES THE DEVICE WAS ALIVE during a quiet stretch (without it, "idle" and
// "died" look identical after a power-cut truncation); and it gives the clock-drift fit
// regular tie points. 10 s costs ~2900 rows over an 8 h session — nothing.
#ifndef PULSELOG_ANCHOR_S
#define PULSELOG_ANCHOR_S 10u
#endif
#define PULSELOG_ANCHOR_SAMP ((uint32_t)PULSELOG_ANCHOR_S * (uint32_t)STIM_SAMPLE_RATE_HZ)

// ===== Arduino glue (SD / File; not host-tested) ======================================
#ifdef ARDUINO
#include <Arduino.h>
#include <SD.h>

// Sized for LATENCY, not bandwidth. Throughput is a non-issue (20 Hz localization is ~1 kB/s;
// the worst burst, a volley at ~400 Hz, is ~20 kB/s). The real hazard is an SD block-erase
// stalling loop() for 100 ms+, so the ring must absorb a stall AT THE BURST RATE:
// 400 Hz x 250 ms == 100 records, and 512 is the next comfortable power of two (~16 kB of the
// Teensy 4.1's 1 MB).
#ifndef PULSELOG_RING_SIZE
#define PULSELOG_RING_SIZE 512u
#endif
// flush() bounds how much is lost to a power cut. At 20 Hz, 1 s bounds it to ~20 pulses.
#ifndef PULSELOG_FLUSH_MS
#define PULSELOG_FLUSH_MS 1000u
#endif
// Cap the records written per loop() pass so one drain burst cannot monopolise a pass and
// stretch the ~200 Hz RC decode cadence (RC_DECODE_PERIOD_MS). 64 rows per pass is far more
// throughput than any burst needs.
#ifndef PULSELOG_DRAIN_MAX
#define PULSELOG_DRAIN_MAX 64u
#endif
// How often loop() retries the card after a logging failure, and the ceiling the interval backs
// off to. Backing off spares a dying card and — more importantly — bounds how fast a card that
// lets a file be created but not written can consume the 9999-index name space.
#ifndef PULSELOG_RETRY_MS
#define PULSELOG_RETRY_MS 2000u
#endif
#ifndef PULSELOG_RETRY_MAX_MS
#define PULSELOG_RETRY_MAX_MS 30000u
#endif
// Rows are batched into one write() per drain to keep SD transactions large.
#ifndef PULSELOG_WRITE_BUF
#define PULSELOG_WRITE_BUF 1024u
#endif

struct PulseLog;
// Hook that lets the CONTROL SURFACE (L3) append its own "#key=value" provenance lines to the
// header. It is stored, not just called once, so a file opened by a mid-session recovery
// carries exactly the same header as the boot file.
typedef void (*PlogHeaderHook)(PulseLog*);

struct PulseLog {
  PlogRing<PULSELOG_RING_SIZE> ring;
  File     f;
  PlogHeaderHook hook;
  bool     ok;             // logging healthy: file open and writes succeeding
  int      index;          // index of the currently open file (-1 == none)
  int      broken_index;   // index of a file cut short by a failure (-1 == none)
  uint32_t seq;            // monotonic row counter WITHIN the current file
  uint32_t last_flush_ms;
  uint32_t last_retry_ms;
  uint32_t retry_ms;       // current retry interval; doubles on failure, resets on success
  uint32_t boot_rtc;       // RTC reading captured when the first file was opened
};

static inline bool plog_healthy(const PulseLog* L) { return L->ok; }

// Raw write with error detection. Teensy's File is a Print, so a failed/short write shows up
// both in the return length and in getWriteError().
static inline bool plog_raw_write(PulseLog* L, const char* s, size_t n) {
  if (!L->ok) return false;
  size_t w = L->f.write((const uint8_t*)s, n);
  if (w != n || L->f.getWriteError()) {
    L->f.clearWriteError();
    L->ok = false;
    L->broken_index = L->index;
    L->f.close();
    L->index = -1;
    return false;
  }
  return true;
}

// Sink adaptor: the pure header emitter writes through this into the open file.
static inline void plog_sink_file(void* ctx, const char* s, size_t n) {
  plog_raw_write((PulseLog*)ctx, s, n);
}

// Append one "#key=value" line. Public so the L3 header hook can add surface-specific
// provenance (marker code, volley item range, ...) using the same generic syntax.
static inline void plog_header_kv(PulseLog* L, const char* key, uint64_t val) {
  plog_emit_kv(plog_sink_file, L, key, val);
}

// Push a record from the ISR. Thin wrapper so the sketch never touches the ring directly.
static inline bool plog_push(PulseLog* L, const PlogRec* r) { return L->ring.push(r); }

// Highest existing log index on the card, or -1 if the directory is empty/absent.
// Returns -2 if the directory itself cannot be opened after a creation attempt (card fault).
static inline int plog_scan_highest_index() {
  if (!SD.exists(PLOG_DIR) && !SD.mkdir(PLOG_DIR)) return -2;
  File d = SD.open(PLOG_DIR);
  if (!d) return -2;
  int highest = -1;
  for (File e = d.openNextFile(); e; e = d.openNextFile()) {
    if (!e.isDirectory()) {
      int i = plog_index_from_name(e.name());
      if (i > highest) highest = i;
    }
    e.close();
  }
  d.close();
  return highest;
}

// Open the next free index and write the header block. Sets L->ok.
static inline bool plog_open_new(PulseLog* L) {
  int highest = plog_scan_highest_index();
  if (highest == -2) { L->ok = false; return false; }
  int idx = plog_next_index(highest);
  if (idx < 0) { L->ok = false; return false; }   // name space exhausted -> logging failure

  char name[PLOG_NAME_BYTES];
  plog_name_for_index(idx, name);
  char path[sizeof(PLOG_DIR) + PLOG_NAME_BYTES];
  size_t k = 0;
  for (const char* s = PLOG_DIR "/"; *s; s++) path[k++] = *s;
  for (size_t i = 0; i < PLOG_NAME_BYTES; i++) path[k + i] = name[i];

  // FILE_WRITE appends; the index chooser guarantees the name is unused, so this creates.
  L->f = SD.open(path, FILE_WRITE);
  if (!L->f) { L->ok = false; return false; }
  L->ok = true;
  L->index = idx;
  L->seq = 0;

  uint32_t rtc = (uint32_t)Teensy3Clock.get();
  if (!L->boot_rtc) L->boot_rtc = rtc;

  PlogHeader h;
  h.format_version         = PULSELOG_FORMAT_VERSION;
  h.file_index             = (uint32_t)idx;
  h.sample_rate_hz         = (uint32_t)STIM_SAMPLE_RATE_HZ;
  h.rtc_unix               = rtc;
  h.boot_rtc_unix          = L->boot_rtc;
  h.stim_format_version    = (uint32_t)STIM_FORMAT_VERSION;
  h.n_stim_items           = (uint32_t)N_STIM_ITEMS;
  h.eod_hv_len             = (uint32_t)EOD_HV_LEN;
  h.eod_net_integral_x1000 = (uint32_t)EOD_NET_INTEGRAL_X1000;
  h.anchor_period_samp     = PULSELOG_ANCHOR_SAMP;
  h.ring_size              = PULSELOG_RING_SIZE;
  h.build                  = __DATE__ " " __TIME__;
  plog_emit_header(&h, plog_sink_file, L);
  if (L->hook) L->hook(L);          // L3 provenance (marker code, volley item range, ...)
  plog_emit_columns(plog_sink_file, L);
  if (L->ok) L->f.flush();
  return L->ok;
}

// Mount the card and open the first file. Call ONCE from setup(), BEFORE the sample clock
// starts. Opening at boot (rather than lazily on the first event) is deliberate: it proves
// the card is WRITABLE — which a bare SD.begin() mount probe does not — and moves the first
// write failure to boot, where a card swap is cheap, instead of to the first trial.
static inline bool plog_begin(PulseLog* L, PlogHeaderHook hook) {
  L->ring.reset();
  L->hook = hook;
  L->ok = false;
  L->index = -1;
  L->broken_index = -1;
  L->seq = 0;
  L->last_flush_ms = 0;
  L->last_retry_ms = 0;
  L->retry_ms = PULSELOG_RETRY_MS;
  L->boot_rtc = 0;
  if (!SD.begin(BUILTIN_SDCARD)) return false;
  return plog_open_new(L);
}

// Write one record. Truncation is impossible with PLOG_ROW_MAX, but check anyway: a partial
// row would corrupt the column structure for everything after it.
static inline void plog_write_rec(PulseLog* L, const PlogRec* r, char* wbuf, size_t* wlen) {
  char row[PLOG_ROW_MAX];
  size_t n = plog_format_row(r, L->seq, row, sizeof(row));
  if (n >= sizeof(row)) return;   // impossible by construction; never emit a partial row
  if (*wlen + n > PULSELOG_WRITE_BUF) {
    if (!plog_raw_write(L, wbuf, *wlen)) { *wlen = 0; return; }
    *wlen = 0;
  }
  for (size_t i = 0; i < n; i++) wbuf[*wlen + i] = row[i];
  *wlen += n;
  L->seq++;
}

// Drain the ring to the card and flush on the interval. Call EVERY loop() pass. This is the
// only place file I/O happens.
static inline void plog_service(PulseLog* L, uint32_t now_ms) {
  if (!L->ok) return;
  static char wbuf[PULSELOG_WRITE_BUF];
  size_t wlen = 0;
  PlogRec r;
  for (uint32_t i = 0; i < PULSELOG_DRAIN_MAX && L->ring.pop(&r); i++) {
    plog_write_rec(L, &r, wbuf, &wlen);
    if (!L->ok) return;
  }
  if (wlen) plog_raw_write(L, wbuf, wlen);
  if (L->ok && (uint32_t)(now_ms - L->last_flush_ms) >= PULSELOG_FLUSH_MS) {
    L->last_flush_ms = now_ms;
    L->f.flush();
    if (L->f.getWriteError()) {
      L->f.clearWriteError();
      L->ok = false;
      L->broken_index = L->index;
      L->f.close();
      L->index = -1;
    }
  }
}

// Retry after a logging failure. Rate-limited. On success a NEW indexed file is opened — the
// interrupted one is never reopened, because there is no way to know how much of it reached
// the card — and a GAP row records the discontinuity explicitly. Call from loop() while
// !plog_healthy().
static inline bool plog_retry(PulseLog* L, uint32_t now_ms) {
  if (L->ok) return true;
  if (L->last_retry_ms && (uint32_t)(now_ms - L->last_retry_ms) < L->retry_ms) return false;
  L->last_retry_ms = now_ms ? now_ms : 1u;

  // Capture the debt BEFORE reopening. plog_open_new() sets L->index up front and can still
  // fail part-way through the header, at which point plog_raw_write() overwrites broken_index
  // with the NEW file's index — losing the discontinuity we still owe a GAP row for.
  int broken = L->broken_index;
  if (!SD.begin(BUILTIN_SDCARD) || !plog_open_new(L)) {
    L->broken_index = broken;   // still owed
    // Back off. Besides sparing a dying card, this bounds how fast a pathological card — one
    // that lets a file be CREATED but not written — can burn through the 9999 index space.
    if (L->retry_ms < PULSELOG_RETRY_MAX_MS) L->retry_ms *= 2u;
    return false;
  }
  L->retry_ms = PULSELOG_RETRY_MS;

  if (broken >= 0) {
    PlogRec g;
    // No tick: this row is written by loop(), which cannot read the ISR-owned counter. An
    // EMPTY tick says "not applicable" in the file's own convention; a literal 0 would read as
    // a real event at device time zero, and would collide with BOOT's legitimate tick 0.
    plog_rec_init(&g, (uint8_t)PLOG_GAP, PLOG_ABSENT_TICK);
    g.val = (uint32_t)broken;
    char wbuf[PLOG_ROW_MAX];
    size_t wlen = 0;
    plog_write_rec(L, &g, wbuf, &wlen);
    if (wlen) plog_raw_write(L, wbuf, wlen);
    // ONLY forget the discontinuity once the row is actually on the card. Clearing it
    // unconditionally would let a file that follows a data-losing gap certify itself as clean —
    // and the GAP row is the one integrity admission the whole recovery design rests on.
    // If the write failed, plog_raw_write has already re-pointed broken_index at the new file;
    // keep the EARLIER index instead, so the next successful recovery still reports the first
    // hole. (Only the earliest unreported file is named; the presence of a GAP row at all is
    // the signal that the log is not continuous.)
    L->broken_index = L->ok ? -1 : broken;
  }
  return L->ok;
}

#endif  // ARDUINO
