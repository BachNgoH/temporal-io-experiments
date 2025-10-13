from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass
class InvoiceRecord:
    """Lightweight representation of an invoice found in an event file.

    Keys `khhdon` (form code) and `shdon` (invoice number/serial) together
    determine a unique invoice.
    """

    event_id: str | None
    invoice_id: str | None
    invoice_number: str | None
    khhdon: str  # invoice form series
    shdon: str  # invoice serial/number
    file_path: str


def iter_event_files(directory: Path) -> Iterable[Path]:
    """Yield all .json files in the directory (non-recursive)."""
    if not directory.exists() or not directory.is_dir():
        raise FileNotFoundError(f"Directory not found: {directory}")
    yield from sorted(p for p in directory.iterdir() if p.is_file() and p.suffix == ".json")


def safe_get(dct: dict[str, Any], *keys: str, default: Any | None = None) -> Any | None:
    """Safely get nested keys from dict."""
    cur: Any = dct
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def load_invoices_from_events(directory: Path) -> list[InvoiceRecord]:
    """Load invoices from event JSON files in directory.

    Only events that contain `payload.invoice_detail` with both `khhdon` and `shdon`
    are considered invoices.
    """
    invoices: list[InvoiceRecord] = []
    for path in iter_event_files(directory):
        try:
            with path.open("r", encoding="utf-8") as f:
                data: dict[str, Any] = json.load(f)
        except Exception:
            # Skip unreadable/invalid files
            continue

        detail = safe_get(data, "payload", "invoice_detail")
        if not isinstance(detail, dict):
            continue

        khhdon = detail.get("khhdon")
        shdon = detail.get("shdon")
        if khhdon is None or shdon is None:
            continue

        event_id = data.get("event_id")
        invoice_id = safe_get(data, "payload", "invoice_id")
        invoice_number = safe_get(data, "payload", "invoice_number")

        invoices.append(
            InvoiceRecord(
                event_id=str(event_id) if event_id is not None else None,
                invoice_id=str(invoice_id) if invoice_id is not None else None,
                invoice_number=str(invoice_number) if invoice_number is not None else None,
                khhdon=str(khhdon),
                shdon=str(shdon),
                file_path=str(path),
            )
        )

    return invoices


def unique_invoice_keys(invoices: Iterable[InvoiceRecord]) -> set[tuple[str, str]]:
    """Return unique keys by (khhdon, shdon)."""
    return {(inv.khhdon, inv.shdon) for inv in invoices}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Scan event JSON files and report unique invoices based on (khhdon, shdon)."
        )
    )
    default_dir = Path(__file__).resolve().parents[1] / "app" / "data" / "events" / "unknown"
    parser.add_argument(
        "--dir",
        dest="directory",
        type=Path,
        default=default_dir,
        help="Directory containing event JSON files (default: app/data/events/unknown)",
    )
    args = parser.parse_args()

    invoices = load_invoices_from_events(args.directory)
    unique_keys = unique_invoice_keys(invoices)

    print(f"Total event files scanned: {len(list(iter_event_files(args.directory)))}")
    print(f"Invoices detected (with khhdon & shdon): {len(invoices)}")
    print(f"Unique invoices by (khhdon, shdon): {len(unique_keys)}")

    # Optional: show a few examples for quick verification
    print("Examples (up to 5):")
    for idx, (khhdon, shdon) in enumerate(sorted(unique_keys)[:5], start=1):
        print(f"  {idx}. khhdon={khhdon}, shdon={shdon}")


if __name__ == "__main__":
    main()


