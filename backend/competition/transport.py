"""Single-host gRPC/UDS control and ZeroMQ reconnaissance transport.

The coordinator is hosted by a Linux worker, never by a challenge sandbox.
Protobuf is the gRPC source-of-truth contract. JSON remains the persisted and
ZeroMQ representation, with explicit conversion through ``Envelope``.
"""
from __future__ import annotations

import json
import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import grpc
import zmq
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.competition.proto import pair_pb2


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[1] = 1
    run_id: str = Field(min_length=1, max_length=128)
    challenge_id: str = Field(min_length=1, max_length=128)
    sender_member_id: str
    receiver_member_id: str
    sender_session_id: str
    receiver_session_id: str
    request_id: str = Field(min_length=1, max_length=128)
    correlation_id: str = ""
    sequence: int = Field(default=0, ge=0)
    lease_epoch: int = Field(ge=1)
    created_at: datetime
    deadline_at: datetime
    message_type: Literal["command", "result", "recon", "status", "context", "cancel"]
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_time(self):
        if self.created_at.tzinfo is None or self.deadline_at.tzinfo is None:
            raise ValueError("message timestamps must be timezone aware")
        if self.deadline_at <= self.created_at:
            raise ValueError("message deadline must follow creation")
        if self.sender_member_id == self.receiver_member_id:
            raise ValueError("cannot synchronously call the same Member")
        return self


def encode(value) -> bytes:
    def default(item):
        if isinstance(item, datetime):
            return item.isoformat()
        raise TypeError(f"{type(item).__name__} is not JSON serializable")

    data = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), default=default
    ).encode()
    if len(data) > 256 * 1024:
        raise ValueError("messages are limited to 256 KiB; use artifact references")
    return data


def envelope_to_proto(message: Envelope) -> pair_pb2.Envelope:
    payload = encode(message.payload)
    return pair_pb2.Envelope(
        protocol_version=message.protocol_version,
        run_id=message.run_id,
        challenge_id=message.challenge_id,
        sender_member_id=message.sender_member_id,
        receiver_member_id=message.receiver_member_id,
        sender_session_id=message.sender_session_id,
        receiver_session_id=message.receiver_session_id,
        request_id=message.request_id,
        correlation_id=message.correlation_id,
        sequence=message.sequence,
        lease_epoch=message.lease_epoch,
        created_at=message.created_at.isoformat(),
        deadline_at=message.deadline_at.isoformat(),
        message_type=message.message_type,
        payload_json=payload,
    )


def envelope_from_proto(raw: pair_pb2.Envelope) -> Envelope:
    try:
        payload = json.loads(raw.payload_json or b"{}")
    except (TypeError, ValueError) as exc:
        raise ValueError("payload_json is not valid JSON") from exc
    return Envelope.model_validate({
        "protocol_version": raw.protocol_version,
        "run_id": raw.run_id,
        "challenge_id": raw.challenge_id,
        "sender_member_id": raw.sender_member_id,
        "receiver_member_id": raw.receiver_member_id,
        "sender_session_id": raw.sender_session_id,
        "receiver_session_id": raw.receiver_session_id,
        "request_id": raw.request_id,
        "correlation_id": raw.correlation_id,
        "sequence": raw.sequence,
        "lease_epoch": raw.lease_epoch,
        "created_at": raw.created_at,
        "deadline_at": raw.deadline_at,
        "message_type": raw.message_type,
        "payload": payload,
    })


def result_to_proto(value: dict, request_id: str) -> pair_pb2.Result:
    return pair_pb2.Result(
        status=str(value.get("status") or "ok"),
        request_id=str(value.get("request_id") or request_id),
        payload_json=encode(value),
    )


def result_from_proto(raw: pair_pb2.Result) -> dict:
    try:
        value = json.loads(raw.payload_json or b"{}")
    except (TypeError, ValueError) as exc:
        raise ValueError("result payload_json is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("result payload must be a JSON object")
    value.setdefault("status", raw.status)
    value.setdefault("request_id", raw.request_id)
    return value


class WaitGraph:
    def __init__(self):
        self._waiting: dict[str, str] = {}
        self._lock = threading.Lock()

    def enter(self, source: str, target: str):
        with self._lock:
            if source in self._waiting:
                raise ValueError("Member already has a synchronous request in progress")
            cursor = target
            seen = {source}
            while cursor in self._waiting:
                if cursor in seen:
                    raise ValueError("cyclic synchronous wait; use asynchronous reconnaissance")
                seen.add(cursor)
                cursor = self._waiting[cursor]
            if cursor == source:
                raise ValueError("cyclic synchronous wait; use asynchronous reconnaissance")
            self._waiting[source] = target

    def leave(self, source: str):
        with self._lock:
            self._waiting.pop(source, None)


class PairCoordinator:
    def __init__(self, socket_path: Path, *, authorize: Callable[[Envelope], bool],
                 execute: Callable[[Envelope, threading.Event], dict],
                 claim: Callable[[Envelope], dict | None],
                 finish: Callable[[Envelope, dict], None]):
        self.socket_path = Path(socket_path)
        self.authorize, self.execute = authorize, execute
        self.claim, self.finish = claim, finish
        self.waits = WaitGraph()
        self._started = False
        self._lock_file = None
        self.pool = ThreadPoolExecutor(max_workers=12, thread_name_prefix="ipc-pair")
        self.server = grpc.server(self.pool, options=[("grpc.max_receive_message_length", 256 * 1024)])
        self.server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler("ipc.Pair", {
            "Call": grpc.unary_unary_rpc_method_handler(
                self._call,
                request_deserializer=pair_pb2.Envelope.FromString,
                response_serializer=lambda value: value.SerializeToString(),
            ),
        }),))

    def start(self):
        if os.name != "posix":
            raise RuntimeError("gRPC Unix sockets require the Linux Docker/WSL worker runtime")
        if len(str(self.socket_path).encode()) > 100:
            raise ValueError("Unix socket path is too long")
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # An advisory owner lock distinguishes a stale crash artifact from a
        # live coordinator before removing the Unix socket path.
        import fcntl

        lock_path = self.socket_path.with_suffix(self.socket_path.suffix + ".lock")
        self._lock_file = lock_path.open("a+b")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            self._lock_file = None
            raise RuntimeError("coordinator socket is owned by another process") from exc
        if self.socket_path.exists():
            self.socket_path.unlink()
        if not self.server.add_insecure_port(f"unix:{self.socket_path}"):
            raise RuntimeError("failed to bind coordinator Unix socket")
        self.server.start()
        os.chmod(self.socket_path, 0o600)
        self._started = True

    def _call(self, raw, context):
        try:
            message = envelope_from_proto(raw)
        except ValueError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        if not self.authorize(message):
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "session or lease is not active")
        remaining = (message.deadline_at - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0 or context.time_remaining() is None:
            context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "a bounded, unexpired deadline is required")
        try:
            self.waits.enter(message.sender_session_id, message.receiver_session_id)
        except ValueError as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        cancel = threading.Event()
        timer = threading.Timer(min(remaining, context.time_remaining(), 30), cancel.set)
        context.add_callback(cancel.set)
        timer.start()
        try:
            previous = self.claim(message)
            if previous is not None:
                return result_to_proto(previous, message.request_id)
            try:
                result = self.execute(message, cancel)
            except Exception as exc:
                # A claimed request must never remain ``running`` forever
                # after a worker exception.  Persist a bounded failure result
                # so a retry receives the same outcome and does not repeat a
                # side effect.
                result = {
                    "status": "failed",
                    "request_id": message.request_id,
                    "error": f"{type(exc).__name__}: {exc}"[:2000],
                }
            if cancel.is_set():
                result = {"status": "cancelled", "request_id": message.request_id}
            self.finish(message, result)
            return result_to_proto(result, message.request_id)
        finally:
            timer.cancel()
            self.waits.leave(message.sender_session_id)

    def close(self):
        self.server.stop(grace=1).wait(timeout=5)
        self.pool.shutdown(wait=False, cancel_futures=True)
        if self._started:
            self.socket_path.unlink(missing_ok=True)
            self._started = False
        if self._lock_file is not None:
            import fcntl

            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None


def call(socket_path: Path, message: Envelope) -> dict:
    remaining = min(30.0, (message.deadline_at - datetime.now(timezone.utc)).total_seconds())
    if remaining <= 0:
        raise TimeoutError("message expired")
    with grpc.insecure_channel(f"unix:{socket_path}", options=[("grpc.max_receive_message_length", 256 * 1024)]) as channel:
        request = channel.unary_unary(
            "/ipc.Pair/Call",
            request_serializer=lambda value: value.SerializeToString(),
            response_deserializer=pair_pb2.Result.FromString,
        )
        return result_from_proto(
            request(envelope_to_proto(message), timeout=remaining)
        )


class ReconPublisher:
    """Persist and publish recon records from one ZeroMQ-owning thread.

    ZeroMQ sockets are thread-bound.  Member workers may call ``publish`` from
    many threads, so the public method only persists and queues an encoded
    notification; the private thread owns bind/send/close.  Durable replay is
    still authoritative when the queue is full or the live socket is down.
    """

    _STOP = object()

    def __init__(
        self,
        endpoint: str,
        persist: Callable[[Envelope], dict],
        *,
        context: zmq.Context | None = None,
    ):
        self.context = context or zmq.Context()
        self._owns_context = context is None
        self.endpoint = endpoint
        self.persist = persist
        self._queue: queue.Queue[tuple[bytes, bytes] | object] = queue.Queue(maxsize=1000)
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._closed = False
        self._error: BaseException | None = None
        self._bound = False
        self._socket = None
        self._lock_file = None
        self._prepare_ipc_lock()
        self._thread = threading.Thread(
            target=self._run, name="ipc-recon-publisher", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=5):
            self.close()
            raise RuntimeError("timed out starting recon publisher")
        if self._error is not None:
            error = self._error
            self.close()
            raise RuntimeError(f"failed to bind recon publisher: {error}") from error

    def _prepare_ipc_lock(self) -> None:
        """Own an IPC endpoint before removing a crash-stale socket path."""
        if not self.endpoint.startswith("ipc://") or os.name != "posix":
            return
        import fcntl

        socket_path = Path(self.endpoint[6:])
        socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = socket_path.with_suffix(socket_path.suffix + ".lock")
        self._lock_file = lock_path.open("a+b")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            self._lock_file = None
            raise RuntimeError("recon socket is owned by another process") from exc
        # flock is released by the OS after a crash; the path itself is not.
        # Only the process holding the lock is allowed to remove it.
        socket_path.unlink(missing_ok=True)

    def _run(self) -> None:
        socket = None
        try:
            socket = self.context.socket(zmq.PUB)
            socket.setsockopt(zmq.SNDHWM, 1000)
            socket.setsockopt(zmq.LINGER, 0)
            socket.bind(self.endpoint)
            self._socket = socket
            self._bound = True
        except BaseException as exc:
            self._error = exc
            self._ready.set()
            if socket is not None:
                socket.close()
            return
        self._ready.set()
        try:
            while not self._stop.is_set():
                try:
                    item = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if item is self._STOP:
                    break
                topic, payload = item
                try:
                    socket.send_multipart([topic, payload], flags=zmq.NOBLOCK)
                except Exception as exc:
                    # PostgreSQL replay remains available after a live socket
                    # failure; stop sending rather than touching the socket
                    # from another thread.
                    self._error = exc
                    break
        finally:
            socket.close()
            self._socket = None

    def publish(self, message: Envelope) -> dict:
        stored = self.persist(message)
        # Persisted sequence is authoritative for reconnect/replay.  A full
        # live queue is intentionally lossy: subscribers catch up from the DB.
        if not self._closed and self._error is None:
            try:
                self._queue.put_nowait((message.challenge_id.encode(), encode(stored)))
            except queue.Full:
                pass
        return stored

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        with suppress(queue.Full):
            self._queue.put_nowait(self._STOP)
        thread = getattr(self, "_thread", None)
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
        if self._bound and self.endpoint.startswith("ipc://"):
            Path(self.endpoint[6:]).unlink(missing_ok=True)
        if self._lock_file is not None:
            import fcntl

            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None
        if self._owns_context:
            self.context.term()


class ReconSubscriber:
    """Receive live notifications with PostgreSQL-backed replay.

    With ``session_id``, ``replay`` and ``advance`` configured, ``receive``
    first fills its queue from durable storage, orders and de-duplicates by
    the persisted sequence, and keeps one record in flight until ``ack``.
    This gives callers at-least-once delivery across subscriber restarts.
    """

    def __init__(
        self,
        endpoint: str | None,
        challenge_id: str,
        *,
        session_id: str | None = None,
        replay: Callable[..., list[dict]] | None = None,
        advance: Callable[[str, int], int] | None = None,
        context: zmq.Context | None = None,
    ):
        if any(value is not None for value in (session_id, replay, advance)) and not all(
            value is not None for value in (session_id, replay, advance)
        ):
            raise ValueError("session_id, replay and advance must be configured together")
        if endpoint is None and replay is None:
            raise ValueError("live-only recon requires a ZeroMQ endpoint")
        self.context = (context or zmq.Context()) if endpoint else None
        self._owns_context = endpoint is not None and context is None
        self.socket = None
        if endpoint:
            self.socket = self.context.socket(zmq.SUB)
            self.socket.setsockopt(zmq.RCVHWM, 1000)
            self.socket.setsockopt(zmq.LINGER, 0)
            self.socket.setsockopt(zmq.SUBSCRIBE, challenge_id.encode())
            self.socket.connect(endpoint)
        self.challenge_id = challenge_id
        self.session_id = session_id
        self.replay = replay
        self.advance = advance
        self._cursor: int | None = None
        self._pending: dict[int, dict] = {}
        self._inflight: dict | None = None

    @property
    def reliable(self) -> bool:
        return self.replay is not None

    def _normalize(self, value: Any) -> dict:
        if not isinstance(value, dict):
            raise ValueError("recon record must be a JSON object")
        envelope_value = value.get("envelope", value)
        if not isinstance(envelope_value, dict):
            raise ValueError("recon envelope must be a JSON object")
        sequence = value.get("sequence", envelope_value.get("sequence"))
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0:
            raise ValueError("recon record requires a positive persisted sequence")
        envelope = Envelope.model_validate(envelope_value)
        if envelope.challenge_id != self.challenge_id:
            raise ValueError("recon record belongs to a different challenge")
        if self.session_id is not None and envelope.receiver_session_id != self.session_id:
            raise ValueError("recon record belongs to a different receiver session")
        if envelope.sequence not in {0, sequence}:
            raise ValueError("envelope sequence does not match the persisted sequence")
        normalized = dict(value)
        normalized["sequence"] = sequence
        normalized["envelope"] = envelope.model_copy(
            update={"sequence": sequence}
        ).model_dump(mode="json")
        return normalized

    def _queue(self, value: Any) -> None:
        record = self._normalize(value)
        sequence = record["sequence"]
        if self._cursor is not None and sequence <= self._cursor:
            return
        existing = self._pending.get(sequence)
        if existing is not None and existing != record:
            raise ValueError("duplicate recon sequence has different content")
        self._pending[sequence] = record

    def _catch_up(self) -> None:
        if self.replay is None or self.session_id is None:
            return
        if self._cursor is None:
            records = self.replay(self.session_id)
        else:
            records = self.replay(self.session_id, after=self._cursor)
        for record in records:
            self._queue(record)

    def ack(self, sequence: int | None = None) -> int:
        if not self.reliable or self.session_id is None or self.advance is None:
            raise RuntimeError("persistent acknowledgement is not configured")
        if self._inflight is None:
            raise RuntimeError("there is no recon record awaiting acknowledgement")
        expected = self._inflight["sequence"]
        if sequence is not None and sequence != expected:
            raise ValueError("only the in-flight recon record can be acknowledged")
        cursor = self.advance(self.session_id, expected)
        if cursor < expected:
            raise RuntimeError("persistent cursor did not advance")
        self._cursor = cursor
        self._pending = {
            key: value for key, value in self._pending.items() if key > cursor
        }
        self._inflight = None
        return cursor

    def receive(self, timeout_ms: int = 1000) -> dict | None:
        if self._inflight is not None:
            return self._inflight
        if self.reliable:
            self._catch_up()
            if self._pending:
                self._inflight = self._pending[min(self._pending)]
                return self._inflight
        if self.socket is None or not self.socket.poll(timeout_ms):
            return None
        topic, payload = self.socket.recv_multipart()
        if topic.decode() != self.challenge_id:
            return None
        try:
            value = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise ValueError("recon payload is not valid JSON") from exc
        if not self.reliable:
            # Legacy live-only consumers keep their original return shape,
            # but versioned envelopes are still validated when present.
            if isinstance(value, dict) and "envelope" in value:
                self._normalize(value)
            return value
        self._queue(value)
        # Persistence precedes publication.  Re-querying here fills any older
        # records that were missed while this subscriber was offline.
        self._catch_up()
        if not self._pending:
            return None
        self._inflight = self._pending[min(self._pending)]
        return self._inflight

    def close(self):
        if self.socket is not None:
            self.socket.close()
        if self._owns_context and self.context is not None:
            self.context.term()
