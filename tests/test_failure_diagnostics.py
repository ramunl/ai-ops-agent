"""Safe diagnostic extraction from untrusted service output."""

from ai_ops_agent.deployment.failure_diagnostics import parse_diagnostics


def test_prestart_failures_without_traceback():
    result = parse_diagnostics(
        "Active: failed (Result: exit-code)\nProcess: 12 ExecStart=/secret --token=SECRET (code=exited, status=217/USER)",
        "systemd[1]: Failed at step USER spawning /secret: No such process\n"
        "python[12]: /usr/local/sbin/ai-coding-runner: No such file or directory",
    )
    assert "systemd: 217/USER" in result
    assert "missing executable: ai-coding-runner" in result
    assert "SECRET" not in repr(result)
    assert "/secret" not in repr(result)


def test_traceback_excludes_source_arguments_and_exception_messages():
    journal = """python[12]: Traceback (most recent call last):
python[12]:   File "/private/SECRET.py", line 42, in SECRET
python[12]:     run(["command", "--token", "SECRET"])
python[12]: ValueError: SECRET token=abc https://user:password@host
python[12]: UnknownError: SECRET
"""
    result = parse_diagnostics("", journal)
    assert "Python traceback (source omitted)" in result
    assert "Python exception: ValueError" in result
    assert "SECRET" not in repr(result)
    assert "password" not in repr(result)
    assert "command" not in repr(result)
    assert "UnknownError" not in repr(result)


def test_bounds_unknown_payloads_and_repeated_tracebacks():
    assert parse_diagnostics("secret=abc", '{"token":"secret"}') == ()
    result = parse_diagnostics(
        "", ("Traceback (most recent call last):\nValueError: SECRET\n" * 10000)
    )
    assert len(result) == len(set(result))
    assert sum(map(len, result)) <= 1024
    assert "diagnostic input truncated" in result
    assert "SECRET" not in repr(result)
