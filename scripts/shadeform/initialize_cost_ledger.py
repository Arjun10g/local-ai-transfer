#!/usr/bin/env python3
"""Offline, explicit one-time initialization of the Shadeform cost ledger."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import shadeform_lifecycle as lifecycle


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Create the reviewed Shadeform JSON cost baseline exactly once; "
            "this performs no provider or network action"
        )
    )
    value.add_argument("--program", required=True)
    value.add_argument("--currency", required=True)
    value.add_argument("--budget-cap-usd", required=True, type=float)
    value.add_argument("--prior-settled-spend-usd", required=True, type=float)
    value.add_argument("--current-pending-owner-count", required=True, type=int)
    value.add_argument("--expected-display-ledger-sha256", required=True)
    value.add_argument("--expected-incidents-sha256", required=True)
    value.add_argument("--confirm-reviewed-genesis", required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        genesis, status = lifecycle.initialize_cost_ledger_genesis_with_status(
            program=args.program,
            currency=args.currency,
            budget_cap_usd=args.budget_cap_usd,
            prior_settled_spend_usd=args.prior_settled_spend_usd,
            current_pending_owner_count=args.current_pending_owner_count,
            expected_display_ledger_sha256=args.expected_display_ledger_sha256,
            expected_incidents_sha256=args.expected_incidents_sha256,
            confirmation=args.confirm_reviewed_genesis,
        )
    except (OSError, ValueError, lifecycle.ShadeformError) as exc:
        print(f"cost ledger genesis refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "status": status,
        "schema": genesis["schema"],
        "program": genesis["program"],
        "currency": genesis["currency"],
        "budget_cap_usd": genesis["budget_cap_usd"],
        "prior_settled_spend_usd": genesis["prior_settled_spend_usd"],
        "current_pending_owner_count": genesis["current_pending_owner_count"],
        "display_ledger_sha256": genesis["display_ledger_sha256"],
        "incidents_sha256": genesis["incidents_sha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
