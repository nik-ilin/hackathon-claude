# Mesa de trades en vivo · Team 15

Este panel amplía el dashboard público del PR #2. Lee su feed y sus perfiles de rivales, y combina esas señales con `/api/me`, ofertas activas y el modelo de valoración verificado de `bazaar-kit/trading.py`. **Sólo hace GET**; no envía ofertas ni acepta tratos.

```bash
export BAZAAR_KEY='la-clave-del-equipo-15'
python3 team15-dashboard/app.py
# Abrir http://127.0.0.1:8775
```

La clave se lee del entorno del proceso y nunca entra en la página. Sin clave, el panel muestra el feed y avisa de que no puede calcular nuestra mano ni el ranking privado. Para cambiar la reserva de efectivo: `--reserve 80` (predeterminado: 100 P). El servidor escucha sólo en `127.0.0.1`.

Si ya hay un recolector de feed y un `.env` en otro checkout, reutilízalos sin copiarlos: `--feed-root /ruta/a/bazaar-kit --env-file /ruta/a/bazaar-kit/.env`. El parser del `.env` lee texto; no lo ejecuta.

## Qué muestra

- Inventario t15, duplicados libres y valor marginal que perderíamos al dar uno.
- Cartas que nos faltan y valor privado de recibirlas; incluye el bono de completar página cuando corresponde.
- Por equipo: referencias observadas en su mano, demandas declaradas y cartas ofrecidas. La ausencia de una referencia en el feed no se interpreta como que no la tenga.
- Ranking de ofertas **activas** que t15 podría aceptar, con oferta, venue, precio, comisión y `ΔU = efectivo neto + cambio en valor de colección`.
- Propuestas de venta dirigidas basadas en una demanda declarada, marcadas como condicionales. El precio toma el mayor entre nuestro mínimo rentable (valor perdido + 2 P), la referencia de mercado del feed y una puja histórica de ese equipo. Las referencias históricas no se presentan como ofertas vigentes.
- Hasta tres posibles canjes por equipo cuando ese equipo pide uno de nuestros duplicados y ofrece una carta que nos falta. Se calcula el cambio de valor de nuestra colección; la aceptación del rival sigue siendo incierta.
- Puntos actuales que devuelve `/api/me`. Los puntos futuros por operación se indican como desconocidos: la regla pública describe el componente de negociación, pero no da una conversión exacta de `ΔU` a puntos del leaderboard.

El alias del maker en un libro público sólo se asocia con un equipo si existe un evento del feed que une **el mismo ID de oferta** con ese equipo. Ofertas anónimas sin esa prueba no reciben una atribución inventada.

El ranking se desactiva si el valor calculado de nuestra colección difiere del que devuelve `/api/me`, si la clave no es de t15 o si no hay datos privados. La página se actualiza cada 15 segundos; el selector de equipo y la pausa sobreviven al refresco.

```bash
python3 -m unittest discover -s team15-dashboard -p 'test_*.py' -q
python3 -m unittest discover -s bazaar-kit -p 'test_*.py' -q
```
