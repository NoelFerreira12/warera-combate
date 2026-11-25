"""
Constantes e configuracao. Nada aqui e adivinhado: todos os numeros abaixo
foram confirmados contra a API real (ver docs/DADOS_REAIS.md).
"""

from __future__ import annotations

import os
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

API_OFICIAL = "https://api2.warera.io/trpc"
API_GATEWAY = "https://gateway.warerastats.io/trpc"

# ---------------------------------------------------------------------------
#  TOKEN
# ---------------------------------------------------------------------------
#  Ordem de procura:
#    1. variavel de ambiente WARERA_API_KEY
#    2. ficheiro .env ao lado do repositorio (WARERA_API_KEY=...)
#
#  O token NUNCA fica escrito no codigo. O .env esta no .gitignore, por isso
#  nao vai para o repositorio nem para o GitHub.
#  Cria o teu em: WarEra -> Definicoes -> API Tokens.
# ---------------------------------------------------------------------------


def _ler_dotenv() -> dict[str, str]:
    caminho = RAIZ / ".env"
    if not caminho.exists():
        return {}
    fora = {}
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        k, v = linha.split("=", 1)
        fora[k.strip()] = v.strip().strip('"').strip("'")
    return fora


_ENV = _ler_dotenv()


def usar_token() -> bool:
    """
    O painel corre SEM a API key pessoal (decisao do dono do projecto, a
    19/11/2026): so a manda para a API se `WARERA_USAR_TOKEN=1` estiver
    definido. A key pode continuar no .env, mas nao vai em pedido nenhum.

    O que so existe com token (`transaction.getPaginatedTransactions`: a fita,
    as vendas reais, os salarios, as aberturas de cases) passa a aparecer como
    indisponivel, com o motivo, em vez de vazio ou a zero.
    """
    v = os.getenv("WARERA_USAR_TOKEN") or _ENV.get("WARERA_USAR_TOKEN", "")
    return v.strip().lower() in ("1", "sim", "true", "yes")


def token() -> str:
    if not usar_token():
        return ""
    return os.getenv("WARERA_API_KEY") or _ENV.get("WARERA_API_KEY", "")


def jwt() -> str:
    return os.getenv("WARERA_JWT") or _ENV.get("WARERA_JWT", "")


# ---------------------------------------------------------------------------
#  Taxas  (country.taxes na API)
#
#  Sao tres, nao uma, e o painel usava so a primeira:
#    market    taxa sobre cada venda no mercado. 163 dos 180 paises usam 1%,
#              mas ha um pais a 0% e varios a 10%.
#    selfWork  imposto sobre trabalhar para si proprio. Varia de 0.01% a 45%
#              entre paises: e a maior diferenca fiscal do jogo e nao aparecia
#              em lado nenhum.
#    income    imposto sobre o salario recebido de outra empresa.
# ---------------------------------------------------------------------------
TAXA_MERCADO_PADRAO = 0.01
TAXA_SELFWORK_PADRAO = 0.0
TAXA_RENDIMENTO_PADRAO = 0.0

# ---------------------------------------------------------------------------
#  Custo de craft de equipamento, por raridade.
#
#  Os scraps saem de transacoes craftItem reais: 6, 18, 54, 162, 486, 1458.
#  O ACO nao aparece em transacao nenhuma — o jogo consome-o sem emitir linha
#  no feed — e tambem nao esta no gameConfig. Esta na tabela que o proprio
#  cliente do jogo usa para desenhar o ecra de craft:
#
#    COMMON1    {baseScrap:   2, bonusScrap:   4, craftCost:    6, craftCostSteel:  1}
#    UNCOMMON2  {baseScrap:   6, bonusScrap:  12, craftCost:   18, craftCostSteel:  2}
#    RARE3      {baseScrap:  18, bonusScrap:  36, craftCost:   54, craftCostSteel:  4}
#    EPIC4      {baseScrap:  54, bonusScrap: 108, craftCost:  162, craftCostSteel:  8}
#    LEGENDARY5 {baseScrap: 162, bonusScrap: 324, craftCost:  486, craftCostSteel: 16}
#    MYTHIC6    {baseScrap: 486, bonusScrap: 972, craftCost: 1458, craftCostSteel: 32}
#
#  (app.warera.io/_next/static/chunks/pages/_app-*.js)
#
#  Tres coisas de uma vez:
#    - craftCost confirma os scraps observados no feed, sem uma excecao;
#    - baseScrap e exatamente craftCost/3, que e o que se mede ao desmantelar
#      uma peca partida — a regra do terco vem daqui, e base+bonus = craftCost
#      da a formula completa: devolvido = base + bonus * (estado/estado_max);
#    - craftCostSteel e um custo que este painel ignorava por completo, e que
#      chega a ser metade do custo de craft de uma peca common.
# ---------------------------------------------------------------------------
SCRAPS_POR_RARIDADE = {
    "common": 6,
    "uncommon": 18,
    "rare": 54,
    "epic": 162,
    "legendary": 486,
    "mythic": 1458,
}

ACO_POR_RARIDADE = {
    "common": 1,
    "uncommon": 2,
    "rare": 4,
    "epic": 8,
    "legendary": 16,
    "mythic": 32,
}

# escolher a peca em vez de aceitar uma aleatoria da mesma raridade custa o
# dobro do aco (`needed: a ? 2*r : r` no ecra de craft). Os scraps sao os
# mesmos nos dois casos.
MULTIPLICADOR_ACO_PECA_ESCOLHIDA = 2

ORDEM_RARIDADE = ["common", "uncommon", "rare", "epic", "legendary", "mythic"]

# ---------------------------------------------------------------------------
#  Desmantelar devolve os scraps todos com a peca a 100% de estado, e um terco
#  com a peca partida (state 0). Medido em 3735 desmantelamentos reais
#  (knife 6->2, boots2 18->6, boots3 54->18, boots4 162->54, tank 486->162,
#  jet 1458->486, sem uma excecao) e confirmado depois pela tabela do jogo,
#  onde baseScrap = craftCost/3 e base+bonus = craftCost.
#  O aco gasto no craft nao volta.
# ---------------------------------------------------------------------------
FRACAO_SCRAPS_PECA_PARTIDA = 1.0 / 3.0

# ---------------------------------------------------------------------------
#  As 14 skills do jogo, de gameConfig.skills. A lista anterior tinha 12 e
#  faltavam-lhe as duas que mais pesam em cada lado: `production` (economia)
#  e `precision` (combate), que assim ficavam de fora da classificacao de
#  build e da recomendacao de pontos.
# ---------------------------------------------------------------------------
SKILLS_COMBATE = ["attack", "armor", "dodge", "precision",
                  "criticalChance", "criticalDamages", "health"]
SKILLS_ECONOMIA = ["production", "companies", "entrepreneurship", "management",
                   "lootChance", "hunger", "energy"]

# ---------------------------------------------------------------------------
#  Postos militares: (dano acumulado necessario, bonus de ataque em %).
#
#  Sao 120 postos, de 0 a 10 mil milhoes de dano, que valem ate +37.5% de
#  ataque. Nao estao no gameConfig — estao no cliente do jogo, e sao o que
#  explica o `militaryRankPercent` que aparece nos stats de cada jogador.
#
#  E a progressao mais longa do jogo e a unica que nao se compra: so se ganha
#  a bater. Por isso vale a pena mostrar quanto falta para o proximo.
# ---------------------------------------------------------------------------
POSTOS_MILITARES = [
    (0, 0.0), (500, 0.25), (1000, 0.5), (2000, 0.75),
    (4000, 1.25), (5000, 1.5), (6000, 1.75), (8000, 2.0),
    (10000, 2.5), (11400, 2.75), (12700, 3.0), (15300, 3.25),
    (18000, 3.75), (20400, 4.0), (22700, 4.25), (27300, 4.5),
    (32000, 5.0), (35000, 5.25), (38000, 5.5), (44000, 5.75),
    (50000, 6.25), (55000, 6.5), (60000, 6.75), (70000, 7.0),
    (80000, 7.5), (87500, 7.75), (95000, 8.0), (110000, 8.25),
    (125000, 8.75), (138000, 9.0), (150000, 9.25), (175000, 9.5),
    (200000, 10.0), (217000, 10.25), (234000, 10.5), (267000, 10.75),
    (300000, 11.25), (325000, 11.5), (350000, 11.75), (400000, 12.0),
    (450000, 12.5), (487000, 12.75), (524000, 13.0), (597000, 13.25),
    (670000, 13.75), (725000, 14.0), (780000, 14.25), (890000, 14.5),
    (1000000, 15.0), (1090000, 15.25), (1170000, 15.5), (1330000, 15.75),
    (1500000, 16.25), (1630000, 16.5), (1750000, 16.75), (1990000, 17.0),
    (2230000, 17.5), (2430000, 17.75), (2620000, 18.0), (3010000, 18.25),
    (3400000, 18.75), (3670000, 19.0), (3940000, 19.25), (4470000, 19.5),
    (5000000, 20.0), (5450000, 20.25), (5900000, 20.5), (6800000, 20.75),
    (7700000, 21.25), (8330000, 21.5), (8950000, 21.75), (10200000, 22.0),
    (11500000, 22.5), (12500000, 22.75), (13400000, 23.0), (15200000, 23.25),
    (17000000, 23.75), (18500000, 24.0), (20000000, 24.25), (23000000, 24.5),
    (26000000, 25.0), (28200000, 25.25), (30400000, 25.5), (34700000, 25.75),
    (39000000, 26.25), (42200000, 26.5), (45400000, 26.75), (51700000, 27.0),
    (58000000, 27.5), (65000000, 27.75), (72000000, 28.0), (86000000, 28.25),
    (100000000, 28.75), (114000000, 29.0), (127000000, 29.25), (153000000, 29.5),
    (180000000, 30.0), (217000000, 30.25), (254000000, 30.5), (327000000, 30.75),
    (400000000, 31.25), (445000000, 31.5), (489000000, 31.75), (577000000, 32.0),
    (666000000, 32.5), (694000000, 32.75), (722000000, 33.0), (767000000, 33.25),
    (833000000, 33.5), (917000000, 33.75), (1000000000, 34.25), (1090000000, 34.5),
    (1170000000, 34.75), (1300000000, 35.0), (1500000000, 35.25), (1750000000, 35.5),
    (2000000000, 36.0), (3000000000, 36.5), (5000000000, 37.0), (10000000000, 37.5),
]

# ---------------------------------------------------------------------------
#  Imagens do jogo. Os itens negociaveis tem imagem propria; o equipamento usa
#  a imagem generica do slot (`iconImg` no gameConfig), porque a peca muda de
#  cor pela raridade e nao de desenho.
# ---------------------------------------------------------------------------
IMAGENS = "https://media.warera.io/images/items"

# quantos pedidos de historico puxar por defeito.
# 1 pagina = 100 transacoes ~= 11 segundos de jogo. 40 paginas ~= 7 minutos.
PAGINAS_HISTORICO = 40

# ---------------------------------------------------------------------------
#  A janela das analises.
#
#  O feed de transacoes da minutos e o livro de ordens da o instante. Nenhum
#  dos dois aguenta uma decisao de uma semana: o que interessa a quem escolhe
#  o que produzir nao e o preco deste minuto, e o que a coisa costuma valer.
#  O historico diario da 30 dias por bem, e e sobre esses que as contas
#  principais passam a ser feitas. O instantaneo fica ao lado, para se ver
#  quando o agora se afastou do normal.
# ---------------------------------------------------------------------------
DIAS_ANALISE = 30

# ---------------------------------------------------------------------------
#  O ritmo do jogo: `user.regenDividedBy` = 10.
#
#  Todas as barras (energia, producao, vida, fome) regeneram um decimo do seu
#  maximo por hora. E o numero que faltava para as vias serem comparaveis:
#
#    producao no maximo (40)  ->  4 pontos de producao por hora  ->  96 por dia
#    energia no maximo (130)  ->  13 de energia/h  ->  1.3 acoes/h (10 cada)
#    vida no maximo (200)     ->  20 de vida/h     ->  2 hits/h de graca
#
#  Sem isto, "0.10 de lucro por ponto de producao" e "12% de ROI por ciclo"
#  sao numeros que nao se podem por na mesma lista — e era o que o painel
#  fazia, com o resultado de uma case a aparecer acima de uma producao so
#  porque 0.107 e maior que 0.1055.
# ---------------------------------------------------------------------------
REGEN_POR_HORA = 1.0 / 10.0

# ---------------------------------------------------------------------------
#  Como se produz, pelo guia oficial de skills economicas do jogo.
#
#  Isto corrige um erro grosseiro que o painel tinha: a barra de PRODUCAO nao
#  e o deposito de pontos de producao. `production` e quanto se produz DE CADA
#  VEZ que se trabalha (10 a 40 PP, com +/-10% de sorte, vezes o bonus da
#  empresa). Os depositos que se gastam sao outros dois:
#
#    entrepreneurship  30-80   trabalhar na PROPRIA empresa, 10 por trabalho
#    energy            30-130  trabalhar para OUTRO por salario, 10 por trabalho
#
#  Ambos regeneram 10% do maximo por hora, como todas as barras. Logo:
#
#    trabalhos por hora = (maximo x 10%) / 10 = maximo / 100
#
#  No maximo das skills sao 0.8 trabalhos/hora na propria empresa e 1.3 a
#  salario — 19 e 31 trabalhos por dia, a 40 PP cada. Nao 4 PP por hora, que
#  era o que o painel dizia.
# ---------------------------------------------------------------------------
PRODUCAO_MAXIMA = 40          # PP por trabalho, com a skill no maximo
EMPREENDEDORISMO_MAXIMO = 80  # deposito para trabalhar na propria empresa
ENERGIA_MAXIMA = 130          # deposito para trabalhar a salario
CUSTO_POR_TRABALHO = 10       # tirado do deposito, seja qual for
RNG_PRODUCAO = 0.10           # +/-10% de sorte em cada trabalho

# cada empresa tem um motor automatico que produz sozinho: 1 PP/hora por
# omissao (24 por dia) e ate 7 PP/hora no nivel 7, sem gastar nada ao jogador
MOTOR_PP_HORA_BASE = 1
MOTOR_PP_HORA_MAX = 7
EMPRESAS_MAXIMAS = 12

ENERGIA_POR_ACAO = 10

# ---------------------------------------------------------------------------
#  As regras de combate, do guia oficial do jogo
#  (warera.io/en/articles/fighting-skills-mechanics-guide-ca4d94).
#
#  O que o painel tinha antes estava errado em duas coisas:
#
#  1. A precisao abaixo de 100 NAO e inutil. Abaixo de 100% ha hipotese de
#     falhar, e uma falha "deals half damage" e nao pode ser critica. Subir
#     precisao de 80 para 100 vale 10% de dano medio, mesmo sem transbordo.
#  2. `softCap: 40` na armadura e no dodge nao e um tecto: e a constante da
#     curva `efetivo = total / (total + 40)`. Com 200 de armadura o efetivo e
#     83%, nao 40. Sao rendimentos decrescentes, nao um travao.
#
#  Os bonus multiplicam-se entre si, nao se somam:
#      ataque = (skill + arma + transbordo) x municao x pilula x posto x batalha
# ---------------------------------------------------------------------------
COMBATE = {
    "dano_falha": 0.5,           # uma falha da metade do dano e nunca e critica
    "curva_defesa": 40.0,        # efetivo = total / (total + curva)
    "precisao_max": 100.0,       # acima disto transborda para ataque
    "critico_max": 100.0,        # acima disto transborda para dano critico
    "vida_por_hit": 10.0,        # sem armadura; a armadura reduz proporcional
}

# multiplicadores de ataque, todos multiplicativos entre si
MULTIPLICADORES = {
    "lightAmmo": 1.10, "ammo": 1.20, "heavyAmmo": 1.40,
    "cocain": 1.60,              # 8 horas; depois 0.40 durante 15.5 horas
    "cocain_ressaca": 0.40,
    "batalha_max": 1.95,         # 2.15 em revoltas
}

# ---------------------------------------------------------------------------
#  A pilula nao e um multiplicador constante: e uma JANELA.
#
#  O painel tratava-a como um `bool` que multiplicava o dia inteiro por 1.60.
#  Isso esta errado nos dois sentidos, e o erro nao e pequeno:
#
#    +60% durante 8 horas, depois -60% durante 15.5 horas.
#
#  Quem combate espalhado pelas 24 horas fica com uma media de
#      (8x1.6 + 0.5x1.0 + 15.5x0.4) / 24 = 0.81
#  ou seja PERDE 19% de dano por tomar a pilula. Quem concentra o combate
#  dentro da janela ganha os 60% inteiros. A diferenca entre as duas leituras
#  e de 2x, e nenhuma delas e "1.60 o dia todo".
#
#  Os numeros reais vem do gameConfig (`flatStats.percentAttack`,
#  `buffDurationHours`, `debuffDurationHours` no item `cocain`); estes ficam
#  como recurso se a config nao os trouxer.
# ---------------------------------------------------------------------------
PILULA = {
    "horas_buff": 8.0,
    "horas_ressaca": 15.5,
    "ataque_pct": 60.0,          # +60% durante o buff
    "ressaca_pct": -60.0,        # -60% durante a ressaca
}

# ---------------------------------------------------------------------------
#  Os bonus de batalha, um a um.
#
#  O simulador tinha um cursor unico "batalha" de x1.00 a x1.95 sem dizer de
#  onde saia o numero. Nao da para responder "porque e que este jogador faz
#  mais dano do que eu no mesmo sitio" com um cursor opaco, nem para saber se
#  vale a pena pagar uma ordem de batalha.
#
#  Sao todos ADITIVOS entre si e o total multiplica o ataque uma vez so:
#      ataque = base x municao x pilula x posto x (1 + soma_dos_bonus)
#
#  Valores da v0.25.5-beta (29 Ago 2026). Duas coisas mudaram nessa versao e
#  invalidam qualquer guia anterior a 1 de Setembro de 2026:
#
#    patriotico   +10% -> +20%
#    alianca      maximo +10% -> +20%, mas o limiar desceu de 15% para 10% da
#                 quota de desenvolvimento mundial, o decaimento passou de
#                 -0.5 para -4 pontos por ponto de quota acima do limiar, e o
#                 chao deixou de ser 0% e passou a ser -20%.
#
#  A segunda e a que apanha toda a gente: uma alianca grande deixou de ser
#  apenas "sem bonus" e passou a ser uma PENALIZACAO. Entre o que um guia
#  antigo promete (+10%) e o que uma alianca inchada da hoje (-20%) vao 30
#  pontos — mais do que qualquer peca de equipamento vale.
#
#  Fonte: warera.io/en/articles/v0-25-5-4a2c5d e warera.wiki/en/battle.
# ---------------------------------------------------------------------------
#  ATENCAO: quase tudo o que estava aqui escrito a mao esta na verdade
#  PUBLICADO no `gameConfig` (`battle.*`, `region.*`, `upgradesConfig.*`), e o
#  `warera/bonus.py` le-o de la. O que sobra neste dicionario e so o que o jogo
#  NAO publica: a curva da alianca e o tecto. Manter os outros aqui era ter
#  duas versoes do mesmo numero a divergirem com o tempo.
BONUS_BATALHA = {
    "alianca": {
        # o maximo vem do gameConfig; a CURVA nao esta publicada em lado nenhum
        "limiar_quota": 10.0,     # % do desenvolvimento mundial ate ao qual da o maximo
        "decaimento": 4.0,        # pontos de bonus perdidos por cada ponto de quota acima
        "piso": -20.0,            # e passa a penalizacao, nao fica em zero
        "o_que": "apoiar um membro da alianca; decai com a quota de desenvolvimento",
    },
    # o ritmo do inimigo jurado (o maximo vem do gameConfig) e a escada das
    # ordens por prioridade: o gameConfig so guarda um numero por ordem
    # (`countryOrderBonusPercent` 10), que e o do nivel medio — a escada
    # 5/10/15 esta na wiki e nao na config.
    "inimigo_jurado": {"por_dia": 1.0, "por_dia_isolacionista": 5.0},
    "ordens": {"baixa": 5.0, "media": 10.0, "alta": 15.0},
    "regiao_nuclear": {"max": 15.0, "o_que": "defender uma regiao nuclear do teu pais"},
}

# ---------------------------------------------------------------------------
#  As ETHICS do pais: a maior fonte de dano que faltava por completo ao painel.
#
#  Quatro ideologias, cada uma boa numa situacao so, em dois graus. Ate +15% que
#  nao apareciam em conta nenhuma:
#
#      militarista    a atacar
#      isolacionista  contra o inimigo jurado
#      pacifista      a defender o proprio pais
#      diplomatico    a lutar por um aliado
#
#  Isto NAO esta no `gameConfig`: os valores vem do simulador de batalha da
#  comunidade (battle-sim.warera.wiki). Pior ainda, `country.rulingParty` vem
#  como id e nao ha procedimento publico que o resolva — por isso o painel nem
#  consegue LER qual e a ideologia do teu pais. Tem de ser escrita a mao.
#
#  A wiki confirma que as ethics existem e mexem noutras coisas (custos de
#  papel de alianca, pacto e ordens), o que da confianca em que a familia esta
#  certa; as percentagens de dano e que ficam por confirmar contra o jogo.
# ---------------------------------------------------------------------------
ETHICS = {
    "fanatico": {"militarista": 15.0, "isolacionista": 15.0,
                 "pacifista": 15.0, "diplomatico": 15.0},
    "basico": {"militarista": 5.0, "isolacionista": 5.0,
               "pacifista": 5.0, "diplomatico": 5.0},
}

# O tecto de +95% (+115% em revoltas) vem do guia da comunidade e NAO da API
# nem da wiki: `battle.getDamageBonuses` recusa tokens, por isso nao ha maneira
# de o confirmar. Com os valores da v0.25.5 a soma dos bonus disponiveis passa
# dos 130%, o que torna o tecto duvidoso — pode ter subido com o resto. Fica
# aplicavel mas assinalado, e o painel diz quando ele esta a cortar.
TETO_BONUS_BATALHA_PCT = 95.0
TETO_BONUS_BATALHA_REVOLTA_PCT = 115.0
TETO_BONUS_BATALHA_VERIFICADO = False

# cada 1% de lootChance da 1% de hipotese de uma case por hit (e 0.01% de uma
# case de elite). E rendimento por HIT, e nao entrava em conta nenhuma.
CASE_POR_LOOTCHANCE = 0.01
CASE_ELITE_POR_LOOTCHANCE = 0.0001

# o relatorio faz ~90 pedidos, quase todos independentes: o que manda no tempo
# e a latencia, nao o CPU. Com 16 em paralelo o painel fica ~4s mais rapido e a
# API nao se queixa (ha retry com recuo para 429 de qualquer maneira).
MAX_WORKERS = 16
# quantos codigos de equipamento espreitar no mercado ao vivo. Cada um e um
# pedido; so vale a pena nos que o modelo de preco sabe avaliar.
MAX_CODIGOS_OFERTAS = 12
TTL_CACHE_CONFIG = 900
TTL_CACHE_MERCADO = 90
