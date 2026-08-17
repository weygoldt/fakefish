"""fakefish — electric-fish EOD playback firmware toolchain.

The Python side of the project: generate the Teensy stimulus library from
source recordings, render/simulate/visualise it, and model synthetic volleys.
The firmware itself (``firmware/``) has no Python dependency — flashing needs
neither this package nor the field data. Playback on the button device does need
an SD card built by ``fakefish-build-card`` (which reads the committed library,
not the dataset); only regenerating the library needs the source recordings.
"""

__version__ = "0.1.0"
