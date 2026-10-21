import os
import requests

def get_price():
    r = requests.get(
        'https://api.sobry.co/v2/user/daily-prices',
        params={
            'day': '2026-09-20',
            'granularity': '15m',  # '15m' | '1h'
            'taxMode': 'ttc',  # 'ht' | 'ttc'
        },
        headers={'Authorization': f"Bearer {os.environ['SOBRY_API_KEY']}"},
    )
    prices = r.json()
    print(prices)