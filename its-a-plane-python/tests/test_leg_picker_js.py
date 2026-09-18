"""The leg picker's browser-side contract, exercised under a stubbed DOM.

Both bugs behind the UA1714 report lived in ``index.html``'s ``<script>``, where
no Python test could reach them: the multi-leg branch was unreachable because
``found`` was tested before ``multiple``, and ``saveCallsign`` posted a bare
callsign, so the backend could not tell LGA->DEN from DEN->GJT.  ``tests/js/
index_harness.mjs`` evals that same ``<script>`` against a stub document and
reports what each response shape actually posts to ``/tracked/set``; this test
runs it and asserts the payloads.

Skipped, not failed, where node is absent -- the Pis have no node and their
own test runs must stay green.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "js" / "index_harness.mjs"
TEMPLATE = ROOT / "web" / "templates" / "index.html"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this host"
)


@pytest.fixture(scope="module")
def harness():
    """Run the DOM harness once and hand every test its result object."""
    proc = subprocess.run(
        ["node", str(HARNESS), str(TEMPLATE)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, (
        f"harness exited {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken harness
        pytest.fail(f"harness printed non-JSON:\n{proc.stdout}\n{proc.stderr}")


def test_multi_leg_saves_nothing_until_picked(harness):
    """A 2-leg response must not guess; nothing is posted until the user picks."""
    assert harness["multi_savedNothing"] is True


def test_multi_leg_renders_one_button_per_leg(harness):
    """The original bug: 'select one' with no picker rendered."""
    assert harness["multi_buttons"] == 2


def test_each_leg_shows_which_airline_flies_it(harness):
    """Codeshare legs share a callsign and can share a route line; without the
    operator on the button there is nothing to choose between them."""
    assert harness["multi_showsAirline"] is True


def test_picking_a_leg_posts_that_leg(harness):
    """Picking the SECOND leg must post DEN->GJT, not a bare callsign.

    Both legs of UA1714 are callsign UAL1714, so the route and the scheduled
    departure are the only things that distinguish them.
    """
    assert harness["pick_payload"] == {
        "callsign": "UAL1714",
        "cached_route": {"origin": "DEN", "destination": "GJT"},
        "scheduled_departure": 2,
    }


def test_single_live_result_still_carries_its_route(harness):
    """The ordinary one-flight path must pin its leg too, not only the picker."""
    assert harness["single_payload"] == {
        "callsign": "UAL1714",
        "cached_route": {"origin": "LGA", "destination": "DEN"},
        "scheduled_departure": 99,
    }


def test_one_leg_multiple_saves_that_leg(harness):
    """``multiple`` with a single leg auto-saves -- it must save the LEG.

    Saving a bare callsign here would drop the pin and let the tracker pick
    whichever leg it found first.
    """
    assert harness["oneLegMultiple_payload"] == {
        "callsign": "UAL1714",
        "cached_route": {"origin": "DEN", "destination": "GJT"},
        "scheduled_departure": 7,
    }


def test_failed_lookup_saves_nothing(harness):
    assert harness["notFound_savedNothing"] is True


def test_failed_lookup_clears_a_stale_picker(harness):
    """A picker left on screen after a failed lookup pins the wrong flight."""
    assert harness["notFound_legsCleared"] is True


def test_no_server_string_reaches_an_html_sink(harness):
    """Every field the picker renders is relayed from AirLabs.

    Counts sink use across ALL elements built during the render, not just the
    button: the first version of this test asserted ``btn.innerHTML === ""``,
    which passes unchanged when the markup goes through a child element -- and
    the sub-line (dep_time, status) is exactly such a child.
    """
    assert harness["xss_noHtmlSinkUsed"] is True


def test_markup_in_a_field_is_still_shown_as_text(harness):
    """Escaping must not silently drop the value -- a leg whose status is odd
    should still read as that odd status, not vanish."""
    assert harness["xss_markupRenderedAsText"] is True


def test_empty_input_clears_a_stale_picker(harness):
    """Track with an empty box returns early -- only the up-front clear runs.

    Without it the previous flight's buttons stay on screen and stay clickable.
    """
    assert harness["emptyInput_legsCleared"] is True
    assert harness["emptyInput_savedNothing"] is True


class TestTheOnDemandOtherLegsLink:
    """A live match never consults AirLabs, so a connection is invisible until
    asked for -- and asking spends a credit, which is why it is a link rather
    than automatic. This shipped with no coverage at all."""

    def test_the_link_is_offered_after_a_match(self, harness):
        assert harness["otherLegs_linkOffered"] is True

    def test_no_credit_is_spent_until_it_is_clicked(self, harness):
        """The whole point of the link: a normal lookup must stay free."""
        assert harness["otherLegs_noCreditBeforeClick"] is True

    def test_clicking_asks_the_server_for_that_callsign(self, harness):
        assert harness["otherLegs_asked"] == "UAL1714"

    def test_the_leg_already_matched_is_not_offered_back(self, harness):
        """Offering the leg you are already tracking is noise, and picking it
        would re-save what is already saved."""
        assert harness["otherLegs_excludesMatchedLeg"] is True

    def test_asking_does_not_change_what_is_tracked(self, harness):
        """Only clicking a LEG switches the flight; consulting must not."""
        assert harness["otherLegs_askingChangesNothing"] is True

    def test_a_flight_with_no_continuation_says_so(self, harness):
        """Not an empty picker, and not silence."""
        assert harness["otherLegs_noneSaysSo"] is True
