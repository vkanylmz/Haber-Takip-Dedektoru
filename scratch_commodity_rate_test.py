import time
import asyncio
from src.commodity_report import build_weekly_commodity_report_data, _fetch_company_quotes
from src.web.market_data import _refresh_market_data_cache, _yahoo_in_cooldown

def simulate_system_load(interval_seconds: int, iterations: int):
    print(f"\n--- Test starting: Interval = {interval_seconds}s | Iterations = {iterations} ---")
    for i in range(iterations):
        t0 = time.time()
        print(f"Iteration {i+1}/{iterations} starting...")
        
        if _yahoo_in_cooldown():
            print("❌ Yahoo is already in cooldown (429). Test failed at interval", interval_seconds)
            return False
            
        print("  -> Fetching Market Ticker Data (async run)")
        asyncio.run(_refresh_market_data_cache())
        
        print("  -> Fetching Commodity Histories (sync wrapper)")
        # This function fetches all commodities (and it calls asyncio.run inside)
        build_weekly_commodity_report_data(summarizer=None)
        
        duration = time.time() - t0
        print(f"Iteration {i+1} completed in {duration:.2f}s")
        
        if _yahoo_in_cooldown():
            print(f"❌ 429 Too Many Requests hit during iteration {i+1} at interval {interval_seconds}s!")
            return False
            
        sleep_time = interval_seconds - duration
        if sleep_time > 0 and i < iterations - 1:
            print(f"  -> Sleeping for {sleep_time:.2f}s...")
            time.sleep(sleep_time)

    print(f"✅ Success! Interval {interval_seconds}s is safe for {iterations} iterations.")
    return True

def main():
    intervals_to_test = [60, 45] 
    for interval in intervals_to_test:
        success = simulate_system_load(interval, iterations=2)
        if not success:
            print("Failed! Exiting.")
            break
        print("Waiting 10 seconds before next test...")
        time.sleep(10)

if __name__ == "__main__":
    main()
