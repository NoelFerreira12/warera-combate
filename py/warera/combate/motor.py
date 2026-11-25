"""
O motor do assistente de combate: a ponte para o motor do projecto.

Nao ha formulas novas aqui. Parte o `optimizador.analisar()` nas fontes de que
ele depende, para que cada uma se releia ao seu ritmo, e junta-lhe:

  1. TODOS os conjuntos de seis pecas Epic, Legendary e Mythic (o motor do
     projecto so procura a fronteira de `builds_epic`);
  2. as regras de vida e fome no tempo: a recuperacao a hora certa
     (verificada), comer com 1 ponto inteiro de fome e bater so com vida >=
     custo do golpe (por confirmar);
  3. as janelas da pilula (+60% durante, -60% na ressaca, verificado na API);
  4. os estados da verificacao da regra do ranking merged.

Tudo o que mede dano, drops, bounty e custos e do motor do projecto:
`optimizador`, `bounty._metade_dano/_metade_defesa`, `bounty.opcoes`, `simulador`.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone

from warera import bounty as B, config, craft, optimizador as O, precos as P, simulador


SLOTS = ("weapon", "helmet", "chest", "pants", "gloves", "boots")


NOME_SLOT = {"weapon": "Arma", "helmet": "Capacete", "chest": "Colete", "pants": "Calças",
             "gloves": "Luvas", "boots": "Botas"}


MUNICOES = ("lightAmmo", "ammo", "heavyAmmo")


BENS = ["lightAmmo", "ammo", "heavyAmmo", "bread", "steak", "cookedFish", "cocain", "case1", "case2", "scraps"]


def todos(op, slots):
    """Como `bounty.frente`, mas sem tirar nenhuma: todas as combinacoes de pecas."""
    conj = [{"capex": 0.0, "liq_arma": 0.0, "liq_resto": 0.0, "stats": {}, "pecas": {}, "faca": False}]
    for slot in slots:
        novos = []
        for o in op.get(slot) or []:
            for c in conj:
                st = dict(c["stats"])
                for s, v in o["stats"].items():
                    st[s] = st.get(s, 0.0) + v
                novos.append({"capex": c["capex"] + o["capex"],
                              "liq_arma": c["liq_arma"] + (o["liquido"] if slot == "weapon" else 0.0),
                              "liq_resto": c["liq_resto"] + (o["liquido"] if slot != "weapon" else 0.0),
                              "stats": st, "pecas": {**c["pecas"], slot: o},
                              "faca": c["faca"] or o["codigo"] == B.ARMA_SEM_MUNICAO})
        conj = novos
    return conj


def montar(base, md, mf, cfg, ctx1):
    """O cruzamento de `optimizador.builds_epic`, guardando tambem a esquiva e o custo de um golpe cheio."""
    for f in mf:
        st = dict(base)
        for s, v in f["combo"]["stats"].items():
            st[s] = float(st.get(s) or 0.0) + v
        r = simulador.dano_disponivel(st, cfg, comida_vida_pct=ctx1["vida_comida"][f["comida"]],
                                      posto_pct=ctx1["posto_pct"], batalha=1.0)
        f["_vida_golpe"] = float(r.get("vida_por_golpe_esperada") or 0.0)
        f["_vida_golpe_cheio"] = float(r.get("vida_por_golpe") or 0.0)
        f["_vida_max"] = float(r.get("vida_maxima") or 0.0)
        f["_vida_comida"] = f["_vida_max"] * float(ctx1["vida_comida"][f["comida"]] or 0) / 100.0
        f["_esquiva"] = float(r.get("esquiva_pct") or 0.0)
    fora = []
    for d in md:
        cd = d["combo"]
        for f in mf:
            cf = f["combo"]
            desg = cd["liq_arma"] / 100.0 + (cf["liq_resto"] + cd["liq_resto"]) / 100.0 * f["poupa"]
            pecas_ = {**cd["pecas"], **cf["pecas"]}
            fora.append({
                "pecas": pecas_, "municao": d["municao"], "comida": f["comida"],
                "dpg1": float(d["dpg"]), "custo_golpe": d["c_mun"] + f["c_comida"] + desg,
                "golpes_hora": f["golpes"] / 24.0, "capex": cd["capex"] + cf["capex"],
                "vida_golpe": f["_vida_golpe"], "vida_comida": f["_vida_comida"],
                "tens": all(p["origem"] != "comprar" for p in pecas_.values()),
                "_poupa": f["poupa"], "_esquiva": f["_esquiva"], "_vida_golpe_cheio": f["_vida_golpe_cheio"],
                "_c_mun": d["c_mun"], "_c_comida": f["c_comida"], "_c_desgaste": desg,
            })
    return fora


def chave_peca(o):
    return f"{o['codigo']}|{o['origem']}|{o.get('id') or ''}"


def chave_pecas(b):
    return tuple(chave_peca(b["pecas"][s]) for s in SLOTS)


def preparar_base(user, equip, cfg, precos_eq, ref_precos, livro, resumos, manuais=None):
    """
    O inicio de `optimizador.analisar`, linha a linha, com as fontes ja lidas.
    `manuais` sao pecas que o jogador introduziu a mao (o inventario nao e
    publico): entram como pecas tuas, sem custo de compra.
    """
    itens = cfg.get("items") or {}
    vestido = {s: v for s, v in (equip or {}).items() if s in B.SLOTS and isinstance(v, dict)}
    mp = P.ProvedorDeMercado(referencia=ref_precos, panorama=livro, equipamento=precos_eq)
    taxa = config.TAXA_MERCADO_PADRAO
    precos_mapa = mp.mapa()
    custos_scrap = craft.custo_em_scraps(itens, {})
    preco_scrap = mp.preco("scraps", "bid")

    def preco_venda(code):
        v = mp.preco(code, "bid")
        return v * (1 - taxa) if v else None

    vendas, info_vendas = O.vendas_com_roll()
    peso_arma = float((cfg.get("loot") or {}).get("weaponChancePercent") or 30) / 100.0
    val = O.Valores(itens, precos_eq, vendas, taxa, peso_arma)
    comp = O.composicao(O.pools_conhecidos())
    hist = B.historico(resumos, precos_eq, taxa)
    base = B.stats_sem_equipamento(user)
    rec = B.receitas(hist, preco_venda, float(base.get("lootChance") or 0.0), 0.0)
    caso_golpe = float(rec["por_golpe"])
    ctx1 = B.contexto(cfg, precos_mapa, B.posto_pct(user), custos_scrap, preco_scrap,
                      B.HORAS_COMBATE, 1.0, B.HORIZONTE_DIAS, None)
    op = B.opcoes(itens, precos_eq, vestido, custos_scrap, preco_scrap, taxa)
    # as pecas introduzidas a mao: como as vestidas (`bounty.opcoes`), com o roll delas e sem compra
    for m in manuais or []:
        code, slot = m.get("codigo"), m.get("slot")
        if slot not in SLOTS or code not in itens:
            continue
        preco = (precos_eq or {}).get(code)
        rec_ = __import__("warera.pecas", fromlist=["x"]).valor_de_reciclagem(code, custos_scrap, preco_scrap, taxa)
        liq = max(0.0, float(preco) - float((rec_ or {}).get("partida") or 0.0)) if preco else 0.0
        op[slot].append({"codigo": code, "slot": slot, "origem": "manual", "id": m.get("id"),
                         "raridade": (itens.get(code) or {}).get("rarity"),
                         "stats": {k: float(v) for k, v in (m.get("stats") or {}).items()},
                         "estado": m.get("estado"), "capex": 0.0, "preco": preco, "liquido": liq})
    op6 = O.opcoes_epic(op)
    ctx_1 = {**ctx1, "batalha": 1.0}
    md = B._metade_dano(base, todos(op6, B.GRUPO_DANO), ctx_1)
    mf = B._metade_defesa(base, todos(op6, B.GRUPO_DEFESA), ctx_1)
    builds = montar(base, md, mf, cfg, ctx1)
    grupos: dict[tuple, list] = {}
    for b in builds:
        grupos.setdefault(chave_pecas(b), []).append(b)
    # a fronteira que o motor do projecto procura, para a prova
    bs = O.builds_epic(base, op, ctx1, cfg)
    sk = user.get("skills") or {}
    atk = sk.get("attack") or {}
    buff_ate = O._ts((user.get("buffs") or {}).get("buffEndAt"))
    buff = 1.0
    if buff_ate and buff_ate > time.time():
        buff *= 1 + float(atk.get("buffsPercent") or 0.0) / 100.0
    buff *= 1 - float(atk.get("debuffsPercent") or 0.0) / 100.0
    cot = {c: dict(mp.cotar(c, registar=False)) for c in
           BENS + sorted({o["codigo"] for s in op.values() for o in s})}
    return {"itens": itens, "vestido": vestido, "val": val, "comp": comp, "rec": rec, "caso_golpe": caso_golpe,
            "base": base, "ctx1": ctx1, "op6": op6, "grupos": grupos, "buff": buff,
            "fronteira": {chave_pecas(b) for b in bs["fronteira"]} | {chave_pecas(b) for b in bs["tens"]},
            "tens_motor": bs["tens"], "cotacoes": cot, "info_vendas": info_vendas, "taxa": taxa,
            "builds_motor": bs["avaliadas"]}


def vida_fome(user):
    sk = user.get("skills") or {}
    h, f = sk.get("health") or {}, sk.get("hunger") or {}
    return ({"agora": float(h.get("currentBarValue") or 0), "max": float(h.get("total") or 0),
             "por_hora": float(h.get("hourlyBarRegen") or 0)},
            {"agora": float(f.get("currentBarValue") or 0), "max": float(f.get("total") or 0),
             "por_hora": float(f.get("hourlyBarRegen") or 0)})


def _gad_de(vida, fome):
    def gad(b):
        if not b.get("vida_golpe"):
            return 0.0
        return (vida + fome * b.get("vida_comida", 0.0)) / b["vida_golpe"]
    return gad


def primeiro_tick_h(lido: datetime) -> float:
    return ((lido.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)) - lido).total_seconds() / 3600.0


def golpes_com(vida_agora, fome_agora, vg_esp, vg_cheio, vc):
    """
    Golpes que a vida deixa dar, com as regras desta maqueta:
      - so se bate com vida >= custo de um golpe nao esquivado (por confirmar);
      - comer gasta 1 ponto INTEIRO de fome e repoe vc de vida (por confirmar);
      - um golpe esquivado nao gasta vida (motor do projecto).
    garantidos: todos sem esquiva. esperados: os nao esquivados que cabem, mais
    os esquivados que vem com eles em media.
    """
    v = vida_agora + math.floor(fome_agora + 1e-9) * vc
    if vg_cheio <= 0 or v + 1e-9 < vg_cheio:
        return 0, 0
    # nao `v // vg_cheio`: 20,4 // 3,4 da 5 em binario (o quociente exacto dos doubles e 5,999...),
    # e o jogo, com valores a uma casa decimal, da 6. A mesma tolerancia no JavaScript (paridade.js).
    garantidos = int(math.floor(v / vg_cheio + 1e-9))
    esquiva = max(0.0, min(0.95, 1.0 - (vg_esp / vg_cheio))) if vg_cheio else 0.0
    esperados = int(math.floor(garantidos / (1.0 - esquiva) + 1e-9)) if esquiva < 1 else garantidos
    return garantidos, esperados


def pronto_em(n, vida, fome, vg_esp, vg_cheio, vc, tick0):
    """Horas ate haver vida para n golpes (esperados), com a recuperacao as horas certas."""
    if n <= 0:
        return 0.0
    for k in range(0, 49):
        v = vida["agora"] + vida["por_hora"] * k
        f = fome["agora"] + fome["por_hora"] * k
        # a gastar a medida que chega, a barra nao enche: o tecto do maximo nao trava
        _, esp = golpes_com(v, f, vg_esp, vg_cheio, vc)
        if esp >= n:
            return 0.0 if k == 0 else tick0 + (k - 1)
    return None


OBJECTIVOS = ("regra", "lucro", "sem")


def chave_obj(obj, t, capex):
    r1k = round(t["lucro_1k"] if t["lucro_1k"] is not None else -1e9, 2)
    if obj == "lucro":
        return (round(t["lucro"], 2), r1k, t["dano"], -capex)
    if obj == "sem":
        return (1 if capex == 0 else 0, r1k, round(t["lucro"], 2), t["dano"])
    return (r1k, round(t["lucro"], 2), t["dano"], -capex)


ESTADOS_REGRA = ("confirmado", "por confirmar no fim da ronda", "discrepância confirmada", "dados indisponíveis")


def estado_regra(x, vivo=True):
    """
    O que uma verificacao (`optimizador.verificar_regra`) quer dizer.

    Uma inversao (raridade acima abaixo de outra) ou um item abaixo do corte viola
    a regra na propria leitura: e uma discrepancia, e fica para conferir no fim.
    Uma contagem fora da folga SEM inversoes, numa leitura viva, e o dano que
    entrou enquanto as paginas eram lidas (Maldives, 19/11): fica por confirmar
    no fim da ronda, contra o ranking final. Uma leitura final que nao confere
    e uma discrepancia confirmada.
    """
    if not x:
        return "dados indisponíveis"
    if x.get("confere") or x.get("ok"):
        return "confirmado"
    if (x.get("inversoes") or 0) > 0 or (x.get("itens_abaixo_do_corte") or x.get("abaixo") or 0) > 0:
        return "discrepância confirmada"
    return "por confirmar no fim da ronda" if vivo else "discrepância confirmada"


RESSACA_PCT_VERIFICADO = 60.0


def janelas(user, itens, agora_ts):
    """
    Os intervalos de tempo em que o multiplicador de ataque e constante.

    Com pilula: [agora, fim do buff) a +60%, [fim do buff, +15,5 h) a -60%, depois x1.
    Em ressaca: [agora, fim da ressaca) a -60%, depois x1. Sem nada: x1 sempre.
    Os valores vem da API quando ela os da (buffsPercent, debuffsPercent, buffEndAt,
    debuffEndAt) e da config do item quando nao (duracao da ressaca).
    """
    bf = user.get("buffs") or {}
    atk = (user.get("skills") or {}).get("attack") or {}
    flat = ((itens.get("cocain") or {}).get("flatStats") or {})
    h_res = float(flat.get("debuffDurationHours") or 15.5)
    pct_buff = float(atk.get("buffsPercent") or 0) or float(flat.get("percentAttack") or 60)
    pct_res = float(atk.get("debuffsPercent") or 0) or RESSACA_PCT_VERIFICADO
    fim_buff = O._ts(bf.get("buffEndAt"))
    fim_res = O._ts(bf.get("debuffEndAt"))
    if fim_buff and fim_buff > agora_ts:
        f2 = fim_buff + h_res * 3600.0
        return [{"nome": "pílula", "inicio": agora_ts, "fim": fim_buff, "mult": 1 + pct_buff / 100.0},
                {"nome": "ressaca", "inicio": fim_buff, "fim": f2, "mult": 1 - pct_res / 100.0},
                {"nome": "normal", "inicio": f2, "fim": None, "mult": 1.0}]
    if fim_res and fim_res > agora_ts:
        return [{"nome": "ressaca", "inicio": agora_ts, "fim": fim_res, "mult": 1 - pct_res / 100.0},
                {"nome": "normal", "inicio": fim_res, "fim": None, "mult": 1.0}]
    return [{"nome": "normal", "inicio": agora_ts, "fim": None, "mult": 1.0}]


def janelas_da_ronda(js, fim_ronda_ts, margem_s=60.0):
    """So as janelas que comecam antes de a ronda acabar (com um minuto de margem para bater)."""
    return [w for w in js if w["inicio"] < fim_ronda_ts - margem_s]


def assinatura_janelas(jr):
    """O que identifica as janelas sem depender do 'agora' (o inicio da primeira e sempre agora)."""
    return [(w["nome"], round(w["mult"], 4), 0 if i == 0 else int(w["inicio"] // 60)) for i, w in enumerate(jr)]


def recursos_em(vida, fome, lido_ts, alvo_ts):
    """Vida e fome previstas num instante futuro, sem bater: +1 recuperacao a cada hora certa, com tecto."""
    if alvo_ts <= lido_ts:
        return vida["agora"], fome["agora"]
    lido = datetime.fromtimestamp(lido_ts, timezone.utc)
    t0 = primeiro_tick_h(lido) * 3600.0
    dt = alvo_ts - lido_ts
    ticks = 0 if dt < t0 else 1 + int((dt - t0) // 3600)
    return (min(vida["max"], vida["agora"] + vida["por_hora"] * ticks),
            min(fome["max"], fome["agora"] + fome["por_hora"] * ticks))
