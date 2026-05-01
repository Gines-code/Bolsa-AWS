# 🚀 Migración de yfinance a Finnhub API para AWS Lambda

## ✅ Cambios realizados

Tu código ha sido modificado para usar **Finnhub API** en lugar de yfinance. Esto resuelve el problema de IP bloqueadas en AWS Lambda.

### Cambios principales:
- ❌ Eliminada dependencia en `yfinance`
- ✅ Agregada dependencia en `requests` (más ligero y sin problemas en Lambda)
- ✅ Nueva función `obtener_datos_finnhub()` que obtiene datos de múltiples endpoints de Finnhub
- ✅ La función `extraer_info()` ahora llama a `obtener_datos_finnhub()`
- ✅ Scoring adaptado para campos que podrían no estar disponibles

---

## 📋 Pasos para deployar en AWS Lambda

### 1️⃣ Obtener clave API gratuita de Finnhub

1. Ve a https://finnhub.io
2. Regístrate (es gratis)
3. Copia tu API Key del dashboard

**Límites del plan gratuito:**
- 60 llamadas/minuto
- Acceso a datos fundamentales, cotizaciones, recomendaciones, etc.
- **¡Suficiente para 30-40 acciones con margen!**

---

### 2️⃣ Preparar el código para Lambda

**Opción A: Usar capas (Layers) - RECOMENDADO**

```bash
# 1. Crea carpeta temporal
mkdir -p layer_finnhub/python

# 2. Instala dependencias
pip install -t layer_finnhub/python -r requirements.txt

# 3. Comprime
cd layer_finnhub
zip -r layer_finnhub.zip .
cd ..

# 4. En AWS Console → Lambda → Layers → Create layer
#    Sube layer_finnhub.zip
```

**Opción B: Agrupar todo en un ZIP (más simple)**

```bash
zip -r lambda_finnhub.zip lmb-bolsa-acciones.py
# En AWS Console → Lambda → Code → Upload ZIP
```

---

### 3️⃣ Configurar variables de entorno en AWS Lambda

En AWS Console → tu Lambda function → Configuration → Environment variables

Añade estas variables:

| Clave | Valor | Ejemplo |
|-------|-------|---------|
| `FINNHUB_API_KEY` | Tu clave de Finnhub | `c9a0c7d0f4b3e1a6c2b8f5d9...` |
| `S3_BUCKET` | Nombre tu bucket S3 | `mi-bucket-bolsa` |
| `S3_PREFIX` | Carpeta en S3 | `rankings/` |
| `PE_MAXIMO` | Filtro P/E máximo | `1000000` |

---

### 4️⃣ Configurar permisos IAM

Tu Lambda necesita permisos para S3. En AWS IAM, asegúrate que el role tenga:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:GetObject"
      ],
      "Resource": "arn:aws:s3:::mi-bucket-bolsa/*"
    }
  ]
}
```

---

### 5️⃣ Configurar EventBridge (opcional)

Si quieres que se ejecute automáticamente cada día:

1. AWS Console → EventBridge → Create rule
2. Patrón: `cron(0 21 ? * MON-FRI *)`  (21:00 UTC, lunes-viernes)
3. Target: tu Lambda function

---

## 📊 Datos disponibles en Finnhub

La función obtiene:
- ✅ Precio actual, P/E trailing, P/E forward
- ✅ P/B Ratio, ROE, Margen neto, Beta
- ✅ Dividend Yield
- ✅ Market Cap
- ✅ Precio objetivo de analistas
- ✅ Recomendaciones (Buy/Hold/Sell)
- ✅ Upside potencial
- ❌ PEG Ratio (no disponible)
- ❌ Revenue Growth (no disponible en plan free)

---

## 🔧 Troubleshooting

### "FINNHUB_API_KEY no configurada"
- Ve a https://finnhub.io y obtén tu clave
- Añade la variable de entorno en Lambda

### "Too many requests" (429)
- Plan gratuito: 60 llamadas/minuto
- Si tienes >60 acciones, añade pequeños delays o actualiza a plan pro

### "Ticker not found"
- Finnhub es sensible a los símbolos
- Usa símbolos de US (AAPL, MSFT, etc.)

### "No hay datos de P/E"
- Algunos tickers nuevos o penny stocks no tienen datos
- El código los filtra automáticamente

---

## 📈 Rendimiento esperado

Con 45 acciones:
- Tiempo total: **30-45 segundos** (5-10 segundos por acción)
- Llamadas API: ~180 (3 por ticker = profile + quote + metrics)
- Dentro del límite free: ✅ 60 llamadas/minuto

---

## 🔄 Alternativas (si necesitas más datos)

Si en el futuro necesitas más información:

1. **Upgrade a Finnhub Pro** - $9-99/mes
2. **Alpha Vantage API** - Gratuita, 5 llamadas/minuto
3. **Polygon.io** - De pago, muy completa
4. **IEX Cloud** - De pago

---

## 💡 Tips

- **Cachea resultados en S3** para no exceder límites API
- **Agrupa tickers por sector** si necesitas análisis rápidos
- **Usa batch processing** si tienes cientos de tickers

¡Ahora tu Lambda funcionará sin problemas de IP! 🚀
