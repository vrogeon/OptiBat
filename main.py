# This is a sample Python script.

import os
import requests

import serial

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

def set_power():
    get_response = requests.get("http://192.168.1.144/properties/report")
    print("GET:", get_response.text)

    post_response = requests.post("http://192.168.1.144/properties/write",
                                  json={"sn": "EOC1NLNAN492379", "properties": {"acMode": 1}})
    print("POST:", post_response.text)

    post_response = requests.post("http://192.168.1.144/properties/write",
                                  json={"sn": "EOC1NLNAN492379", "properties": {"outputLimit": 200}})
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

def decode_teleinfo():
  try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, parity=serial.PARITY_EVEN, bytesize=7, stopbits=1, timeout=1)
    print(f"Lecture de la TIC sur {SERIAL_PORT}...")

    while True:
      line = ser.readline()
      if line:
        try:
          # Décodage de la ligne (suppression des caractères de contrôle et du checksum)
          decoded_line = line.decode("ascii").strip()
          parts = decoded_line.split(" ")

          if len(parts) >= 2:
            etiquette = parts[0]
            valeur = parts[1]
            # Le 3e élément (si présent) est le checksum (clé de contrôle)
            checksum = parts[2] if len(parts) > 2 else ""

            print(f"Étiquette : {etiquette:<10} | Valeur : {valeur}")

        except UnicodeDecodeError:
          # Ignore les erreurs de décodage si la trame est bruitée
          pass

  except serial.SerialException as e:
    print(f"Erreur de port série : {e}")
  except KeyboardInterrupt:
    print("\nArrêt du script.")


# Press the green button in the gutter to run the script.
if __name__ == '__main__':
    #print_hi('PyCharm')

    #set_power()

    #get_price()

    decode_teleinfo()





