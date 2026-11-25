"""
De onde vem cada preco, e quanto vale a pena acreditar nele.

Antes deste ficheiro o resto do painel recebia precos como um `dict[str, float]`
e nada mais. Um numero desses podia ser tres coisas muito diferentes — o topo do
livro de ordens de ha dez segundos, o preco de referencia da API, ou a mediana
de um modelo de regressao sobre vendas antigas — e a conta que os usava nao
tinha maneira de saber qual. O resultado foi o previsivel: um "custo por 1000 de
dano" calculado com um preco de equipamento modelado saia com quatro casas
decimais e a mesma cara de um calculado com o livro ao vivo.

Aqui os precos passam a ter proveniencia, como as regras ja tem em
`warera.regras`:

    fonte       livro | referencia | media_api | modelo | ausente
    confianca   alta | media | baixa | desconhecida
    obtido_em   quando, em epoch
    idade_s     ha quanto tempo
    stale       True se ja passou o prazo de validade da fonte

E ha uma distincao que este modulo obriga a fazer e que nao existia em lado
nenhum: **de que lado do livro**. Municao e comida COMPRAM-SE, por isso custam o
`ask`. Os scraps de uma peca desfeita VENDEM-SE, por isso rendem o `bid`. Usar o
mesmo numero para os dois lados sobrestima o que se recupera e subestima o que
se gasta, as duas na direccao que faz uma build parecer mais barata do que e.

O motor continua a receber um `dict[str, float]` simples — `mapa()` — porque
uma conta nao deve ter de saber de proveniencias. Quem apresenta chama
`cotacoes()` e diz ao utilizador de onde saiu cada numero.
"""

from __future__ import annotations

import time

from . import config

# Quanto tempo um preco de cada fonte ainda serve. Nao sao valores de gosto: o
# livro de ordens muda em segundos e o modelo de equipamento e feito sobre
# vendas de dias, por isso envelhecem a ritmos diferentes.
VALIDADE_S = {
    "livro": config.TTL_CACHE_MERCADO,
    "referencia": 300.0,
    "media_api": 900.0,
    "modelo": 86400.0,
    "ausente": 0.0,
}

CONFIANCA = {
    # o livro herda a confianca que o `mercado.resumo_livro` ja calculou
    "referencia": "media (preço de referência da API, não é o livro)",
    "media_api": ("baixa (média de vendas por código, gameStat, não documentado: sem roll, "
                  "sem data e sem amostra)"),
    "modelo": "baixa (mediana de um modelo, não uma venda)",
    "ausente": "desconhecida",
}

# O que cada numero E. Nenhuma destas fontes e uma venda real: sem transacoes
# (que exigem token) o painel nao tem vendas, e diz isso em vez de deixar uma
# media ou uma ordem passar por negocio fechado.
NATUREZA = {
    "livro": "ordem aberta (não é uma venda)",
    "referencia": "preço de referência do jogo (não é uma venda)",
    "media_api": "estimativa: média de vendas por código, sem roll",
    "modelo": "estimativa de um modelo",
    "ausente": "sem dados suficientes",
}
ENDPOINT = {
    "livro": "tradingOrder.getTopOrders",
    "referencia": "itemTrading.getPrices",
    "media_api": "gameStat.getEquipmentAvgByCode",
}

# O que se compra e o que se vende. Um bem que se compra custa o ask; um que se
# vende rende o bid. Tudo o que nao esta aqui nao tem lado obvio e fica no
# `ask`, que e a leitura conservadora — errar para o caro nao inventa lucro.
VENDE_SE = {"scraps"}


def agora() -> float:
    return time.time()


class Cotacao(dict):
    """
    Um preco com tudo o que e preciso para o julgar.

    Herda de `dict` de proposito: serializa para JSON sem conversao nenhuma e o
    frontend recebe-o como recebe tudo o resto.
    """

    def __init__(self, codigo: str, valor: float | None, fonte: str,
                 obtido_em: float | None = None, confianca: str | None = None,
                 lado: str | None = None, nota: str | None = None):
        quando = obtido_em if obtido_em is not None else agora()
        idade = max(0.0, agora() - quando)
        validade = VALIDADE_S.get(fonte, 0.0)
        super().__init__(
            codigo=codigo,
            valor=float(valor) if valor is not None else None,
            fonte=fonte,
            lado=lado,
            confianca=confianca or CONFIANCA.get(fonte, "desconhecida"),
            obtido_em=quando,
            idade_s=round(idade, 1),
            validade_s=validade,
            stale=(valor is None) or (validade > 0 and idade > validade),
            nota=nota,
            natureza=NATUREZA.get(fonte, "desconhecida"),
            endpoint=ENDPOINT.get(fonte),
            venda_real=False,
            estimativa=fonte in ("media_api", "modelo"),
        )

    @property
    def valor(self):
        return self["valor"]

    @property
    def stale(self) -> bool:
        return bool(self["stale"])


class ProvedorDeMercado:
    """
    A unica porta por onde os precos entram nas contas.

    Recebe as tres fontes que o painel ja tinha espalhadas — o preco de
    referencia da API, o panorama do livro de ordens, e os precos de
    equipamento (media da API ou modelo) — e resolve cada codigo pela melhor
    que houver, dizendo qual foi.

    A ordem de preferencia e a mesma sempre, e e a ordem de quao perto a fonte
    esta de uma transaccao a serio:

        1. livro de ordens, do lado certo
        2. preco de referencia da API
        3. media de vendas de equipamento
        4. modelo de regressao

    Um codigo que nenhuma delas conheca sai com `valor=None` e `fonte="ausente"`
    em vez de zero. Zero e um preco — e o preco que faz o dano por moeda ir ao
    infinito e o optimizador economico recomendar uma build que nao existe.
    """

    def __init__(self, referencia: dict | None = None,
                 panorama: list[dict] | dict | None = None,
                 equipamento: dict | None = None,
                 modelo: dict | None = None,
                 obtido_em: float | None = None,
                 obtido_em_livro: float | None = None):
        self.referencia = {k: float(v) for k, v in (referencia or {}).items()
                           if isinstance(v, (int, float))}
        self.equipamento = {k: float(v) for k, v in (equipamento or {}).items()
                            if isinstance(v, (int, float))}
        self.modelo = modelo or {}
        self._quando = obtido_em if obtido_em is not None else agora()
        self._quando_livro = (obtido_em_livro if obtido_em_livro is not None
                              else self._quando)
        self.livro: dict[str, dict] = {}
        if isinstance(panorama, dict):
            self.livro = {k: v for k, v in panorama.items() if isinstance(v, dict)}
        else:
            for m in panorama or []:
                if isinstance(m, dict) and m.get("codigo"):
                    self.livro[m["codigo"]] = m
        self._pedidos: dict[str, Cotacao] = {}

    # -- resolucao -----------------------------------------------------------

    def lado_de(self, codigo: str) -> str:
        return "bid" if codigo in VENDE_SE else "ask"

    def cotar(self, codigo: str, lado: str | None = None,
              registar: bool = True) -> Cotacao:
        """
        O melhor preco que ha para este codigo, com a proveniencia colada.

        `lado` diz se e para comprar (`ask`) ou para vender (`bid`); por
        omissao segue o que o bem e. Um livro que so tenha o outro lado nao
        serve — nao se compra ao preco a que alguem quer comprar — e cai-se
        para a fonte seguinte em vez de usar o lado errado.
        """
        lado = lado or self.lado_de(codigo)
        m = self.livro.get(codigo) or {}
        preco_livro = m.get("best_ask") if lado == "ask" else m.get("best_bid")
        if preco_livro:
            c = Cotacao(codigo, preco_livro, "livro", self._quando_livro,
                        confianca=m.get("confianca"), lado=lado)
        elif codigo in self.referencia:
            c = Cotacao(codigo, self.referencia[codigo], "referencia",
                        self._quando, lado=lado)
        elif codigo in self.equipamento:
            c = Cotacao(codigo, self.equipamento[codigo], "media_api",
                        self._quando, lado=lado)
        elif codigo in self.modelo:
            m = self.modelo[codigo]
            if isinstance(m, dict):
                # a mediana das vendas reais, e nao a reta: a reta extrapola
                # fora do intervalo em que ha vendas e ja disse que um rifle no
                # minimo valia 0,87 quando nenhum se vendeu abaixo de 11
                v = m.get("preco_mediano")
                nota = None if m.get("fiavel") else (
                    f"modelo pouco fiável (n={m.get('n')}, "
                    f"correlação {m.get('correlacao')})")
            else:
                v, nota = m, None
            c = Cotacao(codigo, v, "modelo", self._quando, lado=lado, nota=nota)
        else:
            c = Cotacao(codigo, None, "ausente", self._quando, lado=lado,
                        nota="nenhuma fonte conhece este código")
        if registar:
            self._pedidos[codigo] = c
        return c

    def preco(self, codigo: str, lado: str | None = None) -> float | None:
        """So o numero. `None` quando nao se sabe — nunca zero."""
        return self.cotar(codigo, lado).valor

    # -- para quem calcula ---------------------------------------------------

    def mapa(self, codigos: list[str] | None = None) -> dict[str, float]:
        """
        O `dict[str, float]` simples que o motor espera.

        So entram os codigos com preco conhecido: um bem sem preco fica de
        FORA do dicionario e nao la dentro a zero, para que `p.get(codigo)`
        devolva `None` e quem calcula possa dizer "sem preco" em vez de
        "de graca".

        Nao conta como pedido: montar o dicionario inteiro nao e o mesmo que
        usar um preco, e se contasse o `usados()` passava a listar o catalogo
        todo em vez do punhado de bens que a conta tocou.
        """
        alvos = codigos if codigos is not None else self.conhecidos()
        fora = {}
        for c in alvos:
            v = self.cotar(c, registar=False).valor
            if v is not None:
                fora[c] = v
        return fora

    def conhecidos(self) -> list[str]:
        return sorted(set(self.referencia) | set(self.equipamento)
                      | set(self.livro) | set(self.modelo))

    # -- para quem apresenta -------------------------------------------------

    def cotacoes(self, codigos: list[str] | None = None) -> dict[str, Cotacao]:
        alvos = codigos if codigos is not None else self.conhecidos()
        return {c: self.cotar(c) for c in alvos}

    def usados(self) -> dict[str, Cotacao]:
        """As cotacoes que esta conta foi mesmo buscar. E o rodape honesto."""
        return dict(self._pedidos)

    def estado(self, codigos: list[str] | None = None) -> dict:
        """
        Quao de confianca e o resultado que sair destes precos.

        `degradado` nao e "faltou um preco qualquer": e faltar um preco que a
        conta pediu, ou usar um que ja passou do prazo. Um painel que diz
        sempre que esta degradado e tao inutil como um que nunca diz.
        """
        usadas = (self.cotacoes(codigos) if codigos is not None else self.usados())
        por_fonte: dict[str, list[str]] = {}
        for codigo, c in usadas.items():
            por_fonte.setdefault(c["fonte"], []).append(codigo)
        for k in por_fonte:
            por_fonte[k].sort()
        em_falta = sorted(por_fonte.get("ausente", []))
        velhos = sorted(c["codigo"] for c in usadas.values()
                        if c["stale"] and c["fonte"] != "ausente")
        idades = [c["idade_s"] for c in usadas.values() if c["fonte"] != "ausente"]
        return {
            "n": len(usadas),
            "por_fonte": por_fonte,
            "em_falta": em_falta,
            "obsoletos": velhos,
            "idade_maxima_s": round(max(idades), 1) if idades else None,
            "degradado": bool(em_falta or velhos),
            "porque": _porque(em_falta, velhos),
        }


def _porque(em_falta: list[str], velhos: list[str]) -> str | None:
    partes = []
    if em_falta:
        partes.append("sem preço para " + ", ".join(em_falta[:6])
                      + (f" e mais {len(em_falta) - 6}" if len(em_falta) > 6 else ""))
    if velhos:
        partes.append("preço fora de prazo em " + ", ".join(velhos[:6])
                      + (f" e mais {len(velhos) - 6}" if len(velhos) > 6 else ""))
    return "; ".join(partes) or None


def do_painel(referencia: dict | None, relatorio: dict | None = None,
              obtido_em: float | None = None) -> ProvedorDeMercado:
    """
    O provedor montado a partir do que o painel ja tem em maos.

    `relatorio` e o que `api.em_cache("mercado:")` devolve: traz o panorama do
    livro, os precos de equipamento e o modelo das pecas. Existe para que o
    servidor nao tenha de saber a forma interna desse relatorio em cinco rotas
    diferentes — que era exactamente o que estava a acontecer, com um
    `next(... codigo == "scraps")` copiado em quatro sitios.
    """
    rel = relatorio or {}
    return ProvedorDeMercado(
        referencia=referencia,
        panorama=rel.get("panorama"),
        equipamento=rel.get("precos_equipamento"),
        modelo=(rel.get("pecas") or {}).get("modelo"),
        obtido_em=obtido_em)
