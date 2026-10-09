# This is a sample Python script.

import time

import prices
import battery
import consumption
import gateway

# Test curl:
#curl 'https://api.sobry.co/v2/user/daily-prices?taxMode=ht&granularity=15m' \
#  -H 'Authorization: Bearer sob_apikey_ZhD3lHQho215asM7Ml5uKrT-ba3alThb5XjTNH4W624'

# Variable globale pour stocker la dernière valeur de SINSTS

power_value = 0

# Press the green button in the gutter to run the script.
if __name__ == '__main__':
    # prices.get_price()

    # battery.set_power(power_value)

    while True:
        gateway.decode_teleinfo()
        # current_sinst = get_sinst()
        # if current_sinst is not None:
        #     print(f"Puissance instantanée : {current_sinst} W")
        #     if current_sinst == 0:
        #         power_value = power_value - 10
        #     else:
        #         power_value = current_sinst + power_value
        #     battery.set_power(power_value)


        time.sleep(1)  # Rafraîchissement toutes les secondes





