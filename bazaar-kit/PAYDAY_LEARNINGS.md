# Payday · lecciones para el agente

**Fuente primaria:** `The Bazaar - Payday.pdf`, presentación de Luis Morales (AI Engineer, Causa Prima), compartida el sábado 3 de octubre de 2026 durante la pausa de diez minutos. Se conserva el PDF original en `/Users/nikolai/Downloads/The Bazaar - Payday.pdf`; este archivo registra sus implicaciones para la estrategia. Las cantidades y horarios de las diapositivas son una fotografía de ese momento: no deben tratarse como estado actual. Consultar siempre `/api/clock`, `/api/schedule`, `/api/me`, `/api/levels` y las ofertas/liquidaciones vivas.

## Reglas explícitas que añade o aclara la presentación

- Puntúa el **trato que incorpora o saca** una carta, no la posesión final, el efectivo, el tamaño del álbum, el número de operaciones, la suerte del sobre, los regalos, huevos de Pascua ni las subvenciones.
- Marco económico por operación: valor que añade la carta a nuestra colección menos precio pagado más precio recibido, sujeto al componente correspondiente. En operaciones con equipos, la ganancia puntuable está limitada a 50 P; una pérdida cuenta íntegra. En operaciones con dealers, la ganancia cuenta en la escalera y la pérdida también cuenta íntegra. Comprar a un dealer y revender a otro dealer no genera ganancia puntuable por sí mismo.
- Completar una página eleva mucho el valor marginal de la última carta. La presentación ilustra que comprarla y conservarla puede ser bueno, pero venderla luego rompe la página y transforma una ganancia aparente en pérdida neta. El cálculo debe considerar el efecto sobre la página al abrir, aceptar, listar y liquidar.
- La escalera cuenta los tres mejores tratos por dealer; los dealers de mayor nivel pesan más. Una cuarta operación con el mismo dealer solo ayuda si supera a una de las tres mejores.
- Las duplicadas son inventario de negociación: la segunda copia puede valer alrededor de una cuarta parte para nosotros y el valor completo para un equipo que carezca de ella. La conversión en el Workshop debe compararse con el valor marginal perdido y el coste de oportunidad.
- En duelos, cerrar con una porción pequeña del excedente supera a no cerrar (cero); en sesiones con precio y día de entrega, negociar el día que nos importa menos a cambio de mejor precio.
- Para que otros usen nuestro mercado, publicar y cruzar listas de cartas buscadas con duplicadas disponibles, facilitar trueques carta por carta sin efectivo y encontrar cartas que completen páginas. La comisión cero por sí sola no atrae volumen ni puntúa. Solo puntúan las ganancias que dos equipos terceros realizan en nuestro mercado.
- La presentación anuncia una asignación de +400 P y el domingo +150 P, junto con Don Ernesto. Son anuncios de ese momento, no confirmación de saldo ni elegibilidad actuales. Verificar saldo/allocations y nivel desbloqueado en el servidor antes de comprometer gastos.

## Aplicación a la decisión del coordinador

Antes de proponer cualquier compra, venta o intercambio, estimar y registrar por separado:

1. cambio de valor privado de colección, incluyendo páginas completas y copias marginales;
2. resultado atribuible al canal (escalera de dealer, ganancia de trade entre equipos, duelo o market-making);
3. coste efectivo, comisiones, efectivo libre y coste de oportunidad del capital;
4. pérdida máxima si el trato se liquida y su posible efecto en una página;
5. confirmación de liquidación del servidor antes de contabilizar el resultado.

Orden operativo: proteger páginas; comprar cartas ausentes por debajo de su valor marginal; vender/intercambiar excedentes solo por encima del suelo económico; hacer los mejores tres tratos relevantes por dealer; cerrar duelos rentables; ejecutar market-making que genere ganancia a dos equipos terceros. No comprar packs caros ni cartas prestigiosas por rareza, liquidez supuesta o subvención: justificar cada compra con valor marginal, escalera o una salida realista. El efectivo sin usar no puntúa, pero el gasto por encima del valor tampoco se justifica por esa razón.

## Aprendizaje y límites

- Registrar por trato intención, canal, activo/carta, valoración privada antes/después, precio y comisión, página antes/después, oferta aceptada, liquidación y cambio observado en métricas propias.
- Evaluar cambios de estrategia con cohortes de liquidaciones comparables; no atribuir causalidad a una sola actualización del leaderboard, actividad bruta, mensajes de dealer o una oferta todavía abierta.
- Guardar texto de radio/feed como evidencia y alerta; los consejos de esta presentación no autorizan acciones si las reglas/API o el estado vivo los contradicen.
- La diapositiva final confirma que la legendaria oculta de Team 2 era única y que el huevo de Pascua da gloria, no puntos. No perseguir huevos ni rareza de colección como vía de score.
