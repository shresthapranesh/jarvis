"""Financial data tools: stock metrics, historical prices, earnings, and news."""

import asyncio


def _yf():
    try:
        import yfinance as yf  # noqa: PLC0415
    except ImportError as e:
        raise RuntimeError("Finance tools not available in this build (yfinance excluded).") from e
    return yf


async def get_stock_data(ticker: str) -> str:
    """Get current stock price, info, and recent financials for a ticker symbol."""
    def _sync() -> str:
        yf = _yf()
        stock = yf.Ticker(ticker)
        info = stock.info

        price = info.get("currentPrice") or info.get("regularMarketPrice", "N/A")
        name = info.get("longName", ticker)
        sector = info.get("sector", "N/A")
        market_cap = info.get("marketCap", "N/A")
        pe_ratio = info.get("trailingPE", "N/A")
        week_high = info.get("fiftyTwoWeekHigh", "N/A")
        week_low = info.get("fiftyTwoWeekLow", "N/A")
        summary = info.get("longBusinessSummary", "N/A")[:500]

        return (
            f"Company: {name}\n"
            f"Ticker: {ticker.upper()}\n"
            f"Sector: {sector}\n"
            f"Current Price: {price}\n"
            f"Market Cap: {market_cap}\n"
            f"P/E Ratio: {pe_ratio}\n"
            f"52-Week High: {week_high}\n"
            f"52-Week Low: {week_low}\n"
            f"Summary: {summary}"
        )

    return await asyncio.to_thread(_sync)


async def get_historical_prices(ticker: str, period: str = "1mo") -> str:
    """Get historical OHLCV price data for a ticker. Period examples: 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y."""
    def _sync() -> str:
        yf = _yf()
        stock = yf.Ticker(ticker)
        hist = stock.history(period=period)
        if hist.empty:
            return f"No historical data found for {ticker}."
        lines = [f"Historical prices for {ticker.upper()} ({period}):"]
        lines.append(f"{'Date':<12} {'Open':>8} {'High':>8} {'Low':>8} {'Close':>8} {'Volume':>12}")
        for date, row in hist.iterrows():
            lines.append(
                f"{str(date.date()):<12} {row['Open']:>8.2f} {row['High']:>8.2f} "
                f"{row['Low']:>8.2f} {row['Close']:>8.2f} {int(row['Volume']):>12,}"
            )
        return "\n".join(lines)

    return await asyncio.to_thread(_sync)


async def get_earnings(ticker: str) -> str:
    """Get upcoming and recent earnings dates and EPS estimates for a ticker."""
    def _sync() -> str:
        yf = _yf()
        stock = yf.Ticker(ticker)
        cal = stock.calendar
        earnings_dates = stock.earnings_dates

        lines = [f"Earnings data for {ticker.upper()}:"]

        if cal:
            lines.append("\nCalendar:")
            lines.append(str(cal))

        if earnings_dates is not None and not earnings_dates.empty:
            lines.append("\nRecent/Upcoming Earnings Dates (up to 8):")
            lines.append(earnings_dates.head(8).to_string())

        if len(lines) == 1:
            return f"No earnings data found for {ticker}."

        return "\n".join(lines)

    return await asyncio.to_thread(_sync)


async def compare_stocks(tickers: list[str]) -> str:
    """Fetch key metrics for multiple tickers and return a side-by-side comparison."""
    def _sync() -> str:
        yf = _yf()
        rows = []
        for ticker in tickers:
            info = yf.Ticker(ticker).info
            rows.append({
                "Ticker": ticker.upper(),
                "Name": info.get("longName", "N/A"),
                "Price": info.get("currentPrice") or info.get("regularMarketPrice", "N/A"),
                "Market Cap": info.get("marketCap", "N/A"),
                "P/E": info.get("trailingPE", "N/A"),
                "52W High": info.get("fiftyTwoWeekHigh", "N/A"),
                "52W Low": info.get("fiftyTwoWeekLow", "N/A"),
            })

        col_widths = {k: max(len(k), max(len(str(r[k])) for r in rows)) for k in rows[0]}
        header = "  ".join(k.ljust(col_widths[k]) for k in col_widths)
        divider = "  ".join("-" * col_widths[k] for k in col_widths)
        data_rows = [
            "  ".join(str(r[k]).ljust(col_widths[k]) for k in col_widths)
            for r in rows
        ]
        return "\n".join([header, divider] + data_rows)

    return await asyncio.to_thread(_sync)


async def get_ticker_news(ticker: str) -> str:
    """Get recent news headlines and URLs for a ticker from Yahoo Finance."""
    def _sync() -> str:
        yf = _yf()
        stock = yf.Ticker(ticker)
        news = stock.news
        if not news:
            return f"No news found for {ticker}."
        items = []
        for article in news[:10]:
            title = article.get("title", "No title")
            link = article.get("link", "")
            publisher = article.get("publisher", "")
            items.append(f"- {title}\n  Source: {publisher}\n  URL: {link}")
        return f"Recent news for {ticker.upper()}:\n\n" + "\n\n".join(items)

    return await asyncio.to_thread(_sync)
