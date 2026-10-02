import time
import json
import requests

# === CONFIGURAÇÕES ===
# Substitua pelos dados do seu Bot do Telegram
TELEGRAM_TOKEN = "SEU_TELEGRAM_TOKEN_AQUI"
TELEGRAM_CHAT_ID = "SEU_TELEGRAM_CHAT_ID_AQUI"

INTERVALO_SEGUNDOS = 600

# Substitua pelas suas credenciais e identificadores do Lyceum ( USUARIO_LYCEUM É A SUA SENHA)
ID_PESSOA = "SEU_ID_PESSOA_AQUI"
USUARIO_LYCEUM = "SEU_USUARIO_LYCEUM_AQUI"

# COLE AQUI O SEU COOKIE MAIS RECENTE COPIADO DO NAVEGADOR
COOKIE_SESSAO = "SEU_COOKIE_DE_SESSAO_AQUI"

ARQUIVO_NOTAS_LOCAL = "notas_salvas.json"
LAST_UPDATE_ID = None

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Referer": "https://uemg.lyceum.com.br/aluno/",
    "x-lyceum-usuario": f"aluno: {USUARIO_LYCEUM}",
    "Cookie": COOKIE_SESSAO
})

def enviar_mensagem_telegram(texto):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": texto,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Erro ao conectar com a API do Telegram: {e}")

def buscar_notas_portal():
    url = f"https://uemg.lyceum.com.br/aluno/apix/pessoas/{ID_PESSOA}/alunos/{USUARIO_LYCEUM}/qtddNotas/null/ultimasNotas"
    
    try:
        resposta = session.get(url, timeout=(5, 6))
    except requests.exceptions.Timeout:
        raise Exception("O servidor do Lyceum demorou para responder (Timeout).")
    except requests.exceptions.RequestException as e:
        raise Exception(f"Falha de conexão com o Lyceum: {e}")

    if resposta.status_code in [401, 403]:
        raise Exception("Sessão expirada. Atualize o COOKIE_SESSAO no bot.py.")
        
    if resposta.status_code != 200:
        raise Exception(f"Erro HTTP {resposta.status_code} no servidor do Lyceum.")
        
    # Proteção: Se o Lyceum retornar a página HTML de login, a sessão caiu
    content_type = resposta.headers.get("Content-Type", "")
    if "application/json" not in content_type:
        raise Exception("Sessão expirada (O servidor retornou HTML de login em vez de JSON).")
        
    try:
        dados = resposta.json()
    except Exception:
        raise Exception("Erro ao processar dados das notas. Cookie inválido ou expirado.")

    notas_atuais = {}
    for item in dados:
        materia = item.get("nomeDisciplina", "Disciplina Desconhecida")
        prova = item.get("nomeProva", "Avaliação")
        nota_valor = item.get("nota", "N/A")
        chave_unica = f"{materia} ({prova})"
        notas_atuais[chave_unica] = nota_valor
        
    return notas_atuais

def carregar_notas_salvas():
    try:
        with open(ARQUIVO_NOTAS_LOCAL, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def salvar_notas(notas):
    with open(ARQUIVO_NOTAS_LOCAL, "w", encoding="utf-8") as f:
        json.dump(notas, f, indent=4, ensure_ascii=False)

def exibir_ultimas_notas_salvas():
    """Lê o banco local e envia a lista completa das últimas notas salvas."""
    notas = carregar_notas_salvas()
    if not notas:
        enviar_mensagem_telegram("⚠️ *Nenhum registro local de notas.* Execute `/checar` para realizar a primeira busca.")
        return

    mensagem = "📊 *ÚLTIMO REGISTRO DE NOTAS SALVO*\n\n"
    for avaliacao, nota in notas.items():
        mensagem += f"• *{avaliacao}:* `{nota}`\n"
        
    enviar_mensagem_telegram(mensagem)

def executar_varredura_manual():
    enviar_mensagem_telegram("🔍 *Consultando o portal do Lyceum agora...*")
    try:
        notas_novas = buscar_notas_portal()
        notas_antigas = carregar_notas_salvas()
        
        alteracao_encontrada = False
        
        if not notas_antigas:
            salvar_notas(notas_novas)
            enviar_mensagem_telegram("✅ *Mapeamento inicial de notas concluído e salvo!*")
        else:
            for chave, nota in notas_novas.items():
                if chave not in notas_antigas:
                    enviar_mensagem_telegram(f"🚨 *NOVA NOTA DETECTADA!*\n\n*Avaliação:* {chave}\n*Nota:* {nota}")
                    alteracao_encontrada = True
                elif notas_antigas[chave] != nota:
                    enviar_mensagem_telegram(f"📢 *NOTA ALTERADA!*\n\n*Avaliação:* {chave}\n*De:* {notas_antigas[chave]}\n*Para:* {nota}")
                    alteracao_encontrada = True
            
            salvar_notas(notas_novas)
            
            if not alteracao_encontrada:
                enviar_mensagem_telegram("✅ *Nenhuma nota nova ou alterada encontrada no momento.*")
                
    except Exception as e:
        enviar_mensagem_telegram(f"⚠️ *Erro na consulta:* {e}")

def checar_comandos_telegram():
    global LAST_UPDATE_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {"timeout": 1, "offset": LAST_UPDATE_ID}
    
    try:
        resposta = requests.get(url, params=params, timeout=5)
        if resposta.status_code == 200:
            dados = resposta.json()
            for resultado in dados.get("result", []):
                LAST_UPDATE_ID = resultado["update_id"] + 1
                mensagem = resultado.get("message", {}).get("text", "").strip().lower()
                
                if mensagem in ["/ping", "/status", "/on", "ping", "status"]:
                    enviar_mensagem_telegram("🟢 *Bot Online!* Servidor Termux ativo.")
                elif mensagem in ["/checar", "/verificar", "checar", "verificar"]:
                    executar_varredura_manual()
                elif mensagem in ["/ultimanota", "/notas", "ultimanota", "notas"]:
                    exibir_ultimas_notas_salvas()
                elif mensagem in ["/ajuda", "/help"]:
                    enviar_mensagem_telegram(
                        "🤖 *Comandos Disponíveis:*\n\n"
                        "`/status` - Verifica se o servidor está online\n"
                        "`/checar` - Força uma consulta imediata às notas no Lyceum\n"
                        "`/notas` - Exibe a lista completa das notas guardadas"
                    )
    except Exception as e:
        print(f"Erro ao checar comandos do Telegram: {e}")

def monitorar():
    enviar_mensagem_telegram("🤖 *Bot de Notas Ativo ( Comandos /status /checar /notas e /ajuda )*")
    
    ultima_checagem_lyceum = time.time()
    alerta_erro_enviado = False
    
    while True:
        checar_comandos_telegram()
        
        tempo_atual = time.time()
        if tempo_atual - ultima_checagem_lyceum >= INTERVALO_SEGUNDOS:
            try:
                notas_novas = buscar_notas_portal()
                notas_antigas = carregar_notas_salvas()
                
                if not notas_antigas:
                    salvar_notas(notas_novas)
                else:
                    for chave, nota in notas_novas.items():
                        if chave not in notas_antigas:
                            enviar_mensagem_telegram(f"🚨 *NOVA NOTA DETECTADA!*\n\n*Avaliação:* {chave}\n*Nota:* {nota}")
                        elif notas_antigas[chave] != nota:
                            enviar_mensagem_telegram(f"📢 *NOTA ALTERADA!*\n\n*Avaliação:* {chave}\n*De:* {notas_antigas[chave]}\n*Para:* {nota}")
                    salvar_notas(notas_novas)
                
                alerta_erro_enviado = False
                ultima_checagem_lyceum = tempo_atual
                
            except Exception as e:
                msg_erro = str(e)
                print(f"Erro na verificação agendada: {msg_erro}")
                
                if not alerta_erro_enviado:
                    enviar_mensagem_telegram(f"⚠️ *Falha na varredura agendada:* {msg_erro}")
                    alerta_erro_enviado = True
                
                ultima_checagem_lyceum = tempo_atual
        
        time.sleep(3)

if __name__ == "__main__":
    monitorar()