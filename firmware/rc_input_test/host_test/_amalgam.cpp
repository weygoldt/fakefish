// _amalgam.cpp — NOT a test. Pulls this standalone diagnostic sketch into one translation
// unit so `make check` can syntax-check it with the real Teensy compiler, the same way it
// checks the two real sketches. See firmware/eel_fakefish_rc/host_test/_amalgam.cpp.
//
// rc_input_test bundles no core (it is deliberately dependency-free — no output stage, no
// playback), so this needs only the sketch dir on the include path.
#include <Arduino.h>
#include "../rc_input_test.ino"
