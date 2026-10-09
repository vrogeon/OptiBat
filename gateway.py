import serial

# Configurez le port série (ajustez '/dev/serial0', '/dev/ttyAMA0' ou 'COM3' selon votre installation)
SERIAL_PORT = "COM5"
BAUD_RATE = 9600  # 1200 bauds en mode historique, 9600 en mode standard

last_sinst = None
last_sinsti = None

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
                        print(f"Nouvelle valeur SINSTS : {last_sinst}")
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