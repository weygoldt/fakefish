"""fakefish — electric-fish EOD playback firmware toolchain.

The Python side of the project: generate the Teensy stimulus library from
source recordings, render/simulate/visualise it, and model synthetic volleys.
The firmware itself (``firmware/eel_fakefish/``) has no Python dependency —
flashing needs neither this package nor the field data.
"""

__version__ = "0.1.0"
