import pytest

from backend.competition.fixture import FixturePlatform
from backend.platform.mapping import PlatformChallenge


def challenge(external_id: str, *, remote: bool = False):
    return PlatformChallenge(
        external_id=external_id,
        title=f"Challenge {external_id}",
        category="web" if remote else "misc",
        description="controlled fixture",
        remote=remote,
    )


def test_fixture_covers_sync_and_all_verdict_states():
    platform = FixturePlatform(
        [challenge("one"), challenge("two", remote=True)],
        flags={"one": "flag{correct}"},
        remote_limit=1,
    )
    assert [item.external_id for item in platform.preflight()] == ["one", "two"]
    assert platform.submit("one", "flag{wrong}")["verdict"] == "wrong"
    assert platform.submit("one", "flag{correct}")["verdict"] == "correct"
    pending = platform.submit("two", "fixture:pending:correct")
    assert pending["verdict"] == "pending"
    assert platform.query("two", pending["submission_id"])["verdict"] == "correct"
    assert platform.submit("two", "fixture:rate-limited") == {
        "verdict": "rate_limited",
        "retry_after": 60,
    }

    platform.replace_challenges([challenge("two", remote=True)])
    assert [item.external_id for item in platform.challenges()] == ["two"]


def test_fixture_instance_quota_renewal_and_release():
    platform = FixturePlatform(
        [challenge("one", remote=True), challenge("two", remote=True)],
        remote_limit=1,
    )
    assert platform.start_instance("one")["state"] == "ready"
    platform.renew_instance("one")
    assert platform.instances()[0]["renewals"] == 1
    with pytest.raises(RuntimeError, match="quota"):
        platform.start_instance("two")
    platform.stop_instance("one")
    assert platform.start_instance("two")["external_id"] == "two"
