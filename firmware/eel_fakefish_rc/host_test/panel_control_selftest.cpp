// Host self-test for the pure panel logic (no Arduino): the button debounce + press
// edge-detector, and the 3-button panel pin map. Compile + run on a PC:
//   g++ -std=c++17 firmware/eel_fakefish_rc/host_test/panel_control_selftest.cpp -o t && ./t
#include <cstdio>

#include "../panel_control.h"
#include "../rc_control.h"   // RC_PIN_* — the panel must not collide with the RC inputs

static int g_fail = 0;
#define CHECK(cond, msg) do { if (!(cond)) { printf("FAIL: %s\n", msg); g_fail++; } } while (0)

static void test_debounce_basic() {
  DebounceState d;
  debounce_init(&d);
  CHECK(!debounce_fell(&d, true, 0, 25), "idle no fire");
  CHECK(!debounce_fell(&d, false, 5, 25), "press t=5 not stable");
  CHECK(!debounce_fell(&d, false, 20, 25), "press t=20 not stable");
  CHECK(debounce_fell(&d, false, 30, 25), "press commits at t=30");
  CHECK(!debounce_fell(&d, false, 40, 25), "held press does not refire");
  CHECK(!debounce_fell(&d, true, 60, 25), "release edge");
  CHECK(!debounce_fell(&d, true, 90, 25), "released stable");
  CHECK(!debounce_fell(&d, false, 100, 25), "2nd press not yet stable");
  CHECK(debounce_fell(&d, false, 130, 25), "2nd press commits");
}

static void test_debounce_bounce() {
  DebounceState d;
  debounce_init(&d);
  CHECK(!debounce_fell(&d, false, 0, 25), "bounce a");
  CHECK(!debounce_fell(&d, true, 10, 25), "bounce b");
  CHECK(!debounce_fell(&d, false, 15, 25), "bounce c");
  CHECK(!debounce_fell(&d, true, 20, 25), "bounce d");
  CHECK(!debounce_fell(&d, false, 30, 25), "bounce e (timer restarted)");
  CHECK(debounce_fell(&d, false, 60, 25), "settles low -> fire");
}

static void test_panel_pins() {
  // The three panel buttons must sit on distinct pins that avoid every other GPIO user on this
  // board: the FOUR DRV8871 output pins (L1 HAL — note this is now 0,1,2,3, not just the old
  // 2,3, with IN2 on 22/23 under the complementary drive), the four RC input pins, the
  // indicator LED, and A0 (== digital 14, the randomSeed source).
  //
  // Checked against the REAL macros rather than pin literals, so moving a pin in
  // src/eel_core/config.h or rc_control.h fails HERE instead of on the water.
  const int pins[3] = { PANEL_LOC_PIN, PANEL_VOLLEY_PIN, PANEL_SHAM_PIN };
  const int drv[4]  = { DRV_A_IN1_PIN, DRV_A_IN2_PIN, DRV_B_IN1_PIN, DRV_B_IN2_PIN };
  const int rc[4]   = { RC_PIN_THROTTLE, RC_PIN_TRIGGER, RC_PIN_RANDOM, RC_PIN_AMP };
  for (int i = 0; i < 3; i++) {
    for (int k = 0; k < 4; k++) {
      CHECK(pins[i] != drv[k], "panel avoids the DRV8871 output pins");
      CHECK(pins[i] != rc[k], "panel avoids the RC input pins");
    }
    CHECK(pins[i] != LED_PIN, "panel avoids the LED pin");
    CHECK(pins[i] != 14, "panel avoids A0 (randomSeed)");
    for (int j = i + 1; j < 3; j++) CHECK(pins[i] != pins[j], "panel pins distinct");
  }
}

int main() {
  test_debounce_basic();
  test_debounce_bounce();
  test_panel_pins();
  if (g_fail == 0) { printf("OK\n"); return 0; }
  printf("%d CHECK(s) FAILED\n", g_fail);
  return 1;
}
