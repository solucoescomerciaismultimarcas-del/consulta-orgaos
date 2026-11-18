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
    return "privado"


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
    """Soma as colunas da linha total de um anexo de restos a pagar da DCA."""
    linhas = {}
    for it in items:
        conta = it.get("conta") or it.get("cod_conta") or "?"
        col = norm(it.get("coluna"))
        v = to_num(it.get("valor"))
        l = linhas.setdefault(conta, {"inscritos": 0.0, "pagos": 0.0, "cancelados": 0.0, "liquidados": 0.0})
        if "inscrit" in col:
            l["inscritos"] += v
        elif "cancelad" in col:
            l["cancelados"] += v
        elif "pago" in col:
            l["pagos"] += v
        elif "liquidad" in col:
            l["liquidados"] += v
    if not linhas:
        return None
    conta, l = max(linhas.items(), key=lambda kv: kv[1]["inscritos"])
    if l["inscritos"] <= 0:
        return None
    l["linha"] = conta
    return l


def dca_ano(id_ente, ano):
    item = {"ano": ano}
    try:
        p = resumo_rp(cached(f"dcaG{id_ente}{ano}", 21600, lambda: siconfi(
            "dca", {"an_exercicio": ano, "no_anexo": "DCA-Anexo I-G", "id_ente": id_ente})))
        np_ = resumo_rp(cached(f"dcaF{id_ente}{ano}", 21600, lambda: siconfi(
            "dca", {"an_exercicio": ano, "no_anexo": "DCA-Anexo I-F", "id_ente": id_ente})))
    except Exception as e:
        item["erro"] = erro_txt(e)
        return item
    if p:
        saldo = max(p["inscritos"] - p["pagos"] - p["cancelados"], 0)
        item.update(inscritos=p["inscritos"], pagos=p["pagos"], cancelados=p["cancelados"], saldo=saldo,
                    pct_pago=p["pagos"] / p["inscritos"] * 100,
                    pct_cancelado=p["cancelados"] / p["inscritos"] * 100)
    if np_:
        item["np"] = {"inscritos": np_["inscritos"], "pagos": np_["pagos"], "cancelados": np_["cancelados"],
                      "liquidados": np_["liquidados"]}
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
    for i, r in enumerate(rows[:25]):
        n = [norm(x) for x in r]
        if any("capag" in x for x in n):
            hi = i
            break
    if hi is None:
        return None
    hdr = [norm(x) for x in rows[hi]]
    orig = [str(x).strip() for x in rows[hi]]
    cols_capag = [i for i, h in enumerate(hdr) if "capag" in h]
    pref = ([i for i in cols_capag if "oficial" in hdr[i] or "final" in hdr[i]]
            or [i for i in cols_capag if hdr[i] == "capag"] or cols_capag[-1:])
    col_nota = pref[0]
    col_ibge = next((i for i, h in enumerate(hdr) if "ibge" in h), None)
    col_uf = next((i for i, h in enumerate(hdr) if h in ("uf", "sigla", "sigla uf")), None)
    col_nome = next((i for i, h in enumerate(hdr) if h in ("ente", "nome", "municipio", "estado", "nome do ente")), None)

    def bate(r):
        if col_ibge is not None and col_ibge < len(r):
            v = digitos(str(r[col_ibge]).split(".")[0])
            if v and (v == cod or (len(cod) == 7 and v == cod[:6])):
                return True
        if tipo == "estados" and col_uf is not None and col_uf < len(r):
            if str(r[col_uf]).strip().upper() == ente.get("uf"):
                return True
        if col_ibge is None and col_nome is not None and col_nome < len(r):
            if norm(r[col_nome]) == norm(ente.get("ente")) and (
                    col_uf is None or str(r[col_uf]).strip().upper() == ente.get("uf")):
                return True
        return False

    for r in rows[hi + 1:]:
        if bate(r):
            det = {orig[i]: str(r[i]).strip() for i in range(min(len(r), len(orig))) if orig[i] and str(r[i]).strip()}
            nota = str(r[col_nota]).strip() if col_nota < len(r) else ""
            return {"nota": nota, "coluna": orig[col_nota], "arquivo": t["nome"], "url": t["url"],
                    "detalhes": dict(list(det.items())[:14])}
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
def avaliar_rp(hist, capag):
    anos = sorted((h for h in hist if h.get("pct_pago") is not None), key=lambda h: h["ano"])
    if not anos:
        return {"nivel": "sem_dados", "titulo": "Sem dados suficientes",
                "motivos": ["O ente não tem declarações de contas anuais (DCA) disponíveis no SICONFI para os últimos anos."]}
    rec = anos[-3:]
    insc = sum(h["inscritos"] for h in rec)
    pct = sum(h["pagos"] for h in rec) / insc * 100
    canc = sum(h["cancelados"] for h in rec) / insc * 100
    periodo = f"{rec[0]['ano']}–{rec[-1]['ano']}" if len(rec) > 1 else str(rec[0]["ano"])
    motivos = [f"Pagou {pct:.0f}% das dívidas já liquidadas com fornecedores (restos a pagar processados) em {periodo}."]
    if canc >= 1:
        motivos.append(f"Cancelou {str(round(canc, 1)).replace('.', ',')}% dessas dívidas. Cancelar despesa já liquidada é sinal de alerta.")
    if pct >= 85 and canc < 5:
        nivel = "bom"
    elif pct >= 65 and canc < 15:
        nivel = "regular"
    else:
        nivel = "ruim"
    if len(anos) >= 2:
        d = anos[-1]["pct_pago"] - anos[-2]["pct_pago"]
        if abs(d) >= 5:
            motivos.append(f"Em {anos[-1]['ano']} o índice {'melhorou' if d > 0 else 'piorou'} {abs(d):.0f} pontos em relação a {anos[-2]['ano']}.")
    nota = (capag or {}).get("nota", "").strip().upper()
    if nota:
        motivos.append(f"Nota CAPAG do Tesouro Nacional: {nota}.")
        if nota[0] in "CD" and nivel == "bom":
            nivel = "regular"
            motivos.append("A nota CAPAG baixa rebaixou a avaliação para regular.")
        if nota[0] == "D" and nivel == "regular":
            nivel = "ruim"
    return {"nivel": nivel, "pct_pago": pct, "pct_cancelado": canc, "periodo": periodo,
            "titulo": {"bom": "Bom pagador", "regular": "Pagador regular — atenção", "ruim": "Mau pagador — alto risco"}[nivel],
            "motivos": motivos}


def avaliar_federal(fed):
    anos = [h for h in fed.get("historico", []) if h.get("pct_pago") is not None and not h.get("parcial")]
    if not anos:
        return None
    rec = anos[-3:]
    liq = sum(h["liquidado"] for h in rec)
    pct = sum(h["pago"] for h in rec) / liq * 100 if liq else 0
    nivel = "bom" if pct >= 90 else "regular" if pct >= 75 else "ruim"
    return {"nivel": nivel, "pct_pago": pct, "periodo": f"{rec[0]['ano']}–{rec[-1]['ano']}",
            "titulo": {"bom": "Bom pagador", "regular": "Pagador regular — atenção", "ruim": "Mau pagador — alto risco"}[nivel],
            "motivos": [f"Pagou no mesmo ano {pct:.0f}% do que liquidou (serviço entregue e aprovado) entre {rec[0]['ano']} e {rec[-1]['ano']}.",
                        "O que sobra vira restos a pagar processados e costuma ser pago no início do ano seguinte."]}


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

    ano = date.today().year
    anos = list(range(ano - 7, ano))
    ente, como = None, None
    try:
        if esf == "federal":
            ente, como = {"cod_ibge": 1, "ente": "União (Governo Federal)", "esfera": "U", "uf": "BR"}, \
                "órgão federal: o histórico abaixo é da União como um todo"
        else:
            ente, como = achar_ente(info, esf)
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
        out["avaliacao"] = avaliar_rp(out["historico"], out.get("capag"))
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
    for anexo in ("DCA-Anexo I-G", "DCA-Anexo I-F"):
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
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Serif:wght@600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* Layout: coluna única tipo "certidão" — cabeçalho, busca, parecer, histórico, fontes */
:root{
  --bg:#f5f6f3; --paper:#ffffff; --ink:#1c2420; --muted:#5d6a63; --line:#d9ded8;
  --accent:#1f5f4a; --accent-soft:#e3efe9;
  --good:#1d7a4c; --good-bg:#e2f3e9; --warn:#a36a00; --warn-bg:#fbf0d9; --bad:#b3261e; --bad-bg:#fbe4e2;
  --np:#9aa8a0;
  --f-display:"IBM Plex Serif",Georgia,serif; --f-body:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --f-mono:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
  color-scheme:light;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#121715; --paper:#1a211e; --ink:#e6ece8; --muted:#9aa8a0; --line:#2e3833;
  --accent:#6cc3a0; --accent-soft:#1f2f29;
  --good:#5fcf94; --good-bg:#183326; --warn:#f0b54a; --warn-bg:#352a14; --bad:#ff8a80; --bad-bg:#3a1c1a; --np:#4f5d56;
  color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 var(--f-body)}
.wrap{max-width:920px;margin:0 auto;padding-block:32px 64px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));display:grid;gap:24px}
header h1{font:600 clamp(26px,4vw,34px)/1.15 var(--f-display);margin:0 0 6px;text-wrap:balance}
header p{margin:0;color:var(--muted);max-width:64ch}
.eyebrow{font:500 12px var(--f-mono);letter-spacing:.08em;text-transform:uppercase;color:var(--accent)}
form{background:var(--paper);border:1px solid var(--line);border-radius:10px;padding:20px;display:grid;gap:14px}
.row{display:flex;gap:10px;flex-wrap:wrap}
label{font-weight:600;font-size:13px}
input{font:500 20px var(--f-mono);padding:12px 14px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);flex:1 1 260px;min-width:0}
input.chave{font-size:14px}
input:focus,button:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
button{font:600 15px var(--f-body);padding:12px 22px;border-radius:8px;border:0;background:var(--accent);color:var(--paper);cursor:pointer}
button:disabled{opacity:.6;cursor:wait}
.chips{display:flex;gap:8px;flex-wrap:wrap;align-items:center;font-size:13px;color:var(--muted)}
.chip{background:var(--accent-soft);color:var(--accent);border:0;padding:6px 10px;border-radius:999px;font:500 13px var(--f-body)}
details summary{cursor:pointer;color:var(--muted);font-size:13px}
details .row{margin-top:10px}
.hint{font-size:13px;color:var(--muted);margin:6px 0 0}
a{color:var(--accent)}
.card{background:var(--paper);border:1px solid var(--line);border-radius:10px;padding:20px;min-width:0}
.card h2{font:600 18px var(--f-display);margin:0 0 12px}
.orgao h2{font-size:22px;margin-bottom:4px}
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px 20px;margin-top:14px}
.meta div span{display:block;font:500 11px var(--f-mono);letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.parecer{border-left:6px solid var(--c);background:var(--cbg)}
.parecer.bom{--c:var(--good);--cbg:var(--good-bg)} .parecer.regular{--c:var(--warn);--cbg:var(--warn-bg)}
.parecer.ruim{--c:var(--bad);--cbg:var(--bad-bg)} .parecer.sem_dados{--c:var(--muted);--cbg:var(--paper)}
.selo{display:inline-block;font:600 13px var(--f-mono);letter-spacing:.06em;text-transform:uppercase;color:var(--c);margin-bottom:4px}
.parecer h2{font-size:26px;color:var(--c);margin-bottom:10px}
.parecer ul{margin:0;padding-left:18px;display:grid;gap:4px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-top:16px}
.stat{background:var(--paper);border:1px solid var(--line);border-radius:8px;padding:12px}
.stat b{display:block;font:600 24px var(--f-mono);font-variant-numeric:tabular-nums}
.stat span{font-size:12px;color:var(--muted)}
.aviso{background:var(--warn-bg);border:1px solid var(--warn);color:var(--ink);padding:12px 14px;border-radius:8px;font-size:14px}
.erro{background:var(--bad-bg);border:1px solid var(--bad);padding:12px 14px;border-radius:8px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px;font-variant-numeric:tabular-nums}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{font:500 11px var(--f-mono);letter-spacing:.05em;text-transform:uppercase;color:var(--muted)}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font:600 12px var(--f-mono)}
.pill.bom{background:var(--good-bg);color:var(--good)} .pill.regular{background:var(--warn-bg);color:var(--warn)} .pill.ruim{background:var(--bad-bg);color:var(--bad)}
svg text{fill:var(--muted);font:11px var(--f-mono)}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-top:6px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.carregando{display:flex;gap:10px;align-items:center;color:var(--muted)}
.spin{width:18px;height:18px;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:g 0.8s linear infinite}
@keyframes g{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.spin{animation:none}}
#out{display:grid;gap:24px} #out:empty{display:none}
.metodo{font-size:14px;color:var(--muted)} .metodo p{margin:0 0 8px;max-width:72ch}
footer{font-size:12px;color:var(--muted)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="eyebrow">Restos a pagar · CAPAG · SICONFI</div>
    <h1>Consulta Pagador Público</h1>
    <p>Digite o CNPJ de uma prefeitura, governo estadual, secretaria ou órgão federal e veja se ele costuma pagar os fornecedores em dia.</p>
  </header>

  <form id="f" autocomplete="off">
    <label for="cnpj">CNPJ do órgão</label>
    <div class="row">
      <input id="cnpj" inputmode="numeric" placeholder="00.000.000/0000-00" maxlength="18" required>
      <button id="btn" type="submit">Consultar</button>
    </div>
    <div class="chips">Exemplos:
      <button type="button" class="chip" data-cnpj="46395000000139">Prefeitura de São Paulo</button>
      <button type="button" class="chip" data-cnpj="42498733000148">Prefeitura do Rio de Janeiro</button>
    </div>
    <details>
      <summary>Órgão federal? Informe a chave do Portal da Transparência (opcional)</summary>
      <div class="row"><input id="chave" class="chave" placeholder="cole aqui a chave de acesso"></div>
      <p class="hint">A chave é gratuita: cadastre-se em <a href="https://portaldatransparencia.gov.br/api-de-dados/cadastrar-email" target="_blank" rel="noopener">portaldatransparencia.gov.br/api-de-dados</a> com sua conta gov.br. Ela fica salva só neste navegador.</p>
    </details>
  </form>

  <main id="out" aria-live="polite"></main>

  <section class="card metodo">
    <h2>Como a avaliação é feita</h2>
    <p><b>Restos a pagar processados</b> são despesas em que o fornecedor já entregou e o órgão já aprovou (liquidou), mas o pagamento ficou para o ano seguinte. É a dívida mais direta com quem vende para o governo.</p>
    <p>Para cada ano, comparamos quanto havia dessa dívida no início do ano com quanto foi <b>pago</b> e quanto foi <b>cancelado</b> (Declaração de Contas Anuais, anexo I-G, no SICONFI). Média dos últimos 3 anos: <b>85% ou mais pago</b> e menos de 5% cancelado é bom pagador; <b>65% a 85%</b> é regular; abaixo disso é alto risco.</p>
    <p>A <b>CAPAG</b> é a nota oficial do Tesouro Nacional sobre a capacidade de pagamento de estados e municípios (A é a melhor, D a pior). Uma nota C ou D rebaixa a avaliação.</p>
    <p>Secretarias, autarquias e câmaras usam as contas consolidadas do município ou estado a que pertencem. Para órgãos federais, com a chave do Portal da Transparência, comparamos o valor pago com o liquidado em cada ano.</p>
    <p>A avaliação é uma leitura dos dados públicos e não é uma certificação oficial. Atrasos de um órgão específico podem ser diferentes da média do ente.</p>
  </section>
  <footer>Dados: Receita Federal (via BrasilAPI), SICONFI e Tesouro Transparente (Tesouro Nacional), Portal da Transparência (CGU).</footer>
</div>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const brl = v => v == null ? "—" : new Intl.NumberFormat("pt-BR",{style:"currency",currency:"BRL",notation:"compact",maximumFractionDigits:1}).format(v);
const pct = v => v == null ? "—" : v.toLocaleString("pt-BR",{maximumFractionDigits:1}) + "%";
const nivelDe = (p, c) => p == null ? "" : (p >= 85 && (c||0) < 5) ? "bom" : (p >= 65 && (c||0) < 15) ? "regular" : "ruim";
const mascara = v => { const d = v.replace(/\D/g,"").slice(0,14);
  return d.replace(/^(\d{2})(\d)/,"$1.$2").replace(/^(\d{2})\.(\d{3})(\d)/,"$1.$2.$3").replace(/\.(\d{3})(\d)/,".$1/$2").replace(/(\d{4})(\d)/,"$1-$2"); };
const fmtCnpj = c => mascara(c);

try { const k = localStorage.getItem("chave-portal"); if (k) $("#chave").value = k; } catch(e) {}
$("#cnpj").addEventListener("input", e => e.target.value = mascara(e.target.value));
document.querySelectorAll(".chip").forEach(b => b.addEventListener("click", () => { $("#cnpj").value = mascara(b.dataset.cnpj); $("#f").requestSubmit(); }));

$("#f").addEventListener("submit", async ev => {
  ev.preventDefault();
  const cnpj = $("#cnpj").value.replace(/\D/g,"");
  if (cnpj.length !== 14) { $("#out").innerHTML = `<div class="erro">O CNPJ precisa ter 14 números.</div>`; return; }
  const chave = $("#chave").value.trim();
  try { chave ? localStorage.setItem("chave-portal", chave) : localStorage.removeItem("chave-portal"); } catch(e) {}
  $("#btn").disabled = true;
  $("#out").innerHTML = `<div class="card carregando"><div class="spin"></div>Consultando Receita, Tesouro Nacional e CAPAG. A primeira consulta pode levar até 1 minuto.</div>`;
  try {
    const r = await fetch(`/api/consulta?cnpj=${cnpj}&chave=${encodeURIComponent(chave)}`);
    const d = await r.json();
    if (!r.ok) throw new Error(d.erro || "Falha na consulta.");
    render(d);
  } catch (e) {
    $("#out").innerHTML = `<div class="erro"><b>Não deu para consultar.</b> ${esc(e.message)}</div>`;
  } finally { $("#btn").disabled = false; }
});

function grafico(hist) {
  const anos = hist.filter(h => h.pct_pago != null);
  if (!anos.length) return "";
  const W = 640, H = 220, L = 40, R = 10, T = 14, B = 28, ih = H - T - B, iw = W - L - R;
  const bw = Math.min(56, iw / anos.length * 0.6), step = iw / anos.length;
  const y = v => T + ih - Math.min(v, 100) / 100 * ih;
  let s = `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Percentual pago por ano">`;
  for (const t of [0, 25, 50, 75, 100]) s += `<line x1="${L}" x2="${W-R}" y1="${y(t)}" y2="${y(t)}" stroke="var(--line)"/><text x="${L-6}" y="${y(t)+4}" text-anchor="end">${t}%</text>`;
  for (const [t, c] of [[85,"var(--good)"],[65,"var(--warn)"]]) s += `<line x1="${L}" x2="${W-R}" y1="${y(t)}" y2="${y(t)}" stroke="${c}" stroke-dasharray="4 4"/>`;
  anos.forEach((h, i) => {
    const x = L + step * i + (step - bw) / 2, n = nivelDe(h.pct_pago, h.pct_cancelado);
    const cor = n === "bom" ? "var(--good)" : n === "regular" ? "var(--warn)" : "var(--bad)";
    const hc = Math.min(h.pct_cancelado, 100 - Math.min(h.pct_pago,100)) / 100 * ih;
    s += `<rect x="${x}" y="${y(h.pct_pago)}" width="${bw}" height="${T+ih-y(h.pct_pago)}" fill="${cor}" rx="2"><title>${h.ano}: ${pct(h.pct_pago)} pago</title></rect>`;
    if (hc > 0.5) s += `<rect x="${x}" y="${y(h.pct_pago)-hc}" width="${bw}" height="${hc}" fill="var(--np)"><title>${h.ano}: ${pct(h.pct_cancelado)} cancelado</title></rect>`;
    s += `<text x="${x+bw/2}" y="${y(h.pct_pago)-hc-5}" text-anchor="middle" style="fill:var(--ink)">${Math.round(h.pct_pago)}%</text>`;
    s += `<text x="${x+bw/2}" y="${H-8}" text-anchor="middle">${h.ano}</text>`;
  });
  return s + `</svg><div class="legend"><span><i style="background:var(--good)"></i>Pago no ano</span><span><i style="background:var(--np)"></i>Cancelado</span><span>Linhas tracejadas: 85% (bom) e 65% (mínimo regular)</span></div>`;
}

function render(d) {
  const o = d.orgao, a = d.avaliacao, h = d.historico || [];
  let html = "";
  for (const av of d.avisos || []) html += `<div class="aviso">${esc(av)}</div>`;
  html += `<section class="card orgao"><div class="eyebrow">${esc(d.esfera === "publico" ? "órgão público" : "esfera " + d.esfera)}</div>
    <h2>${esc(o.razao_social)}</h2>${o.nome_fantasia ? `<div style="color:var(--muted)">${esc(o.nome_fantasia)}</div>` : ""}
    <div class="meta"><div><span>CNPJ</span>${fmtCnpj(o.cnpj)}</div><div><span>Natureza jurídica</span>${esc(o.natureza_juridica)}</div>
    <div><span>Local</span>${esc(o.municipio)} / ${esc(o.uf)}</div><div><span>Situação na Receita</span>${esc(o.situacao)}</div>
    ${d.ente ? `<div><span>Contas analisadas</span>${esc(d.ente.nome)}${d.ente.uf && d.ente.uf !== "BR" && d.ente.cod_ibge > 99 ? " / " + esc(d.ente.uf) : ""}<br><small style="color:var(--muted)">${esc(d.ente.como)}</small></div>` : ""}</div></section>`;

  if (a) {
    const cap = d.capag && d.capag.nota;
    html += `<section class="card parecer ${a.nivel}"><div class="selo">Parecer</div><h2>${esc(a.titulo)}</h2>
      <ul>${a.motivos.map(m => `<li>${esc(m)}</li>`).join("")}</ul>
      ${a.pct_pago != null ? `<div class="stats">
        <div class="stat"><b>${pct(a.pct_pago)}</b><span>${d.federal && !d.federal.erro && a.pct_cancelado == null ? "do liquidado foi pago no ano" : "das dívidas com fornecedores pagas"} (${esc(a.periodo)})</span></div>
        ${a.pct_cancelado != null ? `<div class="stat"><b>${pct(a.pct_cancelado)}</b><span>canceladas sem pagar</span></div>` : ""}
        ${d.capag ? `<div class="stat"><b>${esc(cap || "—")}</b><span>nota CAPAG${d.capag.arquivo ? " · " + esc(d.capag.arquivo) : ""}</span></div>` : ""}
      </div>` : ""}</section>`;
  }

  if (d.federal) {
    const f = d.federal;
    if (f.erro) html += `<div class="aviso">${esc(f.erro)}</div>`;
    else {
      html += `<section class="card"><h2>Pagamentos do órgão federal</h2><p class="hint" style="margin:0 0 10px">Órgão SIAFI ${esc(f.orgao_siafi.codigo)} · ${esc(f.orgao_siafi.descricao)}</p>
      <div class="scroll"><table><tr><th>Ano</th><th>Empenhado</th><th>Liquidado</th><th>Pago</th><th>% pago</th></tr>
      ${f.historico.map(x => `<tr><td>${x.ano}${x.parcial ? " (parcial)" : ""}</td><td>${brl(x.empenhado)}</td><td>${brl(x.liquidado)}</td><td>${brl(x.pago)}</td><td>${pct(x.pct_pago)}</td></tr>`).join("")}
      </table></div></section>`;
    }
  }

  if (h.length) {
    const temDados = h.some(x => x.pct_pago != null);
    html += `<section class="card"><h2>Histórico de pagamento das dívidas com fornecedores</h2>
      ${temDados ? grafico(h) : `<p class="hint">Sem declarações de contas anuais disponíveis para estes anos.</p>`}
      <div class="scroll" style="margin-top:14px"><table>
      <tr><th>Ano</th><th>Dívida no início do ano</th><th>Pago</th><th>Cancelado</th><th>Ficou para depois</th><th>% pago</th><th>Empenhos antigos ainda não liquidados</th></tr>
      ${h.slice().reverse().map(x => x.erro ? `<tr><td>${x.ano}</td><td colspan="6" style="text-align:left;color:var(--muted)">não respondeu (${esc(x.erro)})</td></tr>`
        : x.pct_pago == null ? `<tr><td>${x.ano}</td><td colspan="6" style="text-align:left;color:var(--muted)">declaração não entregue ou sem restos a pagar</td></tr>`
        : `<tr><td>${x.ano}</td><td>${brl(x.inscritos)}</td><td>${brl(x.pagos)}</td><td>${brl(x.cancelados)}</td><td>${brl(x.saldo)}</td>
           <td><span class="pill ${nivelDe(x.pct_pago, x.pct_cancelado)}">${pct(x.pct_pago)}</span></td><td>${x.np ? brl(x.np.inscritos) : "—"}</td></tr>`).join("")}
      </table></div>
      <p class="hint">Fonte: SICONFI, Declaração de Contas Anuais, anexos I-G (processados) e I-F (não processados)${d.ente ? ", ente " + esc(d.ente.nome) : ""}.</p></section>`;
  }

  if (d.capag && (d.capag.detalhes && Object.keys(d.capag.detalhes).length || d.capag.obs)) {
    html += `<section class="card"><h2>CAPAG — capacidade de pagamento</h2>${d.capag.obs ? `<p class="hint">${esc(d.capag.obs)}</p>` : ""}
      <div class="meta">${Object.entries(d.capag.detalhes || {}).map(([k,v]) => `<div><span>${esc(k)}</span>${esc(v)}</div>`).join("")}</div>
      ${d.capag.url ? `<p class="hint">Planilha: <a href="${esc(d.capag.url)}" target="_blank" rel="noopener">${esc(d.capag.arquivo || "Tesouro Transparente")}</a></p>` : ""}</section>`;
  }

  for (const e of d.entregas || []) {
    if (e.erro) continue;
    html += `<section class="card"><h2>Relatórios fiscais entregues em ${e.ano}</h2>
      ${e.total ? `<p class="hint" style="margin:0 0 10px">${e.total} entregas ao Tesouro. Quem atrasa os relatórios pode ficar impedido de receber transferências.</p>
      <div class="scroll"><table><tr><th>Relatório</th><th>Período</th><th>Situação</th><th>Data</th></tr>
      ${e.itens.slice(0,12).map(i => `<tr><td>${esc(i.relatorio)}</td><td>${esc(i.periodo)}</td><td>${esc(i.status)}</td><td>${esc(i.data)}</td></tr>`).join("")}</table></div>`
      : `<p class="hint">Nenhum relatório entregue no SICONFI em ${e.ano}.</p>`}</section>`;
  }
  html += `<footer>Consultado em ${esc(d.consultado_em)}.</footer>`;
  $("#out").innerHTML = html;
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
