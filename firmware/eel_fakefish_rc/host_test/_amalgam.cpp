// _amalgam.cpp — NOT a test. A translation unit that pulls the whole sketch in so it can be
// syntax-checked with the real Teensy compiler:
//
//   arm-none-eabi-g++ -fsyntax-only -std=gnu++17 -Wall -Wextra ...
//       -I <teensy4 core> -I firmware/eel_fakefish_rc
//       firmware/eel_fakefish_rc/host_test/_amalgam.cpp
//
// The host tests cover the PURE logic; this covers everything they cannot — the ISR, the HAL
// call signatures, the Arduino glue and every #include path — without needing a board. It is the
// firmware half of `make check` (arduino-cli is not installed here).
//
// It lives in host_test/ ON PURPOSE. The Arduino build compiles every .cpp in the SKETCH ROOT
// (and recursively under src/), but IGNORES other subdirectories — so a stray _amalgam.cpp at
// the sketch root would be compiled into the real firmware and collide with the .ino's setup()
// and loop(). Here it is invisible to the Arduino build.
//
// Arduino normally auto-generates forward declarations for a .ino; we rely on the sketch being
// written in definition-before-use order instead, which is also what keeps it readable.
#include <Arduino.h>
#include "../eel_fakefish_rc.ino"
