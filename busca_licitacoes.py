#!/usr/bin/env python3
"""
Busca Licitações (Soluções Comerciais)
--------------------------------------
Licitações abertas do PNCP com filtro por PORTAL (Compras.gov.br, ComprasMG, SISLOG, Licitar Digital...).

Etapa 1 (agora): diagnóstico para descobrir como o PNCP informa o portal de origem de cada licitação.

Como rodar:  python3 busca_licitacoes.py   (no Render: Start Command = python busca_licitacoes.py)
Não precisa instalar nada além do Python 3.8+.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# O servidor do Render usa o horário de Londres; as datas do PNCP são de Brasília.
os.environ["TZ"] = "America/Sao_Paulo"
try:
    time.tzset()
except AttributeError:  # Windows não tem tzset
    pass

PORT = int(os.environ.get("PORT", "8000"))
PNCP = "https://pncp.gov.br/api/consulta/v1"
UA = "busca-licitacoes/1.0"


def erro_txt(e):
    if isinstance(e, urllib.error.HTTPError):
        corpo = ""
        try:
            corpo = e.read()[:300].decode("utf-8", "replace")
        except Exception:
            pass
        return f"HTTP {e.code} {corpo}"
    if isinstance(e, urllib.error.URLError):
        return f"sem conexão ({e.reason})"
    return str(e)[:200]


def http_get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if not data.strip():
        return None
    return json.loads(data.decode("utf-8"))


# Conexões reaproveitadas com o PNCP: abrir conexão segura (https) a cada página gasta muito
# processamento, e o servidor gratuito do Render é bem fraco. Assim abre uma vez e reaproveita.
import http.client
import ssl
import threading
_conexoes, _conexoes_lock = {}, threading.Lock()
_SSL = ssl.create_default_context()


_estat = {"inicio": time.strftime("%d/%m %H:%M"), "pedidos": 0, "ok": 0, "segundos_ok": 0.0, "codigos": Counter(),
          "falhas": Counter(), "ultimos_erros": []}


def _anota(t0, codigo=None, erro=None):
    with _conexoes_lock:
        _estat["pedidos"] += 1
        if codigo == 200:
            _estat["ok"] += 1
            _estat["segundos_ok"] += time.time() - t0
        elif codigo is not None:
            _estat["codigos"][codigo] += 1
        if erro:
            _estat["falhas"][erro[:60]] += 1
            _estat["ultimos_erros"] = (_estat["ultimos_erros"] + [time.strftime("%H:%M:%S ") + erro[:120]])[-8:]


def diagnostico_velocidade():
    e = _estat
    media = e["segundos_ok"] / e["ok"] if e["ok"] else 0
    linhas = [f"DIAGNOSTICO VELOCIDADE (desde {e['inicio']})",
              f"pedidos ao PNCP: {e['pedidos']} | deram certo: {e['ok']} | tempo medio: {media:.1f}s",
              f"recusas do PNCP (codigo: vezes): {dict(e['codigos']) or 'nenhuma'}",
              f"falhas de conexao: {dict(e['falhas']) or 'nenhuma'}",
              f"estados guardados: {sorted({k.split('|')[0] for k in _cache if '|' in k and not k.startswith('lic')})}",
              "ultimos erros:"] + (e["ultimos_erros"] or ["nenhum"])
    return "\n".join(linhas)


def http_get_rapido(url, timeout=45):
    """Igual ao http_get, mas reaproveita a conexão. Erros viram urllib.error.HTTPError como antes."""
    u = urllib.parse.urlparse(url)
    host, caminho = u.netloc, u.path + ("?" + u.query if u.query else "")
    for tentativa in (1, 2):
        with _conexoes_lock:
            livres = _conexoes.setdefault(host, [])
            c = livres.pop() if livres else None
        nova = c is None
        if nova:
            proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
            if proxy:  # (só no ambiente de teste)
                pr = urllib.parse.urlparse(proxy)
                c = http.client.HTTPSConnection(pr.hostname, pr.port, timeout=timeout, context=_SSL)
                c.set_tunnel(host, 443)
            else:
                c = http.client.HTTPSConnection(host, timeout=timeout, context=_SSL)
        t0 = time.time()
        try:
            c.timeout = timeout
            if c.sock is not None:
                c.sock.settimeout(timeout)
            c.request("GET", caminho, headers={"User-Agent": UA, "Accept": "application/json", "Connection": "keep-alive"})
            r = c.getresponse()
            corpo = r.read()
        except Exception as e:
            c.close()
            if nova or tentativa == 2:
                _anota(t0, erro=type(e).__name__ + ": " + str(e))
                raise
            continue  # conexão velha que o PNCP fechou: tenta com uma nova
        _anota(t0, codigo=r.status)
        if r.getheader("Connection", "").lower() == "close":
            c.close()
        else:
            with _conexoes_lock:
                if len(_conexoes[host]) < 8:
                    _conexoes[host].append(c)
                else:
                    c.close()
        if r.status != 200:
            import io
            raise urllib.error.HTTPError(url, r.status, r.reason, r.headers, io.BytesIO(corpo))
        if not corpo.strip():
            return None
        return json.loads(corpo.decode("utf-8"))


def dominio(url):
    try:
        return urllib.parse.urlparse(str(url or "")).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def diagnostico():
    """Lê algumas páginas de licitações abertas e conta os portais de origem."""
    linhas = ["DIAGNOSTICO BUSCA LICITACOES", ""]
    fim = (date.today() + timedelta(days=60)).strftime("%Y%m%d")
    usuarios, dominios, exemplos = Counter(), Counter(), {}
    primeiro = None
    for modalidade in (6, 8, 4, 5):  # 6 pregão eletrônico, 8 dispensa, 4 concorrência eletrônica, 5 concorrência presencial
        for pagina in range(1, 6):
            url = f"{PNCP}/contratacoes/proposta?dataFinal={fim}&codigoModalidadeContratacao={modalidade}&pagina={pagina}&tamanhoPagina=50"
            t0 = time.time()
            try:
                d = http_get(url)
            except Exception as e:
                linhas.append(f"modalidade {modalidade} pagina {pagina}: ERRO {erro_txt(e)} ({time.time() - t0:.1f}s)")
                break
            lst = (d or {}).get("data") or []
            if pagina == 1:
                linhas.append(f"modalidade {modalidade}: total={(d or {}).get('totalRegistros')} paginas={(d or {}).get('totalPaginas')} ({time.time() - t0:.1f}s)")
            for c in lst:
                if primeiro is None:
                    primeiro = c
                u = str(c.get("usuarioNome") or "?")
                dm = dominio(c.get("linkSistemaOrigem"))
                usuarios[u] += 1
                dominios[dm] += 1
                exemplos.setdefault(u, c.get("linkSistemaOrigem"))
            if not lst or (d or {}).get("paginasRestantes", 0) <= 0:
                break
            time.sleep(0.3)
    linhas.append("")
    linhas.append("== portais (usuarioNome): quantidade | exemplo de link")
    for u, n in usuarios.most_common(80):
        linhas.append(f"  {n:4d} | {u} | {exemplos.get(u)}")
    linhas.append("")
    linhas.append("== dominios do linkSistemaOrigem")
    for dm, n in dominios.most_common(60):
        linhas.append(f"  {n:4d} | {dm}")
    linhas.append("")
    linhas.append("== campos de um registro (exemplo)")
    if primeiro:
        linhas.append(json.dumps(primeiro, ensure_ascii=False, indent=1)[:4000])
    linhas.append("")
    linhas.append("== busca rapida do site do PNCP (teste)")
    for url in ("https://pncp.gov.br/api/search/?tipos_documento=edital&ordenacao=-data&pagina=1&tam_pagina=5&status=recebendo_proposta",):
        t0 = time.time()
        try:
            d = http_get(url, timeout=40)
            itens = (d or {}).get("items") or (d or {}).get("data") or []
            linhas.append(f"  OK ({time.time() - t0:.1f}s) total={(d or {}).get('total')} chaves={list((d or {}).keys())}")
            if itens:
                linhas.append("  item: " + json.dumps(itens[0], ensure_ascii=False)[:2500])
        except Exception as e:
            linhas.append(f"  ERRO {erro_txt(e)} ({time.time() - t0:.1f}s)")
    return "\n".join(linhas)


SEARCH = "https://pncp.gov.br/api/search/"


def diagnostico_busca():
    """Testa os filtros da busca rápida do PNCP e conta os portais escritos entre [colchetes]."""
    linhas = ["DIAGNOSTICO BUSCA RAPIDA", ""]
    base = {"tipos_documento": "edital", "status": "recebendo_proposta", "pagina": 1}
    testes = [
        ("tam 100", {"tam_pagina": 100, "ordenacao": "-data"}),
        ("tam 500", {"tam_pagina": 500, "ordenacao": "-data"}),
        ("uf DF", {"tam_pagina": 5, "ufs": "DF"}),
        ("uf MG (campo uf)", {"tam_pagina": 5, "uf": "MG"}),
        ("modalidade 6", {"tam_pagina": 5, "modalidades": "6"}),
        ("q=cadeira", {"tam_pagina": 5, "q": "cadeira"}),
        ("q=\"Compras.gov.br\"", {"tam_pagina": 5, "q": "\"Compras.gov.br\""}),
        ("ordem data_fim", {"tam_pagina": 5, "ordenacao": "data_fim_vigencia"}),
        ("ordem -valor", {"tam_pagina": 5, "ordenacao": "-valor_global"}),
        ("ordem relevancia", {"tam_pagina": 5, "ordenacao": "-relevancia"}),
    ]
    prefixos = Counter()
    for nome, extra in testes:
        q = dict(base, **extra)
        url = SEARCH + "?" + urllib.parse.urlencode(q)
        t0 = time.time()
        try:
            d = http_get(url, timeout=40) or {}
            itens = d.get("items") or []
            amostra = "; ".join(f"{i.get('uf')} {i.get('modalidade_licitacao_id')} fim={str(i.get('data_fim_vigencia'))[:10]} {str(i.get('orgao_nome'))[:25]}" for i in itens[:3])
            linhas.append(f"{nome}: {time.time() - t0:.1f}s total={d.get('total')} veio={len(itens)} | {amostra}")
            for i in itens:
                m = re.match(r"\s*\[([^\]]{2,120})\]", str(i.get("description") or ""))
                prefixos[m.group(1) if m else "(sem colchetes)"] += 1
        except Exception as e:
            linhas.append(f"{nome}: ERRO {erro_txt(e)} ({time.time() - t0:.1f}s)")
        time.sleep(0.3)
    linhas.append("")
    linhas.append("== portais escritos entre [colchetes] na descricao (amostra)")
    for p_, n in prefixos.most_common(60):
        linhas.append(f"  {n:4d} | {p_}")
    for termo in ("sislog", "licitar", "minas gerais", "compras.gov"):
        try:
            d = http_get(SEARCH + "?" + urllib.parse.urlencode(dict(base, tam_pagina=3, q=termo)), timeout=40) or {}
            linhas.append(f"q={termo}: total={d.get('total')} | " + " || ".join(str(i.get("description"))[:90] for i in (d.get("items") or [])))
        except Exception as e:
            linhas.append(f"q={termo}: ERRO {erro_txt(e)}")
    return "\n".join(linhas)


# ================================================================ busca
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor

MODALIDADES = {
    6: "Pregão Eletrônico", 8: "Dispensa", 4: "Concorrência Eletrônica", 7: "Pregão Presencial",
    5: "Concorrência Presencial", 12: "Credenciamento", 9: "Inexigibilidade", 1: "Leilão Eletrônico",
    13: "Leilão Presencial", 3: "Concurso", 2: "Diálogo Competitivo", 11: "Pré-qualificação",
    10: "Manifestação de Interesse",
}
MODALIDADES_PADRAO = (6, 8, 4, 7, 5, 12)

# Portais que a usuária mais usa: nome que aparece no PNCP ou endereço do link -> nome curto
PORTAIS_CONHECIDOS = [
    ("Compras.gov.br", ("compras.gov.br",), ("cnetmobile.estaleiro.serpro.gov.br", "comprasnet.gov.br", "compras.gov.br/")),
    ("ComprasMG", ("compras do estado de minas gerais", "comprasmg"), ("compras.mg.gov.br",)),
    ("Licitar Digital", ("licitar digital", "libre solucoes em tecnologia"), ("licitardigital.com.br",)),
    ("SISLOG", ("sislog", "estado de goias", "administracao de goias", "governo de goias"), ("sislog", "comprasnet.go.gov.br", ".go.gov.br")),
    ("BLL Compras", ("bll compras",), ("bllcompras.com",)),
    ("Licitanet", ("licitanet",), ("licitanet.com.br",)),
    ("Portal de Compras Públicas", ("ecustomize", "portal de compras publicas"), ("portaldecompraspublicas.com.br",)),
    ("BNC Compras", ("bolsa nacional de compras",), ("bnccompras.com",)),
    ("Compras RJ", ("compras publicas do estado do rio de janeiro",), ("compras.rj.gov.br",)),
    ("Banrisul (RS)", ("procergs",), ("pregaobanrisul.com.br", "compras.rs.gov.br")),
]


def sem_acento(s):
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()


# Categoria -> palavras (sem acento) que aparecem no objeto da licitação
CATEGORIAS = [
    ("Eletrodomésticos", ["eletrodomestic", "eletroportat", "liquidificador", "geladeira", "refrigerador", "freezer", "frigobar",
                          "micro-ondas", "microondas", "micro ondas", "fogao", "forno eletrico", "ventilador", "climatizador",
                          "ar condicionado", "ar-condicionado", "condicionador de ar", "bebedouro", "purificador de agua",
                          "cafeteira", "sanduicheira", "fritadeira", "air fryer", "airfryer", "batedeira", "espremedor",
                          "chaleira eletrica", "ferro de passar", "aspirador", "lavadora", "maquina de lavar", "secadora",
                          "televisor", "televisao", "smart tv", "umidificador", "exaustor", "coifa", "cooktop", "processador de alimentos"]),
    ("Material de escritório", ["material de escritorio", "material de expediente", "papel a4", "papel sulfite", "papel oficio",
                                "caneta", "lapis", "grampeador", "grampo", "clips", "envelope", "marca texto", "marca-texto",
                                "papelaria", "arquivo morto", "corretivo", "perfurador", "post-it", "bloco de notas", "pasta suspensa"]),
    ("Informática", ["informatica", "computador", "notebook", "desktop", "monitor", "impressora", "multifuncional", "toner",
                     "cartucho", "teclado", "mouse", "nobreak", "no-break", "servidor de rede", "switch", "roteador",
                     "tablet", "ssd", "pen drive", "pendrive", "projetor", "webcam", "licenca de software", "tecnologia da informacao"]),
    ("Limpeza e higiene", ["limpeza", "higiene", "detergente", "desinfetante", "sabao", "agua sanitaria", "papel higienico",
                           "papel toalha", "saco de lixo", "sacos de lixo", "vassoura", "rodo", "alcool", "sabonete", "limpador",
                           "conservacao e limpeza", "fralda", "absorvente", "lenco umedecido", "lencos umedecidos",
                           "creme dental", "escova dental", "escovas dentais", "shampoo", "xampu", "desodorante"]),
    ("Alimentos", ["genero alimenticio", "generos alimenticios", "alimento", "merenda", "alimentacao", "cafe ", "acucar",
                   "leite", "carne", "hortifruti", "frutas", "verduras", "cesta basica", "refeic", "lanche", "agua mineral", "pao "]),
    ("Copa e cozinha", ["copo descartavel", "copos descartaveis", "copa e cozinha", "utensilio", "panela", "talher", "prato",
                        "garrafa termica", "guardanapo", "filtro de papel", "pratos descartave", "prato descartave",
                        "talher descartave", "talheres descartave", "garfo descartave", "garfos descartave", "faca descartave",
                        "facas descartave", "colher descartave", "colheres descartave", "marmitex", "embalagem para alimento",
                        "embalagens para alimento", "material de copa"]),
    ("Móveis", ["mobiliario", "moveis", "cadeira", "mesa ", "mesas", "armario", "estante", "arquivo de aco", "sofa", "poltrona",
                "balcao", "gaveteiro", "longarina"]),
    ("Material de construção", ["material de construcao", "materiais de construcao", "material eletrico", "materiais eletricos",
                               "material hidraulico", "materiais hidraulicos", "tinta", "cimento", "ferramenta", "lampada",
                               "fio eletrico", "cabo eletrico", "tubo pvc", "tubos", "conexoes", "torneira", "fechadura",
                               "parafuso", "areia", "brita", "tijolo", "madeira", "telha"]),
    ("Veículos, peças e combustível", ["veiculo", "pneu", "pecas automotiv", "peca automotiv", "combustivel", "gasolina", "diesel",
                                      "oleo lubrificante", "motocicleta", "moto", "onibus", "ambulancia", "caminhao", "automotiv", "frota"]),
    ("Saúde e medicamentos", ["medicamento", "hospitalar", "odontolog", "laboratori", "luva", "mascara", "seringa", "curativo",
                             "equipamento medico", "material medico", "saude", "farmac", "insumos medic"]),
    ("Uniformes, calçados e EPI", ["uniforme", "fardamento", "camiseta", "camisa", "calcado", "bota", "epi ", "equipamento de protecao",
                                  "colete", "jaleco", "bone"]),
    ("Eventos, gráfica e brindes", ["grafica", "impressao de", "banner", "brinde", "evento", "buffet", "coffee break", "decoracao"]),
]
ASSISTENCIA = ["assistencia tecnica", "garantia on site", "garantia on-site", "garantia onsite", "rede autorizada", "instalacao"]


def _padrao(palavra):
    """A palavra tem que começar no início de uma palavra do texto (a checagem fica em _CAT_RE). Palavras curtas
    também têm que terminar ali (aceitando plural), para "rodo" não pegar "rodovia"."""
    p = re.escape(palavra.strip())
    if len(palavra.strip()) <= 6:
        return p + r"(?:s|es)?(?![a-z])"
    return p


_CAT_RE = [(nome, re.compile(r"(?<![a-z])(?:" + "|".join(_padrao(x) for x in palavras) + ")")) for nome, palavras in CATEGORIAS]


SERVICO = ["prestacao de servico", "prestacao de servicos", "servicos de", "servico de", "servicos continuos", "mao de obra",
           "obra", "obras", "reforma", "pavimentacao", "pavimento", "recapeamento", "engenharia", "execucao de", "terceirizacao",
           "terceirizado", "vigilancia", "consultoria", "assessoria", "locacao", "manutencao preventiva", "manutencao corretiva",
           "manutencao predial", "limpeza e conservacao", "transporte de", "custodia", "auditoria", "treinamento", "capacitacao",
           "credenciamento de", "selecao de pessoas", "agenciamento", "seguro", "publicidade", "buffet", "coffee break"]
PRODUTO = ["aquisicao", "fornecimento de material", "fornecimento de materiais", "fornecimento de produto", "compra de",
           "material", "materiais", "equipamento", "produto", "genero", "insumo", "medicamento"]
_SERV_RE = re.compile(r"(?<![a-z])(?:" + "|".join(re.escape(x) for x in SERVICO) + r")(?![a-z])")
_PROD_RE = re.compile(r"(?<![a-z])(?:" + "|".join(re.escape(x) for x in PRODUTO) + ")")


def tipo_de(texto):
    """'Produto' ou 'Serviço', pelas palavras do objeto. Na dúvida, fica como produto (para não esconder oportunidade)."""
    t = sem_acento(texto)
    if _SERV_RE.search(t):
        # "aquisição de peças... incluindo instalação" continua produto; "prestação de serviços... e fornecimento de peças" é serviço
        if _PROD_RE.search(t) and not re.search(r"(?<![a-z])(prestacao de servic|servicos? de|mao de obra|obras?|reforma|pavimenta|engenharia|locacao|terceiriza|vigilancia|consultoria|assessoria)", t):
            return "Produto"
        return "Serviço"
    return "Produto"


def categorias_de(texto):
    t = sem_acento(texto).replace("mao de obra", "mao-de-servico")
    cats = [nome for nome, rx in _CAT_RE if rx.search(t)]
    return cats or ["Outros"]


def portal_de(c):
    usuario = sem_acento(c.get("usuarioNome"))
    links = " ".join(str(c.get(k) or "").lower() for k in ("linkSistemaOrigem", "linkProcessoEletronico"))
    for nome, nomes, dominios in PORTAIS_CONHECIDOS:
        if any(n in usuario for n in nomes) or any(d in links for d in dominios):
            return nome
    return str(c.get("usuarioNome") or "Não informado").strip()[:60]


def link_comprasgov(c):
    """Link da página pública de acompanhamento no Compras.gov.br (pregão e dispensa)."""
    uasg = re.sub(r"\D", "", str((c.get("unidadeOrgao") or {}).get("codigoUnidade") or ""))
    num = re.sub(r"\D", "", str(c.get("numeroCompra") or ""))
    mod = {6: "05", 8: "06"}.get(c.get("modalidadeId"))
    if len(uasg) != 6 or not num or not mod or not c.get("anoCompra"):
        return ""
    return ("https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-web/public/compras/acompanhamento-compra?compra="
            f"{uasg}{mod}{int(num):05d}{c['anoCompra']}")


def resumir(c, portal):
    org, und = c.get("orgaoEntidade") or {}, c.get("unidadeOrgao") or {}
    cnpj = re.sub(r"\D", "", str(org.get("cnpj") or ""))
    controle = str(c.get("numeroControlePNCP") or "")
    pncp = ""
    m = re.match(r"^(\d{14})-\d+-(\d+)/(\d{4})$", controle)
    if m:
        pncp = f"https://pncp.gov.br/app/editais/{m.group(1)}/{m.group(3)}/{int(m.group(2))}"
    origem = c.get("linkSistemaOrigem") or ""
    if origem and not str(origem).startswith("http"):
        origem = "https://" + str(origem)
    return {
        "id": controle,
        "numero": f"{c.get('numeroCompra') or ''}/{c.get('anoCompra') or ''}".strip("/"),
        "modalidade": c.get("modalidadeId"),
        "modalidadeNome": c.get("modalidadeNome") or MODALIDADES.get(c.get("modalidadeId"), ""),
        "disputa": c.get("modoDisputaNome") or "",
        "srp": bool(c.get("srp")),
        "orgao": org.get("razaoSocial") or "",
        "cnpj": cnpj,
        "unidade": und.get("nomeUnidade") or "",
        "uasg": str(und.get("codigoUnidade") or ""),
        "municipio": und.get("municipioNome") or "",
        "uf": und.get("ufSigla") or "",
        "objeto": re.sub(r"\s+", " ", str(c.get("objetoCompra") or "")).strip(),
        "valor": c.get("valorTotalEstimado"),
        "abertura": str(c.get("dataAberturaProposta") or "")[:16],
        "encerramento": str(c.get("dataEncerramentoProposta") or "")[:16],
        "publicacao": str(c.get("dataPublicacaoPncp") or "")[:16],
        "portal": portal,
        "categorias": categorias_de(str(c.get("objetoCompra") or "")),
        "tipo": tipo_de(str(c.get("objetoCompra") or "")),
        "assistencia": ("Eletrodomésticos" in categorias_de(str(c.get("objetoCompra") or "")))
                       or any(p in sem_acento(str(c.get("objetoCompra") or "") + " " + str(c.get("informacaoComplementar") or "")) for p in ASSISTENCIA),
        "complemento": re.sub(r"\s+", " ", str(c.get("informacaoComplementar") or "")).strip()[:1500],
        "processo": str(c.get("processo") or ""),
        "amparo": (c.get("amparoLegal") or {}).get("nome") or "",
        "esfera": {"F": "Federal", "E": "Estadual", "M": "Municipal", "D": "Distrital"}.get(org.get("esferaId"), ""),
        "poder": {"E": "Executivo", "L": "Legislativo", "J": "Judiciário", "N": "Não se aplica"}.get(org.get("poderId"), ""),
        "situacao": c.get("situacaoCompraNome") or "",
        "linkPncp": pncp,
        "linkPortal": origem or (link_comprasgov(c) if portal == "Compras.gov.br" else ""),
    }


_PNCP_VAGAS = threading.BoundedSemaphore(4)  # no máximo 4 pedidos ao PNCP ao mesmo tempo (ele bloqueia quem pede demais)


def _pagina_proposta(uf, modalidade, pagina, tentativas=4):
    fim = (date.today() + timedelta(days=120)).strftime("%Y%m%d")
    url = (f"{PNCP}/contratacoes/proposta?dataFinal={fim}&codigoModalidadeContratacao={modalidade}"
           f"&uf={uf}&pagina={pagina}&tamanhoPagina=50")
    for t in range(1, tentativas + 1):
        try:
            with _PNCP_VAGAS:
                return http_get_rapido(url, timeout=45) or {}
        except urllib.error.HTTPError as e:
            if e.code == 204:
                return {}
            if (e.code < 500 and e.code != 429) or t == tentativas:
                raise  # antes, "muitos pedidos" (429) virava lista vazia sem aviso
            time.sleep((5 if e.code == 429 else 2) * t)
            continue
        except Exception:
            if t == tentativas:
                raise
        time.sleep(2 * t)
    return {}


MAX_PAGINAS = 100  # até 5.000 licitações por modalidade em cada estado
_cache, _lock = {}, threading.Lock()


def buscar_modalidade(uf, modalidade):
    chave = f"{uf}|{modalidade}"
    with _lock:
        if chave in _cache and time.time() - _cache[chave][0] < 7200:  # guarda 2 horas
            return _cache[chave][1]
    primeira = _pagina_proposta(uf, modalidade, 1)
    itens = list(primeira.get("data") or [])
    total = primeira.get("totalRegistros") or len(itens)
    paginas = min(MAX_PAGINAS, primeira.get("totalPaginas") or 1)
    cortado = (primeira.get("totalPaginas") or 1) > MAX_PAGINAS
    if paginas > 1:
        with ThreadPoolExecutor(max_workers=3) as ex:  # poucos ao mesmo tempo, para o PNCP não cortar
            for r in ex.map(lambda pg: _pagina_proposta(uf, modalidade, pg), range(2, paginas + 1)):
                itens += r.get("data") or []
    agora = time.strftime("%Y-%m-%dT%H:%M")
    lista = []
    for c in itens:
        r = resumir(c, portal_de(c))
        if r["encerramento"] and r["encerramento"] < agora:
            continue  # prazo de proposta já acabou
        r.pop("complemento", None)  # a página da licitação busca o texto completo; a lista fica mais leve
        lista.append(r)
    res = {"itens": lista, "total": total, "cortado": cortado}
    with _lock:
        _cache[chave] = (time.time(), res)
    return res


PNCP_API = "https://pncp.gov.br/api/pncp/v1"


def _get_tentando(url, tentativas=3):
    for t in range(1, tentativas + 1):
        try:
            return http_get(url, timeout=40)
        except urllib.error.HTTPError as e:
            if e.code in (204, 404):
                return None
            if e.code in (301, 302, 307, 308):
                raise
            if e.code < 500 and e.code != 429:
                raise
        except Exception:
            if t == tentativas:
                raise
        time.sleep(1.5 * t)
    return None


def _get_bases(caminho):
    """O PNCP mudou alguns endereços de /api/pncp/v1 para /api/consulta/v1: tenta os dois."""
    ultimo = None
    for base in (PNCP, PNCP_API):
        try:
            r = _get_tentando(base + caminho)
            if r is not None:
                return r
        except urllib.error.HTTPError as e:
            ultimo = e
            if e.code not in (301, 302, 307, 308, 400, 404, 405):
                raise
    if ultimo is not None and ultimo.code not in (301, 302, 307, 308, 404):
        raise ultimo
    return None


def detalhes(id_pncp):
    """Documentos (edital, TR, anexos) e itens de uma licitação, direto do PNCP."""
    m = re.match(r"^(\d{14})-\d+-(\d+)/(\d{4})$", id_pncp or "")
    if not m:
        raise ValueError("Licitação inválida.")
    cnpj, seq, ano = m.group(1), int(m.group(2)), m.group(3)
    chave = f"det|{id_pncp}"
    with _lock:
        if chave in _cache and time.time() - _cache[chave][0] < 3600:
            return _cache[chave][1]
    caminho = f"/orgaos/{cnpj}/compras/{ano}/{seq}"
    base = f"{PNCP_API}{caminho}"
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_arq = ex.submit(_get_bases, f"{caminho}/arquivos")
        f_itens = ex.submit(_get_bases, f"{caminho}/itens?pagina=1&tamanhoPagina=500")
        arquivos, itens, erros = [], [], []
        try:
            for a in (f_arq.result() or []):
                if a.get("statusAtivo") is False:
                    continue
                url = a.get("url") or a.get("uri") or ""
                if not url and a.get("sequencialDocumento") is not None:
                    url = f"{base}/arquivos/{a['sequencialDocumento']}"
                arquivos.append({"titulo": a.get("titulo") or a.get("nomeArquivo") or "Documento",
                                 "tipo": a.get("tipoDocumentoNome") or a.get("tipoDocumentoDescricao") or "",
                                 "data": str(a.get("dataPublicacaoPncp") or "")[:10], "url": url})
        except Exception as e:
            erros.append("documentos: " + erro_txt(e))
        try:
            r = f_itens.result() or []
            r = r.get("data", []) if isinstance(r, dict) else r
            for i in r:
                itens.append({"n": i.get("numeroItem"), "descricao": re.sub(r"\s+", " ", str(i.get("descricao") or "")).strip(),
                              "qtd": i.get("quantidade"), "un": i.get("unidadeMedida") or "",
                              "vu": i.get("valorUnitarioEstimado"), "vt": i.get("valorTotal"),
                              "tipo": i.get("materialOuServicoNome") or "",
                              "me": i.get("tipoBeneficioNome") or "", "situacao": i.get("situacaoCompraItemNome") or ""})
        except Exception as e:
            erros.append("itens: " + erro_txt(e))
    res = {"arquivos": arquivos, "itens": sorted(itens, key=lambda x: x["n"] or 0), "erros": erros}
    if not erros:
        with _lock:
            _cache[chave] = (time.time(), res)
    return res


def licitacao(id_pncp):
    """Dados completos de uma licitação (para a página de detalhes)."""
    m = re.match(r"^(\d{14})-\d+-(\d+)/(\d{4})$", id_pncp or "")
    if not m:
        raise ValueError("Licitação inválida.")
    cnpj, seq, ano = m.group(1), int(m.group(2)), m.group(3)
    chave = f"lic|{id_pncp}"
    with _lock:
        if chave in _cache and time.time() - _cache[chave][0] < 7200:  # guarda 2 horas
            return _cache[chave][1]
    c = _get_bases(f"/orgaos/{cnpj}/compras/{ano}/{seq}")
    if not c:
        raise ValueError("Não encontrei essa licitação no PNCP.")
    res = resumir(c, portal_de(c))
    res["id"] = res["id"] or id_pncp
    with _lock:
        _cache[chave] = (time.time(), res)
    return res


CONSULTA_API = os.environ.get("CONSULTA_PAGADOR", "https://consulta-orgaos.onrender.com") + "/api/consulta?cnpj="
_vaga_score = threading.Semaphore(2)  # no máximo 2 consultas ao mesmo tempo no Consulta Pagador


def nota_orgao(cnpj):
    """Pergunta ao Consulta Pagador a nota do órgão e guarda por 24 horas."""
    cnpj = re.sub(r"\D", "", cnpj or "")
    if len(cnpj) != 14:
        raise ValueError("CNPJ inválido.")
    chave = f"nota|{cnpj}"
    with _lock:
        if chave in _cache and time.time() - _cache[chave][0] < 86400:
            return _cache[chave][1]
    with _vaga_score:
        with _lock:  # outra pessoa pode ter buscado enquanto esperava a vaga
            if chave in _cache and time.time() - _cache[chave][0] < 86400:
                return _cache[chave][1]
        d = http_get(CONSULTA_API + cnpj, timeout=150) or {}
    a = d.get("avaliacao") or {}
    res = {"nivel": a.get("nivel") or ("privado" if d.get("esfera") == "privado" else "sem_dados"),
           "titulo": a.get("titulo") or "", "score": a.get("score"),
           "prazo": (a.get("prazo") or {}).get("dias")}
    with _lock:
        _cache[chave] = (time.time(), res)
    return res


def buscar(uf, modalidades):
    uf = re.sub(r"[^A-Z]", "", uf.upper())[:2]
    if len(uf) != 2:
        raise ValueError("Escolha um estado.")
    mods = [m for m in modalidades if m in MODALIDADES] or list(MODALIDADES_PADRAO)
    itens, erros, cortadas, vistos = [], [], [], set()
    with ThreadPoolExecutor(max_workers=2) as ex:
        futuros = {m: ex.submit(buscar_modalidade, uf, m) for m in mods}
        for m, f in futuros.items():
            try:
                r = f.result()
                for i in r["itens"]:
                    if i["id"] not in vistos:
                        vistos.add(i["id"])
                        itens.append(i)
                if r["cortado"]:
                    cortadas.append(MODALIDADES[m])
            except Exception as e:
                erros.append(f"{MODALIDADES[m]}: {erro_txt(e)}")
    return {"uf": uf, "itens": itens, "erros": erros, "cortadas": cortadas,
            "atualizado": time.strftime("%d/%m/%Y %H:%M")}


# ================================================================ e-mail diário
# Configuração (no Render, em Environment):
#   RESEND_API_KEY  chave do serviço de e-mail Resend (grátis)
#   ALERTA_EMAIL    e-mail que recebe o resumo (o mesmo da conta do Resend)
#   ALERTA_CHAVE    uma senha qualquer, que o despertador (cron-job.org) usa para chamar o envio
#   ALERTA_UF       estado(s), separados por vírgula. Padrão: DF
#   ALERTA_PORTAIS  portal(is), separados por vírgula. Padrão: Compras.gov.br
#   ALERTA_PALAVRAS palavras-chave separadas por vírgula (vazio = todos os assuntos)
SITE = os.environ.get("SITE_URL", "https://busca-licitacoes-j9vl.onrender.com")
_envio = {"rodando": False, "ultimo": "", "dia": ""}  # "dia": data do último envio que deu certo


def _lista_env(nome, padrao=""):
    return [x.strip() for x in os.environ.get(nome, padrao).split(",") if x.strip()]


def _html_email(novas, urgentes, ufs, portais, palavras):
    from html import escape as e

    def linha(x):
        if x.get("valor") is None:
            valor = "valor não informado"
        elif not x["valor"]:
            valor = "valor sigiloso"
        else:
            valor = "R$ " + f"{x['valor']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        prazo = x.get("encerramento") or ""
        prazo = f"{prazo[8:10]}/{prazo[5:7]}/{prazo[0:4]} às {prazo[11:16]}" if len(prazo) >= 16 else "não informado"
        link = SITE + "/licitacao?id=" + urllib.parse.quote(x["id"], safe="")
        return (f'<tr><td style="padding:12px 14px;border-bottom:1px solid #e3e8f0">'
                f'<div style="font-size:12px;color:#5a6b80">{e(x["modalidadeNome"])} {e(x["numero"])} · UASG {e(x["uasg"])} · {e(x["portal"])} · {e(", ".join(x.get("categorias", [])))}'
                + (' · <b style="color:#b3261e">🔧 Exige assistência técnica</b>' if x.get("assistencia") else "") + '</div>'
                f'<div style="font-weight:700;color:#0c326f;margin:2px 0">{e(x["orgao"])}</div>'
                f'<div style="color:#14202e">{e(x["objeto"][:280])}</div>'
                f'<div style="font-size:13px;margin-top:4px"><b>{valor}</b> · propostas até <b style="color:#b3261e">{prazo}</b> · '
                f'<a href="{link}" style="color:#1351b4">Abrir</a></div></td></tr>')

    def bloco(titulo, lista, vazio):
        corpo = "".join(linha(x) for x in lista[:60]) or f'<tr><td style="padding:12px 14px;color:#5a6b80">{vazio}</td></tr>'
        extra = f'<tr><td style="padding:10px 14px;color:#5a6b80">… e mais {len(lista) - 60}. Veja todas no site.</td></tr>' if len(lista) > 60 else ""
        return (f'<h2 style="font-size:16px;margin:22px 0 8px;color:#0c326f">{titulo} ({len(lista)})</h2>'
                f'<table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #e3e8f0;border-radius:8px">{corpo}{extra}</table>')

    filtro = f'{", ".join(portais) or "todos os portais"} · {", ".join(ufs)}' + (f' · palavras: {", ".join(palavras)}' if palavras else "")
    return (f'<div style="font-family:Arial,sans-serif;background:#f3f5f9;padding:20px;color:#14202e">'
            f'<div style="max-width:720px;margin:auto">'
            f'<h1 style="font-size:20px;margin:0;color:#0c326f">Busca Licitações · resumo do dia</h1>'
            f'<div style="color:#5a6b80;font-size:13px">{e(filtro)}</div>'
            + bloco("🆕 Novas nas últimas 24 horas", novas, "Nenhuma licitação nova hoje.")
            + bloco("⚠ Encerram nos próximos 2 dias", urgentes, "Nenhuma encerrando nos próximos 2 dias.")
            + f'<p style="font-size:12px;color:#5a6b80;margin-top:18px">Abra o site para ver todas: <a href="{SITE}" style="color:#1351b4">{SITE}</a>. '
              f'Dados do PNCP. Confira sempre o edital no portal antes de participar.</p></div></div>')


def montar_resumo():
    ufs = _lista_env("ALERTA_UF", "DF")
    if "BR" in [u.upper() for u in ufs]:
        ufs = ["AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG", "PA", "PB", "PR",
               "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO"]
    portais = _lista_env("ALERTA_PORTAIS", "Compras.gov.br")
    palavras = [sem_acento(p) for p in _lista_env("ALERTA_PALAVRAS")]
    itens = []
    vistos = set()
    for uf in ufs:
        for x in buscar(uf, list(MODALIDADES_PADRAO))["itens"]:
            if x["id"] not in vistos:
                vistos.add(x["id"])
                itens.append(x)
    agora = time.time()
    ontem = time.strftime("%Y-%m-%dT%H:%M", time.localtime(agora - 86400))
    daqui2 = time.strftime("%Y-%m-%dT%H:%M", time.localtime(agora + 2 * 86400))
    agora_s = time.strftime("%Y-%m-%dT%H:%M", time.localtime(agora))

    cats = _lista_env("ALERTA_CATEGORIAS")
    so_produto = os.environ.get("ALERTA_TIPO", "produto").lower().startswith("prod")

    def passa(x):
        if so_produto and x.get("tipo") == "Serviço":
            return False
        if portais and x["portal"] not in portais:
            return False
        if cats and not any(c in cats for c in x.get("categorias", [])):
            return False
        texto = sem_acento(x["objeto"] + " " + x["orgao"] + " " + x["unidade"])
        return not palavras or any(p in texto for p in palavras)

    sel = [x for x in itens if passa(x)]
    novas = sorted([x for x in sel if (x.get("publicacao") or "") >= ontem], key=lambda x: x.get("encerramento") or "9")
    urgentes = sorted([x for x in sel if agora_s <= (x.get("encerramento") or "") <= daqui2], key=lambda x: x.get("encerramento") or "9")
    return novas, urgentes, ufs, portais, _lista_env("ALERTA_PALAVRAS")


def enviar_email(assunto, html):
    chave = os.environ.get("RESEND_API_KEY", "")
    para = os.environ.get("ALERTA_EMAIL", "")
    if not chave or not para:
        raise RuntimeError("Faltam RESEND_API_KEY ou ALERTA_EMAIL nas configurações do Render.")
    corpo = json.dumps({"from": "Busca Licitações <onboarding@resend.dev>", "to": [para],
                        "subject": assunto, "html": html}).encode("utf-8")
    req = urllib.request.Request("https://api.resend.com/emails", data=corpo, method="POST",
                                 headers={"Authorization": "Bearer " + chave, "Content-Type": "application/json",
                                          "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def tarefa_email():
    _envio["rodando"] = True
    try:
        novas, urgentes, ufs, portais, palavras = montar_resumo()
        assunto = f"Busca Licitações: {len(novas)} nova{'s' if len(novas) != 1 else ''} · {len(urgentes)} encerrando em 2 dias ({', '.join(ufs)})"
        enviar_email(assunto, _html_email(novas, urgentes, ufs, portais, palavras))
        _envio["ultimo"] = time.strftime("%d/%m/%Y %H:%M") + f" · enviado ({len(novas)} novas, {len(urgentes)} urgentes)"
        _envio["dia"] = time.strftime("%Y-%m-%d")
    except Exception as e:
        _envio["ultimo"] = time.strftime("%d/%m/%Y %H:%M") + " · ERRO: " + erro_txt(e)
    finally:
        _envio["rodando"] = False


PAGINA = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Busca Licitações</title>
<link rel="icon" type="image/png" sizes="64x64" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAY+ElEQVR42s2beZxdVZXvv3uf6Q5VdWtIVaaqpDJAICEhJkBCEBsVQZqGp6IighBbZBAC2DSKLdjS2i0fG+ioTD4UFEGRJ74HD0WG93yABIEkQhKmDJCkUpWpxlu37nDuOXv3H/vcqVJJKjH9+bzzyf0kufcMe02/9Vtr7SMY49BaW0KIMPp3DDgH+BiwBOgAGgCL/7+OEEijwi6lglelLZ4DdyLwGWAJxbQdDqwVxZ7Hg9j8FRbWzF8IIS6x9ye81toFvgxcARx3OCvSOvqgEUIgRekHRfQtQghA/HWim/tZCKsJaTVJaS0ALoUCpF/D3/YUYf9q9Mj7aOHBwn8VwCYAez/CnwzcDSws/QSoaKX7XbHWoLQGwJICIUAI9j1dSPb5VofRP2TpogNJbB6GBmGV7wdole3SYe8qHe59QYQDa9D5XQiEFHYdKvR1fMHXLUj0k+6+p0YBWmtbCBForS8Cfgq4QGBWhDyQyytlFiWlwKpafH86pKfPZ+9gQFuTw7zOGBrI9b0DhT341iTi9W1IrxHbsioK0aFRT0WwWkUJq0ZJKrMZ1fsSwZ7nhRpaL7TfBwiEFUe4TWb52kfGJobW9Itt4B6Rau/TWtt2leUDrfWFwC8ii4ejPWQswYUAKY09e4cCXnlrhDUbs6zbkmNnX5GRnKKnr8hVn2plXudkNGDtfAR/0z1odyp5J0HgTEWkjsdqWkSs7STcZHOkDGVWUq0IYYFWqKH1hHtfJOz9Eyr9Nro4BMJBWDGE01gJDW0WqYtpFZt3pQ3OnqGh9bdrrSWg7Cq3Pwl4IHJ1DmTxUmzLaF1/2ZTl0T8O8uK6DDv7fbQG1xY4tsCzJamkJOZWLCbsGJZbh+OCVoN4+T2o7KuobknOnUKm+TTsaZ8lOXE+tqjyCMDffCdq1zOoka3oIIOQLlhxhNtcCY1yOEWBpnzwJgvRfoEGvFRq/gwhxFqttbSNA2gPuB9wIssf0N1lFKavvj3C/b/v48V1GXK+IhmTNCbtUkihS66kopCt0qDWCqVNFGrpgCWwAEsPoPc8hL/7fzLYdBrOjC9RP+UDyJL7a0XYvwYRn4xwWyIvUaOErsUbXczgzviSkE6TAlLAg1rrxUDRFkIorfWlwLwo5vfr9qHSWFIwMBxyx6O7+c3zAxQDqE9IYq6N0ppQ6VEL4OCAhi79MY93mvBQeEO/p7D2j/R1X0By7goSyRTu0dch7CT+m99FuI0HubcAVUTEJmF3XghoCToAOQ+4VAhxl9Ra1wH/EK1E7m+JShlkf3Fdhk9/6z0eerafuCtJJS20NsqptrIQ5nzLEuWMULMwYVU+NVoyLqy1RtuNxFyH5J77yL30OQa7XkKFRZyZX8ad/y+gigfWsLDQwTB2+3kIry1Kl1JGIn1Va11nA2cAM/enAB1ZRkr4ye96ue2R3UgpaGmwCUJd49sCkFKg0fhFTd43cJIeCSkGVdrRRQNakc2FjIHlmjtEHKGE+ArAnUjSf5PCWzcQNP8WNzkJ4aTQQiLQB7C+j/DacDq/EKVMGeVZNDAL+JgNfLQqz1v7A7vvPriLn/yul6Z6CyEwwlcdlhQEoSadCbAsQXury3EzYhw3I87kFof5M+NlJdnTv4BMLUTnd6KG1qOGNqCzO9CECLsOhF2JaWGj/T508hgaFv8AkZyEv+kHFN9diXDqozvq/cT+IE7nRYjYpEr6pJRekMDpQmv9BrAguouojXmwJHz7Zzt54Pd9tDbaY7q6EDA0EtKYtDj9hAbOPLGBxXMSpJLjY8u6OITqX02w62nC3c+hi4MIJ2VcuNCHbF5MbPE9CK+VwvpvEmx72OT3MhmSkeeIWr5gxYif+kSkAF2dTksKeENorfcCE0YrIAzBsuCHj+3h9kf30Jqyx7R6oajwA83fLmngqk+2cVS7t2/m0KCUxrai2+uIX41BdNTwRoqb7yLY+QcIs9iTz8Zb9APQisLaawh2P2tSXilUhAVhHqQLumi8J/IaZ9bluMd+Y7T1qZJ1r9Ba+1H62wftn1w1xDU/7KKx3kaNQnfbEgyNhExpcbj54smcfkJ9ze9de3xWbRjhL5tzrN+S5eyTU3zlE60oINx8F0HP75CNC7AaP4A1YRki0VFzfdDzv1EDr+POuxmd301hzRWEA68jvGZQQTk8CIbBacY74S7UwOv4b33HeI90iX/wcUR88mjrVx++PVp4pQ2Qbd/tc8vPd1IXt9B6lPBS0J8OWDovyR1faWdSS+UWq9/N8tAz/by0IUNfOsCWguFsyLL5yYr6/QHUwFpUZhPB9l8j3Bas1lNwOi9BNi0CFPaUc2DKOaj02xTWXIXOdRnLVwmvi4PIutnEFt+FqJuN1bQYdIC/7kacOf+IiE+JmKDcX/S59lhJTyD4zoM7GRgOSCXtmtxuW4L+4YCzljZw+1faibnm5v3DAd//1W4e/9MQxUCTjEma6mwsKVC6yv1LlrOThrLqEFSBYOdThD1PYk37PN68m0B6hHuep/D69RBmwG6IQqcEjP1YLUvwFt+DcpsQYYCUEmfW5QgnhTXhlLFgbZ/DHsv1n3olzf9dO0xjvU1YFfe2FAxmQj66qJ4fXtNRvvnajVm+/uNuNncXaKqzSHgSpSqkSI1mgugKe9MhSBeBAqsBq2UJSI9gx2P462+OCEXCxHeEG9rvw57yd3gf+AE6zJN76fP4ky+heeaZoAPsaZ8bNxGzq1FBCkGuoPjRb/fgObIm7qUUZPKKeZ0xVq7oQCCQEl5Yl2HFyi78QI/JDYg4hBSjyuGSWwobimlEYgreoruRqXkUN99L8d3bwE5ExU9QpijaH8CZ8SXceTejsl0U1lyNNfgqKpej0P4RPDdKoUKOq89QyQtKIwQ8/Vqad7bliXuyLIcAwlATcwXfv2IqyZhESlj9TparV3YRKk3CEzVZQkRZQmsYziqyBVWFcFl0cRgQaH8AkZpHbOkjyNQ8/A3fwn/nVrDrozK2hOAKHYzgzv2mEX7wDQp//jxq+C2Id+Dl3mCk67kqocfXZCkrQApBqDS/em4A1xE1wCcjILvsnAnMmRZDaejpK3LdnV0EocZzJKGqtbjSMJgJkBI+urieU46rqyhmwinYk04HXcRqO43Y0l8ivAkU1lxJceuDlcoObTwk9EErvIW348y8lGDnH8i/uhxd6EXY9eiwiGPbqK6HjBH2D3pjh4BSppmxfkuede/liHuSkvcLAXlfcVS7xxfPakFpI8Q/39/Drr6AxnqrxvKWFIzkQxKe5PJzW/nsh5vonOTWesakM7AmnYHKbEbWzUYX+si/eglh/2pT4VWBHWEGnBTewv/AmnAKOsxR3PgfENQCo7YS2JnXGdn7DqlJxx4M/Ws9oLT8Z19LU/AVsqpyKeHCRWe0EPckUsCTLw/xf9YMjyn8cDZkzrQYD908g69dMJHOSW7UKhvVw9NFZN1sVGYz+Zc/RziwNiI4VcIXhxDxacSW/tKguvIRVhx7+oVoHdZQYI3EYYTirmeqUG2cISClcf9Vb47guhX3FwL8omJqq8M5y1JooFDU3PdkL55bC5KWFKSzIScem+Dhm2Ywd3qMIDS0WaCQhBX0R4FwCHtXkf/zhejs9igl1qY52bSI2MmPIOtmU9x0JzosGLedci4yNtk0OkqxrjWW7aL7V+EfQhjIkktv311k686CQX9dsX62oFg6t47GOtOz+9O6DG9tzZtUVxUmhaKifYLDyqs7qItLwoj6CiJXLJW9QoKwCXqepLD6MigOg52MhDdlsvb7sCd/nNjSh8CKk3/tUgpvfhs1sNo8z21EtixFh9kqQRVIDyv3HoWhnqrK8iAYoLUGIXi3K89wNiRVV0t8NLB0brJMKZ5dnd6HGRpFhVzz6TZaG00qtC1RjkOV2ULQ9Sgq/Y5pYTn1hCVXtbyqJqhA+/04nctxj/s2OreT/JorUek3EW4T4a6nsdo+bC5rWUqw47ejKJyDq/vRmXegeeq4wsAuybJpR8EIKSoNV6U19XGLeTNiiMj9179vQBJM2pQCcgXFwlkJzlmWQmlqhA92PIb/5i3oYhqkU67ghF0XWSlKczqEYhb3mBtxZl+BGnqT/NqrINeDcJogyBAObQBVAOkhU8ch7Poatmc4rGZkcBv108aZBUrC7tjrUyhqCkXD4ISAINA0N9i0Ndnlc7bt8lEK8r7p+dnSFEUfXlSPbYnIe0wMqv7XKKy7ESG9Sv+ulAvKIGYZoRC4x/87dvsnCftXU3h1OTrMIayEQXw0emQrKteNTM5ExCYaouT3G8CMKkOhC3j+lnGQ4DK7FWW6OmuKRzIuTeNTQL6omTHZpT5uSsl0VtHaaBN3q3ECWlIWH4yKHVHlkP6mOyOK6VUAbnTXRhfBbsBbeLtBeq0M2rtNCGd6VfEjIcxBMR3hZB2yYS565H2wYpHHWUjVRCGwTE9fHFwFQkcBnfdVuc+vqwBASsoFTxCaVlfNOZHQMVdGoWN0rwt7yb1wNqh8VRdq7LFWbMnPkFElZ6wJYVAY4xqNlC5CGoOo0EePCXQSy3bHT4SgIuQBT7ZEbVW3v6GBEKbnF7n2/sEoYnpea6lhWUmrtnfwFGa5/LVHWQGbuwsMZ0PD36NlKwWJmOSoDg8RNTe39BTK51QfR3d4xN1KASLcFhOjxUyUAvUY7q/ASRgwE7JcROV9xcaufO3ZAoIQZk31aEhYaGBTV55sQWGVLhWgQqhLWMye6o1PAUqbOL7t17v5wytpUkmLUGmDAb5m1lSP//XdWXiuYFN3gQtueR/PFeVCSQpDfX/+T52cMr8OpcFCIdwmrJZlBDseQ3itEWmpNp+Dzg9gTz4r6u8pQi2QUVPlkn/bSjJmlYethpRpfvWtGSw6OkHB11z7ox1s6S4Qc4V5bgTIZy9NcddXOyjJdmAiFKHZ5GYHKcGxBY5l+vkxTzI0EpLOmg7tlBaH5gYbIUT5PFM4wcsbRioAGGGBc/R1iPhUtN9fOwcQFtrvRyTacY5aEYVCxdKrNoyY8ZpjnuHYZozeVG8xJeo+pUdChjIhMU9iWZXzpISJzdF0Sh2cB5QVNLXVqYzWolC2JQxlQrp7iwC0Ndm0tzr4RVUGQqU0iZjkmdVpMjmFEAIzdwSZ6CB20gNYTYvQxTTaHzCfYtoMQk96ABn1ArWWCCEYzoU881qaRMxQbV1FyTvaXFqjlNzd6zM0EmLL2jWjob3VHXdFbJdOmtMRw7bMUKMSd4bhrduSZeHsOJYUnHhMgrUbsyRjBtyUBs+VbO4u8PCz/Vx+7oQaJijrjya27NeEvS+j0m8bxTQcizXh5KjeN4SpRJ0ffrafLT0Fmhsq3SiBwC9qTjwmiRWZbN2WHNmCIubaZezQaGxbcHSHN24QlCUeMKfDo6neIggritOYRb24bqR8wZknNeC5soYOq1CTSljc8/he/rIpi22JSl0eobs14RScmZfizLw06tdVhC8pbO3GLPc+3ktD0kJVVZlaazxXcuZJDeXvXlw3glNlMIEByaZ6m6PbY2V8OngICKPA1kaHOR0xCr4q1xdaQcKTrNk4wvY9Zux9/KwES+cmGc6psjVKo7NioLnmh12sfy+HbRlgCnVUXZb6f9FHa02oRZk6r38vxzU/6qIYaKwqnmFJwXBOcfK8JAtmxtEatu/2WbtxxHStVIUnFXzFnHaPtka7lI3HVw6X2mGnHl+HH+ho345ZhG0JBodDHnt+oFwnXPXJ1nK7q7qd7rmC/nTI8u9t47cvDCJFaauMQFMFgFgIYYamUsBjLwyy/HvbGEiHeBGiV9MKS8JVn2gtP/83zw8wmAmjkK2Eqx9oPrSwzqRDdQj9ABFZ8vTFDTQkrZpOsNKaZNzi0T8OsmegiNZwwpwEXzijmf7hoIYYKQWeIyiGmq/d283yW7fx7Oo0g5mwxhpCwGAm5NnVaZbfuo2v39tNMdR4joi229S24L9wRguL5yTQGnYPFHn0j4Mk45UUSdSzTCUtTl/UQLVM42uICGPBGZNdTp6bZCRf5d7Rbo89A0VW/mYvQpiHXX9+G0vmJsuWqPYES0JDUrJqQ4arV3ZxxvWbePCZvvI5Dz7dxxnXb+LqlV2s2pAhlZRYsrZrZFuCgUzIkrlJrj9/ImFovHTl/9jD3sEirl3xQNOGM2HSOdkdV/7fpylaArWLz2zZh7yGSpNK2vzm/w3wzGtpLEvg2pIfXdvB0e0eg5kQxxa1u8UU1MUlqaTNzv4iW3dViNDWXT47+4o0JO2oeVLbSXdsM3+Y0+Fx57UduLbhJc+8luax5wf3GdaUmOvFZzbXyHJICjATHFh2XJJT5teRiWgxVRnBcwQ3/7SH93YWkBLaGm3uv3E6C2bG6B00Y/FqzStllOfaAs+p/OA5hkAppWtcvoQZvYMBC2bFeODr02lttJES3uspcNNPe/Cc2lRd6kN+cEEdJ8+rKzPCQ1ZA9fEPn22LUFzXWNVxTN/v8tu2091rLDqx2eFn3+jkk6c2Mjgcki9qowi57z6D/f1fSrAsQb6oGcyEfOpDjfz8G520NTvlPsSXb9vOcFbhOKPBV+PYgq9+pu2wiiE5mhYqpZk/M87FH29mcDjcB+QSnsX2PT7Lv7eNLT0FBMbV77i6nZUr2pnW5jKQDhjJqbKFRnuGFJS3zgCM5BQDwyHT21xWrmjn9qvaScYkIirSln9vG917/WjkNgonhgOWf7yF+TPjpr0vDrMarGZ/SsO1n27jlTdHeGtbnrpEJTOESlMXt+jaU+Tif93Kv102lb853gw9zlmW4qOL6nli1RCP/2mQt7flGRoJ6E8HZAsVs2ULiv50gBSGZyw6OsEnPtjIOctSJGKyPIx5/vUM37ivm/50SDJem50sy7j+oqMSXHNeq5lXiEPfcltuiOwzIhewsavABf/yPn6gcWxqtG9J0yNUCr74ty1ccW4r9QlZA0xbugts6S7wl01Zju2M899OSQHw+EuDvL01zweOSjB7qsesUaXrcFZx7xN7uf/3fVjSYMboyVMxMNnpkX+ewVHt3iEh/0EVUL0f8IU3Mlx++zZce99UJSPGNpgJOXZajEv/bgJnLWmImqaHfuQKiqdeSfOTJ3t5e3u+3Iof/cxQgR9o/vs/TuPUBXXltR7OsV8FVI/Ln1g1xNfu3oHnSiyr1hNK7pgrKAq+5phpHqef0MDfHF/HUe0e9YkD7xMazoZs2lHg+dczPLcmzTvbC3iuIO7JGpcvWT4MIV9U3HZlO+csS5XXeLjHARVQrYSnX01z/d070Nq0z0ZviJTCxGCuoMj7ipgrmTLBYeYUjwkpi7OWpDh1QV1UyGR46pUheodC3usp0NNbLF9jptKa0UzWkoK8b8rwO65q54wTG/5q4TnYZujSg0OlOfOkBhqS07n+7h30DQXUJ2rngioqyI31zK7RnX1FduwtsnewiOOIsgKeW53mx0/00tromBivuiYcg8PbEeC1pGzu+Eo7S+clj4jwpTQYjksJoebkeUl+eZMBnYFRdUB1ji9tpXMdQX1C0lxvk6zChWTMfFefkOWO0ujtd6NT3VHtHr+8eYYRPjwywgOBBIbHc6YVDT06J7v84pszOHVBndkEdYAucYkSh6rWpVUk8L5bZ/YVvi8dcOqCOn7xzRl0TnKN5a0jIjxARgI7qjYPHtQTlIKmeov7bpjOp09roncoiOKfI3aU3kHoHQr4zGlN3HfDdJrqrfJ+5SNwlGTdLoHXKtsxxtNCMha0bcG/XzGVFZ9qZWgkHHcDYjzCa216kSs+1cb3r5iKbYto+94R03FJ3lck8ERpn/O4gSMqF5WG68+fyC1/P4W8rwhDs0h9mCsqpzlfccvfT+H689tMyj1MknMQ7BPAkxJ4Btgy3jCocVNhYvmijzVz53UdWJaZJdiHsVpbCvK+xrLgzus6uOhjzWY+IY9seFXJuAV4TgohssBdpWHQod6tlCFOX9zAAzdOp6XBZjgXHnyENjrN5UJaGmweuLGT0xc3HEmkH0sBArhTCJGV0ctDPwY2RrwgPGQlRF3ghbMTPHxTJ8dOi+03Te4vzR07LcbDN3WycHacIDyiSF/D6yIZ3wV+rLWW0hSAIgssx7wyc1ieUNobMG2iy4P/1MmHjq+P0uSBroG+dMCHjq/nwW92Mm2iW54P/BdaPgC+KITImdaheWPMEkK8DHypCiDCwwkHpSBVZ3HfDdM4/yNN9KVDLEvs0xSVlqAvHXL+R5q474ZppJJHNM2NZfkS0H9RCPFy6W05GfUAwujFyQeBi4Ci2bpBcKjeUE6TluDWy6ay4rxWBofDmgJKKRgaDllxXiu3Xja1PEM4gmmu2upBJEsRuFAI8VAka1hTC0QvTlpCiIe11luAezjEV2dryuRoXnfteW14jqRvKKhpY33n0ilcce6E8sbLI2R4XfWRVZ/XgSuFEH8uvSR6APqqrehvV2u9Qmu9Xv8VR6jM331DQfm7vqFizW//hceGSAanWrZRuxTGVsKo1+fPBM7mMF+fH6tbo448uTGvz0MX8ArwO+BpIUQ+kkMKIfYJ5/8EfUXTjO3TJewAAAAASUVORK5CYII=">
<link rel="apple-touch-icon" href="data:image/png;base64,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">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;600&display=swap">
<style>
/* Layout: cabeçalho com logo, barra de busca, filtros à esquerda e cartões de licitação à direita */
:root{
  --bg:#0d1422; --surf:#151f32; --surf2:#1b2740; --line:#26344f; --line2:#31415f;
  --ink:#e8eef8; --muted:#94a3bb; --dim:#6b7a93;
  --accent:#38bdf8; --accent-ink:#06243a; --accent-soft:#12324a;
  --gold:#e6b032; --gold-soft:#3a2c0c;
  --good:#3ecf8e; --good-bg:#0f2e24; --warn:#f5b94a; --warn-bg:#33270f; --bad:#f6766f; --bad-bg:#371a1c;
  --font:"Plus Jakarta Sans",system-ui,-apple-system,"Segoe UI",sans-serif; --mono:"JetBrains Mono",ui-monospace,Consolas,monospace;
  color-scheme:dark;
}
*{box-sizing:border-box}
[hidden]{display:none!important}
html{background:var(--bg)}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--font);-webkit-font-smoothing:antialiased}
.wrap{max-width:1240px;margin:0 auto;padding-block:22px 60px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));display:grid;gap:18px}
a{color:var(--accent)}
:focus-visible{outline:2px solid var(--gold);outline-offset:2px}
button{font-family:inherit}

.top{display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.marca{height:64px;width:auto;max-width:100%;display:block}
.top-t{border-left:1px solid var(--line2);padding-left:18px;min-width:0}
.top h1{margin:0;font-size:22px;font-weight:800}
.top p{margin:0;color:var(--muted);font-size:13.5px}

.busca{display:flex;gap:0;background:var(--surf);border:1px solid var(--line2);border-radius:999px;overflow:hidden;box-shadow:0 6px 24px rgba(0,0,0,.25)}
.busca .q{flex:1 1 auto;min-width:0;display:flex;align-items:center;gap:10px;padding:0 18px}
.busca .q svg{color:var(--dim);flex:none}
.busca input{flex:1;min-width:0;border:0;background:transparent;color:var(--ink);font:500 16px var(--font);padding:15px 0}
.busca input::placeholder{color:var(--dim)}
.busca input:focus{outline:none}
.busca select{border:0;border-left:1px solid var(--line2);background:var(--surf2);color:var(--ink);font:600 14px var(--font);padding:0 14px;max-width:200px}
.busca button{border:0;background:var(--accent);color:var(--accent-ink);font:800 15px var(--font);padding:0 28px;cursor:pointer}
.busca button:disabled{opacity:.6;cursor:wait}

.grade{display:grid;grid-template-columns:270px minmax(0,1fr);gap:20px;align-items:start}
aside{background:var(--surf);border:1px solid var(--line);border-radius:14px;padding:6px 0;position:sticky;top:12px;max-height:calc(100vh - 24px);overflow:auto}
aside details{border-bottom:1px solid var(--line);padding:10px 16px}
aside details:last-child{border-bottom:0}
aside summary{cursor:pointer;font:700 11.5px var(--font);letter-spacing:.08em;text-transform:uppercase;color:var(--muted);list-style:none;display:flex;justify-content:space-between;align-items:center;padding:4px 0}
aside summary::after{content:"▾";font-size:12px}
aside details:not([open]) summary::after{content:"▸"}
aside summary::-webkit-details-marker{display:none}
.opc{display:flex;align-items:center;gap:8px;padding:5px 0;font-size:14px;cursor:pointer}
.opc input{accent-color:var(--gold);width:16px;height:16px;flex:none}
.opc .n{margin-left:auto;font:600 12px var(--mono);color:var(--dim)}
.opc.meu span:first-of-type{font-weight:700}
.sub{font-size:11.5px;color:var(--dim);margin:8px 0 2px;text-transform:uppercase;letter-spacing:.06em}
.mini-bt{background:none;border:0;color:var(--accent);font:600 12.5px var(--font);padding:2px 0;cursor:pointer}
.ordem{display:grid;gap:4px;margin-top:4px}
.ordem button{text-align:left;border:0;background:transparent;color:var(--ink);font:500 14px var(--font);padding:7px 10px;border-radius:8px;cursor:pointer}
.ordem button[aria-pressed="true"]{background:var(--accent);color:var(--accent-ink);font-weight:700}
.data-box{display:grid;gap:8px;margin-top:6px}
.data-box input[type=date]{width:100%;min-width:0;background:var(--bg);border:1px solid var(--line2);border-radius:8px;color:var(--ink);font:600 14px var(--font);padding:8px 10px;color-scheme:dark}
.data-box .modo{display:flex;gap:6px}
.data-box .modo button,.atalhos button{flex:1;font:600 12.5px var(--font);padding:6px 8px;border-radius:8px;border:1px solid var(--line2);background:transparent;color:var(--muted);cursor:pointer}
.ufs{display:flex;flex-wrap:wrap;gap:6px;padding:2px 0 4px}
.ufc{min-width:42px;padding:6px 8px;border-radius:8px;border:1px solid var(--line2);background:transparent;color:var(--ink);font:600 13px var(--font);cursor:pointer;position:relative}
.ufc:hover{border-color:var(--accent)}
.ufc[aria-pressed="true"],.ufc.uf-ok{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.ufc.br{padding:6px 12px}
.ufc.uf-carr[aria-pressed="true"]{opacity:.55}
.ufc.uf-erro{border-color:var(--bad);color:var(--bad)}
.ufc.uf-erro[aria-pressed="true"]{background:var(--bad);border-color:var(--bad);color:#2a0d0c}
.uf-info{font-size:11.5px;color:var(--muted);margin-top:6px;line-height:1.4}
.data-box .modo button[aria-pressed="true"]{background:var(--gold);border-color:var(--gold);color:#241a00}
.atalhos{display:flex;gap:6px;flex-wrap:wrap}
.atalhos button{flex:0 0 auto}
.atalhos button:hover,.data-box .modo button:hover{border-color:var(--accent);color:var(--ink)}
.vals{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:6px}
.vals input{width:100%;min-width:0;background:var(--bg);border:1px solid var(--line2);border-radius:8px;color:var(--ink);font:500 13.5px var(--font);padding:8px}

.res{display:grid;gap:12px;min-width:0}
.cab{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;color:var(--muted)}
.cab b{color:var(--ink);font-size:17px}
.cab .at{margin-left:auto;font-size:12.5px;color:var(--dim)}
.aviso{background:var(--warn-bg);border:1px solid #6e5317;border-radius:10px;padding:10px 14px;font-size:13.5px}
.erro{background:var(--bad-bg);border:1px solid #7a2f2f;border-radius:10px;padding:12px 16px}

.card{background:var(--surf);border:1px solid var(--line);border-radius:14px;padding:16px 18px;display:grid;grid-template-columns:minmax(0,1fr) 220px;gap:10px 22px}
.card:hover{border-color:var(--line2)}
.card.meu{border-left:4px solid var(--gold)}
.tags{display:flex;gap:6px;flex-wrap:wrap;grid-column:1}
.tag{font:600 12px var(--font);padding:3px 9px;border-radius:7px;border:1px solid var(--line2);color:var(--muted);background:var(--surf2);white-space:nowrap}
.tag.num{font-family:var(--mono);color:var(--ink)}
.tag.mod{color:#9cc8ff;border-color:#2b4a74;background:#12243d}
.tag.srp{color:#c9a7ff;border-color:#4b3a74;background:#211a3d}
.tag.prazo{color:var(--warn);border-color:#6e5317;background:var(--warn-bg)}
.tag.prazo.urg{color:var(--bad);border-color:#7a2f2f;background:var(--bad-bg)}
.tag.portal{color:var(--muted)}
.tag.portal.meu{color:var(--gold);border-color:#7a5a12;background:var(--gold-soft)}
.tag.cat{color:#a8e3c8;border-color:#285a45;background:#11291f}
.tag.assist{color:#ffb4a8;border-color:#7a2f2f;background:var(--bad-bg);font-weight:700}
.selo-cg{display:inline-flex;align-items:center;gap:6px;font:800 12px var(--font);padding:3px 10px 3px 4px;border-radius:7px;background:#1351b4;color:#fff;border:1px solid #2a6bd1;white-space:nowrap;letter-spacing:.01em}
.selo-cg i{display:inline-grid;place-items:center;width:18px;height:18px;border-radius:5px;background:#ffcd07;color:#0c326f;font-style:normal}
.selo-cg i svg{display:block}
.selo-cg em{font-style:normal;color:#ffcd07}
.selo-p{display:inline-flex;align-items:center;font:800 12px var(--font);padding:3px 10px;border-radius:7px;color:#fff;white-space:nowrap;letter-spacing:.01em}
.selo-mg{background:#c8102e;border:1px solid #e23a52}
.selo-go{background:#13873f;border:1px solid #2aa95a}
.selo-ld{background:#0e8ec9;border:1px solid #38b1e6}
.corpo{grid-column:1;min-width:0}
.corpo h3{margin:4px 0 2px;font-size:16px;font-weight:800;text-transform:uppercase;letter-spacing:.01em;overflow-wrap:anywhere}
.obj{color:var(--muted);font-size:14px;margin:0;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;cursor:pointer;overflow-wrap:anywhere}
.obj.aberto{-webkit-line-clamp:unset;display:block}
.loc{font-size:12.5px;color:var(--dim);margin-top:6px}
.lado{grid-column:2;grid-row:1 / span 2;display:grid;grid-template-columns:1fr;gap:10px;align-content:start;border-left:1px solid var(--line);padding-left:18px}
.lado span{display:block;font:700 10.5px var(--font);letter-spacing:.08em;text-transform:uppercase;color:var(--dim)}
.lado b{font:600 15px var(--mono);font-variant-numeric:tabular-nums}
.lado .ate b{color:var(--bad)}
.lado .ate b.ok{color:var(--ink)}
.acoes{grid-column:1 / -1;display:flex;gap:8px;flex-wrap:wrap;border-top:1px solid var(--line);padding-top:10px}
.acoes a,.acoes button{font:600 13px var(--font);padding:6px 12px;border-radius:8px;border:1px solid var(--line2);color:var(--ink);background:transparent;text-decoration:none;cursor:pointer}
.acoes a.pri{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.acoes a.pag{color:var(--gold);border-color:#7a5a12}
.acoes button.salvar{color:var(--ink)}
.acoes button.salvar.on{background:var(--gold);border-color:var(--gold);color:#241a00}
.acoes button.remover{color:var(--bad);border-color:#7a2f2f}
.salvar-todas{margin-left:auto;font:700 13px var(--font);padding:6px 14px;border-radius:999px;border:1px solid #7a5a12;background:var(--gold-soft);color:var(--gold);cursor:pointer;white-space:nowrap}
.salvar-todas:hover{background:var(--gold);color:#241a00}
.salvar-todas.feito{cursor:default;opacity:.8}
.salvar-todas.feito:hover{background:var(--gold-soft);color:var(--gold)}
.ja-salva{display:inline-flex;align-items:center;font:700 13px var(--font);padding:6px 12px;border-radius:8px;color:var(--gold);background:var(--gold-soft);border:1px solid #7a5a12;cursor:default}
.conta{margin-left:auto;align-self:center;display:inline-flex;align-items:center;gap:10px;font-size:14px;color:var(--muted);white-space:nowrap}
.conta b{color:var(--ink);font-weight:700}
.conta a{color:var(--muted);text-decoration:none;border:1px solid var(--line2);border-radius:999px;padding:7px 14px;font-weight:600}
.conta a:hover{color:var(--bad);border-color:#7a2f2f}
.conta+.aba-op{margin-left:12px}
@media (max-width:560px){.conta{margin-left:0;width:100%;justify-content:flex-end}.conta+.aba-op{margin-left:auto}}
.aba-op{margin-left:auto;align-self:center;display:inline-flex;align-items:center;gap:8px;font:700 14px var(--font);padding:9px 16px;border-radius:999px;border:1px solid #7a5a12;background:var(--gold-soft);color:var(--gold);cursor:pointer}
.aba-op b{display:inline-grid;place-items:center;min-width:22px;height:22px;padding:0 6px;border-radius:999px;background:var(--gold);color:#241a00;font-size:12px}
.aba-op[aria-pressed="true"]{background:var(--gold);color:#241a00}
.aba-op[aria-pressed="true"] b{background:#241a00;color:var(--gold)}
.tag.encerrada{color:var(--bad);border-color:#7a2f2f;background:var(--bad-bg)}
.nota{display:inline-flex;align-items:center;gap:7px;font:700 13px var(--font);padding:6px 12px;border-radius:8px;border:1px solid var(--line2);color:var(--muted);text-decoration:none;white-space:nowrap}
.nota b{font:800 13px var(--mono)}
.nota .pt{width:9px;height:9px;border-radius:50%;background:var(--dim);flex:none}
.nota.bom{color:var(--good);border-color:#1f6b4c;background:var(--good-bg)}.nota.bom .pt{background:var(--good)}
.nota.regular{color:var(--warn);border-color:#6e5317;background:var(--warn-bg)}.nota.regular .pt{background:var(--warn)}
.nota.ruim{color:var(--bad);border-color:#7a2f2f;background:var(--bad-bg)}.nota.ruim .pt{background:var(--bad)}
.nota.esp .pt{animation:pisca 1s ease-in-out infinite}
@keyframes pisca{50%{opacity:.25}}
@media (prefers-reduced-motion:reduce){.nota.esp .pt{animation:none}}
.acoes a:hover,.acoes button:hover{border-color:var(--accent)}
.det{grid-column:1 / -1;border-top:1px dashed var(--line2);padding-top:12px;display:grid;gap:12px;min-width:0}
.det h4{margin:0 0 6px;font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.docs{display:grid;gap:6px}
.doc{display:flex;gap:10px;align-items:center;background:var(--surf2);border:1px solid var(--line);border-radius:10px;padding:8px 12px;text-decoration:none;color:var(--ink);min-width:0}
.doc:hover{border-color:var(--accent)}
.doc svg{flex:none;color:var(--gold)}
.doc span{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.doc small{margin-left:auto;color:var(--dim);font-size:12px;white-space:nowrap}
.dados{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px 18px;background:var(--surf2);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.dados div{min-width:0}
.dados span{display:block;font:700 10.5px var(--font);letter-spacing:.07em;text-transform:uppercase;color:var(--dim)}
.dados b{font-weight:600;font-size:13.5px;overflow-wrap:anywhere}
.dados b.m{font-family:var(--mono);font-size:12.5px}
.txt{margin:0;color:var(--ink);font-size:14px;white-space:pre-wrap;overflow-wrap:anywhere}
.tipo{font:600 11px var(--font);padding:1px 7px;border-radius:6px;background:var(--surf2);border:1px solid var(--line2);color:var(--muted);margin-left:6px;white-space:nowrap}
.bts{display:flex;gap:8px;flex-wrap:wrap}
.bts button{font:600 13px var(--font);padding:6px 12px;border-radius:8px;border:1px solid var(--line2);color:var(--ink);background:transparent;cursor:pointer}
.bts button:hover{border-color:var(--accent)}
.tw{overflow-x:auto;border:1px solid var(--line);border-radius:10px}
.tw table{border-collapse:collapse;width:100%;font-size:13.5px;font-variant-numeric:tabular-nums}
.tw th,.tw td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:right;vertical-align:top}
.tw th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--dim);background:var(--surf2);white-space:nowrap}
.tw td.d{text-align:left;min-width:260px;color:var(--ink)}
.tw td.d small{display:block;color:var(--dim)}
.tw tr:last-child td{border-bottom:0}
.tw tfoot td{font-weight:700;background:var(--surf2)}
.corpo h3 a,.obj a{color:inherit;text-decoration:none}
.corpo h3 a:hover,.obj a:hover{color:var(--accent);text-decoration:underline}
.pg{display:grid;gap:18px;min-width:0}
.voltar{color:var(--accent);text-decoration:none;font-weight:600;font-size:14px}
.pg h2{margin:6px 0 4px;font-size:clamp(19px,2.6vw,26px);font-weight:800;line-height:1.3;text-transform:uppercase;overflow-wrap:anywhere}
.pg .org{color:var(--accent);font-weight:700}
.pg .org small{color:var(--muted);font-weight:500}
.quad{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));border:1px solid var(--line);border-radius:12px;overflow:hidden;background:var(--surf)}
.quad div{padding:14px 16px;border-right:1px solid var(--line);min-width:0}
.quad div:first-child{background:var(--surf2)}
.quad span{display:block;font:700 10.5px var(--font);letter-spacing:.08em;text-transform:uppercase;color:var(--dim)}
.quad b{font:600 16px var(--mono);font-variant-numeric:tabular-nums}
.quad .urg{color:var(--bad)}
.quad small{display:block;color:var(--muted);font-size:12.5px;margin-top:2px}
.pg .acoes{border-top:0;padding-top:0}
.secao{background:var(--surf);border:1px solid var(--line);border-radius:14px;padding:16px 18px;display:grid;gap:12px;min-width:0}
.secao>h3{margin:0;font-size:17px;font-weight:800;border-bottom:1px solid var(--line);padding-bottom:8px}
.mais{justify-self:center;font:700 14px var(--font);padding:10px 22px;border-radius:999px;border:1px solid var(--line2);background:var(--surf);color:var(--ink);cursor:pointer}
.vazio{background:var(--surf);border:1px dashed var(--line2);border-radius:14px;padding:30px;text-align:center;color:var(--muted)}
.vazio b{display:block;color:var(--ink);font-size:17px;margin-bottom:6px}
.carregando{display:flex;gap:12px;align-items:center;background:var(--surf);border:1px solid var(--line);border-radius:14px;padding:22px;color:var(--muted)}
.spin{width:20px;height:20px;border:2px solid var(--line2);border-top-color:var(--accent);border-radius:50%;animation:g .8s linear infinite;flex:none}
@keyframes g{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.spin{animation:none}}
footer{font-size:12px;color:var(--dim)}

@media (max-width:900px){
  .grade{grid-template-columns:1fr}
  aside{position:static;max-height:none}
  .card{grid-template-columns:1fr}
  .lado{grid-column:1;grid-row:auto;grid-template-columns:1fr 1fr;border-left:0;padding-left:0;border-top:1px solid var(--line);padding-top:10px}
}
@media (max-width:560px){
  .top-t{border-left:0;padding-left:0}.marca{height:52px}
  .busca{border-radius:14px;flex-wrap:wrap}
  .busca .q{flex:1 1 100%}
  .busca select{flex:1 1 auto;border-left:0;border-top:1px solid var(--line2);padding:12px;max-width:none}
  .busca button{padding:12px 20px;flex:1 1 auto}
}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <img class="marca" src="data:image/png;base64,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" alt="SigaLicita" width="170" height="64">
    <div class="top-t"><h1>Busca Licitações</h1><p>Licitações com proposta em aberto no PNCP, com filtro por portal</p></div>
    <!--CONTA-->
    <button type="button" class="aba-op" id="aba-op" aria-pressed="false" title="Licitações que você salvou para analisar depois">⭐ Oportunidades <b id="n-salvas">0</b></button>
  </div>

  <form class="busca" id="f" autocomplete="off">
    <label class="q"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
      <input id="q" placeholder="O que sua empresa vende? Ex.: material de escritório" aria-label="Palavra-chave"></label>
    <select id="uf" aria-label="Estado"></select>
    <button id="bt" type="submit">Buscar</button>
  </form>

  <div class="grade">
    <aside id="filtros">
      <details open id="box-uf"><summary>Estado</summary>
        <div id="f-ufs"></div>
      </details>
      <details open><summary>Tipo</summary>
        <div class="data-box"><div class="modo" id="f-tipo">
          <button type="button" data-t="Produto" aria-pressed="true">Só produtos</button>
          <button type="button" data-t="" aria-pressed="false">Tudo</button>
          <button type="button" data-t="Serviço" aria-pressed="false">Só serviços</button>
        </div></div>
      </details>
      <details open><summary>Portal</summary>
        <div id="f-portal"></div>
      </details>
      <details open><summary>Categoria</summary>
        <div id="f-cat"></div>
      </details>
      <details open><summary>Data da disputa</summary>
        <div class="data-box">
          <input type="date" id="dia" aria-label="Data da disputa (prazo da proposta)">
          <div class="modo" id="modo-dia">
            <button type="button" data-m="no" aria-pressed="true">No dia</button>
            <button type="button" data-m="apartir" aria-pressed="false">A partir do dia</button>
          </div>
          <div class="atalhos" id="atalhos-dia">
            <button type="button" data-d="0">Hoje</button><button type="button" data-d="1">Amanhã</button><button type="button" data-d="3">+3 dias</button><button type="button" data-d="7">+7 dias</button><button type="button" data-d="">Limpar</button>
          </div>
        </div>
      </details>
      <details open><summary>Ordenar por</summary>
        <div class="ordem" id="f-ordem">
          <button type="button" data-o="encerra" aria-pressed="true">Encerra primeiro</button>
          <button type="button" data-o="recentes" aria-pressed="false">Publicadas há menos tempo</button>
          <button type="button" data-o="maior" aria-pressed="false">Maior valor</button>
          <button type="button" data-o="menor" aria-pressed="false">Menor valor</button>
        </div>
      </details>
      <details open><summary>Modalidade</summary>
        <div id="f-mod"></div>
      </details>
      <details><summary>Prazo para proposta</summary>
        <div id="f-prazo"></div>
      </details>
      <details><summary>Valor estimado (R$)</summary>
        <div class="vals"><input id="vmin" inputmode="numeric" placeholder="mínimo" aria-label="Valor mínimo"><input id="vmax" inputmode="numeric" placeholder="máximo" aria-label="Valor máximo"></div>
      </details>
    </aside>

    <main class="res" id="res" aria-live="polite"></main>
  </div>
  <footer>Dados: PNCP, Portal Nacional de Contratações Públicas. Confira sempre o edital no portal antes de participar.</footer>
</div>

<script>
// ===== Mudança de endereço: o endereço antigo leva as Oportunidades e preferências para sigalicita.com.br =====
// (cada endereço tem a sua "gaveta" no navegador; assim nada se perde)
(function () {
  const NOVO = "sigalicita.com.br";
  if (location.hostname.endsWith(".onrender.com")) {
    let pacote = "";
    try {
      const d = {s: localStorage.getItem("busca_salvas") || "", p: localStorage.getItem("busca_pref") || ""};
      if (d.s || d.p) pacote = "#importar=" + encodeURIComponent(JSON.stringify(d));
    } catch (e) {}
    location.replace("https://" + NOVO + location.pathname + location.search + pacote);
    throw new Error("Indo para o endereço novo: " + NOVO);  // para aqui; a página nova carrega em seguida
  }
  if (location.hash.startsWith("#importar=")) {
    try {
      const d = JSON.parse(decodeURIComponent(location.hash.slice(10)));
      if (d.s) {
        const antigas = JSON.parse(d.s) || {}, atuais = JSON.parse(localStorage.getItem("busca_salvas") || "{}") || {};
        localStorage.setItem("busca_salvas", JSON.stringify(Object.assign(antigas, atuais)));  // junta as duas, sem perder nenhuma
      }
      if (d.p && !localStorage.getItem("busca_pref")) localStorage.setItem("busca_pref", d.p);
    } catch (e) {}
    history.replaceState(null, "", location.pathname + location.search);
  }
})();
// ===== Busca direto do navegador no PNCP =====
// O PNCP recusa a maioria dos pedidos que vêm do Render (muitos sites usam o mesmo endereço de lá).
// Por isso o próprio navegador da usuária busca no PNCP, com a internet dela. O servidor fica de reserva.
const CFG = __CONFIG_PNCP__;
const PNCP_URL = "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta";
function semAc(s) { return String(s || "").normalize("NFKD").replace(/[^\x00-\x7f]/g, "").toLowerCase(); }
function escRe(s) { return s.replace(/[.*+?^${}()|[\]\\\/-]/g, "\\$&"); }
function padraoP(w) { w = w.trim(); const p = escRe(w); return w.length <= 6 ? p + "(?:s|es)?(?![a-z])" : p; }
const CAT_RE = CFG.categorias.map(([n, ws]) => [n, new RegExp("(?<![a-z])(?:" + ws.map(padraoP).join("|") + ")")]);
const SERV_RE = new RegExp("(?<![a-z])(?:" + CFG.servico.map(escRe).join("|") + ")(?![a-z])");
const PROD_RE = new RegExp("(?<![a-z])(?:" + CFG.produto.map(escRe).join("|") + ")");
const FORTE_RE = /(?<![a-z])(prestacao de servic|servicos? de|mao de obra|obras?|reforma|pavimenta|engenharia|locacao|terceiriza|vigilancia|consultoria|assessoria)/;
function categoriasDe(t) { t = semAc(t).replaceAll("mao de obra", "mao-de-servico"); const c = CAT_RE.filter(([, rx]) => rx.test(t)).map(([n]) => n); return c.length ? c : ["Outros"]; }
function tipoDe(t) { t = semAc(t); if (SERV_RE.test(t)) return PROD_RE.test(t) && !FORTE_RE.test(t) ? "Produto" : "Serviço"; return "Produto"; }
function portalDe(c) {
  const u = semAc(c.usuarioNome), links = ["linkSistemaOrigem", "linkProcessoEletronico"].map((k) => String(c[k] || "").toLowerCase()).join(" ");
  for (const [nome, nomes, doms] of CFG.portais) if (nomes.some((n) => u.includes(n)) || doms.some((d) => links.includes(d))) return nome;
  return String(c.usuarioNome || "Não informado").trim().slice(0, 60);
}
function linkCompras(c) {
  const uasg = String((c.unidadeOrgao || {}).codigoUnidade || "").replace(/\D/g, ""), num = String(c.numeroCompra || "").replace(/\D/g, "");
  const mod = {6: "05", 8: "06"}[c.modalidadeId];
  if (uasg.length !== 6 || !num || !mod || !c.anoCompra) return "";
  return "https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-web/public/compras/acompanhamento-compra?compra=" + uasg + mod + String(parseInt(num, 10)).padStart(5, "0") + c.anoCompra;
}
function resumirP(c) {
  const org = c.orgaoEntidade || {}, und = c.unidadeOrgao || {}, controle = String(c.numeroControlePNCP || "");
  const m = controle.match(/^(\d{14})-\d+-(\d+)\/(\d{4})$/), portal = portalDe(c), obj = String(c.objetoCompra || "");
  let origem = c.linkSistemaOrigem || ""; if (origem && !String(origem).startsWith("http")) origem = "https://" + origem;
  const cats = categoriasDe(obj), sa = semAc(obj + " " + String(c.informacaoComplementar || ""));
  return {
    id: controle, numero: `${c.numeroCompra || ""}/${c.anoCompra || ""}`.replace(/^\/+|\/+$/g, ""),
    modalidade: c.modalidadeId, modalidadeNome: c.modalidadeNome || CFG.modalidades[c.modalidadeId] || "",
    disputa: c.modoDisputaNome || "", srp: !!c.srp, orgao: org.razaoSocial || "", cnpj: String(org.cnpj || "").replace(/\D/g, ""),
    unidade: und.nomeUnidade || "", uasg: String(und.codigoUnidade || ""), municipio: und.municipioNome || "", uf: und.ufSigla || "",
    objeto: obj.replace(/\s+/g, " ").trim(), valor: c.valorTotalEstimado ?? null,
    abertura: String(c.dataAberturaProposta || "").slice(0, 16), encerramento: String(c.dataEncerramentoProposta || "").slice(0, 16),
    publicacao: String(c.dataPublicacaoPncp || "").slice(0, 16), portal, categorias: cats, tipo: tipoDe(obj),
    assistencia: cats.includes("Eletrodomésticos") || CFG.assistencia.some((x) => sa.includes(x)),
    processo: String(c.processo || ""), amparo: (c.amparoLegal || {}).nome || "",
    esfera: {F: "Federal", E: "Estadual", M: "Municipal", D: "Distrital"}[org.esferaId] || "",
    poder: {E: "Executivo", L: "Legislativo", J: "Judiciário", N: "Não se aplica"}[org.poderId] || "",
    situacao: c.situacaoCompraNome || "",
    linkPncp: m ? `https://pncp.gov.br/app/editais/${m[1]}/${m[3]}/${parseInt(m[2], 10)}` : "",
    linkPortal: origem || (portal === "Compras.gov.br" ? linkCompras(c) : ""),
  };
}
// no máximo 4 pedidos ao mesmo tempo, para o PNCP não recusar
let VAGAS = 4; const ESPERA = [];
const PRIORIDADE = new Set();  // estados que ela marcou: passam na frente
async function vaga(prio) { if (VAGAS > 0) { VAGAS--; return; } await new Promise((ok) => prio ? ESPERA.unshift(ok) : ESPERA.push(ok)); }
function soltaVaga() { const prox = ESPERA.shift(); if (prox) prox(); else VAGAS++; }
const dormir = (ms) => new Promise((ok) => setTimeout(ok, ms));
function doisd(n) { return String(n).padStart(2, "0"); }
async function paginaP(uf, mod, pg) {
  const fim = new Date(); fim.setDate(fim.getDate() + 120);
  const url = `${PNCP_URL}?dataFinal=${fim.getFullYear()}${doisd(fim.getMonth() + 1)}${doisd(fim.getDate())}&codigoModalidadeContratacao=${mod}&uf=${uf}&pagina=${pg}&tamanhoPagina=50`;
  for (let t = 1; t <= 5; t++) {
    let r;
    await vaga(PRIORIDADE.has(uf));
    try { r = await fetch(url, {headers: {Accept: "application/json"}}); }
    catch (e) { soltaVaga(); if (t >= 2) throw new Error("sem conexão com o PNCP"); await dormir(1500); continue; }
    try {
      if (r.status === 204) return {};
      if (r.ok) { const txt = await r.text(); return txt.trim() ? JSON.parse(txt) : {}; }
    } finally { soltaVaga(); }
    if ((r.status < 500 && r.status !== 429) || t === 5) throw new Error("PNCP respondeu " + r.status);
    await dormir((r.status === 429 ? 5000 : 2000) * t);
  }
  return {};
}
const CACHE_P = {};
async function modalidadeP(uf, mod) {
  const k = uf + "|" + mod;
  if (CACHE_P[k] && Date.now() - CACHE_P[k].quando < 2 * 3600 * 1000) return CACHE_P[k].res;
  const p1 = await paginaP(uf, mod, 1);
  let itens = (p1.data || []).slice();
  const tot = p1.totalPaginas || 1, pags = Math.min(100, tot), outras = [];  // até 5.000 por modalidade em cada estado
  for (let pg = 2; pg <= pags; pg++) outras.push(paginaP(uf, mod, pg));
  const rs = await Promise.allSettled(outras);
  // páginas que o PNCP recusou: espera um pouco e tenta de novo, uma de cada vez
  for (let i = 0; i < rs.length; i++) {
    if (rs[i].status === "fulfilled") { itens = itens.concat(rs[i].value.data || []); continue; }
    await dormir(8000);
    itens = itens.concat((await paginaP(uf, mod, i + 2)).data || []);  // se falhar de novo, o estado aparece como "falhou"
  }
  const d = new Date(), agora = `${d.getFullYear()}-${doisd(d.getMonth() + 1)}-${doisd(d.getDate())}T${doisd(d.getHours())}:${doisd(d.getMinutes())}`;
  const lista = itens.map(resumirP).filter((x) => !(x.encerramento && x.encerramento < agora));
  const res = {itens: lista, cortado: tot > 100};
  CACHE_P[k] = {quando: Date.now(), res};
  return res;
}
async function buscarEstadoDireto(uf, mods) {
  const itens = [], erros = [], cortadas = [], vistos = new Set();
  const rs = await Promise.allSettled(mods.map((m) => modalidadeP(uf, m)));
  rs.forEach((r, i) => {
    const nome = CFG.modalidades[mods[i]] || mods[i];
    if (r.status === "rejected") { erros.push(nome + ": " + r.reason.message); return; }
    r.value.itens.forEach((x) => { if (!vistos.has(x.id)) { vistos.add(x.id); itens.push(x); } });
    if (r.value.cortado) cortadas.push(nome);
  });
  const d = new Date();
  return {uf, itens, erros, cortadas, atualizado: `${doisd(d.getDate())}/${doisd(d.getMonth() + 1)}/${d.getFullYear()} ${doisd(d.getHours())}:${doisd(d.getMinutes())}`};
}
// tenta direto; se o navegador não conseguir falar com o PNCP, usa o servidor como antes
async function buscarEstado(uf, mods) {
  try {
    const d = await buscarEstadoDireto(uf, mods);
    if (d.itens.length || !d.erros.some((e) => e.includes("sem conexão"))) return d;
  } catch (e) {}
  const r = await fetch(`/api/busca?uf=${uf}&mods=${mods.join(",")}`), d = await r.json();
  if (!r.ok) throw new Error(d.erro || "Falha na busca.");
  return d;
}
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const brl = (v) => v == null ? "não informado" : Number(v) === 0 ? "Sigiloso" : "R$ " + Number(v).toLocaleString("pt-BR", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const semAcento = (s) => String(s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
// raiz da palavra: tira o plural/singular do fim, para "fraldas" achar "fralda" e "cartão" achar "cartões"
function raiz(w) {
  if (w.length < 4) return w;
  let m;
  if ((m = w.match(/^(.{3,})(?:oes|aes|aos|ao)$/))) return m[1];         // cartões, cartão -> cart
  if ((m = w.match(/^(.{3,})eis$/))) return m[1] + "e";                   // papéis -> pape (acha papel)
  if ((m = w.match(/^(.{3,}[aeiou])is$/))) return m[1];                   // hospitais -> hospita
  if ((m = w.match(/^(.{2,}[rzs])es$/))) return m[1];                   // colheres -> colher, luzes -> luz
  if ((m = w.match(/^(.{3,}[aeiou])l$/))) return m[1];                    // papel -> pape, hospital -> hospita
  if ((m = w.match(/^(.{3,})s$/))) return m[1];                           // fraldas -> fralda, geriátricas -> geriatrica
  return w;
}
const CONSULTA = "https://score.sigalicita.com.br/?cnpj=";
const MEUS = ["Compras.gov.br", "ComprasMG", "SISLOG", "Licitar Digital"];
const UFS = [["AC","Acre"],["AL","Alagoas"],["AP","Amapá"],["AM","Amazonas"],["BA","Bahia"],["CE","Ceará"],["DF","Distrito Federal"],["ES","Espírito Santo"],["GO","Goiás"],["MA","Maranhão"],["MT","Mato Grosso"],["MS","Mato Grosso do Sul"],["MG","Minas Gerais"],["PA","Pará"],["PB","Paraíba"],["PR","Paraná"],["PE","Pernambuco"],["PI","Piauí"],["RJ","Rio de Janeiro"],["RN","Rio Grande do Norte"],["RS","Rio Grande do Sul"],["RO","Rondônia"],["RR","Roraima"],["SC","Santa Catarina"],["SP","São Paulo"],["SE","Sergipe"],["TO","Tocantins"]];
const MODS = [[6,"Pregão Eletrônico"],[8,"Dispensa"],[4,"Concorrência Eletrônica"],[7,"Pregão Presencial"],[5,"Concorrência Presencial"],[12,"Credenciamento"],[9,"Inexigibilidade"],[1,"Leilão Eletrônico"]];
const PRAZOS = [["", "Qualquer prazo"], ["2", "Encerra em até 2 dias"], ["7", "Encerra em até 7 dias"], ["30", "Encerra em até 30 dias"]];

let DADOS = null, ORDEM = "encerra", MOSTRAR = 40, PRAZO = "", DIA = "", MODO_DIA = "no";
let PREF = { uf: "DF", portais: ["Compras.gov.br"], mods: [6, 8, 4, 7, 5, 12], cats: [], tipo: "Produto", ufs: [] };
try { Object.assign(PREF, JSON.parse(localStorage.getItem("busca_pref") || "{}")); } catch (e) {}
if (!Array.isArray(PREF.cats)) PREF.cats = [];
if (typeof PREF.tipo !== "string") PREF.tipo = "Produto";
PREF.cats = PREF.cats.filter((k) => k !== "Serviços" && k !== "Construção e manutenção");
function salvar() { try { localStorage.setItem("busca_pref", JSON.stringify(PREF)); } catch (e) {} }

$("#uf").innerHTML = `<option value="BR">BRASIL — todos os estados</option>` + UFS.map(([s, n]) => `<option value="${s}">${s} — ${n}</option>`).join("");
$("#uf").value = "DF";  // abre sempre no DF; Brasil só se ela escolher
$("#f-prazo").innerHTML = PRAZOS.map(([v, t], i) => `<label class="opc"><input type="radio" name="prazo" value="${v}"${i === 0 ? " checked" : ""}><span>${t}</span></label>`).join("");

function diasAte(s) {
  if (!s) return null;
  const d = new Date(s.length === 16 ? s + ":00" : s);
  if (isNaN(d)) return null;
  return (d - new Date()) / 86400000;
}
function textoPrazo(dias) {
  if (dias == null) return "";
  // conta pelos dias do calendário: encerra amanhã às 03h é "amanhã", não "hoje"
  const fim = new Date(Date.now() + dias * 86400000), hoje = new Date();
  const d0 = new Date(hoje.getFullYear(), hoje.getMonth(), hoje.getDate());
  const d1 = new Date(fim.getFullYear(), fim.getMonth(), fim.getDate());
  const n = Math.round((d1 - d0) / 86400000);
  if (n <= 0) return "Encerra hoje";
  if (n === 1) return "Encerra amanhã";
  return `Faltam ${n} dias`;
}
function dataHora(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(s || "");
  return m ? `${m[3]}/${m[2]}/${m[1]} às ${m[4]}:${m[5]}` : "não informado";
}
function numero(v) { const n = parseFloat(String(v || "").replace(/\./g, "").replace(",", ".")); return isNaN(n) ? null : n; }

function contar(lista, campo) { const c = {}; lista.forEach((x) => { c[x[campo]] = (c[x[campo]] || 0) + 1; }); return c; }

function filtrar() {
  if (!DADOS) return [];
  const q = semAcento($("#q").value).trim().split(/\s+/).filter(Boolean).map(raiz);
  const vmin = numero($("#vmin").value), vmax = numero($("#vmax").value);
  const lim = PRAZO ? parseFloat(PRAZO) : null;
  return DADOS.itens.filter((x) =>
    (!PREF.portais.length || PREF.portais.includes(x.portal)) &&
    (!PREF.tipo || (x.tipo || "Produto") === PREF.tipo) &&
    (DADOS.uf !== "BR" || !UFSEL.length || UFSEL.includes(x.uf)) &&
    (!PREF.cats.length || (x.categorias || []).some((c) => PREF.cats.includes(c))) &&
    (!q.length || q.every((p) => semAcento(x.objeto + " " + x.orgao + " " + x.unidade + " " + x.municipio).includes(p))) &&
    (vmin == null || (x.valor || 0) >= vmin) && (vmax == null || (x.valor != null && x.valor <= vmax)) &&
    (lim == null || (diasAte(x.encerramento) != null && diasAte(x.encerramento) <= lim)) &&
    (!DIA || (x.encerramento && (MODO_DIA === "no" ? x.encerramento.slice(0, 10) === DIA : x.encerramento.slice(0, 10) >= DIA)))
  ).sort((a, b) => {
    if (ORDEM === "recentes") return (b.publicacao || "").localeCompare(a.publicacao || "");
    if (ORDEM === "maior") return (b.valor || 0) - (a.valor || 0);
    if (ORDEM === "menor") return (a.valor ?? Infinity) - (b.valor ?? Infinity);
    return (a.encerramento || "9").localeCompare(b.encerramento || "9");
  });
}

function desenharPortais() {
  const c = DADOS ? contar(DADOS.itens, "portal") : {};
  const outros = Object.keys(c).filter((p) => !MEUS.includes(p)).sort((a, b) => c[b] - c[a]);
  const linha = (p, meu) => `<label class="opc${meu ? " meu" : ""}"><input type="checkbox" data-portal="${esc(p)}"${PREF.portais.includes(p) ? " checked" : ""}><span>${meu ? "★ " : ""}${esc(p)}</span><span class="n">${c[p] || 0}</span></label>`;
  $("#f-portal").innerHTML =
    `<div class="sub">Meus portais</div>` + MEUS.map((p) => linha(p, true)).join("") +
    `<div style="display:flex;gap:12px;margin:6px 0 2px"><button type="button" class="mini-bt" data-sel="meus">Só os meus</button><button type="button" class="mini-bt" data-sel="todos">Todos</button></div>` +
    (outros.length ? `<details><summary style="font-size:11px">Outros portais (${outros.length})</summary>${outros.map((p) => linha(p, false)).join("")}</details>` : "");
}
function desenharCats() {
  const c = {};
  if (DADOS) DADOS.itens.filter((x) => (!PREF.portais.length || PREF.portais.includes(x.portal)) && (!PREF.tipo || (x.tipo || "Produto") === PREF.tipo))
    .forEach((x) => (x.categorias || []).forEach((k) => { c[k] = (c[k] || 0) + 1; }));
  const nomes = Object.keys(c).sort((a, b) => (a === "Outros") - (b === "Outros") || c[b] - c[a]);
  PREF.cats.forEach((k) => { if (!nomes.includes(k)) nomes.push(k); });
  const todas = nomes.length && nomes.every((k) => PREF.cats.includes(k));
  $("#f-cat").innerHTML = (nomes.length ? `<label class="opc" style="border-bottom:1px solid var(--line);padding-bottom:7px;margin-bottom:3px"><input type="checkbox" data-todascat="1"${todas ? " checked" : ""}><span><b>Todas</b></span></label>` : "") + (nomes.length ? nomes.map((k) => `<label class="opc"><input type="checkbox" data-cat="${esc(k)}"${PREF.cats.includes(k) ? " checked" : ""}><span>${k === "Eletrodomésticos" ? "🔧 " : ""}${esc(k)}</span><span class="n">${c[k] || 0}</span></label>`).join("") : `<p class="loc">As categorias aparecem depois da busca.</p>`) +
    (PREF.cats.length ? `<button type="button" class="mini-bt" data-limpacat="1">Mostrar todas as categorias</button>` : "");
}
let UFSEL = [];
function desenharUfs() {
  const atual = $("#uf").value;
  const c = {}, st = (DADOS && DADOS.status) || {};
  if (DADOS && DADOS.uf === "BR") filtrar().forEach((x) => { c[x.uf] = (c[x.uf] || 0) + 1; });
  const falhos = UFS.map((u) => u[0]).filter((u) => st[u] === "erro");
  $("#f-ufs").innerHTML = `<div class="ufs">` +
    `<button type="button" class="ufc br" data-uf="BR" aria-pressed="${atual === "BR"}" title="Todos os estados">Brasil</button>` +
    UFS.map(([u, n]) => {
      const sel = atual === u, s = DADOS && DADOS.uf === "BR" ? st[u] : null;
      const cls = s === "erro" ? " uf-erro" : c[u] ? " uf-ok" : "";
      const dica = n + (s === "erro" ? ": o PNCP não respondeu" : c[u] ? `: ${c[u]} licitaç${c[u] === 1 ? "ão" : "ões"} com a sua busca` : s === "ok" ? ": nenhuma licitação com a sua busca" : DADOS && DADOS.uf === "BR" ? ": ainda buscando..." : "");
      return `<button type="button" class="ufc${cls}" data-uf="${u}" aria-pressed="${sel}" title="${esc(dica)}">${u}</button>`;
    }).join("") + `</div>` +
    `<div class="uf-info">${falhos.length ? `<b style="color:var(--bad)">${falhos.join(", ")}: o PNCP não respondeu.</b> Clique em Buscar para tentar de novo.` : "Clique num estado, ou em Brasil para todos."}</div>`;
}
function desenharMods() {
  const c = DADOS ? contar(DADOS.itens, "modalidade") : {};
  $("#f-mod").innerHTML = MODS.map(([id, nome]) => `<label class="opc"><input type="checkbox" data-mod="${id}"${PREF.mods.includes(id) ? " checked" : ""}><span>${nome}</span><span class="n">${DADOS && PREF.mods.includes(id) ? (c[id] || 0) : ""}</span></label>`).join("");
}

function cartao(x, emSalvas) {
  const dias = diasAte(x.encerramento), meu = MEUS.includes(x.portal);
  const encerrada = x.encerramento && x.encerramento < agoraLocal();
  const urg = dias != null && dias <= 2;
  return `<article class="card${meu ? " meu" : ""}">
    <div class="tags">
      ${x.numero ? `<span class="tag num">${esc(x.numero)}</span>` : ""}
      <span class="tag mod">${esc(x.modalidadeNome)}</span>
      ${x.srp ? `<span class="tag srp">Registro de preços</span>` : ""}
      ${encerrada ? `<span class="tag encerrada">Encerrada</span>` : dias != null ? `<span class="tag prazo${urg ? " urg" : ""}">${textoPrazo(dias)}</span>` : ""}
      <span class="tag">📍 ${esc(x.uf)}</span>
      ${x.uasg ? `<span class="tag">UASG ${esc(x.uasg)}</span>` : ""}
      ${seloPortal(x.portal)}
      ${(x.categorias || []).filter((k) => k !== "Outros").map((k) => `<span class="tag cat">${esc(k)}</span>`).join("")}
      ${x.tipo === "Serviço" ? `<span class="tag">Serviço</span>` : ""}
      ${x.assistencia ? `<span class="tag assist" title="Eletrodoméstico ou objeto que fala em assistência técnica: confira as exigências no edital">🔧 Exige assistência técnica</span>` : ""}
    </div>
    <div class="corpo">
      <h3><a href="/licitacao?id=${encodeURIComponent(x.id)}" target="_blank" rel="noopener">${esc(x.orgao)}</a></h3>
      <p class="obj"><a href="/licitacao?id=${encodeURIComponent(x.id)}" target="_blank" rel="noopener" title="Abrir a página da licitação">${esc(x.objeto)}</a></p>
      <div class="loc">${esc([x.municipio, x.uf].filter(Boolean).join(" · "))}${x.unidade ? " · " + esc(x.unidade) : ""}</div>
    </div>
    <div class="lado">
      <div><span>Valor estimado</span><b>${brl(x.valor)}</b></div>
      <div class="ate"><span>Propostas até</span><b class="${urg ? "" : "ok"}">${dataHora(x.encerramento)}</b></div>
    </div>
    <div class="acoes">
      ${x.linkPortal ? `<a class="pri" href="${esc(x.linkPortal)}" target="_blank" rel="noopener">Abrir no ${esc(x.portal.length > 22 ? "portal" : x.portal)}</a>` : ""}
      ${x.linkPncp ? `<a href="${esc(x.linkPncp)}" target="_blank" rel="noopener">Ver no PNCP</a>` : ""}
      ${x.cnpj ? htmlNota(x.cnpj) : ""}
      ${emSalvas ? `<button type="button" class="remover" data-salvar="${esc(x.id)}">🗑 Remover</button>`
        : SALVAS[x.id] ? `<span class="ja-salva" title="Esta licitação está nas suas Oportunidades">⭐ Salva</span>` : ""}
    </div>
    ${ABERTOS[x.id] ? `<div class="det">${painelDet(DET[x.id], x)}</div>` : ""}
  </article>`;
}

const ABERTOS = {}, DET = {};
// ===== Oportunidades: licitações salvas (ficam guardadas neste navegador) =====
let SALVAS = {}, VER_SALVAS = false;
try { SALVAS = JSON.parse(localStorage.getItem("busca_salvas") || "{}") || {}; } catch (e) { SALVAS = {}; }
function agoraLocal() { const d = new Date(), z = (n) => String(n).padStart(2, "0"); return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())}T${z(d.getHours())}:${z(d.getMinutes())}`; }
function guardarSalvas() {
  try { localStorage.setItem("busca_salvas", JSON.stringify(SALVAS)); } catch (e) {}
  $("#n-salvas").textContent = Object.keys(SALVAS).length;
}
function alternarSalva(id) {
  if (SALVAS[id]) delete SALVAS[id];
  else {
    const x = DADOS && DADOS.itens.find((i) => i.id === id);
    if (!x) return;
    SALVAS[id] = Object.assign({}, x, {salvaEm: Date.now()});
  }
  guardarSalvas(); desenhar();
}
function botaoSalvarDet(id) {
  return SALVAS[id]
    ? `<button type="button" class="salvar on" data-salvar-det="1" title="Clique para tirar das Oportunidades">⭐ Salva nas Oportunidades</button>`
    : `<button type="button" class="salvar" data-salvar-det="1" title="Guardar em Oportunidades para analisar depois">☆ Salvar</button>`;
}
// salvou em outra aba (na página da licitação): a busca atualiza o número e os botões sozinha
window.addEventListener("storage", (e) => {
  if (e.key !== "busca_salvas") return;
  try { SALVAS = JSON.parse(e.newValue || "{}") || {}; } catch (er) { SALVAS = {}; }
  $("#n-salvas").textContent = Object.keys(SALVAS).length;
  if (!document.querySelector("#pg")) desenhar();
});
function botaoSalvarTodas(lista) {
  if (!lista.length) return "";
  const faltam = lista.filter((x) => !SALVAS[x.id]).length;
  if (!faltam) return `<span class="salvar-todas feito" title="Todas desta busca já estão nas Oportunidades">⭐ Todas salvas</span>`;
  return `<button type="button" class="salvar-todas" data-salvar-todas="1" title="Guardar todas desta busca em Oportunidades">⭐ Salvar todas (${faltam.toLocaleString("pt-BR")})</button>`;
}
function salvarTodas() {
  const novas = filtrar().filter((x) => !SALVAS[x.id]);
  if (!novas.length) return;
  if (novas.length > 30 && !confirm(`Salvar as ${novas.length.toLocaleString("pt-BR")} licitações desta busca nas Oportunidades?`)) return;
  const agora = Date.now();
  novas.forEach((x) => { SALVAS[x.id] = Object.assign({}, x, {salvaEm: agora}); });
  guardarSalvas(); desenhar();
}
function desenharSalvas() {
  // as que ainda estão abertas primeiro (a disputa mais próxima em cima); as encerradas vão para o fim
  const agora = agoraLocal(), fechou = (x) => !!(x.encerramento && x.encerramento < agora);
  const lista = Object.values(SALVAS).sort((a, b) => fechou(a) - fechou(b) || (a.encerramento || "9").localeCompare(b.encerramento || "9"));
  let h = `<div class="cab"><span><b>${lista.length}</b> oportunidade${lista.length === 1 ? " salva" : "s salvas"} para analisar</span><button type="button" class="mini-bt" id="voltar">← Voltar para a busca</button></div>`;
  if (!lista.length) h += `<div class="vazio"><b>Nenhuma oportunidade salva ainda</b>Na busca, clique em ☆ Salvar na licitação que quer analisar depois.</div>`;
  h += lista.map((x) => cartao(x, true)).join("");
  $("#res").innerHTML = h;
}
function seloPortal(p) {
  if (p === "Compras.gov.br") return `<span class="selo-cg" title="Licitação do Compras.gov.br"><i><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4h2l2.5 11h11L21 7H6.5"/><circle cx="9" cy="20" r="1.4"/><circle cx="18" cy="20" r="1.4"/></svg></i><span>Compras<em>.gov.br</em></span></span>`;
  const cor = {"ComprasMG": "selo-mg", "SISLOG": "selo-go", "Licitar Digital": "selo-ld"}[p];
  if (cor) return `<span class="selo-p ${cor}" title="Licitação do ${esc(p)}">${esc(p)}</span>`;
  return `<span class="tag portal${MEUS.includes(p) ? " meu" : ""}">${esc(p)}</span>`;
}
// ===== botão Score: abre o Consulta Pagador com o CNPJ do órgão =====
function htmlNota(cnpj) {
  return `<a class="pag" href="${CONSULTA}${cnpj}" target="_blank" rel="noopener" title="Ver a nota do órgão no Consulta Pagador">📊 Score</a>`;
}
function pedirNotas() {}
const ICDOC = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 3H6v18h12V7l-4-4z"/><path d="M14 3v4h4M12 11v6M9 14l3 3 3-3"/></svg>';
function qtd(v) { return v == null ? "" : Number(v).toLocaleString("pt-BR", {maximumFractionDigits: 4}); }
function dataSo(s) { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || ""); return m ? `${m[3]}/${m[2]}/${m[1]}` : ""; }
function cnpjFmt(c) { return c && c.length === 14 ? c.replace(/^(\d{2})(\d{3})(\d{3})(\d{4})(\d{2})$/, "$1.$2.$3/$4-$5") : c || ""; }
function blocoDados(x) {
  const campos = [["Órgão", x.orgao], ["CNPJ", cnpjFmt(x.cnpj), 1], ["Unidade", x.unidade], ["Código da unidade (UASG)", x.uasg, 1],
    ["Local", [x.municipio, x.uf].filter(Boolean).join(" - ")], ["Esfera", x.esfera], ["Poder", x.poder], ["Modalidade", x.modalidadeNome],
    ["Modo de disputa", x.disputa], ["Amparo legal", x.amparo], ["Processo", x.processo, 1], ["Registro de preços", x.srp ? "Sim" : "Não"],
    ["Abertura das propostas", dataHora(x.abertura)], ["Propostas até", dataHora(x.encerramento)], ["Publicado no PNCP", dataSo(x.publicacao)],
    ["Situação", x.situacao], ["Portal", x.portal], ["Nº de controle PNCP", x.id, 1]];
  return `<div class="bts"><button type="button" data-cal="${esc(x.id)}">📅 Salvar no calendário</button><button type="button" data-copiar="${esc(x.linkPortal || x.linkPncp)}">🔗 Copiar link</button></div>
    <div><h4>Objeto completo</h4><p class="txt">${esc(x.objeto)}</p></div>
    ${x.complemento && x.complemento !== x.objeto ? `<div><h4>Informação complementar</h4><p class="txt">${esc(x.complemento)}</p></div>` : ""}
    <div><h4>Dados da contratação</h4><div class="dados">${campos.filter((c) => c[1]).map(([k, v, m]) => `<div><span>${k}</span><b${m ? ' class="m"' : ""}>${esc(v)}</b></div>`).join("")}</div></div>`;
}
function painelDet(d, x) {
  const dados = x ? blocoDados(x) : "";
  if (!d) return dados + `<div class="carregando" style="padding:12px"><div class="spin"></div>Buscando documentos e itens no PNCP…</div>`;
  if (d.erro) return dados + `<div class="aviso">${esc(d.erro)}</div>`;
  let h = dados; `<div class="carregando" style="padding:12px"><div class="spin"></div>Buscando documentos e itens no PNCP…</div>`;
  if (d.erro) return `<div class="aviso">${esc(d.erro)}</div>`;
  if (d.erros && d.erros.length) h += `<div class="aviso">O PNCP não respondeu para ${esc(d.erros.join("; "))}. Tente de novo em alguns minutos.</div>`;
  h += `<div><h4>Documentos (${d.arquivos.length})</h4>` + (d.arquivos.length
    ? `<div class="docs">${d.arquivos.map((a) => `<a class="doc" href="${esc(a.url)}" target="_blank" rel="noopener">${ICDOC}<span>${esc(a.titulo)}${a.tipo && a.tipo !== a.titulo ? " · " + esc(a.tipo) : ""}</span><small>${esc(a.data.split("-").reverse().join("/"))}</small></a>`).join("")}</div>`
    : `<p class="loc">Nenhum documento publicado no PNCP. Veja no portal da licitação.</p>`) + `</div>`;
  if (d.itens.length) {
    const tot = d.itens.reduce((s, i) => s + (i.vt || 0), 0);
    h += `<div><h4>Itens (${d.itens.length})</h4><div class="tw"><table><thead><tr><th>Item</th><th style="text-align:left">Descrição</th><th>Qtd</th><th>Un.</th><th>Valor unit.</th><th>Total</th></tr></thead><tbody>` +
      d.itens.map((i) => `<tr><td>${esc(i.n)}</td><td class="d">${esc(i.descricao)}${i.tipo ? `<span class="tipo">${esc(i.tipo)}</span>` : ""}${i.me && !/sem benef|nao se aplica|não se aplica/i.test(i.me) ? `<small>${esc(i.me)}</small>` : ""}</td><td>${qtd(i.qtd)}</td><td>${esc(i.un)}</td><td>${i.vu != null ? brl(i.vu) : "—"}</td><td>${i.vt != null ? brl(i.vt) : "—"}</td></tr>`).join("") +
      `</tbody>${tot ? `<tfoot><tr><td colspan="5">Total estimado</td><td>${brl(tot)}</td></tr></tfoot>` : ""}</table></div></div>`;
  }
  return h;
}
function salvarCalendario(x) {
  if (!x || !x.encerramento) return;
  const ics = (s) => s.replace(/[-:]/g, "").slice(0, 13) + "00";
  const fim = ics(x.encerramento);
  const d0 = new Date(new Date(x.encerramento + ":00").getTime() - 3600000), z = (n) => String(n).padStart(2, "0");
  const ini = `${d0.getFullYear()}${z(d0.getMonth() + 1)}${z(d0.getDate())}T${z(d0.getHours())}${z(d0.getMinutes())}00`;
  const txt = (s) => String(s || "").replace(/[\\;,]/g, (c) => "\\" + c).replace(/\n/g, " ");
  const corpo = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Busca Licitacoes//PT", "BEGIN:VEVENT",
    "UID:" + x.id.replace(/\W/g, "") + "@busca-licitacoes", "DTSTAMP:" + new Date().toISOString().replace(/[-:]/g, "").slice(0, 15) + "Z",
    "DTSTART:" + ini, "DTEND:" + fim,
    "SUMMARY:" + txt("Prazo de proposta: " + x.modalidadeNome + " " + x.numero + " - " + x.orgao),
    "DESCRIPTION:" + txt(x.objeto + " | " + (x.linkPortal || x.linkPncp)),
    "BEGIN:VALARM", "TRIGGER:-PT2H", "ACTION:DISPLAY", "DESCRIPTION:Prazo de proposta encerra em 2 horas", "END:VALARM",
    "END:VEVENT", "END:VCALENDAR"].join("\r\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([corpo], {type: "text/calendar"}));
  a.download = "prazo-" + (x.numero || "licitacao").replace(/\W/g, "-") + ".ics";
  document.body.appendChild(a); a.click(); a.remove();
}
async function abrirDet(id) {
  ABERTOS[id] = !ABERTOS[id];
  desenhar();
  if (!ABERTOS[id] || DET[id]) return;
  try {
    const r = await fetch(`/api/detalhes?id=${encodeURIComponent(id)}`);
    const d = await r.json();
    DET[id] = r.ok ? d : {erro: d.erro || "Falha ao buscar no PNCP."};
  } catch (e) { DET[id] = {erro: "Falha ao buscar no PNCP. Tente de novo."}; }
  if (DET[id].erro || (DET[id].erros && DET[id].erros.length)) { const x = DET[id]; delete DET[id]; DET[id] = x; setTimeout(() => { if (DET[id] === x) delete DET[id]; }, 1000); }
  desenhar();
}

function desenhar() {
  desenharPortais(); desenharCats(); desenharMods(); desenharUfs();
  $("#aba-op").setAttribute("aria-pressed", VER_SALVAS);
  if (VER_SALVAS) return desenharSalvas();
  if (!DADOS) return;
  const lista = filtrar(), nome = DADOS.uf === "BR" ? "todo o Brasil" : (UFS.find((u) => u[0] === DADOS.uf) || ["", DADOS.uf])[1];
  let h = `<div class="cab"><span><b>${lista.length.toLocaleString("pt-BR")}</b> licitaç${lista.length === 1 ? "ão encontrada" : "ões encontradas"} em <b>${esc(nome)}</b> com proposta em aberto${DIA ? ` · disputa ${MODO_DIA === "no" ? "no dia" : "a partir de"} <b>${DIA.split("-").reverse().join("/")}</b>` : ""}</span>${botaoSalvarTodas(lista)}<span class="at">atualizado ${esc(DADOS.atualizado)}</span></div>`;
  if (DADOS.progresso) h += `<div class="carregando" style="padding:12px 16px"><div class="spin"></div>Carregando os estados: ${DADOS.progresso.feitos} de ${DADOS.progresso.total}. As licitações vão aparecendo enquanto isso.</div>`;
  if (DADOS.erros.length) h += (DADOS.uf === "BR" || DADOS.uf === "MEUS")
    ? `<div class="aviso">O PNCP não respondeu para: <b>${esc(DADOS.erros.join(", "))}</b>. Clique em Buscar de novo daqui a alguns minutos (os estados que já vieram ficam guardados e voltam rápido).</div>`
    : `<div class="aviso">O PNCP não respondeu para: ${esc(DADOS.erros.join("; "))}. Tente buscar de novo em alguns minutos.</div>`;
  if (DADOS.cortadas.length) h += `<div class="aviso">Muitas licitações em ${esc(DADOS.cortadas.join(", "))}: mostrando as 5.000 primeiras de cada uma. Use a palavra-chave para achar o que procura.</div>`;
  if (!lista.length) h += `<div class="vazio"><b>Nenhuma licitação com esses filtros</b>Tente marcar mais portais ou modalidades, ou mudar a palavra-chave.</div>`;
  h += lista.slice(0, MOSTRAR).map((x) => cartao(x)).join("");
  if (lista.length > MOSTRAR) h += `<button type="button" class="mais" id="mais">Mostrar mais (${(lista.length - MOSTRAR).toLocaleString("pt-BR")} restantes)</button>`;
  $("#res").innerHTML = h;
  pedirNotas();
}

let pedido = 0;
async function buscarVarios(meu, lista, novo, modo) {
  if (novo) { DADOS = {uf: modo, itens: [], erros: [], cortadas: [], atualizado: "", status: {}, progresso: null, vistos: new Set()}; UFSEL = []; }
  if (!DADOS.progresso) DADOS.progresso = {feitos: 0, total: 0};
  DADOS.progresso.total += lista.length;
  if (!DADOS.carregando) DADOS.carregando = new Set();
  lista.forEach((u) => DADOS.carregando.add(u));
  lista.forEach((u) => { delete DADOS.status[u]; DADOS.erros = DADOS.erros.filter((x) => x !== u); });
  const vistos = DADOS.vistos, meuDados = DADOS;
  desenhar();
  let ultimoDesenho = 0;
  async function umEstado(uf) {
    try {
      const d = await buscarEstado(uf, PREF.mods);
      if (meu !== pedido) return true;
      d.itens.forEach((x) => { if (!vistos.has(x.id)) { vistos.add(x.id); DADOS.itens.push(x); } });
      d.cortadas.forEach((c) => { if (!DADOS.cortadas.includes(uf + " " + c)) DADOS.cortadas.push(uf + " " + c); });
      DADOS.atualizado = d.atualizado;
      if (d.erros.length && !d.itens.length) return false;  // PNCP não respondeu nada: tenta de novo depois
      return true;
    } catch (e) { return false; }
  }
  async function processa(uf, ultima, falhas, fila) {
    const ok = await umEstado(uf);
    if (meu !== pedido || DADOS !== meuDados) return false;
    DADOS.carregando.delete(uf);
    if (ok) { DADOS.status[uf] = "ok"; DADOS.progresso.feitos++; }
    else if (ultima) { DADOS.status[uf] = "erro"; DADOS.progresso.feitos++; DADOS.erros.push(uf); }
    else falhas.push(uf);
    if (PRIORIDADE.has(uf) || Date.now() - ultimoDesenho > 1500 || !fila.length) { ultimoDesenho = Date.now(); desenhar(); }
    return true;
  }
  async function rodada(fila, nos, ultima) {
    const falhas = [], extras = [];
    // estado marcado por ela que ainda está esperando na fila: sai da fila e começa já
    if (!ultima) DADOS.furarFila = (uf) => {
      const i = fila.indexOf(uf); if (i < 0) return;
      fila.splice(i, 1); extras.push(processa(uf, false, falhas, fila));
    };
    async function trabalhador() {
      while (fila.length) {
        if (!(await processa(fila.shift(), ultima, falhas, fila))) return;
      }
    }
    await Promise.all(Array.from({length: nos}, trabalhador));
    while (extras.length) await extras.shift();
    if (!ultima) DADOS.furarFila = null;
    return falhas;
  }
  let falhas = await rodada(lista.slice(), 3, false);  // 3 estados por vez (no máximo 4 pedidos ao PNCP ao mesmo tempo)
  for (let t = 0; t < 2 && falhas.length && meu === pedido && DADOS === meuDados; t++) {
    await new Promise((ok) => setTimeout(ok, 4000));  // respira e tenta de novo, um por vez
    falhas = await rodada(falhas, 1, t === 1);
  }
  if (meu !== pedido || DADOS !== meuDados) return;
  if (DADOS.progresso && DADOS.progresso.feitos >= DADOS.progresso.total) DADOS.progresso = null;
  desenhar();
}
async function buscar() {
  const uf = $("#uf").value, meu = ++pedido;
  if (uf === "BR" || uf === "MEUS") {
    PREF.uf = uf; salvar(); MOSTRAR = 40; $("#bt").disabled = true;
    try { await buscarVarios(meu, UFS.map((u) => u[0]), true, uf); } finally { if (meu === pedido) $("#bt").disabled = false; }
    return;
  }
  PREF.uf = uf; salvar(); MOSTRAR = 40; desenharUfs();
  $("#bt").disabled = true;
  $("#res").innerHTML = `<div class="carregando"><div class="spin"></div>Buscando licitações abertas de ${esc(uf)} no PNCP. Leva alguns segundos.</div>`;
  try {
    const d = await buscarEstado(uf, PREF.mods);
    if (meu !== pedido) return;
    DADOS = d; desenhar();
  } catch (e) {
    if (meu === pedido) $("#res").innerHTML = `<div class="erro"><b>Não deu para buscar.</b> ${esc(e.message)}</div>`;
  } finally { if (meu === pedido) $("#bt").disabled = false; }
}

$("#f").addEventListener("submit", (e) => {
  e.preventDefault();
  VER_SALVAS = false;
  if (!DADOS || DADOS.uf !== $("#uf").value) return buscar();
  // Brasil / meus estados: "Buscar" de novo tenta só os estados que falharam
  if ((DADOS.uf === "BR" || DADOS.uf === "MEUS") && !DADOS.progresso && DADOS.erros.length) {
    const falhos = DADOS.erros.filter((u) => /^[A-Z]{2}$/.test(u));
    if (falhos.length) { buscarVarios(pedido, falhos, false); return; }
  }
  MOSTRAR = 40; desenhar();
});
$("#uf").addEventListener("change", () => { VER_SALVAS = false; buscar(); });
$("#aba-op").addEventListener("click", () => { VER_SALVAS = !VER_SALVAS; window.scrollTo(0, 0); desenhar(); });
guardarSalvas();
$("#q").addEventListener("input", () => { MOSTRAR = 40; if (DADOS) desenhar(); });
["#vmin", "#vmax"].forEach((s) => $(s).addEventListener("input", () => { if (DADOS) desenhar(); }));
function hojeMais(n) { const d = new Date(); d.setDate(d.getDate() + n); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; }
$("#dia").min = hojeMais(0);
$("#dia").addEventListener("change", () => { DIA = $("#dia").value; MOSTRAR = 40; desenhar(); });
$("#modo-dia").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  MODO_DIA = b.dataset.m; document.querySelectorAll("#modo-dia button").forEach((x) => x.setAttribute("aria-pressed", x === b)); desenhar();
});
$("#atalhos-dia").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  DIA = b.dataset.d === "" ? "" : hojeMais(+b.dataset.d); $("#dia").value = DIA; MOSTRAR = 40; desenhar();
});
$("#f-prazo").addEventListener("change", (e) => { PRAZO = e.target.value; desenhar(); });
$("#f-ordem").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  ORDEM = b.dataset.o; document.querySelectorAll("#f-ordem button").forEach((x) => x.setAttribute("aria-pressed", x === b)); desenhar();
});
$("#f-ufs").addEventListener("click", (e) => {
  const bt = e.target.closest("button[data-uf]"); if (!bt) return;
  VER_SALVAS = false;
  if ($("#uf").value === bt.dataset.uf && DADOS) { desenhar(); return; }
  $("#uf").value = bt.dataset.uf; buscar();
});
$("#f-portal").addEventListener("change", (e) => {
  const p = e.target.dataset.portal; if (p == null) return;
  PREF.portais = e.target.checked ? [...new Set(PREF.portais.concat(p))] : PREF.portais.filter((x) => x !== p);
  salvar(); MOSTRAR = 40; desenhar();
});
$("#f-portal").addEventListener("click", (e) => {
  const s = e.target.dataset.sel; if (!s) return;
  PREF.portais = s === "meus" ? MEUS.slice() : []; salvar(); desenhar();
});
document.querySelectorAll("#f-tipo button").forEach((x) => x.setAttribute("aria-pressed", x.dataset.t === PREF.tipo));
$("#f-tipo").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  PREF.tipo = b.dataset.t; salvar(); MOSTRAR = 40;
  document.querySelectorAll("#f-tipo button").forEach((x) => x.setAttribute("aria-pressed", x === b)); desenhar();
});
$("#f-cat").addEventListener("change", (e) => {
  if (e.target.dataset.todascat) {
    PREF.cats = e.target.checked ? [...document.querySelectorAll("#f-cat [data-cat]")].map((x) => x.dataset.cat) : [];
    salvar(); MOSTRAR = 40; desenhar(); return;
  }
  const k = e.target.dataset.cat; if (k == null) return;
  PREF.cats = e.target.checked ? [...new Set(PREF.cats.concat(k))] : PREF.cats.filter((x) => x !== k);
  salvar(); MOSTRAR = 40; desenhar();
});
$("#f-cat").addEventListener("click", (e) => {
  if (!e.target.dataset.limpacat) return;
  PREF.cats = []; salvar(); MOSTRAR = 40; desenhar();
});
$("#f-mod").addEventListener("change", (e) => {
  const m = +e.target.dataset.mod; if (!m) return;
  PREF.mods = e.target.checked ? [...new Set(PREF.mods.concat(m))] : PREF.mods.filter((x) => x !== m);
  if (!PREF.mods.length) { PREF.mods = [m]; e.target.checked = true; }
  salvar(); buscar();
});
$("#res").addEventListener("click", (e) => {
  if (e.target.id === "mais") { MOSTRAR += 40; desenhar(); return; }
  const bs = e.target.closest("[data-salvar]");
  if (bs) { alternarSalva(bs.dataset.salvar); return; }
  if (e.target.closest("[data-salvar-todas]")) { salvarTodas(); return; }
  if (e.target.id === "voltar") { VER_SALVAS = false; desenhar(); return; }
  const bc = e.target.closest("[data-copiar]");
  if (bc) {
    const t = bc.dataset.copiar, ok = () => { bc.textContent = "✔ Link copiado"; setTimeout(() => { bc.textContent = "🔗 Copiar link"; }, 2000); };
    if (navigator.clipboard) navigator.clipboard.writeText(t).then(ok, () => prompt("Copie o link:", t)); else prompt("Copie o link:", t);
    return;
  }
  const bk = e.target.closest("[data-cal]");
  if (bk) { salvarCalendario(DADOS.itens.find((i) => i.id === bk.dataset.cal)); return; }
  const bd = e.target.closest("[data-det]");
  if (bd) { abrirDet(bd.dataset.det); return; }
  if (e.target.classList.contains("obj")) e.target.classList.toggle("aberto");
});

// ===== página de uma licitação: /licitacao?id=... =====
function textoEncerra(dias) {
  if (dias == null) return "";
  if (dias < 0) return "Prazo encerrado";
  if (dias < 1 / 24) return "Encerra em minutos";
  if (dias < 1) return `Encerra em ${Math.ceil(dias * 24)} horas`;
  return textoPrazo(dias);
}
async function paginaLicitacao(id) {
  document.querySelector("#f").hidden = true;
  document.querySelector(".grade").hidden = true;
  const alvo = document.createElement("main");
  alvo.className = "pg"; alvo.id = "pg";
  document.querySelector(".grade").after(alvo);
  alvo.innerHTML = `<a class="voltar" href="/">← Nova busca</a><div class="carregando"><div class="spin"></div>Carregando a licitação no PNCP…</div>`;
  let x;
  try {
    const r = await fetch(`/api/licitacao?id=${encodeURIComponent(id)}`);
    x = await r.json();
    if (!r.ok) throw new Error(x.erro || "Falha ao buscar no PNCP.");
  } catch (e) {
    alvo.innerHTML = `<a class="voltar" href="/">← Nova busca</a><div class="erro"><b>Não deu para abrir a licitação.</b> ${esc(e.message)}</div>`;
    return;
  }
  DADOS = {itens: [x]};
  document.title = `${x.modalidadeNome} ${x.numero} · ${x.orgao}`;
  const dias = diasAte(x.encerramento), meu = MEUS.includes(x.portal);
  alvo.innerHTML = `<a class="voltar" href="/">← Nova busca</a>
    <div>
      <div class="tags">
        ${x.numero ? `<span class="tag num">${esc(x.numero)}</span>` : ""}
        <span class="tag mod">${esc(x.modalidadeNome)}</span>
        ${x.srp ? `<span class="tag srp">Registro de preços</span>` : ""}
        ${dias != null && dias >= 0 ? `<span class="tag prazo${dias <= 2 ? " urg" : ""}">${textoPrazo(dias)}</span>` : ""}
        <span class="tag">📍 ${esc(x.uf)}</span>
        ${x.uasg ? `<span class="tag">UASG ${esc(x.uasg)}</span>` : ""}
        ${seloPortal(x.portal)}
        ${(x.categorias || []).filter((k) => k !== "Outros").map((k) => `<span class="tag cat">${esc(k)}</span>`).join("")}
        ${x.assistencia ? `<span class="tag assist">🔧 Exige assistência técnica</span>` : ""}
      </div>
      <h2>${esc(x.objeto)}</h2>
      <div class="org">${esc(x.orgao)} <small>· ${esc(x.unidade)}${x.municipio ? " · " + esc(x.municipio) + " - " + esc(x.uf) : ""}</small></div>
    </div>
    <div class="quad">
      <div><span>Valor estimado</span><b>${brl(x.valor)}</b></div>
      <div><span>Propostas até</span><b class="${dias != null && dias <= 2 ? "urg" : ""}">${dataHora(x.encerramento)}</b><small>${esc(textoEncerra(dias))}</small></div>
      <div><span>Abertura das propostas</span><b>${dataHora(x.abertura)}</b></div>
      <div><span>Publicado no PNCP</span><b>${dataSo(x.publicacao) || "—"}</b></div>
    </div>
    <div class="acoes">
      ${x.linkPortal ? `<a class="pri" href="${esc(x.linkPortal)}" target="_blank" rel="noopener">Abrir no ${esc(x.portal.length > 22 ? "portal" : x.portal)}</a>` : ""}
      ${x.linkPncp ? `<a href="${esc(x.linkPncp)}" target="_blank" rel="noopener">Ver no PNCP (oficial)</a>` : ""}
      ${x.cnpj ? htmlNota(x.cnpj) : ""}
      <button type="button" data-cal="${esc(x.id)}">📅 Salvar no calendário</button>
      <button type="button" data-copiar="${esc(location.href)}">🔗 Copiar link</button>
      ${botaoSalvarDet(x.id)}
    </div>
    ${""}<section class="secao" id="sec-docs"><h3>Documentos do edital</h3><div class="carregando" style="padding:12px"><div class="spin"></div>Buscando documentos e itens no PNCP…</div></section>
    <section class="secao" id="sec-itens" hidden></section>
    <section class="secao"><h3>Objeto completo</h3><p class="txt">${esc(x.objeto)}</p>
      ${x.complemento && x.complemento !== x.objeto ? `<h3>Informação complementar</h3><p class="txt">${esc(x.complemento)}</p>` : ""}</section>
    <section class="secao"><h3>Dados da contratação</h3>${blocoDados(x).replace(/^[\s\S]*?<div><h4>Dados da contratação<\/h4>/, "<div>")}</section>`;
  pedirNotas();
  alvo.addEventListener("click", (e) => {
    const bc = e.target.closest("[data-copiar]"), bk = e.target.closest("[data-cal]"), bs = e.target.closest("[data-salvar-det]");
    if (bk) salvarCalendario(x);
    if (bs) {
      if (SALVAS[x.id]) delete SALVAS[x.id];
      else { const y = Object.assign({}, x, {salvaEm: Date.now()}); delete y.complemento; SALVAS[x.id] = y; }
      guardarSalvas();
      bs.outerHTML = botaoSalvarDet(x.id);
    }
    if (bc) {
      const t = bc.dataset.copiar, ok = () => { bc.textContent = "✔ Link copiado"; setTimeout(() => { bc.textContent = "🔗 Copiar link"; }, 2000); };
      if (navigator.clipboard) navigator.clipboard.writeText(t).then(ok, () => prompt("Copie o link:", t)); else prompt("Copie o link:", t);
    }
  });
  let d;
  try {
    const r = await fetch(`/api/detalhes?id=${encodeURIComponent(id)}`);
    d = await r.json();
    if (!r.ok) throw new Error(d.erro || "Falha ao buscar no PNCP.");
  } catch (e) {
    alvo.querySelector("#sec-docs").innerHTML = `<h3>Documentos do edital</h3><div class="aviso">${esc(e.message)} Use o botão "Ver no PNCP (oficial)".</div>`;
    return;
  }
  const docs = d.arquivos.length
    ? `<div class="docs">${d.arquivos.map((a) => `<a class="doc" href="${esc(a.url)}" target="_blank" rel="noopener">${ICDOC}<span>${esc(a.titulo)}${a.tipo && a.tipo !== a.titulo ? " · " + esc(a.tipo) : ""}</span><small>${esc(dataSo(a.data))}</small></a>`).join("")}</div>`
    : `<p class="loc">Nenhum documento publicado no PNCP. Veja no portal da licitação.</p>`;
  alvo.querySelector("#sec-docs").innerHTML = `<h3>Documentos do edital (${d.arquivos.length})</h3>` +
    (d.erros && d.erros.length ? `<div class="aviso">O PNCP não respondeu para ${esc(d.erros.join("; "))}.</div>` : "") + docs;
  if (d.itens.length) {
    const sec = alvo.querySelector("#sec-itens"), tot = d.itens.reduce((s, i) => s + (i.vt || 0), 0);
    sec.hidden = false;
    sec.innerHTML = `<h3>Itens da contratação (${d.itens.length})</h3><div class="tw"><table><thead><tr><th>Item</th><th style="text-align:left">Descrição</th><th>Un.</th><th>Qtd</th><th>Valor unit.</th><th>Valor total</th></tr></thead><tbody>` +
      d.itens.map((i) => `<tr><td>${esc(i.n)}</td><td class="d">${esc(i.descricao)}${i.tipo ? `<span class="tipo">${esc(i.tipo)}</span>` : ""}${i.me && !/sem benef|nao se aplica|não se aplica/i.test(i.me) ? `<small>${esc(i.me)}</small>` : ""}</td><td>${esc(i.un)}</td><td>${qtd(i.qtd)}</td><td>${i.vu != null ? brl(i.vu) : "—"}</td><td>${i.vt != null ? brl(i.vt) : "—"}</td></tr>`).join("") +
      `</tbody>${tot ? `<tfoot><tr><td colspan="5">Total estimado dos itens</td><td>${brl(tot)}</td></tr></tfoot>` : ""}</table></div>`;
  }
}

const qs = new URLSearchParams(location.search);
if (location.pathname === "/licitacao" && qs.get("id")) {
  paginaLicitacao(qs.get("id"));
} else {
desenhar();
if (qs.get("uf")) $("#uf").value = qs.get("uf").toUpperCase();
if (qs.get("q")) $("#q").value = qs.get("q");
buscar();
}
</script>
</body>
</html>
"""
PAGINA = PAGINA.replace("__CONFIG_PNCP__", json.dumps({
    "categorias": CATEGORIAS, "servico": SERVICO, "produto": PRODUTO, "assistencia": ASSISTENCIA,
    "portais": [[n, list(a), list(b)] for n, a, b in PORTAIS_CONHECIDOS], "modalidades": MODALIDADES,
}, ensure_ascii=False))


# ================================================================ senha de acesso
# Usuário e senha ficam no Render (Environment → SITE_USUARIO e SITE_SENHA), iguais nos dois serviços
# (busca-licitacoes e consulta-orgaos).
# Entrando uma vez em sigalicita.com.br, o navegador fica liberado também em score.sigalicita.com.br (por 30 dias).
# Sem SITE_SENHA configurada, o site continua aberto como antes.
import hashlib
import hmac

COOKIE_ACESSO = "sl_acesso"
ABERTOS_SEM_SENHA = ("/entrar", "/sair", "/tarefa/email")


def _senha_site():
    return os.environ.get("SITE_SENHA", "").strip()


def _usuario_site():
    return os.environ.get("SITE_USUARIO", "").strip().lower()


LOGO_ENTRAR = "data:image/png;base64,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"


def _token_site():
    chave = (_usuario_site() + "\n" + _senha_site()).encode("utf-8")
    return hmac.new(chave, b"sigalicita-acesso-v1", hashlib.sha256).hexdigest()


def _host(handler):
    return (handler.headers.get("Host") or "").split(":")[0].lower()


def _logado(handler):
    if not _senha_site():
        return True
    for parte in (handler.headers.get("Cookie") or "").split(";"):
        nome, _, valor = parte.strip().partition("=")
        if nome == COOKIE_ACESSO and hmac.compare_digest(valor, _token_site()):
            return True
    return False


def _cookie_acesso(handler, valor, segundos):
    host = _host(handler)
    extra = "; Domain=.sigalicita.com.br" if host.endswith("sigalicita.com.br") else ""
    if host.endswith("sigalicita.com.br") or host.endswith(".onrender.com"):
        extra += "; Secure"
    return f"{COOKIE_ACESSO}={valor}; Path=/; Max-Age={segundos}; HttpOnly; SameSite=Lax{extra}"


def _volta_segura(v):
    v = str(v or "/")
    return v if v.startswith("/") and not v.startswith("//") and "\n" not in v and "\r" not in v else "/"


def _pagina_entrar(volta="/", erro=False):
    from html import escape as e
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>SigaLicita · Entrar</title>
<meta name="robots" content="noindex">
<style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0d1422;color:#e8eef8;font:16px system-ui,-apple-system,"Segoe UI",sans-serif;padding:16px;box-sizing:border-box}}
.box{{width:100%;max-width:360px;background:#151f32;border:1px solid #26344f;border-radius:18px;padding:28px 24px;box-shadow:0 10px 30px rgba(0,0,0,.35)}}
.logo{{display:block;width:100%;max-width:260px;height:auto;margin:0 auto 14px}}
p{{margin:0 0 20px;color:#94a3bb;font-size:14px;text-align:center}}
label{{display:block;font-size:13px;font-weight:600;color:#94a3bb;margin-bottom:6px}}
input{{width:100%;box-sizing:border-box;font:inherit;padding:12px 14px;border-radius:10px;border:1px solid #31415f;background:#0d1422;color:#e8eef8}}
input:focus{{outline:2px solid #38bdf8;border-color:#38bdf8}}
input:-webkit-autofill,input:-webkit-autofill:hover,input:-webkit-autofill:focus{{-webkit-box-shadow:0 0 0 1000px #0d1422 inset!important;box-shadow:0 0 0 1000px #0d1422 inset!important;-webkit-text-fill-color:#e8eef8!important;caret-color:#e8eef8;transition:background-color 9999s ease-out 0s}}
html{{color-scheme:dark}}
.sw{{position:relative}} .sw input{{padding-right:48px}}
.olho{{position:absolute;right:6px;top:50%;transform:translateY(-50%);width:38px!important;height:38px;margin:0!important;padding:0!important;border-radius:8px!important;background:transparent!important;font-size:18px;line-height:1;cursor:pointer}}
.olho:hover{{background:#26344f!important}}
button{{width:100%;margin-top:14px;font:inherit;font-weight:800;padding:12px;border:0;border-radius:10px;background:#38bdf8;color:#06243a;cursor:pointer}}
.erro{{background:#371a1c;border:1px solid #7a2f2f;color:#f6766f;border-radius:10px;padding:10px 12px;font-size:14px;margin-bottom:14px}}
</style></head><body><form class="box" method="post" action="/entrar">
<img class="logo" src="{LOGO_ENTRAR}" alt="SigaLicita"><p>Área restrita. Entre com o seu e-mail e senha.</p>
{'<div class="erro">E-mail ou senha incorretos. Tente de novo.</div>' if erro else ''}
<label for="u">E-mail</label><input id="u" name="usuario" type="email" inputmode="email" autocomplete="username" autocapitalize="none" placeholder="seu@email.com" autofocus required>
<label for="s" style="margin-top:12px">Senha</label>
<div class="sw"><input id="s" name="senha" type="password" autocomplete="current-password" required>
<button type="button" class="olho" id="olho" aria-label="Mostrar senha" title="Mostrar senha" onclick="var s=document.getElementById('s'),v=s.type==='password';s.type=v?'text':'password';this.setAttribute('aria-label',v?'Esconder senha':'Mostrar senha');this.title=this.getAttribute('aria-label');this.textContent=v?'🙈':'👁';">👁</button></div>
<input type="hidden" name="volta" value="{e(_volta_segura(volta))}">
<button type="submit">Entrar</button></form></body></html>"""


def _barrar_sem_senha(handler, caminho):
    """Devolve True se respondeu (pediu a senha); False se pode seguir."""
    if caminho in ABERTOS_SEM_SENHA or _logado(handler):
        return False
    if caminho.startswith("/api/"):
        corpo = b'{"erro": "acesso restrito: entre com a senha"}'
        handler.send_response(401)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
    else:
        corpo = _pagina_entrar(handler.path).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(corpo)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(corpo)
    return True


def _redirecionar(handler, destino, cookie=None):
    handler.send_response(303)
    handler.send_header("Location", destino)
    if cookie:
        handler.send_header("Set-Cookie", cookie)
    handler.send_header("Content-Length", "0")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()


def _tratar_entrar_sair(handler):
    """POST /entrar (confere a senha) e GET /sair. Devolve True se tratou."""
    caminho = urllib.parse.urlparse(handler.path).path
    if handler.command == "GET" and caminho == "/sair":
        _redirecionar(handler, "/", _cookie_acesso(handler, "", 0))
        return True
    if handler.command == "GET" and caminho == "/entrar":
        _redirecionar(handler, "/")
        return True
    if handler.command == "POST" and caminho == "/entrar":
        tam = min(int(handler.headers.get("Content-Length") or 0), 4000)
        dados = urllib.parse.parse_qs(handler.rfile.read(tam).decode("utf-8", "replace"))
        senha = (dados.get("senha") or [""])[0]
        usuario = (dados.get("usuario") or [""])[0].strip().lower()
        volta = _volta_segura((dados.get("volta") or ["/"])[0])
        if _senha_site() and hmac.compare_digest(senha.strip(), _senha_site()) and (not _usuario_site() or hmac.compare_digest(usuario, _usuario_site())):
            _redirecionar(handler, volta, _cookie_acesso(handler, _token_site(), 30 * 86400))
        else:
            time.sleep(1.5)  # deixa tentativa por tentativa mais lenta
            corpo = _pagina_entrar(volta, erro=True).encode("utf-8")
            handler.send_response(200)
            handler.send_header("Content-Type", "text/html; charset=utf-8")
            handler.send_header("Content-Length", str(len(corpo)))
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
            handler.wfile.write(corpo)
        return True
    return False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("  " + (fmt % args) + "\n")

    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        zip_ok = len(data) > 2000 and "gzip" in (self.headers.get("Accept-Encoding") or "")
        if zip_ok:
            import gzip
            data = gzip.compress(data, 6)
        self.send_response(code)
        if zip_ok:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def do_POST(self):
        if not _tratar_entrar_sair(self):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        # endereço antigo (.onrender.com): leva para o endereço novo (o e-mail diário continua no antigo)
        if _host(self).endswith(".onrender.com") and u.path in ("/", "/index.html", "/licitacao"):
            return _redirecionar(self, "https://sigalicita.com.br" + self.path)
        if _tratar_entrar_sair(self) or _barrar_sem_senha(self, u.path):
            return
        if u.path == "/diagnostico":
            try:
                txt = diagnostico()
            except Exception as e:
                txt = "ERRO: " + erro_txt(e)
            return self._send(200, txt, "text/plain; charset=utf-8")
        if u.path == "/tarefa/email":
            q = urllib.parse.parse_qs(u.query)
            segredo = os.environ.get("ALERTA_CHAVE", "")
            if not segredo or q.get("chave", [""])[0] != segredo:
                return self._send(403, "chave errada ou ALERTA_CHAVE não configurada", "text/plain; charset=utf-8")
            if q.get("ver"):
                return self._send(200, "último envio: " + (_envio["ultimo"] or "nenhum desde que o site ligou")
                                  + (" · enviando agora..." if _envio["rodando"] else ""), "text/plain; charset=utf-8")
            # só um e-mail por dia: o cron-job chama algumas vezes (caso o site esteja dormindo); &forcar=1 manda de novo
            if _envio["dia"] == time.strftime("%Y-%m-%d") and not q.get("forcar"):
                return self._send(200, "ok: o e-mail de hoje já foi enviado (" + _envio["ultimo"] + "). Para mandar de novo, coloque &forcar=1 no fim do link.", "text/plain; charset=utf-8")
            if not _envio["rodando"]:
                threading.Thread(target=tarefa_email, daemon=True).start()
            return self._send(200, "ok: o resumo está sendo preparado e chega no e-mail em 1 ou 2 minutos", "text/plain; charset=utf-8")
        if u.path == "/api/nota":
            q = urllib.parse.parse_qs(u.query)
            try:
                res = nota_orgao(q.get("cnpj", [""])[0])
                return self._send(200, json.dumps(res, ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                return self._send(502, json.dumps({"erro": erro_txt(e)}, ensure_ascii=False), "application/json; charset=utf-8")
        if u.path == "/api/licitacao":
            q = urllib.parse.parse_qs(u.query)
            try:
                res = licitacao(q.get("id", [""])[0])
                return self._send(200, json.dumps(res, ensure_ascii=False), "application/json; charset=utf-8")
            except ValueError as e:
                return self._send(404, json.dumps({"erro": str(e)}, ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                return self._send(502, json.dumps({"erro": erro_txt(e)}, ensure_ascii=False), "application/json; charset=utf-8")
        if u.path == "/api/detalhes":
            q = urllib.parse.parse_qs(u.query)
            try:
                res = detalhes(q.get("id", [""])[0])
                return self._send(200, json.dumps(res, ensure_ascii=False), "application/json; charset=utf-8")
            except ValueError as e:
                return self._send(400, json.dumps({"erro": str(e)}, ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                return self._send(502, json.dumps({"erro": erro_txt(e)}, ensure_ascii=False), "application/json; charset=utf-8")
        if u.path == "/api/busca":
            q = urllib.parse.parse_qs(u.query)
            try:
                mods = [int(x) for x in re.findall(r"\d+", q.get("mods", [""])[0])]
                res = buscar(q.get("uf", [""])[0], mods)
                return self._send(200, json.dumps(res, ensure_ascii=False), "application/json; charset=utf-8")
            except ValueError as e:
                return self._send(400, json.dumps({"erro": str(e)}, ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                return self._send(502, json.dumps({"erro": erro_txt(e)}, ensure_ascii=False), "application/json; charset=utf-8")
        if u.path == "/diagnostico3":
            return self._send(200, diagnostico_velocidade(), "text/plain; charset=utf-8")
        if u.path == "/diagnostico2":
            try:
                txt = diagnostico_busca()
            except Exception as e:
                txt = "ERRO: " + erro_txt(e)
            return self._send(200, txt, "text/plain; charset=utf-8")
        if u.path in ("/", "/index.html", "/licitacao"):
            pagina = PAGINA
            if _senha_site():
                from html import escape
                nome = os.environ.get("SITE_NOME", "").strip() or "Rute Silva"
                pagina = pagina.replace("<!--CONTA-->", f'<div class="conta" title="Você está conectada">👤 <b>{escape(nome)}</b>'
                                        '<a href="/sair" title="Sair do SigaLicita (e do Score)">Sair</a></div>', 1)
            return self._send(200, pagina, "text/html; charset=utf-8")
        self._send(404, "não encontrado", "text/plain; charset=utf-8")


def main():
    na_nuvem = "PORT" in os.environ
    srv = ThreadingHTTPServer(("0.0.0.0" if na_nuvem else "127.0.0.1", PORT), Handler)
    print(f"Busca Licitações rodando na porta {PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
