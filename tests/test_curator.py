from __future__ import annotations

import json

from backend.agent.job_digest import build_deterministic_digest
from backend.core.config import AppConfig, LLMConfig
from backend.core.curator import SugiliteCurator, curator_member_config


class FakeStore:
    def __init__(self, job):
        self.job = job
        self.saved = None
        self.failed = None

    def running_for_project(self, *args, **kwargs):
        return []

    def requeue_curating(self, *args, **kwargs):
        return 0

    def pending_digests(self, *args, **kwargs):
        return [self.job]

    def claim_digest(self, job_id):
        return self.job

    def save_digest(self, job_id, digest):
        self.saved = digest
        self.job["digest_json"] = digest
        return self.job

    def fail_digest(self, job_id, error):
        self.failed = error


class FakeSandbox:
    def __init__(self, stdout="flag{demo}\npassword=hunter2\n"):
        self.stdout = stdout

    def job_output(self, job_id, stream="stdout", *, offset=0, limit=8192):
        text = self.stdout if stream == "stdout" else ""
        return text[offset : offset + limit], min(len(text), offset + limit)

    def job_state(self, job_id):
        return {"state": "exited", "exit_code": 0}


class FakeAdapter:
    def __init__(self, reply):
        self.reply = reply

    def chat(self, *args, **kwargs):
        return self.reply


class FakeLogger:
    def __init__(self):
        self.events = []

    def project(self, event, project_id, **fields):
        self.events.append((event, project_id, fields))


def _config():
    return AppConfig(member=LLMConfig(api_format="mock"))


def _job():
    return {
        "id": "job-1",
        "project_id": "proj_001",
        "member": "amber",
        "command": "curl http://target/",
        "state": "exited",
        "exit_code": 0,
    }


def test_curator_reuses_sugilite_member_config():
    config = _config()
    member = curator_member_config(config)
    assert member is not None
    assert member.name == "sugilite"
    assert member.api_format == "mock"


def test_curator_stores_a_refined_digest_and_keeps_flag():
    job = _job()
    store = FakeStore(job)
    logger = FakeLogger()
    adapter = FakeAdapter(json.dumps({
        "summary_zh": "接口返回了响应",
        "verdict": "success",
        "key_findings": [{"type": "endpoint", "value": "http://target/", "confidence": 0.9}],
        "suggested_next": ["继续验证"],
    }))
    curator = SugiliteCurator(
        None, logger, _config(), "proj_001",
        sandbox_provider=lambda pid, member: FakeSandbox(),
        adapter_factory=lambda config, name: adapter,
    )
    curator.store = store
    curator._curate(job)
    curator.close()

    assert store.saved is not None
    assert store.saved["source"] == "sugilite"
    assert store.saved["summary_zh"] == "接口返回了响应"
    assert any(item["type"] == "flag" for item in store.saved["key_findings"])
    assert any(item["value"] == "http://target/" for item in store.saved["key_findings"])
    assert "hunter2" not in store.saved["excerpt"]


def test_curator_falls_back_when_provider_fails():
    job = _job()
    store = FakeStore(job)

    class BrokenAdapter:
        def chat(self, *args, **kwargs):
            raise RuntimeError("provider down")

    curator = SugiliteCurator(
        None, FakeLogger(), _config(), "proj_001",
        sandbox_provider=lambda pid, member: FakeSandbox(),
        adapter_factory=lambda config, name: BrokenAdapter(),
    )
    curator.store = store
    curator._curate(job)
    curator.close()

    assert store.saved is not None
    assert store.saved["source"] == "deterministic"
    assert any(item["type"] == "flag" for item in store.saved["key_findings"])


def test_curator_deterministic_layer_is_always_available():
    digest = build_deterministic_digest(_job(), "flag{demo}\n", "")
    assert digest["verdict"] == "success"
    assert digest["key_findings"][0]["type"] == "flag"
