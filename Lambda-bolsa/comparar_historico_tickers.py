
"""
Script: Comparar histórico diario de tickers desde CSVs en S3 y enviar resumen por SNS
Lee todos los archivos históricos de cada ticker desde S3 (por ejemplo, rankings/TICKER/*.csv),
los une, calcula variaciones, máximos/mínimos y sube el CSV comparativo a S3 y envía resumen por email.
"""

import os

import pandas as pd
import boto3
from datetime import datetime, timezone
import io

# Configuración
S3_BUCKET = os.environ.get("S3_BUCKET", "bolsa-aws-gic")
S3_PREFIX = os.environ.get("S3_PREFIX", "rankings/")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "d7q9aqhr01qosaar3fm0d7q9aqhr01qosaar3fmg")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "arn:aws:sns:eu-west-1:287238679534:Bolsa-reporte-diario")  # Debes configurar el ARN de tu SNS

TICKERS_PERSONALIZADOS = [
    "MSFT", "GOOGL",  "NVDA", "SIS.TO", "V",
    "AMZN", "TEF", "NEM",
    "JNJ", "UNH", "PFE", "ABBV",
    "XOM", "CVX", "SHEL", "COP", "EOG", "ENB", "IBE.MC", "NEE"
]


# Función para leer todos los CSV históricos de un ticker desde S3
def leer_csvs_ticker_s3(s3, bucket, prefix, ticker):
    import re
    dfs = []
    paginator = s3.get_paginator('list_objects_v2')
    carpeta = f"{prefix}{ticker}/"
    for page in paginator.paginate(Bucket=bucket, Prefix=carpeta):
        for obj in page.get('Contents', []):
            key = obj['Key']
            if key.endswith('.csv'):
                # Extraer fecha del nombre del archivo: ranking_YYYYMMDD.csv en cualquier ruta
                match = re.search(r'ranking_(\d{8})\.csv$', key)
                if match:
                    fecha_str = match.group(1)
                    fecha = pd.to_datetime(fecha_str, format='%Y%m%d')
                else:
                    fecha = pd.NaT
                response = s3.get_object(Bucket=bucket, Key=key)
                df = pd.read_csv(io.BytesIO(response['Body'].read()))
                df['fecha'] = fecha
                df['Ticker'] = ticker
                dfs.append(df)
    if dfs:
        return pd.concat(dfs, ignore_index=True)
    else:
        return pd.DataFrame()


# Procesar todos los tickers y juntar en un solo DataFrame desde S3
def procesar_tickers_desde_s3(tickers, bucket, prefix):
    s3 = boto3.client('s3')
    dfs = []
    for ticker in tickers:
        df = leer_csvs_ticker_s3(s3, bucket, prefix, ticker)
        if not df.empty:
            dfs.append(df)
    if dfs:
        return pd.concat(dfs, ignore_index=True)
    else:
        return pd.DataFrame()

# Calcular variaciones y máximos/mínimos

def analizar_historico(df):
    resumen_rows = []
    resumenes = []
    for ticker in df["Ticker"].unique():
        dft = df[df["Ticker"] == ticker].sort_values("fecha")
        dft = dft.reset_index(drop=True)
        # Diferencias de precio
        dft["diff_precio"] = dft["Precio"].diff()
        dft["pct_change_precio"] = dft["Precio"].pct_change() * 100
        # Diferencias de P/E
        dft["diff_pe_trailing"] = dft["P/E Trailing"].diff()
        dft["diff_pe_forward"] = dft["P/E Forward"].diff()
        # Para cada fila, buscar la primera fecha donde se alcanzó ese precio o P/E
        for i, row in dft.iterrows():
            precio_actual = row["Precio"]
            pe_trailing_actual = row["P/E Trailing"]
            pe_forward_actual = row["P/E Forward"]
            # Primera fecha donde se alcanzó ese precio (igual o superior/inferior)
            mask_precio = dft["Precio"] >= precio_actual
            fecha_primera_precio_mayor_igual = dft.loc[mask_precio, "fecha"].iloc[0] if mask_precio.any() else pd.NaT
            mask_precio = dft["Precio"] <= precio_actual
            fecha_primera_precio_menor_igual = dft.loc[mask_precio, "fecha"].iloc[0] if mask_precio.any() else pd.NaT
            # Primera fecha donde se alcanzó ese P/E Trailing
            if pd.notnull(pe_trailing_actual):
                mask_pe_trailing = dft["P/E Trailing"] >= pe_trailing_actual
                fecha_primera_pe_trailing_mayor_igual = dft.loc[mask_pe_trailing, "fecha"].iloc[0] if mask_pe_trailing.any() else pd.NaT
                mask_pe_trailing = dft["P/E Trailing"] <= pe_trailing_actual
                fecha_primera_pe_trailing_menor_igual = dft.loc[mask_pe_trailing, "fecha"].iloc[0] if mask_pe_trailing.any() else pd.NaT
            else:
                fecha_primera_pe_trailing_mayor_igual = pd.NaT
                fecha_primera_pe_trailing_menor_igual = pd.NaT
            # Primera fecha donde se alcanzó ese P/E Forward
            if pd.notnull(pe_forward_actual):
                mask_pe_forward = dft["P/E Forward"] >= pe_forward_actual
                fecha_primera_pe_forward_mayor_igual = dft.loc[mask_pe_forward, "fecha"].iloc[0] if mask_pe_forward.any() else pd.NaT
                mask_pe_forward = dft["P/E Forward"] <= pe_forward_actual
                fecha_primera_pe_forward_menor_igual = dft.loc[mask_pe_forward, "fecha"].iloc[0] if mask_pe_forward.any() else pd.NaT
            else:
                fecha_primera_pe_forward_mayor_igual = pd.NaT
                fecha_primera_pe_forward_menor_igual = pd.NaT
            resumen_rows.append({
                "Ticker": ticker,
                "Fecha": row["fecha"],
                "Precio": precio_actual,
                "Dif. Precio": row["diff_precio"],
                "% Dif. Precio": row["pct_change_precio"],
                "Primera fecha >= precio": fecha_primera_precio_mayor_igual,
                "Primera fecha <= precio": fecha_primera_precio_menor_igual,
                "P/E Trailing": pe_trailing_actual,
                "Dif. P/E Trailing": row["diff_pe_trailing"],
                "Primera fecha >= P/E Trailing": fecha_primera_pe_trailing_mayor_igual,
                "Primera fecha <= P/E Trailing": fecha_primera_pe_trailing_menor_igual,
                "P/E Forward": pe_forward_actual,
                "Dif. P/E Forward": row["diff_pe_forward"],
                "Primera fecha >= P/E Forward": fecha_primera_pe_forward_mayor_igual,
                "Primera fecha <= P/E Forward": fecha_primera_pe_forward_menor_igual,
            })
        # Construir resumen para SNS (último valor, % cambio, máximo, mínimo)
        if not dft.empty:
            ultimo = dft.iloc[-1]["Precio"]
            if len(dft) > 1:
                penultimo = dft.iloc[-2]["Precio"]
                pct_cambio = ((ultimo - penultimo) / penultimo) * 100 if penultimo != 0 else 0
            else:
                pct_cambio = 0
            maximo = dft["Precio"].max()
            fecha_max = dft.loc[dft["Precio"].idxmax(), "fecha"]
            minimo = dft["Precio"].min()
            fecha_min = dft.loc[dft["Precio"].idxmin(), "fecha"]
            resumenes.append({
                "Ticker": ticker,
                "Ultimo": round(ultimo, 2) if pd.notnull(ultimo) else None,
                "%Cambio": round(pct_cambio, 2) if pd.notnull(pct_cambio) else None,
                "Max": round(maximo, 2) if pd.notnull(maximo) else None,
                "Fecha Max": fecha_max.strftime("%Y-%m-%d") if pd.notnull(fecha_max) else None,
                "Min": round(minimo, 2) if pd.notnull(minimo) else None,
                "Fecha Min": fecha_min.strftime("%Y-%m-%d") if pd.notnull(fecha_min) else None,
            })
    resumen_df = pd.DataFrame(resumen_rows)
    resumen_final = resumen_df.sort_values("Fecha").groupby("Ticker", as_index=False).last()
    return resumen_final, resumenes

# Subir CSV a S3
def subir_csv_s3(df, bucket, prefix):
    s3 = boto3.client("s3")
    fecha = datetime.now(timezone.utc).strftime("%Y%m%d")
    key = f"{prefix}historico_{fecha}.csv"
    buffer = df.to_csv(index=False).encode("utf-8")
    s3.put_object(Bucket=bucket, Key=key, Body=buffer, ContentType="text/csv")
    return key

# Enviar resumen por SNS
def enviar_resumen_sns(df, bucket, key, topic_arn):
    if not topic_arn:
        print("SNS_TOPIC_ARN no configurado")
        return
    # Seleccionar solo las columnas deseadas para el mail
    columnas_mail = [
        'Ticker', 'Fecha', 'Precio', '% Dif. Precio', 'Primera fecha >= precio',
        'P/E Trailing', 'Primera fecha >= P/E Trailing', 'P/E Forward', 'Dif. P/E Forward'
    ]
    # Filtrar solo las columnas deseadas (rellenar con string vacío si falta alguna)
    df_fmt = df.copy()
    for col in columnas_mail:
        if col not in df_fmt.columns:
            df_fmt[col] = ""
    df_fmt = df_fmt[columnas_mail]
    # Redondear valores numéricos a 2 decimales
    for col in ['Precio', '% Dif. Precio', 'P/E Trailing', 'P/E Forward', 'Dif. P/E Forward']:
        if col in df_fmt.columns:
            df_fmt[col] = df_fmt[col].apply(lambda x: f"{x:.2f}" if pd.notnull(x) and x != '' else "")
    # Convertir fechas a string (si no lo son)
    for col in ['Fecha', 'Primera fecha >= precio', 'Primera fecha >= P/E Trailing']:
        if col in df_fmt.columns:
            df_fmt[col] = df_fmt[col].apply(lambda x: x.strftime("%Y-%m-%d") if pd.notnull(x) and hasattr(x, 'strftime') else (str(x) if pd.notnull(x) and x != '' else ""))
    # Construir tabla alineada
    mensaje = "Resumen histórico diario de tickers (último día):\n\n"
    # Cabecera
    header_fmt = "{:<8} {:<10} {:>8} {:>10} {:<22} {:>12} {:<26} {:>12} {:>14}\n"
    mensaje += header_fmt.format(*columnas_mail)
    mensaje += "-" * 130 + "\n"
    # Filas
    if not df_fmt.empty:
        for _, row in df_fmt.iterrows():
            mensaje += header_fmt.format(*[row.get(col, '') for col in columnas_mail])
    else:
        mensaje += "(Sin datos para mostrar)\n"
    mensaje += f"\nCSV completo: s3://{bucket}/{key}"
    sns = boto3.client("sns")
    sns.publish(TopicArn=topic_arn, Subject="Resumen histórico diario de tickers", Message=mensaje)


def lambda_handler(event, context):
    df = procesar_tickers_desde_s3(TICKERS_PERSONALIZADOS, S3_BUCKET, S3_PREFIX)
    if df.empty:
        print("No se pudo obtener histórico de ningún ticker desde S3.")
        exit(1)
    df, resumenes = analizar_historico(df)
    key = subir_csv_s3(df, S3_BUCKET, S3_PREFIX)
    enviar_resumen_sns(df, S3_BUCKET, key, SNS_TOPIC_ARN)
    print("Proceso completado. CSV subido y resumen enviado por SNS.")
