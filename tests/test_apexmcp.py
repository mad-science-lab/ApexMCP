import pytest

from apexmcp import config, tools
from apexmcp.model import parse_status
from apexmcp.redact import redact
from apexmcp.tools import ToolError, compute_duty

from .conftest import STATUS


# -- config -----------------------------------------------------------------

def test_host_required():
    with pytest.raises(config.ConfigError):
        config.load({})


def test_writes_off_by_default():
    s = config.load({"APEX_HOST": "reef.local"})
    assert not s.allow_write and not s.allow_program_write


def test_program_write_requires_write():
    with pytest.raises(config.ConfigError):
        config.load({"APEX_HOST": "x", "APEX_ALLOW_PROGRAM_WRITE": "true", "APEX_PASSWORD": "p"})


def test_write_requires_password():
    with pytest.raises(config.ConfigError):
        config.load({"APEX_HOST": "x", "APEX_ALLOW_WRITE": "true"})


def test_password_file(tmp_path):
    f = tmp_path / "pw"
    f.write_text("secret\n")
    s = config.load({"APEX_HOST": "x", "APEX_PASSWORD_FILE": str(f)})
    assert s.password == "secret"


def test_password_file_with_username_line(tmp_path):
    f = tmp_path / "pw"
    f.write_text("﻿keith\nsecret\n", encoding="utf-8")
    s = config.load({"APEX_HOST": "x", "APEX_PASSWORD_FILE": str(f)})
    assert (s.username, s.password) == ("keith", "secret")


@pytest.mark.parametrize("text", [
    "Username: keith\nPassword: sec ret\n",
    "password=sec ret\nuser = keith\n",
])
def test_password_file_labels(tmp_path, text):
    f = tmp_path / "pw"
    f.write_text(text)
    s = config.load({"APEX_HOST": "x", "APEX_PASSWORD_FILE": str(f)})
    assert (s.username, s.password) == ("keith", "sec ret")


def test_password_file_label_typo(tmp_path):
    f = tmp_path / "pw"
    f.write_text("usernane:admin\npasswrod:secret\n")
    s = config.load({"APEX_HOST": "x", "APEX_PASSWORD_FILE": str(f)})
    assert (s.username, s.password) == ("admin", "secret")


def test_password_file_single_labeled_line(tmp_path):
    f = tmp_path / "pw"
    f.write_text("password: secret\n")
    assert config.load({"APEX_HOST": "x", "APEX_PASSWORD_FILE": str(f)}).password == "secret"


def test_password_file_too_many_lines(tmp_path):
    f = tmp_path / "pw"
    f.write_text("a\nb\nc\n")
    with pytest.raises(config.ConfigError):
        config.load({"APEX_HOST": "x", "APEX_PASSWORD_FILE": str(f)})


def test_protected_is_case_insensitive():
    s = config.load({"APEX_HOST": "x", "APEX_PROTECTED_OUTLETS": "ReturnPump, 2_8"})
    assert s.is_protected("4_1", "returnpump") and s.is_protected("2_8", "Heater")


# -- model / redaction --------------------------------------------------------

def test_parse_status_modes():
    snap = parse_status(STATUS)
    by = {o.did: o for o in snap.outlets}
    assert by["2_7"].mode == "AUTO" and not by["2_7"].energized
    assert by["2_8"].mode == "AUTO" and by["2_8"].energized
    assert by["2_4"].mode == "ON"
    assert by["4_1"].percent == 90


def test_feed_garbage_is_ignored():
    bad = {**STATUS, "feed": {"name": 256, "active": 61438}}
    feed = parse_status(bad).feed
    assert feed.cycle is None and feed.seconds_remaining == 0 and feed.raw_name == 256


def test_redact_nconf_and_linkkey():
    out = redact({"nconf": {"password": "x"}, "link": {"linkKey": "k", "ok": 1}, "a": [{"Password": "p"}]})
    assert out["nconf"] == "[redacted]"
    assert out["link"] == {"linkKey": "[redacted]", "ok": 1}
    assert out["a"][0]["Password"] == "[redacted]"


async def test_system_info_has_no_credentials(client):
    assert "SECRET1" not in str(await tools.system_info(client))


async def test_config_section_redacts_and_rejects_nconf(client):
    with pytest.raises(ToolError):
        await tools.config_section(client, "nconf")
    assert "hunter2" not in str(await tools.config_section(client, "misc"))


# -- reads --------------------------------------------------------------------

async def test_module_health_error_pct(client):
    health = await tools.module_health(client)
    assert health[0]["error_pct"] == 0.5 and health[1]["error_pct"] == 0.0


async def test_temp_unit_guess(client):
    temp = (await tools.probes(client, "Temp"))[0]
    assert temp["unit_guess"] == "F"


def test_duty_cycle_math():
    # window 0..1000; ON at 100, OFF at 300, ON at 600, still on at 1000
    ev = [{"date": 100, "value": "ON"}, {"date": 300, "value": "OFF"}, {"date": 600, "value": "ON"}]
    out = compute_duty(ev, 0, 1000, currently_on=True)
    assert out["on_seconds"] == 200 + 400 and out["runs"] == 2
    assert out["mean_run_seconds"] == 200.0


def test_duty_cycle_started_on():
    ev = [{"date": 250, "value": "OFF"}]
    out = compute_duty(ev, 0, 1000, currently_on=False)
    assert out["on_seconds"] == 250 and out["runs"] == 0


def test_duty_cycle_no_events_uses_current_state():
    assert compute_duty([], 0, 100, True)["duty_pct"] == 100.0


async def test_olog_envelope_tolerance(client):
    client.olog = {"olog": {"record": [{"date": 5, "did": "2_8", "name": "Heater200", "value": "ON"}]}}
    out = await tools.outlet_events(client, "Heater200", 1)
    assert out["count"] == 1 and out["events"][0]["state"] == "ON"


async def test_probe_stats_drift(client):
    day = 86400
    rec = []
    for d in range(6):  # six whole days, pH rising 0.1/day, flat inside each day
        for h in range(0, 24, 6):
            rec.append({"date": d * day + h * 3600, "data": [
                {"name": "pH", "did": "base_pH", "type": "pH", "value": str(7.5 + 0.1 * d)}]})
    client.ilog = {"ilog": {"timezone": "0.00", "record": rec}}
    out = await tools.probe_stats(client, "pH", 7)
    assert out["drift_per_day"] == pytest.approx(0.1, abs=1e-3)


# -- writes -------------------------------------------------------------------

async def test_set_outlet_blocked_when_read_only(client, ro):
    with pytest.raises(ToolError, match="disabled"):
        await tools.set_outlet(client, ro, "Heater50w", "OFF")
    assert not client.calls


async def test_set_outlet_by_name_and_verifies(client, rw):
    out = await tools.set_outlet(client, rw, "heater50w", "ON")
    assert out["applied"] and out["after"]["mode"] == "ON"
    assert ("set_outlet", "2_7", "ON") in client.calls


async def test_protected_outlet_refuses_manual_but_allows_auto(client, rw):
    with pytest.raises(ToolError, match="protected"):
        await tools.set_outlet(client, rw, "Heater200", "OFF")
    out = await tools.set_outlet(client, rw, "Heater200", "AUTO")
    assert out["applied"]


async def test_unknown_outlet_lists_choices(client, rw):
    with pytest.raises(ToolError, match="Heater50w"):
        await tools.set_outlet(client, rw, "nope", "ON")


async def test_cancel_feed_ignored_when_idle(client, rw):
    out = await tools.cancel_feed(client, rw)
    assert out["cancelled"] is False and ("cancel_feed",) not in client.calls


async def test_feed_start_then_cancel(client, rw):
    await tools.start_feed(client, rw, "b")
    assert ("start_feed", 2) in client.calls
    assert (await tools.cancel_feed(client, rw))["cancelled"] is True


async def test_cancel_feed_ignored_when_feed_block_corrupt(client, rw):
    client.status["feed"] = {"name": 256, "active": 61438}
    assert (await tools.cancel_feed(client, rw))["cancelled"] is False


async def test_program_preview_does_not_write(client, rw):
    out = await tools.update_program(client, rw, "Heater50w", "Fallback OFF\nSet OFF")
    assert out["applied"] is False and "-Set ON" in out["diff"]
    assert not any(c[0] == "put_outlet_config" for c in client.calls)


async def test_program_confirm_backs_up_and_verifies(client, rw):
    out = await tools.update_program(client, rw, "Heater50w", "Fallback OFF\nSet OFF", confirm=True)
    assert out["applied"] is True
    assert (rw.backup_dir / out["backup"].split("\\")[-1].split("/")[-1]).exists()
    assert client.config["oconf"][0]["prog"] == "Fallback OFF\nSet OFF\n"


async def test_program_rejects_unknown_directive(client, rw):
    with pytest.raises(ToolError, match="unknown directive"):
        await tools.update_program(client, rw, "Heater50w", "Explode ON", confirm=True)


async def test_program_protected_outlet_refused(client, rw):
    with pytest.raises(ToolError, match="protected"):
        await tools.update_program(client, rw, "Heater200", "Set ON", confirm=True)


async def test_program_edit_needs_its_own_flag(client, rw):
    from dataclasses import replace
    with pytest.raises(ToolError, match="PROGRAM_WRITE"):
        await tools.update_program(client, replace(rw, allow_program_write=False), "Heater50w", "Set ON")
