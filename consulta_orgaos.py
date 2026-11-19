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
def ler_xlsx(raw, max_colunas=40):
    """Lê a primeira planilha aos poucos (iterparse), sem montar o arquivo inteiro na memória."""
    z = zipfile.ZipFile(io.BytesIO(raw))
    m = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    ss = []
    if "xl/sharedStrings.xml" in z.namelist():
        with z.open("xl/sharedStrings.xml") as f:
            for _, el in ET.iterparse(f):
                if el.tag == m + "si":
                    ss.append("".join(t.text or "" for t in el.iter(m + "t")))
                    el.clear()
    folhas = sorted((n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)),
                    key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
    rows = []
    with z.open(folhas[0]) as f:
        for _, row in ET.iterparse(f):
            if row.tag != m + "row":
                continue
            vals = {}
            for c in row.findall(m + "c"):
                ref = re.match(r"[A-Z]+", c.get("r", "A"))
                idx = 0
                for ch in ref.group(0):
                    idx = idx * 26 + ord(ch) - 64
                if idx > max_colunas:
                    continue
                t, v = c.get("t"), c.find(m + "v")
                if t == "s" and v is not None:
                    val = ss[int(v.text)]
                elif t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(m + "t"))
                else:
                    val = v.text if v is not None else ""
                vals[idx - 1] = val
            row.clear()
            if vals:
                rows.append([vals.get(i, "") for i in range(max(vals) + 1)])
    del ss
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


def pncp_pagina(cnpj, ini, fim, pagina, tamanho):
    url = (f"{PNCP}/contratos?dataInicial={ini:%Y%m%d}&dataFinal={fim:%Y%m%d}"
           f"&cnpjOrgao={cnpj}&pagina={pagina}&tamanhoPagina={tamanho}")
    return http_get(url, timeout=30)


def pncp_contratos(cnpj, dias=365, max_paginas=1):
    fim = date.today()
    ini = fim - timedelta(days=dias)
    tamanho = 500
    try:
        d = pncp_pagina(cnpj, ini, fim, 1, tamanho)
    except urllib.error.HTTPError as e:
        if e.code == 204:
            d = None
        elif e.code == 400:
            tamanho, max_paginas = 50, 4
            d = pncp_pagina(cnpj, ini, fim, 1, tamanho)
        else:
            raise
    itens = list((d or {}).get("data") or [])
    total = (d or {}).get("totalRegistros") or len(itens)
    paginas = min(max_paginas, (d or {}).get("totalPaginas") or 1)
    if paginas > 1:
        with ThreadPoolExecutor(max_workers=4) as ex:
            for r in ex.map(lambda pg: pncp_pagina(cnpj, ini, fim, pg, tamanho), range(2, paginas + 1)):
                itens += (r or {}).get("data") or []
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
            out["historico"] = [f.result() for f in f_hist]
            out["entregas"] = [f.result() for f in f_ent]
            if f_cap:
                try:
                    out["capag"] = f_cap.result()
                except Exception as e:
                    out["capag"] = {"nota": "", "obs": f"Não consegui baixar a planilha CAPAG ({erro_txt(e)})."}
        out["avaliacao"] = avaliar_rp(out["historico"], out.get("capag"), out.get("entregas"))
        falhas = [h for h in out["historico"] if h.get("erro")]
        if len(falhas) == len(anos):
            out["avisos"].append(f"O SICONFI não respondeu ({falhas[0]['erro']}). Tente de novo em alguns minutos.")
    elif esf != "federal" and not out["avisos"]:
        out["avisos"].append("Não encontrei o estado ou município deste órgão na base do Tesouro (SICONFI).")

    if out.get("federal") and not out["federal"].get("erro"):
        av = avaliar_federal(out["federal"])
        if av:
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
        msg = erro_txt(e)
        if "timed out" in msg.lower() or "timeout" in msg.lower():
            msg = "demorou mais de 30 segundos"
        return {"erro": f"O PNCP está lento ou fora do ar no momento ({msg}).", "tentar": True}


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

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
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
        if u.path == "/diagnostico-pncp":
            q = urllib.parse.parse_qs(u.query)
            cn = digitos(q.get("cnpj", ["46395000000139"])[0])
            fim = date.today()
            try:
                d = pncp_pagina(cn, fim - timedelta(days=365), fim, 1, 10)
                txt = json.dumps(d, ensure_ascii=False, indent=1)[:12000] if d else "resposta vazia (sem contratos)"
            except Exception as e:
                txt = "ERRO: " + erro_txt(e)
            return self._send(200, txt, "text/plain; charset=utf-8")
        if u.path == "/api/pncp":
            q = urllib.parse.parse_qs(u.query)
            cn, en = digitos(q.get("cnpj", [""])[0]), digitos(q.get("ente", [""])[0])
            if len(cn) != 14:
                return self._send(400, json.dumps({"erro": "CNPJ inválido."}), "application/json; charset=utf-8")
            res = pncp_do_orgao(cn, en if len(en) == 14 else "")
            return self._send(200, json.dumps(res, ensure_ascii=False), "application/json; charset=utf-8")
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
.stats{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:18px}
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
    <img class="marca" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAoAAAADyCAYAAADduHTAAADl5klEQVR42uyddZwcRfrGv1Xd47IadxfiCUnQAIEgwZ3D/bA7juOQEzgDDvlxHIdzHG4HBHeXoAGSQEIS4i6b9d3Zmenu+v3R3bObZGazFu/nc/PZYzM70139Vr1PPfWKoG0hAAkowHJ/Ge04op1hqv6aUqMVajcQw4ACpegihIriwYMHDx48ePCwS0GlEXKhUFRbqJ+EELPA/E6Z+qxE6YwVG3KrEyQ8Zzn8qs0IWxvhBA2eM93/Chbs1k3q4kDQDkZZ4xB0F0hpX7lz/Up5z9+DBw8ePHjwsItCOExMIAClFAhWKpgulXhXWPLt6tLvf9qIa7UJEWwLAqhhq30KRvvCxcmDUPJshJokpBZHKRTKIXvKeZ8QzncL7+F78ODBgwcPHnZRKPullMMGBQghHJqklJVSqI+lko/WqMCrlH5d2YAImtuKALqSpAkQKR52soIrEGJ3m8VagHIuTkiP7Hnw4MGDBw8ePDSFFConjE5owqFQSlkLlMadCSP6MOs/r6JeSLO2JgHUAJv4FQ07QAn1VyG1vVAKpUxX5fNInwcPHjx48ODBQ1uQQSE1ISTKsn4S8NeakpnP2G9pmRrYAoI2QYePjby8oQVpn7hJCHGhAlCm5Xyc9J6XBw8ePHjw4MFDm8ICpRBSQ0hQ6lUT/TfJdd8uaElsYHMJoASsYLvd9pToDwkhBirLcDJ+heY9Gw8ePHjw4MGDhy1PBIXUNWVZJRZcWlcy89kGnK5JJLCppE3A9RI+tsLFw88XQjwjEB2VMg2b+AlP9fPgwYMHDx48eNjyECAkyjJBRKUQx+vhDiGjduBHsERRX45vcx/SlC+yPyhUPOxmKeVVyjIdBuod93rw4MGDBw8ePGwjKFCWrQaqKbX5Nb9g/vykw8+s1hBAJ8Nkgj9SXHYvQp6llGF6CR4ePHjw4MGDBw/bDQ80hNB1pdT7Qpqn1qz9cc3mSOBmFLwTJGBFisvuQ2pnKWUYHvnz4MGDBw8ePHjYniB0pQxDSDFRWfKFvn37BhzyJ1pAAO204kjxsH8g5ZnKSqedRA+P/Hnw4MGDBw8ePGxvJNAy0kKIvVaWh5+G0T4aabqhNUb+wsVDzxdSu1lZpglC3znIn1toWzo/oZmJMx52nslS/8pqDx48ePDgwcMO5dc0sAwh9d18YSOerl3zpl2+b4mVzQNuDAlYocLBY4XUPwblc363g3pF0YDfKZQy7ZqKykIpENIWQYXUQeoeB9yVoCyUlcKusG461iJASkQmuV24bQy98fLgwYMHDzuKg7NjAi3rjNr1Mx/PVixaZCGEgsKx0bCs+xrBAKen2w6W7euofMqyHbvlOHfNh9QjaMF8NH8MzR9F88UQWgChB5B60Cm47SlAu8j8wEzVAAozVYWZrsGsK8NMVWOlq7HSCRQWQmgI2aDakfLIoAcPHjx42L49HAiFoEqacs/q0umz2SgpRN/w/XZv36BM/EMIbYCT8bvjFHgWwkmINlCWgdR86IF8fNFO+CId0INFaP6oTfic21IoR9yxPPK3y80NgRYscmynntwpK4VlJDAS60nXrCZdsxajbj2WkUAIgZB+nCrseMqgBw8ePHjYHhkRWEqg5VnSvBsmHAQfWxu9IUP+NHjODBbstqem61OVXexvxyB/QoIyscw0Qkj0cBGBWHf88R7ooUKEFrQdtTJRloXbTWWDIfB43y7LAzf8P7Z6LIQEqSMQKMvATFaSqllFsnIR6aqVWGYSIXWE9HlE0IMHDx48bK9OzhBS15VlXlBb8sODDY+CRQMWJGCCDBeVfYAU+6DM7V/9ExIsE8tKI30h/LFuhAr644t2QmoBlDJRllFP9rwAfw/NYYbK+SkEQrgxoiZGXRnJisUky34mnViPEBKh+erf78GDBw8ePGwfsGwnxgohGV2zZmaJ46iUS/A0wAoV5h0tdfG77Z78CfsY2zKTSD1IpP1wYt32JVQ8GC2Q5wT3p8mUwBHOyyN/HppuZBvZjQWOTWl6mECsK8GCfuihAiyjDiNZ7vTo1r2h8+DBgwcP25EzU5YQWr5lWgkjsfZDWwWcrRoogKP1cHFqKkKOQVluL7nt0CmDZdSh6WFCxYMJFe+G5o+jrDROizrHaXvwsKWgbFVZaEjNj7LSJCuXULPme9I1q5HSD1JzYko9ePDgwYOHbe20hFJYJUJYw2vXzV6No/JpgBUpLD5ASHkNylT1EfHbE/eTKGWAZRAqGki8xwEECwcghESZKdyjOo/8edg6GxHbzmylWeELtyNY0A/pj2DUrrUTRrSdpHSmBw8ePHjYwZ2WsqTUY0qx0qhd+yVM0DU783e28kU6/k0IOQSs7YwA2kV6LSOBHiwkr/t+RDqMRkgdZSYd4ud1p/OwrczTPiJWVhqEIBDtQrCgD5aRIF2zFiHsuoIePHjw4MHDNuVS9o8ORm23/8IXpgAItxvcUSltjhDk2XVRthM2JQRYdjxfqN0wop3HIrQAyqjzYvo8bJ9Qlp09LHWSpfOoWvkFVrrazkT3joQ9ePDgwcO2c1AKpLBM9qkrm/mZI53pk4TQ8px2CNsJ+ZMoMw1SI97jQOLd9gWEQ/48xc/D9rrJsksSKSNJsGgABf2OwR/rhmUkvPAEDx48ePCwLWEKIdE0jgQ70UMpSx0qtqfyFUKijDr0YAEF/Y4iVDTQdqCug/XgYftmgSAEVjqB5ouQ3+dwIh1Gooxk/b978ODBgwcPW5lcKRSWUAfCaJ8gPrgw7NemgejlFMzbtt5JCJSZJBDvSbzHAfaRr5nyiJ+HHRT2lJJ6kNq1M6haPtXuP53pJOLBgwcPHjxsTbck6hDsKYNBOQDoul0UsBUSy6gjWDiQvN6HIYTPPgb2yJ+HHXfHBYCVriXcfhj5vQ+xK3BuR9EWHjx48OBh16F/SBEENVpqSo4Rdj+rbdsI18n0DRUNIt59f7uuH6YXN+VhJ+GBEiudIJDXi/yeh+Bk5Xsk0IMHDx48bE1YAhBK7SWVqQZnWOE2c46iAfk7wG7fth0lI3vw0JabHH9eD/J7HgwoFB4J9ODBgwcPW88TKbt83gCJYJhT+WXbeCHn2DdUONAhf2mP/HnYqUmgMhIE8nqS1+tgRwX0YgE9ePDgwcNWgUQplFKDJEIUuK5p2zjDJP5oF+Ld9/OUPw+7DAm0jASB/N7EuuyFslJeqIMHDx48eNiKfoh8Caqzk40otva3KyuNFoiR1/Mg53fecZiHXYgEpmsJtxtBuP2IBvUtPXjw4MGDhy0OJYG8bfTdgCDefSLSF3HUP4/8edjFSKBZS7TTHvjiPezWhh4J9ODBgwcPW8UDbYsAJKfWX7TTWHzxLp7j87CLwyLebQLSFwHLKw/jwYMHDx62PLZ+TzUhUEaSQH4fwh1GoNLe0ZeHXXoThrIMtEAesc572ElQHv/z4MGDBw9bgQBuVWeHZSH0ENEue9pqh+fsPOzyHNDJDC7sT7Cgv90yztsUefDgwYOHnYYACjvxI9JxFHog34v78+ChIQm0DKJdxiP9UbAMb0w8ePDgwcNOQACFQJkpfJGOhIuHYJne0a8HDxvAMpD+OJEOo7AsrwWiBw8ePHjYGQigk2oS6TgGhObVvvXgYZNNkkSZSYKFA/FHOqLMFJ5C7sGDBw8edlwC6Kh//ng3/PHutmPzCt968JBlo6QQmo9wh1F2lxBvmnjw4MGDhx2WADqd5sLthjuE0Bt4Dx5yb5aSBOI98EU7o8y0N2E8ePDgwcOOSABt9S8Q744/1tU71vLgoQkbJiE1wu2GojwV0IMHDx487JAEUNivYNFA59jXC/7z4KHxOSOwnJAJPVTkZct78ODBg4cdkABaBnqwAH/UUf+8zEYPHjYPZSG1IKGCfjYB9GJmPXjw4MHDDkMAhcSyDAJ5fZB60A5q9+DBQxPmjt0hJJDfG80XBmV6Y+LBgwcPHnYQAqgspBYgkN8LpTwVw4OHZjBAlJVGCxbgizjJIN788eDBgwcP2z8BtBUMPVSMHiz04pg8eGghAnndM3PKgwcPHjx42L4JoAClTPzx7gipe7kfHjw0ew5JsAx80a4I7xjYgwcPHjzsEARQKaT04Y92QinTEy88eGjRNDLR/FF8oWIvGcSDBw8ePGzvBFDYjisQd8pYmHgM0IOHlm2khNDsotBeEpUHD1sUQojMqznvFd7GzMMOCH0L8T+UaaKHihFaAGUkPeXCg4eWeSSUsvCF2yOEBsqLpfDgoa2haRoA6bSBZZlIKfH5dEBgmmaWfZkibRiOrCFQKHRN84igB48A2mqfQgsWIZzJ4SmAHjy0EMpEC+Qh9aCXDezBQ5vur2z1rqq8EgTkF+YTCYeoq0uxvqQUlEUkHkNKuQER9Pl8FBUWZFR5IQRV1TWkUmmPBHrYxQmgUggh8YUKnVZW3oTw4KGlmymlLKQvhOaPk65dixA+vKwqDx5aT/4spairruGoIydx5mnHMnTIAPLzYiQSdXw/YzYPP/Y8r7/5Iel0mkgsis/vo3zVGn556dnc/LerqEsm0XUNy1QcfORZTP9hDuFwCMvywjU87LIE0EJoQbRAnlf82YOHNthQSc2PFsgnXbMKhN87CvbgoVXbKnteWak099/1d84/5+RN3tOtayeOnDyRjz/9iltuf4D3P/qCmspK2nfpxCUXnkY8HiVOFIAVK9eweNlK/H4fypubHnZdAihQmGh6EKEHHYncUwA9eGgFA0Qh0AIxbyg8eGgLL6VpVJdVcMc/r+f8c07GMEyEACFlxltZloUQggn7jGPCPuOY9u0PLFq8lDFjhtOrR1csS2FaJpqUfPjxF5SuLSFWkJ81ZtCDh12EADqKhR5ESL9Xu8yDhzaC9EW9QfDgobXzSJPUVlYzYb89+PXFZ2KaJpomM6TP1e+klPYxsUMEx4weypjRQx0Xp5BSoJRESsnTz72GkBIvNMPDrk0ABaAsNH8MIaRXu8yDh1bPKZGZUwjdO/714KFVLkpgmSaX/vL0BlPM9lFuNrAL0zQ3IILKiW+XUpBOp/H5fLz7wVTefecTwrEopumFPHnYlQlgZkZJj/h58NCmPFB6g+DBQ6vmkKAumaRrjy5M2GcshmmiHNWvprqW2+74D4sWL2PP8aM45aQjKciPZ4igC6UsTNPOBF6+YjUX//o60GyS6MX/ediRsAU8il32RfPHEU45GA8ePLRyTikT6YsgdT/gqQwePLTI4UlJujbB3nuMpl1xIbqm4fP50DWNq/54M3//8w08+cwrXHLJHxm3zzHcc/8TVFfXoGnaJq+PP/uag488iwULlxEMBb3MXw87HLaYAiik7o2uBw9tOqk0W1nfXi9PCEf037AzglLKUUbUVj+9ru/UYF+Xc0Uo1fC6PGwLW8k8EVEf1bCln4dSCk3XSaXSTHnlHZJ1KXRdI5Go45XXPyBU3Bm/z4cQsHTFai759fXcff+THHHYAQwdMgApBUuWruDTz6fx3gefY5gm0VikVYkf9li05X23TonMOmeUQjWYNx52knkYLh7atk9TSCwjQazLnkQ6jMYyEtu10/LgYceYqRIrXUvZz1NQZortJbNeCIEmJQowDIN0Oo1hmGBZtlcXAqRE13V8Ph1dtzeGbjzVloCUdoyWaVkYacPu7mCaYNkkFCmRmobu0/HpOpomUZbCbAMFZ3NFgFt7z1vq87fGdduxdGCZFoZpYppWhoQLaduRq64JAZaltoiqJoSgri6JWZes/6UmieXFUcoilTJIG2mU5dxzog5U0tZLpA66hvD7iUbCKNW6a9xyZEo0OQLLfTZgH3Wn02nShgmm6TBzAVKgNZjDUgos08LyyOAODU+m8+DBQ8uInyapq0tRU1MDUiOvMI+eXTtR1K6IeCyKrmuk0wYVlVWsX7eelWvXU1VWAUoRiIYJBPwZEtAW0DQNy7Korq6BZAoZDtK+qJCOHYopLCogHA4hgNpEHaXry1i9poS1JaWYtQnw+wlHw2iaxDRapua4pDbn7QjQnISClpIFo5FrE2JDZ95UmKZld2tSjY9tSy7bJeN1yRSp2gSYJnooRDQWJT8WIRQIIKUglU5TWZOgorKK2qpqMC1kMGA/MyHatLSKUopgMIAWCaFpGoZpUlNdS9X6MvRwkPbFRXTsUERhYQGhUMghjHWsLyll1dr1lJSUkkrUUaVqiITtz2jR9Sl7XO1s4racm00nz+4cramsBssknBene9dOtGtXRF5+HL/Ph2WaVFXXUlpSysq1JZSuL4O0gR4OEXKOvj1V0COAHjx42AWgaRrpdJqasgo6de/MYaccxaSJe7P76KG0Ky4iGg1v4nCrqmtYu3Y9X0+bwdvvfcbb737MmhVrCOXF0HWtVdmTLvGrKivHHwyy9x6jOXTSBPbaYxQDB/QhPy9OIODf4G+SyRQVlVXMnbeIL776jrfe/pgvvp5ObXkt4bxYs5y6pmlUl1fw68vO4YzTjqWuLpkpK1JPDhU+n86vf/d3Pv/iWyLRSJOVIykltTW1jBqxG/fc+Vcs09yERJqmRTDg57kX3uAft91HNC++2euXUlJTXcOtN17N/hP2IJlKoTUgj7aAK7Ask9PPu4rFS5YTCPib5OzdDUJ1dS1WMkWnbp3Y+7ADmLDPOIbu1p+ePboRjYbx+XSkEBiGSTKVYtXqdfw0Zz7ffPsDH3z8BT/8OBczlSaSF7NV3TbKspVSkjYMKteXEYpG2W/v3Tnk4P3Yc9xI+vXtRV5ejIDft8Hf1CWTlJdXMnvOfD7/8jvefPtjvpk2k3Qy1ezrs22mklv/cQ0nHn84qVR6E5tpCUzTwu/38cB/n+GGG/9NND8vqx1IKVFKUbm+nHhhPoceewiTD96P8WNH0rlze2LR6CbXU1OboLS0nOkzZ/PBR1/y6hsfsGDeQnzhEMFQoMUbJw8eAfTgwcOOsGDoGpUV1RQWxPn9lb/knDNPoEvnDlnVsIbOJh6LEo9F6dunB7846UiWLlvJAw89w133PkZldQ2xWLRRdavx66kiGAhwxmnHcdEFpzF+3Iisqo9LXIQQBAJ+2rcron27IvbZawxXXXEB0777gfsffJqnn3uVqooqYvkxLNPcrDojBCjDpFeProwcPrjR9xbkxVCm1SwVUDjjGYtF2X3U0Ebf+933P6LSRpMiBIQQKMNg8MB+jBqxW6PvDQUDWMpqUqarpmmkUilqymsZMXoo5599EkcdcSBdOnXY7DW1b1fE8KEDOfmEwzEMg08/n8aDDz3DCy+/QyqVJp4XwzCMVqhjAqlJqsoriUUj/PLC07ng3JMZmeX+N7aZYCBAxw7t6NihHQdM2IM/Xn0Jn02dxr0PPsnzL75FyjCIx5t4fQKUZdGxfTu6de3U5vO0XXGhY2fZn08iUYdQigsu+AWXX3oOgwb2yTKHnZAJ5/4j4RCRcIhuXTtxxGETue73l/L0s69y8+0PsnTJUrsItkcCdyh4wXkePHhommqha1SWVbDXHqP4+N2n+dO1l9KlcwdM08I0TSyr3mHaR38ycxxpx0opTCf2q3u3zvz9z1fw4TtPMXL4blSWV25Sg21zjlwIQWVJGRP2Gcd7bzzOow/dxvhxIzJHpfVHsipzNOrWdHN/b1kWhmGilGLMqKE8eO+NfPTOU0w6aB+q1pc599IkNkUqlcKyLNLpNJZlbfAyDMP+2YqjTPez7LHe8PPd70wmU80rvyUEiUTS+Qxjg880zQbX3sQjPl3XqaqopCAW485//ZUvPnqeiy84lS6dOmTG2v58lXkGDV/u/Zmmia7r7L/veJ569A7eff1R9t5zNJXrSzPPsCWqn1KKqvXlTD70AD5+92nu/fffGDlitxbZDMDee43hyUf+yXtvPMZe40dRWVKKlKLJ15dKp7Es5Yy9avXL/Zx0Op2TnNdU19ClYztem/If7r/rBgYN7LOBXdXP4ez37873gvw8Lr7wNL78+DmOP3YyVevLmzWHPXgE0IMHDzsC+dM0qsoqOfSQ/Xnn1UcYMrg/hmHYWZWazMQy5XJ8tjMVTpC/dByuwcjhg/ngzcfZd59xVFVUNcmBSCmxTIu62gR//vMVvP/G4+y1x6iMAxNCoOtaxnFluyb391JKdF3LFPo1TZMxo4by9isP84+bfk+6LkU6bTQprq4hYcj1Eq2sjbolPr+ho8/1avIGoaSUA/bbg88+eo7LLjqdYMCPaZpO5wyZeS6urWz8kg0SQVA4ZMNi371254M3n+Caay6lurI6Q9CaM26pVBozbXDHP6/ntSkPMnLEbq2yGZeUm6bFPnvtzodvPcnvf38ZtVU1KMtCNGHj4M6Ltn5lu35NShI1Cfr06sb7bz7BpAP3zpBed9wbsyH3/t357pLmTh3b89xTd3HxxWdQVeqRQI8AevDgYedZJKSkrjbBoMF9eerROwiHQxmFJpuzcFWSxoLDbYerY5gmefEY/3vi3/Tu2ZVkoq5RxU1KiWkYCGXx5CO3c/0ffpWJvXIdWGvu040ntCyLq397Ac89fTd+J+axNZ+9K6jDVSWlnH32ibz96iP06dUto6zaCSQtIL4Ch2xITNNC1zVu+uuVPHDPDaTqktBEEiilJJVMEQkGeOl/9/HrS87KPOO2sZn667vhL7/lvw/cgpFKN/uof0tCCIFhmoTDAZ594t/06d0dwzAypHeTOUx9Iknjc7h+vtx9x1+YdMh+VDdxI+fBI4AePHjYAWCaJrfddC35eTEMw8y5wJuO02uoSLkKUDbomoZhmHRoX8RNf7+KVF2yUQXCsixMw+CZx+/kpOMPz8RbNRZA35CQbo6YZlQ2ITEMgyMnT+S5p+5GKnsMhNfdKAv506laX8YZZ57Ifx+4xU6gsayMstrYc2n4avQ7nOdrGAbnn30S9/z7b9RU1Wz2eQghMA2DoN/HS8/dxyGT9iVtGJtVNptrM+71pdMGZ552LI/851aSiTrnGlo3vtmOypvy2vj6EhVV/Pby8xk5fDBpw8iUZNoYlmUhqFeGG7bByzVf3H/6161/JBaPYhiGN1c8AujBg4cdeoFwMkX3GD+KQydNwHIcey5HpWn2UdvsOfP5YdZcyh01oDFnoGkSy7I4+siDGDl6KDU1tVmds5SS2soq/vV/13PE5ImOgqHn/Gz3+DDb0axNTK3cWZvCjmdLpw0OPmgf7r7zrySqqxGeCrjRs9Oorqhkr33G8cA9N2SIgpZjnOwYsvpj14Yv9wh+s6qxYXD+WSfyu99eQHVpOZquNUoAk4kk/7n/H+y791jShoFP1xvd6LjXlt1mzJzZ20IIfD6dtGFwyolH8I8brqamrALZSjUs21F5rpc7HxqSO7v9XZoOXTty4TknOc8n9xx25978BUuYMfMnVq8t2SAOMNccNk2TgQP6cMxRk0hUVnuK+Q4ALwu4DbFBs4HMjPKa4XnYkQmgQCVTHHHYARnSlG1dd2OyXnzlHa67/nYWr1iFUor2hfkcf/zh/OWPvyIUCmaN3XKdv9/n47CDJ/D9NzOQ0QgN/ayua1SuL+fMM4/nl+edgmEYOVVI10m5qkxlZTUrV6+16wMCsWiEzp3aE4tF6xWPHHFfPp+GYRicffpxfPb5NP770NPEiwpalLG80613QmCkDfLzYjx0700E/P5MPFkuZck9Zgeoq7PLqlhKkZcXIxIObfLeXKTTNC3+fv0VfPTJV3z3/Y+EI+FNCnnruk5lSSm/vfJCTjjmUIxGyF+9zdjXVlZWweo1JVTX1CKkIB6L0LVzR8LONdo2I7Oqe7pTW/DKy8/j08+n8cor7xDLUY6lKfjlpb9n3twF+PxNaTdnH7kvXbmaQDTizFdJqqaKfQ7dnw4d2uUcW/f330+fxa9++1dmzpqHaRpEoxEmThjPP/5+Nd26dmo0/lIpxdFHTOLRx57H83weAdwJF70GOzLHxN0MR8stAqs2fb8UAiE3/Jtdsa2O22JICJxe0ZtbznDaENF4kV0PWwSWpRABPyOGDco8v1yOY/rM2Zxy2q9ImiaRcAgFrFy3nltvvJ0VK1fz6IO3YFkqq/NxlZ9RI4eArm3wnN3ODd16dOGWG69xVAotx7WoTAzhex9M5bHHX+DLaTNZvbaEhNP5IRQM0LFDMXuNHclZZ53AhL3HbkBiN97WSSfg/ea//Y533/+MNevW4/P5dvnit5omqSmr4Pp/XMuA/r0xzcZDAzRNYpgmzz3/Bi+/8g4/zFtARaldGDyWF2dw/14cdcQkTj7xcPx+X+Zvsq0hYNe7u+mvV3LI4WduQjXc2omDhw7k+j/8KhPv1xgxBXj19fd58qkX+eb7H1lbUkYiaYckRIJBOnVqz757jubcs09i7JjhOW3GXe8B/nnzH5g69Rtq65JITbZo/fri6xnM/GYGIhJGNaV2pALd78vUbBQCME1GjxqS8VUbT0H3PsrLKznx9F8z/6d5RArzUQgqK6t56tHnmfXTAt5/63HisSioTdcCpSyUgoH9e5GXn0fKMJBCeDTQI4A7PmmR0m5YmTbtVkGptF32QtPsrKiAT8PvdxqFOxPDUhamqUilTepSBmnTxLTsCenTNQI+DZ9PQwqB5UzMnU0RFQ0y0kzTIpU2MQwTw3RabzmdhsQmpM9dyO3sNV13XpqGJkUmSNmrQL+lCaBFMBSkk1PDLdvG330Gr77+AclEHQUdikgm046C5sPfsRPPPPca1117KQP6986+EDnHeIMH9CYYDGyg5thEo5arbrqW9u2KMEwTPYszd8lfeUUVl/z6Op569lWwTPRQEJ9PzxSDNkyTRUtW8POcBTz69Eucc+bx/Pv/rs+pUErn6K+4uJArLz+fX//6OoLFu7YKKKWktraOfoP6ctEFpzaq2LlE7ptvZ3LJr6/nm6+/t9vx+f32cxSwen0pc36ax5Qpb3LHXQ9z951/YY+xI3N+rpusM3H/PTnwoH15++2PNih+LYTASKa47veXEYvavXobU71WrV7LhZf8kVdfew8E+IK2zQQDAQBShsH8hUuYM2sujzwxhUt+eQY333AVPl3LbjPSPhLt3asbF11wGn//+7+It9BmIpEwejxKJBJucvHwhmujUoAm6dKpfYMev5u+X9MkX379PfMXLCavfbtMKRmpaRR07ciMr7/n7bc/4RcnH5lrSwDAgP69KS4uZMnylQT8frxdu0cAd9gdLkqRSBokkgaagKKCMAN6FtO/VzF9uhfQpX2czh3iFOaFiIR86JrMKH2WUpiGorYuTXllglXrqlixtorFK8qYt2g9i1eUsWZ9Dem0id+vEw7qaFJiWtYOO2fc4GGEwDAsErUp0mkTKQXRcICOxVE6FEXo2D5Gh6IohfEQ4ZAPv09D04QzZhaJpEFVTYqS8lpWr6tmTUkVa0trKKuoI5FMo0lBwK8T9Dt9KR0F1kNbbnxcx6ARCgUaPOHsSKZSmb/ZwMFil/246/4nGDtmOMlkchNnrJRC13WWLl2Bpum2U3UuIlFbR9+BfTjrtGNzxpe5SkdpaTmTjz6XL6d+TaxdMTi9ZxsGxkunEHQ4HEQpeOi+x1m6eDlT/ncf4XAYyO7QlVKcedqx3HH3wyxfvhp/E7ti7JwEUGDU1nLOGScQj0UbJViaJnnvw8857qSLqKyuIV5U6DyP+nXO79MRoRBCCr7/4ScmHXYmr055kP32HUcqnc70m264zhimiSY1zjrtON5684MNyWl1DaN3H86xRx2cU3V2yd+y5as45PCzmD1rDvHiokwCyAY2I+1C0DJsk7B/3nIPy5at5MlH/pnZvGRTApVSXHjeKdz/0NNUVteiO4SxuZswN161xX2HhSAUDG72bclkehPCppQinTLwRcI8O+UNTKWoSyY3mYe2Mi9IpQySqVRGBfXgEcAdjviZpkV5ZR1SQp/uhew9qgf7jevF8IEd6dIhjiZbb9zrSmv4Yd4aPv9uKR9/s5hZP6+lti5JNOwj4Nd3KCIohECTgrRhUV2dxLQUBXkhxg3oysjBnRi9W2f69iiiR+d8omFfszPE0oZJSVmChctKmbNgHd/8sIIZc1ezaHkZiaRBKKATCvoycWoeWg+3FZhpWiQSyQb67KaODuDQSRO44R93U1eXIhj0Z8qAWJZFIBjkrnsfh9RDjadFapKwE5unAF2TpGtrOfG4yUQdJSfbUZ6rwpxz0TV8OfUb8jt2IJVKZb8v5+ZcNSa/U0feffNDLr3iLzzywC2ZmMCN79E0TfLiUY4/+hBuveVeQuHgLqkCCgGptEFecREnHndozpgwdxwXLV7GqWdeTm0ySTwvnrVThr3OmWBCPB6jtqaWM869ku++fIXiooIc67RtBycdfxjX/a0fCxcvIxjw22WB6pKcdvKR+Hx2qSF9o6QHl4TV1SU59ewrmD17Hnnti0mn0jnngkJhGQZCQH7nDjz/9Iv07NGVW2+6JqvN2CqgRdcuHZl86P488vCz+AsL2rSvcdMnMyTq6hol9Eopxo0dQedOHVi5ei15+XmYppEpB+MPh3jtrY94Zcqbm01tDsaj+HTdO6HxCOAORPykwFJQVpEgFvFz5MQBnHjIEPYZ04NYJLDR4ubsDkWDSDaRNQckc5ypMl7VPtZsVxjhgPG9OWB8b661LL6ftYoX3pnNqx/OZfnqCqKRAAGfhrEdExrpHI8nkga1iRQFeSEO3KsPh+zdj31370nvbgU5drVOuYJGNCWFG09jH5l3ahelU7soe43qzrknjKa6NsWPP6/h3akLeXfqfH5asA7TsohFAhkS76G1So+tpqxctYZhQwZk3ZBIaWfx7rXHaO7999+4+vc3U1FSSiAaceKQwLJMYvEIQkQ3SzobOkjTtPCHwxw5eaLjTDa1FpcUPvXsK7z8wpvEOxTnJH/ZkEqliHdsx6OPPs8JxxzK5EP3zxF/ZjvJww87gH/e+fBOF7LRHJuoqa5inwP2oXev7jkVNpc4X/XHW1i7ei15xYWk05tvk2YYBuFImGXLV3Lxr6/n6CMPIpGoy6L8ChQKv99Hfl7MLr4sBOm0Qay4gMmH7J9Zo7IrkxoPPPQMn74/lbxO7XKSv2w2mkqlibZvx7/+/TDHHX0w48flOq6217kjJ0/k0UefoyWJEQ2LYzdl47wxwbRDJk1WrFybM47aTcTq2KGYJx+5nbPPv4rFC5agRcKEQ0GEsOdiOBRERkJNeIamF/vnEcAdaCA0SWV1Er9P49Qjh3H+iWMYPqBj/aSyFDgSN25MYBOSGDbNDBYb7ELtdot2/MWYoV0YM7QLvzpjPI+/PINHX/yeFWsrKYiF7KOs7cjhCIfE1talSdSl6dujiOMmDebYSYMZ0Kt4AxJnOWOXybQUNHn8NlQI6hNnhBBEw37GD+/G+OHduPr8vfl02hKefHUG705dQGVNgrxowI7fsrylqOXOXmAlU3w/YzaHHLQvity1wCxL8cvzfsF++47n7vue4MVX3mbF0pUgJcFIKNM9oKnEXEpJIlFHv749GTpkQH0sbpb3GYbJXfc9gQz4mxYon03tlJI773mUww7ZL6ujdTssjBg2iO49urB02UqCTlzhrqUACkgb7Dl+lDN2Fm78V0OCJaXku+mzePW194nk5zWJ/DUkgdF4jOdfeovnnn3Fabicex77o2GCoSAKqEvUMX7sCHr36rZBWZOGz1rTNGprE9z30NPosXCLethKKUmnU9x572OMHzcy53uEEIzbfQTFHdtRWVmds/5eLlRVVWNUVlCRTsPmbFsIgpHwBqTXjgHUmPbdDznnkHutSin223c833z+Mvf/5ymeeOYV5syZD6aFPxIiEAg4GzrLU/c8ArhzODilFOvLa9lnTE/+8Mt9GT+iW71K5ewgtWYSlqYupFoDhuiqYp3axbjqvL057cjh3Pqfz3jy1RnomiQU9G0XaqCmSdJpk/LKWgb2bse5J4zmhIMHkx8PNSC2KpMN1xZjJ5xxEg3GyyWDSin8Po2Je/Rm4h69mTFnNQ88O42X3vuJZNogHg16CSMthGUpZDDAi6+8w1W/Ob9RBcKOxbQY2L83/779Ov5w9UW8/tZHvPjKO3zx1feUrikBn044GkF3FNrGnokQAjOVYrcBfQiHglkVFvd3P86ex/czZhGMhFuk/JqmSTAa5ouvpzN/4RL69em5QUaxez1KKeKxKAP79WLhgsWIUGCXswmlAF1n1MjdGrcbCS+98i7JmhoCocJmH5dblkUkEkbGIuQ8KlDuBt3KkD2VMhg2ZKBTLmbTkAE3LvHLr6czb+4CQtFIi2LrTNMkEAnz4SdfsXbdetq3K8p5HN6xQzF9e3bjy2kz8fl8zfqeifvtQY8uHdEDAVSOzax7WpI2DL78ZjqJRDJju5ZlEYyE+fjTr1i+YjVdOnfImVzjKoHFRQX84epL+PWlZ/Pu+58x5eV3+PCTL1mxbCUAwWgEv0/P9HX24BHAHQ6aJqhLGigE1126P5efsYcdP2JZCETTmsC3MRl1j5ksS9G5fYx//v5QDtm3H9fc9g5LV1WQHwtuMxLoErryygTFBRF+c9aenHfCaOLRgLMg2v0vpRCZTOgtrULai62wd6VO4sDwgR25+/rDOfu4kdz84Ke89/lCIiEffr/uHQs3mwBaRKJhvvl6Ok88/TJnnnYs6bSBz6c3ogTazrhjh3ace+YJnHvmCSxavJy33v2YF158i8+//J7KsgoCMfuI2C4Bk4P0myb9+vWyr0WpTSrXuwTym2kzqauuJV6U3+K4PF3TqCqrYNq3P9CvT8+sypZpWeiaRt8+PWAX7XZgmiahcIjuXTvVK4JZNokAX349HVpRMscmGM3dKCr69+uZm8A6rPGrb2ZgptLOxqUlRFjh8/lYs3Yd02fOZtLEfTKVITYkVCZSavTq3YMvvvi22aVR7rjtuia/N5VOM3T3ySxctCxTBsa9zpI1Jdz8fw/w79uvwzDMnMf2rhJoWRbRSJhjjpzEMUdOomR9Ge9/+DkvTHmD9z/+ktI1JWiREOFQyFMEd1QBbJdlvpqkujZNQTzE0/93AlectaczWe0sw61N/jZRBjXpHJcpDt67L68/cDoTdu/J+vJadG3rP7ZMYkxVgqMmDuLth87girP3JB4NOEqO/Z5tlfllH0mL+oxgSzFmSBee+9fJ/PtPk4lFAlRU1W2TsdsZVMBgOMRvr76RGT/MyXQ7yL2RkQ2Oe+1ySb16duWi80/lvTceZ+qHz3L55edRmBencn0ZlqLRrgFdu3TcQO3JhvkLl7S63IQbKzV/wZLcX+f8skuXjrtknVshbBIcjUYoKMhryLo2IEZCCGoTCZauWIW2FZMBlAKktJ9PrntwpMSf5y8CIVplNnah9DSLFi3fYEOy4fyxf3bu2M7+D0GzbMdNwmjs5XYoqa1NZFUzTdMkkh/nnvuf4KlnX7XncNpotOtKw5AN07RVwZOOn8z/nrqbrz99kRtuvJr+vbpTVVpGOm14/X89ArijKH+SqpokPTvn8fK9p7Lf2J4YpoXIxKZtL4utQNPsLMzO7WM8888TOeHQIVudBOq6PV7BgM6df5rMwzcdQ6+uBRsQv+1JCJENiKBSitOOHM4bD57OAeN7UVJe68RyeZO/OUqH7vNRUVXNkcdfwHfTZ2Uy/DbXukvTNPtZKIXhvHfk8N34561/5OvPXuSaay5FKOwg/2wORAhi0Ugjao6NsvLKNrvfUvezGnHS0Ui4rSNCdhQKaGd1B/xEImHnN9lRUV5FdWU12kaFvbewtYIU5MVjjRN912Zavd7bsYml5RWNviVjM6ql65ls8qsxBEN+zr/oWp5+7jV8PrttnJut35gYUS9I2ESzT+/u/P6qi/n6sxe5+99/p31xAVUVlY225fPgEcBtT/6kIFGXpkuHOM/ecRJ9uxdimtZ2rQzZvVIVPp/k/r8cyQmH7EZpRWKrXLOuSUrLaxk2oCOv3ncapx0xPFNzb3sjftkWTiEEhmnRq2sBz/7zJH579p5UVidRCq9OVTNgWRahcIiVq9ZywGGn88BDzzg2oGVKpJiNNYwXAt15r6tYdO3SkZv+eiXvvf4ofXp0pbqqegMH4n6UcJ10I4/LMMxmKyu5P2vzyQq7tO04iu3myIZhGpim2aSOP20+98Xm18a0adJWLL6xBBfRgExt642c1DSUEJxxzhVc86dbKa+oRNcbzOEmbehsf2QYJtFohIsvPI0vPnyOAyfuRVVpec5e4R48Arht965CkDYtAn6Nh/9hq1hGjnZD2yOZUZa9e/33nw5n79Hdqaiq26LXrmuSkrJajj5wMC/dfQoDexc7vSXFDuUAdYdACwHXXbI///rDYaTSJmnnXjw0DaZpEgwFSSZTXHjxtUw48GSeePolKiqr7DIVTsaj7UgaIYMNjogNw2CPcSN5943H6N+3J7XVtRliIRxCl3GujZC7UChIozWFmoFIaPMFc9OGsYu2OlVOSRBzs0TZ7/ejb/WWebYil2ykDJB7NZFgsA26VNh/H2mkNIr7Fc3Jgt543hmGYXdQasKr8Y2cQmqSQDjEzTffzfh9j+P//vUQK1et2aDUjH3sazYyh0WmqLVhGHTt2olXnn+QyZMPpLKswlMCPQK4fZKo6poUN/zmQEYM7ISxnSt/WUmggmBA557rj6B9UZRkymBLcBhdk5SU13LeCaP5743HEIsEMJ12QTvqswd7YTvtyOH898ZjEEDa8Ehgc+DWT4sV5DP1q+85/azfsPueR/Pbq29k6ufTSDmxQLY6LDJHRrk2ZLquYxgG3bt15rmn7iYWCduqkbvBUIp1JaWbVVc6dShuMwLRsUO7egKa4wvXlZTtmgbgqH91iSTVVTUbEJyNUZCfR35BHMM0ttpJgbCNlLUl69kcI+vkxOS15trcNmsd2hXntBn3d+vLyuv/oxnfqWkauq6j61rOl89n/3thQd5mlVmlFMpSxIsKWLB4OVde+VdG73E0Z19wFa+9+SEVFVXOsW9DMtj4HDZNk1AwwJMP386QoQNJNNjIediOxZFd5UY1TVJRVcfk/fpz6hHDt/tj38aIjGladOuUx98vn8h5f3yJQCwEymrTsSopq+WXp+zOzVdOyhQP1XZwoiQETkN6i0P37cd/bzyGs38/xS76K6XXSq7JTs+OBYpGI3anh2Uruf32B7jz3scYPKgvB0wYz8EH7sNee4whFos4Kobl1ETLsgg5JHDobv254vJzue5PtxEvKnAC+gWLFy/PTcgcDB06EFoZa2ZZChnwM2zYQHJ5aVf5XrxkGWiyzVTHHYj/oWkaVdU1rC+roDdsUqDbPeb3+3306dmNH2fOQYTb6Hy+KQxQwRLHZnJrdo7NtJKZuqVghgzun7n3Tdds288sWbqi3kab8bX3PfgUy1asxu/PraYqZfuGZDLlELjNt5wzDJNg0I8MBymrrOKRR57jkcen0LtPd/bdc3cOPWgfJkwYT4f2xZnNX6aWaxaSapomeXkxbrnhao44+jxvofQI4PYD07SIhHz84ZcTck7UHYnMmqbFsZMG8783f+TdzxeQFw20ScFjXbfJ35lHj+DmKydhWgop2KnKXegOCTxorz7c9afDueBPLxMO+dhKLmqngavsuX11Tcvix59+ZuZ3P/Cvux+lf+8eTD50f8464ziGDhnY+MbGcVhnnnos/7rrEWoSdXa9NF1nzrwFGzjSbM513JhhdOzYnrKKqhb1WxVCkEql6N6tM6OG75bZbGX7Pksp5v28GOHz2aVptvDcUA6zacoGRff7mhELqUDKnCV9XHbr9qB1Y/mklKQSCeYvXMLuo4dm/TK3DuC+e4/j5SlvIoQEmr9JdWsvNmVTItyMXk0ya8783ITM+d0+e44hVpBH2jBbxOGllCRqEwwZ3I+BA/tkrQFYnxFdx4KFS9H8PrtjSTPEhzvveYSfpk8HXwQss3HmKwTBeKzJJzV2hrGJrmv4C/MBxbLlq3jkkf/xyGPP0aVLJybutwdnnn4cB+y3xwb3lI0EWpZi0sR9GDF6CN9/P4twJNzy/sUetrygtKuof5XVSY6eOIhBfdo5RTDbbtHOpMo7yRFuIWTTUpstdtsqOQu44uw98fk02qIWp6ZJyioSHLx3X27//aH2Ip5jx7ezkMCjDxzEX351AOWVSaRXIqYR28h9/OQ6G4EgHAoSLy4kEo2wYOkKbv/ng4zd9ziu/sPNmbIT2eaD65S7d+vM0N0GUOf0Hg4EA8yeM5+Vq9bYytJGf1vfwqodkw/Zn2RVdYuC0HVdI1VVzXFHHUx+ftyuabmR3buObMmS5cydv4hAsGVdR1o63+vqkjmdr4tOndpDEztNWJbC5/fTobEjb+wM7bq6pP29SjnlchTffDuzEXJkf9gxRx1EXvsi0ul0s8U2l5QLZdnlU3K8hLLQnF6/Sin0QIAZM+dQVVWdqWm3CYm3LPr368XECeNJVLbMZjRNYtYmOOXEIwgG/Fn7Abv2OnfuApYsXUnA72/2SUNBQT6+vGLy2xURb1ec4+X8W3FhzqNXTZONzmE35tbv9xMvzCeWn8e6snIee+IFJh12Oiedeimr15Q0SsqVsmPq99ljNFYy6YXXeArg9qBUKIJ+ndOPHuEcEbWdUbo9QxsW/8x8Q4OuFW2tpGlOmZOxw7qy3+49W60CSimoTaTo272Iu68/fIPEiZ3W+B0l9aJTxvLjvLU8+doMivLC23Xv5W0BpRRVpeWZPtabCD7O7/RwiEDAnwlEDwb8yHAI0zK55cY7KC0t5/67b8ROJBBZ56mmCXr17s5HH36OwHZGa1et47PPv+WEYw+1Cdcm5WJsh/S7K87n+SlvkEqlHTWiac9R0yR1iTo6dO3M5ZedbZOsLI7LdXqffPYN5etKiRfmY5jmVhl/XdcoLSunsqqavHhsEyIonKzX3Qb1Iz8/nokNVo2Rq3SaLp3a20Wtyb02rS8to7LSUVapPyqf+vm3pA0D3VFvG/69S7J69ejK+WedyG3/uJv8Tu1JNbHfrs+nU1FSynV/vJzjjjmEVDqdIXkNFUpb8JNcesVf+PyLbwlHIwSDARYtXsbX3/7AARPGb1KcuSGuvepi3njrowzhb+pmXdM0aqpr6Te4Pxecc3LWlnOA3blDwvsffUFdVRXx4uZ3RKlPAjFapaZVVVRBrmx593d+H5FIOGPXPl0nUJgPwP+efpnFy1fx7quPEotFciie9s/+/Xuzi9ZJ8gjgdiVxSkFNbZqRgzsxclCnDXanbaUuri+v5d2pC/h21kpWrKkklTIJhXx0bh9j+MCO7DumJ107xjNOrq2+3429OfGwIbw9dX592mQLxES3ofu/r5tMu4IIpmVlab7e9o7NafHr/LSPMFzeLNjy9frcVoA3/vZAvvtpJYuWlREK+rz2Rg03T0E/J5x1AsFAILvKYSn8fp0vvvye2XN+xuf3Z1Rwy+mWEW3fgUeeepFfX3o2Q3brn7UVlduhIS8vumHBXAHPPP8aJx53WM4evZZlMaBfL26/9Y+ce85viRTlZ+KSNqf8GYZBsraOex7+J127dGy0TRbAsy+8AZrM2Re57eeJTYjWrStl9ep1DgHcULFzx6BXj65M3G9PXnjuNQraF5HMQbj8fh81Jes59sLTyM+LZTayG89PIQTLlq+ioryCYDic6fgQDIeYMfMnZsycw+iRu2UlWa46+8erL+Gjj75g2rczyS8uItVIhi4O6S9fV8Kee4/lD9dcgt/feOu09aXlzFuwGJ/T+ULTNKxkimeff42J++2RdU2U0t78jR0zjL//5bdcdcVfiHVoB2y+V7Wu6ySTSSSK++/6GwUFeTltxu1T/fxLbyMD/m2yrri1/o46chKdOrbDMMysSqVf15k3fxGfTJ2GP+BDOXVU3T7J+Z078PVn3/Dy6+9x+ilHZ22z53K+mFMn01tFPQK4bQmgECTTBvuN7Ymuy6wLXUsXZSHg/me+4d9PfMWKNZV2SI0mMn3L3V1lcUGYow8cxNXn70NxQbjNSKD7GfuN7UX3jnmUlNfia0H8kybtuL/rL92PccO6ttkY5SIU9m7ZPloWGwiyIuf7xRYqPeN2f8mLBrnlyoM57ldPe6tWg7FRysLv83HX7dc3WpAZ4J4HnuSSi39PqEOIdMrYgEi4Dnf1mhKG7NY/e+s352cqaWTYjWmahGJR3n7nE36YNZchgwdkdbb255ucc8bxVFZU8durb8ASgmgsaith7k7D+SYhbKdXWVGFX9d56IGbOfbog7M7Nep7Dn/73Y98+NHnhGPRrdhWUKHrOpXlFcyaM5/+/XplbVPnrkt/v/43fPzJl5SsLyOen9eAWjucWkHZqrUMGTmUa668cLOK57TvfsSoSyKjkUxXC12T1NbU8PhTLzFm1BCyxfe5ilpeXoznn76Ho44/nxnf/Ui4KB9dc7uDNHwmdtZ4+eq1DB2xG0898k/8fp/TtmzT6zMME59P54mnXmLtslXE29nqmmmaBOJRprz0Dn+65hK6dumU1WY0zSaov/vN+VRX1/DXv92BDATsAteZvfRG12dZVJaVE41GefSx29l/wh4510vXlt794HO++WY64Rb2HG6LeZxMJrny1+ex956jG33vt9//wO77HEswGMBkw82TMk00XWf5slWNmar9bEzLDkn0ltHtmx/t9AqGUvh1jfHDu7XVWmwfjQK/u/ltrvjHm5RXJijMC1GQHyIvGiAWCZAXDVCYH6YwL0QyZfDAs9OYdM6jfD97VaZLRVuRl8K8EGOGdCFRl242sZTS7vIxfkRXLj1tfEYJbGu48ZFSikyJkMrqJHMXlfDptMW8/vE8Xn5/Dm98PI/Pvl3C/CXrqa5NZd4vRX2Lty2hApqmxT5jenDGUSMoq/RaxrkEwM6er2buvIWYpkk6bWQKxrqvtGFgmCaHTNqXvOJCamsS+Hy+TF0xn08nmUwRj0YY0L9XThXePcZctXI1aPUZvbqmUVtdy0233e9srlQONV7DtCwuv+xs3n7tUfYYM5xEVTWVJWVUlVVQVVFtv8orqCwppa66ln32HMP7bz3BOWeekJP8NSRDN/3f/dTV1rVYHXdrIDbl1XAe2n2RLd7/4PNM/+tsn62UYuCAPrzx8sOMHjXEudcyqtbbr8qSMupqajjsiIN47cX/0K64KLNRzvU83n7/U9ionZtpWgTjMZ569mWWLl+ZOfLNdk2WZdGjRxc+eOcpzr/wNFCKypL19c+kspqqMvuZKNPkrHNP5v23n6RH9y5YloWua5t0uxBOHbpkKsVDjz2PHg5m1galFAG/j/Vr1nHbHQ81crQrMtf3lz9dzosvPMjw3fpTXVlFVUkZVeUV9rU1uD4jUcfBkybwyfvPOBuG7OTPPZ2xLIub/+/++tjJlthME+0lp+1IAWmDeT8vxDBMUqn0JnPYriFoMGhgX0aPGEJVWTmBgD9T0knXdRRgGiajRw3Z7LqxfMVq8KoqeArgtlUwwDAsCvND9O1ZmNPxNIvIKIUmBU+9OpN7n/maTu3iGKaVI25MZb6zfWGE5asrOe13z/PaA6fRs3N+m2QQugvN7kO78MI7s1okZUop+PNlB+D3aW0e9+cuvG4JmR/nreGdqQv4csYy5i8upbSylkSdgeEkywgh8OmSUNBHcUGEQb2L2Wt0dybt1ZdeXQsyqqBdjqBtSaBSiivP2Ys3Pv6Zyuo6dF3u8muYpmnUVlTx09wFjB45xAny3pAkaY5C1rtnN+6986+ce8HVVKwtATe71DCRus7fb7qabl2zqzGuKpxI1DF77gL0gC+TYGGaJtH8OP/736uc8YujOeSgfXOSNU1KTMviwAP2YuL+e/LRJ1/y4Ydf8NPPizLtugoL8hncrxcTJ+7FvnuP3UCtyTrnnX977c0PefHFN4kWxDd7tJwLNTW1mLUVVAhbUWlUSZGSsFNg2DQtfJEwb7z9MRUVVcTj0awxWO5R8O6jh/Llx1N474OpfPPNDFavXYfUJN26dmbvPUaz5x6jM3Mp25roHvX/OHseU6dOIxSNbKB4KqXw+3yUrFnHjTffy33//humaeY8BrUsRWFBPg/ccyO/vvRsXn/rI6bPnM3aklIsS9GuKJ8RQwcy+dADGDZ0YINrkzmeiYWuaTz82Av8MP1H4kUFG8TWGYZJOD/Ogw89w6knH8XYMcNyhrW4JPDoIw5i8iH78/5Hn/PxR18wb9FSyssrEVJSXJjPkEH9OGjiXowbOzJzDblOSkzLvr5HnniBD9+fSqwwv8U2U11ZiVFZRkUyVd9YeHPrWcBPKBS0bcRZ52fOmoeua5gmOVXucCjE3f/6C8edeBHLFy8Dn5NVblpgmFxwyZlM3H9P58hfy7qOCiH47vsfnU2DR7K2a44ULh7ato9ISCwjQazLnkQ6jMYyEiC2jZoipSBRZ9C/ZxEfPHY2uiY3iZ1pgQBIKm1y8DmPMmfhumbFi/l8GiWlNRxz0GAevumYNiGApmUT0s+/X8YxlzxFONT069E1SWlFgl8cPoy7rz+8zeP+3GsD+OCLhdz/7DSmfr+UqpokPk3i9+voukRzyZxzdq6U/beGaZJM2V0livLDHLRXXy48eUwmlrMt4ykbLuj/fORz/nzXhxTnb0cJIUJipWsp+3kKykyxtQ5XNE2jqqyCk04+kmcevaNRp+c+jxk/zOGpZ17h558XooCevbpz4rGHsse4kTmzWC3LRAjJh598ycGTzyS4UfkIKSXJujp6dOvM5x89T7viwpxxV5tzztkcX67Pcf9t7br1jJ9wPMtXrCYQDGxwbbquUVlSxm23/IHf/ub8rGTSve8/Xn8b3303HX8gd3kMt+ZmVU2CLxtk2rrfc9ddf+eSC09rlLQ2dk/u9Shyt7QzTBNd0zj3omv570PPEC/Kz5q8IKQgWZPg9Zf+y6QD98YwzJwZtW4G+OZOGBqrN9fw3pYsXcn4fY+lrLLKVqg2YhuaJqmtrmXEsEF8/N4zdreYRu65sfFszn24trdw0VLGTziequoatI2uT9M1qtaX8/B/buWsM45v9Ltfe+N9SteXIjV9s+E9lmWHbLz57ic89cwrhGMRlNNre2D/Xkyb+hIBvx9yxFe7drp69Toeefx5vvt+Fslkio6dO3DopAkcfcSBm93sr1lbwojxRzpt5vSt3AnGg6cAZgR+eyErLgi3CflzHdzPi0tYsLSUoF9v1pFkOm2SHwvy3ucLmLOwhIG9i1tNYtwlqHP7KHnRAImUgeZ0DNmcOpo2LPJiAS4/a0871r4NJTWX/C1aXsb1d37AG5/MQymIhf0U54edJAEyyQIbxtvYPdr9Pp2g325YnjYsnn3jB179cA6nHzWCP/xyArGIfwOS2foNg20jZx4zkkdenE5JWU2LYip3JpimSSge4fU3PmDWT/PZbVDfRgLebfVp+NCBDB86sFmkxC1ke/9Dz2CkjawlWILhEPPnL+bsC67ilecfyCQZZPtMzZnvNsmyVXJ3ntlz1v6dpslGyZ/bCeHs869i0cIlxPLzWqTkuPfz979c2eS/WbBoKUPGTG5w3Rb+aJhbbn+AE487jOKigpzrh3scnJVkCmG37Wvkmeuaxseffs3jT04hkh/PmbkqEEhd47yLr+XT956hR/cuOcmMS+qsTM9okVHy3WcihGiUIFrOIm5aFudffC2r16wjlpddkTVNi0gsyrfTZvLLy/7E4w/dhmlZWCr7SZBbPNkdM/d6FU42b4PrEzlJpE3+qmtqOe3sKygpKSMaj7ZY/QM4/LCJzf6bssoqHn/0eYTz3aFwiFk/zmXKS2/zi5OObPQZWZZFx47tuOZ3F+UkiDlVWV3jsSdfZM3ylS3KePawlUWynVzgxLIUBXlBR71rnSN3ecCqtVUtircDkJqkujbF598vdVQuKxPb1qKXE5NYmBcmPx7CMKwmqUOalFTWJDl20mD69ShEWW1X0NYlZW98NI9DznuMVz+cSzxqx0Uq7ABh01KNEivlLDbuGEkBhXkhfLrk3qe+5rDzH+enhevQpMA024ag2Y7IojAvxMmHDaG6NrXDdz9pk12iplNTXcNVv/+HPYuUarTPr2VZGKbp2KiVaQeXy7Ebht0+7q33PmHKlDeJ5udw6IZJvCCP1199j7PP/12D5BIz5/N0W1q5cad2Q/v63zVGfF0Sdfb5V/LGG+8TL8hrlSNvqB419jJNE2UpksnUJhvQYDDA0sXLuPzKv2eSdHI9C/tes8SINUKwDIcYrF23nvMvvjZD0hpTnALBIMuXr+bYky9mXUkpmqY16vjr4yBl5sjQfSaNkT/TNJHCTga74OLf8+47HxPfDCE3DIN4UT5PPPY8l1/5NzQpnZhfc7Nj5hI92cTrs8dOUpuo4+TTf8UXX3xLLC/Waptx51BTXqlUCtM0qa1NbKB2KGXhCwS47q93sL603MmQtxrZCCsMw8yQdbe/t2hEPdV1jSVLV3L7nQ8R3KpJUh48AphDAlQKQgH/BgSuFcs3AHUpI7MTbYkqaZqKdaU1aFLg92lIKVr80jT7ZzwaoLggjGE2LYbPMC2iYT/nHje6TeM07LZqgoee/44zrplCdW2KwvwQpqla1anEJY5KQbvCMHMWreOoi55i2o8rnGy+tiKBtnp68uShFOWHSRvWLp/JZpom0bw4b7z+Plf/8VZHKbHjnHI5EF1z7bpxx5k2DHRdZ9Hi5Vx48R+RmynIaxgmseICHnvsBY4/5VLKyisz5V7aopWf6+w0TaOisooTT72Mxx+fQmyjGLPW2FeTXjI78TIMk1hBPk899SLX/+1fm6hWrdncGk5Nv8rKKk449VJ+nr+YUCS02c82TZNYXpTvvp/FIUecxc8LljixZlabZL26ZETT7KSPs87/Hf996BnnmRibX+sMk1hRAf/610OcdcFVJOqSGZtpC3XfslRGNV21Zh1HHXc+r7/2vl0nsgnXt1kn3YzEoWxJIDgKZjAUZMGCxZx53pXOeMqcdSztHr/1JLixDZM7X1KpNGdfeBVr1pZkSkF52M4397vCTbaZiuMsyPnxUIvaTbmLRSTs481Pfmbl2qpGd1XNIUdSwMp1Vfh9m09ckFJSWV3HIfv0Zbd+7dssls407azRR1/8nitvfpO8WNCpQdW2O8G0YRGPBKiqSfKL3z7Hy/ec6nR4af19uBnavboWMGH3nrz8/hzyY8GcZGdXIoGxwjxuue0+LMvk1httdcitKSZl0zvGuIRFSolP15kzbyHHnnwxy1euJhKNbFYxMQ2TeHEhL770Fj8vWMxdd/yZCQ2SOdzj3uZcj/tyHehnn0/j0suvZ8aM2c5RlrFdPYtoQR5/veFf1CWT3Pz3qzJExy6XJJu8N3Wfhd3pRWf+giWcds5v+err74k3cvSbjWTF8+N8P/Mn9p90Cnfcdh3HH3NIg2dCo0en2Qipq266XWhm//QzF176Jz779Cvixc0j5KZpEi8u4NFH/secuQu4+46/MHrkkMy/NSTfTR83+1jYPkrVePOdj/nVFX9h/oIlTlKKsf3N4fw8Xn/9fY47+SIefuAWCgvzMypfc5+P+3e6rlFeUcmZ5/2ODz/4nFhhXqZ2oAePAG5zpB1jbC3FcY9IB/VuR/vCCOVViUxsYZMJoFIE/BpzF5UwY87qNr3PSNiHT5ebJabCOcY74ZAhmcWstaPjFoJ9/4uF/O6Wt4nHggjEFit8apgW4ZCPsso6LrzuFV67/zSiYX+r4zwbjsdREwfy8vs/4RUGdB2IRTQ/zm233c+MmXO4+YarGOn0zW3o6LMFmDesw+eSLIAnnnmZ3117E2vWlRJtAvmrJxwG8cJ8fpq7gIMOP5PzzzyBK39zPr16dmvR9biOb8myldx+x3+4/7/PkDZM4oVNd+SWZWXq0LVG/LDJsWp0LJRSRPPj3HLLPcyY+RP/uOEqRgwd1MR7txOtpFNeSdM0Uuk0jzz+Atf/9Z+sXlfaLPLX8JlEY1HWlZZzwmmXccoJk7nmt7/MZPW664TbijObKuxet/1M7H8vWV/GfQ8+yT/v/C+l5VUtJleGYRIvKuSrb2cy4aBTuPiCU7n8snPo3Kl9k21GoRCIDbo/zZ23kFtuf4BHnpyClBqx/PhWt5mG118f+5prI5fPK6+9x94HnswtN1zN4Yfuv8H15KqyoOonzgaK4EeffMWvf/c3Zs6YTbwwz4v78wjgdgIny7aqNtVQwGuVAGhZiuKCMMcfPJjb/juVTu1ipNJmcy+LgF8nFPS16e3aO7LN30NdyqRHlwL2G5e7JltzCZMQghVrqvj1DW+g6/V1+7YkDMMiLxpg+pxV3PTAJ9x0xUGk0marFV/lkJ09R3anU7sY5VVJfLrwShoAlmkRKyrg3fc/44uvvufE4w7jjF8cw/hxIwk4oRabQ21tgvc+mMpd9z3Ou+99ij8Uahb5a0g4QuEQSinuuecx/vfCGxx3zCGcfMLhjB87kmAw0KTPSaZSfP3NDJ557jWem/Im61atJZyfh8/vbzrRUIpIJJzprdo62H9fWJDfuGppKmJFBbz97id8/uV3nHz84Zxx2jGMGzsCXxP7Aa9YuYZX33if/zzyHN9+M51AJEwsFm2xEzdNE7/fh9/v5+mnX+b1Nz7kiMkTOeWkI9hr/Gjy8+NImXt83I1BOm0wfeZsXnjpbZ59/jUWz19CMB4jFo+2SlkzDIOYY2u33nofTzzzMiccexgnHjeZsWOG4vM1bU1OJOr44qvveerZV5jy0tuUrS8jWhAHRNPVL6WIRSNtZDOOQ3c+JxIJ54x5cmNp585fzFEnXMhhkyZwzlknsP+E8eTnxWlKIQjDNPnqq+954KFnePaF10mZJrECj/ztaNjpy8DUJFKM2a0Lbzx4ehtxSjuVpKYmxXG/eoYvpy+nfVEY01RtEoO0paFpkrKKBKcfNZw7/zi5TY5N3c+44LqX+d+bP1KcFya9FQOAhRDUJQ3e+s8ZjBjUsU0/+5K/vsZTr86kIC+0bYOat1EZmMYctWGaJCqq0AN+Bgzow/ixwxm22wB69epOhw7FRCNhhBDU1taxdl0JCxYu5fsZs5n6xbfMnbcQgFg81iArtBXXo2ukU2nqqqrRgwH69+vNuDHDGDZ0EH16daNDh3ZEnQ4PNTUJ1qxdx8JFy5j541y++mYGc+ctIJ2oIxCLEnC6TzR9nZEkaxMcOHFvxo0dgWlarZpT7nFcyfoyHnrs+c0+aTvpwiBRWYUvFGLI4H7sMX4Uw3brT6+e3WnXrtDu7GBaVFRWsXLFaub+vIivvpnBN9/9wJoVqxF+n02M2uBZbHhdJomqKpAaPXp2ZeSwQew2sA8T9h3PQRP3znyXEIKamgSPPTmF6T/MYdq3M5k9ZwF1VdX4omFCzvW31bW5iSfJZIpkVQ3+SJhBA/owdvfh9rj16kb79sVEwiFQUFVTw+rV61i4cCnTf5zD19/M4OefF2GmUgTjMXw+vVnHnlJKkok6jph8AMOGDmq1zTRcizVN8tU3M3jv/c8IhII51UBXga2urAIFvXp3Y/fRwxk1Yjf69ulOp04diMei6JpGXTLJ+vVlLF6ynJmz5jH182nM/HEO6bokkbx4JnvYg0cAtxsCaDc8N+jSPs6Hj5/TpkeEQgjWltZw6V9f461P5xMN+wkFdCczd/slgpomKa9M8N8bj+HoAwe1uu2bS/4+/noxx/3qGWKRwFZfCDRpq7z7jO7BeSeMJm2Yrc5otiyFz6fx1mc/8/SrM4lFAq1KYtnZCCDOFUhNw1IWdYkkZl3SaVyr4/P7HDXHLtmRTqUgnQYEeihAMBR0EqKsNp3vmmYXgravp86Wcze4Hvs77esxQIAMBggFg/bftpBkCCGoqU1AXR0N+oi1YmQVSI1wXqyJ3w9SaliWRSJRh1WXtPtp+/34/D6kkIAibZiYyRQYBug6gXCQgNOjdkvMW9dGFIpkXYp02oDadRxx7PG88sKDme+UUjJ/wRL69R0PGBDOJx6POcX8zS2WUJCxGdMZt2Sygc34M2ujYZoYqbRjMwItZNvMhmVtWmAz1TWQSrWBzTQYcaUgFCTiqONNIeoAdXVJ0ok6ME3w6Wh+Hz5dt0N5lEUqZaBSKburSTBAOBRsNAvfw/aPnfoIWCmFT9dYs76aJSvL2a1v+0brGDVn4iqlaF8Y4X93nMR/X/iOe576ip+XlBIK+AgHfYDatoQhx9qQNkzaFUQYM6SzwytaOxb2OP/7iS+ddkdb/55NSxEN+fli+jI++npRGy2m9mf4fRqRsH/7e5bbw/yiPmYqFAoiwyEQ9f2bM11gNJ2A32dntiqwlIW1BdRUN1s0cz1OFw1LqUxje9fhBQL+DeoCurFYrfnuaCSMjEXb8oZyZmlmeWvmWYTDIWQ0nKlfV/8sBMGAhgwFM/3KW3vfzbERv99HJBKmGkWkQV9pdz3Oy4tyxnmnsXT5KmbPns/aVavxRyIEAr42K/XUmM2445bNZgKaRigYsK/VteFWZ14rYrEoYgu03rRj+Zp2fQ2fTzAY2MA23PvXhYbP50NEw5lQKLc8jQePAG7XildpRYIf5q7JEMC2UE/q+0sKzjluFMceNJhn3viBJ1+Zwez5a0EIomE/uiaxlNri8XBNgRSC2mSaYf070KVD3gaLb2vUv2k/rOCzb5cSi/q32EK92WtRiqAbV9kmz9jp5amUV86gqQ6nEUenlAJrK1+P1fj1tLXg1Rynuy3vfVs5bfe7DcPYYJzcNahdcRGPPngrAIuXruD5F9/i9jv+w+p164lGw1s8BGPz49b264BpWbCdHJ02ZhtuVraHnQu7RMd7AXz41aJWE55sJFAIW4HKjwf55cm78/4jZ/PwTcdy8N79EEJQUl5LbSJt11VyCp9uSwKYTpsMG9jRvm6zdTXuXF70wjuzqUumnWOmbej4lMJ0ikybltXKl2rTeCgPHjxkJ+L1r3rVqWf3Llz563P55P1nGDZkALU1ta0KVfHgwcOm2OkVQMuyiIR9fDptCWtKqmlfFG2TY+CGcFuvWZaF369x5MSBHDlxIPMWrefVD+fw5qc/M3v+WmoT6Uz2r+bUm9ua8YJOuDWDerdrg8UbNE2QqEvz8TeLCQV175jUgwcPzdg8iw3akbnrshC28m4YBn179+CZx+5gjwnHk0wbmS4VHjx4aD12+i2VUhDw6axYW8lzb81q0HeyrRe0+v6jptPerX+vIn57zl689Z8zeOnuX/C7c/dmSL/2JFMG68trqUsZTjcPidgKwqBlKYIBjQG9ijKLcGt27wCz5q9l8fIyAn6ftzB78OChKaslhmFQVVVDaVk55RWVVFRUbVCIWQqB3+fDMAwG9u/DUUdOIlFV7amAHjy0IXaJQtBuksB/nv+WXxwxjIJ4sE2ygRsjglAfSKxrkrHDujJ2WFeuvmAfvpu1itc/nsu7ny1g3pL1doHdsB+/T9ti8YL2UbVFLBKgc/u4uw63nEwqhUQw/afV1NSlCQV9GKZHAD148JBrDbJDZqL5cT6dOo1xex+DUpbze0WvPr24786/0bVLxw3UQKUUe+w+gkcfftYbRA8ePALYPCilCAZ0Fi8v4+YHP+XmKydhGBa6vmV3k1II0MQG2Xg2GezC2GFduPr8ffh02hJeeu8nPvxyIatLqgn4dcIhn10io02JoN0APT8eJB4NOL9pOQN0yfOsn9cihfB6ZXjw4KFR8pdMJjESdSAE1ZbF2qUrGi6WzJ7xNT9eeDpdu3TMdBZy/7aoqACEV4zdgwePALYAhmlRkBfioee/Zfzwbhxz0CAM00LfCkcKArfcSn3ZCaUU4aCPg/fuy8F792XZqgpe+2guL7w9m+lzVtslAiJ+p85S67OvhLDHoDAvRCwS2IDEtYzc2uO2eEUZmia8418PHjzkJH9GKk3/vr0YNXI30k4sX/36I1CWYvfRQ9hv33GZQtgNN/DlFZWgtofqlx48eARwh4RSEAr4uOKmN+nVNZ8RgzptFSVwE/LkkEGl7KNUAXTrlMdFp4zlvBPG8OGXC3n8lRl88OVCahNJ8mIBpBCtUgTtoruQFwshnQSUlmYku8fnacOipKy22f2QPXjwsOtA0yQ11TUcNmlfbr3p2hasNYJp3/6AxwA9eGhjLrJrEUCFT5fUpQxOv+oF5i4qQdclxjZq8SWEnUEspXBqMFn4dMmkvfvy+C3H8dp9p3Hy5KGkDYvKmiR6a5JFhEApi1jE7tfaOr5m/3VldR2VNUk06QVme/DgofH1J5lMY5gmqVQK0zSzvtyNpFKKtGGg6xpLlq1kystvE4xFt207Rg8ePAK4Y8O0FKGgj7Xrazjx188yY85qdM0mgdtSxXJbEinnGi1LMXJwJ+798xG8fM8v2G9sL9ZXJLAsmzS2jACD36e12TXXJtIkk6ZzvO1JgB48eGh8/cGp97dpDUD796ZlYlp2YohP11m7bj2nn3MF69eX4fPpXqiJBw8eAWwlCTQtImE/a9bXcNxlz/DKB3My6tq23mEK6lVBy7LbyY0Z0oXn7zyZW343CVDU1hktLoega213hpI2TAzTRHrHMh48eNgM+wuFAui6TiDgR9f1LC8NXdPQpKS6ppan/vcq+x50Cp9OnUYkFvHUPw8e2hj6rnrjpmkRDurUpQzO+8NLXHDS7lx9/t6ZBAnTtDYKVN4G7LxBr1IhBBecOIZRgztx4XWvsHRlBfFooAXH162/IbfRmqK+G4gnAHrw4CEbLEuhBwNM+/5H7nngSQzDzBp/rICqqhp+/nkhX02byU8//Yzm9xGLx7yesx48eASwjUmgpfBpEl/Iz11PfMWHXy7kN2fvyTEHDsoobKZpIaSwS7psYyJoGBZjhnTh5Xt+walXPs+s+evIayYJbIvSMu5I6JpE1ySptGlryR4J9ODBwyYE0CIQDvHJ1G/48J2PN19+QEq0YIBYXswprO+RPw8ePAK4JRYnR8Iqyg+xYHkZF/7pFR567lvOPm4URxwwkFBAz+xilbLJ2Lbigm7CSteOeTxzx4mccNmzzFtcQjTib1KjciHASLfdYhoK+Aj4NWoSKbz0PA8ePOSCco6AZSRM/RlC1nc6MYKWd+TrwcMWhpe+6cAwLUJ+nbxYkG9nr+TC617hwDMf5p+PfM6i5WVOyzaRiRN0CeFWJ4GaxLQsOhXHePzW4+hQFKWuzth8SRensn5NXdomg626Cvuv49GATT4t1aqi0h48eNgFNtuW3d/XMEznZ7aXnQ1seX3FPXjwCOBWXaCUwrQsIiE/BXkhFiwr5c93fchBZz/Kub9/idc+mktldRJNkxkl0HQSNbYmGdSc4tC9uhZwz58Pz3QaaYyCKWz1sryqzqZwreBrQtifFwzoFOWH7SNoj/958ODBgwcPOww0X7jDn9v0E4VAWQaBeDf80c4oy2CbZlK0AG6ZAr+uEQn5SRsmM+eu5qV35/DKB3OYv7QUXZe0K4wQ9OtIp8elZSksp1ap2ML3LKXAcEigYVi8+8UComF/5kg7y4PBUopwQOeUycPw+7RW9UNWTmLKZ9OW8MO8NYSCPq9Ew5aEECgrTV3pT6BMPMbtwYMHDx5aA90bgtywlMIy7Y4Z+fEQKMXKtZU88Ow3PDLlO3p3K2Sv0d2ZtFdfxg7rQkE8VP+3WyFmUHNKxVx2+jhefv8nFq8oJ+DPVSvL7kNcWpmgojpJOOSj8ViczY+NRDC4b3tMU3l0xIMHDx48ePAI4M4FOxPNDkj2+3RCAR+WUixZWc6cRSU8+uL3dOkQZ9ywrhy4Zx/2Gd2DTu1jmb+3S8qINlcFhRBYzpH1+SeM4Yp/vEko6MuaEKKUHT9YWZ1kXWkNndpFW6UAulnRwwd2JBDQvZgdDx48ePDgYQeCdwTcAlhO1Xpd1wiHfAR8GlU1KWbMWc1rH85lyrs/8f3sVSigS4cYwYAPIYRdUka07fGw/VGC7p3zmPLuT9TUptByFHvWNEF1TYq9RnVncN/2WIpWlbcRQlAQCzHl3dmZ2EgPWwjeEbAHDx48eGhDeB67FXD795qWwqdrFOSFKMgLUVmTZMq7szn72hc54IyH+ds9H7F4eRmaJh3Vru3UMiHsPsLFBWH2GtWdmkQamaM3r0Bgmop5i9Znrr8132tZFvFYgD1GdqO2Lr35TGQPuxZnBaQATW76knLb7wuFaOT6xE63b231sxRiC7w8m99xnv9mXjvIPhopvTnvwjsCblMyaBMqXXNiBoEVa6u47aGpPPLi95x+5AguP2sP8mNBTMtCk7LNvlsh2Ht0D55944fcFbaUwqdLZsxdDbS8p3Dm85xvOvagQTz7xo+wjZNAhBCtvqeNYSnlHW+3gFQpwDAhlQbLBKyNvSToGvh1e/EF2Bpl36TjACwFacN+WSYbFjEXIJxr82nO+y37b1o7Nk2f0237TFr7vaZyxqktp5eqfx6tuebGCKvbsshSW255ckkFCtImpFK2vTTF5i2r5fXzmzJGbXXPptr8o28r8rTZz1HNGzPXxkzLXo/S7nq00ZyXOvg18Onu2r/NXZpHAHdMMtgwZlAjVBAmnba4/ZGpvP3Zz9z5x8nsPrQLpqXahLAIYVfh261vOyJhf84CqpZSBAI6Py1cR1llHQXxYKviADUpUAr2GdOTkYM6MmPuGiIh3zYhTFIIkmmDmkS6TR1UwK8RDvm9DOcmLLJCQF0K0knb2RXlQd9Ogg4FUBgRBH323KhOwrpKxapSWF2mqKqy3x8OgS7B2AJEUErbgdUmwUyB0KFDPnQqFHTIh/yIwKfZzqG8VrGmHFatt38qAzQ/hAL1TrsFpkSqiaYpqHdCbYFkU79X2IQ323oW9tsvZbUdCRQCkoZtDxt/ZNps2Ti7g20BluE4egG6H4J++3vaaqOxic1rUBRr3OZXl8Iq1+Y126Z0HVrS7CRtbH5T4tPahpgF9cZJl3CfmWrd91nW5ue/q9g15fkgoKYOVBoCQehSZM/5dnmCaMD+nLo0lNUo1pTBylLF+gpn7Q9CwGdvZD0C6KHF6pxhKqSE9oURFi4r45hLn+bBvx3Jofv2x7JUq49O3QnXrXM++fEQFZUJdF1usntRyiakq9ZWMeOnVew3rheWstCEbMWEVfh9Gr88ZXfO/f1LxMJ+rK3cE05KQV3SoF+PIibv1x9LqVYvepal0KRk5tw1vP/5AkLbiNhu94ofoGn2ImumoEdnwUEjBfsNFezWQ9A+TxAO2MqH+0xMy3YWlbWwfJ3i2/mKt79TfDrLorIawmF7YW4LR+0qklUJmwwM7i44cJRgwhDJgK5QHBcE/fY9uP2tTdN2CusrYO4Ki09+VLz7nWLWEgUSYqGmqwNC2I66XR7cd6lO0J9bSbMsyIvAf962ePhti2ikdWOgSaiugcuOlpy0r6SyJrvahrIJ5+pyxUV3mSQN0BwVV9egshIuO1zjsmMkFVU2SW8LRSkWhilTLX5zn5m5VymhthauPUly2FhJZa3jyJu5Aa9LQ3m1Ytk6+HGJ4rsFivkrFcqCaLhe4Wkzmx8h2G+YYHB3QYd82+Y1rf7aTcsmEpU1sLRE8d18xTuOzVdVQjjSdJt3x+iPp2gcPFpQmcg+RlLAr+4z+Xmlsu1ONXddhdoE3H6BxoEjhP0sZPbxDvlh9lLFuf8yW7iG2/e0/wjJX06TVGW5J9OC/Cjc+7rFk+9ZRBqZH7oG1Ql7To3pLzhirGSvwYKeHQT5Edve3U2hpezPSSRhTbnixyX2s3nrW4t16+1n454CeARwR3RSbZx44dYJbO7fpA2LSNhPKmVwwZ9e4dX7T2PEwI5tQALtv42GfRTEg6wvq8WnC1QWImYrZSYffbOY/cb1anX/Xk2zYxqPmjiIx8fO4LPvlhCPBrdqGychBMmUyXWX7MeBe/Zp08++5v/epS5lENkGxHZHUP0sZROEgT0Ev5wsOWKcpGOBvVjWpW3yU5PclCxJYStKQ3oKRvcTnDMJflwiefhdi6c/tKhK2AShNW1gNWmrX6kk7DFYcOFhGhNHCopiYBj29Rmm7Sg2Og1CSmiXD13bSQ4ZA1ceBx/OUNz/hslnPyr8AVsdaIqZK2Uf++3e3yYGppldJbEsiARBCskzn1gYVstbbAtHIcqPwfmHaPTtZCtV2b5XKftelqzNod4oW6nqmGdfTzaVsLkwLCiMQiS06dhbJvTuLNhzsKC0smlqT7Y1X3NKcFkKSivhq7mKh9+zeHuahabZimBzl6mGNj+gh+CXh9k236mwaTYfCsDQnoIxjs3Pcmz+yWbYvHBspW9n2HOQoLQ6+xi5c6wlRFcKSKagRwfBsXtK4mF7rmS1H+dnzw6CYb0svpmriISaN7buPRXE7Hsqy3JPhglF+fDyF7a6m8ulSwmVVTC8j+Cq4zUOGiWIBG0FPmnYa0IilX2j2LVY0LeT4Ng9YeEqyYNvWzzwpkUqbT+7na074U5PAAWQTlukc1lvC9ifT9fw6bJFC7NpWgT9OqWVddx438c8c/uJbXOTQMCvE4v4sSyrXs7Y2MkoRSig89FXi0hesA8Bf9uYgK5J/v6bAzn0vMcwDLvszdY4NtU1SWlFgqMOHMSBe/YhnTYRsnWN6dxj8eraFO9OXUA45MP01L9NyFUiBbqAq0+WXHqERlEMKhOwvrJ+QXV/ZnsgprKPZGsS9r8P7Cq485cap+0v+cMjJlN/VERj9rGjarZd2GpLxwL4w3kap0yQBPxQXQslFRtdXw6CkXachV1CCY7eQ3DY7jrPfmLxt6dNVq2HeLRpR0RK1SsSuQggAqrqYGA3wX5DJW98bRFroQooJdTUwlHjJT3bw5py+x6yDaRS9aQlpyLuvMcwoC32QaZlf17We3PIR3WtPWYtLS6gGsxlvw8OGSM4ZIzG619Jrn7EZNlaRTTc9PFtaPNXnaRx2ZGy1TY/oKvgXxdqnHaAbfOf/aCIxZoWG1iXspXtXGPkktWWKnLpJBzkbJhKKm1FM5f7MUwozoPDx0q+mmW22NUaJlTleO6GaZP2lEHOMAQhbNX7gsmSP5+qEQ9DRY39fFzFTwhb4c6GVNoeV6WgY4Hg5rM1Ju8uufhuk0Wrm09sPQK4jWFYinZFETq3i5E2zVb1rLWPRARr19eyZn21XW6lBRMsbVrEIn6+mrGchcvK6NujsFUqoHAWOikEfp/e6NGUZSlCAR8/zV/HtB9XsufI7liqdbGIUgpMSzGkX3v+dvlEfvX31ykuiGStR9imCpQUJJIGndvF+NuvJ6IUmTZ9rXJOpoXUJN/OWsnSleVEI37v+HcjclVVC92KBPddprH/CEF5teMknED3ptqtEIDz/kTKPlYb3lvw8p91rn/M5O7XLPvIrhmB37q0FZp9hwvuulijb2dBaZX9+c26vo0cRXmN7VTPOlCy71DBZfeafPid7bCbenQnJY3G3VrCdrQn7iN48+uWcy2F+zl2KIib5UgOBVDKzR+1Nszebb1q33gWsEvM3VdrYSmbCAAcOV4woo/OabcafDtf2arb5uLONJu0dSmEey/TOXC4rVK1lc0P6yV46Xqd6x83uftVi0gYxGZsfnNj1Jpl0LTsuMkjxkkMs4H9NDI+dSk4eJTkthc2DCNorl3kuiepGs+k1iRU1cC1J2lcf6qkrApKq+xn05I5nzJgbbmtSL76Z51j/m4wf4UiFNx5joN3agKoa5LyqlouPGkMf7p4PwzDbFWtOtOyu2k899YsLrjuZQrioRYfdWqaoKIqyZyF6+jbo7DNDhebFJckBUnD4Pm3Z7HXqO6INvhyTdp1Ds86ZiRLVpRz63+n0qEwgmlZWySTSjrfZ5oW/77ucLp1jLd5Us0rH8zBMC0nfMAjgO4iW10LA7oInr1Wp08nWFtmB7LrrTwalAKkE7sjJdx2vkZxnuAvT9oxYspqyryyyd9J+wvuvlhHCFvx0zVanbzgLh0lldAxX/D873Uuu9fkqfetJpPAJo1vAg4YLunfzWLBqubHb0kBiToY2luw12BBdV3LVbSdBWLj51cAT1+jcdh1JkvWKAKNjLEmbfLXv5PgmWt1+nauV1Tb3ObP02iXJ/jzEyaxSOuyhFtzTYmkHZ4xpp+gJrl5Ei6FTWb7d4U9BgrenqZaHcPa3HlTVQ0n7Sf50y8k6yrsa8r1fJSTGd6wvFE2MujTobQauhbDQ7/WOPx6g6RRX+lgR4fcFSZ+bV3abnomRKtebtHkvj0KCQdamxRgx85V1aZaT/ocYzUtRTKVZnM5HaZpEQ0HePPjn1m1rsqu6dcGLE3TJJaluP7S/fn1GeNZW1oDtH1pFl2TpNMWdUmTe64/nAPG98I0rTb5Hrd937rSGt77fKGd1WxaeLAXvboUdCmGZ6/V6Nke1lfZi6TYzJiaVv1rc9NGcwLfSirtZIArjpVUV22exOgaVFXBCftJHviVTtq0j9sac9KK+iDwhtenNvM9iZR9PHzvpRon7iepqsp9RNbc9Spt2Mdpx+whMVLNV8CkBDMNx+1VH7u1M8JNrMj2amzK+nT72LZzoeC28zQa2965Nt+5EJ65VqNnxwY2L5pn86qJNn/NiZLfHqdRVdU26mfzN9e2/Rw2RpDXDPtRylbfjxgvt2r5FCFsta5DoeBPp2jUJBpXqk3LDgmIh+2YS5++GXvRoKwKxvQTXHy4RqJm2zwXjwC2iBgJSstr6y2llcqQUjCgVzE9uuSRTBkt7qShlELTBAXxYNvcKJBMGnZHDrn5CRjwaawqqeLJV2ciBKg2OuIUwj5mvuHyA/n7byZSm0hTk0ij67JVXUdc1U/XJOWVdYSCOo/dfCzHHbybrfy1kcRhOdr+S+/9xLLVFXZvZY/7ZbLldGFns/buJCivbTwhwLTqEwziYciP2Fmu4UB93FAuO3XjqEoq4fpTNQ4eK6iqyU0C3eOfPQYL7r5Is8u9WLnf75I+KezryYs41xe2/1uK+uvP9X2WE89110UaewwRVNe0jdImpa3AHLOnIB6zCWFznlPSgKICOGKcrd5sC/VPsOWLB+dH7ezq4o1fcdve3Oecy6mXVsGBIwSHjRPU1GYfJ0vZR4L3XarTt7OgorrlNi+bavMVcN0vJIeOk7bNa1v32aVNO/P10N0ldemmkx0p7PlwwDBJp2J7g7Q1CitrEpIJOHYvQe9O9uZMitxCSV4ElpcoPpyp+GyWYn2l/ZxcEptr01dZC6fsJ2hXZBPOnaFo9M4dA6jsI9vVJdVOZl/ry62YliIc9HHIPv35v/9OJRzyYRnNpwiWpYiFAwzoVdwGTFwBgsqaJGUVdeja5nMHTUsRDft5/KXpnH3sSArzQlhKtZqkCSewx7IUvz59D0YO7MQf7niPmXPWEA77CAV8mftvSpKIFAIhBShFdSJNOm0ycY/e3HjFgfTvWYxpqpyt71qq/tUlDR5/eQahgO4lfzQgJVVVcM0pGhOHC9ZU5HaE7tFKQdR2AovW2OVequvs3XanAujTSVAct5WYtJHd+dobE9tp3nSWzrR5BjUphSY3XKjdUiuFMfj3xTq6DslGjj1Ny3bIQb/tbOetUKwttwPA/T77eLBnBzv4vabOzuzMFWRvmHa9sH9dqHHIH00SKYXUWldA1nWkg7oJDhgueGmqIhZtepxhshqOHS/p20lQVrXlCYSl7Ozl3zxgJ+6Emxgor0moqIVgoGXZuA+8abGmTKHrG453JCAY2FWw126CkC83IbA3q3Da/pJXvzA3WTE1x+avOlkycaRgbXnu+owb2/ziNbCsRFGTsMMjOhZC345NtHkn4ebGszS+maeorlNo2tYpSuyGeOwzVLBbd0Ei2fRYQuEk73RrB/sNkzz9gWVnyqstb39ShwNHStKNEDM3DOBPj5k88q5FtXNvhVE4Y6LkyuO0nEfuQtjPtXs7wdgBkte/svBHtvy9eQSwlYbh0yUr11RRXZsiGva3qvCxu/AoBeeeMIonX51JTW0Sn09r1nGwT7czVw/euy89Oue3ugyMe0+LV5RRUV1nq1abWS2UUgT9OotXlPPg/77lmgv2cbKH2yCGDjvO0LQU++7ek7f+cwaPTPmex16ezs+LS+3vDuj4fZp9bCs2TM1Rzk2ZliKRNKhLGvh0yYhBHbngpDGccMgQx5Fbbdp/2LJsMjnl3dn8MG81BXnhrVrOZrslf05M0KCegksOl5TV5D5WtZzjFU3CEx9YPP6hxawlispa+9+EUwqjTwfBkXtIzj9EkhdpJJNRQnUdDOoGFx4mufEpk3h8w2MpKaGmGv58qsZu3WFdue10c11fQRRmLVU88o7FhzMUS9crEsn6eRQKQK92goNGCc4+WNK7o53kku36NGknxAztKbj8GMl1j5jE4q0rX9PQ6Zy0r8bLXxhNVqEtZRO+E/aVNqnaGiqF09FjyVrFz4sVvkjzMmtbUvRaCLse3Pz5CoLUd91w71eD3fsJ7r5Eo08nkZUESgG1KRjVR9Ktg8WKEoXf5ybUQV3SLm902REa5dVNsHkNnvzQ4rEPGrf58w6WFETtDN5cNl+ThIFdbZu/4clNbX5LSrfKhCPGSoJ+ewOka7l9Tra/txQcOU7wzEdbPk5OCDszvSAOfTrZylw2V2o68/7+Ny3+9YxFuMCuXQhQkYBbn7BIm/YaUpZjrptOcY0hPQSvf7FzKIA79xGwUvh8GqvXVbNwWWnmd61VuJRSdGkf56YrDqS6NoXlJIc0lfwlkmnCIT/XXLCvQ/xUq+9TAbPmrW1WT17TsojHgvznuWksWl6GJmWbZrtq0o5zjIT8XHLqOD549GweuuFoTjxsCN0756GUoqI6SWl5gvXltZSU17K+vJbSiloqqpMIIejbo5Bzjh/F0/88gTf/cwYnHDLEDuB1CjW3oViMEFCTSHHXE18R8Pu87h+uzUsw0nDRZEmhcyQpcihrwQBU1Ch+cbPBL+8wmTpbkUzbils0bNd9A5i9XHHD4yYH/9Fg+gJFvJFMTF3azvK0AyQdNzpakhJq62BYX8EZB0rKqhshf8o+lrvvdYtJvze49xWL+WvsZxwJOtfnRGTMXaW4Y4rFQdcaPPuxRX4kd+afT7MzhM8+SDKgpyBR13rn4CaD7DtEsFvPpikxmkNahvcR7DlIULOVkz8CPpAB+1k39eX3tfz78qPgy4P8OMTznFfceUXgm9l2YetUOnvQfoY8xGBAF0in68dYCPu/fznZLvXSqM377QzjU282uPAOk6mzGrf5Q/5o8F0zbL5Tu9w1HNsaKQMKC2w1rbaR8IFcZFhz5uOegyR9O2+d6zYsu+tKXkTkHE83hOX96QoZsq/fchJBdA2C+fD0xxY1STvGuShuhxJs/Irl2Ykurs/Y0bHTl4HRNcn6qlqm/biSYQM6Ok699bFolqU4dtJgqmqTXH3rO1QbFrFIwG6PtoF1CPd/GKbF+vIE+fEg9/75CIZnikC3bpV26959Pn2prag10TCVqlcj/3bPR/z3xmPaZHw2HiubsFlEQn6OOWgQxxw0iJpEmkXLSlm+tpKVa6qoqKojbVj4fBoF8SBdOsTp2jFO764FG9QqdDN9RRuvKpajJj74v2+Z9fNaigvCGJ76h3DUv16dBYePkzlVC8spdlxZCyfdZPLdHEVevrPIuokfDewy5ActBHNXKE660eDlP+v06yKoSWSPOaqpg65FcPBoyaNvWwQcRURgB6yfO0mSF7YL/mY78nS7CPzjWZMbnrAIRW3C4MZsbXx9QYfMVCTgvH+aVNbCBYfKTFmJjb2Lm7hx1kTJtQ+ZhANgtNJBpE3b6Ry3p+Qvj5vIoNNFpBGibqbh+L0k8RCUtFHXjuZsoqwGry2Nhkkg2b4vng/fzld8OtvikNGSiiwxmgq7/2unwvqieW57t56dBEeOk1RuxuarEnDSPwzb5vOaYPMrFSfeYPDKn3X6d23c5rs4Nv/IWxbBLawCahJqa+CQkYI+ncg6Xi5e+NziiLESXW7qblJO55tJoyV3v2TZc2ELXbdbAs2n2baeS5lU5CZtlhMLXF0HNz5j0j4OKXNTdcxS4PfDj4sVut8jgDuICmhnp773xULOOW5UmxEHt/bdmUePZFDvdtzyn8/4fPoyampTSCEyKpxSCrcuc148wDEHDuKq8/dhYO/iNmkD59b/Kymr5auZKwgFfViq6cTFNC3yYyFeeu8nDt9vAMdOGtymcXXugqppMqPcCQGRkI8h/TswpH+HJiz0KjPmbZ1RbC8AdhLJz0vWc+djX5AXC2BaHvlzFTYjCZNGCTrm20kZuY6Egn64+B6T7+Yq8gucgq3kVuMsww7IXlthx4+9/TedwriziGdx9rEwnLyv5MkPrMzRWjINXdrDIWNkppTGpjZuk78pUy1ueMoi5jjpxpySpWyy5dNBC8PVD5n07yLYe4igKkvCgFsqZPJYyf+9aFKVaH2JEE3asYBH7SG48xVIpBtXAZNpKC6Ew8c6yR/Cs19hwoJV9eQgl/LvHge6iRhpx+Y7FNhFhBuz+UvvNfjuJ0VeQeMJOw1tvqQSLn/A5J2/b8bmQ3DKBMkTjs1vDRw5XmZPolC2Yru6FG5+zmJcf0mP9psme0gnK/eI3SUPvmlt0VIwylmj6tL2d/q03P2IBXDEWMGbU8EI2s+0YaKXUnD3K1amf3TOL9RtVdfyCOD2D9NRnr78fhmLlpXRs2tBmyQ72Au0rQSOHdaV5+88mek/reKzb5cyb/F6SisSGIZJOOSjXUGE3fq1Z/zwbvTvVZQhHbINyIwbt/bu5wtYvrqSorxQs5UrS1mEgj7+8M/32H1oF7p1ymuz69uUCIrMZLOPrlXOqFu7RtOWIX2bKheKa257l8rqJPFYYIsXsd6RNlBShwOGC4wcIaKmZWfPvv2dYspnNsFKNTFzNW1AJAzfLlAcd6NJUSMqh1trLBSsz/BNJWGf3SSdi8gap6ewSdz6Kvjb0xY+f32HiKbNL6dhvII/P2Xy+l/0nMH7dWno0R7GDRC8/pXCH2mdkxDO/fbrLDhopOR/H1s5k0E0p5fqcXtKencSOeOYtvRmQdOa/r1busadcrx+Y0E2btJFMr0RKdRsmzc3Y/PvfKd4/lNFLL/p2dquzX+/QHH8DQZFeaJRm69L1SfKbKmV0FU9u3WAfYdIauo23WxYTs/fr+cpliy0+HKOxcCu0i6wLja0g5okjOwrGNpL8P0CRTiwhQiTU3pmfbWirEqRF3bWqSzzo7IWTp4gWVUO/3zBpLIS/EE7dMG9v1jUsYlGFEdX4d0ZsEv0AvbpkpLyWp56bSZ/uGiC/fBEWy16IqNqjRjUiRGDOm2WsCFoM3LlZiY/9eoMfLpsUT0/pew2cutKa7js76/zvztOQncUuy0Vv1Hfn3nbyhSGaaFrkv97eCrvf7GAooIwhuGpf+4zco82B3eXJHNlUzoO9tH3naLfqvn259fhve+t+mB+cq/A4VD99QHsNVjYPXNzHO/kReH5zyzmL1fEY80/jjIte8f/7VzFB9MVh48VlOc4HvNpdueA17+w14TmjIVLRrKNz4n7SqZ8lruousKOfTxxH7lNWlUJ7JhFsxLKLTb/HBUEQmQ9QmzLa1LSTg4wzdzqadqyVS33zC9tQHE+DO4uGrV5WmvzPnhvehPOyx2b35Irpdv67cDh9mYql+ppKXhzms2w3pymOO2AHLGRpl1aZfLukm/nmMhQ4+ELrSH5ugYV1TB7qWJAVzvhB5F7Q3X1cZLJYwQPv2vx2tcWK9bZ7w8G7M3uzkTwPALoqICxSIAnXp3BuSeMpkNRtM1UQJcEuuROOaxpg6xWZ9UWUrSpqubGw73z2Xy+mL6MeCTQ4rIlpmmRFwvy0ZeL+NO/3ufmKydhmAp9Jz5HcsnfO1Pnc/MDn5EfD3nkbyMnlzLs0gft8+yYtI2njFvzbOla+HquRaCFO32l7CPephIy+/nZJVgGdxN29p/MsWO34O3vbKfV0rgdgV2S5s1vLY4cn/tsN23aZFnzNe/Izi0ArWkb+i7NyYLee7BgaG/BzEWK0EZjLKXd+WNEH8Eeg0TWjGrlXP8WKWAr7Ps+aKSgS57A1wQbEALen27ZMZV6y56L5iqOmt0mbGNUVsCogYK9BjuKltyUPbhFfueuUOhOBnDKgG7Fgvb5onGbL1F8PW/r2vyWgmXZz+GIcTJrr2r3npevU3w228IXF3wxx2Lhao2uxXYJJbGRCliXgkNGC25/0bbtLdo9Q8GLXyiO3atxWxICymqgXxfB7RdoXHGMxvszLN74RvHVXIt1pYAG4aBtV5a1c8T67dIE0DZejVVrq7jtoc+47epDsEzV5kEyMlezzS2wd7NjRhSptMWtD021ky3agBAVFoS5/5lv6NI+zq/OGI9hWOj6zpcsbjrkb8ac1Vz851fx+yRew7csipQJnYvso5+KLLFvrpIxf6WipJJWHfU0x8kJpwZfQQQ6FjqOOoc6sL4afloGuq/l12YpkD74YZHKGgOYUUxNO4swGrKJRFOOQy0F0SB8N1uRF7GVp0Sy3qEaTjLI8XtLvp+3qZoihZ38ccLeklhowzhNpexn8u18hV+323s1/Ow2Wfec4+9fHaXha2K9Ok3ChKsVq8sUPr1l866iGowKqAhuOnGFhFEDBPdeohHw2YlMGxNAU0E0AFN/sli82u7xCrbNd3VsvrIRm/95haKkgk0I+Zay+S05z+tSMKC7YOwAkZUsW44dfTZbsXq9nXm9vgw+mmlxwSGSuo2PgR21bVA3wbgBgg+mKyLhprVzbAl5DYfhja8t3psuOXCEsLu1NJKp7PZgzndqAJ66PyxeI/lwhuKVLy2+mKOoqrFVar+vbco6eQRwG6s9BfEQj700nUP37c/EPXrb7cN20CaZbtbqA099xdc/LKc4v22yVi3LoiAvxJ/v+pBIyMe5J4zGNC2kJhE7kS3ommT+kvWcefUUqmtThEM+L+4vq0FAcVwgtZwbb3TNLnprGSCCW4dFu51E8iOCqFt4OItqoeuwvkRRUqVsUtTCa3Pvc025HWtYGGeTorMC21HEQ/Y1ratoWhFmOxsf1pQpvvxJMbqf3c7K/VvNiak6Ypzkny9ZVCXse3FDNJJpaFckmDxWbtL5w3KUm5e/tDhoVH3WptgCz6O61iF/TfhwKezyHS29EEvBhYdK1pUq5EYKYjAgGNRNsM8QkZP8ZTbR0j7G3SDWz4KiuMhJ3hX20fWyEjC3os1vKWgSzBQcOlpSGG080eutb1VmEISEt761OPug7L5BWXZoxxHjJO9/ZyLZfGRAS+emBNLAb+43efXPOt3a2fM0V41JtwezYUJZtf27zoWC8w+1y0nNXKR49hOLKVMt1qyHSMQpHL6TuYhdhgBmbljXuPzGN3jjgdPp1ikvc4y6o6lXmiaZ/tNqbv7Pp+TFgm2WteoupPGon6tuewfTsrjgpN0zsYtiB69+6ZK/uYtK+MVvn2PVukqi4YBX8iWHMgD1baway6KsqHWcwlY0D+UEpQf0+k4M2ZxbdR2ZWK4Wr9/KOdZKKmqSiuIcN6qAgB8CPru/tmjGvfh0mPKl4vJjNiQsbuxS745wyCjBE+8r/FFbwZLC7npy8N6CXh3sxvUNBXufbhPRt6cpjt5jyzqw5hwvS9E6EqoUXDhZ2iRZbfoQDMvOyk7k6KWcNmxV9fVvFG98aatTplVPfNx2gI3ZfFUNW4ZNb+010YJgCA7bXZDM0vrNPf5dtg4+n23hD9SHX3w9T7FglaJHe7FpNrCTxT5xuKB9ob1GbKmYT8vJyF68VnHCjQZP/E5jcHdbCXQTuUSOuEBXuUwZUFdpE9vhvQRj+2tcerjGfW+a3P+GHULi9+1c8YE7dSHoTY3E7kCxal0V5/3xJadvrmjT4sdbS/lbV1rDL69/hXTatGsPtuEtuDGLsUiAa/7vXW6472OkU3tvR22NplT9se+0H1dw3GXPsHy1R/6aQwQbI0fbyiyaQjjtjPO2s6OcDsCJIRA0nwhbFoSDgtkLFN/NV0SCG36PxCYoJ+4r8TUgvJbaMPmj4YJuWhBxju1+XqEI+ncuBaO8GkrK7XZ+G7wq7Rp2FtkTONJOKZZFaxW/e8hAaps+r6baFTs4/5MS6upgZB/BsF6C2mT27N9wAKbOtli9vn7D5dOgrBw++sGOS914GXUz43t2EOw7VJCs20IxqA3sPRqGOSsUh11n8Mi7FkGf3SJSc9Q+02o8I1zT7PuvSdp21C4fbj1X49lrNQojkExu2XvwCOBWUM/yYkG+/mEF5/zhJaprU05Nv+2fBJiWhZSSqtoUZ1/7Ij8vWU8k5N8iBNZe3BT5sSC3/Oczzv3DS5RX1aFJgWFaO1RgrJulrWmS59+exfG/eob15bVEwn6P/DUBdencBVbdkgmx4IZOcWuRv1R68/X8Aj4n0aA1ztpRg3SfrfCpHETYzZxOt6BZvCZAJe3jWr++aaJHdQLGDxSM7Gd3G9E1O/ljZF/B+IEiax1EIeClLyyUteXVWbcws2nWF2jO9TLN1itBmrTHINtLk2zSXtJ0Cka3y7fb1p18k8nSdfbzdJd/135dm89pCzjZ6GLHThKQwq5NePhYSbiRnsxKwZvfbnqjQrOPhdNG4zUqjxwnEVuBbZimHU9bXgMX3WVy+PUGj31gUZ2wu3vEw07MrLVhDcBs46Jr9jxeUwYHj5L87w86BVG7Q4zYSeKhdjkCCGAYFoXxEO9/voBf/PY5Sspq0KTcrsmAYVpo0u7acdqVz/P590vJjwW36DUrZWcaF+eHmfLOLI648Am+nL4cXZOZ8jPbvern1DNMpgz+9K/3+eV1r2CathLs9fltwiZAQGlV48qRaUGnQhD61guFUg4BqEgoalP2/994MXfjBAtigryQsAO5W7hwZz4rKsiPOHXbRHZFpSapqK1TNqFrxoCYCgjYmcary+3jJrWxohe0kz0sJ+vZStvJIdHghs5bKbubyaLV8OFMC39wyx5dKWznWpRnx0cWbeZVGHeOqlthMNZmnHjDsfBpdr/YcACe+tDikD8ZzFqqiGZrxybtzGCzkYLClgWdC9kqpGaLbqAMyM+Dg0fb5VO0XMe/JYrPf7KPf921wLQgEIBv5lnMX0VWhdmtCbj3YEnPjoK61OZbGrbFRkTXIRaBr+YpLr7TZOK1Br95wOTDGYqUYfcFzo/UK4O51jch7DCKtRV2b+k7L9J3qiPgXS4GsCGhKswLMfW7pRx50VPcfd3hjBzcKVMUeHuJC7Qs5QSgS+YtKuH8P73MD/PWUpC39UqW2GMVZt7i9Rz3q6e57LTx/Or0cYRDfrvTiWK7iqN0W89pmkQTgm9+WMEf/vkeX89cTkFeKNORxMPmnToarC5VJHPs8F0Vrm9nQV7EVk60FsbaiSbGhbkttjRpl3RYX6koim2aCexmCreLQ8/2sLLUIVWqZddmGdCvs318mCs71E0UqUzkzkJszG6lDxauhE9+sDhub7lBcWtN2pmLk8dKbn/RpKQCOrSza61Vb9T313LI4tvfWZSUQjS65ZQqt0DwPa9a/LRE4fc3gWwKWF6iNiG5zUEsZAfyJ5K5FVfl2MnacsXnsxWPf2Dx6Y/2NUZDm2Z3KocAriyz+/nmsvmkY/P5UbtDy1ax+S2g/tXUwQHDBP062x1ucmX/fvmTYm0pxKNsUBDeL6GiyrbXwYfZ/YMbJoy5paQ6Fthlgh54zSljZG55H2Aqu36nwH6eD7ym+M/bFn07C/YZLDhghGDcAEmnQvt5NtY72++QwCPGCY7dS/LsxxaxyPaRxe0RwFYQm/xYkAVLSzn6kqe4+vx9uPCkMWiazBAEuY2Ijd1CTmWylJ998wf+dMcHlFcmKMgLbvV6dYZpEQ75sCzFTQ98wpufzON35+3N4fsNcJQOmwhKIbaZPN5wzDRNUlJWy78e/YKHp3xHKm16RZ5bsIj6dVi2TlFaZXc+MMxN2z4l09C7o2BoT8HUWYpIqPkLo0sk3WzMxo5Y/U7lfl2zM08XrFIM6WHHL23sTU3LdmB7DxFM/UEhwy3LRBTCzmqcMETmJHZu15GfVyhSSQhEW+C4net/8QuLY/eSmxKPFPRoZx/ZPfi8yeSJGr06btoDWTilLl7+wkJoW1iZdRS2N761+PRLBRGaNMiBEJls5pY8j49+UFRWK0b0FXQssGtBiizxa7EgfPm14oKbTWTMVoYsld1G3WScZesUZVU22c9m83Vp6NXBtvnPfqxPItmSNi+20LM7cpxmK+iNKGrPfaawElClbUiapQSVgv99ZnHmgTIrgZLY5ZGOGCt5+B1rqypo7nf5dQjF7Oe+cLVi3hLFQ+9Aj/YWB48SnD5RMrKPoKI29zhLZ0N5ygTJC59ZXi/gnYUE2iVALH5/+7u88fE8rrlgX/Ye3b1+AphWJgliqyh+yiUxgvlLSrnpgU948d2fCAd9dtLCNiIxLikuzg8zZ1EJZ10zhQPG9+aSU8cxYfeemWwq01KZNm5beshc0iec/suaJiitSPDUqzP5z3PfsnhFGXmxIFGf5pG/FhBAnw6ryuw6f3sOElmVFjfO7uQJkk9nmM1+5m7v0G7tBPFg9pIuLjcyFSxZp+qdpmlnIh67Z3YiIR0idOyekrtfs1rUlN5VfDoUw+HjBNV1jRxjKfhmrmrFHLOL0H7yo+LnlTbZa5hd6dYaPH5vyX/fsEnixsfRlrKTP6YvVEz7WREMbfnMRYWtqGlxu7hxU8hQa4rsSgG/f9Rk1izFkQdKnrhSy6jPDaFJqKqFY/aSPLWnxcczVca2c9l8QIc1pfDzSsVeg7PbvGpg85+0xuaLBfFQ4zZvKTu71WzDOE7Xpju3g/2Gkb1QtjN+1Qm4aLLkrINE9h7BgE8T1DpxqSrLHKxJwpj+gkE9BLMWq1bVTsx1P02p6OY+t5ATx7uyTPHA64onPrC4/BiN3x4rSaazxwu7bfkGdxd0KLSz6336jh0DussTQJfYSCkoyg/z1czlHPerpzlk776ce/xo9tm9Z0aFa0g2RBuVRFFKZfriaprMFJNeubaK/zz/LY+9OJ3SilryYyEspbaLZBXDtAgHfQjggy8X8tHXi9l7VHdOPXI4k/bqQywSaLB7tFMw22rMGo6XS8rd/sKLlpfx3FuzePaNH1mwdD2RsJ8ipz6i6R35tgia0yLqs1kW+w3VbJU3y3sqa+CEvSSPvW/x1U+KvFjTeqNq0j7C695O8NbfdGLOsZzIouRFQ/DVHMXRfzPQ9frizB//YFFRq2WtXSYF1NbZBZDPPkjy7+ct8gub3qsY7Fi1mkq45CSNHu0FpVU5eg5rdubg1DkWeisybv26nV351jST3xytUZusL0/iEtpB3QUXHSMzyufGx78BH7z6lSKRgFgMjK2yjtrPzg2w39IIBsBXYBcAfud7waRRkoosLfosBbqAa47X+HyWgWU13h/YbYv26SyL/YfltvmKGpuIP/aBxZezm2/z3VybD2e3ectpQfj1XMVRfzWykquWQgq7j/b+wyRdixvvHS0ETBgqkCJ3swGl7HmW698Np+zOYWMkM39u29Zwwvn8mgSNy6Ru+0G93j79OoTi9vjf+KiJaSn+cLKW1Y6EU7syLwKdCgQr16sWh5R4BHA7VDsM0yIW9qMUvPbhXN7+bD67D+3CMQcN5sA9etO9c36GbGQIYcPO8m4LuCxxHSpTP1NlthdCiAZHpvZffDtrJS+8PYsX3/uJVWuriEUD5MdD213CgqsGxiMBFPDJtCV89PVi+vUsZNJefTl0336MGtyZYEDfYFZuMmbuzCLHgDUYV5fwNRyvVeuqmfrdUl7/aC6fTlvMurJaIiGb+JmW8rJ8W/ucFWg+u+/nr47MXdjYwl5Y77xQ4/A/G5RUQtw5Psu1QOqafQSmLLj5HI1OBbZTzVbbzRL2rv296YpkHQTi9meHAjBrse0k9xuWPY5JOo3gr/n/9s47zqrifOPfOeX2e7fQmw2k2zX2LvYaLFGjUaNGo0aNJZYYe43+bMEaAxgxdhGxgoqiIk2agoB0pC1sub2cM/P745x72YVdQFgQ2fN8Phvi7r3nzJl558wz77zv856pM3G24utpivJyx5O2rpe35spC1FXDSQcJrjixYUzemot1LOy0b85PlJILNma/o5RTk3TYWMWlxzYRa6g5hKYxrTpTd2q5vjdeYvi33VJWUjq2o4BH3pIctovWqIdK15yYzAP7CM45XOM/H0inJrRch837HJu/+pTGRZGLpN/QHZs/6XaLFT/D5qUND16o06GVU9Hk59h8c3lsNR1O3k9s0EYlkV43+RSsWx6lWBru+L0FT7zTfM9RTGTpVOkmR8l1j9WbXykWVSlMc7U8lOXWifbH4KVPJZccoxNtJNylNPd0J6zEOwLeBlH0FJXFgiip+GbKYkZPWEi7VmH27NOBg/fegd/s2omdulRSWRZ0jxzEBhnq6qmyGtV1GWbNX8lXExfyyZi5TJm5jFQ6TyRcJDFyq85WtUtE0AdCsHBJHU/+dyzPvzaRnbpUsM8undinbyd2792eTu3KqIgFNrDPRKOex6rqFLPnr2LSjGWMmbyQqT8sZ2lVAiEEkZBJa4/4NfsiGwzA1DmKz7+THLd3416WYkB5zy6C1242uPgxmzmLFf6Qc0yi1RvS4ks3noBwCJ78i85xe6/2QshGhH0NDVbUwrCxjnetOLzFuJxBIyVH7KY3ukgJV/bBb8Dg6w0uecxi1LcKX8jxlAmx9n5EKiferhCHUw4RDLjCWO+iZUsYPHLTVzZbOcR20o+OJuC+PRs/dm6M7NgSykPw3gTJ7J8UoeC2XdjeVk728ZjvFW9+KTn3CM2pL6w3bp/Xnabz/nhFTVI1WYNYSqf/p81TfD5NcsI+GrXrsPkenQWv3mrwx/9raPP19SCVK04dTzhevWdcm69NuDVnm7D5qjoY9o3dwOabw/uXzUG3TrB/T22dyQ/1Cdym3jOdhz7bC/buLhg9rYkM7I30/rUpE9x9nt4kmbUlRKKwos5m0IdO2b/63lrlCr3nC04Wf1lEgN34/WwJOWvbkILxCGBTLxbXMiMhH0II0tkCI76aw/ufzyYcNGnbOkK37SrZsXM5O3aupHP7GK3KQ0RCPsJBH6apY+iOy9yyJJlsgUQqR20ix6JldcxZUM2chauYtWAVS1ckyWQL+H06oaBJZXkIKeWvisTYbmqm32cQDJgoqZi3uIbpc6p4cehkwiEfbVuF2b5DGZ3bl9GxbZT2rSNUloeIhnwY5mpvhlSKTLZAXSLLypo0S1bEWVqVZOGSWhYvT1BdmyaXtzEMjWDAoKIsAEr86vrs1wLhevieGi45eg+tyRef7nra9ugq+OAeg8ffthk6RrK02j3uKQbWCIhG4JQDBNf319l9p4ZHUGte3pJO7dH/fCyZvVARja5eOGzpkMj3xkm++E7joD6i0QzdYrJKeRhevdngqeGSwSMkC1Yopz7pGqJxmu4ktlx6vMYfj9Eo2I7HsDHSZdlOTdGPJqpSzdNNNUNdh1QK3hkrObivjpQ0WY5vrfHSYOjXCum2d0vNCE04i6j2c6o9NIOIeFGf8bF3JMf/pqFQdn1Sn83Dju3hL6do3PKCvU6PWjEpY8B7kqP3XL/N776j4EPX5t9uzOY1J/nklP01rjtdY4+dhEMq9XXb/MARklmLnPnSbARQAyvvaNu1ijne4ia9nBuZoNPUZjLgc5JBvphsN0tWi8I5eVheC/NWOJuBxur2SuWQtv4HCAZ/7HgN69et1jWorYM+2wk6VDaeUFSUnkploapOoeu/fi+gRwDX6wFxiI2uC2IRf6kaRtWqFIuX1TFyjJu0oQn8psDv09F1A11z4vkUIG1FwbbJ5W3yBdsll07sWsBnEPAbhIMm0o0x/DXr0ymlSjV1Ay4ZLBLqZS6Js21VqjaiaZqzaNSbbVKtjrcsltPSNIFp6JimTjjkIxoW7nGyQkqJJtx0OSU8o212cu8caX4+1amPef6RWikAurEFMZFx4mQeuljnylN0psxxdMLiGYXPcOL9dttJ0LOLo83XmHel/gLkM5wKD08MszEbOXopyr3c9bLN8DuMEgERjSx8RWmPG8/Q+P0RGmOmS76dq1ha7XhFAn7o1Br27ibYv5dG2zKnfU153Ioac6ks3P2K3WypmrYE09UEvKG/Xqq0INZDhAImzF8On0yV+ANbVqYikweZhqS2gfdVoBuOF3ZT9QBDAZgxXzF4pM1ff6uzsm5tUmPozlhe1E/jtdGSafMUoSb6yHbj70ZPUbzyheSCozSqatdt87EQPPhHnStO0ZkyR/HjUkU87cSJbdfasfleXQSWXL/Nm4YTT/r4UBuzmSu42BJ8fieTPL8eAWdD37jrN0WWMzlHDqay3EkM2dTScMX3w7IaxaIVin16CCehpZFnyubgkF007r0Abn/JJpOq95KQ0KUd3He+jmk4nsA1NR6VdMrfzVjkvC98hkcAWwwcXSFVelOZpo7fp7vkRWFJQSpnkshBwCigC2u1YQuH4Ph9BkG/0eBcoEh2LHvbC9SRRTEmFz63z4Qb06fcPlBNeJ2o/zmKCSAuMXR/q4C8ZZCzDIKmRcC03EVB4KV9NOdYOhUT7hhis38vQefWaycf1H/RW5YjIN0qCsf/RnNEf8Vqj0+u4NZSXU/2niUd0eBrn7WZvRCisbV3+NL1Ao6drnjwdck9f9BYXtP4Yl2sB1ydcBbs0w7UOP3g1SWiitmEtu0sUNXJdbfPllBZBtf/22bqbEWsmeK0lFvbdN4S+OI7Sf8DNGpS626LVA4RGjFJUrWKdca5NauH2PWu3XqWzp+O2zBpF6mcpJ7PpkgefVsSCmwaybHdxfmp9xT9D3LsrrHsXVtCJAw3n6lz9gPWOt8RSjlCx3cOsTlwQ2zeduzFsXmBUSx4XM/m4+kNs/l2MfjrczazF9FsNlW0/3QO9uom2KOrk7nb5PGucmJyf9amxtXea0zju1TTuoPg4D4a74yR+JpBS6/olRsx2fGWJ9KNe8uL1XSuPElj/16Cj8ZLFlY7oSG9OgtO3M/RBGwqI7r4DvxksiKTbt5x8Qjgr9DTJRWk8zrZgkEskGePzks5c7dZJHI+7vl4P6KBPNL1SBUzV20FtFBqUszg3fDnV+tcdHKWzo6Vddx+zBjen7EjX83txLzqMhQQ9hXQhfJIYDNugHymUxbp8n/ZvPV3A5/hHKU0tpgVi6wXLGfhWzPGToj1xxUVLGhXAYNHSp59XxKJNH6843iYHbHjR9+26dEZzjuyaRIo6i3Ytam1y9wV/1tbz0JdcAVun/9I8tRwSSS6GRYEBW99LTlt//UHYRWlPYaOkVu0KksxO3LvncVq8rce0mDbUB6FVXGBLAbbq02zT78PlqxQPD3c5oGLGvcCFrN3j9tbcPJ+gqFfqSaPV4s2v6IGLnNt3m86fby5bL4omvzip5JnXJtvTpsSbvWY4/cRRAKOl3HNPrKlUynjhY8ld75oE97A42ddg1QSbj5b5/ITtEYzi5VLQk/eT/DOmGb0aAbgf6OcpKlYEwkcxXGoS8NuOwn27aE3sNVU1skkbmyMip7GFbXw8ucS0//rF4H2COCmTCQgb+vs3LqGI7sv5MjuC+nTfhVBs0BVMsR/xvZlZTKIachtNgvvl4QuJJmCn349FnB0j/nsv8MSatIBvpzXiU9mbcfouZ3IFEwMTXoksJlgSyeO6evvFRc9ajHoOoOgjwYyJU0tij93MbelQ/6GfaO49lmbgH/9/EABPh9c9YxN0C844yDB8lo3Lk00T9uKngCpnIX65VGKvz5nEwg0/3GQlBAIOpqAPy6F7dbQBFzL++eHqfMV42ap0pHxlnwfpnIb3ge2dMYlnaf5js1tCIZg8CeScw/X6Nqx8dJjxXrNN52h89kUi7y92jPclM2P+V5x4aMWg/+6eW2+vWvz1zxjN11zehPGyLKceMJj99LI5BsnO8X+GTpGUp2AjL1htqRpkE0737vo6CYyst3EmUP6anRpZ7OsxiFWm7o59ftg8QrFva/YPH2lTlXcuZdogqimc443cE2B76YIumU7MjZ/GyiZu1g1ehLxa0SLrAW86TteRdbSubXfN7z9x3e44Yjx9O2wimzBYEUiTGU4y2HdFpEuGE5smodmhyU1Yv48R/eYT3U6QG3GT8C0OKXvjzx22mcMv+Rtdu+0gnRB98agOfvddspBvT9OccZ9Fivjikr3qLE5CIdlOy/oNmUwaITkgkcsJI3X+m1sIdA156X+x0ctBrwnaR1zYvQse9MXU+W2zzScY74nh0kufcJC12lUjqUZnH8lTcAPJsh1kjopnfi/4WMlmXTTcj2bdTFxPaY/56c5Cy0VK7HUJeCxoZKA2fixcjF7d9cdBZccp5NZz9F60eY/HKc4/T6LqjpFq81k84NHSi74P8str9m8NqVpkM3Cfj2dWMRMbu3+V25Zv1k/KSb+qAiFnXaYxvp/dA3CYUeAfMaixsWenZMb6NgKjtxNo5Dd9AzjIvmPRJx3xv8NlbQvX10ruilbNfSGttiUGlnBhvaV8PIoyYBhrkfU3jbe5x4B3MgXXc7SWZYIE/JZVCVDpPMGQigMXWJLwfG95uHTJZ7+8Obof0W6YLBbpxX0br+KrGVg6hJbCWoyAdIFE12TLKqNYuoK5SWGNDP5duLLPp+q6HerzTvfSCrCTm3W4kt3Qxcu5ZKX4pFNq5hTZuvqZ20u/5eNcl/UGzqPigunrsNfn7W59AmbupSiddlqyZifOydlPa2w1jGoSykuftzm+udtTNPNet2MdXY1A4Z9I9cp12EasCoB749XGD4nYL2lblDCYXjrK8mX3ytiTcjg6LoTj3fFiRpduwjHGybWbfPRKIyepjj6706mbwObtzfO5nFtPleAa561uexJu5TZ2txrh3AJzUn7OpnSqgl7C/jgkymKeHz13FMb+FPMXh85STkEXDbhibSdduhm89mqUk4s8G2Dbe76n6Qs5NTDtuzVMb4/51qW7Tx/u3J4caTk8gE2hrmNraXecrYxhiYImhbDp+/EikSIoGmXvEyaUKTyJrt3WkGfDivJ5D0vYHPDyfrUOaH3PEx99RG7ozCiiPpzjJi5A/OrYwQM2zsC3kwLbTQMS2sUv3/I5tx/WoyfpYgEoTLiZnbivHgtu/EfKZ1jmnAQWpc5vxs4QtLvFovnhkvCIYdcbQxhE67sxkufSI66xebp4ZK8W40g5G/YtmLlivo/Vr1jr5Df+V7Bgqffkxx5s8X/PpVEI2trCK7TS7GOvrCaOGYr6jBOnqOYNEcR8DlxYvW/ly843r8xMxQ/LFIEmiiztTH3r9+ntr3u72/Mj1yHR9O2HeLV1HfVujboBXjkLbv03HYj903noDIK156mUcisX9fNdm1+WY3ivH86Nj9uliIScBKVfo7NR4KOTdm247Xqd4vFs/VtfgNL6a1zPOv3ket5a1UBh+6iEU+t3tis2b5MDj6a6NaQ3oi5pxnw0beSZHa117zBuCmHfO/dTdCtY0NPpFLrfya5jjJ+4MyXe4bYnPmAxYxFjrc2FnL6fV1zvvi3YmnLNmVOdvc1z9pc+qRdShDblkK6vBjAjdyVB0ybhdVRxszvyIl95lKX8aFryp2YgvJQjn49FvDt4raERcGTJ2nGXWzB0ugQS3LITovIFIwGO3chnNjMD2bsiM+wPQ/sZoTtHjvig2FjFB9OtDikr+Dk/TT27yno0loQDTfutVI4hKo2BVPmKT6dLHlnrOKHBQrD3PQMu2ICeiwKS2sV1z1r8+wHkv4HCo7dW6N7J0FFxNXJU25VCVZXNCj+PpmB7+YrPpooeeNLxcyFCjPgZthuYPuKge+VUadG75plvyw3ziwWolFWo7u1VId9Izn6N45MhaE1HIdI2Im9kutQoimPOKQjlWsYoyZd+ZhEtonvuvGF4TJo5creNIcXORp15IVoRAQ5EnQyvhENn7W+B6+pxbjYHyMnKd6foDj7cEEy1XhCglLwpxM0Pv5W8u43jobjusiX7ZbZ89e3+T6Ck/ffCJufIhn2jWLGxti8m21bVubYa2N9RL1YRF2DZBbOPUxj126QSELZGt+x3XGeNMetIR34+SLiUjpSSpPnKOYsdUTMU9m1YyItG6JlcM5hgtsHK7Sw80w+A8rLnPFd85kKNkTKnCNq1kMCoxH4cLxi9HcWp+yncfrBjvZiq5hz3WIcb/Hzxfg/gZOpPHeZ4t1vFIM/kSxYqghHVs+VbWo9DbXepXkfSWhIK0O00wGE2+2FtDJrC+psA9A1RV3Gx/G95jHgjE9IZHxo2upC437TZt6qMs4YdBK2Enj0r/n6vTbt57e7zebxUz+jJuNfTbyVIGRaTF9eydkvnrBNKLU3mFeFNDWz30LZzRg532zj4rwcUxnAdkRsd+wg2LkDdGwlqIgKAm75pWRWUVULC6sUs5c4/+azTvmtYoml5i4UrwnHs2HlIRSC7p0Eu+4g6NZJ0KkVlIcFpu4sMnUpxU/VMPsnxbT5ipmLFen06vbVXzg25N62dIjfiftoTuKAWpsYmDosrlaMmKTWKjAvhBO83yrq1FJd697CWXjfHS9JZBovK6aAE/bWqHSzStfQvS4RhOHjZYMMSk04FVH22lmw+06CXL554vaK8Xpzlio+/16VNNWEcDyah/QR7NxROHpsTSTwvDdRUR1vvKKH5l5n+3aCI3cV5KxGjrvcfvP7nHaMmqbwmxtuew1sXjpZzTu0F+zcQdCpNWvYPFTVKhauUMxeumk2LwQUCnBIX0HXDoJCE32EgPcnSKqTjn3lCnBgb0HPzo2PY9HzNXOJYvT3Tl9sjLdLE04Yx4G9BL27NJ6IU7T5n1YpRkx2SrPl8rBje8ERu7hCzI04Xvw+mDBbMWWuwr8efURdczYamTQI3ZGf2W0H6NFFsF1rQWUUAj4n+yeVUyyvhTnLnGtPnaeoiztanAH/thPz5xHAZvRE2UoQMCzeuPBdOpUlyVs6Qqz2Akb8BS59rR+fzNqOWDCPLT0auKnQhCKZ8/HcWR9z5M4LSeR8pSN2W2pUhDL889N9eOzzvagMZ7DlNmJ7WzkBrP/SLe7W8wWQVj0ysqY8iOZUcPCb7mIqN+8Ou7jLt2xHt67UNrd6RfE4V0pnQUc4x1kBnxsLtQntk8rJkFwnIzIcr05ji26xBFU20/QlAu4xV1NNTLtEZV0vtVCoccKRyYFsbrNTgOnEaa1JeFNZoLDu+wWCrhdwHUQpb0Ehs552N9GOjbH5XAHUFrD5jekj4WoAqnWNowJhOnqSm3LUuaH3qm/zGzReCnS/Myc3pH1Fj75SDim18qvHROirialdnPNumwI+Z4MiJdu0iod3BLwp7y7dZmUqxCeztuOyA6aQLRjoYrVAsa5Ljus1j5GztqPx2gQefu5LJWsZ7Nymhn26LCNdMEuEG8DQJbWZAB/P3IGgz/KSP34BFOPIdOF4NkSg6dgqVe8YZksIqkrllueiYduUKoqSr7az0u/V6lipTSWf0WgTrwC1uv7wuorZ67p7LNrU860n0zkaXl3irKmbNKWHF/KDFtwwjb8NfYE29czKPQLWQuu+3/qet6jdFixbx3WK7dgEcl/f5sNbyOY3po+UctrX5DhugB3+nPaFAz/vXusdr40YK1VvfPymc3zc2JxHrK7dLJWTmLKtev08AthcJFAJfLrNRzN34Ly9p6Npq2eNpjnJIAd3XUynsiSr0kFM3fZIySYtoo7235E7L6RVKMuqdBDd7XMpBdFAnk9/3I4fV5YT9hVKItwefpkNUr3COVsd5LratpnabDfDoropi9Km3L8+ed4i4yObp4bxltpc/BI2vzF9tCXHcWPatznHa43CVFtkzm/1a6q3VG3KS1EQ9BWYtqQ13y1tTdC0SqSjmKzQPpLm8J0Xkcqbzap51TK9SxpRf55jes4jYxsNvH8KgSYUH8zYgYLUtq34Pw8ePHjw4MEjgFsXdKHIFAw++GFH/Gt4+IQAW2kc03M+fsPyKoJsiqG62n+7d1pB7/bVZOvJ6ygl8JsWi+sijJ7T2fH+efGWHjx48ODBg0cANxek0giaFp/N7sKKZAhTtxuQllTeYK/Oy+nVtppMoaHXysOGo6j9d1zP+QTWkHeRCkKmxec/dmFZPIzPK//mwYMHDx48eARw8xJARxNw7qoyxi7sQMhnNcj2taVGxF/gmJ7zyXql4Taa/OVtjfbRJIftvIhU3ihJ7qz+u86HP+yIrimP/Hnw4MGDBw8eAdxyGP79To2QF0XO0jmq+0IqQlks6XX5zzZSocjkTQ7YcQldyuPkLL2UIFasyjJjeSUTFrXzkj88ePDgwYMHjwBuGdhSEPJZjF3QgbmrygiYq2MBNTdGsHubavbuspx03iwJF3vYMCiXSJ/Qex6lfH0XUoHfsPh45vYkc2YDz6AHDx48ePDgwSOAmxWmbrMiGWLUj13cbOB6BEY5FSxO6D3Xk4H5mRBCkS3odG9Tw2+2W0Yqb5a0FgEMTVGXDTBy5vb4TdtLtPHgwYOHX+373lsfPQL4K0RRE/D9GTs6sX71elbTHC/g/jssoV00Rd72ZEo22EAFZAsGB+30E5WhLJZdL75SCUK+AhMWtmNmVSVBw/KOfz38+hY9QNO0BoufEALD0NG1DX9FCyHWus7mWKA39z1aOgFqif1btPdCoYDcRBXq4rU0zaM3HgHcQpDKOQb+bmkrpixpQ8gsNNAEzFk6ncqSHNJ1Mamc6SWDbGi/SifG7/he88itSZyV4yF8f8aO2LbwSLWHDX/xaRqGoZd+dP2XW3RtKUmnM+Ry+dICls/nia+sIZFMsaGGnS8USKXSFAoWm2UyCCgULPceBa+w0WZAoVAglS6OYct4Zt3QkVISr6qmbZtWhMOhjSaBQghyOWfuJJMpb6PiEcAt2JnCqf4xctZ2+I01NAFdknhcr3mYuvSOKjewP9MFg107rqRv+5VkCquJswJ8hmR5IsyXczsR8lme9p+HDSJ+QhMkkyniK6tLP4m6BFJKdF3fgl4PkFISDofo27s722/XCSEEhVyeHXfowqBBj3LGaceRT2fW6c0oXqd929bsuktPWreuQDZ7HSuBtCVt2lSy6y49adumFdJ2CyZ7aAZbEEgpadu2Nbv26Umb1pUtpn8T1XUI4IYb/8zkb96lTetK8vkC2s8kb5oQ5HN5unfbgcGDHuXUk/qRW8/c8Qigh2aDUhA0bT77sQurUkEMffUuRril4fbZbhk7t6khW9A9TcANWCDzlsaxPecR8hUa1KeU0jn+HT23M0vjEXy67cm/eFi3p0HXSafTZNNZ9ttnd66+5lLuuvMGbvjbnznm6EMxNI1EXXyLkUBN08km0xx6wN5MGjOMlwc+gkCQT6W56a+X8odzT+Nfj95OrDyGZVlNejOK1/n7jX9m8phhXH7xOWTrEhhG81X6NAyNbDzJNVdcwOQxw7jh6ovJxpMYhtbovN3WyNmW2JhkEyluvOZiJn8zjKsuP88dw61niRaboV+llPzuzBOZPukjHrrvb0yd9gNVVavw+cx1vs8ba4umaxTSaW698XLOP/c0Bjx6B+FIGMu2PU9gU/Pa64Lmg1SCoGExu6qC8QvbcWT3hcSzPnRNIXA0AStCWY7usYBHl+5F0GdhezFrTU7wgq3RJpLliJ0XkS6skeErnOzrD2bs6BBpAR4D9LAu8peoi7Pn7n24/54bOfrIg9b6zJSpM7j9nsd5Z9jHRMpjrgfG+a4QAtu2UWu47jVNQ9M0pJRrHVsViaRCoQmBlAolJbqhlzaMzjUEmiYwTROlJMIwGDdhKuecdTLfjJtEOpVGN8017imQSiEQTvsAwzQa3PfntMVaowCrJgSarpXuYRhOH2i6874yTH0t0qdrOlIpbHfBNXTnvzf0OK/YRtt2NnOGrgMKu14RYwGlNtu2LHl3is9l27I0Ro0984a2pdjHlmUjpXMfXdcaXH+9/auU+3u1dv9qTqxfqX9dGysSPkPX1nsP5T6P0x8Cy7LW8TwCTdNRSiKlQq83tgDSbnwDresa4Ni+VArDvZfdiIdZCIGuaU5N5Eb6Qbm2UfxsLpdnuy4d+d9/n2Dit99x5bV3MHLkl2imUfp8Y32gUNiWDUK4/VScmyB0g7Hjp3D6accyZuwkMtksps9XmmzFcVVq4+zCI4Ae1stcpBIMn74T/XosWGOCKPKWTr/uC3jhm12wPU3Apl9YQpHImxzWbRE7VtYRz/kalH4Lmg7RHrewPSFfoYH4tofm3qlvxY1T6+f9uq6TjCc45OB9Gfb6s5SVRZk0ZTqvvT6cBYuX0aYyxvHHHckx/Q5m6GvPcOW1dzDgqReJVpRh2zaJmjqwLIxIGL/fV1qYhHCOksnmIBggHAqWyALgfE9JhG6gLAt8JqFQkHhVNQDhyvIGrgylFJYtCZVFeOaF/zFs+EiWr6x2F11KC2k6ncHOZMHQHR0kTQNb1tPGVGt5r5y2KISuN9qWQFkMzS1Wrus6mWwWK5VpeA/LRhNaA/IKIDSBtBWp2lo0n0kgFMS2LNJ1cbSA0y/2eo6kNU0jURsHIBgJOW2ujYMQRGKR0gJtSUmqqg6EwB8Nk0umHQPVBFg2ZjhEIOBHSlnvmTWUZSH8fiKRUANC2bSnOIOdzeIPhzBMk2w2h53NEoxF0XW91B4hBChVuhe6DlYB4fcTDAaIr1gFmiBYFi2Nha7rZDJZrHT9/hVg24jG+rc4hrV1INccw1C9MYyi69pa4UVCCNKZHDKVhoAf0zQppFJgGM69lSLktq++7WiaRqIuCUoSjIQRQrj/rYiURRuQJk3TsG2blDtmaAIKFno4iGkYpKprQdcJxSIlGzVNk3g8wYFHnMnXn30FWGCGED6TSDiErRqK/QuhOX0gNEKRkNPvtQkQEIlFsW2bYCzCE08N5s23P2TJ8pX4fGbxNYGu66RSaWQ264wTAmzLsdFwaL026hFAD+uFLQVhX4Gv53dkYU2MNtE0BVtHoEoxbb3ar2KPzsv5el4nIv68l7nalAtQwQm95+K+Y0uLpVQQMGxGzNye2oyfylDWI4CbEQVrK3WuKtA1WFeITzGhom3b1gx+/p+UlUV5fMAgbrjpfgrZnPtlyRP/GsTll/+BJx+9ncf++Xe+nTKdceMmEwgGuOaai9lph84MHvI2U6ZOJxAMApBNZzjr9BM4cL+9GPHpl3zw/qeEYhFyuTy2bXPWWSdxbL9DaNOqgiXLqxj037f4bvosHnr4NqSUPPH0i6SqaxscT2maRj6dZb/f7MFvTzma+QsWM/DF113y55Ck3n26c+5ZJ9O7Z1fy+QKjvhzHC88NIZPJrvXslmUjbYuzzjqZY/sd7LRlWRUDX3qT6dNn89DDt6GU4v+e/A+1NXF8AR+JugRdd9qec393Mrv17YFUiq/HTuKZAYNJpzJrEbdsNodhGFx51QWceNwRdN1pexLxBF+PncSz//4f0777gWhZtFHiVZzbyUSSU07ux7m/O5m+fXogEEydNoPBLw/l/Q8+IxKLUChYtK4o46pbr8ayLAY8819OPOdUjj7yIDShMeW7mbz48lvMn7cI0+/jjDNP5JQTjiIaCTF3wSKGvPouE8ZPbUAo1yKiuk4inqB3r25cdvG5HLjfHpSXlzF/wWI+GvEFz/7nVTKZDD6/D6XAKhRQCs499zT6HXkgrSrKWbR4KS+8+Abz5i/m7kdvJ5VO8+RTL5JIpTFNk0RdnB49unLu706hb69uFCyb0V+N5/lnXyKdzqw1hrZtU8gXOOOMEzmu3yG0aV3JsuUrefHlt5k85Xvuvf9mTNPgsX8NZOWqWkzTKBE5TdPIpjIcfMBe9D/tWMZNmMKoL8Zxxd+vpk/PrsSTaUaNHsuQV4dh2zaG4XxXCEEynuC4Yw/j/HNOY9ddeqFrgmnTZzHklXd4Z9gIQuEQqp4NBAN+Lv3T7znkoL2JRSLMmrOAZ/79MraU3PDAzSz6aTlPDBiEdL27ibo47du3Zq89d+Haqy4kGAgwc/Y83nrnI776agLhWNjx6rmEOJ1K0b//8Zxz5kn07d0d25ZM+34mL73yDu++9wnRWIRMKsOBB+zFKSf2Y86cBQx+6U1AczcYdey6W29OP+1Y+vTaGSEEM36Ywxtvf8ikSdOIlMVanCdQhFrv0rzvdqEhrQzRTgcQbrcX0sqAaFmeLl1TVKf93H/Cl5y/z3Rq0v6S+LMtBeWhHEMm9uKmdw+mIpTzyMtai4LjKe1UluSNC94lYDpH5WIND+HvXjyR6ctaOfGB2zqJFhqykKZm9lsoO8+WCg7XBFSE3SmstjY7gWQO0tmmvZSGoRNfWc3fb7uGu/9xLV9+PYHDjj4H0+9zFnHpLHYKRWLZCp5+/p9cdvE5vPPuCE4763L8wQCTxwyjR/edOO/iG3jpxdeJVlY4HplVNbw85EnOPvMkHh8wmGv+chvRNpUopRj83EP89pRj1mrPvwe+ysUXngVArz2P5YdJ33HGOafx2pAn+Xby9xzS7xxStXVc8qdzee5f9zJpynR+c9CphCIR4rV1nHXmSTz7xN2UlUUbXPebcZOoq0twTL9DuP/hZ7nl5vuIVFYgUAx+7p+cdsrR62zLzrsexcJFSynk8xx79KEMfv4h2rSubPD5KdN+YMHCnzj5hCN5+rkh/PnKvxMujxHwmbw25F8cceh+ANTU1FFeXoYQEE8kOeu8q/no48+JxCJrkUBN00gnUzx079+47pqLAUil0gghCIUcon33/U9yxz1PoJsmPXbajmnffgjAtO9nskufHg2ut2TpCvqfeSkXXXQ2l7jPVkQ2m+PiP9/MkP8NJVpWtpbHR9c1kvEkhx92AG+8/CQV5WUooLa6lorKcgC+/HoCp5zxJ9LZHCAIBXwMGfQox/Y7pJH+fY2LLzwTKSU79TyM5dU1ZNMZ+p96LM8NuJfKirIGn5846TuWL6/i+GMP55HHX+D66+8m2qocaUv+88wDnNn/+LXu8Z9Br3HB+aejaRp99j6OH2bOIxTyI10hWt3QSVRVc+ONl/PgfTexaPFS/H4/bds0HNuPRo7mzN//hYJlYRgGyXiCO/9+NbfdchUAmUwW25ZEXO/sI0+8wI03P0A4FiGXydKxfVte+e8T7LvPbg2um8nkeOWN4Vx4Xn9+WrKcnrv1A10jWZfg7LNO5l+P3rFWPwA8+H/P8ffbH8EfDCCEIJNM88iDt3D1lRcAEI8n8flMAgE/ALfe/gj3PfwMWDZ//vP5DHj0DsaNm8wBh59BOBYlXhvnL1ecz/133VCyq/p2cdtdj/LIY/8mHIuUQj88D6CHjXNMKDB1yXszduSsPWaiifovPEUmb3DITovpEEuVYgQ9NCQdmYLBYd0W0SaSoboegZZKEPXn+WpeJ6Yvr2wZ5O8XIlcFC9rGYPidJrEQuGE3WwUsGyqicM//bJ58WxKNQWMnOLYt8YVCHNvvEIeYDXkLO5cveZTqL/56OMyg/77FpRedzcEH7kOXLh1Z/NMyausSWJZFPpdv2AFCkEimsCyLVDoNukEykeLJR27nt6ccQzyR5L4Hn2LEJ18Si4S46soLufjCsygULLK5XGmRbqzzc7k8lmVRU1PnHEmm0uy55y4Meu4hAn4f7304iieeeIEVq2o4+MB9eODevxEKBkrPgtBIpdIM+L/bOe2Uo6mLJ7j/wacY8elXTluuuqjUFkd+RmBls/To3pX/DXqUsrIoo74Yyz8feYbFS6vYZ8++/POBm9ltl54l4obQSKfSPHLfXRxx6H7MmbuAK6+5g+++n0UkEuLKP5/PFX86j0HPPsheB55C1RreKV3XSdTU8bvfncx111xMNpvj+lse4J13RyA0jROOOZR/3ncTt918FeMmTmP40A8RQlBTW0csFmXHHbpw6Z9vYez4Key4QxfuvfM6+vTemY/eH0IsGubu+57k7Xc+oqI8xo03XM4xRx3Mk4/czpdfT2TJsip8Ph9KyXqe4gJt27Zi0PMPUlFexsD/vsF9DwwgmUyx2669eP6p+znogL35v4f+zoWX3IDQBM8+cRfH9juE6po67r7vCT4fPY6KsijXX/cnLr7wTAoFi5XVNU5CWzrDbrv05L8vPEwwGODjkaP5v0f/zbKVqzhw3z158P6b2GuPvqv7V9NIJFI8+sAtnNn/eFKpNPc+OICPR35JJBjgiiv+wEUXnEnBsshkc0gpG5+f9eypS+cOTPz2O35//tVUVddw9FEHc9stV3HMUQdz9RV/4O57n0Q3DE48/ghuu+UqLMvi5n88zCtvvAcI+h2+Pw8/cAvX/eWPjBs/lTfeGI4/GOA/zz7IvvvsxoKFP3H3fU8yftJ3dGrXhjv+cQ0Xnte/ZMuarpNKJDlg/7148d8PYxg6Lwx6jcEvvk4mW+C4Yw/l1puu4G9/vZTly1fy6JP/QQjBOWedzNVXXkBdPME119/N+x99jmEYXHLBmdxx29Xce+d1TJ46g/ffeZdCoeDcr7YOwzBI1CU47ZSjefzhfzjk9bF/88ab74EQnHH6ifz1LxfxT5ccv/rau0TLy1rMcbBHADcDpBKETIupP7Vl+rJW9Gm/0kliEMrVBDToUp7goJ1+4o3J3Sn3vIBr9V/AtDmu17y1RLOVEuhC8cGMHcjbOmGf5SV/bGZEghANbn0EsDwEfmNdJNYJjC8rj5UkVmbOnIfwmWvt8qVU6D6TRYuWsHzFSjq0b0PHju1YOH+RqxVoNJpJ6OgJGhi6AYUMvXfdgwvP749l2fz56tsZMvAljGgFlmUxatQY3nj9Wfqfeiy5XG49BFxgGE4wvKYJcpkcV/7p9wT8Pj4Z9TWnnn4plmVh+P1MHjeBWXMWMOz1Z/H5TKedhSx9duvFBef3x7Kdtrw8aAhGrAKr4LTlzTee47enHEM+n3eSArIZLv3j7ygrizJh4jRO/O3FpOIJjGCQqeMnMmPmHD4cNohoNOxmaWXp1rMP5/zuZDKZLL/93Z+ZOn4U6K3BLnDlZVfQtnUrzuh/PKedfDT/enIggdYVpYQIpSSGaXDphWchleKm2/7JgEfvA60dAM/M+gZbSp598h4uufAshr/1QYk46prGNdffxQvPvIC/oi1TJ3zLyuoaRr7/IrFomKeefYl/3Ho3ZqwVhXSaqT/8yLdfvUOXzh04/JB9GTTodQIBf2nToOsaqZoUp513Ol06deDbyd/zpyv/jmVLAsEAHw0fyUVK8bszTqZq5SpM06RXr26cfeZJ5HJ5Lrn8Zt569U2MWCVWPs+nn4/lg2EDOfboQ0pC3zKT5fJLziUYDDD6q/Gc0v9SstksRiDAlHET+GH2XIa//W+CgUBpDHv06svFF56JLSVX/fUuBj430LEn2+bzUWP43ysD+N0ZJzm6jOs5FTAMg+UrVnLaWZexaO4CfNEok8eNpaKinBv/egnHHXMo/3zs3+QyOS696Hcopbjzvid5+P47QLQBIRg4eyyZbI4hAx/lskvO5rWXXufIE47k8EP3oy6e4Mzz/sK4L77GiJUz9dvv+GrMRD4f+Qq779a7NH9s2+b6ay7BMHQG/vcNLr7wWkQohKbrTBgzhurqWp589E6uu/qPDHl1GCurVnH5xWejlOKhR55j0PODCLZuTyFf4M77niBvWfTs0ZXy8liDOanrBlJK/AEfN157KQD3PfQ0t/7tDvSI49H9ZvRXZDJZbv3bn7nxukt5d/gnLSoW0COAmwm6JqlOB/h45vbs0Xk5qbwP3CQGgUIqwQm95zF0WjdPE7CB98+Ry9mj0wp26bCSdL6h9p+p2yxLhPh8ThdCpqf9tyWQKzg/WxsBzBVgfac1SjkyKaabRWtZFkqIRvcMq+Ot8qUFk3qZkuskbJoAcuy5e29CoSDffT+TN97+kEi7jgic5I26mjr+9cxL9D/1WDdRZP0TXwinzdGKGPvsvRtKKZ55/mWsQoGKNq0oFCy0cCc+GvEFX349gSMO29+9bo49d+9LMBBg6rQfeHOo2xal0CM6ddV1DHjmJX57yjGlAH4RCPGbfZx7PD/oNVK1cSo7tCWfz2NEOvH1l+P5+JPR9D/1WOcWKkffXt2IRsJUrazm4AP34dCD9nXiFQ2dXDqBruvYts2eu/cFsTq5oeRxa9+WXfr2QBOC9m0queLKG9BMnxOfX8iz3XadUUrRvduOBMpjFAoWpmFg2TZffjMJX3kb/H4/Ztt2fDd9FgsXLmHnbjsw4rOv0QNRwuEQRMKsXLGKCROn0blTe9q1a+OM65rDKuA3e++KUoqPRoymkM5S3rY1hXyeWPt2fPbFOEZ+9AX4TMgX2Hfv3TBNg7HjJjN0+Ehi7TshpcSIhqmtqmbAsy85BNC1Kz0WZb9990ApxbP//h/ZTIaKtq0pFCz0SCc+/fRrvhg9nmP6HeyMocqx5+59iETCTP/hR1594z0ibTs4Gde6Trw2zoBnh3Bm/xPQtHVLFynX2zzt+1ksWrKcsnZtHTJWKOfTL8Zw0/V/om3rVvhMk2AwwG679kIIQUVZlD9feR2GL4BSCmkVaNe+DaDYuesOmNEYe+3RF6UUIz75inFfT6S8Yycsq4AZjVCzdAXPvvAKTz9xFwpFIV+gbbs2pX4e+OKbaKEQUTf+zvb7+N/rw/nb9ZfRpXMHevXsypR8gd69d0ZKyQcjvsCIVmLoOnpAQMDP/Q8OAFvii4TACJfCDDRNUMjm6NZ9J/bcow+5fJ7BL72FWVZJsBjHq+v858U3uPaqi9itb0926rY902f8SMhN6PII4MYfIrV4L1bQtBkxawcuPWAqhi6RbkCr0JxkkL27OJqAc1eVE/DKmLkLg6JgaxzTcz5hX4HqtFGq/SulIBbM88GMHVlUE22BnlPxC4wHVESgPLz1EcBoDAK+dfAopdANjVQqxfIVVbRt04r2HdogLMuRUZENe9a2bCpaldO2bWuy2RzVq2rAMJBKrnc0nH6RVFSUo5Ri/oLFWJaFz2diS0f4XTNNVlStapApvCGwbUUg4Kd1q3KEECxbvhJhmhQKlnvsJxBSMW/+ImB/l2RJKiudtixY+BOFgoXP53PaYoHmc7xBti1LlRgMn0lFeRlCCBb/tAzNV7yHQkobTQgWLFribsYcsY/yWASlFK1bVfCvR+9o8hnKK2Jr2a+UilAoQDgSBuCmG//c5PcrKmIYpllKUMjlC440iSu/Y9s2Ap1sLueQy0IBWynsYrauUmSzWVfKRmt0o4CmEY2EEEJQG4874v3SkUexLItwOIgei6BrGiuXrqDMffaFPy1DSulKrEisgoXwmfy0bEXJY2nbkmDAR1ksghCCpe4YWpY7hrZAA+bMW1i/h6ioKEMpxU9LlpMv5AkYQUeKRirHnlasIpPJ4vf7G5VNWROZTLZESIUAWyksNxRC13WUlAQCIWIxJ8b0r1df3PSYlsUwfSaVrs3Mm7/InVc2Uiosy0LzmSxesrzkmbOlpCwSIhoNY1k2q6prEe4moWjLqXSWZcur6NypPRXlZfgDPqLRCNlcjnTKyfouytmAorxVRcm7mF8jSQkpKYtG8PmcuRdPpUsbHnC0A+sSSaprauncqT2VLSwRZLMRQCWtFk1klBIEDIsfV5YxfmF7jtrZ0QQUriagZetUhrMc1X0hj3/RukHpuBZL/gBLarQKZTmy+0JX+0829IZIjQ9+2LGhJmCLMSob1JZ7OQnheNne+FIS8sPW9F6UEkIh+H6hQjdp1IvuaMkZJFbVMH7CVHbt25NTTjiKt199F03T0TRVWjRNv49k1SqOOeIgAgE/Eyd9x9wFi/H5fSWCKTSBLjSnQoEQKE2UavU6lxHE6+IIIejUqYOj2yaEo4+ma2DblMWiJd3ADfaKa4JsLk9NbR3t2rahdasKR5bG0CkUFJqrvdahfdt6xFRQV1tsSztHK65BWyRlrmxIoVBAaBpWoUBdPIFSinZtWyMLFoauOeTP1fRr16ZVvXtoVLv3+GnJcv50xS0l7T/h6tTV1iXQNUF1XQJ/JLSWdEgqlSGZTBIM+LnqmtuYN38hhhFwq7Jo1MYTKKmwbJtcLu88q1JNboWaEvxVat2CzsJhe8QTKZRSVJSVOaRd1xG2ja4bpFJpR37HZ4KARNJJVunQvo1bv1dDCCdbW9g2rdzkBmlLNF0jl8uTSKZLhFlZdklSRtc1JNCxQ9v6I09dXQIhBO3btcYwnPtqmkDXdJSdpawsQiDgd8WON2ROi3UQRceW0pkstXVxymIRrv/bPfzwwyxMn0M8dV0jnkg6JFRAJpkinkiilKJzpw5OKIWmYwvbeSbbprWbQKOkRNMEyUyOTCZLNBJ2PLRKuRJEztj6/SatKh1Sl0imyGZzxOsSlJXFiETCrvdWc9cGQe2qGrBsfNHw2ptkTZDKZLFtm3A4RMDnK80p50RAEQ4FKItFkUoRT6QQLahyyGYggM6RiZ2Pu7vElktqhHDEn9+fsSP9ui9o4KgQQpEr6BzTYz4vjN2FgqcJ6Gj/Zf2c2Huuo/1XL0GmSKjnrCpvgdp/CiF0ZCGFtPIIzWBzBz4qV2IlkYWrBthba7dg+CAQaJqcKqXQ/D6eH/gaF/y+P2edfgLDho/krVffxoiVo7tivKmqVeyy167c8NdLAHh+4KtkU2kMn4/aujhSSvbevQ+vDH6ZghUmm82hm0YpKcKBn8nTZpDL5+nbuzv9jjyQ9956l2CrNmQyBexUgvPPOdXx6skNK/OllHMUnaiJM3HS9/Tq0Y0Lz+vP0DffJ5FI4vP5qVu6jL3235uDDti7YVumTiefL7BLn570O/xA3h/6HsFWrUm7bfnDub91PYw2hq6jMhm+nfw9+++7B+efcyqDX3yD2roEgYCf2qUr6NZ7Z/oVBbSFAOFn2vTZ1NUlqKwoY+myKr79ZhT4WkPekdg54+zTqKiI8clnY1D1Bkkp5Xhllq1g0pTp9DviIEx/kPfeeQNM54gWK8UhR/WjR/edmPXjPKxstrRwbw57Bxg7fjIXnX86xx59MLff8zh1tXUEgkFSy1bQ74SjOPfsk1m+fCW33vF/TJj8HbZts9cefTj0oN/w2UefEmzVinQqj8xkOP+c09zNisTQdQrxJOMnTmWXPt254Pen8dqrw4jHk/j9PmqXLKfv3rty6MH7rjGGM0inM/Tq0Y3jjz6Et159i2CrNuTzBexkHeedfRq6rpcSeTa1D0zTYOWKVXw76Xu279KRcDTKe8PeBKON63pPsN+hR9C3984sWLQElc/z7eTvEULQ74gD6dGnOzOn/kCwsox4PIHQNM5zbV4BPtNk+YqVfD99Nm0PbcWpJx7F+NFfkjOd2NVs9QrOPv9stt+uEytXVjNz5lzitXG+mz6bgw/cm+OPOYyJX4/Bcr3TVj7PrTddSZ/eO/PuB5/xv8FDVouDK4Xu9zN//iJmzppHr55dOf6YQ3jq8afRWrVDKkmuporTL/09kUiIH2bN5cd5Cwj4VycHbfNrLh42o5fCKVf25ZzOLKqNOvWB65GdTEGnZ7tqftNlKXlLx6dLDKFa7I+uSXyazfG95iFEw12+dMWfR8zcnpp0AENricrtW97rKQREI1vpTxT8PtYZQyulJBwJM/abidx+7+MEAn5e/e8T3HbnjfTcaXsqKsvo0rk9F192Hh+9O4hOHdsz7L1PGDjodWIV5ViZLCM/+wpN0/jjhWdywSUXEI2E2a5jW5567E723KOve/ynwAwy9buZvPbmB5imwTNP3M2Z555BWVmUTu3a8ODDd3DZJecgZcO4QuVWy5BrEKT6v9MNg+deeAWlFCefeBTPP/cQ3XfcjlgszNEn9uOtV58m5urbSbm6La+/9T6mafDcv+7hzHNPpywWpXO7Njz0yJ1c+sffucdoTkku4ffz74Gvks3mOOyQffnvwEfo230nyqIRDj3yIIa9/pxTA1g6R5D4fMydu4D/vPgGwWCA118ewImnnUWn7TrRvU8Pnnrqfl576Qke++ffGx2kYg3jp54dghCC++++nmtuuIXtu25Ppx27cOmVl/HZR0N4bsC9dOjQ1hFzdsuHNeVBlVK6R+6qkb+p1WPVyPf80Qhvv/Mx8xf8xO679ub5p+51+jgS4pgTjuKFp+/jD+f+llgsgl2wmDTpe4Z/8BnBQIAXnrmf0848hbJYlO07tefRJ+/lvHNOdfpXuFVbfCbP/+dVbCk57pjDGPSfh+nVbQfKohGOPPZwhr76DBXlsdVj6AswY8aPvPLGexiGzoDH7uScP5xNWVmUjm1bc/cDt3HVn89HFsXy1knu1OrrNkL8bFs2ELd++vkhANx+y1XcdNud7LjzDnTcoTN/uPgiPv3wJZ5/6j6269IRze9n5KdfMv7babRuXcErgx/j8KMPoSwaoVe3HfjfS0+4camga5ozfpbNv575L0IIrr/mYq698WpaV5ZTWRHjD388nyce+Qe6rjHguSEsXrwUoQmees6xkRv/egkXX3YRZbEo7du24h+3/IV77vgrZ595EsuXrwRE6Vlt28Y0HL3BAc+9hBCCe+74KxdccgHlsShtW1Xyl+uu4u5/XIMQgseeHEiyNu5mqnsewI1fo4RA5hLOkVULPtVUgE+XLEuEGDWnM+fvPaOBJiA45c4ePPkLatMBdE22+IQQTUD7WIpkztfgmFfTJImcn49nbo9ft1Et6bjcOb/CyifdOWWyJQ3l1y6LZds2kfIY9z/4FJZlc8etf+Guf1zLXf+4ltraeCl7EGDosBH87ry/YPp9SOlUFnjuhVc48bgjOeiAvRj43IMkkimibtza0mVVdGjfxqk4ICXBYIAbbr6Pbjttx/777sGrLz1JIpFyMmeB9z/6nOOOPoRIJOQe2SkMw0DTNMKhUGm8TdP5XcitohGOhhk9eizX3XQfjzxwCxdfdBYXX3QW8XiSWCzC3PmLGDN2EvvvuwemaYB0slevv+l+uu60Pfv9Zve12vLeh6M4/phDCYeDSKkwQ0EmT53BZX+5jef+dS/nnHUy55x1MnXxBGWxKMuWr+SLr8ZzyIH7YPoMsCWR8hi33/M4O3fdgROPP5x333qeeCJJJBJGE4Kamjr6n3MFc+YsIFoea5BhaduScCzKsHdHcue9T3D7rX/h0Ydu5YF7bsC27JJe2y23P8wrr76LGXVi7sLhUImorIlwKITuZoE23C8pgkE/mqY5Y7XG/FFK4TMNVq6q4YJLbuD1l//F+eecxvnnnEZtXZzyMsdGhn/4GdfddD/BSAjLsrnq2jvYrktH9titN2+9+jTxRJJY1Kl28d4Hn3HCcYdTXhZBSYkvEuabsd9y9XV38fjDt/GH3/fnD7/vT108SVkswqLFSxn91XgOPnAf155sguEoN936ED2778QB++3JkEGPNrC/9z8cxZFHHEgoGGz6CNituqFpWkkqqIEXXXfK3EXCIWwpCcYijBg5mhtueZB/3vc37r/reu76x9XkcgUibt/f8+AAXnjhFaKV5WSSKS6+7GaGvvY0u+/Wm08/fIm6ujhlbp+9+c5H9D/lGHBjD8NlUYYO+5jb7nqUu/9xLf/34C3cf9f12NIm5CZnDPrvmzzwz2cIRUJomsbrb77Hnnv04cZrL+H5p+8n5Ypq+3wmtm1z5V/v4tNRX4MZRte10tyxLJtIeRnP//t/9O21M5dfei4Dn3uIVDqDaRilSiEPP/YCLwx6jXB5bK2yfdsydDPU7o5m9hm45XcM/JU9EC3cyagBltTJWTon9ZmLJVfLmggBEkHIsGkdyVAWyFEebNk/ZcEctmpoM1IJIn6LiYvb8tyYXQn6rJZFAFFomkk+voB8YhFCM/G0b37+bswX8PHZJ1/y7kejSCZTjgi0pjFr1lwWLlrCdl06kkpneO+Dz6ipSziLg3Di794c+iHpTNapdysl4ydM5bI/38KceYuoLC/jk1FjmPjtNILhMPFEklfffI9EPIluODVNJ03+nltuf5gBz/yXPXfrw7x5i3jznY+J1yVp174t3bvuwJSp03l/xBdYlk3HTu3pukNnJnw7jRGffInQNPyBAF98MZavxnzrlqSD6upa3hj6IRdcdB1CE1RWlPHp598wcVK9trzxPvF4Yq22PPWs05b58xfxxjsfkUymiURCjBs3mc+++AafzwdCUFeX4N33PuG8P1xLOp2hXdvWjPpiLN+Mn0wgGCSfz/Pam++zcPESNE3DNB05nbffHcEV197OuPGTiZbFGpXXUErh8/sYMXI0Y8ZNxtR1dN0gHk8wavQ4/vb3h/jPoNeJlEUdUhgM0Lf3zsyfv5i3ho0gmUy75c+c+LVd+3Qnnc7w7vufsPinZZi+Yva3Te8e3QgG/Iz87GumfjfTreahGrQlEPAza/Y8hg4fScGy0DUNy5J8O+k7Hh8wmJtvf8SN2dMxDJ3a2gSvvfk+yXQGw5UdmTBxGtff8gD/Gfw6e+zai1mz5vH2uyNIZ7OEwkG+/HI8o8dMKJGPmto6hr47gvMvvA7LsmnTqoJPP/+G8ROnEgyHSKbSvPL6cGrqHF07JWHy1OncdvfjPP7kQHbfpRdLliznjaEfEk+kGtSC1oSgULDp0rkD23XpwLgJU/lk1BiHIAOWbVNZUUafnjvz/YzZDHv/EwqWTSAYYNTnY/j8y/FOxq2mkUimGP3VeG694/946rkhhKIOsTV9PhYvWcZb73xcqgNtWZKvvpnIHy+5gQmTv+eC3/dnxYpV/HvQa06cX8DHyJFf8vXYSei65vZlnM9Hj+P2e5/gwYefwfCZpZhP0zR5/8NRTJ46A9MwMEyDlSur+eSzMVz7t3t54833KKsoI5/L06lTe3bcvhPjJ05l5GdfoxsGmq7zzvCRTPnuB3RdxzQNqlZW88lnX3PDLQ/w9HMvEQyHaWkemOavBFJ0fWk6ld1PQ/dFUcqmRccCArYSvHL+cHq1X0WmnrRJsbuUEng5wEVirNbwQGlUhLLc8t5BvDi+d8urnqIUwvATnz+SbPUPCCO4RZNBtqkdb716oEYwSCDop5DNg4A3X3maE449jCnTfuCYE//Aqpo6AsFAqYh9NpHEDAbx+U1SiRQIQSDgR9kSNIHuZqk6pMEmm0hgBgP4fD5S6QxYFqFYDCuXQ6EwfD6EpiEtG+XK0xgu6ZSWhbLs1b8rtV8jEU+CVISiYWzLJpdK4QuFnKB7y15PW/ykUmmwbcKxCIVc3llg/f7SK1rXdRKJJFg2wWgYJSXZZBojGMBn6NhuOrjuetKKiQXpeAJhGARDwVK7NrgWsFuFA1sSdDNx026d3/ql25RSWPmCc3zlMx35nXqvCyufBwWaaTjZvvX/ViggpFOvVzeNJhf7Uq3eTAZfKITpM8mmM9j5wlo1czVNo2BZ5OJJzHAQn+kjlUqBlISiEaxc3pGvcse1NIaJFNg2wUgYKSW5ZAozFHSSaOqNIS6xtWybbDyBEQzg9/tIpzIoyyYUc++hFKbf13iavhDYlgWWjdIEhmk29NfYEtvNBDb8vjVsLQW25dQC1twxUaxVCxghyGayyFSaUKsKhIBsJoedjNP/nP68MeRfTJn2A/sddnopRq9ky7ZNwK01nEmmAafWsFKsVZs4WZcAnHrRSimyyRToBtHYavkXadvO3Kn3rMJNgErWxQHR8PuaTqSsZVUA2bwEEIGSBcq7noAv2gUlt1zpqq1y0dEUNWk/Vx/yLTccMYHqtB/Dq/6x4XsJIF0w6D/wZJYnQ/g02eL8X0Jo1Pz4LoXUMs8DuKleeU1D00Qp7kk3dAq5AkG/j7dfe4bDDtmXz74Yy1nnXkkinXWyZt0Fq/QdXXdj2Bp6kFaPl2jk8678hpvIUNRmQ1CKCVSrhfJKb8w1Y9ac9ogSqSpmkq7pWWvQFleCY71tqddHxc+U7qEk9cPN1m6XXiLLzvPrpXisDSPnaz8XqLXKxzXV5uKzNta2+n9r6u8Nn1+4xEui3D7ThMCW9lq8cb1jrRrpK00rHYlu8hgK4RA5qdbx/lj3szfVb2uPyer/FkJQKFi0aV3B6y89ic9nctlVtzHhq3H4KhzJolx1NU8//zB/+uPZvPrG+5z9+6uIVsRKY9r4mNPkhmHNv+u6DvXkftb3rI1+vxEbaynYPDIwQoCysbO1iNj2ziCIlksAi5qAw6fvxKUHTMXUHa07TXiL+PpQsDU6xFIMn9CL+dWxllk7WQiklcXOJxDCy9va5PkoZSlrWNM0pC0xfSapbI7+51zJ9p07EAoFMHw+lKsrppQqxQYVExHW7bRt+vNrLdSqqKvX0Ovb1NthzcVqfd41pRwZlQ1qS70+auoeTbfLbrAA/9yKChv6XOsiOusidupnHO8VtQ/r95nckLHWNqx/bdmMY6jUeveD63v2pv6+9pisncm9bOkK4vEkx/Q7mJf+8wg3//0hpn03AyF0zr72Ei6+4CyEEAx88fW19C9/ri2v+femwgo25fueB7BZFqwcwcruxHboh7JyLZoAFr2A8ayPP+0/lb8f/Q2W1LDsDasG0DI9fwJNKIKGxeyVFVzw8rEsiYfxGXaLi/8Tmo98YjG1c4a7EjAeNo9nUGBZNrl8ASUlfr/PibnySvV48NDknMnn8rRr25rXXnqS/X6zOwC5XB5/vePkW29/hPseerrBcb6HbZUAIlDKQveVUdn9tyA8ouP0inOUechOP9F/t1lsVxFnwwpNtTDypxyZnKxlMHZBe4ZM7MWqdLBlVktRCmEESC2bQHLJGDQjgFc7cDPOUTdWyOl6Lyvfg4f1k0CNbDZLMBjg3LNOpt8RB9Gt6/bkXY3AV15/j89GfU04Fm2gBelhmyWAqxev8p1PwQy1Qcn1F6tuEZNFKJI5H1JByGd5FtiY2ThbCGypk84bhP0FfLrdMiulKIXQTWrnfkC+bgHC8HkE0IMHD1sdCbRtm0w8CbqO4TdRUmFnc2A0TNLwsPVg850nCQ1pZygkl+ALt0epfIs/BoaipEnerRLi9ce6oBs2AbdEXkstkyc0HbuQwspUIXTDI38ePHjY+tY1t751rFW5m/jjZkmHgrTkJIutnrhvvks78gD55JIWLwPTGAncFPIncDyJmlC/Gk4t3PZqQm2wJSjlSMCollojWUkQBlZqBbKQdkMpPHjw4GErfF25yTC27VRbKWaEe+Rv64WxGa0BoZlYqWXYuTp0XwylLI8IbgpbdwmfZWsUbCed3dAkpu7EKm2NXjJdczLU8raO7bbPp9tu1ZOW69n7OWw/F5/vev4EXiytBw8ePHhoFk6xeRcvHdvKkKudB5p3fLWpRCqdN6lJBzANm+0r4mxfWUfIV6Am4yeZ86H/DO/aZuctQiGEoi7jpy7noyyYY4fKOrqUJxACqtMBsgWjQVk8D2vOTgM7nyAfX+ho/3nizx48ePDgoZmweTUllEJoOrm6eYTa7urFAG4MkcLx+dSk/fxmu2X0320We3dZTptIGgHUZAJM+qkt70zrxqg5nQmZVsm79ovxFqHI2zqWLTipzxxO6D2Xvh1WEvPnsZVgaTzCmPkdeG1KD2Ysq6Q8mPM8gWvNHYmm+8nWzUAWUl71Dw8ePHjw0Lz8YrNmAQMIgbK9qiAb2XUoBXlb44oDp3D5QZMJGDaZgoElnTJHhiYJ+CykFLw0sRcPffobbCl+MRJYJH9lgRx3Hvs1x/eehy0FOUvHlhoIhalLgqZFTTrAg5/uwyvf9iTsK3gkcC0D0KiZPRQrXeVV//DgwYMHD80K3Qy1u2OzsxhpoZRFoGJnkLbnCfwZZCqe83PXcWO4+pBvqcv4SRfMBsTOloJMwSBnGRzSdTE7Vtbx7nddf5FSc07NYw2fYTP4nA85pOtiVqWC5G3dabNwBJ4tWyedNzE0xSm7/Eg86+fLuZ0J+QotN+GjPtzav/n4QtIrJqPpPo/8efDgwYOH5uUYW2Qx033k44sppFa4lQy8xWy9zNytHNJ/11mcv/f3LI2HEUJhaBIhVAN+rWsKXZMsjYc5rtd8Lj1gGvGcb4vH1wlNkc4b3HTkeHbvVMWKZAhDl+ja6mzlYgazoUmkElQlg1x32AQO2OEnkjnTK49Xb95kV81AeHGzHjx48ODhV0kAHWaAsnNkVk7zkkE2ELYURPwFLt1/GjlL3yD5FENTJLI+/vCb7+lYliRn6VvssF0TTpLKrh2r+O2us6lJ+/Hp645ZE0KhlIbPkPzpgKklL2FLJ35C91FILSUXX4Aw/N588eDBgwcPv1ICqCRC95OrnUMhuQThHWmtl0xlCga7dqyia+taMgVjgzxjwo2/axtJsd/2S53vbSEvoBCQs3QO77aYkLnh8Xy6JknlDHbvVMUOreJkC3oDD2eLgwBQpJZO8JI+PHjw4MHDr5wAugxB2Rap5RNwg8E8rINM5W2d3Tut2Ij6twpNwF6dl29Rx5FSjr7fHp1XYEntZ4V5WlKjIpSlT/tV5CwDraV6AYuZvzU/kksscjZKnvfPgwcPHjz8qgmgUgjDR75uIbna2d7R1npIHEDEV3AI4UZcIewrbNHTVAXoQhHyFZAbcWeBIuwrtGyTEDqykCG1bLwTK+tNDw8ePHjw8KsngC5LEJpBcsk4R9tM0/FWucbpEEAqb270FdJ5c4v2rJMBLMjkDbSNuLNCkM6bLTdBXEk03UdqxSTsbI2XLOXBgwcPHrYhAohyqhvkakku+catbuANwlq9pMDUbCb/1JasZfzMmDiBUvDtT223KJkqxh9OXtIWQ5M/y5Nn6IqadIDpyyvxGzZStbwBF7qffGIx6aqpCD3gxf958ODBg4dtiQDiJIQYAbLVP5CpmYVm+L3Fbg1IJQj5bKYuacO86hgBc8PiABVg6jZVyRDfzO9I0HQEorcMhxH4dZvPZnchU9hwT54tNUK+AlOXtmbuqjIChtXCtAAVQmhIO0t88Ree8Xvw4MGDh22UADpswTkKXjwaK1vr1TltBLomiedMnhuzKwHDRkqxXmepZevEAjkGT+jN4roIfsPeYg5WqQQhf4EpS9owdFpXKkNZ8ra2XtIohMKyNZ79eteWqQCjAN0ksWg0dqbaq/jhwYMHDx62EAFUZH+ROwsdaeWIL/gEpRR48YANYEtBWSDPG5N78PLEXnQoSyKVwJJag+NVpZzPWlKjfSzJiFnb8+xXuxH157HllqVUSgqCpsX9I/dl2tJWtI1kKEgNWzUkr8p9DiEU7SJpHvt8T0bP7UzU38LKwSmJZgRJr5hMrmYWwvCOfj148ODBw5aBbobb/x4hWrvsa4uuvkIzsHM1SCtBoLxrvcXPKwfmdIPAp9t8+uN2SCnYf/tlRHwFFKJElExdEvFbBEyLIRN78/f3DgaBq/+35ftR1xRZy+DjmTuwXUWC3TquxNQkUmmOxw/wGZJYIEfeNrjzo/0ZOG4Xov58iyN/wvCTq5tHYtHnnufPgwcPHjxsYQIYancBgo6/BAEEt+pBcikCgb98R5Rd8GoF1+eAOMLQn/64HeMXtkcJiPoLhEwLBFSnAoye25mHPt2H57/ZFc0tC/dLxdEpBKYuSeZ8DP++K3NWlWPqklggj8+wkUqwqDbK8OldueOjA/ho5g5E/fmWFfenJJoRwEqtoG7O+wgh+KWiMTx48ODBQwuEIiWCrfoO1DT9AqVsG4T+y7AcDWlliXTcj0j7vZFW1iOBa5JATZHMmRRsjTaRDG0iaQRQnQ6wPBFCCBwi5WYB/9LQhEIB8awfXUg6lKUpC+SwpWBZIkx12k/AsAn5rC1+VP1Lkz+h+7GzNdTMGY6yMk74g6eJ6cGDBw8etgT1E0II+NYQmj69Hs/4xRZFzfCTWjIGAYQ9ErjGaDlxfmFfASEgUzCYs7IcAEOXlAXzKMVWdYRabEt5MIdSsCoVYEUiiMDJVK4M5Urxiy2K/Bl+7EwNtXOGI60UQvN5cX8ePHjw4GGLLUQCTZdSzjaEVBOVQIL4Zc+glEIYAZJLxgAQ7rAP0sr84tx0a4JUTgk9XSgM03K7TWzVJKrYNlOX+HTHK8hW3ubNRv70AFamirq5H2IXUgjdy3734MGDBw9bFAJAE/oYLaX5p4NaVlymfnESqAdILhlLfOEoxzsiNG+RXLObXDIolfjVpA0UPZTqV9Tm5iR/mhkin1hAzayh2IW0S/68Y18PHjx48LBF+Z9QSlqaYLxG1fhloE0UQmPrYFoKYfhJV02hbt6HIC2E7olFe/g1Ej8nr0oYQdIrplA790PHE+jF/Hnw4MGDhy0P6WQdijkJTU7RAJS0P96qjlmVRDNCZGvnUDP7HQrpFWhmCMf35S2cHn4N5E86Xj4kiQWfEl/0BUJoTp6VR/48ePDgwcOWX5iUEJpSii9YPjWlAUhLfaCUnXWzgLeO1ckVybWyNdT+OIzU8m8RmoHQDM8b6GFrnl8Aju2mV1Iz+x3Sq753Sh46H/D6yIMHDx48/BIQKCWEkEMBdECzc1XVRqjdQZrQimrMW4komXKOy4Bc3TwK6RWYobYY/nKUsp2mepnCHrYSWy3GsAolSa2YRHzRKGQhhWYEPK+fBw8ePHj4JSERmoZSc9K6dhOp5QUdDtVhgfT72xbQ9DNAsjWyKqH7sLI15Gpmo6SNGWqDZgQ8IuhhKyF+JprmIx+fT92CT8hWz0TTDC/ez4MHDx48bA1rlRRC15TgaWvF1I/gDF3gBv+1atUjkhG+bxGiq1OcdyssTSAEKIm08xjBSsLt9iZQvhNoBsrO1yOCHhn0sLnnknI3JiZC6BQyVaSWTSRXO7e0YfFCFTx48ODBw9awYrn/pnUKeyZW/jALpwQBwBl6JvNF1h9ub4F2IshfXhewaRaI0H1IK0O25kfyiZ8QmoERKEMzgs5zFhdezyvoobnnkFIgNITuQ2gahfQKUssmkPjpK+x0lfP7rSiU1oMHDx48tPi1yxbC0EENTK2cPhjO0GF6SY3X+bdN73BIGRMQdN9qvYD1iCACp3awUhjBSgIVO+Mv3wkjUOHkC0sLpO0sxp5n0MNGET5KmwmhGQihIa0c+cQiMqt+IJ/8CSUtNN3neqg94ufBgwcPHramhQwFImELfe9c1aQ5LhmS9Wv/6qSrcv5AuyXo2lmordkLWI8GCh2h60grTS6+kFzNHKx0FUrZ6EYQzQyi6abLZd1+UPXkZASs8X88tLi5Uf//qtWETxiO7WgmKAsrvZL0yu9I/vQ1mVXfY+frEJrpyr14xM+DBw8ePGx1a5wtNENHqQeyK6e8XfT+NcZ6dECFWu8yVAjtJKVs25WG+RVAuB4YG2UXUIDui2KG2mBGO+ELtUPzxdAMP0Lo7lqvAAlKodx/PbQkCEebD5y9jnBtSCqULCDzcQrpKgqpZeRTy5C5OqQsOKSvGD3h2YwHDx48eNg6yZ+T+Qs/pPNif+pOjcOdJQ+YseanASkt4yph2AcKoVWgtiZZmHU+aL3KC34EoKwsudq5ZGvnoOk+NCOE7i/DCLZC98XQfRE0M4TQfG4Wp4nyPDkthvyhLOxCFgC7kEIW0tjZGqxcHXauFjufQFpZt3CijhC6E2eqlEf8PHjw4MHDVk6KhBJgI7mCuim1cGrxKLS4Cq6JM3R43Q622eV0De11pWzL9QL+Os9Ii7F/SoKSq2VjwAnm10wQGprur3eU5x0Ht4jZoSSykHb3SXkn96k4/kJzRMeLiUT1j4c9ePDgwYOHrXuFKwjNMJWU/0ivnHp3kds1oEeNf9Elga13uU/T9JuVtCwQxq+/Q4T7xGI1QXbjAVXRq+NxvxazN3KiBorOba2ebSiP8Hnw4MGDh1/rAmcJzTCUst9NV007Fc4Q8Lpcc1ETTTOlMzTorUKth74uhPitUtsKCVwPQfTQApmgBw8ePHjwsE2saTZC14VkkpbP9Uskfqh2yY38OYzH+Vu3br5QXehDIbTDth1PoAcPHjx48ODBwzZF/iyEbqDUAoS1X7pq+jKcHI5GqxKsK7lDwe2CH3/MkbLOVkqNFkI3QFleJ3vw4MGDBw8ePGw95E8I3UAxTyl5ukP+ztCbIn+wYWeeDnvs1s0fqg29IjT9VCULv+7EEA8ePHjw4MGDh22A+bmVPgyUnCKlOjFTPW0xjqyfva4v6ht2cTSqq61COjrUDBlRIfQDQDmie78CsWgPHjx48ODBg4dtDBKUEJqpK6WGaYZ2Rrpqquv5m26v78s/x4PnpkeCv81uF+qoRwWi7FcvE+PBgwcPHjx48PDrgXKSPTQDhEKoO9Mrpt7tEMKmY/7WhPbzboiAM/Rc1ZSBwuIghRglNMMVS1MWXkqlBw8ePHjw4MHD5iJ+FiAc7sV0UMelV0y90yV9YkPJH2y81654tqwHW+3+JyHkTULTuihp4x4LC34V1UM8ePDgwYMHDx62ari1aoUuNB2lZFyhPZHJq0ecCh+HGvC5zc90wm3KsW3JzRhstUdHhHWlQFwsNK2NUhJXWVl6ZNCDBw8ePHjw4GFjSB8CoWkCDaVUCiGGaHbhsWT19zPcz6032WNzEEAXhxrwubWaCMqzEJynwR4gnNq6SlL8H48QevDgwYMHDx48NIBi9TGuhhAINBAKJdWPQojXhK29lKye5BK/M/TGqntsYQJYvM4ZWr06c2a41W4HKjhJCXWUQPUWQjNU8XhaeaGCHjx48ODBgwcPDosSCJeSKefwdKaC0UKJoUGV/WLVqpmJesSvSBY3lbg17yPAoXrRI+hgLzPYKru70P17KGkdIFC9gJ5AjFJiiQcPHjx48ODBQ4tEFsQslJwlhDZGCDE2KbNTKZE+cE9bZXMQvyL+H1GjMylZABsvAAAAAElFTkSuQmCC" alt="Soluções Comerciais - Qualidade em cada negócio" width="200" height="75">
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

function statPncp(p) {
  if (!p) return "";
  if (p.carregando) return `<div class="stat"><div class="lb">${ICON.contrato} Contratos no PNCP</div><div class="v c-acc" style="display:flex;align-items:center;gap:8px"><span class="spin" style="width:16px;height:16px"></span></div><div class="s">buscando no PNCP…</div></div>`;
  if (p.erro) return `<div class="stat"><div class="lb">${ICON.contrato} Contratos no PNCP</div><div class="v c-acc">—</div><div class="s">PNCP lento ou fora do ar</div></div>`;
  return `<div class="stat"><div class="lb">${ICON.contrato} Contratos no PNCP</div><div class="v c-acc num">${(p.total || 0).toLocaleString("pt-BR")}</div><div class="s">${brl(p.valor_total)} · 12 meses</div></div>`;
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
    else if (p.erro) html += `<div class="aviso">${esc(p.erro)} ${p.tentar ? `<button class="btn-o" type="button" id="pncp-de-novo" style="margin-left:8px">Tentar de novo</button>` : ""}</div>`;
    else if (!p.total) html += `<div class="faixa t-neutro">${ICON.alerta}<div><b>Nenhum contrato no PNCP</b><p>Este órgão não publicou contratos no Portal Nacional de Contratações Públicas entre ${esc(data(p.inicio))} e ${esc(data(p.fim))}.</p></div></div>`;
    else {
      html += `${p.obs ? `<div class="aviso">${esc(p.obs)}</div>` : ""}
      <div class="resumo-pncp">
        <div class="mini"><span>Contratos publicados</span><b class="num">${p.total.toLocaleString("pt-BR")}</b></div>
        <div class="mini"><span>Valor somado${p.somados < p.total ? ` (dos ${p.somados.toLocaleString("pt-BR")} contratos lidos)` : ""}</span><b class="num">${brl(p.valor_total)}</b></div>
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
      <p class="hint" style="margin:0">Fonte: PNCP, Portal Nacional de Contratações Públicas. Mostra os contratos publicados nos últimos 12 meses.</p>`;
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
  const novo = document.getElementById("pncp-de-novo");
  if (novo) novo.addEventListener("click", () => {
    d.pncp = {carregando: true};
    render(d, "contratos");
    carregarPncp(d, (d.orgao || {}).cnpj || "");
  });
}
</script>
</body>
</html>
"""


def aquecer():
    """Baixa as listas grandes com calma, depois que o site já abriu, e renova a cada 12 horas."""
    time.sleep(60)  # deixa o site ligar e atender primeiro
    while True:
        for f in (lista_entes, lambda: capag_tabela("municipios"), lambda: capag_tabela("estados")):
            try:
                f()
            except Exception:
                pass
            time.sleep(20)
        time.sleep(12 * 3600)
        with _lock:
            for k in ("entes", "capagmunicipios", "capagestados"):
                _cache.pop(k, None)


def main():
    na_nuvem = "PORT" in os.environ or "--publico" in sys.argv
    srv = ThreadingHTTPServer(("0.0.0.0" if na_nuvem else "127.0.0.1", PORT), Handler)
    if os.environ.get("PORTAL_TRANSPARENCIA_CHAVE"):
        threading.Timer(30, iniciar_lista_orgaos, args=(os.environ["PORTAL_TRANSPARENCIA_CHAVE"],)).start()
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
