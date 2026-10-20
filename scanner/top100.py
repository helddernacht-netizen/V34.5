"""
TOP100 Universe - Holt Top Vol Coins für Turtles
"""
import requests

def get_top100_universe():
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=volume_desc&per_page=100&page=1"
        r = requests.get(url, timeout=10)
        data = r.json()
        if isinstance(data, list):
            return [f"{d['symbol'].upper()}USDT" for d in data if d.get('symbol')]
    except Exception as e:
        print(f"top100 fail {e}")
    # Fallback
    return [
        "WIFUSDT","PEPEUSDT","BONKUSDT","FLOKIUSDT","POPCATUSDT","SHIBUSDT","DOGEUSDT",
        "HYPEUSDT","SOLUSDT","AVAXUSDT","LINKUSDT","ARBUSDT","OPUSDT","APTUSDT","SUIUSDT"
    ]*7  # mock 100

if __name__ == "__main__":
    print(get_top100_universe()[:10])
