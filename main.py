import asyncio
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from html import escape

import requests
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters

PASTA = Path(__file__).resolve().parent
ARQUIVO = PASTA / "boletim.json"
ARQUIVO_ESTADO = PASTA / "estado.json"
INTERVALO_LEMBRETE = 6 * 60 * 60
TRAVA_CONSULTA = asyncio.Lock()

load_dotenv(PASTA / ".env")

TOKEN = os.environ["TELEGRAM_TOKEN"]

ARQUIVO_CHAT = PASTA / "chat_id.txt"

CHAT_ID = (
    int(ARQUIVO_CHAT.read_text().strip())
    if ARQUIVO_CHAT.exists()
    else None
)

URL = f"https://uemg.lyceum.com.br/aluno/apix/pessoas/{os.environ["CODIGO_PESSOA"]}/alunos/{os.environ["CODIGO_ALUNO"]}/boletim"

ARQUIVO_COOKIE = PASTA / "cookie.txt"

COOKIES = {
    "JSESSIONID": (
        ARQUIVO_COOKIE.read_text(encoding="utf-8").strip()
        if ARQUIVO_COOKIE.exists()
        else os.getenv("SESSION_ID", "")
    )
}

def ler_estado():
    if ARQUIVO_ESTADO.exists():
        return json.loads(ARQUIVO_ESTADO.read_text(encoding="utf-8"))
    return {"pendentes": [], "ultima_consulta": None, "em_falha": False,
            "ultimo_aviso": None}


def gravar_json(caminho, dados):
    temporario = caminho.with_suffix(".tmp")
    temporario.write_text(
        json.dumps(dados, ensure_ascii=False, indent=4), encoding="utf-8",
    )
    temporario.replace(caminho)


def salvar_estado(estado):
    gravar_json(ARQUIVO_ESTADO, estado)


def ler_json():
    estado = ler_estado()
    if "boletim" in estado:
        return estado["boletim"]
    return json.loads(ARQUIVO.read_text(encoding="utf-8"))


def horario_consulta(estado):
    instante = estado.get("ultima_consulta")
    if not instante:
        return "Última consulta bem-sucedida: horário desconhecido."
    horario = datetime.fromisoformat(instante).astimezone()
    return f"Última consulta bem-sucedida: {horario:%d/%m/%Y às %H:%M:%S %z}."


def resumo_consulta(dados, estado, recente=False):
    origem = "Dados consultados agora." if recente else "Dados do boletim salvo."
    if estado.get("em_falha"):
        origem += " O acesso está indisponível; os dados podem estar desatualizados."
    return origem + "\n" + horario_consulta(estado) + "\n\n" + resumo(dados)


def salvar_json(dados):
    gravar_json(ARQUIVO, dados)


def consultar_url():
    resposta = requests.get(URL, cookies=COOKIES, timeout=30)
    resposta.raise_for_status()
    dados = resposta.json()

    if not isinstance(dados, list) or any(
        not isinstance(d, dict) or "disciplina" not in d
        for d in dados
    ):
        raise ValueError("A URL não retornou um boletim válido.")

    return dados


def numero(valor):
    if valor is None:
        return "Não informado"
    return format(Decimal(str(valor)).normalize(), "f").replace(".", ",")


def provas_com_nota(disciplina):
    return [
        p for p in disciplina.get("provas") or []
        if p.get("nota") is not None
    ]


def soma_notas(disciplina):
    provas = provas_com_nota(disciplina)
    if not provas:
        return "Sem notas"
    return numero(sum(Decimal(str(p["nota"])) for p in provas))


def frequencia(disciplina):
    registros = disciplina.get("faltas") or []
    if not registros:
        return "Frequência não informada"

    return "\n".join(
        f"• <b>Faltas:</b> {numero(f.get('faltasAcumuladas'))}"
        f" / {numero(f.get('faltasPermitidas'))}\n"
        f"• <b>Presença:</b> {numero(f.get('percentualPresenca'))}"
        f"{'%' if f.get('percentualPresenca') is not None else ''}"
        for f in registros
    )


def resumo(dados):
    blocos = ["<b>Resumo do boletim</b>"]

    for d in dados:
        blocos.append(
            f"<b>{escape(d['nomeDisciplina'])}</b>\n"
            f"• <b>Soma das notas:</b> {soma_notas(d)}\n"
            f"{frequencia(d)}"
        )

    return "\n\n".join(blocos)


def detalhamento(d):
    media = d.get("media")
    if media is None:
        media = d.get("mediaNumerica")

    linhas = [
        f"<b>{escape(d['nomeDisciplina'])}</b>",
        "",
        f"• <b>Código:</b> {escape(str(d['disciplina']))}",
        f"• <b>Professor:</b> {escape(d.get('nomeDocente') or 'Não informado')}",
        f"• <b>Aulas previstas:</b> {numero(d.get('aulasPrevistas'))}",
        f"• <b>Aulas ministradas:</b> {numero(d.get('aulasMinistradas'))}",
        f"• <b>Média:</b> {escape(str(media)) if media is not None else 'Não informada'}",
        "",
        "<b>Frequência:</b>",
        frequencia(d),
        "",
        "<b>Avaliações:</b>",
    ]

    provas = provas_com_nota(d)

    if not provas:
        linhas.append("Nenhuma nota lançada.")

    for p in provas:
        nota = numero(p["nota"])

        if p.get("notaMaxProva") is not None:
            nota += f" / {numero(p['notaMaxProva'])}"

        linhas.extend([
            "",
            f"<b>{escape(p['codigoProva'])} — {escape(p['nomeProva'])}</b>",
            f"• <b>Nota:</b> {nota}",
        ])

    linhas.extend([
        "",
        f"<b>Soma das notas:</b> {soma_notas(d)}",
    ])

    return "\n".join(linhas)


def assinatura_notas(d):
    return {
        (
            p.get("codigoProva"),
            p.get("codSubdisciplina"),
            p.get("codTurma"),
        ): (numero(p.get("nota")), numero(p.get("notaMaxProva")))
        for p in d.get("provas") or []
    }


def assinatura_faltas(d):
    return sorted(
        (
            numero(f.get("faltasAcumuladas")),
            numero(f.get("faltasPermitidas")),
            numero(f.get("percentualPresenca")),
        )
        for f in d.get("faltas") or []
    )


def comparar(antigos, novos):
    antes = {d["disciplina"]: d for d in antigos}
    depois = {d["disciplina"]: d for d in novos}
    alteracoes = []

    for codigo in sorted(antes.keys() | depois.keys()):
        anterior = antes.get(codigo)
        atual = depois.get(codigo)

        if anterior is None:
            alteracoes.append(f"• {atual['nomeDisciplina']}: adicionada.")
            continue

        if atual is None:
            alteracoes.append(f"• {anterior['nomeDisciplina']}: removida.")
            continue

        nome = atual['nomeDisciplina']
        provas_antes = assinatura_notas(anterior)
        provas_depois = assinatura_notas(atual)
        nomes = {
            (p.get("codigoProva"), p.get("codSubdisciplina"), p.get("codTurma")):
            p.get("nomeProva") or p.get("codigoProva") or "Avaliação"
            for d in (anterior, atual) for p in d.get("provas") or []
        }
        for chave in sorted(provas_antes.keys() | provas_depois.keys(), key=str):
            antes_nota = provas_antes.get(chave)
            depois_nota = provas_depois.get(chave)
            rotulo = f"• {nome} — {nomes[chave]}"
            if antes_nota is None:
                alteracoes.append(f"{rotulo}: adicionada (nota {depois_nota[0]}; máximo {depois_nota[1]}).")
            elif depois_nota is None:
                alteracoes.append(f"{rotulo}: removida (nota {antes_nota[0]}).")
            else:
                for indice, campo in enumerate(("nota", "nota máxima")):
                    if antes_nota[indice] != depois_nota[indice]:
                        alteracoes.append(f"{rotulo}, {campo}: {antes_nota[indice]} → {depois_nota[indice]}.")

        faltas_antes = assinatura_faltas(anterior)
        faltas_depois = assinatura_faltas(atual)
        if faltas_antes != faltas_depois:
            # A API não fornece uma identidade estável para cada registro de frequência.
            # Compare o conjunto completo, sem atribuir pares arbitrários a registros.
            def descrever(registros):
                return "; ".join(
                    f"faltas {faltas}/{limite}, presença {presenca}%"
                    for faltas, limite, presenca in registros
                ) or "Não informado"
            alteracoes.append(
                f"• {nome} — frequência: {descrever(faltas_antes)} → {descrever(faltas_depois)}."
            )

    return alteracoes


async def enviar(bot, texto, com_detalhes=False, html=False):
    while texto:
        corte = 4000

        if len(texto) > corte:
            corte = texto.rfind("\n", 0, corte)
            if corte <= 0:
                corte = 4000

        bloco = texto[:corte]
        texto = texto[corte:].lstrip("\n")

        teclado = None

        if not texto:
            if com_detalhes:
                botao = InlineKeyboardButton(
                    "📖 Ver detalhes",
                    callback_data="disciplinas",
                )
            elif html:
                botao = InlineKeyboardButton(
                    "⬅️ Voltar ao boletim",
                    callback_data="boletim",
                )
            else:
                botao = None

            if botao:
                teclado = InlineKeyboardMarkup([[botao]])

        await bot.send_message(
            chat_id=CHAT_ID,
            text=bloco,
            parse_mode="HTML" if com_detalhes or html else None,
            reply_markup=teclado,
        )

async def clicar_botao(update, context):
    consulta = update.callback_query

    if update.effective_chat.id != CHAT_ID:
        await consulta.answer("Este boletim pertence a outro chat.")
        return

    await consulta.answer()
    dados = ler_json()

    if consulta.data == "boletim":
        await enviar(context.bot, resumo_consulta(dados, ler_estado()), com_detalhes=True)
        return

    if consulta.data == "disciplinas":
        codigos = [d["disciplina"] for d in dados]
        context.user_data["disciplinas"] = codigos

        teclado = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                d["nomeDisciplina"],
                callback_data=f"materia:{indice}",
            )]
            for indice, d in enumerate(dados)
        ])

        await consulta.message.reply_text(
            "Escolha uma disciplina:" if dados else "Nenhuma disciplina disponível.",
            reply_markup=teclado if dados else None,
        )

    elif consulta.data.startswith("materia:"):
        indice = int(consulta.data.split(":")[1])
        codigos = context.user_data.get("disciplinas", [])

        if indice >= len(codigos):
            await consulta.message.reply_text(
                "Abra novamente o botão Ver detalhes."
            )
            return

        disciplina = next(
            (d for d in dados if d["disciplina"] == codigos[indice]),
            None,
        )

        if disciplina is None:
            await consulta.message.reply_text(
                "Essa disciplina não está mais no boletim."
            )
            return

        await enviar(
            context.bot, "Dados do boletim salvo.\n"
            + horario_consulta(ler_estado()) + "\n\n" + detalhamento(disciplina),
            html=True,
        )


async def entregar_pendentes(bot, estado):
    while estado["pendentes"]:
        mensagem = estado["pendentes"][0]
        try:
            await enviar(bot, **mensagem)
        except TelegramError as erro:
            print(f"Notificação pendente; nova tentativa na próxima consulta: {erro}")
            return False
        estado["pendentes"].pop(0)
        salvar_estado(estado)
    return True


def agendar_mensagem(estado, texto, com_detalhes=False):
    estado["pendentes"].append({"texto": texto, "com_detalhes": com_detalhes})


async def atualizar(bot, motivo="automatico"):
    if CHAT_ID is None:
        return False
    async with TRAVA_CONSULTA:
        try:
            estado = ler_estado()
            agora = datetime.now(timezone.utc)
            try:
                novos = await asyncio.to_thread(consultar_url)
            except (requests.RequestException, ValueError) as erro:
                print(f"Erro ao acessar o boletim: {erro}")
                await avisar_falha(bot, estado, agora, manual=motivo in ("manual", "cookie"))
                return False

            agora = datetime.now(timezone.utc)
            antigos = estado.get("boletim")
            if antigos is None and ARQUIVO.exists():
                antigos = ler_json()
            alteracoes = comparar(antigos, novos) if antigos is not None else []
            recuperou = estado.get("em_falha", False)
            estado.update(boletim=novos, ultima_consulta=agora.isoformat(),
                          em_falha=False, ultimo_aviso=None)
            if recuperou:
                agendar_mensagem(estado, "O acesso ao boletim foi restabelecido!")
            if alteracoes:
                agendar_mensagem(
                    estado, "Atualização no boletim!\n\n"
                    + escape("\n".join(alteracoes)) + "\n\n"
                    + resumo_consulta(novos, estado, recente=True), True,
                )
            elif antigos is None or motivo != "automatico":
                cabecalho = {
                    "inicio": "Bot ativo! Monitoramento a cada 10 minutos.\n\n",
                    "cookie": "Sessão validada com o novo cookie!\n\n",
                }.get(motivo, "")
                agendar_mensagem(estado, cabecalho + resumo_consulta(novos, estado, recente=True), True)
            elif recuperou:
                agendar_mensagem(estado, horario_consulta(estado))
            if motivo == "cookie" and alteracoes:
                agendar_mensagem(estado, "Sessão validada com o novo cookie!")

            # Dados e notificações são confirmados juntos antes de atualizar o cache.
            salvar_estado(estado)
            try:
                salvar_json(novos)
            except OSError as erro:
                print(f"Erro ao atualizar boletim.json; dados preservados em estado.json: {erro}")
            await entregar_pendentes(bot, estado)
            return True
        except (ValueError, OSError) as erro:
            print(f"Erro ao ler ou salvar o estado do boletim: {erro}")
            await enviar(bot, "Não foi possível ler ou salvar os dados do boletim. Confira o terminal.")
            return False


async def verificar(context):
    await atualizar(context.bot)


async def comando(update, context):
    global CHAT_ID

    nome_comando = update.message.text.split()[0].split("@")[0]

    if CHAT_ID is None:
        if nome_comando != "/start":
            await update.message.reply_text("Envie /start para ativar o bot.")
            return

        ARQUIVO_CHAT.write_text(
            str(update.effective_chat.id),
            encoding="utf-8",
        )
        CHAT_ID = update.effective_chat.id

        await iniciar_servidor(context.application)
        return

    if update.effective_chat.id != CHAT_ID:
        return

    if nome_comando == "/atualizar":
        await atualizar(context.bot, motivo="manual")
        return

    if "boletim" not in ler_estado() and not ARQUIVO.exists():
        await update.message.reply_text(
            "Boletim ainda indisponível. "
            "Se o cookie expirou, atualize usando /cookie."
        )
        return

    dados = ler_json()

    if nome_comando == "/materia":
        codigo = " ".join(context.args).strip().casefold()
        disciplina = next(
            (d for d in dados if d["disciplina"].casefold() == codigo),
            None,
        )

        if disciplina:
            await enviar(
                context.bot, "Dados do boletim salvo.\n"
                + horario_consulta(ler_estado()) + "\n\n" + detalhamento(disciplina),
                html=True,
            )
        else:
            await enviar(
                context.bot,
                "Use /materia CODIGO. Exemplo: /materia 09-AAB\n\n"
                + "\n".join(
                    f"{d['disciplina']} — {d['nomeDisciplina']}"
                    for d in dados
                ),
            )
    else:
        await enviar(context.bot, resumo_consulta(dados, ler_estado()), com_detalhes=True)


async def erro_bot(update, context):
    print(f"Erro no bot: {context.error}")


async def iniciar_servidor(app):
    if CHAT_ID is None:
        print("Envie /start ao bot para registrar o chat.")
        return

    await atualizar(app.bot, motivo="inicio")


async def comando_cookie(update, context):
    if update.effective_chat.id != CHAT_ID:
        return

    context.user_data["aguardando_cookie"] = True

    await update.message.reply_text(
        "Envie o novo cookie de sessão na próxima mensagem.\n"
        "Cole somente o valor do JSESSIONID, sem aspas."
    )


async def receber_cookie(update, context):
    if update.effective_chat.id != CHAT_ID:
        return

    if not context.user_data.get("aguardando_cookie"):
        return

    cookie = update.message.text.strip()

    if not cookie or any(c.isspace() for c in cookie) or ";" in cookie:
        await update.message.reply_text(
            "Envie somente o valor do JSESSIONID, "
            "sem espaços ou outros cookies."
        )
        return

    try:
        ARQUIVO_COOKIE.write_text(cookie, encoding="utf-8")
    except OSError:
        await update.message.reply_text(
            "Não foi possível salvar cookie.txt. Tente novamente."
        )
        return

    COOKIES["JSESSIONID"] = cookie
    context.user_data.pop("aguardando_cookie", None)

    await update.message.reply_text(
        "Cookie salvo! Consultando o boletim para validar a sessão…"
    )
    await atualizar(context.bot, motivo="cookie")


async def avisar_falha(bot, estado, agora, manual=False):
    ultimo = estado.get("ultimo_aviso")
    deve_avisar = (
        not estado.get("em_falha") or ultimo is None
        or (agora - datetime.fromisoformat(ultimo)).total_seconds() >= INTERVALO_LEMBRETE
    )
    estado["em_falha"] = True
    if deve_avisar or manual:
        aviso = (
            "Não foi possível acessar o boletim. O cookie de sessão pode ter expirado. "
            "Use /cookie para informar um novo cookie.\n"
            + horario_consulta(estado)
            + "\nUse /resumo para consultar os dados salvos, que podem estar desatualizados."
        )
        agendar_mensagem(estado, aviso)
        estado["ultimo_aviso"] = agora.isoformat()
    salvar_estado(estado)
    await entregar_pendentes(bot, estado)


def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(iniciar_servidor)
        .build()
    )

    app.add_handler(
        CommandHandler(["start", "resumo", "materia", "atualizar"], comando)
    )

    app.add_handler(
        CallbackQueryHandler(
            clicar_botao,
            pattern=r"^(boletim|disciplinas|materia:\d+)$",
        )
    )

    app.add_handler(CommandHandler("cookie", comando_cookie))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            receber_cookie,
        )
    )

    app.add_error_handler(erro_bot)
    app.job_queue.run_repeating(verificar, interval=600, first=600)

    print("Bot iniciado. Verificação a cada 10 minutos.")
    app.run_polling()


if __name__ == "__main__":
    main()