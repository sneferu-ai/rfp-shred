"""Public surface and baseline browser security headers."""


def test_framework_documentation_surfaces_are_disabled(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_dynamic_pages_are_private_and_framing_is_denied(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_static_assets_have_short_public_cache(client):
    response = client.get("/static/style.css")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=3600"
