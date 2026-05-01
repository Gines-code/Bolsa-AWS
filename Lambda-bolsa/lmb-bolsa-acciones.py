"""
Lambda: Análisis diario de acciones personalizadas → S3
Ejecutado por EventBridge cada día al cierre del mercado US (~21:00 UTC / ~22h Madrid)
"""

import json
import os
import io
import logging
import requests
from datetime import datetime, timezone

import boto3
import pandas as pd

# ─── Logging ────────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ─── Configuración (ajusta estas variables en Lambda → Configuration → Environment variables) ───
S3_BUCKET       = os.environ.get("S3_BUCKET", "bolsa-aws-gic")
S3_PREFIX       = os.environ.get("S3_PREFIX", "rankings/")
PE_MAXIMO       = float(os.environ.get("PE_MAXIMO", "1000000"))
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "d7q9aqhr01qosaar3fm0d7q9aqhr01qosaar3fmg")  # Obtén tu clave gratuita en https://finnhub.io

if not FINNHUB_API_KEY:
    logger.warning("⚠️  FINNHUB_API_KEY no configurada. Ve a https://finnhub.io para obtener una clave gratuita.")

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

TICKERS_PERSONALIZADOS = [
    # --Mis empresas-- #
    "MSFT", "GOOGL",  "NVDA", "SIS.TO", "V",

    # --Empresas interesantes-- #
    "AMZN", "TEF", "NEM",

    # --Sector Salud-- #
    "JNJ", "UNH", "PFE", "ABBV",

    # --Sector Energia-- #
    "XOM", "CVX", "SHEL", 
    #PETROLERAS# 
    "COP", "EOG",
    #TRANSPORTE#
    "ENB",
    #RENOVABLES
    "IBE.MC", "NEE"
]

# ─── Obtener datos de Finnhub API ────────────────────────────────────────────
def obtener_datos_finnhub(ticker: str) -> dict | None:
    """
    Obtiene datos de un ticker desde Finnhub API.
    Combina Quote (precio) y Company Profile (fundamentales).
    """
    try:
        headers = {}
        
        # 1. Obtener datos de empresa (sector, nombre)
        profile_url = "https://finnhub.io/api/v1/stock/profile2"
        profile_params = {"symbol": ticker, "token": FINNHUB_API_KEY}
        profile_resp = requests.get(profile_url, params=profile_params, timeout=5)
        profile_resp.raise_for_status()
        profile_data = profile_resp.json()
        
        if not profile_data or "name" not in profile_data:
            logger.warning(f"{ticker}: No profile data")
            return None
        
        nombre = profile_data.get("name")
        sector = profile_data.get("finnhubIndustry", "Desconocido")
        market_cap = profile_data.get("marketCapitalization", 0)
        if market_cap:
            market_cap = int(market_cap * 1_000_000)  # Convierte a valor absoluto
        
        # 2. Obtener cotización actual (precio)
        quote_url = "https://finnhub.io/api/v1/quote"
        quote_params = {"symbol": ticker, "token": FINNHUB_API_KEY}
        quote_resp = requests.get(quote_url, params=quote_params, timeout=5)
        quote_resp.raise_for_status()
        quote_data = quote_resp.json()
        
        precio = quote_data.get("c")  # current price
        if not precio or precio <= 0:
            return None
        
        # 3. Obtener fundamentales (ratios financieros)
        metrics_url = "https://finnhub.io/api/v1/stock/metric"
        metrics_params = {"symbol": ticker, "metric": "all", "token": FINNHUB_API_KEY}
        metrics_resp = requests.get(metrics_url, params=metrics_params, timeout=5)
        metrics_resp.raise_for_status()
        metrics_data = metrics_resp.json()
        
        metric = metrics_data.get("metric", {})
        
        pe = metric.get("peTTM")
        forward_pe = metric.get("forwardPE")
        pb_ratio = metric.get("pb") or metric.get("pbAnnual") or metric.get("pbQuarterly")
        eps_growth = metric.get("epsGrowth5Y")
        eps = metric.get("epsTTM")
        roe_5y = metric.get("roe5Y")
        roe = metric.get("roeTTM")
        margen_neto = metric.get("netProfitMarginTTM")
        dividend_yield = metric.get("currentDividendYieldTTM", 0) or 0.0
        beta = metric.get("beta")
        peg_ratio = metric.get("pegTTM")
        dividend_growth = metric.get("dividendGrowthRate5Y")
        revenue_growth = metric.get("revenueGrowth5Y")
        debt_equity = metric.get("totalDebt/totalEquityAnnual") or metric.get("totalDebt/totalEquityQuarterly")
        
        
        # Filtro: necesitamos P/E válido
        if not pe or pe >= PE_MAXIMO or pe <= 0:
            return None
        
        # 4. Obtener análisis de recomendaciones y precio objetivo
        rec_url = "https://finnhub.io/api/v1/stock/recommendation"
        rec_params = {"symbol": ticker, "token": FINNHUB_API_KEY}
        rec_resp = requests.get(rec_url, params=rec_params, timeout=5)
        rec_data = rec_resp.json() if rec_resp.status_code == 200 else []
        
        recomendacion = None
        if rec_data and len(rec_data) > 0:
            # Finnhub devuelve [{'buy': X, 'hold': Y, 'sell': Z, ...}]
            # Convertimos a escala 1-5 (1=buy, 5=sell) como Yahoo Finance
            rec_dict = rec_data[0]
            buy = rec_dict.get("buy", 0)
            hold = rec_dict.get("hold", 0)
            sell = rec_dict.get("sell", 0)
            total = buy + hold + sell
            if total > 0:
                # Media ponderada: 1*buy + 3*hold + 5*sell / total
                recomendacion = (1 * buy + 3 * hold + 5 * sell) / total
        
        target_price_url = "https://finnhub.io/api/v1/stock/price-target"
        target_params = {"symbol": ticker, "token": FINNHUB_API_KEY}
        target_resp = requests.get(target_price_url, params=target_params, timeout=5)
        target_data = target_resp.json() if target_resp.status_code == 200 else {}
        
        target_price = target_data.get("targetMean")
        upside = ((target_price - precio) / precio * 100) if target_price else None
        
        
        # Deuda/Equity
        debt_equity = metric.get("debtToEquityTTM")
        
        return {
            "Ticker":           ticker,
            "Empresa":          nombre,
            "Sector":           sector,
            "Precio":           round(precio, 2),
            "P/E Trailing":     round(pe, 2),
            "P/E Forward":      round(forward_pe, 2) if forward_pe and forward_pe > 0 else None,
            "PEG Ratio":        peg_ratio,
            "P/B Ratio":        round(pb_ratio, 2) if pb_ratio and pb_ratio > 0 else None,
            "EPS Growth (%)":   eps_growth,
            "EPS":              round(eps, 2) if eps else None,
            "ROE 5Y (%)":       round(roe_5y * 100, 2) if roe_5y else None,
            "ROE (%)":          round(roe * 100, 2) if roe else None,
            "Margen Neto (%)":  round(margen_neto * 100, 2) if margen_neto else None,
            "Crec. Ingresos (%)": revenue_growth,
            "Deuda/Equity":     round(debt_equity, 2) if debt_equity and debt_equity > 0 else None,
            "Beta":             round(beta, 2) if beta and beta > 0 else None,
            "Dividend Growth (%)": dividend_growth,
            "Dividend Yield (%)": round(dividend_yield * 100, 2),
            "MarketCap":        market_cap,
            "Precio Objetivo":  round(target_price, 2) if target_price else None,
            "Upside (%)":       round(upside, 2) if upside else None,
            "Recomendación":    round(recomendacion, 2) if recomendacion else None,
        }

    except requests.exceptions.RequestException as e:
        logger.warning(f"Error en solicitud para {ticker}: {e}")
        return None
    except Exception as e:
        logger.warning(f"Error al procesar {ticker}: {e}")
        return None


# Función anterior (mantenida para compatibilidad pero no se usa)
def extraer_info(ticker: str) -> dict | None:
    """Descarga y extrae todas las métricas de un ticker. Devuelve None si falla."""
    return obtener_datos_finnhub(ticker)


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
        if col in df.columns:
            df[norm_col] = _minmax(df[col].fillna(0))
        else:
            df[norm_col] = 0.0

    # ─── ValorScore (mayor = más atractiva) ─────────────────────────────────
    # Ajustado para Finnhub: algunos campos pueden no estar disponibles
    df["ValorScore"] = (
        0.30 * (1 - df["PE_relativo"])  +  # P/E bajo respecto al sector
        0.20 * df.get("roe_norm", 0)    +  # Alta rentabilidad
        0.20 * df.get("upside_norm", 0) +  # Potencial según analistas
        0.15 * df.get("mn_norm", 0)     +  # Buen margen neto
        0.10 * df.get("dy_norm", 0)     +  # Dividendo
        0.05 * df.get("mc_norm", 0)        # Tamaño empresa
    ).round(4)

    return df.sort_values("ValorScore", ascending=False)


def subir_a_s3(df: pd.DataFrame, bucket: str, prefix: str) -> str:
    """Guarda el DataFrame como CSV en S3 y devuelve la clave (path)."""
    s3 = boto3.client("s3")
    fecha = datetime.now(timezone.utc).strftime("%Y%m%d")
    for unique_ticker in df['Ticker'].unique():
        df_ticker = df[df['Ticker'] == unique_ticker]
        key = f"{prefix}{unique_ticker}/ranking_{fecha}.csv"

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
