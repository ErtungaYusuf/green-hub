"""The hub against real Green nodes: a wallet that sends, compute bought from a miner
and checked, steering the node's mining, and web pages from other sites kept out."""

import json
import socket
import time
import urllib.error
import urllib.request

import pytest

from green import compute, hd, params
from green.chain import make_genesis
from green.cli import create_wallet
from green.compute import Provider
from green.crypto import Key
from green.node import Node
from green_hub.hub import Hub

PASSWORD = "a good password"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(cond, what, timeout=90):
    end = time.time() + timeout
    while not cond():
        assert time.time() < end, f"timed out waiting for {what}"
        time.sleep(0.1)


def call(ui, path, body=None, token=True, headers=None):
    h = {"Content-Type": "application/json", **({"X-UI-Token": ui.token} if token else {}), **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(ui.url + path, data=data, headers=h, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read()) if path != "/" else r.read()


def refused(ui, path, **kw) -> tuple[int, str]:
    with pytest.raises(urllib.error.HTTPError) as e:
        call(ui, path, **kw)
    return e.value.code, json.loads(e.value.read()).get("error", "")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """alice mines and sells compute; carol's node is where the hub points, with carol's
    wallet (which the genesis block paid) in the hub's folder."""
    monkeypatch.setattr(compute, "OFFER_RETRY", 0.5)
    folder = str(tmp_path / "carol")
    carol = create_wallet(folder, PASSWORD, hd.new_phrase(24))
    alice = Key.generate()
    genesis = make_genesis(carol.address, "hub")
    seller = Node(str(tmp_path / "s"), genesis, port=free_port(), miner_key=alice, log=lambda m: None)
    seller.start(mine=True)
    mine = Node(str(tmp_path / "b"), genesis, port=free_port(), miner_key=carol, log=lambda m: None,
                peers=[seller.p2p.address])
    mine.start()
    p = Provider(seller, alice, url="")
    p.start()
    hub = Hub(folder, mine.url, port=0)
    hub.start()
    try:
        wait_until(lambda: [o["provider"] for o in mine.p2p.offer_list()] == [alice.address], "alice's offer")
        yield hub, seller, mine, carol
    finally:
        hub.stop()
        p.stop()
        mine.stop()
        seller.stop()


def test_the_wallet_unlocks_sends_and_locks(world):
    hub, seller, mine, carol = world
    st = call(hub, "/api/state", token=False)
    assert st["exists"] and not st["unlocked"] and st["address"] == carol.address and st["balance"] > 0
    assert refused(hub, "/api/send", body={"to": Key.generate().address, "amount": "0.1"})[1] == "unlock the wallet first"
    assert refused(hub, "/api/unlock", body={"password": "wrong password"})[1] == "wrong password"
    assert call(hub, "/api/unlock", body={"password": PASSWORD})["address"] == carol.address
    friend = Key.generate().address
    assert refused(hub, "/api/send", body={"to": "nonsense", "amount": "1"})[0] == 400
    sent = call(hub, "/api/send", body={"to": friend, "amount": "0.25"})
    assert sent["fee"] == params.MIN_TX_FEE and len(sent["txid"]) == 64
    wait_until(lambda: seller.chain.state.balance(friend) == params.YAPRAK_PER_COIN // 4, "the payment")
    assert refused(hub, "/api/create", body={"password": PASSWORD})[1] == "this folder already holds a wallet"
    call(hub, "/api/lock", body={})
    assert not call(hub, "/api/state", token=False)["unlocked"]


def test_compute_is_bought_checked_and_the_channels_closed(world):
    hub, seller, mine, carol = world
    assert refused(hub, "/api/buy", body={"m": 64, "k": 64, "n": 64, "privacy": "none"})[1] == "unlock the wallet first"
    call(hub, "/api/unlock", body={"password": PASSWORD})
    assert call(hub, "/api/state", token=False)["offers"][0]["provider"] == seller.miner_key.address
    assert refused(hub, "/api/buy", body={"m": 0, "k": 64, "n": 64})[0] == 400
    assert refused(hub, "/api/buy", body={"m": 64, "k": 64, "n": 64, "privacy": "nonsense"})[0] == 400
    assert call(hub, "/api/buy", body={"m": 96, "k": 64, "n": 80, "privacy": "none"})["started"]
    assert refused(hub, "/api/buy", body={"m": 64, "k": 64, "n": 64, "privacy": "none"})[1] == "a purchase is already running"
    wait_until(lambda: not call(hub, "/api/state", token=False)["compute"]["busy"], "the purchase")
    c = call(hub, "/api/state", token=False)["compute"]
    assert c["error"] is None, c["error"]
    r = c["result"]
    assert r["shape"] == [96, 64, 80] and r["checked"] and r["matches_local"] and r["cost"] > 0
    assert c["channels"] >= 1 and c["paid"] >= r["cost"]
    call(hub, "/api/close", body={})
    wait_until(lambda: not call(hub, "/api/state", token=False)["compute"]["busy"], "closing")
    c = call(hub, "/api/state", token=False)["compute"]
    assert c["error"] is None and c["channels"] == 0


def test_the_hub_steers_the_nodes_mining(world):
    hub, seller, mine, carol = world
    st = call(hub, "/api/state", token=False)["mining"]
    assert st["here"] and not st["mining"] and st["miner"] == carol.address and st["share_factor"] == params.SHARE_FACTOR
    assert call(hub, "/api/mining", body={"on": True, "intensity": 40})["mining"]
    assert mine.mining and mine.intensity == 40
    wait_until(lambda: call(hub, "/api/state", token=False)["mining"]["units_per_second"] > 0, "mining speed")
    call(hub, "/api/mining", body={"on": False})
    assert not mine.mining
    assert refused(hub, "/api/mining", body={})[0] == 400


def test_web_pages_cannot_use_the_hub(world):
    hub, seller, mine, carol = world
    assert hub.token.encode() in call(hub, "/", token=False)
    assert refused(hub, "/api/unlock", body={"password": PASSWORD}, token=False)[0] == 403
    assert refused(hub, "/api/mining", body={"on": True}, token=False)[0] == 403
    assert refused(hub, "/", token=False, headers={"Host": "evil.example"})[0] == 403
    assert refused(hub, "/api/state", token=False, headers={"Host": "evil.example:8630"})[0] == 403
    assert not mine.mining


def test_an_unencrypted_wallet_needs_no_password(world, tmp_path):
    hub, seller, mine, carol = world
    folder = str(tmp_path / "plain")
    key = create_wallet(folder, None)
    plain = Hub(folder, mine.url, port=0)
    plain.start()
    try:
        st = call(plain, "/api/state", token=False)
        assert st["unlocked"] and not st["encrypted"] and st["address"] == key.address
    finally:
        plain.stop()
