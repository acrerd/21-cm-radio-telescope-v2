# PlatformIO pre-build step, two jobs.
#
# 1. Make diag.cpp compile on every build. Its __DATE__ __TIME__ is the build
# date /diag reports and the page shows under About; an incremental build that
# reused diag.o kept an old date (2026-10-02: a fresh flash still read the
# previous build's time). Touching the source was not enough - SCons decides by
# content, not by timestamp - so the object is removed and has to be rebuilt.
#
# 2. Write the source hash (source_hash.py) into build_hash.h in the build
# directory, for /diag to report. The scheduler's Update firmware compares it
# with the checkout's and flashes nothing when they match. The header lives in
# the build directory, not src/, so it is not itself part of what is hashed.
import os
import sys

Import("env")  # noqa: F821 - provided by PlatformIO's SCons

project_dir = env.subst("$PROJECT_DIR")  # noqa: F821
build_dir = env.subst("$BUILD_DIR")  # noqa: F821
sys.path.insert(0, project_dir)
from source_hash import source_hash  # noqa: E402

obj = os.path.join(build_dir, "src", "diag.cpp.o")
if os.path.exists(obj):
    os.remove(obj)

os.makedirs(build_dir, exist_ok=True)
with open(os.path.join(build_dir, "build_hash.h"), "w") as f:
    f.write('#define SRT_SOURCE_HASH "%s"\n' % source_hash(project_dir, env.subst("$PIOENV")))  # noqa: F821
env.Append(CPPPATH=[build_dir])  # noqa: F821
