import serial
import requests

# Define battery parameters
battery_limit_high = 90
battery_limit_low = 10

battery_charge_value = 500
battery_discharge_value = 200


def set_power(power):
    # get_response = requests.get("http://192.168.1.144/properties/report")
    # print("GET:", get_response.text)
    #
    # post_response = requests.post("http://192.168.1.144/properties/write",
    #                               json={"sn": "EOC1NLNAN492379", "properties": {"acMode": 1}})
    # print("POST:", post_response.text)

    post_response = requests.post("http://192.168.1.144/properties/write",
                                  json={"sn": "EOC1NLNAN492379", "properties": {"outputLimit": power}})
    print("Set power:", power)
    print("POST:", post_response.text)