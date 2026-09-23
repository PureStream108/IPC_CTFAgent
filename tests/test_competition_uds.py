from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from backend.competition.transport import Envelope, PairCoordinator, call


pytestmark = pytest.mark.skipif(os.name != "posix", reason="gRPC UDS requires Linux")


def test_grpc_uds_uses_protobuf_and_idempotency_callbacks(tmp_path):
    completed = []
    now = datetime.now(timezone.utc)
    message = Envelope(
        run_id="run", challenge_id="challenge",
        sender_member_id="amber", receiver_member_id="agate",
        sender_session_id="sender", receiver_session_id="receiver",
        request_id="request", lease_epoch=1,
        created_at=now, deadline_at=now + timedelta(seconds=10),
        message_type="command", payload={"task": "inspect"},
    )
    coordinator = PairCoordinator(
        tmp_path / "pair.sock",
        authorize=lambda item: item.request_id == "request",
        claim=lambda _item: None,
        execute=lambda item, cancel: {
            "status": "success", "request_id": item.request_id,
            "cancelled": cancel.is_set(),
        },
        finish=lambda item, result: completed.append((item.request_id, result)),
    )
    coordinator.start()
    try:
        result = call(tmp_path / "pair.sock", message)
    finally:
        coordinator.close()

    assert result == {
        "status": "success",
        "request_id": "request",
        "cancelled": False,
    }
    assert completed[0][0] == "request"
    assert not (tmp_path / "pair.sock").exists()
