"""Camada de dados: lê o Neon (PostgreSQL) ou, na falta de credenciais, os CSVs locais."""
from functools import lru_cache
from pathlib import Path
import re

import pandas as pd

MARKETING = {
    "INSTAGRAM": "Instagram", "FACEBOOK": "Facebook", "SITE": "Site",
    "GOOGLE": "Google", "GOOGLE ADS": "Google", "INDICAÇÃO": "Indicação",
    "FAIXADA": "Faixada", "ABC DAS MÃES": "ABC das Mães",
    "CONVIDADO": "Convidado", "LINKEDIN": "LinkedIn", "TV": "TV",
}
TABELAS = {"cardapio": "cardapios", "tema": "temas", "vendedor": "vendedores",
           "prospect": "prospects_mapa_sp", "evento": "eventos"}


@lru_cache(maxsize=2)
def _engine(url: str):
    from sqlalchemy import create_engine
    url = url.split("?")[0]  # pg8000 não aceita ?sslmode=... na URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+pg8000://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+pg8000://", 1)
    # Neon exige SSL; pool_recycle evita usar conexões que o Neon já encerrou por inatividade
    return create_engine(url, connect_args={"ssl_context": True}, pool_pre_ping=True, pool_recycle=240)


# Impressão digital dos dados: muda sempre que QUALQUER linha das 5 tabelas é inserida, alterada ou apagada.
_SQL_ASSINATURA = """
SELECT md5(
  COALESCE((SELECT string_agg(CAST(t AS text), '|' ORDER BY t.id_evento)   FROM public.evento   t), '') ||
  COALESCE((SELECT string_agg(CAST(t AS text), '|' ORDER BY t.id_prospect) FROM public.prospect t), '') ||
  COALESCE((SELECT string_agg(CAST(t AS text), '|' ORDER BY t.id_vendedor) FROM public.vendedor t), '') ||
  COALESCE((SELECT string_agg(CAST(t AS text), '|' ORDER BY t.id_cardapio) FROM public.cardapio t), '') ||
  COALESCE((SELECT string_agg(CAST(t AS text), '|' ORDER BY t.id_tema)     FROM public.tema     t), '')
)
"""


def assinatura(url: str) -> str:
    """Consulta leve para saber se o banco mudou desde a última leitura."""
    from sqlalchemy import text
    with _engine(url).connect() as conn:
        return str(conn.execute(text(_SQL_ASSINATURA)).scalar())


def _ler_sql(url: str) -> dict:
    eng = _engine(url)
    with eng.connect() as conn:
        return {t: pd.read_sql(f"SELECT * FROM public.{t}", conn) for t in TABELAS}


def _ler_csv(pasta: Path) -> dict:
    out = {}
    for t, nome in TABELAS.items():
        for cand in (pasta / f"{nome}.csv", pasta / f"{nome}_3.csv"):
            if cand.exists():
                out[t] = pd.read_csv(cand, encoding="utf-8-sig")
                break
        else:
            raise FileNotFoundError(f"CSV de '{t}' não encontrado em {pasta}")
    return out


def montar(tb: dict) -> pd.DataFrame:
    """Une as 5 tabelas em um DataFrame plano, com uma linha por evento/lead."""
    ev, pr = tb["evento"].copy(), tb["prospect"].copy()
    pr = pr.rename(columns={"endereço": "endereco"})
    pr = pr[["id_prospect", "nome", "marketing", "bairro", "cidade", "regiao", "latitude", "longitude"]]
    ve = tb["vendedor"][["id_vendedor", "nome"]].rename(columns={"nome": "nome_vendedor"})
    ca = tb["cardapio"].rename(columns={"tipo": "tipo_cardapio"})
    te = tb["tema"]

    df = (ev.merge(pr, left_on="fk_prospect", right_on="id_prospect", how="left")
            .merge(ve, left_on="fk_vendedor", right_on="id_vendedor", how="left")
            .merge(ca, left_on="fk_cardapio", right_on="id_cardapio", how="left")
            .merge(te, left_on="fk_tema", right_on="id_tema", how="left"))

    df["data_interesse"] = pd.to_datetime(df["data_interesse"], errors="coerce")
    df["ano"] = df["data_interesse"].dt.year
    for c in ("valor", "custo_evento", "qtd_adultos", "qtd_criancas", "latitude", "longitude"):
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # --- padronização de textos sujos ---
    df["marketing"] = (df["marketing"].fillna("").str.strip().str.upper()
                       .map(MARKETING).fillna("Outros"))
    df["tipo"] = df["tipo"].str.strip().replace({"Aniversário adulto": "Aniversário de adulto"})
    df["nome_vendedor"] = df["nome_vendedor"].fillna("Sem vendedor")
    return df.dropna(subset=["ano"]).assign(ano=lambda d: d["ano"].astype(int))


def carregar(url: str | None, pasta_csv: str = "dados") -> tuple[pd.DataFrame, str]:
    """Retorna (dataframe, origem) onde origem é 'neon' ou 'csv'."""
    if url:
        return montar(_ler_sql(url)), "neon"
    return montar(_ler_csv(Path(pasta_csv))), "csv"


def agregar_mapa(df: pd.DataFrame) -> pd.DataFrame:
    """Uma linha por localidade (bairro + cidade) com leads, eventos fechados e receita."""
    g = df.dropna(subset=["latitude", "longitude"]).copy()
    g["fechado"] = g["status"].eq("Tornou-se cliente")
    g["rec"] = g["valor"].where(g["fechado"], 0).fillna(0)
    out = (g.groupby(["cidade", "bairro", "regiao"], dropna=False)
             .agg(lat=("latitude", "median"), lon=("longitude", "median"),
                  leads=("id_evento", "count"), fechados=("fechado", "sum"),
                  receita=("rec", "sum"))
             .reset_index())
    out["local"] = out["bairro"].astype(str) + ", " + out["cidade"].astype(str)
    return out


# ----------------------------------------------------------------------------
# Payload para o painel HTML (dashboard.html)
# ----------------------------------------------------------------------------
def _mascarar(nome) -> str:
    """LGPD: 'Carina Torres Amaral' -> 'Carina T. A.'"""
    p = str(nome).split()
    return (p[0] + "".join(f" {x[0]}." for x in p[1:])) if p else ""


def payload_json(df: pd.DataFrame) -> str:
    """Converte o DataFrame em JSON enxuto para o painel. Só sai nome (abreviado) de leads em negociação."""
    import json
    from datetime import datetime, timedelta, timezone

    d = df.copy()
    d["local"] = (d["bairro"].astype(str) + ", " + d["cidade"].astype(str)).where(
        d["latitude"].notna() & d["longitude"].notna() & d["bairro"].notna())
    out = pd.DataFrame({
        "a": d["ano"], "t": d["tipo"], "s": d["status"], "v": d["valor"], "c": d["custo_evento"],
        "ad": d["qtd_adultos"], "cr": d["qtd_criancas"], "p": d["pagamento"], "k": d["tipo_cardapio"],
        "h": d["nome_tema"], "m": d["marketing"], "vd": d["nome_vendedor"], "l": d["local"],
        "rg": d["regiao"],
        "n": d["nome"].where(d["status"].eq("Em negociação")).map(lambda x: _mascarar(x) if pd.notna(x) else None),
        "d": d["data_interesse"].dt.strftime("%Y-%m-%d"),
    }).astype(object)
    out = out.where(out.notna(), None)

    locs = {}
    for r in agregar_mapa(df).itertuples():
        locs.setdefault(r.local, {"bairro": r.bairro, "cidade": r.cidade, "regiao": r.regiao,
                                  "lat": round(float(r.lat), 6), "lon": round(float(r.lon), 6)})

    def _nativo(o):
        return o.item() if hasattr(o, "item") else str(o)

    gerado = datetime.now(timezone(timedelta(hours=-3))).strftime("%d/%m %H:%M")  # horário de Brasília
    txt = json.dumps({"rows": out.to_dict("records"), "locs": locs, "gerado": gerado},
                     ensure_ascii=False, separators=(",", ":"), default=_nativo)
    # evita fechar o <script> ou quebrar a linha do JS por acidente
    return txt.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
