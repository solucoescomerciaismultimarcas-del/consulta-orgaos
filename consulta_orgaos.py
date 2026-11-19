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
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8000"))
SICONFI = "https://apidatalake.tesouro.gov.br/ords/siconfi/tt"
PORTAL = "https://api.portaldatransparencia.gov.br/api-de-dados"
CKAN = "https://www.tesourotransparente.gov.br/ckan/api/3/action/package_show?id="
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
def federal(info, chave):
    H = {"chave-api-dados": chave}
    nome = info["razao_social"]
    candidatos = []
    for termo in (nome, " ".join(nome.split()[:4])):
        try:
            r = http_get(f"{PORTAL}/orgaos-siafi?" + urllib.parse.urlencode({"descricao": termo, "pagina": 1}), H)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return {"erro": "A chave do Portal da Transparência foi recusada. Confira se copiou a chave certa."}
            return {"erro": f"Portal da Transparência respondeu {erro_txt(e)}."}
        except Exception as e:
            return {"erro": f"Portal da Transparência indisponível: {erro_txt(e)}."}
        candidatos = [c for c in (r or []) if c.get("codigo")]
        if candidatos:
            break
    if not candidatos:
        return {"erro": f"Não encontrei '{nome}' na lista de órgãos do SIAFI."}
    alvo = next((c for c in candidatos if norm(c.get("descricao")) == norm(nome)), candidatos[0])
    cod = str(alvo["codigo"])
    hist = []
    ano_atual = date.today().year
    for ano in range(ano_atual - 5, ano_atual + 1):
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
        hist.append({"ano": ano, "empenhado": emp, "liquidado": liq, "pago": pago,
                     "pct_pago": (pago / liq * 100) if liq else None, "parcial": ano == ano_atual})
        time.sleep(0.4)  # respeita o limite de requisições do Portal
    return {"orgao_siafi": {"codigo": cod, "descricao": alvo.get("descricao")},
            "outros": [{"codigo": c.get("codigo"), "descricao": c.get("descricao")} for c in candidatos[:6]],
            "historico": hist}


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
        if u.path == "/api/consulta":
            q = urllib.parse.parse_qs(u.query)
            try:
                res = consultar(q.get("cnpj", [""])[0], q.get("chave", [""])[0].strip())
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
.top{display:flex;align-items:center;gap:12px}
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
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:18px}
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
  .score{grid-template-columns:1fr}
  .badges{justify-content:flex-start}
}
@media (max-width:420px){.score-l{flex-direction:column;align-items:flex-start}}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div class="logo" aria-hidden="true"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 21h18M4 10h16M12 3l8 5H4l8-5zM6 10v8M10 10v8M14 10v8M18 10v8"/></svg></div>
    <div><h1>Consulta Pagador Público</h1><p>Veja se um órgão do governo costuma pagar os fornecedores em dia</p></div>
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
  brilho:'<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l1.8 5.2L19 9l-5.2 1.8L12 16l-1.8-5.2L5 9l5.2-1.8zM19 14l.9 2.1L22 17l-2.1.9L19 20l-.9-2.1L16 17l2.1-.9z"/></svg>'
};

try { const k = localStorage.getItem("chave-portal"); if (k) $("#chave").value = k; } catch(e) {}
$("#cnpj").addEventListener("input", e => e.target.value = mascara(e.target.value));
document.querySelectorAll(".chip-ex").forEach(b => b.addEventListener("click", () => { $("#cnpj").value = mascara(b.dataset.cnpj); $("#f").requestSubmit(); }));

$("#f").addEventListener("submit", async ev => {
  ev.preventDefault();
  const cnpj = $("#cnpj").value.replace(/\D/g,"");
  if (cnpj.length !== 14) { $("#out").innerHTML = `<div class="erro">O CNPJ precisa ter 14 números.</div>`; return; }
  const chave = $("#chave").value.trim();
  try { chave ? localStorage.setItem("chave-portal", chave) : localStorage.removeItem("chave-portal"); } catch(e) {}
  $("#btn").disabled = true;
  $("#out").innerHTML = `<div class="carregando"><div class="spin"></div>Consultando Receita, Tesouro Nacional e CAPAG. Se o site estava parado, a primeira consulta pode levar até 1 minuto.</div>`;
  try {
    const r = await fetch(`/api/consulta?cnpj=${cnpj}&chave=${encodeURIComponent(chave)}`);
    const d = await r.json();
    if (!r.ok) throw new Error(d.erro || "Falha na consulta.");
    render(d);
  } catch (e) {
    $("#out").innerHTML = `<div class="erro"><b>Não deu para consultar.</b> ${esc(e.message)}</div>`;
  } finally { $("#btn").disabled = false; }
});

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

function render(d) {
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
    </div><hr class="sep">`;
  }

  const abas = [["resumo","Resumo"]];
  if (h.length) abas.push(["historico","Histórico"]);
  if (d.federal) abas.push(["federal","Pagamentos federais"]);
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
}
</script>
</body>
</html>
"""


def main():
    na_nuvem = "PORT" in os.environ or "--publico" in sys.argv
    srv = ThreadingHTTPServer(("0.0.0.0" if na_nuvem else "127.0.0.1", PORT), Handler)
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
