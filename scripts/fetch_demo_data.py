#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch the UCI "Online Retail" dataset and produce a demo/test CSV sample.

WHY THIS SCRIPT EXISTS
----------------------
The dataset is used as a **demo/eval anchor** for a data-analysis product. Its
value is that it contains several *real* data-quality traps:

  * returns / write-offs   -> rows with ``Quantity < 0``
  * cancellations          -> ``InvoiceNo`` starting with the letter ``C``
  * missing customer ids   -> ``CustomerID`` is NaN
  * non-positive prices    -> ``UnitPrice <= 0``
  * unbalanced countries   -> United Kingdom dominates
  * an incomplete month    -> 2011-12 stops on 2011-12-09, so the last month has
                              roughly a third of the rows of a normal month

A naive random sample destroys most of these (they are all rare-ish minority
classes), which would silently make the demo meaningless. This script therefore
does a **trap-preserving stratified sample**: every row belonging to a trap is
kept, and the rest of the row budget is filled with random rows.

Specifically, a sample produced by this script always retains:

  * 100 % of ``Quantity < 0`` rows
  * 100 % of ``InvoiceNo`` values starting with ``C``
  * 100 % of the ``2011-12`` month
  * 100 % of ``UnitPrice <= 0`` rows
  * missing-``CustomerID`` rows at least in proportion to their source share,
    with a floor of ``MIN_MISSING_CUSTOMER`` rows

DATA SOURCE, CITATION AND LICENCE
---------------------------------
Source page : https://archive.ics.uci.edu/dataset/352/online+retail
Download URL: https://archive.ics.uci.edu/static/public/352/online+retail.zip
DOI         : 10.24432/C5BW33
Licence     : Creative Commons Attribution 4.0 International (CC BY 4.0)
              https://creativecommons.org/licenses/by/4.0/
              This permits sharing and adaptation for any purpose **provided
              that appropriate credit is given**. Attribution is mandatory, so
              any downstream use (including the demo) must credit the source.

Required citation (as given by UCI on the dataset page)::

    Chen, D. (2015). Online Retail [Dataset]. UCI Machine Learning Repository.
    https://doi.org/10.24432/C5BW33

Original paper describing the data::

    Chen, D., Sain, S. L., & Guo, K. (2012). Data mining for the online retail
    industry: A case study of RFM model-based customer segmentation using data
    mining. Journal of Database Marketing & Customer Strategy Management,
    19(3), 197-208.

Also note: the dataset itself is real transaction data from a UK online retailer.
The ``Description`` and ``CustomerID`` fields are commercial data; the UCI page
does not state that the customer identifiers were anonymised or that consent was
obtained for redistribution. **If you plan to redistribute this data outside the
project, confirm the licensing/privacy position yourself** - the CC BY 4.0 label
above is what UCI publishes for the dataset, nothing more.

USAGE
-----
Run from the **project root** (paths are resolved relative to the repo root, not
to this file's location, but the defaults assume the usual layout)::

    python scripts/fetch_demo_data.py                      # ~100k row demo sample
    python scripts/fetch_demo_data.py --full               # entire dataset (~541,909 rows)
    python scripts/fetch_demo_data.py --rows 5000 --cap-month 2000 \\
        --out data/processed/online_retail_small.csv       # test-sized sample
    python scripts/fetch_demo_data.py --url <mirror-url>    # override the download URL
    python scripts/fetch_demo_data.py --source data/raw/Online\\ Retail.xlsx  # use a local copy

The download is cached under ``data/raw/`` and is **skipped when already
present**; use ``--force-download`` to re-fetch.

IMPORTANT LIMITATION - SMALL SAMPLES
------------------------------------
The incomplete final month 2011-12 contains **25,525 rows on its own**, so no
target below roughly 37,000 rows can hold *every* trap at once. Asking for a
smaller sample without resolving that conflict is an error rather than a silent
surprise: the script refuses and tells you the two ways out.

  * ``--rows 100000``          -> all four traps kept 100 % (recommended demo).
  * ``--rows 100000 --cap-month 2000`` -> same, but only 2000 month rows.
  * ``--rows 5000 --cap-month 2000``   -> test-sized; the 2011-12 month is then a
    *day-stratified sample*, not the full month, and the report says ``CAPPED``
    instead of claiming 100 % retention. Everything else is still kept in full.
"""

from __future__ import annotations

import argparse
import io
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

DOWNLOAD_URL = (
    "https://archive.ics.uci.edu/static/public/352/online+retail.zip"
)
SOURCE_PAGE = "https://archive.ics.uci.edu/dataset/352/online+retail"
CITATION = (
    "Chen, D. (2015). Online Retail [Dataset]. UCI Machine Learning Repository. "
    "https://doi.org/10.24432/C5BW33"
)
LICENCE = "CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/)"

#: Directory (relative to the project root) used as the download cache.
DEFAULT_RAW_DIR = Path("data") / "raw"
#: Name of the xlsx inside the UCI zip.
XLSX_NAME = "Online Retail.xlsx"
#: Rows kept for a trap-preserving demo sample.
DEFAULT_ROWS = 100_000
#: Rows kept for a trap-preserving unit-test sample.
SMALL_ROWS = 5_000
#: Floor for how many missing-CustomerID rows a sample must retain.
MIN_MISSING_CUSTOMER = 3_000
#: Preferred number of plain (non-trap) rows, so a sample is never all traps.
MIN_RANDOM_FILL = 1_000
#: The month that is incomplete in the source data and must be kept in full.
TRAP_MONTH = "2011-12"
#: Fixed seed so the sample is reproducible.
DEFAULT_SEED = 20240101

EXPECTED_COLUMNS = [
    "InvoiceNo",
    "StockCode",
    "Description",
    "Quantity",
    "InvoiceDate",
    "UnitPrice",
    "CustomerID",
    "Country",
]


# --------------------------------------------------------------------------- #
# Download / load
# --------------------------------------------------------------------------- #

def download_zip(url: str, dest: Path, force: bool = False) -> Path:
    """Download ``url`` to ``dest`` unless it already exists.

    Returns the path to the local zip. Raises RuntimeError with an actionable
    message when the download fails.
    """
    if dest.exists() and not force:
        print(f"[download] cache hit, skipping download: {dest}")
        return dest

    if dest.exists() and force:
        print(f"[download] --force-download given, re-fetching over {dest}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"[download] fetching {url}")
    print(f"[download]   -> {dest}")

    try:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            url,
            headers={"User-Agent": "datamind-fetch-demo-data/1.0 (+python-urllib)"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            read = 0
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    read += len(chunk)
                    if total:
                        pct = 100.0 * read / total
                        print(
                            f"\r[download]   {read / 1e6:7.1f} / {total / 1e6:.1f} MB "
                            f"({pct:5.1f}%)",
                            end="",
                            flush=True,
                        )
                    else:
                        print(f"\r[download]   {read / 1e6:7.1f} MB", end="", flush=True)
        print()
    except Exception as exc:  # noqa: BLE001 - we want a friendly message for all
        tmp.unlink(missing_ok=True)
        raise RuntimeError(_download_help(url, dest, exc)) from exc

    if read == 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            _download_help(url, dest, RuntimeError("server returned 0 bytes"))
        )

    tmp.replace(dest)
    print(f"[download] done: {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
    return dest


def _download_help(url: str, dest: Path, exc: BaseException) -> str:
    """Build a clear, actionable failure message.

    NOTE: the separator is bound to a local *before* being used, and is not
    written as an inline ``"=" * 78`` literal. CPython 3.12.14 mis-folds
    constant expressions of the shape::

        ("\\n" "=" * 78 + "\\n" "HEADER\\n" "=" * 78 + "\\n" f"...")

    where the first fragment already contains an f-string - the earlier
    fragment gets replicated N times (N == the repetition count) and the
    message balloons from ~1 KB to tens of KB. Hoisting the product into a
    variable sidesteps the folder entirely.
    """
    rule = "=" * 78
    return (
        "\n"
        + rule
        + "\nDOWNLOAD FAILED - manual step required\n"
        + rule
        + f"\nReason      : {type(exc).__name__}: {exc}\n"
        f"Tried URL   : {url}\n"
        f"Target path : {dest.resolve()}\n"
        "\n"
        "What to do:\n"
        "  1. Open the dataset page in a browser:\n"
        f"       {SOURCE_PAGE}\n"
        "  2. Download the file (the 'Download' button, ~22.6 MB zip):\n"
        f"       {url}\n"
        "  3. Unzip it. It contains a single file named 'Online Retail.xlsx'.\n"
        "  4. Put that file at exactly:\n"
        f"       {dest.parent.resolve() / XLSX_NAME}\n"
        "     (create the folder if needed), then re-run this script.\n"
        "\n"
        "Alternative: if you already have the xlsx somewhere else, skip the\n"
        "download entirely:\n"
        f"  python {Path(sys.argv[0]).name} --source \"<path to Online Retail.xlsx>\"\n"
        "\n"
        "If the machine has no internet access, copy the xlsx from another\n"
        "machine - no other network step is required after that.\n"
        + rule
    )


def extract_xlsx(zip_path: Path, raw_dir: Path, force: bool = False) -> Path:
    """Extract the xlsx from the UCI zip into ``raw_dir`` and return its path."""
    target = raw_dir / XLSX_NAME
    if target.exists() and not force:
        print(f"[unzip] cache hit, skipping extraction: {target}")
        return target

    print(f"[unzip] extracting {zip_path.name}")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            print(f"[unzip] archive contains: {names}")
            pick = None
            for name in names:
                if Path(name).name.lower() == XLSX_NAME.lower():
                    pick = name
                    break
            if pick is None:
                xlsx = [n for n in names if n.lower().endswith(".xlsx")]
                if not xlsx:
                    raise RuntimeError(
                        f"no .xlsx found inside {zip_path}; archive members: {names}"
                    )
                pick = xlsx[0]
            raw_dir.mkdir(parents=True, exist_ok=True)
            with zf.open(pick) as src, open(target, "wb") as dst:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    dst.write(chunk)
    except zipfile.BadZipFile as exc:
        raise RuntimeError(
            f"{zip_path} is not a valid zip archive - it is probably an HTML error "
            "page saved under a .zip name. Delete it and re-download from "
            f"{DOWNLOAD_URL} (see the dataset page {SOURCE_PAGE})."
        ) from exc

    print(f"[unzip] done: {target} ({target.stat().st_size / 1e6:.1f} MB)")
    return target


def load_source(xlsx_path: Path):
    """Read the xlsx into a DataFrame, normalising column names/dtypes."""
    print(f"[read] pandas.read_excel({xlsx_path.name}) - this takes ~1-2 min for 540k rows")
    df = pd.read_excel(xlsx_path)
    print(f"[read] loaded {len(df):,} rows x {len(df.columns)} cols")

    # Be forgiving about stray whitespace in headers from re-saved copies.
    df.columns = [str(c).strip() for c in df.columns]

    missing = [c for c in EXPECTED_COLUMNS if c not in df.columns]
    if missing:
        raise SystemExit(
            f"unexpected columns in {xlsx_path}: missing {missing}; "
            f"found {list(df.columns)}. Expected the 8 UCI Online Retail columns."
        )
    # Keep the canonical column order.
    return df[EXPECTED_COLUMNS].copy()


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #

def _month_series(df):
    return pd.to_datetime(df["InvoiceDate"]).dt.to_period("M").astype(str)


def compute_metrics(df, label: str) -> dict:
    """Compute every trap metric we report on."""
    inv = df["InvoiceNo"].astype(str).str.strip()
    is_c = inv.str.upper().str.startswith("C")
    neg = df["Quantity"] < 0
    missing = df["CustomerID"].isna()
    month = _month_series(df)
    dt = pd.to_datetime(df["InvoiceDate"])

    rows = len(df)
    pct = lambda n: (100.0 * n / rows) if rows else 0.0  # noqa: E731

    return {
        "label": label,
        "rows": rows,
        "neg_qty": int(neg.sum()),
        "neg_qty_pct": pct(int(neg.sum())),
        "c_invoice": int(is_c.sum()),
        "c_invoice_pct": pct(int(is_c.sum())),
        "neg_qty_and_c": int((neg & is_c).sum()),
        "trap_month": int((month == TRAP_MONTH).sum()),
        "trap_month_pct": pct(int((month == TRAP_MONTH).sum())),
        "missing_cust": int(missing.sum()),
        "missing_cust_pct": pct(int(missing.sum())),
        "unitprice_le0": int((df["UnitPrice"] <= 0).sum()),
        "unitprice_le0_pct": pct(int((df["UnitPrice"] <= 0).sum())),
        "unitprice_eq0": int((df["UnitPrice"] == 0).sum()),
        "unitprice_lt0": int((df["UnitPrice"] < 0).sum()),
        "countries": int(df["Country"].nunique()),
        "uk_rows": int((df["Country"] == "United Kingdom").sum()),
        "uk_pct": pct(int((df["Country"] == "United Kingdom").sum())),
        "date_min": str(dt.min()),
        "date_max": str(dt.max()),
        "monthly": {
            str(k): int(v)
            for k, v in pd.to_datetime(df["InvoiceDate"])
            .dt.to_period("M")
            .value_counts()
            .sort_index()
            .items()
        },
    }


# --------------------------------------------------------------------------- #
# Trap-preserving sample
# --------------------------------------------------------------------------- #

def stratify_sample(
    df,
    target_rows: int,
    seed: int = DEFAULT_SEED,
    cap_month: int | None = None,
    allow_over_target: bool = False,
    baseline_rows: int = MIN_RANDOM_FILL,
):
    """Sample ``target_rows`` rows while keeping every trap row.

    Strategy, in order of priority:

    1. **Must-keep (kept 100 %)** - the union of:
         * ``Quantity < 0``                (returns / write-offs)
         * ``InvoiceNo`` starts with ``C`` (cancellations)
         * ``InvoiceDate`` in ``TRAP_MONTH`` (the incomplete final month)
         * ``UnitPrice <= 0``             (non-positive prices)
       These are disjoint from the fill pool, so none of them can be dropped.

       ``TRAP_MONTH`` alone accounts for 25,525 source rows, so **no target
       below ~37k rows can hold every trap**. If the must-keep set does not fit
       the target this raises ``ValueError`` unless the caller explicitly opts
       into ``allow_over_target``; pass ``cap_month`` to keep a day-stratified
       sample of the month instead of all of it.

    2. **Missing CustomerID quota** - the remaining budget is split so that
       missing-CustomerID rows are kept *at least* in proportion to their share
       of the source, with a floor of ``MIN_MISSING_CUSTOMER`` rows (when the
       budget allows). Drawn from the rows that are not in the must-keep set.

    3. **Random fill** - whatever budget is left is filled with uniformly random
       rows from the rest.

    All parts are recombined and sorted by original source order, so the output
    stays chronologically usable. Indices are chosen with a seeded RNG, so the
    result is deterministic for a given seed.
    """
    if target_rows <= 0:
        raise ValueError("target_rows must be positive")
    if target_rows >= len(df):
        print(
            f"[sample] target {target_rows:,} >= source {len(df):,} rows: "
            "keeping everything (no rows dropped)."
        )
        return df.copy(), {
            "target": target_rows,
            "covered_by_must_keep": True,
            "must_keep": len(df),
            "missing_quota": 0,
            "random_fill": 0,
            "month_capped_to": None,
        }

    inv = df["InvoiceNo"].astype(str).str.strip()
    neg = df["Quantity"] < 0
    is_c = inv.str.upper().str.startswith("C")
    month = _month_series(df)

    mask_neg = neg.to_numpy()
    mask_c = is_c.to_numpy()
    mask_month_all = (month == TRAP_MONTH).to_numpy()
    mask_missing = df["CustomerID"].isna().to_numpy()
    mask_price = (df["UnitPrice"] <= 0).to_numpy()

    rng = np.random.default_rng(seed)
    n = len(df)

    # --- the incomplete month is the only trap we may ever have to cap ------ #
    mask_month = mask_month_all.copy()
    month_capped_to = None
    if cap_month is not None and mask_month_all.sum() > cap_month:
        # Day-stratified so the cap still spans every day of the month
        # (a plain random draw could miss days and hide the cut-off).
        days = pd.to_datetime(df["InvoiceDate"]).dt.normalize()
        month_pos = mask_month_all.nonzero()[0]
        day_keys = days.iloc[month_pos]
        uniq_days = sorted(day_keys.unique())
        # Largest-remainder allocation of exactly `cap_month` seats across days,
        # guaranteeing every day gets at least one row when the cap allows it.
        quota = _largest_remainder(
            [int((day_keys == d).sum()) for d in uniq_days], cap_month
        )
        picked: list[np.ndarray] = []
        for day, want in zip(uniq_days, quota):
            if want <= 0:
                continue
            day_pos = month_pos[(day_keys == day).to_numpy()]
            picked.append(rng.choice(day_pos, size=want, replace=False))
        sel = np.unique(np.concatenate(picked)) if picked else np.empty(0, dtype=np.int64)
        keep_month = np.zeros(n, dtype=bool)
        keep_month[sel] = True
        mask_month = keep_month
        month_capped_to = int(len(sel))
        print(
            f"[sample] --cap-month={cap_month:,}: keeping {month_capped_to:,} of "
            f"{int(mask_month_all.sum()):,} {TRAP_MONTH} rows, spread over all "
            f"{len(uniq_days)} days. {TRAP_MONTH} is NO LONGER complete in this sample."
        )

    mask_must = mask_neg | mask_c | mask_month | mask_price
    must_idx = mask_must.nonzero()[0]
    must_count = len(must_idx)
    budget = target_rows - must_count

    if budget < 0:
        suggested = must_count + MIN_MISSING_CUSTOMER
        msg = (
            f"the {must_count:,} must-keep trap rows already exceed the requested "
            f"{target_rows:,}-row target, so a sample of that size cannot contain "
            f"every trap.\n"
            f"    must-keep breakdown (overlapping; union = {must_count:,} rows):\n"
            f"      Quantity < 0            : {int(mask_neg.sum()):,}\n"
            f"      InvoiceNo starts with C : {int(mask_c.sum()):,}\n"
            f"      UnitPrice <= 0          : {int(mask_price.sum()):,}\n"
            f"      {TRAP_MONTH} rows kept     : {int(mask_month.sum()):,} "
            f"(of {int(mask_month_all.sum()):,} in source"
            + (f", capped by --cap-month {cap_month:,}" if month_capped_to is not None else "")
            + ")\n"
            f"    The smallest sample that still keeps every trap is therefore about "
            f"{suggested:,} rows.\n"
            "    Options:\n"
            f"      * keep every trap:   --rows {suggested}\n"
            f"      * allow a bigger file than asked: --allow-over-target\n"
        )
        if month_capped_to is None:
            msg += (
                f"      * give up some {TRAP_MONTH} rows: --cap-month N\n"
                f"        (keeps ALL returns, ALL cancellations and ALL UnitPrice<=0\n"
                f"         rows; only the incomplete month becomes a sample.)\n"
                f"        Note: this lowers the floor to "
                f"{int((mask_neg | mask_c | mask_price).sum()) + MIN_MISSING_CUSTOMER:,} "
                f"rows, so it still cannot reach {target_rows:,} if the other traps\n"
                f"        are kept in full."
            )
        else:
            msg += (
                f"      * lower the month cap further: --cap-month N\n"
                f"        (going from {month_capped_to:,} to N month rows saves at most\n"
                f"         {month_capped_to:,} rows and still leaves "
                f"{int((mask_neg | mask_c | mask_price).sum()):,} non-month trap rows.)"
            )
        if not allow_over_target:
            raise ValueError(msg)
        print("[sample] WARNING: --allow-over-target given; emitting all trap rows.")
        print("         " + msg.replace("\n", "\n         "))
        budget = 0

    # Optional reservation for rows that are BOTH missing-CustomerID and a trap.
    # They are already guaranteed by the must-keep set; we count them so the
    # reported missing-CustomerID quota is what the user actually gets *extra*.
    pool_mask = ~mask_must
    pool_idx = pool_mask.nonzero()[0]
    pool_missing = pool_idx[mask_missing[pool_idx]]

    missing_share = mask_missing.sum() / n
    want_missing = int(round(target_rows * missing_share))

    # The floor cannot exceed the budget; scale it down rather than overflow.
    # Order matters: the floor is clamped to the budget, then the normal-row
    # baseline is taken out of whatever is left, then the missing-CustomerID
    # quota is clamped again. That guarantees `fill >= baseline` whenever the
    # budget can afford it, so the sample is never 100 % trap rows.
    effective_floor = int(min(MIN_MISSING_CUSTOMER, budget))
    want_missing = max(want_missing, effective_floor)
    want_missing = min(want_missing, budget, len(pool_missing))

    baseline = min(baseline_rows, budget)
    if budget - baseline >= effective_floor:
        # Enough room for both the floor and a normal-row baseline.
        want_missing = min(want_missing, budget - baseline)
    else:
        # Very tight budget: the floor and baseline compete. Give missing-
        # CustomerID rows whatever the baseline does not need.
        baseline = max(0, budget - effective_floor)
        want_missing = min(want_missing, budget - baseline)
    want_missing = max(want_missing, 0)
    want_missing = min(want_missing, len(pool_missing))

    if want_missing > 0:
        pick_missing = rng.choice(pool_missing, size=want_missing, replace=False)
    else:
        pick_missing = np.empty(0, dtype=np.int64)

    already = np.zeros(n, dtype=bool)
    already[pool_idx] = True
    if len(pick_missing):
        already[pick_missing] = False
    rest_idx = already.nonzero()[0]

    fill = budget - want_missing
    fill = max(fill, 0)
    fill = min(fill, len(rest_idx))
    if fill > 0:
        pick_rest = rng.choice(rest_idx, size=fill, replace=False)
    else:
        pick_rest = np.empty(0, dtype=np.int64)

    keep = np.concatenate([must_idx, pick_missing, pick_rest])
    keep = np.unique(keep)  # sorts ascending == original source order
    out = df.iloc[keep].reset_index(drop=True)

    stats = {
        "target": target_rows,
        "covered_by_must_keep": False,
        "must_keep": int(must_count),
        "must_keep_neg": int(mask_neg.sum()),
        "must_keep_c": int(mask_c.sum()),
        "must_keep_month": int(mask_month.sum()),
        "must_keep_month_source_total": int(mask_month_all.sum()),
        "month_window_end": str(
            pd.to_datetime(df["InvoiceDate"])[mask_month_all].max()
        ) if mask_month_all.any() else "",
        "month_days": int(
            pd.to_datetime(df["InvoiceDate"])[mask_month_all].dt.normalize().nunique()
        ),
        "must_keep_price": int(mask_price.sum()),
        "month_capped_to": month_capped_to,
        "missing_quota": int(len(pick_missing)),
        "missing_effective_floor": effective_floor,
        "missing_share_source_pct": 100.0 * missing_share,
        "random_fill": int(len(pick_rest)),
        "baseline_effective": int(baseline),
        "budget": int(budget),
        "shortfall": int(target_rows - len(out)),
    }
    return out, stats


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #

def write_csv(df, out_path: Path) -> None:
    """Write the sample as UTF-8 CSV with the source column names/order."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False, encoding="utf-8")
    print(
        f"[write] {out_path}  ({len(df):,} rows, {out_path.stat().st_size / 1e6:.2f} MB, UTF-8)"
    )


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def _delta(orig: int, samp: int, orig_rows: int, samp_rows: int) -> str:
    """Return a short, fixed-width-friendly retention note for one metric."""
    if orig == 0:
        return "n/a"
    if samp >= orig:
        return "KEPT 100%"
    return f"{100.0 * samp / orig:.1f}% kept"


def print_report(orig: dict, samp: dict, stats: dict, sample_df=None,
                 identical_to_source: bool = False) -> None:
    """Print the original-vs-sample trap comparison report."""
    w = 78
    line = "=" * w
    print()
    print(line)
    if identical_to_source:
        print("SAMPLING COMPARISON REPORT (--full: no sampling applied)".center(w))
    else:
        print("SAMPLING COMPARISON REPORT (trap preservation)".center(w))
    print(line)
    print(f"source : {orig['rows']:,} rows   ({orig['date_min']} .. {orig['date_max']})")
    print(f"sample : {samp['rows']:,} rows   ({samp['date_min']} .. {samp['date_max']})")
    if not stats.get("covered_by_must_keep"):
        print(
            f"budget : {stats['target']:,} target = "
            f"{stats['must_keep']:,} must-keep traps + "
            f"{stats['missing_quota']:,} missing-CustomerID + "
            f"{stats['random_fill']:,} random fill"
        )
        if stats.get("month_capped_to") is not None:
            print(
                f"note   : {TRAP_MONTH} is CAPPED to {stats['month_capped_to']:,} rows "
                f"(day-stratified, of {stats.get('must_keep_month_source_total', 0):,} "
                "in source) - this metric is intentionally NOT 100%."
            )
        if stats.get("shortfall"):
            print(
                f"note   : {stats['shortfall']:,} rows of the target were not available "
                "to fill (source exhausted)."
            )
    print("-" * w)
    header = f"{'metric':<32} {'original':>16} {'sample':>16}  {'verdict':<10}"
    print(header)
    print("-" * w)

    def row(name: str, o: int, s: int, o_pct: float, s_pct: float) -> None:
        verdict = _delta(o, s, orig["rows"], samp["rows"])
        left = f"{o:,} ({o_pct:.1f}%)"
        right = f"{s:,} ({s_pct:.1f}%)"
        print(f"{name:<32} {left:>16} {right:>16}  {verdict:<10}")

    row(
        "Quantity < 0 (returns)",
        orig["neg_qty"], samp["neg_qty"], orig["neg_qty_pct"], samp["neg_qty_pct"],
    )
    row(
        "InvoiceNo starts with C (cancels)",
        orig["c_invoice"], samp["c_invoice"],
        orig["c_invoice_pct"], samp["c_invoice_pct"],
    )
    row(
        f"{TRAP_MONTH} full month (incomplete)",
        orig["trap_month"], samp["trap_month"],
        orig["trap_month_pct"], samp["trap_month_pct"],
    )
    row(
        "CustomerID missing",
        orig["missing_cust"], samp["missing_cust"],
        orig["missing_cust_pct"], samp["missing_cust_pct"],
    )
    row(
        "UnitPrice <= 0",
        orig["unitprice_le0"], samp["unitprice_le0"],
        orig["unitprice_le0_pct"], samp["unitprice_le0_pct"],
    )
    row(
        "  of which UnitPrice == 0",
        orig["unitprice_eq0"],
        samp["unitprice_eq0"],
        100.0 * orig["unitprice_eq0"] / orig["rows"],
        100.0 * samp["unitprice_eq0"] / samp["rows"],
    )
    row(
        "  of which UnitPrice < 0",
        orig["unitprice_lt0"],
        samp["unitprice_lt0"],
        100.0 * orig["unitprice_lt0"] / orig["rows"],
        100.0 * samp["unitprice_lt0"] / samp["rows"],
    )
    row(
        "United Kingdom rows",
        orig["uk_rows"], samp["uk_rows"], orig["uk_pct"], samp["uk_pct"],
    )
    print("-" * w)
    print(f"{'distinct countries':<32} {orig['countries']:>16} {samp['countries']:>16}")
    print(
        f"{'Quantity<0 AND C-invoice':<32} {orig['neg_qty_and_c']:>16} "
        f"{samp['neg_qty_and_c']:>16}"
    )
    print("-" * w)

    print("MONTHLY ROW COUNTS (original -> sample)")
    keys = sorted(set(orig["monthly"]) | set(samp["monthly"]))
    month_capped = stats.get("month_capped_to")
    for k in keys:
        o = orig["monthly"].get(k, 0)
        s = samp["monthly"].get(k, 0)
        note = ""
        ok = ""
        if k == TRAP_MONTH:
            if month_capped is None:
                ok = "OK" if s == o and o > 0 else "*** MISSING ***"
                note = "  <== incomplete month, kept in full"
            else:
                ok = "CAPPED"
                note = f"  <== incomplete month, capped at {month_capped:,} rows"
        print(
            f"  {k}  {o:>8,} -> {s:>8,}  ({_pct(s, o):>6})"
            f"{('  ' + ok) if ok else ''}{note}"
        )
    print("-" * w)

    # Explicit pass/fail gate: a sample that fails these is unusable as a demo.
    floor = stats.get("missing_effective_floor", MIN_MISSING_CUSTOMER)
    month_ok = (
        samp["trap_month"] >= orig["trap_month"]
        if month_capped is None
        else stats.get("must_keep_month", 0) >= min(month_capped, orig["trap_month"])
    )
    checks = [
        ("all Quantity<0 rows kept", samp["neg_qty"] >= orig["neg_qty"]),
        ("all C-invoice rows kept", samp["c_invoice"] >= orig["c_invoice"]),
        (
            f"all {TRAP_MONTH} rows kept" if month_capped is None
            else f"{TRAP_MONTH} capped sample present ({month_capped:,} rows)",
            month_ok,
        ),
        ("all UnitPrice<=0 rows kept", samp["unitprice_le0"] >= orig["unitprice_le0"]),
        (
            f"missing CustomerID >= floor ({floor:,}) or fully kept",
            samp["missing_cust"] >= min(floor, orig["missing_cust"])
            or samp["missing_cust"] >= orig["missing_cust"],
        ),
        ("sample preserves row order (chronological)", _is_sorted(sample_df)),
        (
            "date span reaches the source end"
            if month_capped is None
            else f"{TRAP_MONTH} cap spans all {stats.get('month_days', '?')} trading days",
            samp["date_max"] == orig["date_max"] if month_capped is None
            else samp["date_max"] >= stats.get("month_window_end", ""),
        ),
    ]
    # Only demand a random-row baseline when the budget could actually afford
    # one; a sample pinned to the must-keep floor legitimately has none.
    if not stats.get("covered_by_must_keep"):
        want_baseline = stats.get("baseline_effective", MIN_RANDOM_FILL)
        if want_baseline > 0:
            checks.append(
                (
                    f"random (non-trap) baseline >= {want_baseline:,}",
                    stats.get("random_fill", 0) >= want_baseline,
                )
            )
        else:
            checks.append(
                (
                    "sample is not 100% trap rows",
                    stats.get("random_fill", 0) > 0,
                )
            )
    print("TRAP PRESERVATION GATE")
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(line)
    if not all(ok for _, ok in checks):
        print("WARNING: at least one trap was NOT fully preserved - do not use this")
        print("         sample for demos until --rows is increased.")
        print(line)


def _pct(part: int, whole: int) -> str:
    if whole == 0:
        return "n/a"
    return f"{100.0 * part / whole:.1f}%"


def _is_sorted(sample_df) -> bool:
    """True when the sample is still in non-decreasing InvoiceDate order.

    The requirement is that sampling must not scramble time ordering; we keep
    source order, which is chronological, so this must hold.
    """
    if sample_df is None or len(sample_df) < 2:
        return True
    dt = pd.to_datetime(sample_df["InvoiceDate"])
    return bool(dt.is_monotonic_increasing)


def _largest_remainder(weights: list[int], total: int) -> list[int]:
    """Allocate ``total`` seats across ``weights`` using largest remainder.

    Every bucket with a non-zero weight gets at least one seat when ``total``
    allows it, and the result sums to exactly ``min(total, sum(weights))``.
    """
    n = len(weights)
    if n == 0 or total <= 0:
        return [0] * n
    wsum = sum(weights)
    if wsum <= 0:
        return [0] * n
    total = min(total, wsum)
    raw = [total * w / wsum for w in weights]
    seats = [int(x) for x in raw]
    # Guarantee a seat for every non-empty bucket, then claw back from the
    # largest allocations so the total still matches exactly.
    for i, w in enumerate(weights):
        if w > 0 and seats[i] == 0:
            seats[i] = 1
    while sum(seats) > total:
        j = max(range(n), key=lambda i: (seats[i], weights[i]))
        if seats[j] <= 1:
            break
        seats[j] -= 1
    # Hand out leftovers by largest fractional remainder.
    order = sorted(
        range(n), key=lambda i: (raw[i] - int(raw[i]), weights[i]), reverse=True
    )
    idx = 0
    while sum(seats) < total:
        i = order[idx % n]
        if seats[i] < weights[i]:
            seats[i] += 1
        idx += 1
        if idx > len(order) * (total + 1):
            break
    return seats


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fetch_demo_data.py",
        description=(
            "Download the UCI Online Retail dataset and write a trap-preserving "
            "CSV sample (all returns, all cancellations, the whole incomplete "
            "2011-12 month, proportional missing CustomerIDs)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python scripts/fetch_demo_data.py\n"
            "  python scripts/fetch_demo_data.py --rows 16769 --cap-month 2000 \\\n"
            "      --out data/processed/online_retail_small.csv\n"
            "  python scripts/fetch_demo_data.py --full --out data/processed/online_retail_full.csv\n"
            "\n"
            "note: keeping every 2011-12 row (25,525 of them) needs --rows >= ~39,916,\n"
            "      so smaller samples must pass --cap-month.\n"
            "\n"
            f"source: {SOURCE_PAGE}\n"
            f"cite  : {CITATION}\n"
            f"licence: {LICENCE}\n"
        ),
    )
    p.add_argument(
        "--full",
        action="store_true",
        help="do not sample; write every row of the source dataset",
    )
    p.add_argument(
        "--rows",
        type=int,
        default=DEFAULT_ROWS,
        metavar="N",
        help=f"target number of rows for the sample (default: {DEFAULT_ROWS:,})",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path("data") / "processed" / "online_retail.csv",
        metavar="PATH",
        help="output CSV path (default: data/processed/online_retail.csv)",
    )
    p.add_argument(
        "--raw-dir",
        type=Path,
        default=DEFAULT_RAW_DIR,
        metavar="DIR",
        help=f"download cache directory (default: {DEFAULT_RAW_DIR})",
    )
    p.add_argument(
        "--source",
        type=Path,
        default=None,
        metavar="XLSX",
        help="use an existing Online Retail.xlsx instead of downloading/extracting",
    )
    p.add_argument(
        "--url",
        default=DOWNLOAD_URL,
        metavar="URL",
        help="override the download URL (useful for mirrors)",
    )
    p.add_argument(
        "--force-download",
        action="store_true",
        help="re-download and re-extract even if the cache is populated",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"random seed for reproducibility (default: {DEFAULT_SEED})",
    )
    p.add_argument(
        "--small",
        action="store_true",
        help=(
            f"shorthand for --rows {SMALL_ROWS} (smallest sample that still keeps "
            "every trap; below this the 2011-12 month must be capped). Combine "
            "with --cap-month for a genuinely small file."
        ),
    )
    p.add_argument(
        "--cap-month",
        type=int,
        default=None,
        metavar="N",
        help=(
            f"keep only N day-stratified {TRAP_MONTH} rows instead of all "
            f"{25525:,}. Needed for any --rows below ~37k, because the complete "
            "month alone is larger than such a target. All returns, all "
            "cancellations and all UnitPrice<=0 rows are still kept in full."
        ),
    )
    p.add_argument(
        "--allow-over-target",
        action="store_true",
        help=(
            "when the must-keep trap rows exceed --rows, emit them anyway "
            "instead of failing (the output will be larger than --rows)"
        ),
    )
    p.add_argument(
        "--baseline",
        type=int,
        default=MIN_RANDOM_FILL,
        metavar="N",
        help=(
            "preferred number of plain (non-trap, CustomerID present) rows in the "
            f"sample, so the result is not 100%% trap rows (default: {MIN_RANDOM_FILL:,}). "
            "Automatically reduced when the budget is too small."
        ),
    )
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.small and args.rows == DEFAULT_ROWS:
        args.rows = SMALL_ROWS

    print("=" * 78)
    print("UCI Online Retail :: fetch + trap-preserving sample")
    print("=" * 78)
    print(f"source  : {SOURCE_PAGE}")
    print(f"cite    : {CITATION}")
    print(f"licence : {LICENCE} (attribution required)")
    print("-" * 78)

    # --- 1. obtain the xlsx ------------------------------------------------ #
    if args.source is not None:
        xlsx = args.source
        if not xlsx.exists():
            print(f"[error] --source does not exist: {xlsx.resolve()}", file=sys.stderr)
            return 2
        print(f"[source] using local file: {xlsx.resolve()}")
    else:
        raw_dir = args.raw_dir
        zip_path = raw_dir / "online+retail.zip"
        try:
            download_zip(args.url, zip_path, force=args.force_download)
            xlsx = extract_xlsx(zip_path, raw_dir, force=args.force_download)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if not xlsx.exists():
            print(
                f"[error] expected {xlsx.resolve()} after extraction but it is missing.\n"
                f"        Download '{XLSX_NAME}' manually from {SOURCE_PAGE}\n"
                f"        and place it at {xlsx.resolve()}.",
                file=sys.stderr,
            )
            return 1

    # --- 2. load ----------------------------------------------------------- #
    df = load_source(xlsx)

    # --- 3. sample --------------------------------------------------------- #
    if args.full:
        print("[sample] --full: keeping all rows, no sampling.")
        sample = df.copy()
        stats = {"target": len(df), "covered_by_must_keep": True, "must_keep": len(df),
                 "missing_quota": 0, "random_fill": 0, "month_capped_to": None}
        report_vs_source = True
    else:
        print(f"[sample] trap-preserving stratified sample, target {args.rows:,} rows, seed {args.seed}")
        try:
            sample, stats = stratify_sample(
                df,
                args.rows,
                seed=args.seed,
                cap_month=args.cap_month,
                allow_over_target=args.allow_over_target,
                baseline_rows=args.baseline,
            )
        except ValueError as exc:
            print(f"\n[error] cannot build a {args.rows:,}-row sample:\n    {exc}",
                  file=sys.stderr)
            return 2
        report_vs_source = len(sample) != len(df)

    # --- 4. sanity checks -------------------------------------------------- #
    if list(sample.columns) != EXPECTED_COLUMNS:
        print(
            f"[error] column mismatch: {list(sample.columns)} != {EXPECTED_COLUMNS}",
            file=sys.stderr,
        )
        return 1

    # --- 5. write + report ------------------------------------------------- #
    write_csv(sample, args.out)

    orig_metrics = compute_metrics(df, "original")
    samp_metrics = compute_metrics(sample, "sample")
    print_report(orig_metrics, samp_metrics, stats, sample_df=sample,
                 identical_to_source=not report_vs_source)

    print(f"[done] wrote {args.out.resolve()}")
    print(f"[cite] {CITATION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
