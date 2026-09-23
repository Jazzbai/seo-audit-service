from types import SimpleNamespace

import pytest

from scripts.split_api_firewall import configure, rule, verify_local_address


@pytest.mark.parametrize("bind,proxy,port", [
    ("0.0.0.0", "10.20.0.13", 18001),
    ("10.20.0.10", "0.0.0.0", 18001),
    ("127.0.0.1", "10.20.0.13", 18001),
    ("10.20.0.10", "8.8.8.8", 18001),
    ("10.20.0.10", "10.20.0.10", 18001),
    ("10.20.0.10", "10.20.0.13", 22),
    ("10.20.0.10", "10.20.0.13", 65536),
    ("::1", "10.20.0.13", 18001),
])
def test_unsafe_scope_is_rejected_before_any_command(bind, proxy, port):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid input must not reach iptables")
    with pytest.raises(ValueError):
        configure("apply", bind, proxy, port, run=forbidden)


def execute(action, outcomes):
    calls = []
    results = iter(outcomes)
    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["timeout"] == 10
        return SimpleNamespace(returncode=next(results))
    result = configure(action, "10.20.0.10", "10.20.0.13", 18001, run=run)
    return result, calls


def test_apply_matches_one_original_destination_before_docker_nat():
    result, calls = execute("apply", [1, 0, 0])
    assert result
    assert [command[5] for command in calls] == ["-C", "-I", "-C"]
    for command in calls:
        assert command[:5] == ["iptables", "-w", "5", "-t", "raw"]
        assert command[6] == "PREROUTING"
        assert command[7:] == rule("10.20.0.10", "10.20.0.13", 18001)
        assert command[7:17] == ["-p", "tcp", "-d", "10.20.0.10/32", "--dport", "18001", "!", "-s", "10.20.0.13/32", "-m"]
        assert "-F" not in command and "-P" not in command and "ACCEPT" not in command


def test_apply_is_idempotent_and_remove_targets_the_exact_rule():
    assert [c[5] for c in execute("apply", [0, 0])[1]] == ["-C", "-C"]
    assert [c[5] for c in execute("remove", [0, 0, 1])[1]] == ["-C", "-D", "-C"]
    assert [c[5] for c in execute("remove", [1, 1])[1]] == ["-C", "-C"]
    assert execute("check", [0])[0] is True
    assert execute("check", [1])[0] is False


@pytest.mark.parametrize("outcomes", [[2], [1, 2], [1, 0, 1]])
def test_inspection_write_or_verification_errors_fail_closed(outcomes):
    with pytest.raises(RuntimeError):
        execute("apply", outcomes)


def test_address_must_belong_to_the_backend_host():
    def run(command, **kwargs):
        assert command == ["ip", "-j", "-4", "address", "show"]
        return SimpleNamespace(returncode=0, stdout='[{"addr_info":[{"local":"10.20.0.10"}]}]')
    verify_local_address("10.20.0.10", run=run)
    with pytest.raises(ValueError):
        verify_local_address("10.20.0.13", run=run)
