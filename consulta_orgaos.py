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
<link rel="icon" type="image/png" sizes="64x64" href="data:image/png;base64,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">
<link rel="apple-touch-icon" href="data:image/png;base64,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">
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
.olho{{color:#c9d5ea;display:grid;place-items:center}}.olho:hover{{color:#fff}}.olho .f,.olho.on .a{{display:none}}.olho.on .f{{display:block}}
button{{width:100%;margin-top:14px;font:inherit;font-weight:800;padding:12px;border:0;border-radius:10px;background:#38bdf8;color:#06243a;cursor:pointer}}
.erro{{background:#371a1c;border:1px solid #7a2f2f;color:#f6766f;border-radius:10px;padding:10px 12px;font-size:14px;margin-bottom:14px}}
</style></head><body><form class="box" method="post" action="/entrar">
<img class="logo" src="{LOGO_ENTRAR}" alt="SigaLicita"><p>Área restrita. Entre com o seu e-mail e senha.</p>
{'<div class="erro">E-mail ou senha incorretos. Tente de novo.</div>' if erro else ''}
<label for="u">E-mail</label><input id="u" name="usuario" type="email" inputmode="email" autocomplete="username" autocapitalize="none" placeholder="seu@email.com" autofocus required>
<label for="s" style="margin-top:12px">Senha</label>
<div class="sw"><input id="s" name="senha" type="password" autocomplete="current-password" required>
<button type="button" class="olho" id="olho" aria-label="Mostrar senha" title="Mostrar senha" onclick="var s=document.getElementById('s'),v=s.type==='password';s.type=v?'text':'password';this.setAttribute('aria-label',v?'Esconder senha':'Mostrar senha');this.title=this.getAttribute('aria-label');this.classList.toggle('on',v);"><svg class="a" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg><svg class="f" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.6 5.1A10 10 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-2.6 3.5M6.6 6.6C3.7 8.4 2 12 2 12s3.5 7 10 7a9.6 9.6 0 0 0 5.4-1.6M9.9 9.9a3 3 0 0 0 4.2 4.2"/><path d="M2 2l20 20"/></svg></button></div>
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
<script>try{if(localStorage.getItem("score_tema")==="claro")document.documentElement.setAttribute("data-theme","light")}catch(e){}</script>
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
:root[data-theme="light"]{--bg:#f3f6fb;--surface:#ffffff;--surface2:#eef2f8;--line:#d8e0eb;--line2:#c5d0df;--ink:#132033;--muted:#526179;--dim:#7a889e;--accent:#0a7fc0;--accent-ink:#ffffff;--accent-soft:#e2f1fb;--good:#16803c;--good-bg:#e4f5ea;--good-line:#9fd5b3;--warn:#8a4b00;--warn-bg:#fdf1dc;--warn-line:#e8c27a;--bad:#c2302f;--bad-bg:#fde8e7;--bad-line:#f0aaa6;--note:#b04a12;--note-bg:#fdece2;--note-line:#efb592;color-scheme:light}
.marca.marca-clara{display:none}
:root[data-theme="light"] .marca{display:none}
:root[data-theme="light"] .marca.marca-clara{display:block}
.top{position:relative;padding-right:52px}
.tema{position:absolute;right:0;top:0;width:40px;height:40px;display:grid;place-items:center;border-radius:10px;border:1px solid var(--line2);background:transparent;color:var(--muted);cursor:pointer;padding:0}
.tema:hover{color:var(--ink);border-color:var(--muted)}
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
    <img class="marca" src="data:image/png;base64,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" alt="SigaScore" width="240" height="75"><img class="marca marca-clara" src="data:image/png;base64,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" alt="SigaScore" width="240" height="75">
    <div class="top-t"><h1>Consulta Pagador Público</h1><p>Veja se um órgão do governo costuma pagar os fornecedores em dia</p></div>
    <button class="tema" id="tema" type="button" title="Versão clara / escura" aria-label="Trocar versão clara ou escura"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg></button>
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
<script>document.getElementById("tema").addEventListener("click",()=>{const claro=document.documentElement.getAttribute("data-theme")!=="light";if(claro)document.documentElement.setAttribute("data-theme","light");else document.documentElement.removeAttribute("data-theme");try{localStorage.setItem("score_tema",claro?"claro":"escuro")}catch(e){}});</script>
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
