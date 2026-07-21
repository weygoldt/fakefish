// Host self-test for the pure control logic (no Arduino): the button debounce + press
// edge-detector, and the button->directory map. Compile + run on a PC:
//   g++ -std=c++17 eel_control_selftest.cpp -o t && ./t
#include <cstdio>
#include <cstring>

#include "../eel_control.h"

static int g_fail = 0;
#define CHECK(cond, msg) do { if (!(cond)) { printf("FAIL: %s\n", msg); g_fail++; } } while (0)

// Drive the debouncer over a millis() timeline of raw levels (true == released/HIGH).
static void test_debounce_basic() {
  DebounceState d;
  debounce_init(&d);
  // idle released: no fire
  CHECK(!debounce_fell(&d, true, 0, 25), "idle no fire");
  // press (LOW) but not yet stable for 25 ms
  CHECK(!debounce_fell(&d, false, 5, 25), "press t=5 not stable");
  CHECK(!debounce_fell(&d, false, 20, 25), "press t=20 not stable");
  // stable long enough -> fires exactly once
  CHECK(debounce_fell(&d, false, 30, 25), "press commits at t=30");
  CHECK(!debounce_fell(&d, false, 40, 25), "held press does not refire");
  // release, then a second clean press fires again
  CHECK(!debounce_fell(&d, true, 60, 25), "release edge");
  CHECK(!debounce_fell(&d, true, 90, 25), "released stable");
  CHECK(!debounce_fell(&d, false, 100, 25), "2nd press not yet stable");
  CHECK(debounce_fell(&d, false, 130, 25), "2nd press commits");
}

static void test_debounce_bounce() {
  DebounceState d;
  debounce_init(&d);
  // chattering contact: raw flips within the window, never stabilises -> no fire
  CHECK(!debounce_fell(&d, false, 0, 25), "bounce a");
  CHECK(!debounce_fell(&d, true, 10, 25), "bounce b");
  CHECK(!debounce_fell(&d, false, 15, 25), "bounce c");
  CHECK(!debounce_fell(&d, true, 20, 25), "bounce d");
  CHECK(!debounce_fell(&d, false, 30, 25), "bounce e (timer restarted)");
  // now it settles low for 25 ms -> fires once
  CHECK(debounce_fell(&d, false, 60, 25), "settles low -> fire");
}

static void test_dir_map() {
  CHECK(N_BUTTONS == 6, "six buttons");
  CHECK(strcmp(BTN_DIRS[0], "/A") == 0, "A -> /A");
  CHECK(strcmp(BTN_DIRS[3], "/D") == 0, "D -> /D");
  CHECK(strcmp(BTN_DIRS[5], "/F") == 0, "F -> /F");
}

int main() {
  test_debounce_basic();
  test_debounce_bounce();
  test_dir_map();
  if (g_fail == 0) { printf("OK\n"); return 0; }
  printf("%d CHECK(s) FAILED\n", g_fail);
  return 1;
}
