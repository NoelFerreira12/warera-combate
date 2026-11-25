"""
V3.2 — uma ronda lida A CONTAR COM O DANO QUE JA FIZESTE.

E o `optimizador.analisar_ronda` do projecto, linha a linha, com duas
correccoes (o `analisar_ronda` do projecto fica como esta, para o painel):

  1. tu nao es concorrente de ti proprio: a tua linha do ranking sai da
     projeccao dos outros (no projecto entrava, e o teu dano de agora ocupava
     um lugar acima do teu dano novo), e o dano que ainda vai entrar
     reparte-se so pelos outros (o teu e o do plano);
  2. o teu lugar no fim e o do teu dano TOTAL: o que ja fizeste nesta ronda
     (ou nesta batalha, para o pool da batalha) mais o que o plano manda fazer.
     Em vez de mudar todas as contas, os danos dos outros guardam-se ja
     descontados do teu: rank(ja + d) = 1 + #{outros com proj >= ja + d}
     = 1 + #{outros com proj - ja >= d}. Assim o `planear`, as tabelas das
     barras e os drops mostrados ficam todos certos sem mais nada.

O bounty e o custo continuam a contar so o dano novo (o bounty do que ja
fizeste ja foi pago).
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timezone

from warera import api, optimizador as O


def analisar_ronda_eu(b, onde, cfg, comp, val, builds, caso_golpe, golpes_agora_de, buff, t, uid,
                      titulo=None, builds_compra=None):
    d_ = O.drops()
    loot = cfg.get("loot") or {}
    por_r = float(loot.get("damagePerLootItem") or 200000)
    por_b = float(loot.get("battleLootDamagePerLootItem") or 500000)
    rd = b.get("currentRound") if isinstance(b.get("currentRound"), dict) else \
        api.safe("round.getById", {"roundId": b.get("currentRound")}, {}) or {}
    rid, bid = rd.get("_id"), b["_id"]
    agora_dt = datetime.fromtimestamp(t, timezone.utc)
    fim = d_.estimate_round_seconds(rd, cfg.get("battle") or {}, agora_dt)
    rows = O.ranking_merged({"roundId": rid}, vivo=True)
    fotos = O.guardar_foto(rid, rows, t)
    rtw = int(b.get("roundsToWin") or 2)
    lider = d_.winning_side(rd)
    vitorias = int(((b.get(lider) or {}).get("wonRoundsCount") or 0)) if lider else 0
    fecha = bool(lider) and vitorias >= rtw - 1
    rows_b = O.ranking_merged({"battleId": bid}, vivo=True) if fecha else []

    for n, rid_ant in enumerate(b.get("rounds") or [], start=1):
        if rid_ant == rid:
            continue
        try:
            ant = O.ranking_merged({"roundId": rid_ant}, vivo=False)
        except api.ErroAPI:
            continue
        O.guardar_pool("round:" + rid_ant, ant, int(sum(r["value"] for r in ant) // por_r))

    rit = O.ritmos(fotos, rows, t, O._ts(rd.get("createdAt")), por_r)
    ritmo_agora = max(rit.get("recente") or 0.0, rit.get("media_desde_inicio") or 0.0)

    def folga(chave, por):
        return 1 + math.ceil(ritmo_agora * O.DURACAO_LEITURA.get(chave, 0.0) / por)
    regra = {"ronda": O.verificar_regra(rows, por_r, vivo=True, folga_itens=folga("round_" + rid, por_r))}
    if rows_b:
        regra["batalha"] = O.verificar_regra(rows_b, por_b, vivo=True,
                                             folga_itens=folga("battle_" + bid, por_b))
    w = O.pesos(rows, t)
    D = sum(r["value"] for r in rows)
    P = len(rows)
    DB = sum(r["value"] for r in rows_b)
    forma = sorted((r["value"] for r in rows), reverse=True)
    vistos_r = Counter(k for k in (O.tier(r.get("item")) for r in rows) if k)
    vistos_b = Counter(k for k in (O.tier(r.get("item")) for r in rows_b) if k)
    blocos_r = O.blocos_de_valor(rows, val)
    blocos_b = O.blocos_de_valor(rows_b, val) if rows_b else {}
    # V3.2 (1): a tua linha nao entra na projeccao dos outros
    eu = next((r for r in rows if r.get("user") == uid), None)
    eu_b = next((r for r in rows_b if r.get("user") == uid), None)
    ja_r = float(eu["value"]) if eu else 0.0
    ja_b = float(eu_b["value"]) if eu_b else 0.0
    val_r = [(r.get("user"), r["value"]) for r in rows if r.get("user") != uid]
    val_b = [(r.get("user"), r["value"]) for r in rows_b if r.get("user") != uid]
    # e o dano que ainda vai entrar reparte-se so pelos outros (o teu e o do plano): o total conserva-se
    w = {u: x for u, x in w.items() if u != uid}
    contextos = {}
    for nome in O.CONTEXTOS:
        T = 0.0 if nome == "agora" or fim is None else float(fim)
        R = 0.0 if nome == "agora" else rit["R"][nome]
        J = 0.0 if nome == "agora" else rit["J"][nome]
        extra, n_novos = R * T, J * T
        novos = O.novos_jogadores(n_novos, extra, rit["rajadas"], forma)
        proj = O.projectar(val_r, extra, novos, w)
        Nf = int((D + extra) // por_r)
        Pf = P + len(novos) + (0 if eu else 1)          # tu so es mais um jogador se ainda nao estavas
        cr = O.cortes_por_rank(O.contagem_final(vistos_r, int(D // por_r), Nf, comp), Nf, Pf)
        c = {"T": T, "R": R, "J": J, "extra": extra, "novos": len(novos), "N_final": Nf,
             "P_final": Pf,
             # V3.2 (2): os outros descontados do teu dano de agora -> o rank e o do teu total
             "ronda": {"cortes": cr, "neg": [-x + ja_r for x in proj], "blocos": blocos_r, "ja": ja_r,
                       "dano_corte": {k: v for k, v in ((k, _falta(O.dano_para_rank(cr[k], proj), ja_r))
                                                         for k in O.TIERS if cr[k]) if v is not None}}}
        if rows_b:
            pb = O.projectar(val_b, extra, novos, w)
            NBf = int((DB + extra) // por_b)
            PBf = len(rows_b) + len(novos) + (0 if eu_b else 1)
            cb = O.cortes_por_rank(O.contagem_final(vistos_b, int(DB // por_b), NBf, comp), NBf, PBf)
            c["batalha"] = {"cortes": cb, "neg": [-x + ja_b for x in pb], "blocos": blocos_b, "ja": ja_b,
                            "dano_corte": {k: v for k, v in ((k, _falta(O.dano_para_rank(cb[k], pb), ja_b))
                                                             for k in O.TIERS if cb[k]) if v is not None}}
            c["N_batalha"], c["P_batalha"] = NBf, PBf
        contextos[nome] = c

    lados = O._lados_da_batalha(onde, bid)
    plano = O.planear(contextos, lados, builds, val, caso_golpe, golpes_agora_de, fim, buff)
    plano_c = (O.planear(contextos, lados, builds_compra, val, caso_golpe, golpes_agora_de, fim, buff)
               if builds_compra else {"recomendado": None})
    bt = O.backtest(fotos, rows, t, por_r)

    def resumo_contexto(c):
        fora = {k: c[k] for k in ("T", "R", "J", "extra", "novos", "N_final", "P_final")}
        for pool in O.POOLS:
            x = c.get(pool)
            if x:
                fora[pool] = {str(k): {"rank": x["cortes"][k], "dano": x["dano_corte"].get(k)}
                              for k in O.TIERS if x["cortes"].get(k)}
        return fora

    return {
        "batalha": bid, "ronda": rid, "numero": rd.get("number"),
        "titulo": titulo or bid,
        "estado": {
            "dano": D, "jogadores": P, "itens": int(D // por_r),
            "mais_itens_que_jogadores": int(D // por_r) > P,
            "fim_segundos": fim, "fim_txt": d_.fmt_time(fim),
            "pontos": {s: (rd.get(s) or {}).get("points") for s in ("attacker", "defender")},
            "lider": lider, "fecha_batalha": fecha,
            "porque_batalha": (f"o lado que vai à frente já tem {vitorias} de {rtw} rondas: "
                               "a batalha acaba com esta ronda se ele a ganhar"
                               if fecha else
                               "a batalha continua depois desta ronda: o pool da batalha "
                               "fecha mais tarde e não entra nesta conta"),
            "dano_batalha": DB, "itens_batalha": int(DB // por_b) if rows_b else None,
            "tu": {"rank": eu["rank"], "dano": eu["value"], "item": eu["item"]} if eu else None,
            "tu_batalha": {"rank": eu_b["rank"], "dano": eu_b["value"], "item": eu_b["item"]} if eu_b else None,
        },
        "regra": regra,
        "ritmo": {k: v for k, v in rit.items() if k != "rajadas"} | {"chegados_amostra": len(rit["rajadas"])},
        "contextos": {n: resumo_contexto(c) for n, c in contextos.items()},
        "lados": lados,
        "plano": O._resumo_plano(plano),
        "plano_com_compras": O._resumo_plano(plano_c),
        "backtest": bt,
        "_contextos": contextos,
    }


def _falta(total, ja):
    """O dano A MAIS para chegar a um rank (o total que o rank pede menos o que ja tens)."""
    if total is None:
        return None
    return total - ja
