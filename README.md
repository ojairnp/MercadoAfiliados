# MercadoAfiliados

MVP seguro para actualizar recomendaciones fitness de **Vero & Beth** con información pública de Mercado Libre México, conservando por separado cada enlace de afiliado.

## Qué resuelve esta primera versión

1. Agregas el enlace afiliado y la categoría en `config/products.json`; el `id` es opcional.
2. GitHub Actions sigue las redirecciones oficiales y obtiene automáticamente el ID `MLM...`.
3. Obtiene un access token temporal mediante OAuth.
4. Consulta el artículo con `/items/bulk` y su precio actual con `/sale_price`.
5. Genera `public/data/products.json` sin credenciales y hace commit solo si cambió.

No modifica todavía `verobethfit.netlify.app` y no genera análisis con IA.

## Configurar productos

```json
[
  {
    "affiliate_url": "https://meli.la/TU_ENLACE",
    "category": "basculas",
    "enabled": true
  }
]
```

Reglas:

- `id` es opcional. Si ya existe, debe usar el formato `MLM` + dígitos y no se consulta la red para resolverlo.
- Sin `id`, usa una liga corta `https://meli.la/...` o `https://mercado.li/...`; el workflow obtiene el ID automáticamente.
- `affiliate_url` debe ser el enlace HTTPS generado en tus herramientas de afiliado. Nunca se reemplaza con el permalink normal.
- `category` es un slug en minúsculas, por ejemplo `basculas` o `accesorios-gym`.
- Los productos desactivados no aparecen en la salida.
- Conforme a las reglas mexicanas consultadas el 2026-09-03, suplementos alimenticios no son recomendables mediante el programa; el validador bloquea proteína, creatina, electrolitos y suplementos.

## Configurar OAuth y GitHub

Sigue [docs/OAUTH_SETUP.md](docs/OAUTH_SETUP.md). Los únicos pasos manuales son:

1. autorizar una vez la aplicación con PKCE;
2. copiar el refresh token inicial a GitHub Secrets;
3. crear un token de GitHub limitado a este repositorio con `Secrets: write` para guardar las rotaciones;
4. agregar tus productos y ejecutar el workflow manualmente la primera vez.

El cron queda programado cada 12 horas, en el minuto 17 UTC, sin concurrencia simultánea.

## Probar sin credenciales

Requiere Python 3.12 o superior:

```bash
python -m unittest discover -s tests -v
python -m json.tool config/products.json
python -m json.tool public/data/products.json
```

Las pruebas no llaman a Mercado Libre y usan respuestas simuladas.

## Salida pública

Cada producto incluye solamente:

- ID, título, precio y moneda;
- URL técnica de imagen entregada por la API y permalink;
- enlace afiliado configurado;
- categoría, disponibilidad y estado;
- condición e identificadores públicos del vendedor/tienda oficial;
- fecha del último cambio observado.

No incluye access tokens, refresh tokens, secretos ni credenciales.

## Seguridad y comportamiento ante fallos

- Biblioteca estándar de Python; sin dependencias de terceros para el MVP.
- Timeout, rate limiting conservador, reintentos y backoff con jitter para 429/5xx.
- Errores explícitos para 401, 403 y 404.
- Validación estricta de JSON, IDs, categorías, moneda, sitio y URLs.
- El resolvedor sigue redirecciones manualmente y solo permite HTTPS, puerto 443 y dominios oficiales de Mercado Libre; bloquea hosts parecidos, credenciales embebidas, ciclos y respuestas excesivas.
- Escritura atómica: una sincronización parcial nunca sobrescribe el último JSON correcto.
- Logs sin cuerpos OAuth ni valores secretos.
- Permisos mínimos del workflow: `contents: write` únicamente para el `GITHUB_TOKEN` temporal.

Consulta las decisiones y fuentes oficiales en [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Aviso de transparencia

La futura página debe identificar claramente los enlaces afiliados. Los precios y la disponibilidad pueden cambiar en Mercado Libre; `updated_at` indica cuándo cambió el dato observado. La URL de imagen se conserva como referencia técnica y no se copia al repositorio, pero **no debe mostrarse automáticamente en Vero & Beth hasta confirmar los derechos o la autorización aplicable**.
