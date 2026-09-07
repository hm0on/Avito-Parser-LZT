import requests
import json
import os

API_KEY = os.environ.get("SX_PROXY_API_KEY", "").strip()
if not API_KEY:
    raise SystemExit("Set SX_PROXY_API_KEY in the environment before running this script")

url = f"https://api.sx.org/v2/proxy/create-port?apiKey={API_KEY}"
payload = {
    "country_code": "RU",       # код страны
    "type_id": 3,               # тип прокси (возможно у вас другое значение)
    "proxy_type_id": 1,         # http/socks
    "name": "My Proxy",
    "server_port_type_id": 1,
    "count": 10                 # сколько портов создать
}

headers = {
    "Content-Type": "application/json",
    "Accept": "application/json"
}

response = requests.post(url, headers=headers, json=payload)

# проверяем, что запрос успешен
if response.status_code != 200:
    print("Ошибка API:", response.status_code, response.text)
    exit()

data = response.json()

# Проверяем успешность
if data.get("success"):

    for p in data.get("data", []):
        login = p.get("login")        # логин
        password = p.get("password")  # пароль
        server = p.get("server")      # сервер (IP)
        port = p.get("port")          # порт

        if login and password and server and port:
            # Формируем строку прокси в нужном формате
            proxy_str = f"http://{login}:{password}@{server}:{port}"
            print(proxy_str)
        else:
            print("Не удалось распарсить порт:", p)
else:
    print("Ошибка в данных ответа:", data)
