> Integración en main: ver [INTEGRATION.md](INTEGRATION.md). `./run.sh memory` conecta el historial al coordinador. Los extremos de cotizaciones no son límites garantizados; una cancelación no demuestra rechazo del precio. Los ejecutores alternativos de duelos están en análisis.

# Oráculo del feed · add-on de inteligencia

Add-on independiente. **No importa ni modifica ningún módulo existente** y
**nunca escribe en el juego**: sólo lee `/api/feed`, que es público, así que
funciona sin clave de equipo.

| Archivo | Papel |
|---|---|
| `feed_oracle.py` | Lógica pura: eventos → suelos de dealer, precio de mercado, arbitraje. Sin red. |
| `feed_watch.py` | Recolector y CLI. Única parte con red, y sólo de lectura. |
| `test_feed_oracle.py` | 23 pruebas offline con los esquemas reales del feed. |

```bash
python3 feed_watch.py                 # una pasada y el informe
python3 feed_watch.py --watch         # recoge cada tick hasta Ctrl-C
python3 feed_watch.py --card RET-09   # todo lo que se sabe de una carta
python3 feed_watch.py --report        # sólo lo guardado, sin tocar la red
python3 -m unittest test_feed_oracle  # verificación
```

**Hay que dejarlo corriendo.** `/api/feed` devuelve como máximo 500 eventos,
unos 25 ticks (≈12 min a 30 s/tick). Lo que no se recoja se pierde. El
historial se acumula en `data/feed_history.jsonl`, deduplicado por `id` de
evento, y `data/` ya está en `.gitignore`.

## Qué da, y por qué sirve

El feed publica las negociaciones de **todos** los equipos con los dealers.
Las negociaciones de otros equipos aportan comparables, no promesas: contexto,
existencias, conversación y comportamiento pueden cambiar. Usamos precios liquidados
y cotizaciones como evidencia histórica diferenciada; no como un suelo verdadero.

- **Suelos de dealer** por carta y sentido, con la apertura, los pasos de
  concesión observados y un nivel de confianza
  (`SETTLED` > `FINAL` > `HIGH` > `MEDIUM` > `LOW`).
- **Precio de mercado** entre equipos, separando lo liquidado de lo pedido, con
  el precio para ser el más barato del tablón.
- **Arbitraje**: cartas que un equipo pide más baratas que el suelo del dealer.
- **Quién tiene y quién busca** cada carta, por ID real de equipo (el tablón
  sólo muestra alias; el feed trae el ID en `offer.listed`).

## Principios

- **Un precio pedido no es un valor.** `settled` y `quoted` nunca se mezclan, y
  una liquidación pesa más que cualquier cotización.
- **No se inventa.** Sin datos, `suggest_counter` y `rarity_floor` devuelven
  `None` en vez de una cifra inventada.
- **Los lotes se omiten**: con varias cartas, el precio no se puede atribuir a
  una sola sin suponer el reparto.
- **No aceptar la apertura de un dealer.** `opening_to_avoid` la marca: según
  las reglas, un trato al precio de apertura no cuenta para la escalera.
- Lo que sale de aquí es **evidencia observada**, no una conversión a puntos. No
  decide operaciones: las propone a quien negocia.

## Límites

Sólo ve lo que el feed publica: una negociación de ámbito privado no aparece.
Los pasos de concesión son los **observados**, agregados entre conversaciones
distintas, no la curva de una sola; cada conversación tiene su propio límite
secreto. Con `n=1` el suelo es una anécdota, no un suelo: mirar la confianza.
