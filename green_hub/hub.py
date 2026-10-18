"""Green Hub: one page in the browser for everything a person does with Green.

    python -m green_hub.hub [--data FOLDER] [--node URL] [--port 8630]

- Wallet: create a wallet (a 24-word recovery phrase, encrypted with a password) or
  restore one, see the balance and history, send coins.
- Compute: buy matrix products from the network's miners through payment channels,
  each result checked on arrival, the data hidden from them (split or secret-shared);
  post a job to the on-chain market.
- Mining: start and stop the node's mining, set its intensity, GPU or CPU, and watch
  the shares and blocks found and what they earn.

The key never leaves this program: transactions are signed here and only signed
transactions go to the node. Mining is steered on the node itself, so it needs the
node on this computer; run it with the same folder (`grn --data FOLDER node --mine
--gpu`) and it mines for this wallet.
"""

import argparse
import json
import os
import threading
import time
import webbrowser
from collections import deque
from urllib.parse import urlparse

from green import hd, params, tx as txmod
from green.app import App, Account, AppError
from green.cli import create_wallet, wallet_path
from green.crypto import Key, WrongPassword, decrypt_secret, is_address
from green.node import http_get, http_post
from green.private import MODES

from .server import LocalUI, UIError

MIN_PASSWORD = 8
MAX_HUB_DIM = 2048                       # products bought from the page (the network takes up to 4096)


def _read(data: str) -> dict | None:
    try:
        with open(wallet_path(data), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def unlock(w: dict, password: str) -> Key:
    """The wallet's key from its file (and password, if it is encrypted)."""
    try:
        if "path" in w:                                  # made from a recovery phrase
            secret = ({"phrase": w["phrase"], "passphrase": w.get("passphrase", "")} if "phrase" in w
                      else json.loads(bytes.fromhex(decrypt_secret(w["encrypted_phrase"], password))))
            key = hd.key_from_phrase(secret["phrase"], secret["passphrase"], w["path"])
        elif "secret" in w:
            key = Key.from_hex(w["secret"])
        else:
            key = Key.from_hex(decrypt_secret(w["encrypted"], password))
    except WrongPassword:
        raise UIError("wrong password") from None
    if key.address != w["address"]:
        raise UIError("the wallet file is corrupt: the address does not match the key")
    return key


def is_local(url: str) -> bool:
    return (urlparse(url).hostname or "") in ("127.0.0.1", "localhost", "::1")


class Hub(LocalUI):
    def __init__(self, data: str, node: str, port: int = 8630, allow_local: bool | None = None):
        super().__init__("hub.html", port, "Green Hub")
        self.data, self.node = data, node.rstrip("/")
        # Providers on this computer's addresses are only used when the node is here too
        # (a local network); on a real network they would be someone pointing us home.
        self.allow_local = is_local(self.node) if allow_local is None else allow_local
        self.key: Key | None = None
        self.app: App | None = None                     # buys compute for the unlocked key
        self.lock = threading.Lock()
        self.samples: deque = deque(maxlen=720)          # (time, balance): the miner's last hour at 5 s
        self.compute = {"busy": False, "what": None, "result": None, "error": None}
        w = _read(data)
        if w is not None and ("phrase" in w or "secret" in w):    # not encrypted (a local network's): no password
            self._use(unlock(w, ""))

    # -- the key ------------------------------------------------------------------------

    def _use(self, key: Key | None) -> None:
        with self.lock:
            old, self.key = self.app, key
            self.app = (App([Account("me", "this wallet", key, self.node, self.data)], buyer="me",
                            allow_local=self.allow_local, log=lambda m: None) if key else None)
        if old is not None:                              # its channels: providers claim what was used
            threading.Thread(target=old.stop, daemon=True, name="hub-close").start()

    def stop(self) -> None:
        if self.app is not None:
            self.app.stop()
        super().stop()

    # -- reading ------------------------------------------------------------------------

    def get_state(self, q: dict) -> dict:
        w = _read(self.data)
        out = {"exists": w is not None, "unlocked": self.key is not None, "folder": os.path.abspath(self.data),
               "node": self.node, "modes": list(MODES) + ["none"], "max_dim": MAX_HUB_DIM}
        try:
            st = http_get(f"{self.node}/status", timeout=5)
        except OSError as e:
            out.update(online=False, error=str(e))
            return out
        out.update(online=True, height=st["height"], peers=len(st["peers"]), mining=self._mining(st))
        if w is None:
            return out
        addr = w["address"]
        out.update(address=addr, encrypted="phrase" not in w and "secret" not in w)
        try:
            acct = http_get(f"{self.node}/account/{addr}", timeout=5)
            fee = http_get(f"{self.node}/v1/fee?blocks=2", timeout=5)
            out.update(balance=acct["balance"], pending=acct["pending_balance"], transfer_fee=fee["transfer_fee"],
                       history=self._history(addr))
            offers = http_get(f"{self.node}/providers", timeout=5)
            out["offers"] = [{"provider": o["provider"], "price": o["price"], "max_dim": o["max_dim"]} for o in offers[:8]]
        except OSError as e:
            out["error"] = str(e)
        app = self.app
        cl = app.accounts["me"].cluster if app else None
        out["compute"] = dict(self.compute, channels=len(cl.channels) if cl is not None else 0,
                              paid=App.paid(app.accounts["me"]) if app else 0)
        return out

    def _mining(self, st: dict) -> dict:
        m = {"here": is_local(self.node), "mining": st["mining"], "gpu": st.get("gpu"),
             "intensity": st.get("intensity", 100), "units_per_second": st["units_per_second"],
             "units_per_block": st["units_per_block"], "miner": st["miner"],
             "blocks_found": st["stats"].get("blocks_found", 0), "shares_found": st["stats"].get("shares_found", 0),
             "share_factor": params.SHARE_FACTOR, "seconds_per_block": params.TARGET_BLOCK_SECONDS}
        if st["units_per_block"]:                     # shares per hour from our part of the work
            m["expected_shares_per_hour"] = round(st["units_per_second"] * 3600 / st["units_per_block"]
                                                  * params.SHARE_FACTOR, 1)
        if st["miner"]:
            try:
                bal = http_get(f"{self.node}/account/{st['miner']}", timeout=5)["balance"]
            except OSError:
                return m
            now = time.time()
            with self.lock:
                if not self.samples or now - self.samples[-1][0] >= 5:
                    self.samples.append((now, bal))
                old = next((s for s in self.samples if now - s[0] <= 600), self.samples[0])
            span = now - old[0]
            m["balance"] = bal
            m["earned_per_hour"] = int((bal - old[1]) * 3600 / span) if span >= 30 else None
        return m

    def _history(self, addr: str) -> list[dict]:
        h = http_get(f"{self.node}/v1/address/{addr}/history?limit=15", timeout=5)
        rows = [dict(p, height=None) for p in h.get("pending", [])] + h.get("items", [])
        for r in rows[:15]:                              # amounts of transfers, from the transactions
            if r.get("type") == "transfer" and r.get("txid"):
                try:
                    t = http_get(f"{self.node}/v1/tx/{r['txid']}", timeout=5)["tx"]
                    r["amount"], r["other"] = t["amount"], t["to"] if r["role"] == "sender" else txmod.sender(t)
                except (OSError, KeyError, TypeError):
                    pass
        return rows[:15]

    # -- the wallet ---------------------------------------------------------------------

    def _new_password(self, body: dict) -> str:
        if _read(self.data) is not None:
            raise UIError("this folder already holds a wallet")
        password = body.get("password", "")
        if len(password) < MIN_PASSWORD:
            raise UIError(f"choose a password of at least {MIN_PASSWORD} characters")
        return password

    def post_create(self, body: dict) -> dict:
        """A new wallet; the phrase is shown once, to be written down."""
        password = self._new_password(body)
        phrase = hd.new_phrase(24)
        self._use(create_wallet(self.data, password, phrase))
        return {"address": self.key.address, "phrase": phrase}

    def post_restore(self, body: dict) -> dict:
        password = self._new_password(body)
        try:
            phrase = hd.normalize_phrase(body.get("phrase", ""))
        except hd.BadPhrase as e:
            raise UIError(f"that phrase cannot be used: {e}") from None
        self._use(create_wallet(self.data, password, phrase))
        return {"address": self.key.address}

    def post_unlock(self, body: dict) -> dict:
        w = _read(self.data)
        if w is None:
            raise UIError("no wallet in this folder yet")
        self._use(unlock(w, body.get("password", "")))
        return {"address": self.key.address}

    def post_lock(self, body: dict) -> dict:
        self._use(None)
        return {"locked": True}

    def _key(self) -> Key:
        key = self.key
        if key is None:
            raise UIError("unlock the wallet first")
        return key

    def post_send(self, body: dict) -> dict:
        key = self._key()
        to = str(body.get("to", "")).strip()
        if not is_address(to):
            raise UIError("that is not a Green address (they start with grn1)")
        amount = params.parse_amount(str(body.get("amount", "")))
        if amount <= 0:
            raise UIError("the amount must be positive")
        chain_id = http_get(f"{self.node}/status", timeout=10)["genesis"]
        acct = http_get(f"{self.node}/account/{key.address}", timeout=10)
        fields = {"to": to, "amount": amount}
        fee = txmod.min_fee({"type": "transfer", **fields})
        rate = http_get(f"{self.node}/v1/fee?blocks=2", timeout=10)["fee_rate"]
        size = txmod.tx_size(txmod.make_tx(key, chain_id, "transfer", acct["pending_nonce"], fee, **fields))
        fee = max(fee, int(rate * size) + 1 if rate else 0)
        if amount + fee > acct["pending_balance"]:
            raise UIError(f"not enough coins: {params.fmt_amount(acct['pending_balance'])} available, "
                          f"{params.fmt_amount(amount + fee)} needed with the fee")
        t = txmod.make_tx(key, chain_id, "transfer", acct["pending_nonce"], fee, **fields)
        return {"txid": http_post(f"{self.node}/tx", t, timeout=10)["txid"], "fee": fee}

    # -- compute ------------------------------------------------------------------------

    def _run(self, what: str, fn) -> dict:
        """Compute takes seconds (the first purchase waits a block for its channels): it runs
        in the background and the page follows it in the state."""
        self._key()
        with self.lock:
            if self.compute["busy"]:
                raise UIError("a purchase is already running")
            self.compute = {"busy": True, "what": what, "result": None, "error": None}

        def go():
            try:
                result, error = fn(), None
            except (AppError, UIError, OSError, ValueError) as e:
                result, error = None, str(e)
            with self.lock:
                self.compute = {"busy": False, "what": what, "result": result, "error": error}
        threading.Thread(target=go, daemon=True, name="hub-compute").start()
        return {"started": what}

    def post_buy(self, body: dict) -> dict:
        m, k, n = (int(body.get(d, 0)) for d in ("m", "k", "n"))
        if not all(1 <= d <= MAX_HUB_DIM for d in (m, k, n)):
            raise UIError(f"each dimension must be 1 to {MAX_HUB_DIM}")
        privacy = body.get("privacy", "split")
        if privacy not in MODES + ("none",):
            raise UIError(f"privacy must be one of {', '.join(MODES)} or none")
        app = self.app
        return self._run(f"a {m}x{k}x{n} product ({privacy})", lambda: app.product("me", m, k, n, privacy))

    def post_job(self, body: dict) -> dict:
        m, k, n = (int(body.get(d, 0)) for d in ("m", "k", "n"))
        if not all(1 <= d <= 4096 for d in (m, k, n)):
            raise UIError("each dimension must be 1 to 4096")
        price = params.parse_amount(str(body.get("price", "")))
        if price <= 0:
            raise UIError("the price per unit must be positive")
        app = self.app
        return self._run(f"a {m}x{k}x{n} job on chain", lambda: app.post_job("me", m, k, n, price))

    def post_close(self, body: dict) -> dict:
        app = self.app
        self._key()
        return self._run("closing the channels", lambda: {"returned": app.close("me")})

    # -- mining -------------------------------------------------------------------------

    def post_mining(self, body: dict) -> dict:
        if not is_local(self.node):
            raise UIError("mining is steered on the node itself: run the hub with a node on this computer")
        req = {k: body[k] for k in ("on", "intensity", "gpu") if k in body}
        if not req:
            raise UIError("nothing to change")
        if "intensity" in req:
            req["intensity"] = int(req["intensity"])
        return http_post(f"{self.node}/mining", req, timeout=60)     # (turning the GPU on checks it first)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m green_hub.hub", description="Green Hub: wallet, compute and mining")
    ap.add_argument("--data", default="wallet", help="the wallet's folder (default: wallet); "
                                                     "a node run with the same folder mines for it")
    ap.add_argument("--node", default=f"http://127.0.0.1:{params.DEFAULT_PORT}", help="a Green node's API")
    ap.add_argument("--port", type=int, default=8630)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    os.makedirs(args.data, exist_ok=True)
    h = Hub(args.data, args.node, args.port)
    h.start()
    print(f"Green Hub: {h.url}   (folder {os.path.abspath(args.data)}, node {args.node}); Ctrl+C to stop")
    if not args.no_browser:
        webbrowser.open(h.url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("Closing channels...")
        h.stop()


if __name__ == "__main__":
    main()
