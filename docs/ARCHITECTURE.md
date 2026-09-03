# Arquitectura mínima

Revisión documental: **2026-09-03**.

```text
config/products.json
        │
        ▼
GitHub Actions ──► OAuth refresh ──► Mercado Libre API
        │                                  │
        │                                  ├─ /items/bulk?ids=
        │                                  └─ /items/{id}/sale_price
        ▼
public/data/products.json ──► futura integración Vero & Beth
```

## Decisiones

- El navegador nunca recibe secretos ni llama a OAuth.
- La configuración conserva `affiliate_url`; el permalink de la API nunca lo reemplaza.
- La consulta de ítems usa `/items/bulk?ids=` porque Mercado Libre anunció la retirada del multiget `/items?ids=` para el 25/10/2026.
- El precio se obtiene de `/items/{id}/sale_price?context=channel_marketplace`, porque los campos de precio de `/items` están en retirada progresiva.
- Las imágenes no se descargan: se conserva únicamente la URL HTTPS entregada por la API (`pictures[].secure_url`) como referencia técnica. No debe renderizarse en Vero & Beth hasta confirmar la autorización de uso aplicable.
- La actualización es atómica: si falla cualquier producto, el JSON público anterior queda intacto.
- Los refresh tokens de Mercado Libre son rotativos y de un solo uso. El workflow guarda el nuevo valor en `MELI_REFRESH_TOKEN` mediante un token de GitHub limitado a este repositorio y al permiso **Secrets: write**.
- La salida es determinista. `updated_at` solo cambia cuando cambian los datos públicos del producto; así GitHub no recibe commits vacíos cada 12 horas.

## Alcance de afiliados

La ayuda vigente del Programa de Afiliados y Creadores de México permite productos de Deportes y Fitness, pero excluye suplementos alimenticios. Por ello, el MVP rechaza las categorías `suplementos`, `proteina`, `creatina` y `electrolitos`. Esta regla debe revisarse si Mercado Libre actualiza el programa.

Los términos permiten contenido promocional en sitios web propios, pero responsabilizan al afiliado de contar con los derechos o autorizaciones de imágenes de terceros. La documentación técnica confirma que la API devuelve `pictures[].secure_url`; no encontramos una autorización general explícita para reutilizar automáticamente esas fotografías fuera del marketplace. El campo `image` se genera para comprobar la integración, no como aprobación jurídica para publicarlo.

Las reglas también prohíben datos falsos o desactualizados y anuncios pagos en buscadores dirigidos al programa. Esto coincide con el alcance del proyecto: contenido útil, actualización periódica y tráfico orgánico.

## Fuentes oficiales

- [Autenticación, PKCE y renovación de tokens](https://developers.mercadolibre.com.mx/es_ar/como-empezar/autenticacion-y-autorizacion)
- [Ítems, búsquedas y migración a bulk](https://developers.mercadolibre.com.mx/es_ar/manejo-de-pagos/items-y-busquedas)
- [API vigente de precios](https://developers.mercadolibre.com.mx/api-de-precios)
- [Rate limit y error 429](https://developers.mercadolibre.com.mx/es_ar/como-empezar/rate-limit-error-429)
- [Permisos funcionales](https://developers.mercadolibre.com.mx/es_mx/permisos-funcionales)
- [Productos permitidos para afiliados](https://www.mercadolibre.com.mx/ayuda/30088)
- [Términos del Programa de Afiliados](https://www.mercadolibre.com.mx/ayuda/terminos-condiciones-programa-afiliados_30228)
- [GitHub: crear o actualizar un secreto](https://docs.github.com/en/rest/actions/secrets#create-or-update-a-repository-secret)
