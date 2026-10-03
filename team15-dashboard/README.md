# Mesa de trades en vivo · Team 15

Este panel amplía el dashboard público del PR #2. Lee su feed y sus perfiles de rivales, y combina esas señales con `/api/me`, ofertas activas y el modelo de valoración verificado de `bazaar-kit/trading.py`. **Sólo hace GET**; no envía ofertas ni acepta tratos.

```bash
export BAZAAR_KEY='la-clave-del-equipo-15'
python3 team15-dashboard/app.py
# Abrir http://127.0.0.1:8775
```

La clave se lee del entorno del proceso o del archivo indicado por `--env-file`, y nunca entra en la página. Sin clave, el panel muestra posibles compradores a partir de duplicados observados en el feed, con el inventario libre y el valor privado marcados como desconocidos. Para cambiar la reserva de efectivo: `--reserve 80` (predeterminado: 100 P). El servidor escucha sólo en `127.0.0.1`.

Si ya hay un recolector de feed y un `.env` en otro checkout, reutilízalos sin copiarlos: `--feed-root /ruta/a/bazaar-kit --env-file /ruta/a/bazaar-kit/.env`. El parser del `.env` lee texto; no lo ejecuta.

## Qué muestra

- Inventario t15, duplicados libres y valor marginal que perderíamos al dar uno.
- Guía **por cada duplicado libre**: mínimo rentable (pérdida marginal + 2 P, redondeado hacia arriba), precio sugerido cuando hay un comprador observado, todos los equipos interesados y valor neto esperado. Si nadie pidió la carta, sólo muestra el mínimo y no inventa un comprador.
- Margen de concesión por carta: diferencia entre precio sugerido y mínimo rentable si negociamos una propuesta que acepte el rival. Las pujas activas se valoran con su comisión real y no se presentan como precios negociables.
- Radio del feed con titular, momento, coincidencia con nuestros duplicados y decisión prudente. Una noticia sobre raras de Malasaña, por ejemplo, no altera el precio de nuestras comunes de ese barrio. Los rumores con plazo anunciado se marcan como vencidos después de ese plazo.
- Vista de **todo el catálogo** con mapa por colección, estado de cada carta, filtros por situación y rareza, y orden por ventas, escasez, valor de compra o señales. Muestra stock propio, copias libres, mínimo de venta, máximo de compra de la siguiente copia y equipos observados/interesados.
- Por carta, precios de ventas **liquidadas** de una sola carta por efectivo: mediana, rango, cantidad y tres precios recientes. Las ofertas sin liquidar y los lotes no cuentan como ventas individuales. La escasez usa copias acuñadas / tirada publicadas, además de la rareza; una baja proporción acuñada no demuestra que hoy haya pocas cartas ofertadas.
- La compra visible respeta tanto el límite de valor privado como el efectivo disponible tras la reserva, e identifica cuál de los dos limita el tope. No presupone un vendedor dispuesto ni incluye comisiones de una oferta concreta.
- Cartas que nos faltan y valor privado de recibirlas; incluye el bono de completar página cuando corresponde.
- Por equipo: referencias observadas en su mano, demandas declaradas y cartas ofrecidas. La ausencia de una referencia en el feed no se interpreta como que no la tenga.
- Ranking de ventas de duplicados, primero ofertas **activas** que t15 podría aceptar, luego propuestas dirigidas y por último canjes alternativos. Muestra oferta, venue, precio, comisión y `ΔU = efectivo neto + cambio en valor de colección`.
- Propuestas de venta dirigidas basadas en una demanda declarada, marcadas como condicionales. El precio toma el mayor entre nuestro mínimo rentable (valor perdido + 2 P), la referencia de mercado del feed y una puja histórica de ese equipo. Las referencias históricas no se presentan como ofertas vigentes.
- Hasta tres posibles canjes por equipo cuando ese equipo pide uno de nuestros duplicados y ofrece una carta que nos falta. Se calcula el cambio de valor de nuestra colección; la aceptación del rival sigue siendo incierta.
- Puntos actuales que devuelve `/api/me`. La regla pública confirma que el valor ganado en trades a precios privados contribuye a la puntuación de negociación (30 % del total); no publica una conversión exacta de `ΔU` a puntos del leaderboard, por lo que el panel ordena por ganancia neta y marca los puntos futuros como desconocidos.

El alias del maker en un libro público sólo se asocia con un equipo si existe un evento del feed que une **el mismo ID de oferta** con ese equipo. Ofertas anónimas sin esa prueba no reciben una atribución inventada.

El ranking se desactiva si el valor calculado de nuestra colección difiere del que devuelve `/api/me`, si la clave no es de t15 o si no hay datos privados. La página se actualiza cada 15 segundos; el selector de equipo y la pausa sobreviven al refresco.

Todas las lecturas del panel comparten una separación mínima de 0,3 segundos,
incluidas las públicas y los libros. Otros procesos siguen compartiendo el límite
del servidor y pueden provocar `rate_limited`. Se rechazan redirecciones HTTP
para no reenviar la clave a otro destino.

```bash
python3 -m unittest discover -s team15-dashboard -p 'test_*.py' -q
python3 -m unittest discover -s bazaar-kit -p 'test_*.py' -q
```


## Integración para agentes

El endpoint `GET /api/strategy` expone el mismo conocimiento que usa la interfaz en JSON: KPIs, ratios globales, campos por carta, decisiones sugeridas y definiciones de cada señal. Consulta [STRATEGY.md](STRATEGY.md) para las fórmulas y límites de interpretación.


## Auditoría y siguiente versión

La auditoría extrema y el roadmap de V2 están en [AUDIT_NEXT_VERSION.md](AUDIT_NEXT_VERSION.md).
