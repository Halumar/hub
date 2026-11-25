"""Unique Bonjour announcement per hub instance.

Instance name ``<home title> (<short id>)``, host ``hub-<short id>.local.``
with collision fallback, TXT ``iid`` + ``fp`` from one registration.
"""

import asyncio
import uuid

import pytest

from brand import BRAND_NAME
import services.mdns as mdns
from services import tls


class _FakeResolver:
    taken: dict[str, list[str]] = {}

    def __init__(self, host):
        self.host = host

    async def async_request(self, zc, timeout):
        return self.host in self.taken

    def parsed_addresses(self):
        return self.taken.get(self.host, [])


class _FakeInfo:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.name = kwargs["name"]
        self.addresses = kwargs["addresses"]


class _FakeAioZC:
    instances: list["_FakeAioZC"] = []

    def __init__(self, **kwargs):
        self.zeroconf = object()
        self.registered = []
        self.updated = []
        self.rescans = 0
        self.unregistered = []
        self.closed = False
        _FakeAioZC.instances.append(self)

    async def async_register_service(self, info, allow_name_change=False):
        assert allow_name_change is True
        self.registered.append(info)

    async def async_update_interfaces(self):
        self.rescans += 1

    async def async_update_service(self, info):
        self.updated.append((self.rescans, list(info.addresses)))

    async def async_unregister_service(self, info):
        self.unregistered.append(info)

    async def async_close(self):
        self.closed = True


class _FakeDB:
    def __init__(self, settings):
        self.settings = settings

    async def get_setting(self, key):
        return self.settings.get(key)


@pytest.fixture
def fake_zc(monkeypatch, tmp_path):
    _FakeAioZC.instances.clear()
    _FakeResolver.taken = {}
    monkeypatch.setattr(mdns, "AsyncZeroconf", _FakeAioZC)
    monkeypatch.setattr(mdns, "AsyncServiceInfo", _FakeInfo)
    monkeypatch.setattr(mdns, "AddressResolverIPv4", _FakeResolver)
    monkeypatch.setattr(mdns, "_get_local_ip", lambda: "192.0.2.20")
    monkeypatch.setattr(tls, "get_fingerprint", lambda *a, **k: "ab" * 32)
    path = tmp_path / "instance_id"
    yield path


async def _start(path, db):
    await mdns.start_mdns(
        version="2026.12.0", port=8099, db=db, instance_id_path=str(path)
    )
    await asyncio.wait_for(mdns._register_task, 5)
    return _FakeAioZC.instances[-1]


async def test_unique_name_host_and_txt(fake_zc):
    try:
        zc = await _start(fake_zc, _FakeDB({"home_title": "Zuhause"}))
        info = zc.registered[0].kwargs
        iid = fake_zc.read_text().strip()
        sid = iid.replace("-", "")[:6]
        assert info["name"] == f"Zuhause ({sid})._halumar._tcp.local."
        assert info["server"] == f"hub-{sid}.local."
        assert info["properties"]["iid"] == iid
        assert info["properties"]["fp"] == "ab" * 32
        assert info["properties"]["version"] == "2026.12.0"
    finally:
        await mdns.stop_mdns()
    assert zc.unregistered and zc.closed


async def _until(check, tries=200):
    for _ in range(tries):
        if check():
            return True
        await asyncio.sleep(0.01)
    return False


async def test_announcement_follows_the_address(fake_zc, monkeypatch):
    """No address yet -> wait for one; a DHCP change is announced."""
    import socket

    ip = {"now": "127.0.0.1"}
    monkeypatch.setattr(mdns, "_get_local_ip", lambda: ip["now"])
    monkeypatch.setattr(mdns, "ADDRESS_CHECK_SECONDS", 0.01)
    try:
        await mdns.start_mdns(
            version="2026.12.0",
            port=8099,
            db=_FakeDB({}),
            instance_id_path=str(fake_zc),
        )
        zc = _FakeAioZC.instances[-1]
        await asyncio.sleep(0.05)
        assert zc.registered == []  # never announced as 127.0.0.1
        ip["now"] = "192.0.2.20"
        assert await _until(lambda: zc.registered)
        assert zc.registered[0].addresses == [socket.inet_aton("192.0.2.20")]
        # the sockets were bound before the address existed: rescanned first
        assert zc.rescans == 1
        ip["now"] = "192.0.2.77"
        assert await _until(lambda: zc.updated)
        # rescanned again before the new address is announced
        assert zc.updated == [(2, [socket.inet_aton("192.0.2.77")])]
        ip["now"] = "127.0.0.1"  # network gone for a moment: keep the last one
        await asyncio.sleep(0.05)
        assert len(zc.updated) == 1
    finally:
        await mdns.stop_mdns()
    assert zc.unregistered and zc.closed


async def test_instance_id_is_stable(fake_zc):
    first = mdns.load_instance_id(str(fake_zc))
    assert mdns.load_instance_id(str(fake_zc)) == first
    assert str(uuid.UUID(first)) == first


async def test_unwritable_instance_id_stays_the_same_and_is_retried(tmp_path, caplog):
    """A write that fails keeps one id for the life of the process (the
    status must not name a new hub on every poll), logs once, and retries."""
    folder = tmp_path / "missing"
    path = str(folder / "instance_id")
    with caplog.at_level("WARNING", logger=mdns.logger.name):
        first = mdns.load_instance_id(path)
        assert mdns.load_instance_id(path) == first
        assert mdns.load_instance_id(path) == first
    assert caplog.text.count("Could not persist instance id") == 1
    folder.mkdir()
    assert mdns.load_instance_id(path) == first
    assert (folder / "instance_id").read_text().strip() == first


async def test_corrupt_instance_id_is_replaced(fake_zc):
    fake_zc.write_text("not-a-uuid")
    iid = mdns.load_instance_id(str(fake_zc))
    assert str(uuid.UUID(iid)) == iid


async def test_host_collision_resolved(fake_zc):
    iid = mdns.load_instance_id(str(fake_zc))
    sid = mdns.short_id(iid)
    _FakeResolver.taken = {
        f"hub-{sid}.local.": ["192.0.2.100"],
        f"hub-{sid}-2.local.": ["192.0.2.20"],  # our own address: free
    }
    try:
        zc = await _start(fake_zc, _FakeDB({}))
        assert zc.registered[0].kwargs["server"] == f"hub-{sid}-2.local."
    finally:
        await mdns.stop_mdns()


async def test_fallback_title_and_label_limit(fake_zc):
    assert mdns.instance_label(None, "abc123") == f"{BRAND_NAME} (abc123)"
    assert mdns.instance_label("Haus.Nord\n", "abc123") == "Haus Nord (abc123)"
    long = mdns.instance_label("Ä" * 80, "abc123")
    assert len(long.encode()) <= mdns.MAX_INSTANCE_LABEL
    assert long.endswith(" (abc123)")


async def test_title_from_location_name_without_home_title(fake_zc):
    try:
        zc = await _start(fake_zc, _FakeDB({"ha_location_name": "Ferienhaus"}))
        assert zc.registered[0].name.startswith("Ferienhaus (")
    finally:
        await mdns.stop_mdns()


async def test_title_read_only_from_db_file(fake_zc, tmp_path, monkeypatch, db):
    await db.set_setting("home_title", "Wohnung")
    monkeypatch.setenv("HUB_DB_PATH", db.db_path)
    assert await mdns._home_title(None) == "Wohnung"


async def test_two_instances_differ(tmp_path):
    a = mdns.load_instance_id(str(tmp_path / "a"))
    b = mdns.load_instance_id(str(tmp_path / "b"))
    assert mdns.instance_label("Home", mdns.short_id(a)) != mdns.instance_label(
        "Home", mdns.short_id(b)
    )
    assert mdns.host_name(mdns.short_id(a)) != mdns.host_name(mdns.short_id(b))
