# Broker Execution

A contribution of the existing Traders@SMU execution prototype. Both Alpaca and
Interactive Brokers integrations are preserved. This is preparation, read-only
reconciliation and offline simulation software: **all order submission remains
disabled**. Do not change the hard-coded safety locks. Keep TWS Read-Only API checked.

## Repository approvals still needed

`AI.md` and `CLAUDE.md` require Context7 and Ponytail review. Those skills were not
available during preparation; maintainer review or a waiver is still needed before
merge. Platform developer **dplynn** must approve dependencies absent from
`approved-packages.md`: `alpaca-py`, `python-dotenv`, `ibapi`, `pytest`, and optional
research dependencies `numba` and `pyarrow`. Nothing was installed for preparation.
The existing Python 3.9 environment was used for offline tests; a clean UV install
and other supported Python versions have not been verified. Transitive dependencies
also require the team's normal dependency review. No UV lockfile is supplied.

## Setup after package approval

Run commands from this folder. Python 3.9 through 3.12 is declared; tests during
preparation ran on Python 3.9. The paper ledger uses Unix `fcntl` file locking.

```sh
uv sync --extra ibkr --extra test
uv run --no-sync python -m pytest -q
```

`pyproject.toml` pins direct dependencies to the existing tested environment.
`requirements-ibkr.txt` is retained for the standalone optional IBKR diagnostic.
Keep local credentials in an ignored `.env`; `example.env` contains blank names
only. Do not put credentials or account IDs in `config.json`. Diagnostics can print
account information and create sensitive outputs: keep their output local and
never attach it to a PR. No credentials are needed for the offline tests.

## Configuration and private research

`config.json` controls runtime book size, signal-age limit, diagnostic connection
defaults and the legacy Alpaca shortability-check subset. Set
`BROKER_EXECUTION_CONFIG` to an ignored `config.local.json` to use private settings.
API safety allowlists, the 32-ticker strategy universe and execution locks remain
fixed intentionally. CLI options and explicit snapshot/limits inputs retain their
existing behavior. These settings do not change Ian's strategy parameters.

`research_directory` may point to a separately approved, unchanged
`ResidualBeta_share` checkout; a relative path is relative to this folder.
Its default is the ignored `ian_original/ResidualBeta_share` location. The checkout
and its datasets are **not distributed here**. Do not copy them into Git. Request
approved access from the research owner. Historical portfolio generation requires
that checkout and its expected local data; it is not a standalone capability of
this contribution.

The historical parity suite also requires the private regression inputs
`tests/fixtures/dry_run_report_before_refactor.csv`,
`tests/fixtures/october15_corrected_portfolio.csv` and `data/dry_run_report.csv`.
Those generated reports are excluded. Tests explicitly skip if the private
prerequisites are absent; skips do not establish historical parity. One collector
universe-comparison test also skips without the original `fetch_bars.py`.

## Commands

Offline tests use synthetic in-memory broker responses and block socket connections:

```sh
uv run --no-sync python -m pytest -q
uv run --no-sync python -m strategy.collect_preclose --help
uv run --no-sync python main.py --help
```

With separately approved private research inputs, historical preview can be run
using `main.py --dry-run`. That path reads Alpaca paper account/asset information
and needs local credentials; it cannot submit orders. The original strategy signals
retain full-history calibration and adjustment-provenance limitations. Historical
parity is not a claim of true point-in-time validity.

For pre-close replay, supply a verified snapshot; no fabricated example is supplied:

```sh
uv run --no-sync python main.py --dry-run --preclose-snapshot data/snapshot.json
```

The collector deliberately blocks snapshot export until adjustment-factor provenance
and historical initialization compatibility are resolved. This contribution does
not weaken those validations or provide current signals.

Optional authenticated **read-only** TWS diagnostics (not run during preparation):

```sh
uv run --no-sync python -m strategy.ibkr_diagnostic --host 127.0.0.1 --port 7497 --client-id 17
uv run --no-sync python -m strategy.ibkr_shortability --port 7497 --client-id 17
```

For a historical offline IBKR preview, first obtain a local 32-contract diagnostic
CSV. Contracts, live borrow availability, and fees are not bundled or fabricated:

```sh
uv run --no-sync python -m strategy.ibkr_order_preview --signal-date YYYY-MM-DD --contracts data/contracts.csv
```

This creates non-transmitting MOC/MOO objects and hypothetical partial fills/exits;
no broker client is created. IBKR borrow is UNKNOWN when unavailable, never inferred
from contract resolution. Auction simulator behavior and real borrow remain
unverified. Alpaca preparation/reconciliation commands are listed by:

```sh
uv run --no-sync python -m strategy.paper_trader --help
```

Entry requires explicit current-session pre-close JSON and private account limits.
Both submit-entry and submit-exit reject before loading credentials. Exits use
actual reconciled strategy fills; ambiguous state prevents automatic retries.

## Contribution boundaries

Only Python source/tests, dependency declarations, blank configuration examples
and documentation are included. Historical data, broker/account observations,
contract snapshots, receipts, reports, ledgers, research files, backups and virtual
environments are excluded. The ignore file is a safeguard, not a substitute for
reviewing each staged file. Do not force-add ignored outputs.

Official API references used by the existing integrations:

- [IBKR TWS API](https://www.interactivebrokers.com/docs/tws-api/)
- [Alpaca API](https://docs.alpaca.markets/)
