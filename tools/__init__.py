from tools.code import run_python
from tools.datetime import get_current_datetime
from tools.files import list_files, read_file, write_file
from tools.finance import (
    compare_stocks,
    get_earnings,
    get_historical_prices,
    get_stock_data,
    get_ticker_news,
)
from tools.web import extract_links, fetch_page, web_search

TOOLS = [
    get_current_datetime,
    run_python,
    web_search,
    fetch_page,
    extract_links,
    get_stock_data,
    get_historical_prices,
    get_earnings,
    compare_stocks,
    get_ticker_news,
    write_file,
    read_file,
    list_files,
]
