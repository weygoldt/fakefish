// Standalone RC-input BENCH TEST for the fakefish stimulator — READ-ONLY diagnostic.
//
// NOT the firmware: no output stage, no DRV8871, no playback. It never drives the output pins
// (2/3) — its only job is to confirm the receiver -> HY-M154 PC817 board -> Teensy path before you
// trust the real firmware. Flash it, open the Serial Monitor at 115200, and wiggle the sticks.
//
// WIRING (HY-M154 4-channel PC817 board, BARE COLLECTOR — no pull-up pin on the board):
//   receiver CHx signal -> input Vx ;  receiver GND -> input-side G   (isolation barrier)
//   board output Vx -> Teensy pin below, as INPUT_PULLUP (internal pull-up = the collector load)
//   board output G  -> Teensy GND ;  NOTHING to the Teensy 3.3 V
// The PC817 INVERTS: the pin is LOW while the RC servo pulse is HIGH, so the true 1000-2000 us
// width is the LOW duration -> this sketch measures the LOW time. If widths jitter, add an
// external 1-2 kOhm pull-up per Vx to 3.3 V (try INPUT_PULLUP alone first).
//
//   Teensy pin 4 = CH3 (throttle / rate)     pin 6 = CH5 (randomness pot)
//   Teensy pin 5 = CH4 (trigger)             pin 7 = CH6 (amplitude pot)
// (Same pin numbers as RC_PIN_* in firmware/eel_fakefish_rc/rc_control.h — keep them in sync.
//  Pins are 4-7, not 5-8: pin 8 was dead on the build Teensy, so every channel shifted down one.)

static const uint8_t  RC_PIN[4]  = { 4, 5, 6, 7 };
static const char*    RC_NAME[4] = { "CH3(rate)", "CH4(trig)", "CH5(jit)", "CH6(amp)" };
static const uint32_t PRINT_MS   = 200;      // one report line every ~200 ms
static const uint32_t PRESENT_MS = 500;      // "present" == a valid pulse seen in the last 500 ms
// "present" window. The PC817 offset pushes real widths well below nominal (this rig reads ~630 us
// at a stick's low extreme), so the floor is 500 not 900 — else working channels false-read ABSENT.
static const uint32_t VALID_LO   = 500;
static const uint32_t VALID_HI   = 2100;

static volatile uint32_t low_start[4]      = {0, 0, 0, 0};  // micros() at the falling edge (LOW begins)
static volatile uint32_t width_us[4]       = {0, 0, 0, 0};  // last measured LOW (== servo) width
// millis() of the last VALID pulse. A ms clock is wrap-safe for ~49 days (matching the firmware's
// presence tracking); a micros() diff would wrap at ~71 min and could then briefly false-report
// "present" on a long-dead channel.
static volatile uint32_t last_valid_ms[4]  = {0, 0, 0, 0};

// One edge on channel i: measure the LOW duration (the PC817-inverted servo pulse).
static inline void rc_edge(uint8_t i) {
  uint32_t now = micros();
  bool high = (digitalReadFast(RC_PIN[i]) != LOW);
  if (!high) {                         // falling edge (HIGH -> LOW): the active LOW pulse starts
    low_start[i] = now;
  } else {                             // rising edge (LOW -> HIGH): LOW pulse ended
    uint32_t w = now - low_start[i];
    if (w >= 300u && w <= 3000u) {     // plausible width -> show it
      width_us[i] = w;
      if (w >= VALID_LO && w <= VALID_HI) last_valid_ms[i] = millis() | 1u;  // valid pulse (non-zero ms stamp)
    }
  }
}
static void rc_isr0() { rc_edge(0); }
static void rc_isr1() { rc_edge(1); }
static void rc_isr2() { rc_edge(2); }
static void rc_isr3() { rc_edge(3); }

void setup() {
  Serial.begin(115200);
  static void (*const isrs[4])() = { rc_isr0, rc_isr1, rc_isr2, rc_isr3 };
  for (int i = 0; i < 4; i++) {
    pinMode(RC_PIN[i], INPUT_PULLUP);  // internal pull-up = the bare-collector load
    attachInterrupt(digitalPinToInterrupt(RC_PIN[i]), isrs[i], CHANGE);
  }
}

void loop() {
  static uint32_t last_print = 0;
  if ((uint32_t)(millis() - last_print) < PRINT_MS) return;
  last_print = millis();

  uint32_t now_ms = millis();
  for (int i = 0; i < 4; i++) {
    uint32_t w    = width_us[i];                       // atomic 32-bit read on the M7
    uint32_t seen = last_valid_ms[i];
    bool present  = seen != 0 && (uint32_t)(now_ms - seen) < PRESENT_MS;
    bool high     = (digitalReadFast(RC_PIN[i]) != LOW);   // live raw pin state
    Serial.print(RC_NAME[i]); Serial.print(": ");
    Serial.print(w);          Serial.print(" us [");
    Serial.print(high ? "HIGH" : "LOW"); Serial.print("] ");
    Serial.print(present ? "present" : "ABSENT");
    if (i < 3) Serial.print(" | ");
  }
  Serial.println();
}
