# Configuración segura de OAuth + PKCE

No compartas aquí el Client Secret, authorization code, access token ni refresh token. Los pasos que manejan credenciales se ejecutan en tu terminal local.

## 1. Generar la autorización

En PowerShell, desde la raíz del repositorio:

```powershell
$env:MELI_CLIENT_ID = "TU_APP_ID"
$env:MELI_REDIRECT_URI = "https://verobethfit.netlify.app/"
python scripts/meli_oauth.py start
```

El comando crea `.local/meli_oauth_session.json` con el `code_verifier` y `state` temporales. El archivo está ignorado por Git. Abre la URL mostrada, inicia sesión con la cuenta principal de Mercado Libre y autoriza la aplicación.

Mercado Libre redirigirá a una URL similar a:

```text
https://verobethfit.netlify.app/?code=...&state=...
```

La página puede mostrar un 404 y aun así la barra de direcciones contendrá los parámetros necesarios.

## 2. Intercambiar el código

```powershell
$env:MELI_CLIENT_SECRET = "TU_CLIENT_SECRET"
python scripts/meli_oauth.py exchange
```

El programa pedirá `code` y `state` sin guardarlos en el historial de comandos. Confirma que el estado coincide y, bajo confirmación explícita, muestra el refresh token una sola vez. No conserva el access token.

## 3. Configurar GitHub Actions

En `Settings → Secrets and variables → Actions`:

Variables:

- `MELI_CLIENT_ID`
- `MELI_REDIRECT_URI`

Secrets:

- `MELI_CLIENT_SECRET`
- `MELI_REFRESH_TOKEN`
- `MELI_SECRET_ROTATION_TOKEN`

`MELI_SECRET_ROTATION_TOKEN` debe ser un **fine-grained personal access token** limitado solamente a `ojairnp/MercadoAfiliados`, con `Secrets: Read and write`. No necesita permiso `Contents`; el workflow utiliza su `GITHUB_TOKEN` temporal para el commit.

Este tercer secreto es necesario porque Mercado Libre invalida cada refresh token después de usarlo y devuelve uno nuevo. El `GITHUB_TOKEN` normal del workflow no tiene permiso para modificar secretos del repositorio.

## 4. Probar manualmente

Agrega un producto real a `config/products.json`, ejecuta el workflow `Sincronizar productos de Mercado Libre` con `Run workflow` y verifica `public/data/products.json`.

Si falta cualquier configuración, el workflow termina antes de consumir el refresh token y muestra solo el nombre faltante, nunca su valor.
