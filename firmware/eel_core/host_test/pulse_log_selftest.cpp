// Host self-test for the pure pulse-log logic (no Arduino / no SD card).
// Compile + run on a PC:  g++ -std=c++17 -I.. pulse_log_selftest.cpp -o t && ./t
//
// TWO MODES:
//   (no args)  run the assertions and print exactly "OK"   — the gate's group-2 check.
//   --emit     print a complete sample log to stdout        — the golden file generator.
//
// The --emit output is committed as tests/data/pulse_log_golden.csv and is the SINGLE SOURCE
// OF TRUTH shared by both sides of the format: check.sh asserts this binary still reproduces
// it byte-for-byte, and tests/test_pulse_log.py parses that same committed file with the
// Python reader. So the C writer and the Python reader are pinned to one artifact and cannot
// drift apart silently — changing either without the other fails the gate.
//
// Everything --emit prints goes through the real firmware code path (plog_emit_header /
// plog_emit_columns / plog_format_row); nothing here re-implements the format.
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

#include "../pulse_log.h"

static int g_fail = 0;
#define CHECK(cond, msg) do { if (!(cond)) { printf("FAIL: %s\n", msg); g_fail++; } } while (0)

static void sink_str(void* ctx, const char* s, size_t n) {
  ((std::string*)ctx)->append(s, n);
}

// Format one record into a std::string using the real row formatter.
static std::string row_of(const PlogRec& r, uint32_t seq) {
  char buf[PLOG_ROW_MAX];
  size_t n = plog_format_row(&r, seq, buf, sizeof(buf));
  CHECK(n < sizeof(buf), "row fits in PLOG_ROW_MAX");
  return std::string(buf, n);
}

// ===== file naming / index choice ======================================================
static void test_name_index() {
  char nm[PLOG_NAME_BYTES];
  plog_name_for_index(0, nm);
  CHECK(std::string(nm) == "PULS0000.CSV", "index 0 name");
  plog_name_for_index(7, nm);
  CHECK(std::string(nm) == "PULS0007.CSV", "index 7 name");
  plog_name_for_index(1234, nm);
  CHECK(std::string(nm) == "PULS1234.CSV", "index 1234 name");
  plog_name_for_index(9999, nm);
  CHECK(std::string(nm) == "PULS9999.CSV", "index 9999 name");
  // 8.3-safe: exactly 8 chars of stem + '.' + 3 of extension.
  CHECK(strlen(nm) == 12, "name is 8.3 (12 chars)");

  // round-trip every index through name -> parse
  for (int i : {0, 1, 9, 10, 99, 100, 999, 1000, 4321, 9999}) {
    plog_name_for_index(i, nm);
    CHECK(plog_index_from_name(nm) == i, "name/index round trip");
  }

  // the SD library may hand back either case
  CHECK(plog_index_from_name("puls0042.csv") == 42, "lowercase name parses");
  CHECK(plog_index_from_name("PuLs0042.CsV") == 42, "mixed case name parses");

  // non-log names must be rejected, not misparsed into an index
  CHECK(plog_index_from_name("PULS0042.TXT") == -1, "wrong extension rejected");
  CHECK(plog_index_from_name("DATA0042.CSV") == -1, "wrong stem rejected");
  CHECK(plog_index_from_name("PULS004.CSV") == -1, "too short rejected");
  CHECK(plog_index_from_name("PULS00042.CSV") == -1, "too long rejected");
  CHECK(plog_index_from_name("PULS00A2.CSV") == -1, "non-digit rejected");
  CHECK(plog_index_from_name("PULS0042_CSV") == -1, "missing dot rejected");
  CHECK(plog_index_from_name("") == -1, "empty rejected");
  CHECK(plog_index_from_name(nullptr) == -1, "null rejected");
}

static void test_next_index() {
  CHECK(plog_next_index(-1) == 0, "empty dir -> index 0");
  CHECK(plog_next_index(0) == 1, "0 -> 1");
  CHECK(plog_next_index(5) == 6, "5 -> 6");
  // GAPS ARE NOT REUSED: highest+1, never lowest-free, so the index keeps ordering sessions.
  CHECK(plog_next_index(9) == 10, "gaps below the highest are not reused");
  CHECK(plog_next_index(9998) == 9999, "9998 -> 9999");
  // Exhaustion returns -1 == a LOGGING FAILURE. It must never wrap, because wrapping would
  // overwrite an existing session and never-overwrite is the whole point.
  CHECK(plog_next_index(9999) == -1, "9999 exhausted -> -1, never wraps to 0");
}

// Mimic a directory scan: the highest index wins regardless of enumeration order, and
// non-log entries are ignored.
static void test_scan_semantics() {
  const char* entries[] = {"PULS0003.CSV", "README.TXT", "PULS0001.CSV",
                           "PULS0007.CSV", "._PULS0009.CSV", "PULS0004.CSV"};
  int highest = -1;
  for (const char* e : entries) {
    int i = plog_index_from_name(e);
    if (i > highest) highest = i;
  }
  CHECK(highest == 7, "scan finds the highest index out of order");
  CHECK(plog_next_index(highest) == 8, "next index after a gapped scan");

  int empty_highest = -1;
  CHECK(plog_next_index(empty_highest) == 0, "empty directory starts at 0");
}

// ===== milli-unit conversion ===========================================================
static void test_milli() {
  CHECK(plog_milli(0.0f) == 0, "0 -> 0");
  CHECK(plog_milli(0.5f) == 500, "0.5 -> 500");
  CHECK(plog_milli(1.0f) == 1000, "1.0 -> 1000");
  CHECK(plog_milli(0.45f) == 450, "0.45 -> 450");
  CHECK(plog_milli(0.8f) == 800, "0.8 -> 800");
  CHECK(plog_milli(0.0004f) == 0, "rounds to 0");
  CHECK(plog_milli(0.0006f) == 1, "rounds to 1");
  CHECK(plog_milli(-1.0f) == 0, "negative clamps to 0");
  // Must never produce ABSENT_U16, or a real value would render as an empty column.
  CHECK(plog_milli(1e9f) == 65534, "huge clamps below the ABSENT sentinel");
  CHECK(plog_milli(1e9f) != PLOG_ABSENT_U16, "clamp never collides with ABSENT_U16");
  float nan = 0.0f / 0.0f;
  CHECK(plog_milli(nan) == 0, "NaN -> 0");
}

// ===== 64-bit tick =====================================================================
static void test_tick() {
  PlogTick t;
  plog_tick_reset(&t);
  CHECK(plog_tick_value(&t) == 0, "tick starts at 0");
  for (int i = 0; i < 5; i++) plog_tick_advance(&t);
  CHECK(plog_tick_value(&t) == 5, "tick counts");

  // The whole point of the 64-bit counter: g_tick (uint32) wraps after 2^32/50000 s ~ 23.9 h.
  t.lo = 0xFFFFFFFEu;
  t.hi = 0;
  plog_tick_advance(&t);
  CHECK(plog_tick_value(&t) == 0xFFFFFFFFull, "just below the 32-bit wrap");
  plog_tick_advance(&t);
  CHECK(plog_tick_value(&t) == 0x100000000ull, "carries past the 32-bit wrap");
  plog_tick_advance(&t);
  CHECK(plog_tick_value(&t) == 0x100000001ull, "keeps counting past the wrap");

  t.lo = 0xFFFFFFFFu;
  t.hi = 3;
  plog_tick_advance(&t);
  CHECK(plog_tick_value(&t) == 0x400000000ull, "carry from a non-zero high word");
}

// ===== the SPSC ring ===================================================================
static PlogRec mk(uint8_t ev, uint64_t tick) {
  PlogRec r;
  plog_rec_init(&r, ev, tick);
  return r;
}

static void test_ring_basic() {
  PlogRing<4> ring;
  ring.reset();
  CHECK(ring.empty(), "starts empty");
  CHECK(ring.space() == 4, "starts with full space");
  CHECK(ring.available() == 0, "nothing available");

  PlogRec r = mk(PLOG_LOC, 100);
  CHECK(ring.push(&r), "push into empty ring");
  CHECK(ring.available() == 1, "one available");
  CHECK(ring.space() == 3, "one slot consumed");

  PlogRec out;
  CHECK(ring.pop(&out), "pop returns a record");
  CHECK(out.tick == 100 && out.ev == PLOG_LOC, "popped record round-trips");
  CHECK(ring.empty(), "empty after draining");
  CHECK(!ring.pop(&out), "pop on empty returns false");
}

// Free-running head/tail counters must keep working past many wraps of the ring.
static void test_ring_wrap() {
  PlogRing<4> ring;
  ring.reset();
  PlogRec out;
  for (uint64_t i = 0; i < 100; i++) {
    PlogRec r = mk(PLOG_LOC, i);
    CHECK(ring.push(&r), "push in a drain-as-you-go loop");
    CHECK(ring.pop(&out), "pop in a drain-as-you-go loop");
    CHECK(out.tick == i, "FIFO order preserved across wraps");
  }
  CHECK(ring.empty(), "ring empty after the wrap loop");
  CHECK(ring.total_drops == 0, "no drops when drained as we go");
}

// OVERFLOW IS RECORDED, NEVER SILENT — the integrity property the whole feature rests on.
static void test_ring_overflow() {
  PlogRing<4> ring;
  ring.reset();

  for (uint64_t i = 0; i < 4; i++) {
    PlogRec r = mk(PLOG_LOC, i);
    CHECK(ring.push(&r), "fill the ring to capacity");
  }
  CHECK(ring.space() == 0, "ring is full");

  // Three pushes are lost while the consumer is stalled.
  for (uint64_t i = 4; i < 7; i++) {
    PlogRec r = mk(PLOG_LOC, i);
    CHECK(!ring.push(&r), "push on a full ring reports the drop");
  }
  CHECK(ring.total_drops == 3, "three records counted as dropped");

  // Drain everything the consumer can see: the 4 that made it in.
  PlogRec out;
  for (uint64_t i = 0; i < 4; i++) {
    CHECK(ring.pop(&out), "drain the backlog");
    CHECK(out.tick == i, "backlog is in order");
  }
  CHECK(ring.empty(), "backlog drained");

  // Space is back. The next push must emit a DROP row FIRST, carrying the count and the tick
  // of the record that follows it ("3 records were lost immediately before tick 7").
  PlogRec r = mk(PLOG_LOC, 7);
  CHECK(ring.push(&r), "push succeeds once space returns");
  CHECK(ring.available() == 2, "DROP row plus the real record");

  CHECK(ring.pop(&out), "pop the DROP row");
  CHECK(out.ev == PLOG_DROP, "DROP row comes first");
  CHECK(out.val == 3, "DROP row carries the exact lost count");
  CHECK(out.tick == 7, "DROP row is stamped with the resuming tick");
  CHECK(out.item == PLOG_ABSENT_ITEM, "DROP row has no item");

  CHECK(ring.pop(&out), "pop the record that followed");
  CHECK(out.ev == PLOG_LOC && out.tick == 7, "the real record survives the DROP insertion");

  // The counter is consumed: a later push must not re-emit a stale DROP.
  PlogRec r2 = mk(PLOG_LOC, 8);
  CHECK(ring.push(&r2), "push after the drop is reported");
  CHECK(ring.available() == 1, "no second DROP row is emitted");
  CHECK(ring.pop(&out) && out.ev == PLOG_LOC, "only the real record follows");
}

// A drop reported while the ring is still nearly full must not lose the DROP row itself.
static void test_ring_overflow_needs_two_slots() {
  PlogRing<4> ring;
  ring.reset();
  for (uint64_t i = 0; i < 4; i++) { PlogRec r = mk(PLOG_LOC, i); ring.push(&r); }
  PlogRec lost = mk(PLOG_LOC, 4);
  CHECK(!ring.push(&lost), "overflow recorded");

  // Free exactly ONE slot — not enough for DROP + record, so the push must be deferred and
  // counted rather than emitting a DROP row with no room for the record it annotates.
  PlogRec out;
  ring.pop(&out);
  PlogRec r = mk(PLOG_LOC, 5);
  CHECK(!ring.push(&r), "one free slot is not enough for DROP + record");
  CHECK(ring.pending_drops == 2, "the deferred record is itself counted as dropped");

  ring.pop(&out);
  PlogRec r2 = mk(PLOG_LOC, 6);
  CHECK(ring.push(&r2), "two free slots is enough");

  // The DROP row is inserted at the point in the STREAM where logging resumed, so it lands
  // AFTER the backlog that was still queued — which is exactly the order the file needs.
  CHECK(ring.pop(&out) && out.tick == 2, "backlog still drains in order first");
  CHECK(ring.pop(&out) && out.tick == 3, "backlog drains fully before the DROP");
  CHECK(ring.pop(&out) && out.ev == PLOG_DROP && out.val == 2, "DROP carries the full count");
  CHECK(ring.pop(&out) && out.ev == PLOG_LOC && out.tick == 6, "record follows the DROP");
  CHECK(ring.empty(), "nothing left after the drop is reported");
}

// ===== CSV rendering ===================================================================
static void test_format_pulse_rows() {
  // A localization pulse: no item, no pulse index, no trial.
  PlogRec loc;
  plog_rec_init(&loc, PLOG_LOC, 123456789);
  loc.pol = -1;
  loc.amp_m = 450;
  loc.master_m = 900;
  loc.rand_m = 1000;
  loc.tick_ipi = 10000;
  CHECK(row_of(loc, 42) == "42,123456789,LOC,,,,-1,450,900,1000,10000,,,,,,,,\n", "LOC row");

  // A volley pulse: item AND pulse index present.
  PlogRec vol;
  plog_rec_init(&vol, PLOG_VOLLEY, 500);
  vol.item = 13;
  vol.pulse = 27;
  vol.trial = 4;
  vol.pol = 1;
  vol.amp_m = 900;
  vol.master_m = 900;
  vol.rand_m = 1000;
  vol.tick_ipi = 10000;
  CHECK(row_of(vol, 7) == "7,500,VOLLEY,13,27,4,1,900,900,1000,10000,,,,,,,,\n", "VOLLEY row");

  // A marker pulse: pulse index present, NO item (it is built at runtime, not in the library).
  PlogRec mk_;
  plog_rec_init(&mk_, PLOG_MARKER, 300);
  mk_.pulse = 1;
  mk_.trial = 4;
  mk_.pol = 1;
  mk_.amp_m = 500;
  mk_.master_m = 900;
  mk_.rand_m = 1000;
  mk_.tick_ipi = 10000;
  CHECK(row_of(mk_, 6) == "6,300,MARKER,,1,4,1,500,900,1000,10000,,,,,,,,\n", "MARKER row");

  // v3: with the raw decode populated. This is the row shape a real device writes — every
  // other control column here is derived from these five, which is why they are worth the bytes.
  PlogRec raw;
  plog_rec_init(&raw, PLOG_LOC, 900);
  raw.pol = 1;
  raw.amp_m = 225;
  raw.master_m = 900;
  raw.rand_m = 1000;
  raw.tick_ipi = 10000;
  raw.ch_us[0] = 1115; raw.ch_us[1] = 1200; raw.ch_us[2] = 1010; raw.ch_us[3] = 1580;
  raw.zero_us = 905;
  CHECK(row_of(raw, 11) == "11,900,LOC,,,,1,225,900,1000,10000,,,,1115,1200,1010,1580,905\n",
        "LOC row with the raw decode populated");
  // ...and a channel that has never been seen stays EMPTY, not 0. A width of 0 us is a value in
  // this column's own units, so a 0 default would read as a zero-length pulse from the receiver
  // rather than as "this device has no receiver" — the same trap as item 0 below.
  raw.ch_us[2] = PLOG_ABSENT_U16;
  CHECK(row_of(raw, 11) == "11,900,LOC,,,,1,225,900,1000,10000,,,,1115,1200,,1580,905\n",
        "an unseen channel renders EMPTY, never 0 us");

  // Item 0 is a REAL volley and must render as "0", not as an empty column — the empty
  // column means "no item", and conflating the two would be the same class of bug as the
  // zero-sentinel this design exists to avoid.
  PlogRec zero;
  plog_rec_init(&zero, PLOG_VOLLEY, 1);
  zero.item = 0;
  zero.pulse = 0;
  CHECK(row_of(zero, 0) == "0,1,VOLLEY,0,0,,,,,,,,,,,,,,\n", "item 0 renders as 0, not empty");
}

// THE SENTINEL MUST NEVER REACH THE FILE. -1 is safe in C but in Python STIM_ITEMS[-1]
// silently returns the LAST item instead of raising, so a leaked -1 would misattribute
// every localization and marker pulse without anything appearing to fail.
static void test_absent_never_leaks() {
  const uint8_t evs[] = {PLOG_BOOT, PLOG_LOC, PLOG_MARKER, PLOG_VOLLEY, PLOG_BASE,
                         PLOG_TRIAL, PLOG_SHAM, PLOG_LOCON, PLOG_LOCOFF, PLOG_LINK,
                         PLOG_ANCHOR, PLOG_DROP, PLOG_GAP};
  for (uint8_t ev : evs) {
    PlogRec r;
    plog_rec_init(&r, ev, 1);
    std::string s = row_of(r, 0);
    CHECK(s.find("-1") == std::string::npos, "no -1 sentinel in any default row");
    CHECK(s.find("65535") == std::string::npos, "no u16 sentinel in any default row");
    CHECK(s.find("4294967295") == std::string::npos, "no u32 sentinel in any default row");
    // 14 columns == 13 commas, exactly, on every event type.
    size_t commas = 0;
    for (char c : s) if (c == ',') commas++;
    CHECK(commas == 18, "every row has exactly 18 commas");
    CHECK(s.back() == '\n', "every row ends with a newline");
  }
  // A fully-default row is all-empty after the fixed three columns.
  PlogRec r;
  plog_rec_init(&r, PLOG_LOCOFF, 99);
  CHECK(row_of(r, 3) == "3,99,LOCOFF,,,,,,,,,,,,,,,,\n", "default row is all-empty");
}

static void test_format_event_rows() {
  // A BLINDED trial: requested RANDOM, resolved to VOLLEY. Recording both is what tells a
  // genuinely blinded trial apart from a bench-forced one.
  PlogRec t;
  plog_rec_init(&t, PLOG_TRIAL, 1000);
  t.trial = 1;
  t.pol = 1;
  t.master_m = 900;
  t.rand_m = 1000;
  t.tick_ipi = 10000;
  t.req = PLOG_KIND_RANDOM;
  t.res = PLOG_KIND_VOLLEY;
  CHECK(row_of(t, 5) == "5,1000,TRIAL,,,1,1,,900,1000,10000,,R,V,,,,,\n", "blinded TRIAL row");

  // A bench trial forced from the panel: requested VOLLEY, resolved VOLLEY.
  t.req = PLOG_KIND_VOLLEY;
  CHECK(row_of(t, 5) == "5,1000,TRIAL,,,1,1,,900,1000,10000,,V,V,,,,,\n", "bench TRIAL row");

  PlogRec link;
  plog_rec_init(&link, PLOG_LINK, 77);
  link.val = 0;
  CHECK(row_of(link, 1) == "1,77,LINK,,,,,,,,,0,,,,,,,\n", "LINK down row");

  PlogRec anc;
  plog_rec_init(&anc, PLOG_ANCHOR, 500000);
  anc.val = 1755720000u;
  anc.master_m = 900;
  anc.rand_m = 1000;
  anc.tick_ipi = 10000;
  CHECK(row_of(anc, 2) == "2,500000,ANCHOR,,,,,,900,1000,10000,1755720000,,,,,,,\n", "ANCHOR row");

  // The 64-bit tick must render in full, not truncated to 32 bits.
  PlogRec big;
  plog_rec_init(&big, PLOG_LOC, 0x1FFFFFFFFull);
  CHECK(row_of(big, 0) == "0,8589934591,LOC,,,,,,,,,,,,,,,,\n", "64-bit tick renders in full");

  // A GAP row is written by loop(), which cannot read the ISR-owned tick. The column must be
  // EMPTY — a literal 0 would read as a real event at device time zero and would collide with
  // BOOT's legitimate tick 0.
  PlogRec gap;
  plog_rec_init(&gap, PLOG_GAP, PLOG_ABSENT_TICK);
  gap.val = 6;
  CHECK(row_of(gap, 9) == "9,,GAP,,,,,,,,,6,,,,,,,\n", "GAP row leaves the tick column empty");
  CHECK(row_of(gap, 9).find("18446744073709551615") == std::string::npos,
        "the tick sentinel never reaches the file");

  // ...but tick 0 is a REAL value for BOOT and must still render as 0.
  PlogRec boot;
  plog_rec_init(&boot, PLOG_BOOT, 0);
  boot.val = 7;
  CHECK(row_of(boot, 0) == "0,0,BOOT,,,,,,,,,7,,,,,,,\n", "BOOT keeps its legitimate tick 0");
}

// A truncated row must be DETECTED, never silently written: a partial row corrupts the
// column structure for everything after it.
static void test_truncation_detected() {
  PlogRec r;
  plog_rec_init(&r, PLOG_VOLLEY, 123456789012345ull);
  r.item = 24;
  r.pulse = 207;
  r.trial = 65535;
  r.pol = -1;
  r.amp_m = 65534;
  r.master_m = 65534;
  r.rand_m = 65534;
  r.tick_ipi = 4294967294u;
  // v3 columns left at their init value on this row ON PURPOSE: it proves an absent width
  // renders as an EMPTY column rather than a 0, which in this column's units is a real width.
  
  r.val = 4294967294u;
  r.req = PLOG_KIND_RANDOM;
  r.res = PLOG_KIND_SHAM;

  char big[PLOG_ROW_MAX];
  size_t full = plog_format_row(&r, 4294967294u, big, sizeof(big));
  CHECK(full < PLOG_ROW_MAX, "the widest possible row still fits PLOG_ROW_MAX");

  // Same record into a deliberately tiny buffer: the reported length is the FULL length, so
  // the caller can tell it was truncated, and nothing is written past the buffer.
  char small[16];
  memset(small, 0x7f, sizeof(small));
  size_t need = plog_format_row(&r, 4294967294u, small, sizeof(small));
  CHECK(need == full, "truncated call still reports the full length");
  CHECK(need >= sizeof(small), "truncation is detectable by need >= cap");
  CHECK(small[sizeof(small) - 1] == '\0', "truncated output stays NUL-terminated");
}

static void test_kv_and_header() {
  char line[96];
  size_t n = plog_format_kv("file_index", 7, line, sizeof(line));
  CHECK(std::string(line, n) == "#file_index=7\n", "kv line");
  n = plog_format_kv("rtc_unix", 1755720000u, line, sizeof(line));
  CHECK(std::string(line, n) == "#rtc_unix=1755720000\n", "kv line with a big value");

  PlogHeader h;
  h.format_version = PULSELOG_FORMAT_VERSION;
  h.file_index = 7;
  h.sample_rate_hz = 50000;
  h.rtc_unix = 1755720000u;
  h.boot_rtc_unix = 1755720000u;
  h.stim_format_version = 3;
  h.n_stim_items = 31;
  h.eod_hv_len = 131;
  h.eod_net_integral_x1000 = 41577;
  h.anchor_period_samp = 500000;
  h.ring_size = 512;
  h.build = "Jan  1 2026 00:00:00";

  std::string out;
  plog_emit_header(&h, sink_str, &out);
  CHECK(out.rfind("#fakefish-pulse-log\n", 0) == 0, "header starts with the magic line");
  CHECK(out.find("#format_version=4\n") != std::string::npos, "header carries the version");
  CHECK(out.find("#rtc_valid=1\n") != std::string::npos, "a plausible RTC is marked valid");
  CHECK(out.find("#eod_net_integral_x1000=41577\n") != std::string::npos, "library fingerprint");
  CHECK(out.find("#build=Jan  1 2026 00:00:00\n") != std::string::npos, "build stamp");

  // With no coin cell the RTC reads back as unset; the design is RTC-optional, so this must
  // be RECORDED as invalid rather than silently written as a real time.
  h.rtc_unix = 0;
  std::string out2;
  plog_emit_header(&h, sink_str, &out2);
  CHECK(out2.find("#rtc_valid=0\n") != std::string::npos, "an unset RTC is marked invalid");
  CHECK(plog_rtc_valid(0) == 0, "rtc 0 is invalid");
  CHECK(plog_rtc_valid(1599999999u) == 0, "pre-2020 rtc is invalid");
  CHECK(plog_rtc_valid(1600000000u) == 1, "2020-09-13 rtc is valid");

  std::string cols;
  plog_emit_columns(sink_str, &cols);
  CHECK(cols == "#" PLOG_COLUMNS "\n" PLOG_COLUMNS "\n", "columns emitted commented + bare");
  size_t commas = 0;
  for (char c : std::string(PLOG_COLUMNS)) if (c == ',') commas++;
  CHECK(commas == 18, "the column line declares 19 columns (v3 added five raw-decode ones)");
}

// ===== golden log emitter ==============================================================
// A complete, realistic miniature session exercising EVERY event type, printed through the
// real formatters. Values are fixed constants (never __DATE__ or a live clock) so the output
// is byte-reproducible and can be committed and diffed by the gate.
static void emit_golden() {
  std::string out;

  PlogHeader h;
  h.format_version = PULSELOG_FORMAT_VERSION;
  h.file_index = 7;
  h.sample_rate_hz = 50000;
  h.rtc_unix = 1755720000u;          // 2025-08-20 20:00:00 UTC
  h.boot_rtc_unix = 1755720000u;
  h.stim_format_version = 3;
  h.n_stim_items = 31;
  h.eod_hv_len = 131;
  h.eod_net_integral_x1000 = 41577;
  h.anchor_period_samp = 500000;
  h.ring_size = 512;
  h.build = "Jan  1 2026 00:00:00";
  plog_emit_header(&h, sink_str, &out);

  // L3 (control-surface) provenance, appended by the sketch's header hook. The reader treats
  // these generically, so a surface can add its own keys without a reader change.
  //
  // SCOPE NOTE: these values are ILLUSTRATIVE, not pinned. The real device emits them from
  // stim_levels.h and rc_control.h via log_header_hook(); this is an L2 test and cannot include
  // an L3 surface header without inverting the layering. What the golden pins is the row and
  // header FORMAT — the thing both sides parse — not the numeric values of surface constants.
  // Editing shared/stim_constants.json therefore does NOT make this file stale, and should not.
  plog_emit_kv(sink_str, &out, "surface", 0);
  plog_emit_kv(sink_str, &out, "marker_ipi_samp", 500);
  plog_emit_kv(sink_str, &out, "marker_pulses_volley", 2);
  plog_emit_kv(sink_str, &out, "marker_pulses_sham", 4);
  plog_emit_kv(sink_str, &out, "marker_amp_milli", 500);
  plog_emit_kv(sink_str, &out, "volley_item_first", 7);
  plog_emit_kv(sink_str, &out, "volley_item_count", 18);
  plog_emit_kv(sink_str, &out, "trial_w_volley_milli", 334);
  plog_emit_kv(sink_str, &out, "trial_w_baseline_milli", 333);
  plog_emit_kv(sink_str, &out, "trial_w_silence_milli", 333);
  plog_emit_kv(sink_str, &out, "trial_base_tick_milli_hz", 3150);
  plog_emit_kv(sink_str, &out, "trial_base_randomness_milli", 1000);
  plog_emit_kv(sink_str, &out, "loc_refractory_samp", 250);
  plog_emit_columns(sink_str, &out);

  uint32_t seq = 0;
  std::vector<PlogRec> rows;

  // Settings in force for this session: master 0.9, loc 0.225, randomness 1.0 (the measured
  // eel), ticking at 5 Hz (a 10000-sample median interval).
  const uint16_t MASTER = 900, LOC_AMP = 225, RANDOMNESS = 1000;
  const uint32_t TICK_IPI = 10000;
  // v3: the RAW decode behind those settings. A throttle sitting 210 us above its captured zero,
  // a centred trigger, and two pots — the near end of the chain every other column is derived
  // from. ZERO_US is deliberately unlike the calibrated minimum, because the whole point of the
  // column is that the two differ and by how much.
  const uint16_t CH_US[4] = { 1115u, 1200u, 1010u, 1580u };
  const uint16_t ZERO_US  = 905u;

  auto ctx = [&](PlogRec& r) {
    r.master_m = MASTER; r.rand_m = RANDOMNESS; r.tick_ipi = TICK_IPI;
    for (int i = 0; i < 4; i++) r.ch_us[i] = CH_US[i];
    r.zero_us = ZERO_US;
  };

  PlogRec r;
  plog_rec_init(&r, PLOG_BOOT, 0);       r.val = 7;                 ctx(r); rows.push_back(r);
  plog_rec_init(&r, PLOG_ANCHOR, 0);     r.val = 1755720000u;       ctx(r); rows.push_back(r);
  plog_rec_init(&r, PLOG_LINK, 12500);   r.val = 1;                 ctx(r); rows.push_back(r);

  // Localization starts; three jittered pulses (the tick deltas ARE the drawn IPIs).
  plog_rec_init(&r, PLOG_LOCON, 100000); ctx(r); rows.push_back(r);
  const uint64_t loc_ticks[] = {100000, 111873, 119204};
  for (uint64_t t : loc_ticks) {
    plog_rec_init(&r, PLOG_LOC, t);
    r.amp_m = LOC_AMP;
    r.pol = -1;
    ctx(r);
    rows.push_back(r);
  }

  // A BLINDED trial: the lever asked for RANDOM, the ISR drew VOLLEY. Localization stops,
  // the 2-pulse marker plays, then the volley (library item 13).
  plog_rec_init(&r, PLOG_LOCOFF, 130000); ctx(r); rows.push_back(r);
  plog_rec_init(&r, PLOG_TRIAL, 130000);
  r.trial = 1; r.pol = 1; r.req = PLOG_KIND_RANDOM; r.res = PLOG_KIND_VOLLEY;
  r.item = 13;   // v4: the drawn item, on the TRIAL row, for every arm
  ctx(r); rows.push_back(r);
  for (uint16_t i = 0; i < 2; i++) {     // marker: no item, it is built at runtime
    plog_rec_init(&r, PLOG_MARKER, 130000 + (uint64_t)i * 500);
    r.pulse = i; r.trial = 1; r.pol = 1; r.amp_m = 500;
    ctx(r); rows.push_back(r);
  }
  // Volley pulses. amp_m is the amplitude APPLIED to each pulse: the item's per-pulse envelope
  // (rel_amp, 255 -> 204 across a volley) on top of the master scale, so it decays down the
  // burst while master_m stays put. The two columns differing is the point.
  const uint64_t vol_ticks[] = {131000, 131160, 131295};
  const uint16_t vol_amps[]  = {900, 883, 866};
  for (uint16_t i = 0; i < 3; i++) {
    plog_rec_init(&r, PLOG_VOLLEY, vol_ticks[i]);
    r.item = 13; r.pulse = i; r.trial = 1; r.pol = 1; r.amp_m = vol_amps[i];
    ctx(r); rows.push_back(r);
  }

  // Localization resumes after the trial.
  plog_rec_init(&r, PLOG_LOCON, 140000); ctx(r); rows.push_back(r);
  plog_rec_init(&r, PLOG_LOC, 140000); r.amp_m = LOC_AMP; r.pol = -1; ctx(r); rows.push_back(r);

  // An SD stall lost 3 records; the gap is admitted, never silent.
  plog_rec_init(&r, PLOG_DROP, 152400); r.val = 3; rows.push_back(r);
  plog_rec_init(&r, PLOG_LOC, 152400); r.amp_m = LOC_AMP; r.pol = -1; ctx(r); rows.push_back(r);

  // A BLINDED trial that drew SILENCE: marker of 4 pulses, then NO water output at all — the
  // SHAM row is the only thing that makes the trial visible in the log.
  plog_rec_init(&r, PLOG_LOCOFF, 160000); ctx(r); rows.push_back(r);
  plog_rec_init(&r, PLOG_TRIAL, 160000);
  r.trial = 2; r.pol = -1; r.req = PLOG_KIND_RANDOM; r.res = PLOG_KIND_SHAM;
  r.item = 18;   // the item whose LENGTH this silent arm borrowed
  ctx(r); rows.push_back(r);
  for (uint16_t i = 0; i < 4; i++) {
    plog_rec_init(&r, PLOG_MARKER, 160000 + (uint64_t)i * 500);
    r.pulse = i; r.trial = 2; r.pol = -1; r.amp_m = 500;
    ctx(r); rows.push_back(r);
  }
  plog_rec_init(&r, PLOG_SHAM, 161500); r.trial = 2; r.item = 18; ctx(r); rows.push_back(r);

  // resume_after_playback() restarts the train once the silence arm's duration elapses, so every
  // LOCOFF in this file is matched by a LOCON — the grammar the firmware actually emits.
  plog_rec_init(&r, PLOG_LOCON, 163000); ctx(r); rows.push_back(r);
  plog_rec_init(&r, PLOG_LOC, 163000); r.amp_m = LOC_AMP; r.pol = 1; ctx(r); rows.push_back(r);

  // A trial whose REQUEST was an explicit VOLLEY rather than RANDOM. No input can produce this
  // any more — the panel's explicit buttons became one blinded TRIAL button on 2026-08-24 — but
  // v2/v3 files contain such rows, and the reader must keep reading them. Kept as legacy
  // coverage for the `req` column, alongside the MARKER rows above, which are legacy for the
  // same reason (the marker was deleted in 30e2dca).
  plog_rec_init(&r, PLOG_LOCOFF, 170000); ctx(r); rows.push_back(r);
  plog_rec_init(&r, PLOG_TRIAL, 170000);
  r.trial = 3; r.pol = 1; r.req = PLOG_KIND_VOLLEY; r.res = PLOG_KIND_VOLLEY;
  r.item = 20;
  ctx(r); rows.push_back(r);
  for (uint16_t i = 0; i < 2; i++) {
    plog_rec_init(&r, PLOG_MARKER, 170000 + (uint64_t)i * 500);
    r.pulse = i; r.trial = 3; r.pol = 1; r.amp_m = 500;
    ctx(r); rows.push_back(r);
  }
  const uint64_t v3_ticks[] = {171000, 171190};
  const uint16_t v3_amps[]  = {900, 878};
  for (uint16_t i = 0; i < 2; i++) {
    plog_rec_init(&r, PLOG_VOLLEY, v3_ticks[i]);
    r.item = 20; r.pulse = i; r.trial = 3; r.pol = 1; r.amp_m = v3_amps[i];
    ctx(r); rows.push_back(r);
  }

  // Localization resumes after the trial, so the LOCOFF below is properly paired.
  plog_rec_init(&r, PLOG_LOCON, 180000); ctx(r); rows.push_back(r);
  plog_rec_init(&r, PLOG_LOC, 180000); r.amp_m = LOC_AMP; r.pol = 1; ctx(r); rows.push_back(r);

  // A BLINDED trial that drew BASELINE (v4) — the third arm. A fish that is present and NOT
  // hunting: resting-rhythm pulses at LOCALIZATION amplitude, for exactly as long as the drawn
  // volley (item 22, ~0.5 s == 25000 samples) would have run.
  //
  // Three things here are the whole reason the arm exists, and all three are visible in the rows:
  //   * they are BASE rows, not LOC. Both sit at localization amplitude, so once the ambient
  //     train resumes beside them nothing else could separate the treatment from the fish
  //     ticking along. A shared row type would make the arm unrecoverable.
  //   * the FIRST pulse is at the TRIAL row's own tick (181100). The arm is ANCHORED at onset,
  //     exactly as a volley's first pulse is. Unanchored, at the measured 3.15 Hz tick, 40 % of
  //     these arms would hold no pulse at all and be indistinguishable from a SILENCE arm.
  //   * `item` is on the TRIAL row AND on every BASE row. That is the only record of how long a
  //     non-volley arm was meant to last — nothing was emitted at its end to measure.
  // The intervals are a heavy-tailed resting draw, not a metronome: 235 ms then 188 ms.
  plog_rec_init(&r, PLOG_LOCOFF, 181000); ctx(r); rows.push_back(r);
  plog_rec_init(&r, PLOG_TRIAL, 181100);
  r.trial = 4; r.pol = 1; r.req = PLOG_KIND_RANDOM; r.res = PLOG_KIND_BASELINE;
  r.item = 22;
  ctx(r); rows.push_back(r);
  const uint64_t base_ticks[] = {181100, 192850, 202250};
  for (uint16_t i = 0; i < 3; i++) {
    plog_rec_init(&r, PLOG_BASE, base_ticks[i]);
    r.item = 22; r.trial = 4; r.pol = 1; r.amp_m = LOC_AMP;
    ctx(r); rows.push_back(r);
  }
  // The arm ends at 181100 + 25000 == 206100; the train resumes just after.
  plog_rec_init(&r, PLOG_LOCON, 207000); ctx(r); rows.push_back(r);
  plog_rec_init(&r, PLOG_LOC, 207000); r.amp_m = LOC_AMP; r.pol = 1; ctx(r); rows.push_back(r);

  // RC link lost -> the throttle drops -> localization stops. The LINK row explains the gap.
  plog_rec_init(&r, PLOG_LINK, 260000);  r.val = 0; ctx(r); rows.push_back(r);
  plog_rec_init(&r, PLOG_LOCOFF, 260000); ctx(r); rows.push_back(r);

  // A later anchor with a DEAD coin cell. The Teensy RTC does not read 0 in that case — it
  // FREE-RUNS UPWARD from a small value after every power cycle — so the row carries a
  // plausible-looking but meaningless number. A reader that merely tested `val > 0` would fit
  // wall-clock time to it and report confident 1970 timestamps; the floor is PLOG_RTC_MIN_VALID
  // on both sides. Absolute time is unavailable from here; relative timing is unaffected.
  plog_rec_init(&r, PLOG_ANCHOR, 500000); r.val = 372; ctx(r); rows.push_back(r);

  // The card failed and was re-mounted into a new file; PULS0006.CSV was cut short. Written by
  // loop(), which cannot read the ISR-owned counter, so the tick column is EMPTY — not 0, which
  // would be indistinguishable from BOOT's legitimate tick 0.
  plog_rec_init(&r, PLOG_GAP, PLOG_ABSENT_TICK); r.val = 6; rows.push_back(r);

  for (const PlogRec& rec : rows) out += row_of(rec, seq++);
  fwrite(out.data(), 1, out.size(), stdout);
}

int main(int argc, char** argv) {
  if (argc > 1 && std::string(argv[1]) == "--emit") {
    emit_golden();
    return g_fail == 0 ? 0 : 1;
  }
  test_name_index();
  test_next_index();
  test_scan_semantics();
  test_milli();
  test_tick();
  test_ring_basic();
  test_ring_wrap();
  test_ring_overflow();
  test_ring_overflow_needs_two_slots();
  test_format_pulse_rows();
  test_absent_never_leaks();
  test_format_event_rows();
  test_truncation_detected();
  test_kv_and_header();
  if (g_fail == 0) { printf("OK\n"); return 0; }
  printf("%d CHECK(s) FAILED\n", g_fail);
  return 1;
}
