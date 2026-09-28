from services.sentry import scrub_event


def test_sentry_scrubber_removes_auth_and_meeting_body():
    event = {
        "request": {
            "headers": {
                "Authorization": "Bearer secret",
                "Cookie": "session=secret",
                "X-Auth-Request-Email": "person@example.com",
                "Content-Type": "application/json",
            },
            "cookies": {"session": "secret"},
            "data": {"transcript": "private meeting transcript"},
        },
        "user": {"email": "person@example.com"},
    }

    scrubbed = scrub_event(event, {})
    assert scrubbed["request"]["headers"]["Authorization"] == "[Filtered]"
    assert scrubbed["request"]["headers"]["Cookie"] == "[Filtered]"
    assert scrubbed["request"]["headers"]["X-Auth-Request-Email"] == "[Filtered]"
    assert "cookies" not in scrubbed["request"]
    assert "data" not in scrubbed["request"]
    assert "user" not in scrubbed
