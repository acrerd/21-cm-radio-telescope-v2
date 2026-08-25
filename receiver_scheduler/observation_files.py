"""Where a recording goes, and what it is called.

Two rules, and everything here follows from them.

**The name carries only what you cannot get from the file.** A filename is a
handle, not a header. Until 2026-08-25 it carried the target name and the
calibrator flag as well, which sounds helpful and is not: both are already
attributes inside the HDF5 (`obs_name`, `calibrator`, put there by
`H1_OBS_METADATA`), so the name was a second copy that could disagree with the
first. Rename the file and the copy in the name is wrong; edit the schedule
entry after the fact and it is wrong too. What is left is the date and time -
which genuinely is not in the name's gift to be wrong about, since it is when
the file was made - and the observing mode, below.

**The mode is a physical fact, not a label.** `track` means the mount followed
the sky for the whole recording; `drift` means it was parked and the sky moved
through the beam. That distinction changes how the data must be reduced before
anything else does, so it earns its place in the name: it tells you whether a
row of the spectrogram is the same piece of sky as the row above it.

Note which coordinate systems land on which side. `altaz` is a *drift* scan.
It is not called one anywhere in the UI, but the scheduler sends it to
`/direct` and leaves tracking off - "no drift scan requires PyEphem" is about
the pointing calculation, not about what the mount then does. A dish parked at
a fixed alt/az is a drift scan by construction, and naming it `track` because
the entry was typed into a different box would be a lie about the data.

**All recordings live in one folder.** `data/` had become a junk drawer - on
2026-08-25 it held 248 files, of which 11 were observations and the rest were
plots, horizon partials, gain fits and a Stellarium zip. Meanwhile the manual
receiver, which has no `H1_OUTPUT_FILE` set for it, defaulted to `h1_data.h5`
in the working directory and had scattered 22 more across the repository root.
Recordings now go in one place and nothing else does.
"""

import os
from datetime import datetime

# The subfolder of the configured data folder that recordings go in. Its own
# folder, so "everything in here is a recording" stays true and a listing is
# not 96% plots.
OBSERVATION_SUBFOLDER = "observations"

# Coordinate systems where the mount follows the sky. Everything else parks the
# dish and lets the sky drift through - including `altaz`, deliberately: see
# the module docstring. Keep this in step with srt_point_telescope(), which is
# where the endpoint is actually chosen; this set is a description of what that
# function does, and if the two disagree the filename is the one that is wrong.
TRACKING_COORD_SYSTEMS = frozenset({'radec', 'galactic', 'object', 'satellite'})

# What a recording made outside the schedule is called. Not `track` or `drift`,
# because nobody told the mount to do either - the operator started the
# receiver at the console and pointed the dish by hand, if at all. Claiming a
# mode here would be inventing one.
MANUAL_MODE = 'manual'


def observations_folder(data_folder):
    """The recordings folder inside a configured data folder.

    Derived rather than separately configurable: two settings that must agree
    is one setting and a bug waiting to happen.
    """
    return os.path.join(data_folder, OBSERVATION_SUBFOLDER)


def observation_mode(obs):
    """'track' or 'drift' for a schedule entry: does the mount follow the sky?"""
    return ('track' if obs.get('coord_system', 'altaz') in TRACKING_COORD_SYSTEMS
            else 'drift')


def observation_filename(folder, mode, when=None):
    """<folder>/YYYYMMDD_HHMMSS_<mode>.h5, and it does not already exist.

    The uniqueness check is not ceremony. The receiver rolls to a new file
    whenever the frequency axis or the FFT width changes under it, and a
    console operator changing two settings in one motion can produce two rolls
    inside the same second - which before this returned the same name twice and
    the second open truncated the first file. One second of resolution is the
    right amount for a filename; the collision is handled rather than designed
    out with a longer stamp nobody can read.
    """
    when = when or datetime.now()
    stem = "%s_%s" % (when.strftime("%Y%m%d_%H%M%S"), mode)
    path = os.path.join(folder, stem + ".h5")
    n = 2
    while os.path.exists(path):
        path = os.path.join(folder, "%s_%d.h5" % (stem, n))
        n += 1
    return path
