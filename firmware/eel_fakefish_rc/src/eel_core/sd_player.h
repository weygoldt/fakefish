// SD-card WAV streaming for the fakefish stimulator.
//
// Full-SD playback: every stimulus is a mono int16 @ 50 kHz WAV on the Teensy 4.1's
// built-in SD card, one directory per program button. A press lists the button's
// directory, picks a WAV at random, and streams it to out_write() at the 50 kHz sample
// clock. All stimulus *generation* is offline Python (src/fakefish/build_sd_card.py);
// this firmware just plays files.
//
// The delicate part is real-time: the 50 kHz ISR must never block on the card. So samples
// flow through a lock-free single-producer / single-consumer ring buffer — loop() refills
// it from SD (File::read), the ISR only pops. The pure logic (WAV header parse, ring
// buffer, gain/polarity apply, random-index pick) sits ABOVE the `#ifdef ARDUINO` line so
// it compiles and is unit-tested on a PC (firmware/eel_core/host_test/sd_player_selftest).
#pragma once
#include <stdint.h>
#include <stddef.h>

// ===== WAV header parse (pure) ========================================================
// We accept the canonical PCM WAV the Python exporter writes: RIFF/WAVE, a `fmt ` chunk
// (PCM, mono, 16-bit, 50 kHz) and a `data` chunk. Extra chunks (LIST/fact/...) are skipped
// by walking the chunk list, so a slightly non-minimal header still parses.
struct WavInfo {
  uint32_t sample_rate;
  uint16_t channels;
  uint16_t bits;
  uint32_t data_offset;  // byte offset of the data chunk's payload from file start
  uint32_t data_bytes;   // payload length in bytes
  bool     ok;           // true == parsed AND matches (PCM / mono / 16-bit / 50 kHz)
};

static inline uint16_t wav_rd_u16(const uint8_t* p) {
  return (uint16_t)(p[0] | (p[1] << 8));
}
static inline uint32_t wav_rd_u32(const uint8_t* p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static inline bool wav_id_eq(const uint8_t* p, const char* id) {
  return p[0] == (uint8_t)id[0] && p[1] == (uint8_t)id[1]
      && p[2] == (uint8_t)id[2] && p[3] == (uint8_t)id[3];
}

// Parse a header buffer (the first `n` bytes of the file; a few hundred bytes is plenty).
// Fills `out`. `out.ok` also asserts the required format (PCM mono 16-bit 50 kHz), so a
// caller can reject a stray/mismatched WAV loudly. Returns out.ok.
static inline bool wav_parse_header(const uint8_t* buf, size_t n, uint32_t expect_rate,
                                    WavInfo* out) {
  out->sample_rate = out->data_offset = out->data_bytes = 0;
  out->channels = out->bits = 0;
  out->ok = false;
  if (n < 12 || !wav_id_eq(buf, "RIFF") || !wav_id_eq(buf + 8, "WAVE"))
    return false;

  bool have_fmt = false, have_data = false;
  size_t off = 12;  // first chunk after "RIFF"<size>"WAVE"
  while (off + 8 <= n) {
    const uint8_t* ch = buf + off;
    uint32_t csize = wav_rd_u32(ch + 4);
    const uint8_t* body = ch + 8;
    if (wav_id_eq(ch, "fmt ") && off + 8 + 16 <= n) {
      uint16_t fmt = wav_rd_u16(body);
      out->channels = wav_rd_u16(body + 2);
      out->sample_rate = wav_rd_u32(body + 4);
      out->bits = wav_rd_u16(body + 14);
      have_fmt = (fmt == 1);  // PCM
    } else if (wav_id_eq(ch, "data")) {
      out->data_offset = (uint32_t)(off + 8);
      out->data_bytes = csize;
      have_data = true;
      break;  // data is the stream; stop once located
    }
    // chunks are word-aligned: an odd size carries a pad byte. Advance in 64-bit and break
    // on non-progress / past-buffer, so a corrupt csize cannot wrap size_t into an endless
    // walk (which would hang loop()); data beyond the header buffer just self-mutes.
    uint64_t next = (uint64_t)off + 8u + (uint64_t)csize + (csize & 1u);
    if (next <= off || next > n) break;
    off = (size_t)next;
  }
  out->ok = have_fmt && have_data && out->channels == 1 && out->bits == 16
            && out->sample_rate == expect_rate;
  return out->ok;
}

// ===== Gain + polarity (pure) =========================================================
// One streamed sample scaled by the master output gain and this playback's polarity, then
// clamped to +/-32767. Polarity is randomised per press: the eel EOD is net-DC/monophasic,
// so a fixed polarity would slowly pit the electrodes by electrolysis. The marker LUT sums
// to zero and the song is zero-mean, so flipping their sign is harmless.
static inline int16_t sdp_apply(int16_t s, float gain, int8_t polarity) {
  float v = (float)s * gain * (float)polarity;
  long r = (long)(v < 0 ? v - 0.5f : v + 0.5f);  // round half away from zero
  if (r > 32767) r = 32767;
  if (r < -32767) r = -32767;
  return (int16_t)r;
}

// ===== Lock-free SPSC ring buffer (pure) ==============================================
// Single producer (loop() via push) + single consumer (ISR via pop). Free-running 32-bit
// head/tail counters, masked on access; SIZE must be a power of two. On the Teensy 4.1
// (single core, 32-bit aligned word read/write is atomic, ISR preempts loop) volatile
// head/tail with each side touching only its own counter is safe without a lock.
template <uint32_t SIZE>
struct SdpRing {
  static_assert((SIZE & (SIZE - 1)) == 0, "SdpRing SIZE must be a power of two");
  int16_t           buf[SIZE];
  volatile uint32_t head;  // next write slot (producer)
  volatile uint32_t tail;  // next read slot (consumer)

  void reset() { head = 0; tail = 0; }
  uint32_t available() const { return head - tail; }        // readable samples
  uint32_t space() const { return SIZE - (head - tail); }   // writable slots
  bool empty() const { return head == tail; }

  // Producer: push one sample. Caller checks space() first (drops on full). The compiler
  // barrier publishes the data write BEFORE the head increment, so the preempting ISR can
  // never see head advance past a not-yet-written slot (a signal_fence-equivalent is enough
  // on one core; no DMB needed).
  bool push(int16_t v) {
    if (space() == 0) return false;
    buf[head & (SIZE - 1)] = v;
    asm volatile("" ::: "memory");   // release: data store before index publish
    head = head + 1;
    return true;
  }
  // Consumer: pop one sample into *out. Returns false when empty (underrun). The barrier
  // orders the empty-check (head load) before the data read.
  bool pop(int16_t* out) {
    if (head == tail) return false;
    asm volatile("" ::: "memory");   // acquire: index check before data read
    *out = buf[tail & (SIZE - 1)];
    tail = tail + 1;
    return true;
  }
};

// Pick a file index in [0, count) from a raw random draw (host-testable; the driver passes
// random(count)). Returns -1 for an empty directory (self-mute).
static inline int sdp_pick_index(int count, int draw) {
  if (count <= 0) return -1;
  int i = draw % count;
  return i < 0 ? i + count : i;
}

// True if `nm` is a playable WAV basename: ends in ".wav" (any case) AND does not begin with
// '.'. Rejecting dotfiles skips hidden entries and macOS AppleDouble '._<name>.wav' sidecars
// (created whenever a card is touched by a Mac) that would otherwise be counted as playable
// and picked -> a random ~half of presses would self-mute on a non-WAV blob. Pure; used by
// BOTH directory passes so the count and the open stay in exact lockstep. Host-tested.
static inline bool sdp_is_wav_name(const char* nm) {
  if (nm == 0 || nm[0] == '.') return false;
  size_t L = 0;
  while (nm[L]) L++;
  if (L < 4 || nm[L - 4] != '.') return false;
  char a = (char)(nm[L - 3] | 0x20), b = (char)(nm[L - 2] | 0x20), c = (char)(nm[L - 1] | 0x20);
  return a == 'w' && b == 'a' && c == 'v';
}

// ===== Arduino glue (SD / File; not host-tested) ======================================
#ifdef ARDUINO
#include <Arduino.h>
#include <SD.h>

// One int16 every 20 us == 100 kB/s. A 4096-sample ring is ~82 ms of headroom, refilled
// from SD in loop() in <1 ms bursts, so the ISR never waits on the card.
#ifndef SDP_RING_SIZE
#define SDP_RING_SIZE 4096u
#endif
#ifndef SDP_FILL_CHUNK
#define SDP_FILL_CHUNK 512u   // samples read from SD per prefetch burst
#endif

struct SdPlayer {
  SdpRing<SDP_RING_SIZE> ring;
  File     file;
  uint32_t data_remaining;   // data-chunk bytes not yet read into the ring
  int8_t   polarity;
  volatile bool source_eof;  // set by loop() when the file's data is fully read
  bool     card_ok;
};

// Open the SD card once at boot. Returns false if no card (the caller can flash an error).
static inline bool sdp_begin(SdPlayer* p) {
  p->card_ok = SD.begin(BUILTIN_SDCARD);
  p->data_remaining = 0;
  p->source_eof = true;
  return p->card_ok;
}

// Count the playable *.wav files directly under `dir`. Returns -1 if `dir` cannot be opened
// (card gone / directory missing) so the caller can distinguish that from an empty directory.
static inline int sdp_count_wavs(const char* dir) {
  File d = SD.open(dir);
  if (!d) return -1;
  int n = 0;
  for (File f = d.openNextFile(); f; f = d.openNextFile()) {
    if (!f.isDirectory() && sdp_is_wav_name(f.name())) n++;
    f.close();
  }
  d.close();
  return n;
}

// Open the `idx`-th playable *.wav under `dir` into `out`. Uses the SAME predicate as
// sdp_count_wavs so the count pass and this open pass enumerate identically. Returns true on
// success (the chosen file is left OPEN in *out; all others are closed).
static inline bool sdp_open_wav_index(const char* dir, int idx, File* out) {
  File d = SD.open(dir);
  if (!d) return false;
  int seen = 0;
  bool ok = false;
  for (File f = d.openNextFile(); f; f = d.openNextFile()) {
    if (!f.isDirectory() && sdp_is_wav_name(f.name())) {
      if (seen == idx) { *out = f; ok = true; break; }   // keep this one open
      seen++;
    }
    f.close();
  }
  d.close();
  return ok;
}

// Read the header of an already-open file and position it at the data chunk. The 512-byte
// window covers any realistic PCM header (build_sd_card.py / scipy write a minimal 44-byte
// one); a `data` chunk pushed beyond 512 B by some other writer just self-mutes (wav_parse
// returns false) rather than misbehaving.
static inline bool sdp_read_header(File* f, uint32_t expect_rate, WavInfo* info) {
  uint8_t hdr[512];
  f->seek(0);
  int got = f->read(hdr, sizeof(hdr));
  if (got < 12) return false;
  if (!wav_parse_header(hdr, (size_t)got, expect_rate, info)) return false;
  f->seek(info->data_offset);
  return true;
}

// Refill the ring from the file (call every loop() while playing). Reads whole int16s in
// bursts up to SDP_FILL_CHUNK; sets source_eof when the data chunk is exhausted.
static inline void sdp_prefetch(SdPlayer* p) {
  if (p->source_eof) return;
  int16_t tmp[SDP_FILL_CHUNK];
  while (p->ring.space() >= SDP_FILL_CHUNK && p->data_remaining >= 2) {
    uint32_t want = p->ring.space();
    if (want > SDP_FILL_CHUNK) want = SDP_FILL_CHUNK;
    uint32_t avail = p->data_remaining / 2;   // whole samples left
    if (want > avail) want = avail;
    int nbytes = p->file.read(tmp, want * 2);
    if (nbytes <= 0) {   // short/failed read with data still expected == a runtime SD error:
      p->data_remaining = 0;   // end this playback and flag the card so loop() re-mounts it
      p->card_ok = false;
      break;
    }
    int nsamp = nbytes / 2;
    for (int i = 0; i < nsamp; i++) p->ring.push(tmp[i]);
    p->data_remaining -= (uint32_t)nbytes;
  }
  if (p->data_remaining < 2) p->source_eof = true;
}

// Start streaming a random WAV from `dir`. Returns false (self-mute) if the directory has
// no WAVs or the chosen file is malformed. `draw` is random(count); `polarity` is +/-1.
static inline bool sdp_start(SdPlayer* p, const char* dir, int draw, int8_t polarity,
                             uint32_t expect_rate) {
  if (!p->card_ok) return false;
  int count = sdp_count_wavs(dir);
  int idx = sdp_pick_index(count, draw);
  if (idx < 0) return false;
  if (p->file) p->file.close();
  if (!sdp_open_wav_index(dir, idx, &p->file)) return false;
  WavInfo info;
  if (!sdp_read_header(&p->file, expect_rate, &info)) { p->file.close(); return false; }
  p->ring.reset();
  p->data_remaining = info.data_bytes;
  p->polarity = polarity;
  p->source_eof = false;
  sdp_prefetch(p);           // prime the ring before the clock starts
  return true;
}

// True once the file is fully read AND the ring has drained — the driver then stops.
static inline bool sdp_finished(const SdPlayer* p) {
  return p->source_eof && p->ring.empty();
}

#endif  // ARDUINO
