// Host self-test for the pure SD-player logic (no Arduino / no SD card).
// Compile + run on a PC:  g++ -std=c++17 -I.. sd_player_selftest.cpp -o t && ./t
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <vector>

#include "../sd_player.h"

static int g_fail = 0;
#define CHECK(cond, msg) do { if (!(cond)) { printf("FAIL: %s\n", msg); g_fail++; } } while (0)

static void put_u16(std::vector<uint8_t>& v, uint16_t x) { v.push_back(x & 0xff); v.push_back(x >> 8); }
static void put_u32(std::vector<uint8_t>& v, uint32_t x) {
  for (int i = 0; i < 4; i++) v.push_back((x >> (8 * i)) & 0xff);
}
static void put_id(std::vector<uint8_t>& v, const char* id) {
  for (int i = 0; i < 4; i++) v.push_back((uint8_t)id[i]);
}

// Build a WAV header: optional extra `LIST` chunk before `data`, then `data_bytes` payload.
static std::vector<uint8_t> make_wav(uint16_t channels, uint16_t bits, uint32_t rate,
                                     uint32_t data_bytes, bool with_list) {
  std::vector<uint8_t> v;
  put_id(v, "RIFF");
  put_u32(v, 0);  // riff size (unused by parser)
  put_id(v, "WAVE");
  put_id(v, "fmt ");
  put_u32(v, 16);
  put_u16(v, 1);            // PCM
  put_u16(v, channels);
  put_u32(v, rate);
  put_u32(v, rate * channels * bits / 8);  // byte rate
  put_u16(v, channels * bits / 8);         // block align
  put_u16(v, bits);
  if (with_list) {
    put_id(v, "LIST");
    put_u32(v, 6);
    for (int i = 0; i < 6; i++) v.push_back('x');  // even size, no pad
  }
  put_id(v, "data");
  put_u32(v, data_bytes);
  return v;
}

static void test_wav_parse() {
  // canonical mono/16/50k
  auto v = make_wav(1, 16, 50000, 2000, false);
  WavInfo info;
  CHECK(wav_parse_header(v.data(), v.size(), 50000, &info), "canonical parses");
  CHECK(info.ok, "canonical ok");
  CHECK(info.sample_rate == 50000 && info.channels == 1 && info.bits == 16, "canonical fmt");
  CHECK(info.data_offset == 44, "canonical data_offset == 44");
  CHECK(info.data_bytes == 2000, "canonical data_bytes");

  // extra LIST chunk before data -> data_offset shifts by 8 + 6
  auto v2 = make_wav(1, 16, 50000, 100, true);
  WavInfo i2;
  CHECK(wav_parse_header(v2.data(), v2.size(), 50000, &i2), "LIST-variant parses");
  CHECK(i2.data_offset == 44 + 14, "LIST variant data_offset");
  CHECK(i2.data_bytes == 100, "LIST variant data_bytes");

  // reject wrong rate / stereo / 8-bit / not-RIFF
  auto vr = make_wav(1, 16, 44100, 10, false);
  WavInfo ir;
  CHECK(!wav_parse_header(vr.data(), vr.size(), 50000, &ir), "wrong rate rejected");
  auto vs = make_wav(2, 16, 50000, 10, false);
  WavInfo is;
  CHECK(!wav_parse_header(vs.data(), vs.size(), 50000, &is), "stereo rejected");
  auto vb = make_wav(1, 8, 50000, 10, false);
  WavInfo ib;
  CHECK(!wav_parse_header(vb.data(), vb.size(), 50000, &ib), "8-bit rejected");
  uint8_t junk[12] = {0};
  WavInfo ij;
  CHECK(!wav_parse_header(junk, 12, 50000, &ij), "non-RIFF rejected");
  CHECK(!wav_parse_header(v.data(), 4, 50000, &ij), "too-short rejected");
}

static void test_wav_parse_robust() {
  // A corrupt csize must TERMINATE the chunk walk (not hang) and reject the file. If the
  // guard is missing, the 32-bit advance wraps to 0 and this call never returns -> the test
  // process hangs, which is itself the failure signal.
  std::vector<uint8_t> v;
  put_id(v, "RIFF"); put_u32(v, 0); put_id(v, "WAVE");
  put_id(v, "fmt "); put_u32(v, 16);
  put_u16(v, 1); put_u16(v, 1); put_u32(v, 50000); put_u32(v, 100000); put_u16(v, 2); put_u16(v, 16);
  put_id(v, "junk"); put_u32(v, 0xFFFFFFF8u);   // corrupt: 8 + csize + pad wraps uint32 -> 0
  WavInfo wi;
  CHECK(!wav_parse_header(v.data(), v.size(), 50000, &wi), "corrupt csize rejected + terminates");

  // A 'data' chunk pushed past the provided buffer must self-mute (false), not read OOB.
  std::vector<uint8_t> w;
  put_id(w, "RIFF"); put_u32(w, 0); put_id(w, "WAVE");
  put_id(w, "fmt "); put_u32(w, 16);
  put_u16(w, 1); put_u16(w, 1); put_u32(w, 50000); put_u32(w, 100000); put_u16(w, 2); put_u16(w, 16);
  put_id(w, "LIST"); put_u32(w, 500);   // 500-byte LIST whose body (and the following data) is beyond the buffer
  WavInfo wi2;
  CHECK(!wav_parse_header(w.data(), w.size(), 50000, &wi2), "data-past-buffer self-mutes");
}

static void test_is_wav_name() {
  CHECK(sdp_is_wav_name("loc_00.wav"), "accept loc_00.wav");
  CHECK(sdp_is_wav_name("volley_12.WAV"), "accept upper .WAV");
  CHECK(sdp_is_wav_name("a.wav"), "accept a.wav");
  CHECK(!sdp_is_wav_name("._loc_00.wav"), "reject AppleDouble ._*.wav");
  CHECK(!sdp_is_wav_name(".wav"), "reject .wav (dotfile)");
  CHECK(!sdp_is_wav_name(".DS_Store"), "reject .DS_Store");
  CHECK(!sdp_is_wav_name("song.txt"), "reject .txt");
  CHECK(!sdp_is_wav_name("wav"), "reject too short");
  CHECK(!sdp_is_wav_name(""), "reject empty");
  CHECK(!sdp_is_wav_name("noext"), "reject no ext");
}

static void test_ring() {
  SdpRing<8> r;
  r.reset();
  CHECK(r.empty() && r.available() == 0 && r.space() == 8, "ring init");
  for (int i = 0; i < 8; i++) CHECK(r.push((int16_t)(100 + i)), "push fills");
  CHECK(r.space() == 0 && r.available() == 8, "ring full");
  CHECK(!r.push(999), "push on full drops");
  int16_t out;
  for (int i = 0; i < 8; i++) {
    CHECK(r.pop(&out), "pop nonempty");
    CHECK(out == (int16_t)(100 + i), "pop FIFO order");
  }
  CHECK(!r.pop(&out), "pop empty underruns");
  // wrap-around: push/pop past the end many times
  int16_t expect = 0;
  for (int cyc = 0; cyc < 100; cyc++) {
    for (int i = 0; i < 5; i++) r.push((int16_t)(expect + i));
    for (int i = 0; i < 5; i++) { r.pop(&out); CHECK(out == (int16_t)(expect + i), "wrap FIFO"); }
    expect += 5;
  }
}

static void test_apply() {
  CHECK(sdp_apply(20000, 1.0f, 1) == 20000, "unity +");
  CHECK(sdp_apply(20000, 1.0f, -1) == -20000, "unity -");
  CHECK(sdp_apply(20000, 0.5f, 1) == 10000, "half gain");
  CHECK(sdp_apply(30000, 1.5f, 1) == 32767, "clamp +");
  CHECK(sdp_apply(-30000, 1.5f, -1) == 32767, "clamp - flips to +");
  CHECK(sdp_apply(-30000, 1.5f, 1) == -32767, "clamp -");
  CHECK(sdp_apply(0, 1.0f, -1) == 0, "zero stays zero");
}

static void test_pick() {
  CHECK(sdp_pick_index(0, 5) == -1, "empty dir -> -1");
  CHECK(sdp_pick_index(5, 7) == 2, "wrap draw");
  CHECK(sdp_pick_index(5, 0) == 0, "draw 0");
  CHECK(sdp_pick_index(1, 123) == 0, "single file");
}

int main() {
  test_wav_parse();
  test_wav_parse_robust();
  test_is_wav_name();
  test_ring();
  test_apply();
  test_pick();
  if (g_fail == 0) { printf("OK\n"); return 0; }
  printf("%d CHECK(s) FAILED\n", g_fail);
  return 1;
}
