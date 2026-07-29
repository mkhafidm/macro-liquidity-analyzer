"""
Fetches and formats live market prices for macro liquidity tracking.

Design notes:
- NYSE-tied assets (SP500, Nasdaq, VIX) follow the NYSE trading calendar.
- Futures/FX assets (DXY, US10Y, Gold, Crude_Oil) trade nearly 24/6, closed
  only during the Fri 17:00 ET -> Sun 18:00 ET weekend gap. This does not
  account for futures-specific holidays (e.g. Thanksgiving early close),
  only the weekly weekend gap.
- BTC trades 24/7.
"""

import math
import logging
from datetime import datetime, timedelta

import pytz
import pandas_market_calendars as mcal
import yfinance as yf

from config import (
    MARKET_ASSETS,
    NYSE_TIED_ASSETS,
    FUTURES_FX_ASSETS,
    DOLLAR_VOLUME_ASSETS,
    MARKET_HISTORY_PERIOD,
    MARKET_HISTORY_INTERVAL,
)

logger = logging.getLogger(__name__)

EASTERN = pytz.timezone("US/Eastern")
JAKARTA = pytz.timezone("Asia/Jakarta")


def check_market_status():
    """Check NYSE trading status and resolve the target trading date."""
    now_et = datetime.now(EASTERN)
    now_wib = datetime.now(JAKARTA)

    nyse = mcal.get_calendar("NYSE")
    schedule_today = nyse.schedule(start_date=now_et.date(), end_date=now_et.date())

    past_schedule = nyse.schedule(
        start_date=(now_et - timedelta(days=7)).date(), end_date=now_et.date()
    )
    last_valid_trading_date = past_schedule.index[-1].strftime("%Y-%m-%d")
    today_date_str = now_et.strftime("%Y-%m-%d")

    if schedule_today.empty:
        is_open = False
        market_open_et = market_close_et = None
        target_trading_date = last_valid_trading_date
    else:
        market_open_et = schedule_today.iloc[0]["market_open"].tz_convert(EASTERN)
        market_close_et = schedule_today.iloc[0]["market_close"].tz_convert(EASTERN)
        is_open = market_open_et <= now_et <= market_close_et

        # Before today's open, "today's data" isn't available yet -> fall back
        # to the previous valid trading day.
        if now_et < market_open_et:
            target_trading_date = (
                past_schedule.index[-2].strftime("%Y-%m-%d")
                if len(past_schedule) >= 2
                else last_valid_trading_date
            )
        else:
            target_trading_date = today_date_str

    status_lines = [
        f"US MARKET STATUS (NYSE) : {'OPEN' if is_open else 'CLOSED (Holiday/Weekend)'}",
        f"New York Time            : {now_et.strftime('%A, %d %b %Y - %H:%M:%S %Z')}",
        f"Jakarta Time             : {now_wib.strftime('%A, %d %b %Y - %H:%M:%S %Z')}",
        f"Target Trade Date        : {target_trading_date}",
    ]

    if market_open_et is not None:
        open_wib = market_open_et.astimezone(JAKARTA)
        close_wib = market_close_et.astimezone(JAKARTA)
        status_lines.append(
            f"NYSE Opens               : {market_open_et.strftime('%H:%M')} ET  =  {open_wib.strftime('%H:%M')} WIB"
        )
        status_lines.append(
            f"NYSE Closes              : {market_close_et.strftime('%H:%M')} ET  =  {close_wib.strftime('%H:%M')} WIB"
        )

    logger.info("\n" + "\n".join(status_lines))

    return is_open, target_trading_date


def check_futures_fx_status(now_et: datetime) -> bool:
    """
    Futures/FX (DXY, Gold, Crude Oil, US10Y bond futures) trade nearly 24/6:
    open from Sunday 18:00 ET through Friday 17:00 ET, closed only for the
    weekend gap. Futures-specific holidays are not accounted for.
    """
    weekday = now_et.weekday()  # Monday=0 ... Sunday=6
    hour = now_et.hour

    if weekday == 5:  # Saturday -> always closed
        return False
    if weekday == 6 and hour < 18:  # Sunday before 18:00 ET -> still closed
        return False
    if weekday == 4 and hour >= 17:  # Friday after 17:00 ET -> already closed
        return False
    return True


def fetch_market_price(period: str = MARKET_HISTORY_PERIOD, interval: str = MARKET_HISTORY_INTERVAL) -> dict:
    """Fetch historical price/volume data for all tracked assets via yfinance."""
    logger.info("Fetching live macro liquidity tickers...")

    raw_close_dict = {}
    raw_volume_dict = {}
    metadata_dict = {}

    now_et = datetime.now(EASTERN)
    nyse_market_open, last_valid_trading_date = check_market_status()
    futures_fx_open = check_futures_fx_status(now_et)

    logger.info("Futures/FX Session       : %s", "OPEN" if futures_fx_open else "CLOSED (Weekend gap)")

    for name, ticker in MARKET_ASSETS.items():
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period=period, interval=interval)

            if hist.empty or len(hist) < 2:
                logger.warning("Insufficient historical data for %s", name)
                metadata_dict[name] = {"market_status": "NO_DATA", "fetch_date": "N/A"}
                continue

            hist.index = hist.index.strftime("%Y-%m-%d")

            # yfinance sometimes returns US10Y in basis points instead of percent.
            if name == "US10Y" and hist["Close"].iloc[-1] > 10.0:
                hist["Close"] = hist["Close"] / 10.0

            # --- Resolve which bar represents "current" for this asset category ---
            if name == "BTC":
                target_idx = -1
                status_label = "OPEN (24/7)"

            elif name in FUTURES_FX_ASSETS:
                if futures_fx_open:
                    target_idx = -1
                    status_label = "OPEN (Live, Futures/FX session)"
                else:
                    # Last available bar = last session before the weekend gap.
                    target_idx = -1
                    status_label = f"CLOSED (Weekend gap, Last: {hist.index[-1]})"

            elif name in NYSE_TIED_ASSETS:
                if nyse_market_open:
                    target_idx = -1
                    status_label = "OPEN (Live)"
                elif last_valid_trading_date in hist.index and not math.isnan(
                    hist.loc[last_valid_trading_date, "Close"]
                ):
                    target_idx = hist.index.get_loc(last_valid_trading_date)
                    status_label = f"CLOSED (Last Close: {last_valid_trading_date})"
                else:
                    # Fallback: walk backwards to the most recent non-NaN close
                    # at or before the target trading date.
                    valid_dates = [
                        d for d in hist.index
                        if d <= last_valid_trading_date and not math.isnan(hist.loc[d, "Close"])
                    ]
                    if valid_dates:
                        fallback_date = valid_dates[-1]
                        target_idx = hist.index.get_loc(fallback_date)
                        status_label = f"CLOSED (Last Close: {fallback_date})"
                    else:
                        target_idx = -1
                        status_label = "CLOSED (Fallback)"
            else:
                target_idx = -1
                status_label = "UNKNOWN"

            # Trim rows after the target index (avoids leaking future/NaN bars)
            # unless the target is already the last row.
            if target_idx == -1 or target_idx == len(hist) - 1:
                valid_hist = hist.copy()
            else:
                valid_hist = hist.iloc[: target_idx + 1].copy()

            if not valid_hist.empty and len(valid_hist) >= 2:
                raw_close_dict[name] = valid_hist["Close"]
                if "Volume" in valid_hist.columns and not valid_hist["Volume"].empty:
                    raw_volume_dict[name] = valid_hist["Volume"]
                metadata_dict[name] = {
                    "fetch_date": valid_hist.index[-1],
                    "market_status": status_label,
                }
            else:
                logger.warning("Processed history is empty for %s", name)
                metadata_dict[name] = {"market_status": "NO_DATA", "fetch_date": "N/A"}

        except Exception as e:
            logger.error("Error fetching %s: %s", name, e)
            metadata_dict[name] = {"market_status": "ERROR", "fetch_date": "N/A"}

    return {
        "close_series": raw_close_dict,
        "volume_series": raw_volume_dict,
        "metadata": metadata_dict,
    }


def _format_volume(vol: float, use_dollar: bool = True) -> str:
    prefix = "$" if use_dollar else ""
    if vol >= 1e9:
        return f"{prefix}{vol / 1e9:.2f}B"
    elif vol >= 1e6:
        return f"{prefix}{vol / 1e6:.2f}M"
    return f"{prefix}{vol:,.0f}"


def format_market_report(raw_data: dict) -> dict:
    """Turn raw historical data into a clean per-asset summary + log table."""
    close_series = raw_data["close_series"]
    volume_series = raw_data["volume_series"]
    metadata = raw_data["metadata"]

    report_data = {}

    for name, prices in close_series.items():
        if prices.empty or len(prices) < 2:
            continue

        latest_price = float(prices.iloc[-1])
        prev_price = float(prices.iloc[-2])
        price_20d_ago = float(prices.iloc[-20]) if len(prices) >= 20 else prev_price

        pct_change_1d = ((latest_price - prev_price) / prev_price) * 100.0
        pct_change_20d = ((latest_price - price_20d_ago) / price_20d_ago) * 100.0

        volume_str = "-"
        if name in volume_series and not volume_series[name].empty:
            vol = float(volume_series[name].iloc[-1])
            if vol > 0:
                volume_str = _format_volume(vol, use_dollar=(name in DOLLAR_VOLUME_ASSETS))
                if name in {"SP500", "Nasdaq"} and volume_str != "-":
                    volume_str += " shares"

        report_data[name] = {
            "price": round(latest_price, 2),
            "prev_price": round(prev_price, 2),
            "change_1d_pct": round(pct_change_1d, 2),
            "change_20d_pct": round(pct_change_20d, 2),
            "volume": volume_str,
            "fetch_date": metadata[name]["fetch_date"],
            "market_status": metadata[name]["market_status"],
        }

    # Assets that failed to fetch (NO_DATA/ERROR) are kept as placeholders so
    # the downstream AI prompt sees an explicit failure, not a silently
    # missing asset.
    for name, meta in metadata.items():
        if name not in report_data:
            report_data[name] = {
                "price": None,
                "prev_price": None,
                "change_1d_pct": None,
                "change_20d_pct": None,
                "volume": "-",
                "fetch_date": meta.get("fetch_date", "N/A"),
                "market_status": meta.get("market_status", "NO_DATA"),
            }

    # Build the summary table as one string so log lines don't get
    # timestamp/level prefixes injected between every row.
    lines = [
        "=" * 96,
        " MACRO LIQUIDITY REPORT (PHASE 1)",
        "=" * 96,
        f" {'ASSET':<10} | {'PRICE':<10} | {'PREV':<10} | {'1D CHG (%)':<10} | "
        f"{'20D CHG (%)':<11} | {'VOLUME':<10} | {'STATUS':<35}",
        "-" * 96,
    ]

    for name, item in report_data.items():
        if item["price"] is None:
            lines.append(
                f" {name:<10} | {'N/A':<10} | {'N/A':<10} | {'N/A':<10} | "
                f"{'N/A':<11} | {'-':<10} | {item['market_status']:<35}"
            )
            continue
        chg_1d_str = f"{item['change_1d_pct']:+.2f}%"
        chg_20d_str = f"{item['change_20d_pct']:+.2f}%"
        lines.append(
            f" {name:<10} | {item['price']:<10.2f} | {item['prev_price']:<10.2f} | "
            f"{chg_1d_str:<10} | {chg_20d_str:<11} | {item['volume']:<10} | {item['market_status']:<35}"
        )

    lines.append("=" * 96)
    logger.info("\n" + "\n".join(lines))

    return report_data


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    raw_data = fetch_market_price()
    report = format_market_report(raw_data)

    print("\n[REPORT DICT]")
    for name, item in report.items():
        print(f"   {name}: {item}")