# PlatformIO pre-build step: make diag.cpp compile on every build. Its
# __DATE__ __TIME__ is the build date /diag reports and the page shows under
# About; an incremental build that reused diag.o kept an old date (2026-10-02:
# a fresh flash still read the previous build's time). Touching the source
# was not enough - SCons decides by content, not by timestamp - so the object
# is removed and has to be rebuilt.
import os

Import("env")  # noqa: F821 - provided by PlatformIO's SCons

obj = os.path.join(env.subst("$BUILD_DIR"), "src", "diag.cpp.o")  # noqa: F821
if os.path.exists(obj):
    os.remove(obj)
