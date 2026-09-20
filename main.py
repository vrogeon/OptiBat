# This is a sample Python script.

import os
import requests
import time

import serial

# Define main parameters
battery_limit_high = 90
battery_limit_low = 10

battery_charge_value = 500
battery_discharge_value = 200




# Configurez le port série (ajustez '/dev/serial0', '/dev/ttyAMA0' ou 'COM3' selon votre installation)
SERIAL_PORT = "COM5"
BAUD_RATE = 9600  # 1200 bauds en mode historique, 9600 en mode standard

os.environ['SOBRY_API_KEY'] = "sob_apikey_ZhD3lHQho215asM7Ml5uKrT-ba3alThb5XjTNH4W624"

# Test curl:
#curl 'https://api.sobry.co/v2/user/daily-prices?taxMode=ht&granularity=15m' \
#  -H 'Authorization: Bearer sob_apikey_ZhD3lHQho215asM7Ml5uKrT-ba3alThb5XjTNH4W624'

def print_hi(name):
    # Use a breakpoint in the code line below to debug your script.
    print(f'Hi, {name}')  # Press Ctrl+F8 to toggle the breakpoint.

def set_power(power):
    # get_response = requests.get("http://192.168.1.144/properties/report")
    # print("GET:", get_response.text)
    #
    # post_response = requests.post("http://192.168.1.144/properties/write",
    #                               json={"sn": "EOC1NLNAN492379", "properties": {"acMode": 1}})
    # print("POST:", post_response.text)

    post_response = requests.post("http://192.168.1.144/properties/write",
                                  json={"sn": "EOC1NLNAN492379", "properties": {"outputLimit": 200}})
    print("Set power:", power)
    print("POST:", post_response.text)

def get_price():
    r = requests.get(
        'https://api.sobry.co/v2/user/daily-prices',
        params={
            'day': '2026-06-12',
            'granularity': '15m',  # '15m' | '1h'
            'taxMode': 'ttc',  # 'ht' | 'ttc'
        },
        headers={'Authorization': f"Bearer {os.environ['SOBRY_API_KEY']}"},
    )
    prices = r.json()
    print(prices)

# Variable globale pour stocker la dernière valeur de SINSTS
last_sinst = None
last_sinsti = None
power_value = 0

def get_sinst():
    """Retourne la dernière valeur de SINSTS lue."""
    return int(last_sinst)

def get_sinsti():
    """Retourne la dernière valeur de SINSTI lue."""
    return last_sinsti

def decode_teleinfo():
    global last_sinst  # Permet de modifier la variable globale
    global last_sinsti # Permet de modifier la variable globale

    try:
        ser = serial.Serial(
            SERIAL_PORT,
            BAUD_RATE,
            parity=serial.PARITY_EVEN,
            bytesize=7,
            stopbits=1,
            timeout=1
        )
        # print(f"Lecture de la TIC sur {SERIAL_PORT}...")

        while True:
            line = ser.readline()
            if line:
                try:
                    decoded_line = line.decode("ascii").strip()
                    if not decoded_line:
                        continue

                    parts = decoded_line.split()
                    if len(parts) < 2:
                        continue

                    etiquette = parts[0]
                    valeur = parts[1].replace(" ", "")
                    checksum = parts[2] if len(parts) > 2 else None

                    # Mise à jour de last_sinst si l'étiquette est SINSTS
                    if etiquette == "SINSTS":
                        last_sinst = valeur
                        # print(f"Nouvelle valeur SINSTS : {last_sinst}")
                        break
                    #
                    # # Mise à jour de last_sinsti si l'étiquette est SINSTI
                    # if etiquette == "SINSTI":
                    #     last_sinsti = valeur
                    #     print(f"Nouvelle valeur SINSTI : {last_sinsti}")

                    # Affichage optionnel de toutes les trames (pour débogage)
                    # print(f"{etiquette:<12} | {valeur}")

                except UnicodeDecodeError:
                    pass
                except Exception as e:
                    print(f"Erreur de traitement : {e}")

    except serial.SerialException as e:
        print(f"Erreur de port série : {e}")
    except KeyboardInterrupt:
        print("\nArrêt du script.")


# Press the green button in the gutter to run the script.
if __name__ == '__main__':


    #print_hi('PyCharm')

    set_power(power_value)

    #get_price()


    while True:
        decode_teleinfo()
        current_sinst = get_sinst()
        if current_sinst is not None:
            print(f"Puissance instantanée : {current_sinst} W")
            if current_sinst == 0:
                power_value = power_value - 10
            else:
                power_value = current_sinst + power_value
            set_power(power_value)


        time.sleep(1)  # Rafraîchissement toutes les secondes





