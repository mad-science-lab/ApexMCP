import copy
from pathlib import Path

import pytest

from apexmcp import tools
from apexmcp.config import Settings

STATUS = {
    "system": {"hostname": "Reef", "serial": "AC6L:00000", "type": "AC6L",
               "software": "5.15L_6B26", "hardware": "1.0"},
    "modules": [
        {"abaddr": 1, "hwtype": "FMM", "present": True, "swstat": "OK", "pcount": 1000, "perror": 5},
        {"abaddr": 2, "hwtype": "EB832", "present": True, "swstat": "OK", "pcount": 0, "perror": 0},
    ],
    "nstat": {"ipaddr": "192.168.1.9", "quality": 90, "strength": -50, "link": True},
    "link": {"linkKey": "SECRET1", "linkState": 3},
    "feed": {"name": 0, "active": 0},
    "power": {"failed": 1, "restored": 2},
    "inputs": [
        {"did": "base_Temp", "type": "Temp", "name": "Tmp", "value": 77.0},
        {"did": "base_pH", "type": "pH", "name": "pH", "value": 7.77},
        {"did": "1_I1", "type": "digital", "name": "Lvl-L", "value": 200},
    ],
    "outputs": [
        {"did": "2_7", "name": "Heater50w", "type": "outlet", "status": ["AOF", "", "OK", ""]},
        {"did": "2_8", "name": "Heater200", "type": "outlet", "status": ["AON", "", "OK", ""]},
        {"did": "2_4", "name": "SecondPwr", "type": "outlet", "status": ["ON", "", "OK", ""]},
        {"did": "4_1", "name": "ReturnPump", "type": "iotaPump|Sicce|Syncra",
         "status": ["AON", "90", "OK", ""]},
    ],
}

CONFIG = {
    "nconf": {"password": "hunter2", "ssid": "x"},
    "misc": {"logInterval": 5},
    "oconf": [
        {"did": "2_7", "name": "Heater50w", "type": "outlet", "prog": "Fallback OFF\nSet ON\n"},
        {"did": "2_8", "name": "Heater200", "type": "outlet", "prog": "Fallback OFF\nIf Tmp < 77 Then ON\n"},
    ],
}


class FakeClient:
    def __init__(self):
        self.status = copy.deepcopy(STATUS)
        self.config = copy.deepcopy(CONFIG)
        self.calls: list[tuple] = []
        self.ilog = {"ilog": {"timezone": "0.00", "record": []}}
        self.olog: object = []

    async def get_status(self, *, fresh=False):
        return self.status

    async def get_config(self):
        return self.config

    async def get_ilog(self, days, sdate):
        self.calls.append(("ilog", days, sdate))
        return self.ilog

    async def get_olog(self, days, sdate):
        self.calls.append(("olog", days, sdate))
        return self.olog

    async def set_outlet(self, did, mode):
        self.calls.append(("set_outlet", did, mode))
        for o in self.status["outputs"]:
            if o["did"] == did:
                o["status"][0] = mode if mode != "AUTO" else "AON"

    async def start_feed(self, cycle):
        self.calls.append(("start_feed", cycle))
        self.status["feed"] = {"name": cycle, "active": 300}

    async def cancel_feed(self):
        self.calls.append(("cancel_feed",))
        self.status["feed"] = {"name": 0, "active": 0}

    async def put_outlet_config(self, outlet):
        self.calls.append(("put_outlet_config", outlet["did"]))
        self.config["oconf"] = [outlet if o["did"] == outlet["did"] else o for o in self.config["oconf"]]


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(tools, "POST_WRITE_DELAY", 0)


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def rw(tmp_path: Path):
    return Settings(host="http://reef.local", password="x", allow_write=True,
                    allow_program_write=True, backup_dir=tmp_path / "bk",
                    protected=frozenset({"heater200"}))


@pytest.fixture
def ro():
    return Settings(host="http://reef.local")
