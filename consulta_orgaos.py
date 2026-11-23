#!/usr/bin/env python3
"""
Consulta Pagador Público
------------------------
Digite o CNPJ de um órgão público e veja o histórico de pagamentos
(restos a pagar), a nota CAPAG do Tesouro e a regularidade dos relatórios fiscais.

Como usar:
    python3 consulta_orgaos.py
O navegador abre em http://localhost:8000

Não precisa instalar nada além do Python 3.8+.
Chave do Portal da Transparência (opcional, só para órgãos federais):
    defina a variável PORTAL_TRANSPARENCIA_CHAVE ou digite a chave na página.

Fontes de dados (todas públicas):
    - BrasilAPI / Minha Receita: cadastro do CNPJ na Receita Federal
    - SICONFI (Tesouro Nacional): DCA Anexos I-F e I-G (execução de restos a pagar),
      extrato de entregas de relatórios
    - Tesouro Transparente: CAPAG (capacidade de pagamento) de estados e municípios
    - Portal da Transparência (com chave): despesas por órgão federal
"""
import csv
import io
import json
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8000"))
SICONFI = "https://apidatalake.tesouro.gov.br/ords/siconfi/tt"
PORTAL = "https://api.portaldatransparencia.gov.br/api-de-dados"
CKAN = "https://www.tesourotransparente.gov.br/ckan/api/3/action/package_show?id="
PNCP = "https://pncp.gov.br/api/consulta/v1"
UA = "consulta-pagador-publico/1.0"

_cache = {}
_lock = threading.Lock()


# ----------------------------------------------------------------- utilidades
def http_get(url, headers=None, timeout=45, raw=False):
    h = {"User-Agent": UA, "Accept": "application/json, text/csv, */*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if raw:
        return data
    if not data.strip():
        return None
    return json.loads(data.decode("utf-8"))


def cached(key, ttl, fn):
    now = time.time()
    with _lock:
        if key in _cache and now - _cache[key][0] < ttl:
            return _cache[key][1]
    v = fn()
    with _lock:
        _cache[key] = (now, v)
    return v


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.lower()).strip()


def digitos(s):
    return re.sub(r"\D", "", str(s or ""))


def to_num(v):
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("R$", "").strip()
    if re.search(r",\d{1,2}$", s):
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def cnpj_valido(c):
    if len(c) != 14 or c == c[0] * 14:
        return False

    def dv(base, pesos):
        r = sum(int(a) * b for a, b in zip(base, pesos)) % 11
        return "0" if r < 2 else str(11 - r)

    p1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    d1 = dv(c[:12], p1)
    d2 = dv(c[:12] + d1, [6] + p1)
    return c[12:] == d1 + d2


def erro_txt(e):
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"sem conexão ({e.reason})"
    return str(e)[:200]


# ----------------------------------------------------------------- CNPJ
def endereco(d):
    tipo, logr = str(d.get("descricao_tipo_de_logradouro") or ""), str(d.get("logradouro") or "")
    if tipo and norm(logr).startswith(norm(tipo)):
        tipo = ""
    rua = " ".join(x for x in (tipo, logr) if x)
    partes = [rua + (", " + str(d.get("numero")) if d.get("numero") else ""), str(d.get("bairro") or "")]
    return " - ".join(x for x in partes if x.strip())


def consulta_cnpj(c):
    erros = []
    for url in (f"https://brasilapi.com.br/api/cnpj/v1/{c}", f"https://minhareceita.org/{c}"):
        try:
            d = http_get(url)
            return {
                "cnpj": c,
                "razao_social": d.get("razao_social") or d.get("nome") or "",
                "nome_fantasia": d.get("nome_fantasia") or "",
                "natureza_juridica": d.get("natureza_juridica") or "",
                "codigo_natureza": digitos(d.get("codigo_natureza_juridica")),
                "municipio": d.get("municipio") or "",
                "uf": d.get("uf") or "",
                "codigo_ibge": digitos(d.get("codigo_municipio_ibge")),
                "situacao": d.get("descricao_situacao_cadastral") or "",
                "abertura": d.get("data_inicio_atividade") or "",
                "endereco": endereco(d),
                "telefone": d.get("ddd_telefone_1") or "",
                "email": d.get("email") or "",
                "efr": d.get("ente_federativo_responsavel") or "",
                "matriz": str(d.get("identificador_matriz_filial") or "1") == "1",
                "fonte": url.split("/")[2],
            }
        except Exception as e:  # tenta a próxima fonte
            erros.append(f"{url.split('/')[2]}: {erro_txt(e)}")
    raise RuntimeError("Não consegui consultar o CNPJ na Receita (" + "; ".join(erros) + ").")


def esfera_do_orgao(info):
    cod, txt = info["codigo_natureza"], norm(info["natureza_juridica"])
    if cod == "1341" or txt == "uniao":
        return "federal"
    if cod == "1236" or "estado ou distrito federal" in txt:
        return "estadual"
    if cod == "1244" or txt == "municipio":
        return "municipal"
    if "federal" in txt and "distrito federal" not in txt:
        return "federal"
    if "estadual" in txt or "distrito federal" in txt:
        return "estadual"
    if "municipal" in txt:
        return "municipal"
    if cod.startswith("1"):
        return "publico"
    if cod in ESTATAIS:
        return "estatal"
    return "privado"


# Empresa Pública, Sociedade de Economia Mista e equivalentes
ESTATAIS = {"2011", "2038", "2208"}
UFS = {"AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG", "PA", "PB", "PR",
       "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO"}


def cnpj_matriz(c):
    base = c[:8] + "0001"

    def dv(b, pesos):
        r = sum(int(a) * p for a, p in zip(b, pesos)) % 11
        return "0" if r < 2 else str(11 - r)

    p1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    d1 = dv(base, p1)
    return base + d1 + dv(base + d1, [6] + p1)


def esfera_pelo_efr(efr):
    """Ente federativo responsável informado na Receita: 'UNIÃO', 'PE' ou 'RECIFE - PE'."""
    e = norm(efr)
    if not e:
        return None, None
    if e in ("uniao", "brasil", "uniao federal"):
        return "federal", {}
    if e.upper() in UFS:
        return "estadual", {"uf": e.upper()}
    m = re.match(r"^(.+?)\s*-\s*([a-z]{2})$", e)
    if m and m.group(2).upper() in UFS:
        return "municipal", {"municipio": m.group(1), "uf": m.group(2).upper(), "codigo_ibge": ""}
    return None, None


def esfera_da_estatal(info):
    """Descobre se a empresa estatal é federal, estadual ou municipal."""
    esf, base = esfera_pelo_efr(info.get("efr"))
    if esf:
        return esf, base, "pelo ente responsável informado na Receita Federal"
    sede = info
    if not info.get("matriz"):
        try:
            sede = consulta_cnpj(cnpj_matriz(info["cnpj"]))
            esf, base = esfera_pelo_efr(sede.get("efr"))
            if esf:
                return esf, base, "pelo ente responsável informado na Receita Federal"
        except Exception:
            sede = info
    nome = norm(info["razao_social"])
    uf = sede.get("uf", "")
    if "distrito federal" in nome:
        return "estadual", {"uf": "DF"}, "pelo nome da empresa"
    if "municipal" in nome or (sede.get("municipio") and norm(sede["municipio"]) in nome and uf != "DF"):
        return "municipal", {"municipio": sede.get("municipio", ""), "uf": uf,
                             "codigo_ibge": sede.get("codigo_ibge", "")}, "pelo nome e pela sede da empresa"
    if uf == "DF" or "federal" in nome or "brasil" in nome:
        return "federal", {}, "pela sede da empresa em Brasília"
    return "estadual", {"uf": uf}, f"pela sede da empresa ({uf})"


# ----------------------------------------------------------------- SICONFI
def siconfi(path, params):
    items, offset = [], 0
    while True:
        q = dict(params)
        if offset:
            q["offset"] = offset
        d = http_get(f"{SICONFI}/{path}?{urllib.parse.urlencode(q)}")
        it = d.get("items", [])
        items += it
        if not d.get("hasMore") or not it or offset > 60000:
            break
        offset += len(it)
    return items


def lista_entes():
    return cached("entes", 86400, lambda: siconfi("entes", {}))


def achar_ente(info, esf):
    entes = lista_entes()
    for e in entes:
        if digitos(e.get("cnpj")) == info["cnpj"]:
            return e, "CNPJ do próprio ente"
    if esf in ("municipal", "publico") and info["codigo_ibge"]:
        for e in entes:
            if str(e.get("cod_ibge")) == info["codigo_ibge"]:
                return e, "contas consolidadas do município onde o órgão está"
    if esf == "municipal" and info["municipio"]:
        for e in entes:
            if (e.get("esfera") == "M" and e.get("uf") == info["uf"]
                    and norm(e.get("ente")) == norm(info["municipio"])):
                return e, "contas consolidadas do município onde o órgão está"
    if esf == "estadual" and info["uf"]:
        for e in entes:
            if (e.get("esfera") in ("E", "D") and e.get("uf") == info["uf"]
                    and len(str(e.get("cod_ibge"))) <= 2):
                return e, "contas consolidadas do estado"
    return None, None


def resumo_rp(items):
    """Le o DCA Anexo I-F (linha Total Despesas) separando processados e nao processados."""
    linhas = {}
    for it in items:
        conta = norm(it.get("conta") or it.get("cod_conta") or "?")
        col = norm(it.get("coluna"))
        v = to_num(it.get("valor"))
        tipo = "np" if "nao processad" in col else "p" if "processad" in col else None
        if not tipo:
            continue
        l = linhas.setdefault(conta, {"p": {"inscritos": 0.0, "pagos": 0.0, "cancelados": 0.0, "liquidados": 0.0},
                                      "np": {"inscritos": 0.0, "pagos": 0.0, "cancelados": 0.0, "liquidados": 0.0}})
        d = l[tipo]
        if "inscrit" in col:
            d["inscritos"] += v
        elif "cancelad" in col:
            d["cancelados"] += v
        elif "liquidad" in col:
            d["liquidados"] += v
        elif "pago" in col:
            d["pagos"] += v
    if not linhas:
        return None
    total = next((v for k, v in linhas.items() if k.startswith("total")), None)
    if total is None:
        total = max(linhas.values(), key=lambda l: l["p"]["inscritos"] + l["np"]["inscritos"])
    return total


def dca_ano(id_ente, ano):
    item = {"ano": ano}
    try:
        r = resumo_rp(cached(f"dcaF{id_ente}{ano}", 21600, lambda: siconfi(
            "dca", {"an_exercicio": ano, "no_anexo": "DCA-Anexo I-F", "id_ente": id_ente})))
    except Exception as e:
        item["erro"] = erro_txt(e)
        return item
    if not r:
        return item
    p, np_ = r["p"], r["np"]
    if p["inscritos"] > 0:
        saldo = max(p["inscritos"] - p["pagos"] - p["cancelados"], 0)
        item.update(inscritos=p["inscritos"], pagos=p["pagos"], cancelados=p["cancelados"], saldo=saldo,
                    pct_pago=p["pagos"] / p["inscritos"] * 100,
                    pct_cancelado=p["cancelados"] / p["inscritos"] * 100)
    if np_["inscritos"] > 0:
        item["np"] = dict(np_)
        if np_["liquidados"] > 0:
            item["np"]["pct_pago_liquidado"] = np_["pagos"] / np_["liquidados"] * 100
    return item


def _rreo_fornecedores(items):
    """Soma liquidado e pago ATÉ o bimestre das despesas com fornecedores
    (Outras Despesas Correntes + Investimentos), sem salários e juros."""
    contas = {}
    for it in items:
        conta, col = norm(it.get("conta")), norm(it.get("coluna"))
        if not (conta.startswith("outras despesas correntes") or conta.startswith("investimentos")):
            continue
        if "intra" in conta:
            continue
        if "ate o bimestre" not in col and "ate o periodo" not in col:
            continue
        chave = "odc" if conta.startswith("outras") else "inv"
        c = contas.setdefault(chave, {"liq": 0.0, "pago": 0.0})
        v = to_num(it.get("valor"))
        if "liquidad" in col and "inscrit" not in col:
            c["liq"] = max(c["liq"], v)
        elif "paga" in col and "inscrit" not in col:
            c["pago"] = max(c["pago"], v)
    liq = sum(c["liq"] for c in contas.values())
    pago = sum(c["pago"] for c in contas.values())
    return liq, pago


def prazo_ente(id_ente):
    """Prazo médio estimado de pagamento a fornecedores, pelo último RREO entregue."""
    hoje = date.today()
    tentativas = [(hoje.year, n) for n in range(6, 0, -1)] + [(hoje.year - 1, 6)]
    for ano, per in tentativas:
        if ano == hoje.year and per * 2 > hoje.month + 1:
            continue  # bimestre ainda não terminou
        try:
            its = cached(f"rreo1{id_ente}{ano}{per}", 21600, lambda: siconfi("rreo", {
                "an_exercicio": ano, "nr_periodo": per, "co_tipo_demonstrativo": "RREO",
                "no_anexo": "RREO-Anexo 01", "id_ente": id_ente}))
        except Exception:
            continue
        if not its:
            continue
        liq, pago = _rreo_fornecedores(its)
        if liq <= 0 or pago <= 0:
            continue
        dias = round(per * 365 / 6)
        return {"dias": max(0, round((liq - pago) / (pago / dias))), "liquidado": liq, "pago": pago,
                "fila": max(liq - pago, 0), "ano": ano, "periodo": f"{per}º bimestre de {ano}",
                "fonte": "RREO (relatório bimestral do Tesouro), só despesas com fornecedores"}
    return None


def prazo_federal(fed=None):
    """Órgão federal: o Portal só tem o total com salários, que esconde a demora com fornecedores.
    Por isso usa o RREO da União (só fornecedores): é a média do Governo Federal."""
    pz = prazo_ente(1)
    if pz:
        pz["fonte"] = "RREO da União, só despesas com fornecedores (média do Governo Federal, não do órgão)"
        pz["periodo"] = pz["periodo"] + ", média do Governo Federal"
    return pz


def aplicar_prazo(av, pz):
    """Coloca o prazo na avaliação: etiqueta, motivo e um pequeno ajuste na nota."""
    if not av or not pz or av.get("score") is None:
        return
    d = pz["dias"]
    av["prazo"] = pz
    av["motivos"].append(f"Prazo médio estimado de pagamento: cerca de {d} dias ({pz['periodo']}).")
    if d > 60:
        av["alertas"] = (av.get("alertas") or []) + [{"texto": "Atrasa muito", "tipo": "ruim"}]
        av["score"] = max(0, av["score"] - 10)
    elif d > 30:
        av["alertas"] = (av.get("alertas") or []) + [{"texto": "Costuma atrasar", "tipo": "atencao"}]
        av["score"] = max(0, av["score"] - 5)
    else:
        av["alertas"] = (av.get("alertas") or []) + [{"texto": "Paga em até 30 dias", "tipo": "bom"}]
    av["nivel"] = nivel_do_score(av["score"])
    av["titulo"] = TITULOS[av["nivel"]]


def entregas(id_ente, ano):
    try:
        its = siconfi("extrato_entregas", {"id_ente": id_ente, "an_referencia": ano})
    except Exception as e:
        return {"ano": ano, "erro": erro_txt(e)}
    lista = []
    for i in its:
        lista.append({
            "relatorio": i.get("entregavel") or i.get("tipo_relatorio") or "",
            "periodo": i.get("periodo"),
            "status": {"HO": "Homologado", "RE": "Retificado", "RT": "Retificado", "EN": "Enviado"}.get(i.get("status_relatorio"), i.get("status_relatorio") or ""),
            "data": (i.get("data_status") or "")[:10],
        })
    lista.sort(key=lambda x: x["data"], reverse=True)
    return {"ano": ano, "total": len(lista), "itens": lista[:40]}


# ----------------------------------------------------------------- CAPAG
def ler_xlsx(raw):
    z = zipfile.ZipFile(io.BytesIO(raw))
    m = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    ss = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall(m + "si"):
            ss.append("".join(t.text or "" for t in si.iter(m + "t")))
    folhas = sorted((n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)),
                    key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
    root = ET.fromstring(z.read(folhas[0]))
    rows = []
    for row in root.iter(m + "row"):
        vals = {}
        for c in row.findall(m + "c"):
            ref = re.match(r"[A-Z]+", c.get("r", "A"))
            idx = 0
            for ch in ref.group(0):
                idx = idx * 26 + ord(ch) - 64
            t, v = c.get("t"), c.find(m + "v")
            if t == "s" and v is not None:
                val = ss[int(v.text)]
            elif t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(m + "t"))
            else:
                val = v.text if v is not None else ""
            vals[idx - 1] = val
        if vals:
            rows.append([vals.get(i, "") for i in range(max(vals) + 1)])
    return rows


def ler_tabela(raw):
    if raw[:2] == b"PK":
        return ler_xlsx(raw)
    for enc in ("utf-8-sig", "latin-1"):
        try:
            txt = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    delim = ";" if txt.count(";") > txt.count(",") else ","
    return list(csv.reader(io.StringIO(txt), delimiter=delim))


def capag_tabela(tipo):
    def f():
        pk = http_get(CKAN + f"capag-{tipo}")
        res = pk["result"]["resources"]
        res = sorted(res, key=lambda r: (r.get("last_modified") or r.get("created") or ""), reverse=True)
        for r in res:
            fmt, url = (r.get("format") or "").lower(), r.get("url", "")
            if fmt in ("csv", "xlsx") or url.lower().endswith((".csv", ".xlsx")):
                try:
                    rows = ler_tabela(http_get(url, raw=True, timeout=120))
                    if len(rows) > 5:
                        return {"nome": r.get("name") or "", "url": url, "rows": rows}
                except Exception:
                    continue
        return None
    return cached("capag" + tipo, 86400, f)


def capag_do_ente(ente):
    cod = str(ente.get("cod_ibge"))
    tipo = "municipios" if len(cod) > 2 else "estados"
    t = capag_tabela(tipo)
    if not t:
        return None
    rows = t["rows"]
    hi = None
    for i, r in enumerate(rows[:30]):
        if any(norm(x) == "capag" for x in r):
            hi = i
            break
    if hi is None:
        return None
    hdr = [norm(x) for x in rows[hi]]
    orig = [str(x).strip() for x in rows[hi]]
    col_nota = hdr.index("capag")
    col_ibge = next((i for i, h in enumerate(hdr) if "ibge" in h or h.startswith("codigo") or h.startswith("cod")), None)
    col_uf = next((i for i, h in enumerate(hdr) if h in ("uf", "sigla", "sigla uf", "sigla_uf")), None)
    col_nome = next((i for i, h in enumerate(hdr) if "nome" in h or h in ("ente", "municipio", "estado")), None)

    def bate(r):
        if col_ibge is not None and col_ibge < len(r):
            v = digitos(str(r[col_ibge]).split(".")[0])
            if v and (v == cod or (len(cod) == 7 and v == cod[:6])):
                return True
        if tipo == "estados" and col_uf is not None and col_uf < len(r):
            if str(r[col_uf]).strip().upper() == ente.get("uf"):
                return True
        if col_nome is not None and col_nome < len(r) and norm(r[col_nome]) == norm(ente.get("ente")):
            return col_uf is None or str(r[col_uf]).strip().upper() == ente.get("uf")
        return False

    nomes = {"nota 1": "Endividamento", "nota 2": "Poupança corrente", "nota 3": "Liquidez",
             "observacao": "Observação", "origem da nota final": "Origem da nota"}
    for r in rows[hi + 1:]:
        if bate(r):
            det = {}
            for i, h in enumerate(hdr):
                if h in nomes and i < len(r) and str(r[i]).strip() and h not in [norm(k) for k in det]:
                    val = str(r[i]).strip()
                    if not (h == "observacao" and norm(val) == "nenhum"):
                        det[nomes[h]] = val
            return {"nota": str(r[col_nota]).strip(), "arquivo": t["nome"], "url": t["url"], "detalhes": det}
    return {"nota": "", "arquivo": t["nome"], "url": t["url"], "detalhes": {},
            "obs": "Ente não encontrado na planilha CAPAG mais recente."}


# ----------------------------------------------------------------- Federal (com chave)
PARADAS = {"de", "da", "do", "das", "dos", "e", "em", "para", "a", "o"}


def palavras(txt):
    return [w for w in re.findall(r"[a-z0-9]+", norm(txt)) if w not in PARADAS and len(w) > 1]


def parecido(a, b):
    """Semelhança entre nomes, tolerando abreviações do SIAFI (DESENV., CIA, S.FRANCISCO)."""
    A, B = palavras(a), palavras(b)
    if not A or not B:
        return 0.0
    sin = {"cia": "companhia", "univ": "universidade", "fed": "federal", "inst": "instituto", "min": "ministerio"}
    A = [sin.get(w, w) for w in A]
    B = [sin.get(w, w) for w in B]

    def casa(x, Y):
        return any(x == y or (len(x) >= 3 and len(y) >= 3 and (x.startswith(y) or y.startswith(x))) for y in Y)

    return (sum(casa(x, B) for x in A) / len(A) + sum(casa(y, A) for y in B) / len(B)) / 2


_orgaos = {"lista": None, "carregando": False, "paginas": 0, "erro": ""}
_orgaos_lock = threading.Lock()


def _baixar_orgaos(chave):
    H = {"chave-api-dados": chave}
    todos, pagina = [], 1
    try:
        while pagina <= 600:
            r = None
            for tentativa in range(4):
                try:
                    r = http_get(f"{PORTAL}/orgaos-siafi?pagina={pagina}", H)
                    break
                except urllib.error.HTTPError as e:
                    if e.code in (401, 403):
                        raise
                    time.sleep(5 * (tentativa + 1))  # limite de requisições: espera e tenta de novo
            if not r:
                break
            todos += [c for c in r if c.get("codigo") and "codigo invalido" not in norm(c.get("descricao"))]
            _orgaos["paginas"] = pagina
            pagina += 1
            time.sleep(0.75)
        _orgaos["lista"] = todos
        _orgaos["erro"] = ""
    except Exception as e:
        _orgaos["erro"] = erro_txt(e)
    finally:
        _orgaos["carregando"] = False


def iniciar_lista_orgaos(chave):
    with _orgaos_lock:
        if _orgaos["lista"] is not None or _orgaos["carregando"]:
            return
        _orgaos["carregando"] = True
        _orgaos["paginas"] = 0
    threading.Thread(target=_baixar_orgaos, args=(chave,), daemon=True).start()


def lista_orgaos_siafi(chave, espera=25):
    iniciar_lista_orgaos(chave)
    fim = time.time() + espera
    while _orgaos["lista"] is None and _orgaos["carregando"] and time.time() < fim:
        time.sleep(1)
    return _orgaos["lista"]


def limpar_nome_siafi(d):
    return re.sub(r"\s*-\s*unidades com vinculo direto\s*$", "", norm(d))


def achar_orgao_siafi(info, chave):
    lista = lista_orgaos_siafi(chave)
    if lista is None:
        return "carregando", []
    nome = info["razao_social"]
    sigla = norm(info.get("nome_fantasia"))

    def nota(c):
        d = limpar_nome_siafi(c.get("descricao"))
        return parecido(nome, d) + (0.3 if sigla and len(sigla) >= 3 and re.search(r"\b" + re.escape(sigla) + r"\b", d) else 0)

    ordem = sorted(lista, key=nota, reverse=True)[:6]
    if not ordem:
        return None, []
    return (ordem[0] if nota(ordem[0]) >= 0.55 else None), ordem


def federal(info, chave):
    H = {"chave-api-dados": chave}
    nome = info["razao_social"]
    alvo, candidatos = achar_orgao_siafi(info, chave)
    if alvo == "carregando":
        if _orgaos["erro"]:
            return {"erro": f"Não consegui baixar a lista de órgãos do Portal da Transparência ({_orgaos['erro']})."}
        return {"erro": "Estou baixando a lista de órgãos federais do Portal da Transparência pela primeira vez "
                        f"(página {_orgaos['paginas']}). Isso leva de 1 a 3 minutos. Consulte de novo daqui a pouco."}
    if not alvo:
        parecidos = "; ".join(f"{c.get('codigo')} {c.get('descricao')}" for c in candidatos[:4])
        return {"erro": f"Não encontrei '{nome}' na lista de órgãos do SIAFI."
                        + (f" Nomes parecidos: {parecidos}." if parecidos else "")}
    cod = str(alvo["codigo"])
    ano_atual = date.today().year

    def um_ano(ano):
        linhas = []
        for param in ("orgao", "orgaoSuperior"):
            try:
                linhas = http_get(f"{PORTAL}/despesas/por-orgao?" + urllib.parse.urlencode(
                    {"ano": ano, param: cod, "pagina": 1}), H) or []
            except Exception:
                linhas = []
            if linhas:
                break
        liq = sum(to_num(l.get("liquidado")) for l in linhas)
        pago = sum(to_num(l.get("pago")) for l in linhas)
        emp = sum(to_num(l.get("empenhado")) for l in linhas)
        return {"ano": ano, "empenhado": emp, "liquidado": liq, "pago": pago,
                "pct_pago": (pago / liq * 100) if liq else None, "parcial": ano == ano_atual}

    with ThreadPoolExecutor(max_workers=3) as ex:  # poucos ao mesmo tempo, por causa do limite do Portal
        hist = list(ex.map(lambda a: cached(f"fed{cod}{a}", 6 * 3600, lambda: um_ano(a)), range(ano_atual - 5, ano_atual + 1)))
    return {"orgao_siafi": {"codigo": cod, "descricao": alvo.get("descricao")},
            "outros": [{"codigo": c.get("codigo"), "descricao": c.get("descricao")} for c in candidatos[:6]],
            "historico": hist}


# ----------------------------------------------------------------- PNCP
def _campo(d, *nomes):
    for n in nomes:
        v = d.get(n) if isinstance(d, dict) else None
        if v not in (None, ""):
            return v
    return None


def link_pncp(numero):
    """'00394460000141-2-000123/2024' -> página do contrato no PNCP."""
    m = re.match(r"^(\d{14})-\d+-(\d+)/(\d{4})$", str(numero or ""))
    if not m:
        return ""
    return f"https://pncp.gov.br/app/contratos/{m.group(1)}/{m.group(3)}/{int(m.group(2))}"


def pncp_pagina(cnpj, ini, fim, pagina, tamanho, timeout=40, tentativas=2):
    url = (f"{PNCP}/contratos?dataInicial={ini:%Y%m%d}&dataFinal={fim:%Y%m%d}"
           f"&cnpjOrgao={cnpj}&pagina={pagina}&tamanhoPagina={tamanho}")
    for tentativa in range(1, tentativas + 1):  # o PNCP às vezes demora: tenta de novo
        try:
            return http_get(url, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code == 204:
                return None
            if tentativa == tentativas or e.code < 500:
                raise
        except Exception:
            if tentativa == tentativas:
                raise
        time.sleep(2)


def pncp_contratos(cnpj, dias=None):
    """Tenta 12 meses; se o PNCP demorar (órgãos gigantes), tenta 90 e depois 30 dias."""
    if dias:
        return _pncp_contratos(cnpj, dias)
    ultimo = None
    for d, espera in ((365, 25), (90, 25), (30, 30)):
        try:
            r = _pncp_contratos(cnpj, d, espera)
            r["dias"] = d
            return r
        except Exception as e:
            ultimo = e
    raise ultimo


def _pncp_contratos(cnpj, dias=365, espera=40):
    """1º pede 50 contratos (rápido, já traz o total). Órgão até 2.000 contratos: lê todos.
    Órgão muito grande: lê uma amostra de até 500 contratos para somar valores e fornecedores."""
    fim = date.today()
    ini = fim - timedelta(days=dias)
    d = pncp_pagina(cnpj, ini, fim, 1, 50, timeout=espera, tentativas=1)
    itens = list((d or {}).get("data") or [])
    total = (d or {}).get("totalRegistros") or len(itens)
    if total > len(itens):
        if total <= 2000:
            tam, paginas = 500, range(1, (total + 499) // 500 + 1)
        else:
            tam, paginas = 50, range(2, 11)  # amostra: mais 9 páginas de 50
        extras = []
        with ThreadPoolExecutor(max_workers=4) as ex:
            futuros = [ex.submit(pncp_pagina, cnpj, ini, fim, pg, tam) for pg in paginas]
            for f in futuros:
                try:
                    extras += (f.result() or {}).get("data") or []
                except Exception:
                    pass  # se uma página falhar, fica com o que conseguiu
        if tam == 500 and extras:
            itens = extras
        else:
            itens += extras
    contratos, fornecedores = [], {}
    valor_total = 0.0
    for c in itens:
        valor = to_num(_campo(c, "valorGlobal", "valorInicial", "valorParcela"))
        valor_total += valor
        nome = _campo(c, "nomeRazaoSocialFornecedor", "nomeFornecedor") or "Não informado"
        doc = digitos(_campo(c, "niFornecedor", "cnpjFornecedor"))
        f = fornecedores.setdefault(doc or nome, {"nome": nome, "doc": doc, "contratos": 0, "valor": 0.0})
        f["contratos"] += 1
        f["valor"] += valor
        unidade = _campo(c.get("unidadeOrgao") or {}, "nomeUnidade")
        contratos.append({
            "assinatura": str(_campo(c, "dataAssinatura", "dataPublicacaoPncp") or "")[:10],
            "fim": str(_campo(c, "dataVigenciaFim") or "")[:10],
            "fornecedor": nome, "doc": doc,
            "objeto": str(_campo(c, "objetoContrato", "objeto") or "")[:400],
            "valor": valor,
            "tipo": _campo(c.get("tipoContrato") or {}, "nome") or "",
            "unidade": unidade or "",
            "link": link_pncp(_campo(c, "numeroControlePNCP")),
        })
    contratos.sort(key=lambda x: x["assinatura"], reverse=True)
    top = sorted(fornecedores.values(), key=lambda f: f["valor"], reverse=True)[:10]
    return {"cnpj": cnpj, "inicio": ini.isoformat(), "fim": fim.isoformat(), "total": total or len(itens),
            "somados": len(itens), "valor_total": valor_total, "contratos": contratos[:60], "fornecedores": top}


# ----------------------------------------------------------------- avaliação
TITULOS = {"bom": "Bom pagador", "regular": "Pagador regular", "ruim": "Mau pagador", "sem_dados": "Sem dados suficientes"}


def nivel_do_score(score):
    return "bom" if score >= 80 else "regular" if score >= 60 else "ruim"


def br(v, casas=0):
    return f"{v:.{casas}f}".replace(".", ",")


def avaliar_rp(hist, capag, entregas_lista=None):
    anos = sorted((h for h in hist if h.get("pct_pago") is not None), key=lambda h: h["ano"])
    if not anos:
        return {"nivel": "sem_dados", "titulo": TITULOS["sem_dados"], "score": None, "alertas": [],
                "resumo": "O ente não entregou ao Tesouro as declarações de contas anuais dos últimos anos, por isso não dá para medir como ele paga.",
                "motivos": ["Sem declarações de contas anuais (DCA) no SICONFI para os últimos anos."]}
    rec = anos[-3:]
    insc = sum(h["inscritos"] for h in rec)
    pct = sum(h["pagos"] for h in rec) / insc * 100
    canc = sum(h["cancelados"] for h in rec) / insc * 100
    periodo = f"{rec[0]['ano']}–{rec[-1]['ano']}" if len(rec) > 1 else str(rec[0]["ano"])
    motivos = [f"Pagou {br(pct)}% das contas já entregues e aprovadas que ficaram para o ano seguinte ({periodo})."]
    alertas = []

    pct_np = None
    np_rec = [h["np"] for h in rec if h.get("np") and h["np"].get("liquidados")]
    if np_rec:
        liq = sum(n["liquidados"] for n in np_rec)
        if liq:
            pct_np = sum(n["pagos"] for n in np_rec) / liq * 100
            motivos.append(f"Das despesas antigas que foram entregues e aprovadas durante o ano, pagou {br(pct_np)}%.")

    base = pct if pct_np is None else 0.7 * pct + 0.3 * pct_np
    score = base - 2 * canc

    if canc >= 1:
        motivos.append(f"Cancelou {br(canc, 1)}% dessas contas sem pagar.")
    if canc >= 5:
        alertas.append({"texto": "Cancela contas antigas", "tipo": "ruim"})

    tendencia = 0
    if len(anos) >= 2:
        tendencia = anos[-1]["pct_pago"] - anos[-2]["pct_pago"]
        if abs(tendencia) >= 5:
            motivos.append(f"Em {anos[-1]['ano']} o índice {'melhorou' if tendencia > 0 else 'piorou'} {br(abs(tendencia))} pontos em relação a {anos[-2]['ano']}.")
            alertas.append({"texto": "Melhorou no último ano" if tendencia > 0 else "Piorou no último ano",
                            "tipo": "bom" if tendencia > 0 else "atencao"})

    nota = (capag or {}).get("nota", "").strip().upper()
    if nota:
        ajuste = {"A": 5, "B": 0, "C": -10, "D": -20}.get(nota[0], 0)
        score += ajuste
        motivos.append(f"Nota CAPAG do Tesouro Nacional: {nota}.")
        if nota[0] == "A":
            alertas.append({"texto": f"CAPAG {nota}", "tipo": "bom"})
        elif nota[0] in "CD":
            alertas.append({"texto": f"CAPAG {nota}", "tipo": "ruim"})
        else:
            alertas.append({"texto": f"CAPAG {nota}", "tipo": "neutro"})

    if entregas_lista:
        atual = entregas_lista[0]
        if not atual.get("erro") and atual.get("total", 0) == 0 and date.today().month >= 3:
            alertas.append({"texto": "Relatórios do ano não entregues", "tipo": "atencao"})
            score -= 5

    if pct >= 95 and canc < 1:
        alertas.insert(0, {"texto": "Paga quase tudo", "tipo": "bom"})

    score = max(0, min(100, round(score)))
    nivel = nivel_do_score(score)

    if nivel == "bom":
        resumo = f"Paga em dia a grande maioria das contas com fornecedores: {br(pct)}% das contas que ficaram para o ano seguinte foram pagas."
    elif nivel == "regular":
        resumo = f"Paga a maior parte das contas, mas deixa uma fatia relevante para depois: só {br(pct)}% das contas que ficaram para o ano seguinte foram pagas."
    else:
        resumo = f"Atrasa ou deixa de pagar muitas contas: só {br(pct)}% das contas que ficaram para o ano seguinte foram pagas."
    if canc >= 5:
        resumo += f" Além disso, cancelou {br(canc, 1)}% dessas contas em vez de pagar."
    if nota and nota[0] in "CD":
        resumo += f" A nota CAPAG {nota} indica dificuldade financeira."

    return {"nivel": nivel, "titulo": TITULOS[nivel], "score": score, "pct_pago": pct, "pct_cancelado": canc,
            "pct_np": pct_np, "periodo": periodo, "motivos": motivos, "alertas": alertas, "resumo": resumo}


def avaliar_federal(fed):
    anos = [h for h in fed.get("historico", []) if h.get("pct_pago") is not None and not h.get("parcial")]
    if not anos:
        return None
    rec = anos[-3:]
    liq = sum(h["liquidado"] for h in rec)
    pct = sum(h["pago"] for h in rec) / liq * 100 if liq else 0
    score = max(0, min(100, round(pct)))
    nivel = nivel_do_score(score)
    periodo = f"{rec[0]['ano']}–{rec[-1]['ano']}"
    return {"nivel": nivel, "titulo": TITULOS[nivel], "score": score, "pct_pago": pct, "periodo": periodo, "alertas": [],
            "resumo": f"Pagou no mesmo ano {br(pct)}% do que foi entregue e aprovado entre {periodo}. O restante costuma ser pago no início do ano seguinte.",
            "motivos": [f"Pagou no mesmo ano {br(pct)}% do que liquidou (serviço entregue e aprovado) entre {periodo}."]}


# ----------------------------------------------------------------- consulta
def consultar(cnpj, chave=""):
    c = digitos(cnpj)
    if not cnpj_valido(c):
        raise ValueError("CNPJ inválido. Confira os 14 números.")
    info = consulta_cnpj(c)
    esf = esfera_do_orgao(info)
    out = {"orgao": info, "esfera": esf, "avisos": [], "consultado_em": time.strftime("%d/%m/%Y %H:%M")}
    if esf == "privado":
        out["avisos"].append(f"Este CNPJ não é de um órgão público (natureza jurídica: {info['natureza_juridica'] or 'não informada'}). "
                             "Esta ferramenta só analisa órgãos de governo.")
        return out

    base = info
    if esf == "publico":
        e2, b2 = esfera_pelo_efr(info.get("efr"))
        if e2:
            esf, base = e2, {**info, **b2}
    if esf == "estatal":
        esf, b2, motivo = esfera_da_estatal(info)
        base = {**info, **b2}
        nome_esf = {"federal": "federal", "estadual": "estadual", "municipal": "municipal"}[esf]
        out["esfera"] = esf
        out["tipo"] = f"Empresa estatal {nome_esf}"
        out["avisos"].append(
            f"É uma empresa estatal {nome_esf} ({info['natureza_juridica']}), identificada {motivo}. "
            "A análise usa as contas do governo a que ela pertence. Estatais que se sustentam com receita própria, "
            "como bancos e a Petrobras, não entram nesses relatórios e podem pagar de forma diferente.")
    else:
        out["esfera"] = esf

    ano = date.today().year
    anos = list(range(ano - 7, ano))
    ente, como = None, None
    try:
        if esf == "federal":
            ente, como = {"cod_ibge": 1, "ente": "União (Governo Federal)", "esfera": "U", "uf": "BR"}, \
                "órgão federal: o histórico abaixo é da União como um todo"
        else:
            ente, como = achar_ente(base, esf)
    except Exception as e:
        out["avisos"].append(f"Não consegui acessar o SICONFI do Tesouro ({erro_txt(e)}). Tente de novo em alguns minutos.")

    if esf == "federal":
        chave = chave or os.environ.get("PORTAL_TRANSPARENCIA_CHAVE", "")
        if chave:
            out["federal"] = federal(info, chave)
        else:
            out["avisos"].append("Para ver os pagamentos deste órgão federal específico, cadastre uma chave gratuita do "
                                 "Portal da Transparência e cole no campo \"Chave\". Sem ela, mostramos o histórico da União.")

    if ente:
        out["ente"] = {"nome": ente.get("ente"), "uf": ente.get("uf"), "cod_ibge": ente.get("cod_ibge"),
                       "populacao": ente.get("populacao"), "como": como}
        idt = ente.get("cod_ibge")
        with ThreadPoolExecutor(max_workers=8) as ex:
            f_hist = [ex.submit(dca_ano, idt, a) for a in anos]
            f_ent = [ex.submit(entregas, idt, a) for a in (ano, ano - 1)] if esf != "federal" else []
            f_cap = ex.submit(capag_do_ente, ente) if esf != "federal" else None
            f_prz = ex.submit(prazo_ente, idt) if esf != "federal" else None
            out["historico"] = [f.result() for f in f_hist]
            out["entregas"] = [f.result() for f in f_ent]
            if f_cap:
                try:
                    out["capag"] = f_cap.result()
                except Exception as e:
                    out["capag"] = {"nota": "", "obs": f"Não consegui baixar a planilha CAPAG ({erro_txt(e)})."}
        out["avaliacao"] = avaliar_rp(out["historico"], out.get("capag"), out.get("entregas"))
        if f_prz:
            try:
                aplicar_prazo(out["avaliacao"], f_prz.result())
            except Exception:
                pass
        falhas = [h for h in out["historico"] if h.get("erro")]
        if len(falhas) == len(anos):
            out["avisos"].append(f"O SICONFI não respondeu ({falhas[0]['erro']}). Tente de novo em alguns minutos.")
    elif esf != "federal" and not out["avisos"]:
        out["avisos"].append("Não encontrei o estado ou município deste órgão na base do Tesouro (SICONFI).")

    if out.get("federal") and not out["federal"].get("erro"):
        av = avaliar_federal(out["federal"])
        if av:
            aplicar_prazo(av, prazo_federal(out["federal"]))
            out["avaliacao"] = av
            if out.get("ente"):
                out["ente"]["nome"] = out["federal"]["orgao_siafi"]["descricao"]
                out["ente"]["como"] = ("a nota usa os pagamentos do próprio órgão, na aba Pagamentos federais; "
                                       "a aba Histórico mostra a União como um todo")

    ente_cnpj = digitos((ente or {}).get("cnpj")) if isinstance(ente, dict) else ""
    out["pncp_ente"] = ente_cnpj if ente_cnpj != c and esf != "federal" else ""
    return out


def pncp_do_orgao(cnpj, ente_cnpj=""):
    def f():
        r = pncp_contratos(cnpj)
        if r["total"] == 0 and ente_cnpj and ente_cnpj != cnpj:
            r2 = pncp_contratos(ente_cnpj)
            if r2["total"]:
                r2["obs"] = ("Este CNPJ não tem contratos próprios no PNCP. Mostramos os contratos publicados "
                             "pelo CNPJ principal do ente, que costuma contratar em nome das secretarias e fundos.")
                return r2
        return r
    try:
        return cached(f"pncp{cnpj}{ente_cnpj}", 6 * 3600, f)
    except Exception as e:
        return {"erro": f"O PNCP não respondeu ({erro_txt(e)}). Tente de novo em alguns minutos."}


def consultar_rapido(cnpj, chave=""):
    """Guarda o resultado por 6 horas; consultas repetidas aparecem na hora."""
    c = digitos(cnpj)
    chave_cache = f"consulta{c}{'k' if chave else ''}"
    with _lock:
        if chave_cache in _cache and time.time() - _cache[chave_cache][0] < 6 * 3600:
            return _cache[chave_cache][1]
    out = consultar(cnpj, chave)
    falhou = (any(h.get("erro") for h in out.get("historico", []))
              or (out.get("federal") or {}).get("erro")
              or any("não respondeu" in a or "Não consegui" in a for a in out.get("avisos", [])))
    if not falhou:
        with _lock:
            _cache[chave_cache] = (time.time(), out)
    return out


def diagnostico(ibge, ano):
    """Mostra os dados crus do SICONFI e da CAPAG para conferir a leitura."""
    linhas = [f"DIAGNOSTICO ente={ibge} ano={ano}", ""]
    for anexo in ("DCA-Anexo I-F",):
        its = siconfi("dca", {"an_exercicio": ano, "no_anexo": anexo, "id_ente": ibge})
        linhas.append(f"== {anexo}: {len(its)} itens")
        if its:
            linhas.append("campos: " + ", ".join(sorted(its[0].keys())))
        vistos = 0
        for it in its:
            linhas.append(" | ".join(str(it.get(k, "")) for k in ("instituicao", "cod_conta", "conta", "coluna", "valor")))
            vistos += 1
            if vistos >= 60:
                linhas.append("...")
                break
        linhas.append("")
    tipo = "municipios" if len(str(ibge)) > 2 else "estados"
    try:
        t = capag_tabela(tipo)
    except Exception as e:
        t = None
        linhas.append("CAPAG erro: " + erro_txt(e))
    linhas.append(f"== CAPAG {tipo}: " + (t["nome"] + " " + t["url"] if t else "nao encontrada"))
    if t:
        for i, r in enumerate(t["rows"][:8]):
            linhas.append(f"linha {i}: " + " | ".join(str(x) for x in r))
        for r in t["rows"]:
            if any(digitos(str(x).split(".")[0]) in (str(ibge), str(ibge)[:6]) for x in r):
                linhas.append("ENTE: " + " | ".join(str(x) for x in r))
                break
    return "\n".join(linhas)


def diagnostico_federal(cnpj):
    """Mostra o que o Portal da Transparência responde, sem expor a chave."""
    chave = os.environ.get("PORTAL_TRANSPARENCIA_CHAVE", "")
    if not chave:
        return "Sem chave configurada no servidor."
    H = {"chave-api-dados": chave}
    info = consulta_cnpj(digitos(cnpj))
    linhas = [f"DIAGNOSTICO FEDERAL {info['cnpj']} {info['razao_social']} ({info.get('nome_fantasia')})", ""]
    lista = lista_orgaos_siafi(chave, espera=20)
    linhas.append(f"Lista de órgãos: {'pronta, ' + str(len(lista)) + ' órgãos' if lista is not None else 'baixando'}"
                  f" (páginas lidas: {_orgaos['paginas']}) {('erro: ' + _orgaos['erro']) if _orgaos['erro'] else ''}")
    if lista is None:
        linhas.append("Abra esta página de novo em 1 ou 2 minutos.")
        return "\n".join(linhas)
    alvo, cands = achar_orgao_siafi(info, chave)
    linhas.append("ESCOLHIDO: " + (json.dumps(alvo, ensure_ascii=False) if alvo else "nenhum"))
    for c in cands:
        linhas.append(f"   {parecido(info['razao_social'], limpar_nome_siafi(c.get('descricao'))):.2f}  {c.get('codigo')} {c.get('descricao')}")
    if alvo:
        ano = date.today().year - 1
        for param in ("orgao", "orgaoSuperior"):
            url = f"{PORTAL}/despesas/por-orgao?" + urllib.parse.urlencode({"ano": ano, param: alvo["codigo"], "pagina": 1})
            try:
                r = http_get(url, H)
                linhas.append(f"== despesas/por-orgao {param}={alvo['codigo']} ano={ano}: {len(r or [])} itens")
                for c in (r or [])[:5]:
                    linhas.append("   " + json.dumps(c, ensure_ascii=False)[:400])
            except Exception as e:
                linhas.append(f"== despesas/por-orgao {param}: ERRO {erro_txt(e)}")
            time.sleep(0.5)
    return "\n".join(linhas)


# ----------------------------------------------------------------- servidor
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
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
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
            return _redirecionar(self, "https://score.sigalicita.com.br" + self.path)
        if _tratar_entrar_sair(self) or _barrar_sem_senha(self, u.path):
            return
        if u.path in ("/", "/index.html"):
            return self._send(200, PAGINA, "text/html; charset=utf-8")
        if u.path == "/diagnostico":
            q = urllib.parse.parse_qs(u.query)
            try:
                txt = diagnostico(q.get("ibge", ["3550308"])[0], q.get("ano", [str(date.today().year - 1)])[0])
            except Exception as e:
                txt = "ERRO: " + erro_txt(e)
            return self._send(200, txt, "text/plain; charset=utf-8")
        if u.path == "/diagnostico-federal":
            q = urllib.parse.parse_qs(u.query)
            try:
                txt = diagnostico_federal(q.get("cnpj", ["00399857000126"])[0])
            except Exception as e:
                txt = "ERRO: " + erro_txt(e)
            return self._send(200, txt, "text/plain; charset=utf-8")
        if u.path == "/diagnostico-prazo":
            q = urllib.parse.parse_qs(u.query)
            ibge = digitos(q.get("ibge", ["3550308"])[0])
            linhas = [f"DIAGNOSTICO PRAZO ente={ibge}"]
            try:
                pz = prazo_ente(ibge)
                linhas.append("resultado: " + json.dumps(pz, ensure_ascii=False))
                hoje = date.today()
                for ano, per in [(hoje.year, n) for n in range(6, 0, -1)] + [(hoje.year - 1, 6)]:
                    if ano == hoje.year and per * 2 > hoje.month + 1:
                        continue
                    its = siconfi("rreo", {"an_exercicio": ano, "nr_periodo": per, "co_tipo_demonstrativo": "RREO",
                                           "no_anexo": "RREO-Anexo 01", "id_ente": ibge})
                    linhas.append(f"== RREO {ano} bimestre {per}: {len(its)} itens")
                    if its:
                        for it in its:
                            cn = norm(it.get("conta"))
                            if cn.startswith(("outras despesas correntes", "investimentos", "despesas correntes", "total")):
                                linhas.append(f"   {it.get('conta')} | {it.get('coluna')} | {it.get('valor')}")
                        break
            except Exception as e:
                linhas.append("ERRO: " + erro_txt(e))
            return self._send(200, "\n".join(linhas[:200]), "text/plain; charset=utf-8")
        if u.path == "/diagnostico-pncp":
            q = urllib.parse.parse_qs(u.query)
            cn = digitos(q.get("cnpj", ["46395000000139"])[0])
            fim = date.today()
            linhas = [f"DIAGNOSTICO PNCP {cn}"]
            for dias in (365, 90, 30):
                t0 = time.time()
                try:
                    d = pncp_pagina(cn, fim - timedelta(days=dias), fim, 1, 10, timeout=60, tentativas=1)
                    linhas.append(f"{dias} dias: {time.time() - t0:.1f}s total={(d or {}).get('totalRegistros')} paginas={(d or {}).get('totalPaginas')}")
                except Exception as e:
                    linhas.append(f"{dias} dias: ERRO depois de {time.time() - t0:.1f}s: {erro_txt(e)}")
            return self._send(200, "\n".join(linhas), "text/plain; charset=utf-8")
        if u.path == "/api/consulta":
            q = urllib.parse.parse_qs(u.query)
            try:
                res = consultar_rapido(q.get("cnpj", [""])[0], q.get("chave", [""])[0].strip())
                return self._send(200, json.dumps(res, ensure_ascii=False), "application/json; charset=utf-8")
            except ValueError as e:
                return self._send(400, json.dumps({"erro": str(e)}, ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                return self._send(502, json.dumps({"erro": str(e)}, ensure_ascii=False), "application/json; charset=utf-8")
        self._send(404, "não encontrado", "text/plain; charset=utf-8")


PAGINA = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Consulta Pagador Público</title>
<link rel="icon" type="image/png" sizes="64x64" href="data:image/png;base64,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">
<link rel="apple-touch-icon" href="data:image/png;base64,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">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap">
<style>
/* Layout: painel escuro azul-marinho — busca no topo, ficha do órgão com números, abas, nota em círculo */
:root{
  --bg:#0d1422; --surface:#151f32; --surface2:#1b2740; --line:#26344f; --line2:#31415f;
  --ink:#e8eef8; --muted:#94a3bb; --dim:#6b7a93;
  --accent:#38bdf8; --accent-ink:#06243a; --accent-soft:#12324a;
  --good:#3ecf8e; --good-bg:#0f2e24; --good-line:#1f6b4c;
  --warn:#f5b94a; --warn-bg:#33270f; --warn-line:#6e5317;
  --bad:#f6766f; --bad-bg:#371a1c; --bad-line:#7a2f2f;
  --note:#f08a4b; --note-bg:#33201a; --note-line:#6b3a22;
  --font:"Plus Jakarta Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --r:14px; color-scheme:dark;
}
*{box-sizing:border-box}
[hidden]{display:none!important}
html{background:var(--bg)}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 var(--font);-webkit-font-smoothing:antialiased}
.wrap{max-width:1000px;margin:0 auto;padding-block:28px 64px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));display:grid;gap:20px}
a{color:var(--accent)}
button{font-family:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.num{font-variant-numeric:tabular-nums}

/* topo */
.top{display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.marca{height:75px;width:auto;max-width:100%;display:block}
.top-t{border-left:1px solid var(--line2);padding-left:18px;min-width:0}
.logo{width:38px;height:38px;border-radius:10px;background:var(--accent-soft);display:grid;place-items:center;color:var(--accent);flex:none}
.top h1{font-size:20px;font-weight:800;margin:0;letter-spacing:-.01em}
.top p{margin:0;color:var(--muted);font-size:13px}

/* busca */
.busca{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:18px;display:grid;gap:12px}
.campo{display:flex;gap:10px;flex-wrap:wrap}
.campo .in{flex:1 1 280px;min-width:0;position:relative}
.campo svg{position:absolute;left:14px;top:50%;transform:translateY(-50%);color:var(--dim)}
input{width:100%;font:600 18px var(--font);font-variant-numeric:tabular-nums;padding:13px 14px 13px 44px;border:1px solid var(--line2);border-radius:10px;background:var(--bg);color:var(--ink)}
input::placeholder{color:var(--dim)}
input.chave{font-size:14px;font-weight:500;padding-left:14px}
.btn{font-weight:700;font-size:15px;padding:13px 24px;border-radius:10px;border:0;background:var(--accent);color:var(--accent-ink);cursor:pointer}
.btn:disabled{opacity:.6;cursor:wait}
.btn-o{font-weight:700;font-size:14px;padding:9px 16px;border-radius:10px;border:1px solid var(--accent);background:transparent;color:var(--accent);cursor:pointer}
.ex{display:flex;gap:8px;flex-wrap:wrap;align-items:center;font-size:13px;color:var(--muted)}
.chip-ex{background:var(--surface2);border:1px solid var(--line2);color:var(--ink);padding:5px 12px;border-radius:999px;font-size:13px;font-weight:500;cursor:pointer}
.chip-ex:hover{border-color:var(--accent)}
details.chv summary{cursor:pointer;color:var(--muted);font-size:13px}
details.chv .campo{margin-top:10px}
.hint{font-size:13px;color:var(--muted);margin:6px 0 0}

/* ficha */
.ficha{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:22px;display:grid;gap:20px;min-width:0}
.cab{display:flex;gap:16px;justify-content:space-between;align-items:flex-start;flex-wrap:wrap}
.cab-t{min-width:0;flex:1 1 380px}
.cab h2{margin:0;color:var(--accent);font-size:17px;font-weight:800;text-transform:uppercase;line-height:1.35;text-wrap:balance}
.cab .sub{color:var(--muted);font-size:13px;margin-top:4px}
.badges{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:13px;font-weight:600;padding:5px 12px;border-radius:999px;background:var(--accent-soft);color:var(--accent);white-space:nowrap}
.badge.ok{background:var(--good-bg);color:var(--good)} .badge.no{background:var(--bad-bg);color:var(--bad)}
hr.sep{border:0;border-top:1px solid var(--line);margin:0}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:18px}
.stat .lb{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:13px}
.stat .v{font-size:22px;font-weight:800;margin-top:4px;line-height:1.2}
.stat .s{color:var(--muted);font-size:12.5px;margin-top:2px}
.c-bom{color:var(--good)} .c-regular{color:var(--warn)} .c-ruim{color:var(--bad)} .c-sem_dados{color:var(--muted)} .c-acc{color:var(--accent)}

/* abas */
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);overflow-x:auto;scrollbar-width:none}
.tab{background:none;border:0;color:var(--ink);font-weight:600;font-size:15px;padding:10px 14px 12px;cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap}
.tab[aria-selected="true"]{color:var(--note);border-bottom-color:var(--note)}
.tab:hover{color:var(--accent)}
.painel{display:grid;gap:18px;min-width:0}

/* faixa */
.faixa{display:flex;gap:12px;align-items:flex-start;padding:14px 16px;border-radius:10px;border:1px solid var(--line-c);background:var(--bg-c)}
.faixa b{display:block;font-size:16px}
.faixa p{margin:2px 0 0}
.faixa svg{flex:none;margin-top:2px;color:var(--c)}
.t-bom{--c:var(--good);--bg-c:var(--good-bg);--line-c:var(--good-line)}
.t-regular,.t-atencao{--c:var(--warn);--bg-c:var(--warn-bg);--line-c:var(--warn-line)}
.t-ruim{--c:var(--bad);--bg-c:var(--bad-bg);--line-c:var(--bad-line)}
.t-sem_dados,.t-neutro{--c:var(--muted);--bg-c:var(--surface2);--line-c:var(--line2)}

/* score */
.sec-h{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.sec-h h3{margin:0;color:var(--accent);font-size:16px;font-weight:700}
.sec-h p{margin:2px 0 0;color:var(--muted);font-size:13px}
.score{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:20px;align-items:center}
.score-l{display:flex;gap:18px;align-items:center}
.gauge{position:relative;width:104px;height:104px;flex:none}
.gauge svg{transform:rotate(-90deg)}
.gauge .n{position:absolute;inset:0;display:grid;place-items:center;text-align:center;line-height:1}
.gauge .n b{font-size:30px;font-weight:800;display:block}
.gauge .n span{font-size:12px;color:var(--muted)}
.score-l .nv{font-size:26px;font-weight:800;line-height:1.1}
.score-l .det{font-size:13px;color:var(--muted);margin:4px 0 8px}
.score-l .det b{color:var(--ink)}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{font-size:12.5px;font-weight:600;padding:4px 10px;border-radius:999px;background:var(--bg-c);color:var(--c);border:1px solid var(--line-c)}
.nota{background:var(--note-bg);border:1px solid var(--note-line);border-radius:10px;padding:14px 16px}
.nota b{display:flex;gap:6px;align-items:center;color:var(--note);font-size:15px}
.nota p{margin:6px 0 0;font-size:14px;color:var(--ink)}
ul.porque{margin:0;padding-left:18px;display:grid;gap:4px;color:var(--ink)}
.box{background:var(--surface2);border:1px solid var(--line);border-radius:10px;padding:16px;min-width:0}
.box h4{margin:0 0 10px;font-size:15px}

/* gráfico e tabelas */
svg text{fill:var(--muted);font:11px var(--font)}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-top:6px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px;font-variant-numeric:tabular-nums}
th,td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{font-size:12px;font-weight:600;color:var(--muted)}
td.obj{white-space:normal;text-align:left;min-width:300px;max-width:440px;font-size:13px;color:var(--muted)}
td.forn{white-space:normal;text-align:left;min-width:200px;max-width:280px}
td.forn small{display:block;color:var(--dim);font-size:12px}
.resumo-pncp{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-weight:700;font-size:12.5px;background:var(--bg-c);color:var(--c)}
.capag-big{display:flex;gap:18px;align-items:center;flex-wrap:wrap}
.capag-big .letra{width:84px;height:84px;border-radius:16px;display:grid;place-items:center;font-size:40px;font-weight:800;background:var(--bg-c);color:var(--c);border:1px solid var(--line-c)}
.grid3{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.mini{background:var(--surface2);border:1px solid var(--line);border-radius:10px;padding:12px}
.mini span{display:block;font-size:12.5px;color:var(--muted)}
.mini b{font-size:20px}
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px 22px}
.meta div span{display:block;font-size:12px;color:var(--muted)}
.meta div{min-width:0;overflow-wrap:anywhere}

.aviso{background:var(--warn-bg);border:1px solid var(--warn-line);padding:12px 14px;border-radius:10px;font-size:14px}
.erro{background:var(--bad-bg);border:1px solid var(--bad-line);padding:14px 16px;border-radius:var(--r)}
.carregando{display:flex;gap:12px;align-items:center;color:var(--muted);background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:20px}
.spin{width:20px;height:20px;border:2px solid var(--line2);border-top-color:var(--accent);border-radius:50%;animation:g .8s linear infinite;flex:none}
@keyframes g{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.spin{animation:none}}
.metodo{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:16px 18px;color:var(--muted);font-size:14px}
.metodo summary{cursor:pointer;color:var(--ink);font-weight:700}
.metodo p{margin:10px 0 0;max-width:75ch}
footer{font-size:12px;color:var(--dim)}

@media (max-width:760px){
  .stats{grid-template-columns:repeat(2,minmax(0,1fr))}
  td.obj{min-width:220px}
  .score{grid-template-columns:1fr}
  .badges{justify-content:flex-start}
}
@media (max-width:420px){.score-l{flex-direction:column;align-items:flex-start}}
@media (max-width:560px){.top-t{border-left:0;padding-left:0}.marca{height:60px}}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <img class="marca" src="data:image/png;base64,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" alt="SigaScore" width="251" height="75">
    <div class="top-t"><h1>Consulta Pagador Público</h1><p>Veja se um órgão do governo costuma pagar os fornecedores em dia</p></div>
  </div>

  <form class="busca" id="f" autocomplete="off">
    <div class="campo">
      <div class="in">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
        <input id="cnpj" inputmode="numeric" placeholder="Digite o CNPJ do órgão" maxlength="18" aria-label="CNPJ do órgão" required>
      </div>
      <button class="btn" id="btn" type="submit">Consultar</button>
    </div>
    <div class="ex">Exemplos:
      <button type="button" class="chip-ex" data-cnpj="46395000000139">Prefeitura de São Paulo</button>
      <button type="button" class="chip-ex" data-cnpj="42498733000148">Prefeitura do Rio de Janeiro</button>
    </div>
    <details class="chv">
      <summary>Órgão federal? Informe a chave do Portal da Transparência (opcional)</summary>
      <div class="campo"><input id="chave" class="chave" placeholder="Cole aqui a chave de acesso" aria-label="Chave do Portal da Transparência"></div>
      <p class="hint">A chave é gratuita: cadastre-se em <a href="https://portaldatransparencia.gov.br/api-de-dados/cadastrar-email" target="_blank" rel="noopener">portaldatransparencia.gov.br/api-de-dados</a> com sua conta gov.br. Ela fica salva só neste navegador.</p>
    </details>
  </form>

  <main id="out" aria-live="polite" style="display:grid;gap:20px"></main>

  <details class="metodo">
    <summary>Como a nota é calculada</summary>
    <p><b>Contas que ficaram para o ano seguinte</b> (restos a pagar processados) são serviços e compras que o fornecedor já entregou e o órgão já aprovou, mas não pagou no ano. É a dívida mais direta com quem vende para o governo.</p>
    <p>A nota de 0 a 100 parte do percentual dessas contas que foi pago nos últimos 3 anos (Declaração de Contas Anuais, anexo I-F, no SICONFI). Ela também considera o pagamento de despesas antigas aprovadas durante o ano, desconta as contas canceladas sem pagar e ajusta pela nota CAPAG do Tesouro Nacional (A soma, C e D tiram pontos).</p>
    <p><b>80 ou mais:</b> bom pagador. <b>60 a 79:</b> regular. <b>Abaixo de 60:</b> mau pagador.</p>
    <p>Secretarias, autarquias e câmaras usam as contas do município ou estado a que pertencem. A nota é uma leitura dos dados públicos e não é uma certificação oficial.</p>
  </details>
  <footer>Dados: Receita Federal (via BrasilAPI), SICONFI e Tesouro Transparente (Tesouro Nacional), Portal da Transparência (CGU).</footer>
</div>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const brl = v => v == null ? "—" : new Intl.NumberFormat("pt-BR",{style:"currency",currency:"BRL",notation:"compact",maximumFractionDigits:1}).format(v);
const pct = (v, d=1) => v == null ? "—" : v.toLocaleString("pt-BR",{maximumFractionDigits:d}) + "%";
const nivelAno = (p, c) => p == null ? "sem_dados" : (p >= 85 && (c||0) < 5) ? "bom" : (p >= 65 && (c||0) < 15) ? "regular" : "ruim";
const nivelCapag = n => !n ? "neutro" : n[0] === "A" ? "bom" : n[0] === "B" ? "neutro" : n[0] === "C" ? "atencao" : "ruim";
const mascara = v => { const d = v.replace(/\D/g,"").slice(0,14);
  return d.replace(/^(\d{2})(\d)/,"$1.$2").replace(/^(\d{2})\.(\d{3})(\d)/,"$1.$2.$3").replace(/\.(\d{3})(\d)/,".$1/$2").replace(/(\d{4})(\d)/,"$1-$2"); };
const data = s => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || ""); return m ? `${m[3]}/${m[2]}/${m[1]}` : ""; };
const ESFERA = {municipal:"Órgão municipal", estadual:"Órgão estadual", federal:"Órgão federal", publico:"Órgão público", privado:"Não é órgão público"};
const ICON = {
  ok:'<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="10"/><path d="m8 12 3 3 5-6" stroke="var(--bg)" stroke-width="2.2" fill="none" stroke-linecap="round"/></svg>',
  alerta:'<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2 1 21h22L12 2z"/><path d="M12 9v5M12 17.5v.5" stroke="var(--bg)" stroke-width="2.2" stroke-linecap="round"/></svg>',
  escudo:'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6l-8-3z"/></svg>',
  barras:'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg>',
  doc:'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 3H6v18h12V7l-4-4z"/><path d="M9 13h6M9 17h6"/></svg>',
  moeda:'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M15 9.5c-.5-1-1.6-1.5-3-1.5-1.7 0-3 .8-3 2s1.3 1.7 3 2 3 .8 3 2-1.3 2-3 2c-1.4 0-2.5-.5-3-1.5M12 6v12"/></svg>',
  predio:'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 21h18M4 10h16M12 3l8 5H4l8-5zM6 10v8M10 10v8M14 10v8M18 10v8"/></svg>',
  contrato:'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 3H6v18h12V7l-4-4z"/><path d="M14 3v4h4M9 12h6M9 16h4"/></svg>',
  brilho:'<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l1.8 5.2L19 9l-5.2 1.8L12 16l-1.8-5.2L5 9l5.2-1.8zM19 14l.9 2.1L22 17l-2.1.9L19 20l-.9-2.1L16 17l2.1-.9z"/></svg>'
};

try { const k = localStorage.getItem("chave-portal"); if (k) $("#chave").value = k; } catch(e) {}
$("#cnpj").addEventListener("input", e => e.target.value = mascara(e.target.value));
document.querySelectorAll(".chip-ex").forEach(b => b.addEventListener("click", () => { $("#cnpj").value = mascara(b.dataset.cnpj); $("#f").requestSubmit(); }));

// Abrir já consultando: ?cnpj=00000000000000 (com ou sem pontuação)
setTimeout(() => {
  let c = "";
  try { c = new URLSearchParams(location.search).get("cnpj") || ""; } catch (e) {}
  if (!c) { const m = /cnpj=([\d./-]+)/.exec(location.hash); if (m) c = m[1]; }
  c = c.replace(/\D/g, "");
  if (c.length === 14) { $("#cnpj").value = mascara(c); $("#f").requestSubmit(); }
}, 0);

$("#f").addEventListener("submit", async ev => {
  ev.preventDefault();
  const cnpj = $("#cnpj").value.replace(/\D/g,"");
  if (cnpj.length !== 14) { $("#out").innerHTML = `<div class="erro">O CNPJ precisa ter 14 números.</div>`; return; }
  try { history.replaceState(null, "", "?cnpj=" + cnpj); } catch (e) {}
  const chave = $("#chave").value.trim();
  try { chave ? localStorage.setItem("chave-portal", chave) : localStorage.removeItem("chave-portal"); } catch(e) {}
  $("#btn").disabled = true;
  $("#out").innerHTML = `<div class="carregando"><div class="spin"></div>Consultando Receita Federal e Tesouro Nacional…</div>`;
  try {
    const r = await fetch(`/api/consulta?cnpj=${cnpj}&chave=${encodeURIComponent(chave)}`);
    const d = await r.json();
    if (!r.ok) throw new Error(d.erro || "Falha na consulta.");
    if (d.esfera !== "privado") d.pncp = {carregando: true};
    render(d);
    if (d.pncp) carregarPncp(d, cnpj);
  } catch (e) {
    $("#out").innerHTML = `<div class="erro"><b>Não deu para consultar.</b> ${esc(e.message)}</div>`;
  } finally { $("#btn").disabled = false; }
});

let consultaAtual = 0;
async function carregarPncp(d, cnpj) {
  const minha = ++consultaAtual;
  try {
    const r = await fetch(`/api/pncp?cnpj=${cnpj}&ente=${d.pncp_ente || ""}`);
    d.pncp = await r.json();
  } catch (e) {
    d.pncp = {erro: "O PNCP não respondeu. Tente de novo em alguns minutos."};
  }
  if (minha !== consultaAtual) return;  // o usuário já fez outra consulta
  const aba = (document.querySelector('.tab[aria-selected="true"]') || {}).dataset?.tab;
  render(d, aba);
}

function gauge(score, nivel) {
  const r = 44, c = 2 * Math.PI * r, v = score == null ? 0 : score;
  const cor = {bom:"var(--good)", regular:"var(--warn)", ruim:"var(--bad)"}[nivel] || "var(--dim)";
  return `<div class="gauge"><svg width="104" height="104" viewBox="0 0 104 104">
    <circle cx="52" cy="52" r="${r}" fill="none" stroke="var(--line)" stroke-width="9"/>
    <circle cx="52" cy="52" r="${r}" fill="none" stroke="${cor}" stroke-width="9" stroke-linecap="round" stroke-dasharray="${c * v / 100} ${c}"/>
  </svg><div class="n"><div><b class="num">${score == null ? "—" : score}</b><span>/100</span></div></div></div>`;
}

function grafico(hist) {
  const anos = hist.filter(h => h.pct_pago != null);
  if (!anos.length) return `<p class="hint">Sem declarações de contas anuais disponíveis para estes anos.</p>`;
  const W = 680, H = 230, L = 42, R = 10, T = 18, B = 28, ih = H - T - B, iw = W - L - R;
  const bw = Math.min(54, iw / anos.length * 0.56), step = iw / anos.length;
  const y = v => T + ih - Math.min(v, 100) / 100 * ih;
  let s = `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Percentual pago por ano">`;
  for (const t of [0, 25, 50, 75, 100]) s += `<line x1="${L}" x2="${W-R}" y1="${y(t)}" y2="${y(t)}" stroke="var(--line)"/><text x="${L-8}" y="${y(t)+4}" text-anchor="end">${t}%</text>`;
  for (const [t, c] of [[85,"var(--good)"],[65,"var(--warn)"]]) s += `<line x1="${L}" x2="${W-R}" y1="${y(t)}" y2="${y(t)}" stroke="${c}" stroke-opacity=".7" stroke-dasharray="4 5"/>`;
  anos.forEach((h, i) => {
    const x = L + step * i + (step - bw) / 2, n = nivelAno(h.pct_pago, h.pct_cancelado);
    const cor = {bom:"var(--good)", regular:"var(--warn)", ruim:"var(--bad)"}[n];
    const hc = Math.min(h.pct_cancelado, 100 - Math.min(h.pct_pago,100)) / 100 * ih;
    s += `<rect x="${x}" y="${y(h.pct_pago)}" width="${bw}" height="${T+ih-y(h.pct_pago)}" fill="${cor}" rx="4"><title>${h.ano}: ${pct(h.pct_pago)} pago</title></rect>`;
    if (hc > 0.5) s += `<rect x="${x}" y="${y(h.pct_pago)-hc}" width="${bw}" height="${hc}" fill="var(--dim)" rx="2"><title>${h.ano}: ${pct(h.pct_cancelado)} cancelado</title></rect>`;
    s += `<text x="${x+bw/2}" y="${y(h.pct_pago)-hc-6}" text-anchor="middle" style="fill:var(--ink);font-weight:700">${Math.round(h.pct_pago)}%</text>`;
    s += `<text x="${x+bw/2}" y="${H-8}" text-anchor="middle">${h.ano}</text>`;
  });
  return s + `</svg><div class="legend"><span><i style="background:var(--good)"></i>Pago</span><span><i style="background:var(--dim)"></i>Cancelado sem pagar</span><span>Linhas tracejadas: 85% (bom) e 65% (mínimo regular)</span></div>`;
}

function periodoPncp(p) { return !p.dias || p.dias >= 365 ? "12 meses" : "últimos " + p.dias + " dias"; }
function statPncp(p) {
  if (!p) return "";
  if (p.carregando) return `<div class="stat"><div class="lb">${ICON.contrato} Contratos no PNCP</div><div class="v c-acc" style="display:flex;align-items:center;gap:8px"><span class="spin" style="width:16px;height:16px"></span></div><div class="s">buscando no PNCP…</div></div>`;
  if (p.erro) return `<div class="stat"><div class="lb">${ICON.contrato} Contratos no PNCP</div><div class="v c-acc">—</div><div class="s">PNCP indisponível</div></div>`;
  return `<div class="stat"><div class="lb">${ICON.contrato} Contratos no PNCP</div><div class="v c-acc num">${(p.total || 0).toLocaleString("pt-BR")}</div><div class="s">${p.somados < p.total ? periodoPncp(p) : brl(p.valor_total) + " · " + periodoPncp(p)}</div></div>`;
}

function render(d, abaInicial) {
  const o = d.orgao || {}, a = d.avaliacao, h = d.historico || [], cap = d.capag;
  const nv = a ? a.nivel : "sem_dados";
  const ativa = /ativa/i.test(o.situacao || "");
  const sigla = o.nome_fantasia && o.nome_fantasia.length < 40 ? o.nome_fantasia : "";
  const sub = [sigla, mascara(o.cnpj || ""), [o.municipio, o.uf].filter(Boolean).join("/"), o.abertura ? "Aberto em " + data(o.abertura) : ""].filter(Boolean).map(esc).join(" · ");

  let html = `<section class="ficha">`;
  for (const av of d.avisos || []) html += `<div class="aviso">${esc(av)}</div>`;
  html += `<div class="cab"><div class="cab-t"><h2>${esc(o.razao_social)}</h2><div class="sub">${sub}</div></div>
    <div class="badges"><span class="badge">${esc(d.tipo || ESFERA[d.esfera] || "Órgão")} ${ICON.predio}</span>
    ${o.situacao ? `<span class="badge ${ativa ? "ok" : "no"}">${esc(o.situacao.toUpperCase())}</span>` : ""}</div></div>`;

  if (a) {
    html += `<hr class="sep"><div class="stats">
      <div class="stat"><div class="lb">${ICON.escudo} Parecer</div><div class="v c-${nv}">${esc(a.titulo)}</div><div class="s">pagamento a fornecedores</div></div>
      <div class="stat"><div class="lb">${ICON.barras} Nota do órgão</div><div class="v c-${nv} num">${a.score == null ? "—" : a.score + " · " + esc(a.titulo.split(" ")[0])}</div><div class="s">de 0 a 100</div></div>
      <div class="stat"><div class="lb">${ICON.doc} Nota CAPAG</div><div class="v c-acc">${esc((cap && cap.nota) || "—")}</div><div class="s">Tesouro Nacional</div></div>
      <div class="stat"><div class="lb">${ICON.moeda} Contas pagas</div><div class="v c-acc num">${pct(a.pct_pago)}</div><div class="s">${esc(a.periodo || "")}</div></div>
      ${a.prazo ? `<div class="stat"><div class="lb">${ICON.barras} Prazo de pagamento</div><div class="v num c-${a.prazo.dias > 60 ? "ruim" : a.prazo.dias > 30 ? "regular" : "bom"}">~${a.prazo.dias} dia${a.prazo.dias === 1 ? "" : "s"}</div><div class="s">${/Governo Federal/.test(a.prazo.periodo) ? "média do Governo Federal" : "média estimada"}</div></div>` : ""}
      ${statPncp(d.pncp)}
    </div><hr class="sep">`;
  } else if (d.pncp && !d.pncp.erro) {
    html += `<hr class="sep"><div class="stats">${statPncp(d.pncp)}</div><hr class="sep">`;
  }

  const abas = [["resumo","Resumo"]];
  if (h.length) abas.push(["historico","Histórico"]);
  if (d.federal) abas.push(["federal","Pagamentos federais"]);
  if (d.pncp) abas.push(["contratos","Contratos"]);
  if (cap) abas.push(["capag","Nota CAPAG"]);
  if ((d.entregas || []).some(e => !e.erro)) abas.push(["relatorios","Relatórios"]);
  abas.push(["cadastro","Cadastro"]);
  html += `<div class="tabs" role="tablist">${abas.map(([id, t], i) => `<button class="tab" role="tab" data-tab="${id}" aria-selected="${i === 0}">${t}</button>`).join("")}</div>`;

  // Resumo
  html += `<div class="painel" data-p="resumo">`;
  if (a) {
    html += `<div class="faixa t-${nv}">${nv === "bom" ? ICON.ok : ICON.alerta}<div><b style="color:var(--c)">${esc(a.titulo)}</b><p>${esc(a.resumo || "")}</p></div></div>
      <div class="sec-h"><div><h3>Nota do órgão</h3><p>Quanto maior a nota, menor o risco de atraso no pagamento.</p></div>
      ${h.length ? `<button class="btn-o" type="button" data-ir="historico">Ver histórico completo</button>` : ""}</div>
      <div class="score"><div class="score-l">${gauge(a.score, nv)}<div>
        <div class="nv c-${nv}">${esc(a.titulo)}</div>
        <div class="det">Contas pagas <b>${pct(a.pct_pago, 0)}</b>${a.pct_cancelado != null ? ` · Canceladas <b>${pct(a.pct_cancelado)}</b>` : ""}</div>
        <div class="chips">${(a.alertas || []).map(c => `<span class="chip t-${esc(c.tipo)}">${esc(c.texto)}</span>`).join("")}</div>
      </div></div>
      <div class="nota"><b>${ICON.brilho} Por que essa nota</b><ul class="porque" style="margin-top:6px">${(a.motivos || []).map(m => `<li>${esc(m)}</li>`).join("")}</ul></div></div>`;
  } else {
    html += `<div class="faixa t-sem_dados">${ICON.alerta}<div><b>Sem avaliação</b><p>Não há dados de pagamento para este CNPJ.</p></div></div>`;
  }
  if (d.ente) html += `<p class="hint" style="margin:0">Contas analisadas: <b style="color:var(--ink)">${esc(d.ente.nome)}${d.ente.uf && d.ente.uf !== "BR" && d.ente.cod_ibge > 99 ? "/" + esc(d.ente.uf) : ""}</b> (${esc(d.ente.como)}).</p>`;
  html += `</div>`;

  // Histórico
  if (h.length) {
    html += `<div class="painel" data-p="historico" hidden><div class="box"><h4>Contas pagas por ano</h4>${grafico(h)}</div>
      <div class="scroll"><table>
      <tr><th>Ano</th><th>Contas a pagar</th><th>Pago</th><th>Cancelado</th><th>Ficou para depois</th><th>% pago</th><th>Reservas antigas</th></tr>
      ${h.slice().reverse().map(x => x.erro ? `<tr><td>${x.ano}</td><td colspan="6" style="text-align:left;color:var(--muted)">não respondeu (${esc(x.erro)})</td></tr>`
        : x.pct_pago == null ? `<tr><td>${x.ano}</td><td colspan="6" style="text-align:left;color:var(--muted)">declaração não entregue ou sem contas pendentes</td></tr>`
        : `<tr><td>${x.ano}</td><td>${brl(x.inscritos)}</td><td>${brl(x.pagos)}</td><td>${brl(x.cancelados)}</td><td>${brl(x.saldo)}</td>
           <td><span class="pill t-${nivelAno(x.pct_pago, x.pct_cancelado)}">${pct(x.pct_pago)}</span></td><td>${x.np ? brl(x.np.inscritos) : "—"}</td></tr>`).join("")}
      </table></div>
      <p class="hint" style="margin:0">"Contas a pagar" são serviços já entregues e aprovados que ficaram para o ano seguinte. "Reservas antigas" é dinheiro reservado para serviços que ainda não tinham sido entregues. Fonte: SICONFI, Declaração de Contas Anuais, anexo I-F.</p></div>`;
  }

  // Federal
  if (d.federal) {
    const f = d.federal;
    html += `<div class="painel" data-p="federal" hidden>${f.erro ? `<div class="aviso">${esc(f.erro)}</div>` :
      `<p class="hint" style="margin:0">Órgão SIAFI ${esc(f.orgao_siafi.codigo)} · ${esc(f.orgao_siafi.descricao)}</p>
      <div class="scroll"><table><tr><th>Ano</th><th>Empenhado</th><th>Liquidado</th><th>Pago</th><th>% pago</th></tr>
      ${f.historico.map(x => `<tr><td>${x.ano}${x.parcial ? " (parcial)" : ""}</td><td>${brl(x.empenhado)}</td><td>${brl(x.liquidado)}</td><td>${brl(x.pago)}</td><td>${pct(x.pct_pago)}</td></tr>`).join("")}
      </table></div>`}</div>`;
  }

  // Contratos PNCP
  if (d.pncp) {
    const p = d.pncp;
    html += `<div class="painel" data-p="contratos" hidden>`;
    if (p.carregando) html += `<div class="carregando"><div class="spin"></div>Buscando os contratos no PNCP. Aparecem aqui em alguns segundos.</div>`;
    else if (p.erro) html += `<div class="aviso">${esc(p.erro)}</div>`;
    else if (!p.total) html += `<div class="faixa t-neutro">${ICON.alerta}<div><b>Nenhum contrato no PNCP</b><p>Este órgão não publicou contratos no Portal Nacional de Contratações Públicas entre ${esc(data(p.inicio))} e ${esc(data(p.fim))}.</p></div></div>`;
    else {
      html += `${p.obs ? `<div class="aviso">${esc(p.obs)}</div>` : ""}
      <div class="resumo-pncp">
        <div class="mini"><span>Contratos publicados</span><b class="num">${p.total.toLocaleString("pt-BR")}</b></div>
        <div class="mini"><span>Valor somado${p.somados < p.total ? ` (amostra de ${p.somados.toLocaleString("pt-BR")} contratos)` : ""}</span><b class="num">${brl(p.valor_total)}</b></div>
        <div class="mini"><span>Período</span><b style="font-size:15px">${esc(data(p.inicio))} a ${esc(data(p.fim))}</b></div>
      </div>
      <div class="box"><h4>Maiores fornecedores (por valor)</h4><div class="scroll"><table>
        <tr><th>Fornecedor</th><th>Contratos</th><th>Valor somado</th></tr>
        ${p.fornecedores.map(f => `<tr><td class="forn">${esc(f.nome)}${f.doc ? `<small>${esc(f.doc.length === 14 ? mascara(f.doc) : f.doc)}</small>` : ""}</td><td>${f.contratos}</td><td>${brl(f.valor)}</td></tr>`).join("")}
      </table></div></div>
      <div class="box"><h4>Contratos mais recentes</h4><div class="scroll"><table>
        <tr><th>Assinatura</th><th style="text-align:left">Fornecedor</th><th style="text-align:left">Objeto</th><th>Valor</th><th>Vigência até</th><th></th></tr>
        ${p.contratos.map(k => `<tr><td>${esc(data(k.assinatura))}</td><td class="forn">${esc(k.fornecedor)}</td><td class="obj">${esc(k.objeto)}</td><td>${brl(k.valor)}</td><td>${esc(data(k.fim))}</td>
          <td>${k.link ? `<a href="${esc(k.link)}" target="_blank" rel="noopener">Ver no PNCP</a>` : ""}</td></tr>`).join("")}
      </table></div></div>
      <p class="hint" style="margin:0">Fonte: PNCP, Portal Nacional de Contratações Públicas. Mostra os contratos publicados ${p.dias && p.dias < 365 ? "nos últimos " + p.dias + " dias (o PNCP demorou demais para responder sobre 12 meses deste órgão)" : "nos últimos 12 meses"}.</p>`;
    }
    html += `</div>`;
  }

  // CAPAG
  if (cap) {
    const nc = nivelCapag(cap.nota);
    html += `<div class="painel" data-p="capag" hidden>
      <div class="capag-big"><div class="letra t-${nc}">${esc(cap.nota || "—")}</div>
      <div style="flex:1 1 280px;min-width:0"><b style="font-size:17px">Capacidade de pagamento (CAPAG)</b>
      <p class="hint" style="margin-top:4px">Nota oficial do Tesouro Nacional. A é a melhor e D a pior. Estados e municípios com nota A ou B podem pegar empréstimos com garantia da União.</p></div></div>
      ${cap.obs ? `<div class="aviso">${esc(cap.obs)}</div>` : ""}
      ${Object.keys(cap.detalhes || {}).length ? `<div class="grid3">${Object.entries(cap.detalhes).map(([k,v]) => `<div class="mini"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("")}</div>` : ""}
      ${cap.url ? `<p class="hint" style="margin:0">Planilha oficial: <a href="${esc(cap.url)}" target="_blank" rel="noopener">${esc(cap.arquivo || "Tesouro Transparente")}</a></p>` : ""}</div>`;
  }

  // Relatórios
  const ents = (d.entregas || []).filter(e => !e.erro);
  if (ents.length) {
    html += `<div class="painel" data-p="relatorios" hidden><p class="hint" style="margin:0">Relatórios fiscais entregues ao Tesouro. Quem atrasa pode ficar impedido de receber transferências voluntárias.</p>
      ${ents.map(e => `<div class="box"><h4>${e.ano} · ${e.total} entrega${e.total === 1 ? "" : "s"}</h4>${e.total ? `<div class="scroll"><table><tr><th>Relatório</th><th>Período</th><th>Situação</th><th>Data</th></tr>
      ${e.itens.slice(0,12).map(i => `<tr><td>${esc(i.relatorio)}</td><td>${esc(i.periodo)}</td><td>${esc(i.status)}</td><td>${esc(data(i.data) || i.data)}</td></tr>`).join("")}</table></div>`
      : `<p class="hint" style="margin:0">Nenhum relatório entregue no SICONFI em ${e.ano}.</p>`}</div>`).join("")}</div>`;
  }

  // Cadastro
  const campos = [["Razão social", o.razao_social], ["Nome fantasia", o.nome_fantasia], ["CNPJ", mascara(o.cnpj || "")],
    ["Natureza jurídica", o.natureza_juridica], ["Situação na Receita", o.situacao], ["Data de abertura", data(o.abertura)],
    ["Endereço", o.endereco], ["Município", [o.municipio, o.uf].filter(Boolean).join(" / ")], ["Telefone", o.telefone], ["E-mail", o.email]];
  html += `<div class="painel" data-p="cadastro" hidden><div class="meta">${campos.filter(([,v]) => v).map(([k,v]) => `<div><span>${k}</span>${esc(v)}</div>`).join("")}</div>
    <p class="hint" style="margin:0">Fonte: Receita Federal, via ${esc(o.fonte || "BrasilAPI")}.</p></div>`;

  html += `</section><footer>Consultado em ${esc(d.consultado_em)}.</footer>`;
  $("#out").innerHTML = html;

  const abrir = id => {
    document.querySelectorAll(".tab").forEach(t => t.setAttribute("aria-selected", t.dataset.tab === id));
    document.querySelectorAll(".painel").forEach(p => p.hidden = p.dataset.p !== id);
  };
  document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => abrir(t.dataset.tab)));
  document.querySelectorAll("[data-ir]").forEach(b => b.addEventListener("click", () => abrir(b.dataset.ir)));
  if (abaInicial && document.querySelector(`.tab[data-tab="${abaInicial}"]`)) abrir(abaInicial);
}
</script>
</body>
</html>
"""


def aquecer():
    """Baixa as listas grandes assim que o site liga e renova a cada 12 horas."""
    while True:
        for f in (lista_entes, lambda: capag_tabela("municipios"), lambda: capag_tabela("estados")):
            try:
                f()
            except Exception:
                pass
        time.sleep(12 * 3600)
        with _lock:
            for k in ("entes", "capagmunicipios", "capagestados"):
                _cache.pop(k, None)


def main():
    na_nuvem = "PORT" in os.environ or "--publico" in sys.argv
    srv = ThreadingHTTPServer(("0.0.0.0" if na_nuvem else "127.0.0.1", PORT), Handler)
    if os.environ.get("PORTAL_TRANSPARENCIA_CHAVE"):
        iniciar_lista_orgaos(os.environ["PORTAL_TRANSPARENCIA_CHAVE"])
    threading.Thread(target=aquecer, daemon=True).start()
    url = f"http://localhost:{PORT}"
    print(f"\n  Consulta Pagador Público rodando em {url}\n  (feche esta janela ou aperte Ctrl+C para parar)\n")
    if not na_nuvem and "--sem-navegador" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
