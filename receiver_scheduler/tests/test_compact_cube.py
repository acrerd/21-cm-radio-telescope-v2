"""The compact HI4PI cube is memory-mapped from an unpacked copy beside its
.xz, shared by every simulator, and rebuilt when the .xz changes.

On 2026-10-01 the scheduler held two in-memory copies of the 378 MB cube (one
per cached simulator configuration) and peaked at 2.1 GB while unpacking."""
import io
import lzma
import os
import sys

import numpy as np
import pytest

import observatory

if observatory.SIMULATOR_DIR not in sys.path:
    sys.path.insert(0, observatory.SIMULATOR_DIR)
import hi4pi_compress as H                                      # noqa: E402


def _write_xz(path, t):
    buf = io.BytesIO()
    np.savez(buf, t=t, scale=0.01, v=np.linspace(-1e5, 1e5, t.shape[0]),
             lon=np.arange(t.shape[2], dtype=float), lat=np.arange(t.shape[1], dtype=float),
             fwhm=1.0, sigma=0.1, thresh=0.3)
    with lzma.open(path, "wb") as f:
        f.write(buf.getvalue())


@pytest.fixture(autouse=True)
def _fresh_process_cache():
    H._LOADED.clear()
    yield
    H._LOADED.clear()


def test_the_cube_is_mapped_from_disk_and_shared(tmp_path):
    t = np.arange(4 * 5 * 6, dtype=np.int16).reshape(4, 5, 6)
    xz = str(tmp_path / "cube.npz.xz")
    _write_xz(xz, t)
    c = H.load_compact(xz)
    assert os.path.exists(str(tmp_path / "cube.t.npy")) and os.path.exists(str(tmp_path / "cube.meta.npz"))
    assert isinstance(c.t, np.memmap) and not c.t.flags.writeable
    assert np.array_equal(c.t, t) and c.scale == 0.01 and c.fwhm == 1.0
    assert H.load_compact(xz) is c                              # one cube per process
    H._LOADED.clear()
    again = H.load_compact(xz)                                  # a new process: mapped, not unpacked
    assert isinstance(again.t, np.memmap) and np.array_equal(again.t, t)


def test_a_changed_xz_is_unpacked_again(tmp_path):
    xz = str(tmp_path / "cube.npz.xz")
    _write_xz(xz, np.zeros((2, 3, 4), np.int16))
    H.load_compact(xz)
    new = np.full((3, 3, 4), 7, np.int16)
    _write_xz(xz, new)
    os.utime(xz, ns=(os.stat(xz).st_atime_ns, os.stat(xz).st_mtime_ns + 10 ** 9))
    assert np.array_equal(H.load_compact(xz).t, new)


def test_an_unwritable_folder_falls_back_to_memory(tmp_path, monkeypatch):
    xz = str(tmp_path / "cube.npz.xz")
    t = np.ones((2, 3, 4), np.int16)
    _write_xz(xz, t)

    def refuse(*a, **k):
        raise PermissionError("read-only")
    monkeypatch.setattr(H, "_write_unpacked", refuse)
    c = H.load_compact(xz)
    assert not isinstance(c.t, np.memmap) and np.array_equal(c.t, t)
