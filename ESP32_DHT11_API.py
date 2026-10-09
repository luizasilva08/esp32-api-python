import requests

url = "http://10.185.93.240/api/dados"


def consultar_esp32():
    resposta = requests.get(url)

    if resposta.status_code == 200:
        print("Deu certo!")
        json = resposta.json()
        temperatura = json["temperatura"]
        umidade = json["umidade"]
        print(f"Temperatura: {temperatura} °C")
        print(f"Umidade: {umidade} %")
        return json

    else:
        print(f"Deu ruim... Código: {resposta.status_code}")


consultar_esp32()
