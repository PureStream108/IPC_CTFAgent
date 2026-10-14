import requests
import time

base = "http://127.0.0.1:8000"
workflow = {
    "name": "Deterministic E2E Competition",
    "competition_id": "e2e-competition",
    "team_id": "e2e-team",
    "allow_private_networks": True,
    "challenges": {
        "platform": "http_json",
        "list_url": "http://e2e-platform:8080/api/challenges",
        "list_path": "data",
        "id_field": "id",
        "title_field": "name",
        "category_field": "category",
        "description_field": "description",
        "attachments_field": "files",
    },
    "submit": {
        "url": "http://e2e-platform:8080/api/submit",
        "method": "POST",
        "json_template": {"challenge_id": "{{external_id}}", "flag": "{{flag}}"},
        "success_path": "verdict",
        "success_values": ["correct"],
        "wrong_values": ["wrong"],
    },
}
created = requests.post(f"{base}/api/ops/workflows", json={"workflow": workflow})
print("create", created.status_code, created.text)
workflow_id = created.json()["id"]
confirmed = requests.post(
    f"{base}/api/ops/workflows/{workflow_id}/confirm",
    json={"confirmation_phrase": f"CONFIRM WORKFLOW {workflow_id}"},
)
print("confirm", confirmed.status_code, confirmed.text)
print("preflight", requests.post(f"{base}/api/workflows/{workflow_id}/preflight").text)
started = requests.post(
    f"{base}/api/workflows/{workflow_id}/start",
    json={"idempotency_key": f"probe-start-{int(time.time())}"},
)
print("start", started.status_code, started.text[:2000])
payload = started.json()
run_id = payload["run"]["id"]
assignment = None
for _ in range(30):
    snapshot = requests.get(f"{base}/api/runs/{run_id}").json()
    challenge = snapshot["challenges"][0]
    if challenge["assignments"]:
        assignment = challenge["assignments"][0]
        break
    time.sleep(0.5)
if assignment:
    response = requests.post(
        f"{base}/api/challenges/{challenge['id']}/candidates",
        json={"run_id": run_id, "session_id": assignment["session_id"],
              "flag": "flag{e2e-deterministic}",
              "evidence": "deterministic evidence",
              "instance_generation": challenge["instance_generation"]},
    )
    print("candidate", response.status_code, response.text)
    for _ in range(30):
        snapshot = requests.get(f"{base}/api/runs/{run_id}").json()
        if snapshot["challenges"][0]["state"] == "solved":
            print("solved", run_id)
            break
        time.sleep(0.5)
