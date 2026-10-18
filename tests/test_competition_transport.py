import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
import zmq

from backend.competition.proto import pair_pb2
from backend.competition.transport import (
    Envelope,
    PairCoordinator,
    ReconPublisher,
    ReconSubscriber,
    WaitGraph,
    call,
    encode,
    envelope_from_proto,
    envelope_to_proto,
    result_from_proto,
    result_to_proto,
)


class _Abort(Exception):
    pass


class _RpcContext:
    def __init__(self, *, remaining=10.0):
        self.remaining = remaining
        self.callbacks = []

    def abort(self, code, detail):
        raise _Abort(f"{code.name}: {detail}")

    def time_remaining(self):
        return self.remaining

    def add_callback(self, callback):
        self.callbacks.append(callback)
        return True


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
    with pytest.raises(ValueError, match="already"):
        waits.enter("a", "d")
    waits = WaitGraph()
    waits.enter("a", "b")
    waits.enter("b", "c")
    with pytest.raises(ValueError, match="cyclic"):
        waits.enter("c", "b")


def test_transport_validation_and_result_error_paths():
    with pytest.raises(ValueError, match="timezone"):
        message(created_at=datetime.now(), deadline_at=datetime.now() + timedelta(seconds=1))
    with pytest.raises(ValueError, match="same Member"):
        message(receiver_member_id="amber")
    with pytest.raises(ValueError, match="256 KiB"):
        encode({"payload": "x" * (256 * 1024)})
    with pytest.raises(ValueError, match="not valid JSON"):
        result_from_proto(pair_pb2.Result(payload_json=b"not-json"))
    with pytest.raises(ValueError, match="JSON object"):
        result_from_proto(pair_pb2.Result(payload_json=b"[]", status="ok"))
    expired_at = datetime.now(timezone.utc)
    expired = message(
        created_at=expired_at - timedelta(seconds=2),
        deadline_at=expired_at - timedelta(seconds=1),
    )
    with pytest.raises(TimeoutError, match="expired"):
        call("unused.sock", expired)


def test_pair_coordinator_call_is_idempotent_and_converts_failures():
    finished = []
    coordinator = PairCoordinator(
        "/tmp/unused.sock",
        authorize=lambda _message: True,
        claim=lambda _message: None,
        execute=lambda _message, _cancel: {"status": "done", "value": 3},
        finish=lambda message, result: finished.append((message.request_id, result)),
    )
    try:
        result = coordinator._call(envelope_to_proto(message()), _RpcContext())
        assert result_from_proto(result)["value"] == 3
        assert finished[0][0] == "request"
    finally:
        coordinator.close()

    coordinator = PairCoordinator(
        "/tmp/unused.sock",
        authorize=lambda _message: True,
        claim=lambda _message: {"status": "cached", "value": 9},
        execute=lambda *_args: pytest.fail("cached request must not execute"),
        finish=lambda *_args: pytest.fail("cached request must not finish"),
    )
    try:
        assert result_from_proto(coordinator._call(envelope_to_proto(message()), _RpcContext()))["value"] == 9
    finally:
        coordinator.close()


def test_pair_coordinator_authorization_deadline_and_cancel_paths():
    raw = envelope_to_proto(message())
    coordinator = PairCoordinator(
        "/tmp/unused.sock", authorize=lambda _message: False,
        claim=lambda _message: None, execute=lambda *_args: {}, finish=lambda *_args: None,
    )
    try:
        with pytest.raises(_Abort, match="PERMISSION_DENIED"):
            coordinator._call(raw, _RpcContext())
    finally:
        coordinator.close()

    coordinator = PairCoordinator(
        "/tmp/unused.sock", authorize=lambda _message: True,
        claim=lambda _message: None, execute=lambda *_args: {}, finish=lambda *_args: None,
    )
    try:
        with pytest.raises(_Abort, match="INVALID_ARGUMENT"):
            coordinator._call(pair_pb2.Envelope(payload_json=b"not-json"), _RpcContext())
        with pytest.raises(_Abort, match="DEADLINE_EXCEEDED"):
            expired_created = datetime.now(timezone.utc) - timedelta(seconds=2)
            coordinator._call(
                envelope_to_proto(message(
                    created_at=expired_created,
                    deadline_at=expired_created + timedelta(seconds=1),
                )),
                _RpcContext(),
            )
        with pytest.raises(_Abort, match="DEADLINE_EXCEEDED"):
            coordinator._call(envelope_to_proto(message()), _RpcContext(remaining=None))
        coordinator.waits.enter("sender-session", "other")
        with pytest.raises(_Abort, match="FAILED_PRECONDITION"):
            coordinator._call(envelope_to_proto(message()), _RpcContext())
        coordinator.waits.leave("sender-session")
    finally:
        coordinator.close()

    coordinator = PairCoordinator(
        "/tmp/unused.sock", authorize=lambda _message: True,
        claim=lambda _message: None,
        execute=lambda _message, cancel: cancel.set(),
        finish=lambda _message, result: None,
    )
    try:
        assert result_from_proto(coordinator._call(raw, _RpcContext()))["status"] == "cancelled"
    finally:
        coordinator.close()

    coordinator = PairCoordinator(
        "/tmp/unused.sock", authorize=lambda _message: True,
        claim=lambda _message: None,
        execute=lambda *_args: (_ for _ in ()).throw(RuntimeError("boom")),
        finish=lambda _message, result: None,
    )
    try:
        result = result_from_proto(coordinator._call(raw, _RpcContext()))
        assert result["status"] == "failed" and "RuntimeError" in result["error"]
    finally:
        coordinator.close()


def test_recon_subscriber_validation_and_ack_errors():
    with pytest.raises(ValueError, match="configured together"):
        ReconSubscriber(None, "challenge", session_id="s")
    with pytest.raises(ValueError, match="live-only"):
        ReconSubscriber(None, "challenge")
    context = zmq.Context()
    subscriber = ReconSubscriber(
        "inproc://recon-errors", "challenge", session_id="receiver-session",
        replay=lambda _session, **_kwargs: [],
        advance=lambda _session, sequence: sequence,
        context=context,
    )
    try:
        with pytest.raises(RuntimeError, match="no recon record"):
            subscriber.ack()
        with pytest.raises(ValueError, match="positive persisted"):
            subscriber._queue({"sequence": 0, "envelope": message(message_type="recon").model_dump(mode="json")})
    finally:
        subscriber.close()
        context.term()


def test_recon_subscriber_rejects_malformed_and_duplicate_records():
    context = zmq.Context()
    subscriber = ReconSubscriber(
        "inproc://recon-malformed", "challenge", session_id="receiver-session",
        replay=lambda _session, **_kwargs: [],
        advance=lambda _session, sequence: sequence,
        context=context,
    )
    record = {"sequence": 1, "envelope": message(message_type="recon").model_dump(mode="json")}
    try:
        with pytest.raises(ValueError, match="JSON object"):
            subscriber._normalize([])
        with pytest.raises(ValueError, match="different challenge"):
            subscriber._normalize({"sequence": 1, "envelope": message(challenge_id="other", message_type="recon").model_dump(mode="json")})
        with pytest.raises(ValueError, match="different receiver"):
            subscriber._normalize({"sequence": 1, "envelope": message(receiver_session_id="other", message_type="recon").model_dump(mode="json")})
        with pytest.raises(ValueError, match="does not match"):
            subscriber._normalize({"sequence": 1, "envelope": message(sequence=2, message_type="recon").model_dump(mode="json")})
        subscriber._queue(record)
        with pytest.raises(ValueError, match="different content"):
            subscriber._queue({"sequence": 1, "envelope": message(request_id="other", message_type="recon").model_dump(mode="json")})
        subscriber._inflight = subscriber._pending[1]
        with pytest.raises(ValueError, match="in-flight"):
            subscriber.ack(2)
        subscriber._inflight = None
        subscriber._pending.clear()
        subscriber._cursor = 1
        subscriber._inflight = record
        subscriber.advance = lambda _session, _sequence: 0
        with pytest.raises(RuntimeError, match="did not advance"):
            subscriber.ack()
    finally:
        subscriber.close()
        context.term()


def test_recon_subscriber_replays_orders_deduplicates_and_acknowledges():
    first = message(
        request_id="first", message_type="recon",
        sequence=0,
    ).model_dump(mode="json")
    second = message(
        request_id="second", message_type="recon",
        sequence=0,
    ).model_dump(mode="json")
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
        assert subscriber.receive(timeout_ms=0)["sequence"] == 4
        assert subscriber.receive(timeout_ms=0)["sequence"] == 4
        assert subscriber.ack() == 4
        assert subscriber.receive(timeout_ms=0)["sequence"] == 9
        subscriber.ack()
        assert subscriber.receive(timeout_ms=0) is None
    finally:
        subscriber.close()
        context.term()


def test_recon_subscriber_ignores_peer_broadcasts():
    """One challenge topic broadcasts every receiver's mail to every member."""
    context = zmq.Context()
    records = []

    def persist(envelope):
        record = {"sequence": len(records) + 1, "envelope": envelope.model_dump(mode="json")}
        records.append(record)
        return record

    publisher = ReconPublisher("inproc://recon-peer-filter", persist, context=context)
    subscriber = ReconSubscriber(
        "inproc://recon-peer-filter", "challenge", session_id="receiver-session",
        replay=lambda _session, **_kwargs: [],
        advance=lambda _session, sequence: sequence,
        context=context,
    )
    try:
        time.sleep(0.05)
        publisher.publish(message(request_id="peer", receiver_session_id="peer-session", message_type="recon"))
        publisher.publish(message(request_id="mine", receiver_session_id="receiver-session", message_type="recon"))
        received = subscriber.receive(timeout_ms=2000)
        assert received is not None
        assert received["sequence"] == 2
        assert received["envelope"]["request_id"] == "mine"
        assert subscriber.ack() == 2
        assert subscriber.receive(timeout_ms=100) is None
    finally:
        subscriber.close()
        publisher.close()
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
