import serial
import time
import threading
from flask import Flask, jsonify

app = Flask(__name__)

# CONFIGURAÇÃO DA PORTA SERIAL
# ⚠️ IMPORTANTE: Altere 'COM3' para a porta real do seu ESP32 (ex: COM4, COM5...)
PORTA = 'COLOQUE A PORTA UTILIZADA'  
BAUD_RATE = 115200

# Variáveis para guardar as leituras do cabo USB
dados_globais = {
    "temperatura": 0.0,
    "umidade": 0.0,
    "status": "Aguardando dados..."
}

def ler_porta_serial():
    """Roda em segundo plano lendo o cabo USB continuamente"""
    global dados_globais
    try:
        # Abre a conexão com o ESP32
        esp32 = serial.Serial(PORTA, BAUD_RATE, timeout=1)
        time.sleep(2)  # Aguarda a placa estabilizar
        print(f" Conectado com sucesso ao ESP32 na porta {PORTA}!")
        
        while True:
            if esp32.in_waiting > 0:
                # Lê a linha vinda do cabo USB e limpa os espaços
                linha = esp32.readline().decode('utf-8').strip()
                
                if not linha:
                    continue
                    
                if 'erro' in linha:
                    dados_globais["status"] = "Erro na leitura do sensor DHT"
                    continue
                
                if ',' in linha:
                    try:
                        # Separa o texto antes e depois da vírgula
                        dados = linha.split(',')
                        
                        # Correção aqui: extrai a posição 0 e 1 da lista
                        dados_globais["temperatura"] = float(dados[0])
                        dados_globais["umidade"] = float(dados[1])
                        dados_globais["status"] = "OK"
                        
                        # Mostra no terminal do VS Code para você ver que está funcionando
                        print(f"[Cabo USB] Temp: {dados_globais['temperatura']}°C | Umid: {dados_globais['umidade']}%")
                        
                    except (ValueError, IndexError):
                        pass
                        
            time.sleep(0.1)
            
    except serial.SerialException:
        dados_globais["status"] = f"Erro: Não foi possível abrir a porta {PORTA}"
        print(f"\n❌ ERRO: Não consegui abrir a porta {PORTA}.")
        print("-> Verifique se o Monitor Serial da Arduino IDE está FECHADO.")
        print("-> Verifique se o número da porta COM está correto no código.")

# =====================================================
# ROTA DA API (Acessada pelo seu navegador)
# =====================================================
@app.route('/api/dados', methods=['GET'])
def handle_dados():
    if dados_globais["status"] != "OK" and dados_globais["temperatura"] == 0.0:
        return jsonify({"erro": dados_globais["status"]}), 500
        
    return jsonify({
        "temperatura": dados_globais["temperatura"],
        "umidade": dados_globais["umidade"]
    })

if __name__ == '__main__':
    # Inicia a leitura do cabo USB em segundo plano
    thread_serial = threading.Thread(target=ler_porta_serial, daemon=True)
    thread_serial.start()
    
    # Inicia o servidor da API no seu computador na porta 5000
    print("\nIniciando o servidor local...")
    print("Acesse no seu navegador: http://localhost:5000/api/dados\n")
    app.run(host='0.0.0.0', port=5000, debug=False)
