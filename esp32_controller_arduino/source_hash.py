"""A hash of the controller firmware's source, so a flash of code the controller
already runs can be skipped.

The binary cannot be compared: every build stamps its own date into diag.cpp
(stamp_build.py), so two builds of the same source never match. The hash is
compiled into the firmware (stamp_build.py writes it into build_hash.h, /diag
reports it as "source_hash"), and the scheduler's Update firmware computes it
on the checkout and compares the two. One definition, imported by both.

Covers everything that decides what is built: src/, platformio.ini, the
partition tables and these two scripts, plus the PlatformIO environment name.
Library versions under .pio/libdeps are not covered.
"""
import hashlib
import os

_FILES = ("platformio.ini", "partitions.csv", "partitions_wt32_ota.csv",
          "stamp_build.py", "source_hash.py")


def source_files(project_dir):
    """Paths, relative to project_dir and sorted, of every file hashed."""
    rel = [f for f in _FILES if os.path.isfile(os.path.join(project_dir, f))]
    src = os.path.join(project_dir, "src")
    for root, _dirs, files in os.walk(src):
        for name in files:
            rel.append(os.path.relpath(os.path.join(root, name), project_dir).replace(os.sep, "/"))
    return sorted(rel)


def source_hash(project_dir, env_name):
    """First 16 hex digits of a SHA-256 over the environment name and each
    file's relative path and contents."""
    h = hashlib.sha256(env_name.encode() + b"\0")
    for rel in source_files(project_dir):
        h.update(rel.encode() + b"\0")
        with open(os.path.join(project_dir, rel), "rb") as f:
            h.update(f.read())
        h.update(b"\0")
    return h.hexdigest()[:16]
