"""Hardware sources. The controller talks to these instead of GPIO pins."""

from brewski.sources.base import HardwareSource
from brewski.sources.dummy import DummySource

__all__ = ["DummySource", "HardwareSource"]
