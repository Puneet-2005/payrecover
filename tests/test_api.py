from fastapi.testclient import TestClient

from payrecover.api.main import app

client = TestClient(app)


def test_health():
    assert client.get("/health").json()["status"] == "ok"


def test_failed_event_requires_error_code():
    response = client.post("/v1/payments/events", json={"payment_id":"pay_1", "merchant_id":"mer_1",
        "method":"upi", "issuer":"bank_x", "provider":"phonepe", "amount_paise":349900,
        "status":"failed", "latency_ms":100})
    assert response.status_code == 422
