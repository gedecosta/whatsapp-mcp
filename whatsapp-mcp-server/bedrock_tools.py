"""Ferramentas extras do WhatsApp MCP para a rotina da bedrock mentorship.

Tudo aqui lê o banco do bridge (whatsapp-bridge/store/messages.db) e guarda dados
próprios (etiquetas, notas, follow-ups, modelos) em crm.db, ao lado deste arquivo.
Só `enviar_em_massa` e a leitura de membros de grupo falam com o bridge.
"""
import os
import re
import sqlite3
import statistics
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from whatsapp import MESSAGES_DB_PATH, WHATSAPP_API_BASE_URL, send_message

AQUI = os.path.dirname(os.path.abspath(__file__))
CRM_DB_PATH = os.path.join(AQUI, "crm.db")
EXPORT_DIR = os.path.join(AQUI, "..", "exportacoes")

MODELOS_INICIAIS = {
    "boas-vindas": "Oi, {nome}! Aqui é da bedrock mentorship. Que bom ter você com a gente. "
                   "Vou te passar os próximos passos para começarmos a mentoria.",
    "lembrete-sessao": "Oi, {nome}! Lembrando da nossa sessão de mentoria {quando}. "
                       "Qualquer imprevisto, me avisa por aqui.",
    "cobranca-gentil": "Oi, {nome}! Passando para lembrar do pagamento da mentoria. "
                       "Se já fez, pode desconsiderar. Qualquer dúvida é só chamar.",
    "retomar-contato": "Oi, {nome}! Faz um tempinho que a gente não se fala. "
                       "Como está a preparação? Posso te ajudar com algo?",
}


# ---------------------------------------------------------------- bancos

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(MESSAGES_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _crm() -> sqlite3.Connection:
    conn = sqlite3.connect(CRM_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS contato (
            chave TEXT PRIMARY KEY, nome TEXT, etiquetas TEXT DEFAULT '', atualizado_em TEXT);
        CREATE TABLE IF NOT EXISTS nota (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chave TEXT, texto TEXT, criado_em TEXT);
        CREATE TABLE IF NOT EXISTS followup (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chave TEXT, nome TEXT, data TEXT,
            motivo TEXT, feito INTEGER DEFAULT 0, criado_em TEXT);
        CREATE TABLE IF NOT EXISTS modelo (nome TEXT PRIMARY KEY, texto TEXT);
    """)
    if conn.execute("SELECT COUNT(*) FROM modelo").fetchone()[0] == 0:
        conn.executemany("INSERT INTO modelo VALUES (?, ?)", MODELOS_INICIAIS.items())
        conn.commit()
    return conn


def _ts(valor: Optional[str]) -> Optional[datetime]:
    if not valor:
        return None
    try:
        d = datetime.fromisoformat(valor)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _corta(texto: Optional[str], n: int = 200) -> str:
    texto = (texto or "").replace("\n", " ").strip()
    return texto if len(texto) <= n else texto[: n - 1] + "…"


def _so_digitos(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _telefone_do_chat(conn: sqlite3.Connection, chat_jid: str) -> Optional[str]:
    user = chat_jid.split("@")[0]
    if chat_jid.endswith("@s.whatsapp.net"):
        return user
    row = conn.execute("SELECT phone FROM contacts WHERE user = ?", (user,)).fetchone()
    return row["phone"] if row and row["phone"] else None


def _ultima_msg_por_chat(conn: sqlite3.Connection, grupos: bool) -> List[sqlite3.Row]:
    filtro = "" if grupos else "AND c.jid NOT LIKE '%@g.us'"
    return conn.execute(f"""
        SELECT c.jid, c.name, m.content, m.media_type, m.is_from_me, m.timestamp
        FROM chats c JOIN messages m ON m.chat_jid = c.jid
         AND m.timestamp = (SELECT MAX(timestamp) FROM messages WHERE chat_jid = c.jid)
        WHERE c.jid != '0@s.whatsapp.net' {filtro}
        GROUP BY c.jid
    """).fetchall()


def _texto_msg(row) -> str:
    return row["content"] or (f"[{row['media_type']}]" if row["media_type"] else "")


# ------------------------------------------------------ resolver contato

def resolver(conn: sqlite3.Connection, consulta: str) -> List[Dict[str, Any]]:
    """Acha conversas individuais por nome (parcial) ou telefone. Retorna candidatos."""
    consulta = (consulta or "").strip()
    if not consulta:
        return []
    achados: Dict[str, Dict[str, Any]] = {}
    digitos = _so_digitos(consulta)
    if "@" in consulta:
        linhas = conn.execute("SELECT jid, name FROM chats WHERE jid = ?", (consulta,)).fetchall()
    elif len(digitos) >= 6 and len(digitos) >= len(consulta) - 4:
        linhas = conn.execute("""
            SELECT DISTINCT c.jid, c.name FROM chats c
            LEFT JOIN contacts k ON k.user = substr(c.jid, 1, instr(c.jid, '@') - 1)
            WHERE c.jid LIKE ? OR k.phone LIKE ?""", (f"%{digitos}%", f"%{digitos}%")).fetchall()
    else:
        linhas = conn.execute(
            "SELECT jid, name FROM chats WHERE LOWER(name) LIKE LOWER(?)", (f"%{consulta}%",)).fetchall()
    for r in linhas:
        achados[r["jid"]] = {"chat_jid": r["jid"], "nome": r["name"],
                             "telefone": _telefone_do_chat(conn, r["jid"]),
                             "grupo": r["jid"].endswith("@g.us")}
    return list(achados.values())


def _um(conn: sqlite3.Connection, consulta: str, aceita_grupo: bool = False):
    """Retorna (contato, None) se único, ou (None, erro) com os candidatos."""
    cands = resolver(conn, consulta)
    if not aceita_grupo:
        cands = [c for c in cands if not c["grupo"]] or cands
    if not cands:
        return None, {"erro": f"Nenhuma conversa encontrada para '{consulta}'."}
    if len(cands) > 1:
        return None, {"erro": f"'{consulta}' bate com mais de uma conversa. Seja mais específico.",
                      "candidatos": [{"nome": c["nome"], "telefone": c["telefone"]} for c in cands[:10]]}
    return cands[0], None


def _chave(c: Dict[str, Any]) -> str:
    return c["telefone"] or c["chat_jid"].split("@")[0]


# --------------------------------------------------------- atendimento

def aguardando_resposta(horas_minimas: float = 1.0, dias_maximos: int = 14,
                        incluir_grupos: bool = False, limite: int = 30) -> List[Dict[str, Any]]:
    """Conversas em que a última mensagem é do outro lado e ainda não foi respondida."""
    conn = _db()
    agora = _agora()
    saida = []
    for r in _ultima_msg_por_chat(conn, incluir_grupos):
        t = _ts(r["timestamp"])
        if r["is_from_me"] or not t:
            continue
        horas = (agora - t).total_seconds() / 3600
        if horas < horas_minimas or horas > dias_maximos * 24:
            continue
        saida.append({"nome": r["name"], "telefone": _telefone_do_chat(conn, r["jid"]),
                      "chat_jid": r["jid"], "aguardando_ha_horas": round(horas, 1),
                      "ultima_mensagem": _corta(_texto_msg(r))})
    saida.sort(key=lambda x: -x["aguardando_ha_horas"])
    return saida[:limite]


def sem_retorno(dias_minimos: int = 2, dias_maximos: int = 30, limite: int = 30) -> List[Dict[str, Any]]:
    """Conversas em que você falou por último e o outro lado não respondeu (candidatas a follow-up)."""
    conn = _db()
    agora = _agora()
    saida = []
    for r in _ultima_msg_por_chat(conn, False):
        t = _ts(r["timestamp"])
        if not r["is_from_me"] or not t:
            continue
        dias = (agora - t).total_seconds() / 86400
        if dias < dias_minimos or dias > dias_maximos:
            continue
        saida.append({"nome": r["name"], "telefone": _telefone_do_chat(conn, r["jid"]),
                      "chat_jid": r["jid"], "sem_resposta_ha_dias": round(dias, 1),
                      "sua_ultima_mensagem": _corta(_texto_msg(r))})
    saida.sort(key=lambda x: -x["sem_resposta_ha_dias"])
    return saida[:limite]


def resumo_do_periodo(horas: int = 24) -> Dict[str, Any]:
    """Visão geral da atividade nas últimas N horas, por conversa."""
    conn = _db()
    desde = _agora() - timedelta(hours=horas)
    por_chat: Dict[str, Dict[str, Any]] = {}
    for r in conn.execute("""
            SELECT m.chat_jid, c.name, m.content, m.media_type, m.is_from_me, m.timestamp
            FROM messages m JOIN chats c ON c.jid = m.chat_jid ORDER BY m.timestamp"""):
        t = _ts(r["timestamp"])
        if not t or t < desde:
            continue
        d = por_chat.setdefault(r["chat_jid"], {"conversa": r["name"], "grupo": r["chat_jid"].endswith("@g.us"),
                                                "recebidas": 0, "enviadas": 0, "ultima": ""})
        d["enviadas" if r["is_from_me"] else "recebidas"] += 1
        d["ultima"] = _corta(_texto_msg(r), 120)
    conversas = sorted(por_chat.values(), key=lambda x: -(x["recebidas"] + x["enviadas"]))
    return {"periodo_horas": horas,
            "total_recebidas": sum(c["recebidas"] for c in conversas),
            "total_enviadas": sum(c["enviadas"] for c in conversas),
            "conversas_ativas": len(conversas), "conversas": conversas}


def historico_do_contato(contato: str, limite: int = 40) -> Dict[str, Any]:
    """Últimas mensagens de uma conversa, em ordem cronológica. Aceita nome ou telefone."""
    conn = _db()
    c, erro = _um(conn, contato, aceita_grupo=True)
    if erro:
        return erro
    linhas = conn.execute("""
        SELECT sender, content, media_type, is_from_me, timestamp FROM messages
        WHERE chat_jid = ? ORDER BY timestamp DESC LIMIT ?""", (c["chat_jid"], limite)).fetchall()
    nomes: Dict[str, str] = {}
    msgs = []
    for r in reversed(linhas):
        if r["is_from_me"]:
            quem = "Eu"
        elif c["grupo"]:
            if r["sender"] not in nomes:
                k = conn.execute("SELECT name FROM contacts WHERE user = ?", (r["sender"],)).fetchone()
                nomes[r["sender"]] = k["name"] if k else r["sender"]
            quem = nomes[r["sender"]]
        else:
            quem = c["nome"]
        msgs.append({"quando": r["timestamp"], "quem": quem, "texto": r["content"] or "",
                     "midia": r["media_type"] or None})
    return {"conversa": c["nome"], "telefone": c["telefone"], "mensagens": msgs}


def buscar_em_todas(texto: str, dias: Optional[int] = None, limite: int = 30) -> List[Dict[str, Any]]:
    """Procura um texto em todas as conversas, com o nome de quem falou."""
    conn = _db()
    sql = """SELECT m.chat_jid, c.name, m.sender, m.content, m.is_from_me, m.timestamp
             FROM messages m JOIN chats c ON c.jid = m.chat_jid
             WHERE LOWER(m.content) LIKE LOWER(?)"""
    linhas = conn.execute(sql + " ORDER BY m.timestamp DESC", (f"%{texto}%",)).fetchall()
    corte = _agora() - timedelta(days=dias) if dias else None
    saida = []
    for r in linhas:
        t = _ts(r["timestamp"])
        if corte and (not t or t < corte):
            continue
        quem = "Eu" if r["is_from_me"] else r["name"]
        if not r["is_from_me"] and r["chat_jid"].endswith("@g.us"):
            k = conn.execute("SELECT name FROM contacts WHERE user = ?", (r["sender"],)).fetchone()
            quem = k["name"] if k else r["sender"]
        saida.append({"conversa": r["name"], "quem": quem, "quando": r["timestamp"],
                      "texto": _corta(r["content"], 300)})
        if len(saida) >= limite:
            break
    return saida


def exportar_conversa(contato: str, caminho: Optional[str] = None) -> Dict[str, Any]:
    """Salva a conversa inteira em um arquivo Markdown (pasta 'exportacoes') e devolve o caminho."""
    conn = _db()
    c, erro = _um(conn, contato, aceita_grupo=True)
    if erro:
        return erro
    linhas = conn.execute("""SELECT sender, content, media_type, is_from_me, timestamp FROM messages
                             WHERE chat_jid = ? ORDER BY timestamp""", (c["chat_jid"],)).fetchall()
    if not caminho:
        os.makedirs(EXPORT_DIR, exist_ok=True)
        seguro = re.sub(r"[^\w\-]+", "_", c["nome"] or "conversa").strip("_")
        caminho = os.path.abspath(os.path.join(EXPORT_DIR, f"{seguro}.md"))
    with open(caminho, "w", encoding="utf-8") as f:
        f.write(f"# Conversa com {c['nome']}\n\n")
        for r in linhas:
            quem = "Eu" if r["is_from_me"] else c["nome"]
            if not r["is_from_me"] and c["grupo"]:
                k = conn.execute("SELECT name FROM contacts WHERE user = ?", (r["sender"],)).fetchone()
                quem = k["name"] if k else r["sender"]
            midia = f" [{r['media_type']}]" if r["media_type"] else ""
            f.write(f"**{r['timestamp']} · {quem}:**{midia} {r['content'] or ''}\n\n")
    return {"arquivo": caminho, "mensagens": len(linhas)}


def estatisticas_de_atendimento(dias: int = 30) -> Dict[str, Any]:
    """Volume por dia e tempo de resposta (do outro lado até a sua resposta) nas conversas individuais."""
    conn = _db()
    corte = _agora() - timedelta(days=dias)
    por_dia: Dict[str, Dict[str, int]] = {}
    esperas: List[float] = []
    ativos = set()
    ultimo_recebido: Dict[str, datetime] = {}
    for r in conn.execute("""SELECT chat_jid, is_from_me, timestamp FROM messages
                             WHERE chat_jid NOT LIKE '%@g.us' ORDER BY timestamp"""):
        t = _ts(r["timestamp"])
        if not t:
            continue
        if t >= corte:
            ativos.add(r["chat_jid"])
            d = por_dia.setdefault(t.astimezone().strftime("%Y-%m-%d"), {"recebidas": 0, "enviadas": 0})
            d["enviadas" if r["is_from_me"] else "recebidas"] += 1
        if r["is_from_me"]:
            ini = ultimo_recebido.pop(r["chat_jid"], None)
            if ini and t >= corte and (t - ini).total_seconds() < 86400:
                esperas.append((t - ini).total_seconds() / 60)
        else:
            ultimo_recebido.setdefault(r["chat_jid"], t)
    return {"periodo_dias": dias, "conversas_individuais_ativas": len(ativos),
            "total_recebidas": sum(d["recebidas"] for d in por_dia.values()),
            "total_enviadas": sum(d["enviadas"] for d in por_dia.values()),
            "tempo_resposta_mediano_min": round(statistics.median(esperas), 1) if esperas else None,
            "tempo_resposta_medio_min": round(statistics.mean(esperas), 1) if esperas else None,
            "respostas_medidas": len(esperas),
            "por_dia": dict(sorted(por_dia.items()))}


# --------------------------------------------------------------- grupos

def listar_grupos() -> List[Dict[str, Any]]:
    """Grupos que você participa, com atividade recente."""
    conn = _db()
    return [{"nome": r["name"], "chat_jid": r["jid"], "ultima_atividade": r["last_message_time"],
             "mensagens_guardadas": r["n"]}
            for r in conn.execute("""
                SELECT c.jid, c.name, c.last_message_time,
                       (SELECT COUNT(*) FROM messages WHERE chat_jid = c.jid) n
                FROM chats c WHERE c.jid LIKE '%@g.us' ORDER BY c.last_message_time DESC""")]


def membros_do_grupo(grupo: str) -> Dict[str, Any]:
    """Lista os membros de um grupo (nome, telefone, se é admin). Exige o bridge rodando."""
    conn = _db()
    c, erro = _um(conn, grupo, aceita_grupo=True)
    if erro:
        return erro
    if not c["grupo"]:
        return {"erro": f"'{c['nome']}' não é um grupo."}
    try:
        r = requests.get(f"{WHATSAPP_API_BASE_URL}/group", params={"jid": c["chat_jid"]}, timeout=20)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        return {"erro": f"Não consegui falar com o bridge (ele está rodando?): {e}"}


# ------------------------------------------------------------ mini-CRM

def _etiquetas(texto: str) -> List[str]:
    return [e for e in (texto or "").split(",") if e]


def definir_contato(contato: str, etiquetas: Optional[List[str]] = None, nota: Optional[str] = None,
                    substituir_etiquetas: bool = False) -> Dict[str, Any]:
    """Marca um contato com etiquetas (ex.: mentorado, lead, cliente, ITA, EEAR) e/ou adiciona uma nota."""
    conn = _db()
    c, erro = _um(conn, contato)
    if erro:
        return erro
    chave = _chave(c)
    crm = _crm()
    atual = crm.execute("SELECT etiquetas FROM contato WHERE chave = ?", (chave,)).fetchone()
    tags = [] if substituir_etiquetas or not atual else _etiquetas(atual["etiquetas"])
    for e in etiquetas or []:
        e = e.strip().lower()
        if e and e not in tags:
            tags.append(e)
    agora = _agora().isoformat()
    crm.execute("INSERT OR REPLACE INTO contato VALUES (?, ?, ?, ?)", (chave, c["nome"], ",".join(tags), agora))
    if nota:
        crm.execute("INSERT INTO nota (chave, texto, criado_em) VALUES (?, ?, ?)", (chave, nota, agora))
    crm.commit()
    return {"contato": c["nome"], "etiquetas": tags, "nota_adicionada": bool(nota)}


def listar_por_etiqueta(etiqueta: str) -> List[Dict[str, Any]]:
    """Lista os contatos que têm uma etiqueta."""
    etiqueta = etiqueta.strip().lower()
    return [{"nome": r["nome"], "chave": r["chave"], "etiquetas": _etiquetas(r["etiquetas"])}
            for r in _crm().execute("SELECT * FROM contato ORDER BY nome")
            if etiqueta in _etiquetas(r["etiquetas"])]


def ficha_do_contato(contato: str) -> Dict[str, Any]:
    """Tudo sobre um contato: etiquetas, notas, follow-ups pendentes e as últimas mensagens."""
    conn = _db()
    c, erro = _um(conn, contato)
    if erro:
        return erro
    chave = _chave(c)
    crm = _crm()
    meta = crm.execute("SELECT * FROM contato WHERE chave = ?", (chave,)).fetchone()
    notas = [{"quando": n["criado_em"][:10], "texto": n["texto"]} for n in
             crm.execute("SELECT * FROM nota WHERE chave = ? ORDER BY id DESC", (chave,))]
    fups = [{"id": f["id"], "data": f["data"], "motivo": f["motivo"]} for f in
            crm.execute("SELECT * FROM followup WHERE chave = ? AND feito = 0 ORDER BY data", (chave,))]
    recentes = historico_do_contato(c["chat_jid"], limite=8)["mensagens"]
    return {"nome": c["nome"], "telefone": c["telefone"], "etiquetas": _etiquetas(meta["etiquetas"]) if meta else [],
            "notas": notas, "followups_pendentes": fups, "ultimas_mensagens": recentes}


def agendar_followup(contato: str, data: str, motivo: str) -> Dict[str, Any]:
    """Cria um lembrete de retorno. data no formato AAAA-MM-DD."""
    try:
        datetime.strptime(data, "%Y-%m-%d")
    except ValueError:
        return {"erro": "Use a data no formato AAAA-MM-DD."}
    conn = _db()
    c, erro = _um(conn, contato)
    if erro:
        return erro
    crm = _crm()
    cur = crm.execute("INSERT INTO followup (chave, nome, data, motivo, criado_em) VALUES (?, ?, ?, ?, ?)",
                      (_chave(c), c["nome"], data, motivo, _agora().isoformat()))
    crm.commit()
    return {"id": cur.lastrowid, "contato": c["nome"], "data": data, "motivo": motivo}


def followups_pendentes(ate: Optional[str] = None) -> List[Dict[str, Any]]:
    """Follow-ups não concluídos com data até hoje (ou até a data informada, AAAA-MM-DD)."""
    limite = ate or datetime.now().strftime("%Y-%m-%d")
    return [{"id": f["id"], "contato": f["nome"], "data": f["data"], "motivo": f["motivo"],
             "atrasado": f["data"] < datetime.now().strftime("%Y-%m-%d")}
            for f in _crm().execute("SELECT * FROM followup WHERE feito = 0 AND data <= ? ORDER BY data", (limite,))]


def concluir_followup(id: int) -> Dict[str, Any]:
    """Marca um follow-up como feito."""
    crm = _crm()
    cur = crm.execute("UPDATE followup SET feito = 1 WHERE id = ?", (id,))
    crm.commit()
    return {"concluido": cur.rowcount > 0}


# -------------------------------------------------------------- modelos

def listar_modelos() -> Dict[str, str]:
    """Modelos de mensagem salvos. Use {nome} para o primeiro nome do contato; outras {variaveis} são livres."""
    return {r["nome"]: r["texto"] for r in _crm().execute("SELECT * FROM modelo ORDER BY nome")}


def salvar_modelo(nome: str, texto: str) -> Dict[str, Any]:
    """Cria ou atualiza um modelo de mensagem."""
    crm = _crm()
    crm.execute("INSERT OR REPLACE INTO modelo VALUES (?, ?)", (nome.strip().lower(), texto))
    crm.commit()
    return {"salvo": nome.strip().lower()}


def _primeiro_nome(nome: str) -> str:
    nome = re.sub(r"\b(cliente|mentorado|mentorada)\b", "", nome or "", flags=re.I).strip()
    return nome.split()[0].capitalize() if nome.split() else ""


def _preencher(texto: str, nome: str, variaveis: Optional[Dict[str, str]]) -> str:
    valores = {"nome": _primeiro_nome(nome), **(variaveis or {})}
    return re.sub(r"\{(\w+)\}", lambda m: valores.get(m.group(1), m.group(0)), texto)


def renderizar_modelo(modelo: str, contato: Optional[str] = None,
                      variaveis: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Preenche um modelo para um contato, sem enviar. Devolve o texto pronto."""
    crm = _crm()
    row = crm.execute("SELECT texto FROM modelo WHERE nome = ?", (modelo.strip().lower(),)).fetchone()
    if not row:
        return {"erro": f"Modelo '{modelo}' não existe.", "disponiveis": list(listar_modelos())}
    nome = ""
    if contato:
        c, erro = _um(_db(), contato)
        if erro:
            return erro
        nome = c["nome"]
    texto = _preencher(row["texto"], nome, variaveis)
    return {"contato": nome or None, "texto": texto, "variaveis_pendentes": re.findall(r"\{(\w+)\}", texto)}


def enviar_em_massa(contatos: List[str], mensagem: str, simulacao: bool = True,
                    intervalo_segundos: float = 8.0) -> Dict[str, Any]:
    """Envia a mesma mensagem (com {nome}) para vários contatos, com pausa entre os envios.

    Por padrão só simula e mostra o que seria enviado. Máximo de 30 contatos por chamada.
    """
    if len(contatos) > 30:
        return {"erro": "Máximo de 30 contatos por chamada, para evitar bloqueio do WhatsApp."}
    conn = _db()
    plano, problemas = [], []
    for consulta in contatos:
        c, erro = _um(conn, consulta)
        if erro:
            problemas.append({"contato": consulta, **erro})
            continue
        plano.append({"contato": c["nome"], "destino": c["telefone"] or c["chat_jid"],
                      "texto": _preencher(mensagem, c["nome"], None)})
    if problemas or simulacao:
        return {"simulacao": True, "enviados": 0, "plano": plano, "problemas": problemas,
                "aviso": "Nada foi enviado." + (" Resolva os problemas antes de enviar." if problemas else "")}
    resultados = []
    for i, p in enumerate(plano):
        ok, resp = send_message(p["destino"], p["texto"])
        resultados.append({"contato": p["contato"], "ok": ok, "resposta": resp})
        if i < len(plano) - 1:
            time.sleep(max(intervalo_segundos, 5.0))
    return {"simulacao": False, "enviados": sum(r["ok"] for r in resultados), "resultados": resultados}
