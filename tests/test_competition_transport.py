from datetime import datetime, timedelta, timezone
import threading
import time

import pytest

from backend.competition.proto import pair_pb2
from backend.competition.transport import (
    Envelope,
    ReconPublisher,
    ReconSubscriber,
    WaitGraph,
    envelope_from_proto,
    envelope_to_proto,
    result_from_proto,
    result_to_proto,
)
import zmq


def message(**changes):
    now = datetime.now(timezone.utc)
    values = {
        "run_id": "run",
        "challenge_id": "challenge",
        "sender_member_id": "amber",
        "receiver_member_id": "agate",
        "sender_session_id": "sender-session",
        "receiver_session_id": "receiver-session",
        "request_id": "request",
        "lease_epoch": 2,
        "created_at": now,
        "deadline_at": now + timedelta(seconds=30),
        "message_type": "command",
        "payload": {"task": "inspect", "artifact": "shared/result.txt"},
    }
    values.update(changes)
    return Envelope.model_validate(values)


def test_protobuf_is_the_grpc_envelope_contract():
    original = message()
    encoded = envelope_to_proto(original).SerializeToString()
    wire = pair_pb2.Envelope.FromString(encoded)
    restored = envelope_from_proto(wire)
    assert restored == original

    response = result_from_proto(
        pair_pb2.Result.FromString(
            result_to_proto({"status": "partial", "artifact": "a.txt"}, "request").SerializeToString()
        )
    )
    assert response == {
        "status": "partial",
        "artifact": "a.txt",
        "request_id": "request",
    }


def test_invalid_protobuf_payload_and_cyclic_wait_are_rejected():
    raw = envelope_to_proto(message())
    raw.payload_json = b"not-json"
    with pytest.raises(ValueError, match="payload_json"):
        envelope_from_proto(raw)

    waits = WaitGraph()
    waits.enter("a", "b")
    waits.enter("b", "c")
    with pytest.raises(ValueError, match="cyclic"):
        waits.enter("c", "a")


def test_recon_subscriber_replays_orders_deduplicates_and_acknowledges():
    first = message(
        request_id="first", message_type="recon",
        sequence=0,
    ).model_dump(mode="json")
    second = message(
        request_id="second", message_type="recon",
        sequence=0,
    ).model_dump(mode="json")
    # Global message sequences can contain records for other sessions, so a
    # missing integer between two records is not itself an error.
    first = {"sequence": 4, "envelope": first, "status": "done"}
    second = {"sequence": 9, "envelope": second, "status": "done"}
    records = [first, second]
    cursor = {"value": 3}

    def replay(session_id, **kwargs):
        assert session_id == "receiver-session"
        after = kwargs.get("after", cursor["value"])
        return [item for item in records if item["sequence"] > after]

    def advance(session_id, sequence):
        assert session_id == "receiver-session"
        cursor["value"] = max(cursor["value"], sequence)
        return cursor["value"]

    context = zmq.Context()
    subscriber = ReconSubscriber(
        "inproc://recon-replay", "challenge", session_id="receiver-session",
        replay=replay, advance=advance, context=context,
    )
    try:
        # No live PUB message is needed: reconnect recovery is driven by the
        # durable callback and remains deterministic in unit tests.
        assert subscriber.receive(timeout_ms=0)["sequence"] == 4
        assert subscriber.receive(timeout_ms=0)["sequence"] == 4
        assert subscriber.ack() == 4
        assert subscriber.receive(timeout_ms=0)["sequence"] == 9
        subscriber.ack()
        assert subscriber.receive(timeout_ms=0) is None
    finally:
        subscriber.close()
        context.term()


def test_recon_subscriber_rejects_unknown_protocol_version():
    record = message(message_type="recon").model_dump(mode="json")
    record["protocol_version"] = 99
    context = zmq.Context()
    subscriber = ReconSubscriber(
        "inproc://recon-version", "challenge", session_id="receiver-session",
        replay=lambda _session, **_kwargs: [{"sequence": 1, "envelope": record}],
        advance=lambda _session, sequence: sequence,
        context=context,
    )
    try:
        with pytest.raises(ValueError, match="protocol_version"):
            subscriber.receive(timeout_ms=0)
    finally:
        subscriber.close()
        context.term()


def test_recon_publisher_accepts_member_worker_threads():
    context = zmq.Context()
    records = []

    def persist(envelope):
        record = {
            "sequence": len(records) + 1,
            "envelope": envelope.model_dump(mode="json"),
            "status": "done",
        }
        records.append(record)
        return record

    publisher = ReconPublisher("inproc://recon-workers", persist, context=context)
    subscriber = ReconSubscriber("inproc://recon-workers", "challenge", context=context)
    try:
        # PUB/SUB subscriptions are asynchronous; allow the subscriber to
        # connect before sending from several independent Member threads.
        time.sleep(0.05)
        workers = [
            threading.Thread(
                target=publisher.publish,
                args=(message(request_id=str(index), message_type="recon"),),
            )
            for index in range(10)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=2)
            assert not worker.is_alive()
        received = {}
        deadline = time.monotonic() + 3
        while len(received) < len(workers) and time.monotonic() < deadline:
            item = subscriber.receive(timeout_ms=100)
            if item is not None:
                received[item["sequence"]] = item
        assert set(received) == set(range(1, 11))
    finally:
        subscriber.close()
        publisher.close()
        context.term()


@pytest.mark.skipif(__import__("os").name != "posix", reason="requires POSIX IPC socket")
def test_recon_publisher_recovers_stale_ipc_socket_without_stealing_live_owner(tmp_path):
    socket_path = tmp_path / "recon.sock"
    socket_path.write_bytes(b"stale socket path")
    endpoint = f"ipc://{socket_path}"
    publisher = ReconPublisher(endpoint, lambda _message: {})
    try:
        assert socket_path.exists()
        with pytest.raises(RuntimeError, match="owned by another process"):
            ReconPublisher(endpoint, lambda _message: {})
        assert socket_path.exists()
    finally:
        publisher.close()
    assert not socket_path.exists()
