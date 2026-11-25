"""
O pouco que vale a pena guardar entre execucoes: as rajadas de loot.

Porque e preciso: o loot de batalha nao pinga ao longo do tempo, e entregue
todo de uma vez no fim de cada ronda. Uma amostra de dez minutos do feed tanto
apanha seiscentas pecas como nenhuma — e correu-se o painel varias vezes
seguidas sem apanhar uma unica. Sem memoria, a analise do combate so funciona
por sorte.

Cada rajada e identificada pelo seu instante ao segundo, que e a mesma chave em
qualquer execucao. Isso resolve o unico problema real de acumular amostras:
ver a mesma rajada duas vezes nao a conta duas vezes.

O ficheiro vive em `saida/`, que ja esta no .gitignore, e e so um JSON. Se
desaparecer, o painel volta a funcionar so com a amostra do momento.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import config

CAMINHO = config.RAIZ / "saida" / "loot.json"
DIAS_A_GUARDAR = 7
MAX_RAJADAS = 400

# as vendas de equipamento seguem a mesma ideia por outra razao: numa janela de
# cinco minutos ha vendas a mais para ignorar e a menos para ajustar uma reta
# de preco por roll. Guardadas pelo id da transacao, duas execucoes seguidas
# nao contam a mesma venda duas vezes.
#
# A janela e curta de proposito: preco de ha uma semana ja nao e o preco de
# hoje, e o que se quer aqui e saber quanto vale um roll AGORA.
CAMINHO_VENDAS = config.RAIZ / "saida" / "vendas_equipamento.json"
# 30 dias, a mesma janela que a API da para os bens. O equipamento nao tem
# historico diario nenhum (`itemTrading` da 404 para equipamento), por isso o
# unico historico que existe e este, construido venda a venda.
#
# Trinta dias de precos misturam o mercado de hoje com o de ha um mes, e por
# isso o modelo pesa cada venda pela idade (ver pecas.MEIA_VIDA_DIAS): as
# antigas contam, mas contam menos.
DIAS_DE_VENDAS = 30
MAX_VENDAS = 20000


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _ler(caminho=None, chave: str = "rajadas") -> dict:
    caminho = caminho or CAMINHO
    try:
        d = json.loads(caminho.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and isinstance(d.get(chave), dict) else {chave: {}}
    except (FileNotFoundError, ValueError, OSError):
        return {chave: {}}


def _escrever(d: dict, caminho=None) -> bool:
    caminho = caminho or CAMINHO
    try:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        caminho.write_text(json.dumps(d, separators=(",", ":")), encoding="utf-8")
        return True
    except OSError:
        return False


def _recente(chave: str, limite: datetime) -> bool:
    try:
        return datetime.fromisoformat(chave.replace("Z", "")).replace(tzinfo=timezone.utc) >= limite
    except ValueError:
        return False


def registar_loot(rajadas: dict[str, Counter]) -> dict:
    """
    Junta as rajadas desta amostra as que ja se conheciam e devolve o total
    acumulado. Rajadas com mais de `DIAS_A_GUARDAR` dias caem fora: o loot de
    ha um mes nao diz nada sobre os precos de hoje.
    """
    d = _ler()
    guardadas: dict = d.get("rajadas") or {}
    novas = 0
    for chave, contador in (rajadas or {}).items():
        if chave not in guardadas:
            novas += 1
        guardadas[chave] = dict(contador)

    limite = _agora() - timedelta(days=DIAS_A_GUARDAR)
    guardadas = {k: v for k, v in guardadas.items() if _recente(k, limite)}
    if len(guardadas) > MAX_RAJADAS:
        guardadas = dict(sorted(guardadas.items())[-MAX_RAJADAS:])

    gravado = _escrever({"rajadas": guardadas})

    total: Counter = Counter()
    for v in guardadas.values():
        total.update(v)
    chaves = sorted(guardadas)
    return {
        "contador": total,
        "rajadas": len(guardadas),
        "rajadas_novas": novas,
        "pecas": sum(total.values()),
        "primeira": chaves[0] if chaves else None,
        "ultima": chaves[-1] if chaves else None,
        "gravado": gravado,
    }


# O que conta como preco de AGORA. As vendas guardadas podem ter semanas (o
# ficheiro fica em disco ate DIAS_DE_VENDAS), mas uma venda de ha um mes e
# historia, nao preco actual: so entram no modelo de preco por roll as das
# ultimas 48 horas, e as outras sao contadas e ditas, nunca usadas.
HORAS_VENDAS_ACTUAIS = 48.0


def vendas_recentes(horas: float = HORAS_VENDAS_ACTUAIS, caminho=None) -> dict:
    """
    So as vendas de equipamento com roll das ultimas `horas`, sem escrever nada.

    Devolve as recentes em {codigo: [(preco, skills, quando), ...]} e, a parte,
    quantas ficaram de fora por serem antigas e de quando eram — para o painel
    poder dizer "sem dados suficientes" em vez de usar historia como preco.
    """
    d = _ler(caminho or CAMINHO_VENDAS, "vendas")
    guardadas: dict = d.get("vendas") or {}
    limite = _agora() - timedelta(hours=horas)
    por_codigo: dict[str, list] = {}
    antigas = []
    recentes = []
    for v in guardadas.values():
        em = (v.get("em") or "")[:19]
        code, preco, skills = v.get("codigo"), v.get("preco"), v.get("skills")
        if not (code and preco and skills):
            continue
        if _recente(em, limite):
            por_codigo.setdefault(code, []).append((float(preco), skills, v.get("em")))
            recentes.append(v.get("em") or "")
        else:
            antigas.append(v.get("em") or "")
    return {
        "vendas": por_codigo,
        "n": len(recentes),
        "janela_horas": horas,
        "ultima": max(recentes) if recentes else None,
        "antigas_ignoradas": len(antigas),
        "antigas_de": min(antigas) if antigas else None,
        "antigas_ate": max(antigas) if antigas else None,
    }


def registar_vendas_equipamento(vendas: dict[str, dict]) -> dict:
    """
    Acumula as vendas de equipamento vistas, com o roll de cada peca, e devolve
    o conjunto todo em {codigo: [(preco, skills), ...]} — pronto para o modelo
    de preco por roll.
    """
    d = _ler(CAMINHO_VENDAS, "vendas")
    guardadas: dict = d.get("vendas") or {}
    novas = sum(1 for tid in (vendas or {}) if tid not in guardadas)
    guardadas.update(vendas or {})

    limite = _agora() - timedelta(days=DIAS_DE_VENDAS)
    guardadas = {k: v for k, v in guardadas.items()
                 if _recente((v.get("em") or "")[:19], limite)}
    if len(guardadas) > MAX_VENDAS:
        recentes = sorted(guardadas.items(), key=lambda kv: kv[1].get("em") or "")
        guardadas = dict(recentes[-MAX_VENDAS:])

    gravado = _escrever({"vendas": guardadas}, CAMINHO_VENDAS)

    # (preco, skills, quando): a data vai junto porque o modelo de preco pesa
    # as vendas pela idade — trinta dias de mercado nao sao todos iguais
    por_codigo: dict[str, list] = {}
    for v in guardadas.values():
        code, preco, skills = v.get("codigo"), v.get("preco"), v.get("skills")
        if code and preco and skills:
            por_codigo.setdefault(code, []).append((float(preco), skills, v.get("em")))
    marcas = sorted(v.get("em") or "" for v in guardadas.values())
    return {
        "vendas": por_codigo,
        "n": len(guardadas),
        "novas": novas,
        "codigos": len(por_codigo),
        "desde": marcas[0] if marcas else None,
        "gravado": gravado,
    }
