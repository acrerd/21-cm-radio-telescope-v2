"""A simulator target keeps its name all the way to the recording.

Booking "Lockman Hole" from the simulator used to arrive as
`Spectrum l=150.0 b=+53.0`. The coordinates are already in the entry's own
fields, and they do not say what the field is *for* - which, for an
off-position or an HVC, is the whole reason it was chosen. The name becomes the
entry's name and then the recording's `obs_name`, so it is worth carrying.

Two halves are guarded: the scheduler uses the name when it is given one, and
the simulator page actually sends it - matched by position rather than
remembered from a click, so that editing the coordinate boxes off the target
stops the name being sent instead of dragging it onto a different patch of sky.
"""
import os
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import h1_web_scheduler as sched

SIM_JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "..", "astro_simulator", "web", "js", "ui.js")


@pytest.fixture
def client(tmp_path):
    """A Flask test client with the schedule and config isolated, so booking
    here never touches the observatory's real schedule."""
    sched.app.config['TESTING'] = True
    patches = [patch.object(sched, 'SCHEDULE_FILE', str(tmp_path / "schedule.json")),
               patch.object(sched, 'CONFIG_FILE', str(tmp_path / "config.json"))]
    for p in patches:
        p.start()
    try:
        with sched.app.test_client() as c:
            yield c
    finally:
        for p in patches:
            p.stop()


def _book(client, **over):
    epoch = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    body = {'l': 150.0, 'b': 53.0, 'mode': 'hi',
            'epoch_utc': epoch.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'center_freq_mhz': 1420.405752, 'bandwidth_mhz': 2.0,
            'channels': 327, 'integration_time_s': 600.0}
    body.update(over)
    with patch.object(sched, 'apply_horizon_trim', side_effect=lambda obs: obs):
        resp = client.post('/api/simulator/schedule', json=body)
    return resp


class TestTheSchedulerUsesTheName:
    def test_a_named_target_names_the_entry(self, client):
        d = _book(client, target_name='Lockman Hole').get_json()
        assert d['success'], d
        assert d['entry']['name'] == 'Lockman Hole spectrum'

    def test_a_drift_scan_says_drift(self, client):
        d = _book(client, target_name='Lockman Hole', mode='cont',
                  scan_minutes=120).get_json()
        assert d['success'], d
        assert d['entry']['name'] == 'Lockman Hole drift'

    def test_without_a_name_the_coordinates_still_name_it(self, client):
        """The old behaviour has to survive: most pointings are not a target."""
        d = _book(client).get_json()
        assert d['success'], d
        assert d['entry']['name'] == 'Spectrum l=150.0 b=+53.0'

    def test_a_moving_body_wins_over_a_target_name(self, client):
        """The Sun is tracked as an object, and 'Sun spectrum' is what the
        rest of the system calls it. A coincidental target name must not
        displace that."""
        d = _book(client, object='sun', target_name='Lockman Hole').get_json()
        assert d['success'], d
        assert d['entry']['name'] == 'Sun spectrum'
        assert d['entry']['object_name'] == 'sun'

    def test_the_name_reaches_the_stored_schedule(self, client):
        d = _book(client, target_name='HVC Complex C').get_json()
        assert d['success'], d
        stored = client.get('/api/schedule').get_json()
        assert d['entry']['name'] in [o['name'] for o in stored]


class TestTheNameIsCleaned:
    """It arrives from a web page and ends up in the schedule, the scheduler
    log and the HDF5 `obs_name`. It never reaches a filename - those carry
    only the time and the mode - so there is nothing to traverse, but a
    newline in the operational log would still be a small forgery."""

    def test_control_characters_become_spaces_rather_than_vanishing(self):
        """A newline that simply disappeared would weld two words into one
        that was never sent - 'Lock\\nman' reading back as 'Lockman'. Turning
        it into a space keeps the name honest about what arrived."""
        assert sched._clean_target_name('Lock\nman\tHole') == 'Lock man Hole'
        assert '\n' not in sched._clean_target_name('a\nb')
        assert sched._clean_target_name('a\nb') == 'a b'

    def test_whitespace_is_collapsed_and_trimmed(self):
        assert sched._clean_target_name('  Lockman   Hole  ') == 'Lockman Hole'

    def test_it_is_capped(self):
        assert len(sched._clean_target_name('x' * 500)) == sched._TARGET_NAME_MAX

    def test_rubbish_is_not_a_name(self):
        for bad in (None, 42, [], {}, '', '   ', '\n\t'):
            assert sched._clean_target_name(bad) == ''

    def test_a_cleaned_away_name_falls_back_to_coordinates(self, client):
        d = _book(client, target_name='\n\t  ').get_json()
        assert d['success'], d
        assert d['entry']['name'] == 'Spectrum l=150.0 b=+53.0'


class TestTheSimulatorSendsIt:
    """No JavaScript engine on this host (see test_page_javascript), so these
    read the source. Narrow, but they catch the change being made on one side
    only, which is the way this would actually break."""

    @pytest.fixture
    def js(self):
        with open(SIM_JS) as fh:
            return fh.read()

    def test_the_schedule_post_carries_target_name(self, js):
        body = js[js.index('/api/simulator/schedule'):]
        body = body[:body.index('});')]
        assert 'target_name' in body, "the booking must send the name"

    def test_the_name_is_matched_by_position_not_remembered(self, js):
        """Remembering the clicked target would survive an edit to the
        coordinate boxes and label a different patch of sky with it."""
        assert 'function namedTarget(' in js
        assert 'TARGET_MATCH_DEG' in js
        fn = js[js.index('function namedTarget('):]
        fn = fn[:fn.index('\n  }')]
        assert 'sepDeg(' in fn, "matched by angular separation"
        assert 'TARGETS' in fn and 'sky.sources' in fn, "both lists searched"

    def test_a_body_is_still_preferred(self, js):
        """Sun and Moon are in sky.sources too; they must stay objects."""
        assert 'object ? "" : namedTarget(' in js

    def test_the_tolerance_is_tight_enough_to_mean_something(self, js):
        m = re.search(r'TARGET_MATCH_DEG\s*=\s*([0-9.]+)', js)
        assert m, "the tolerance should be a named constant"
        tol = float(m.group(1))
        # The menu writes two decimals, so it has to exceed 0.005; and it must
        # stay far inside the 4.57 deg beam or it would name a neighbouring
        # field as the target.
        assert 0.005 < tol < 0.5, tol
