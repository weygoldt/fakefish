// Host harness: compile the device-agnostic engine + the generated library on a
// PC and stream one item's samples to stdout, so it can be diffed against the
// Python reference (tools/export_teensy_stimuli.py :: reconstruct_item).
//
//   g++ -I firmware/eel_fakefish firmware/eel_fakefish/eel_player.cpp \
//       firmware/eel_fakefish/eel_stimuli.cpp \
//       firmware/eel_fakefish/host_test/eel_player_selftest.cpp -lm -o /tmp/selftest
//   /tmp/selftest <item_index> [amplitude] [polarity]
#include <cstdio>
#include <cstdlib>
#include "eel_player.h"

int main(int argc, char** argv) {
  int idx = argc > 1 ? atoi(argv[1]) : 0;
  float amp = argc > 2 ? (float)atof(argv[2]) : 1.0f;
  int pol = argc > 3 ? atoi(argv[3]) : 1;
  if (idx < 0 || idx >= N_STIM_ITEMS) { fprintf(stderr, "bad index\n"); return 1; }
  EelPlayer p;
  eel_player_start(&p, (uint8_t)idx, amp, (int8_t)pol);
  int16_t s;
  while (eel_player_next(&p, &s)) printf("%d\n", s);
  return 0;
}
