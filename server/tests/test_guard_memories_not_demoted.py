'`_cold_always_loaded` is the sharpest of the three. A guard memory is one you HOPE is\nnever read: it earns its slot by being in context before a dangerous action, not by\nbeing consulted. "No reads over a full window" is its expected steady state, so ranking\nby coldness puts the must-keep memories at the top of the demotion queue.'
import pytest

from agent_context import index as I


class _FakeStore:
    root = "/nonexistent"        

    def __init__(self, entities):
        self.entities = {i: e for i, e in enumerate(entities)}


def _mem(slug, description, updated="2020-01-01T00:00:00Z", memory_type="project",
         body=""):
    return {"type": "memory", "slug": slug, "scope": "global",
            "memory_type": memory_type, "description": description, "body": body,
            "load_behavior": "always", "updated_at": updated}



@pytest.mark.parametrize("body", [
    "GOTCHA: the cache is a projection, not a source of truth.",
    "This is a TRAP: perl -pi replaces the seeded symlink.",
    "The 2026-07-09 outage came from a rename migration on the shared DB.",
    "Caused a regression in route planning.",
    "See policy for the collision this prevents.",
    "The per-target key silently overrides the deploy-generated one.",
])
def test_a_body_recording_an_incident_is_recognized(body):
    assert I._records_an_incident(_mem("x", "d", body=body))


def test_an_ordinary_body_is_not():
    assert not I._records_an_incident(
        _mem("x", "d", body="The grid uses an 8px rhythm and a 4px half-step."))


def test_the_two_signals_are_independent():
    'Either alone must be enough. They catch different things: a guard is known from\n    its DESCRIPTION, which is all the bootstrap loads, while an incident memory is known\n    only from its BODY -- the four this observation named say nothing about deploys.'
    assert I._must_stay_loaded(_mem("x", "a push here deploys prod", body="plain"))
    assert I._must_stay_loaded(_mem("x", "plain description", body="GOTCHA: ordering"))
    assert not I._must_stay_loaded(_mem("x", "plain description", body="plain body"))



@pytest.mark.parametrize("slug,desc", [
    ("project_user_dev_deploy", "the site, push-to-deploy. Live."),
    ("some-service", "a push here deploys the live site"),
    ("db-migration", "running this against production cannot be undone"),
    ("key-rotation", "rotating this invalidates every existing token"),
    ("cleanup-job", "destructive: removes the originals"),
])
def test_a_guard_memory_is_recognized(slug, desc):
    assert I._guards_irreversible(_mem(slug, desc))


@pytest.mark.parametrize("slug,desc", [
    ("ui-spacing", "the card grid uses an 8px rhythm"),
    ("fzf-tab-override", "the vendored completion widget is patched"),
    ("test-naming", "tests are named after the behavior, not the method"),
])
def test_an_ordinary_memory_is_not(slug, desc):
    'The guard must not swallow the queue. A demotion check that flags nothing is\n    just as broken as one that flags a guard, and far quieter about it.'
    assert not I._guards_irreversible(_mem(slug, desc))



def test_stale_always_loaded_skips_a_guard_but_still_flags_a_worklog():
    store = _FakeStore([
        _mem("project_site_deploy", "a push here IS the deploy"),
        _mem("project_old_worklog", "migrating the settings screen"),
    ])
    flagged = {r["slug"] for r in I._stale_always_loaded(store)}
    assert "project_old_worklog" in flagged, "the check stopped doing its job"
    assert "project_site_deploy" not in flagged, "policy: proposed demoting a guard"


def test_prune_candidates_skips_a_guard():
    store = _FakeStore([
        _mem("project_site_deploy", "push-to-deploy. Done, shipped and stable."),
        _mem("project_done_thing", "Done, shipped and stable."),
    ])
    flagged = {r["slug"] for r in I._prune_candidates(store)}
    assert "project_done_thing" in flagged
    assert "project_site_deploy" not in flagged


def test_cold_always_loaded_skips_a_guard_precisely_because_it_is_cold(monkeypatch):
    "The inversion in its purest form: zero reads is a guard memory's SUCCESS."
    class _Fleet:
        since = 0.0
        def stats(self, *a, **k):
            return {"reads": 0, "hits": 0}

    monkeypatch.setattr(I.usage, "load_fleet", lambda *a, **k: _Fleet())
    store = _FakeStore([
        _mem("project_site_deploy", "push-to-deploy", memory_type="reference"),
        _mem("some-cold-note", "an 8px grid rhythm", memory_type="reference"),
    ])
    flagged = {r["slug"] for r in I._cold_always_loaded(store)}
    assert "some-cold-note" in flagged, "the demotion queue stopped working"
    assert "project_site_deploy" not in flagged, "policy: cold guard queued for demotion"
