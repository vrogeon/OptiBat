import os
import requests

os.environ['SOBRY_API_KEY'] = "sob_apikey_ZhD3lHQho215asM7Ml5uKrT-ba3alThb5XjTNH4W624"

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