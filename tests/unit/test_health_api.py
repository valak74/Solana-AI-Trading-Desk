import httpx
from fastapi import FastAPI

from backend.api.health import router
from backend.monitoring.readiness import Check, ReadinessProbe


def app_with(checks: dict[str, Check]) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.readiness = ReadinessProbe(checks)
    return app


def const(value: bool) -> Check:
    async def check() -> bool:
        return value

    return check


async def get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def test_live_is_always_ok() -> None:
    response = await get(app_with({"database": const(False)}), "/health/live")
    assert response.status_code == 200


async def test_ready_ok_when_all_checks_pass() -> None:
    response = await get(
        app_with({"database": const(True), "schema": const(True)}), "/health/ready"
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": True, "schema": True}}


async def test_ready_503_when_a_check_fails() -> None:
    response = await get(
        app_with({"database": const(True), "schema": const(False)}), "/health/ready"
    )
    assert response.status_code == 503
    assert response.json()["checks"]["schema"] is False


async def test_ready_503_when_a_check_raises() -> None:
    async def boom() -> bool:
        raise ConnectionError("db down")

    response = await get(app_with({"database": boom}), "/health/ready")
    assert response.status_code == 503


async def test_no_checks_means_not_ready() -> None:
    response = await get(app_with({}), "/health/ready")
    assert response.status_code == 503
