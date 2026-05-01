"""
Lambda: Análisis diario de acciones personalizadas → S3
Ejecutado por EventBridge cada día al cierre del mercado US (~21:00 UTC / ~22h Madrid)
"""

import json
import os
import io
import logging
from datetime import datetime, timezone

import boto3
import pandas as pd
import yfinance as yf

# ─── Logging ────────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ─── Configuración (ajusta estas variables en Lambda → Configuration → Environment variables) ───
S3_BUCKET   = os.environ.get("S3_BUCKET", "mi-bucket-bolsa")
S3_PREFIX   = os.environ.get("S3_PREFIX", "rankings/")          # carpeta dentro del bucket
PE_MAXIMO   = float(os.environ.get("PE_MAXIMO", "1000000"))     # filtro de P/E máximo

# ─── Lista personalizada de tickers ─────────────────────────────────────────
# Puedes moverla también a una variable de entorno o a un JSON en S3
TICKERS_PERSONALIZADOS = [
    # --- Tecnología ---
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSM", "ASML",
    # --- Finanzas ---
    "JPM", "BAC", "GS", "V", "MA",
    # --- Salud ---
    "JNJ", "UNH", "PFE", "ABBV",
    # --- Energía ---
    "XOM", "CVX",
    # --- Consumo / Retail ---
    "WMT", "COST", "MCD",
    # ↑ Añade o quita los que quieras
]

# ─── Métricas adicionales que se descargan de yfinance ──────────────────────
def extraer_info(ticker: str) -> dict | None:
    """Descarga y extrae todas las métricas de un ticker. Devuelve None si falla."""
    try:
        info = yf.Ticker(ticker).info

        pe              = info.get("trailingPE")
        forward_pe      = info.get("forwardPE")           # 🆕 P/E estimado próximo año
        precio          = info.get("currentPrice")
        nombre          = info.get("shortName")
        sector          = info.get("sector", "Desconocido")
        dividend_yield  = info.get("dividendYield") or 0.0
        market_cap      = info.get("marketCap") or 0
        pb_ratio        = info.get("priceToBook")          # 🆕 Precio/Valor contable
        roe             = info.get("returnOnEquity")        # 🆕 Rentabilidad sobre fondos propios
        deuda_ebitda    = info.get("debtToEquity")          # 🆕 Deuda/Patrimonio (proxy apalancamiento)
        margen_neto     = info.get("profitMargins")         # 🆕 Margen neto
        revenue_growth  = info.get("revenueGrowth")         # 🆕 Crecimiento de ingresos YoY
        peg_ratio       = info.get("pegRatio")              # 🆕 PEG (P/E ajustado por crecimiento)
        beta            = info.get("beta")                  # 🆕 Volatilidad relativa al mercado
        target_price    = info.get("targetMeanPrice")       # 🆕 Precio objetivo analistas
        recomendacion   = info.get("recommendationMean")    # 🆕 Consenso analistas (1=buy, 5=sell)

        # Filtro básico: necesitamos P/E para el scoring
        if not pe or pe >= PE_MAXIMO:
            return None

        # Upside potencial respecto al precio objetivo de analistas
        upside = ((target_price - precio) / precio * 100) if target_price and precio else None

        return {
            "Ticker":           ticker,
            "Empresa":          nombre,
            "Sector":           sector,
            "Precio":           round(precio, 2) if precio else None,
            "P/E Trailing":     round(pe, 2),
            "P/E Forward":      round(forward_pe, 2) if forward_pe else None,
            "PEG Ratio":        round(peg_ratio, 2) if peg_ratio else None,
            "P/B Ratio":        round(pb_ratio, 2) if pb_ratio else None,
            "ROE (%)":          round(roe * 100, 2) if roe else None,
            "Margen Neto (%)":  round(margen_neto * 100, 2) if margen_neto else None,
            "Crec. Ingresos (%)": round(revenue_growth * 100, 2) if revenue_growth else None,
            "Deuda/Equity":     round(deuda_ebitda, 2) if deuda_ebitda else None,
            "Beta":             round(beta, 2) if beta else None,
            "Dividend Yield (%)": round(dividend_yield * 100, 2),
            "MarketCap":        market_cap,
            "Precio Objetivo":  round(target_price, 2) if target_price else None,
            "Upside (%)":       round(upside, 2) if upside else None,
            "Recomendación":    round(recomendacion, 2) if recomendacion else None,
        }

    except Exception as e:
        logger.warning(f"Error al procesar {ticker}: {e}")
        return None


def _minmax(series: pd.Series) -> pd.Series:
    """Normalización min-max simple [0, 1] sin dependencias externas."""
    mn, mx = series.min(), series.max()
    if mx == mn:
        return pd.Series(0.0, index=series.index)
    return (series - mn) / (mx - mn)


def calcular_scores(df: pd.DataFrame) -> pd.DataFrame:
    """Añade columnas de scoring normalizado al DataFrame."""

    # P/E relativo dentro del sector
    pe_sector = df.groupby("Sector")["P/E Trailing"].mean().rename("P/E Sector")
    df = df.join(pe_sector, on="Sector")
    df["PE_relativo"] = (df["P/E Trailing"] / df["P/E Sector"]).clip(0, 2)

    # Normalización min-max con pandas puro (sin sklearn)
    cols_norm = {
        "Dividend Yield (%)": "dy_norm",
        "MarketCap":          "mc_norm",
        "ROE (%)":            "roe_norm",
        "Margen Neto (%)":    "mn_norm",
        "Upside (%)":         "upside_norm",
    }
    for col, norm_col in cols_norm.items():
        df[norm_col] = _minmax(df[col].fillna(0)) if col in df.columns else 0.0

    # ─── ValorScore (mayor = más atractiva) ─────────────────────────────────
    df["ValorScore"] = (
        0.30 * (1 - df["PE_relativo"])  +  # P/E bajo respecto al sector
        0.20 * df["roe_norm"]           +  # Alta rentabilidad
        0.20 * df["upside_norm"]        +  # Potencial según analistas
        0.15 * df["mn_norm"]            +  # Buen margen neto
        0.10 * df["dy_norm"]            +  # Dividendo
        0.05 * df["mc_norm"]               # Tamaño empresa
    ).round(4)

    return df.sort_values("ValorScore", ascending=False)


def subir_a_s3(df: pd.DataFrame, bucket: str, prefix: str) -> str:
    """Guarda el DataFrame como CSV en S3 y devuelve la clave (path)."""
    s3 = boto3.client("s3")
    fecha = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    key = f"{prefix}ranking_{fecha}.csv"

    buffer = io.StringIO()
    df.to_csv(buffer, index=False)
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=buffer.getvalue().encode("utf-8"),
        ContentType="text/csv",
    )
    logger.info(f"Archivo subido → s3://{bucket}/{key}")
    return key


# ─── Handler principal ───────────────────────────────────────────────────────
def lambda_handler(event, context):
    logger.info(f"Inicio ejecución | tickers configurados: {len(TICKERS_PERSONALIZADOS)}")

    # 1. Descargar métricas
    resultados = []
    for ticker in TICKERS_PERSONALIZADOS:
        datos = extraer_info(ticker)
        if datos:
            resultados.append(datos)
        else:
            logger.info(f"  ⚠️  {ticker} descartado (sin P/E o error)")

    if not resultados:
        logger.error("No se obtuvo información de ningún ticker.")
        return {"statusCode": 500, "body": "Sin datos"}

    # 2. Construir DataFrame y calcular scores
    df = pd.DataFrame(resultados)
    df = calcular_scores(df)

    logger.info(f"\n🏆 Top 10:\n{df[['Ticker','Empresa','P/E Trailing','ValorScore']].head(10).to_string()}")

    # 3. Columnas de salida ordenadas
    columnas_salida = [
        "Ticker", "Empresa", "Sector", "Precio", "ValorScore",
        "P/E Trailing", "P/E Forward", "PEG Ratio", "P/B Ratio",
        "ROE (%)", "Margen Neto (%)", "Crec. Ingresos (%)",
        "Deuda/Equity", "Beta", "Dividend Yield (%)",
        "MarketCap", "Precio Objetivo", "Upside (%)", "Recomendación",
        "P/E Sector", "PE_relativo",
    ]
    columnas_salida = [c for c in columnas_salida if c in df.columns]

    # 4. Subir a S3
    key = subir_a_s3(df[columnas_salida], S3_BUCKET, S3_PREFIX)

    return {
        "statusCode": 200,
        "body": json.dumps({
            "tickers_procesados": len(df),
            "archivo_s3": f"s3://{S3_BUCKET}/{key}",
            "top3": df[["Ticker", "ValorScore"]].head(3).to_dict(orient="records"),
        })
    }
