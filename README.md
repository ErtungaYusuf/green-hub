# Green Hub

Everything a person does with [Green (GRN)](https://github.com/ErtungaYusuf/green) in one page in the browser. It runs on your computer, listens only on `127.0.0.1`, and talks to a Green node's API. The single-purpose pages are [green-wallet](https://github.com/ErtungaYusuf/green-wallet) and [green-miner](https://github.com/ErtungaYusuf/green-miner).

- **Wallet:** create a wallet (24-word recovery phrase, encrypted with a password) or restore one; see the balance and history; send coins. Transactions are signed here and only the signed transaction goes to the node, so the node can be anyone's.
- **Buy compute:** have the network's miners multiply matrices for you, paid product by product through payment channels. Every result is checked (Freivalds' test) before it is paid for and compared with the same product computed here. The data can be hidden from the miners: *split* (no miner sees the whole problem) or *secret* (each of three miners sees only random shares). Or post a job to the on-chain market, which miners compute as their mining work.
- **Final or not:** on a network with its founder's checkpoints, the history marks the payments that are final (no chain can undo them).
- **Mining:** start and stop the node's mining, set the intensity (the share of the time the GPU or CPU works), switch between GPU and CPU, and watch the shares found and what they earn. Nearly all of each block's reward goes to shares, about 128 a block, so income follows the work done, with no pool.

## Install

Python 3.11 or newer, with Green 0.24 or newer installed (the green repository next to this one works too):

```bash
pip install -e ../green
pip install -e ".[dev]"
```

## Use

Run a node with your wallet's folder, so it mines for that wallet, then the hub on the same folder:

```bash
grn --data me wallet new
grn --data me node --mine --gpu
green-hub --data me --node http://127.0.0.1:8545
```

(or `python -m green_hub.hub ...`). On a local network (`grn localnet start`), point it at a persona's node and folder, for example `green-hub --data ../green/localnet/alice --node http://127.0.0.1:18501`.

Mining is steered on the node itself, so the Mining tab needs the node on this computer. The wallet and compute tabs work with any node.

## Safety

The page only answers requests that name this computer (`Host: 127.0.0.1` or `localhost`), so a DNS-rebinding web page cannot reach it. Every action needs the page's own token, which a web site open in another tab can neither read nor send. Anyone who can run programs on your computer can still use it, as they could use the wallet file itself. Compute providers on this computer's own addresses are only used when the node is on this computer too (a local network).

## Tests

```bash
python -m pytest -q
```

They start two real Green nodes (one mining and selling compute) and check unlocking and sending from the wallet, buying a product that is checked and matches the local one, closing the channels, steering the node's mining, and that web pages from other sites cannot use the hub.
