# MyAgent

*[English](README.md) · **Italiano***

**La tua workstation AI personale.**

Gira in locale. Funziona offline. Controlla i tuoi dispositivi. Risponde da
una biblioteca che è tua. Continua a funzionare quando tutto il resto si
ferma.

![MyAgent — un agente AI locale in un case rugged, silenzioso e affidabile](docs/images/myagent-case.jpg)

## Non è l'ennesima chat AI

La maggior parte degli assistenti AI è un frontend per un modello nel cloud.
MyAgent trasforma un computer qualsiasi in un **sistema AI autosufficiente**,
dove il modello linguistico è solo un componente:

```text
         Modello locale
               │
  Biblioteca di conoscenza offline
               │
        Agenti autonomi
               │
       ┌───────┴───────┐
       │               │
  Tool locali    Dispositivi IoT
       │               │
       └───────┬───────┘
               │
        Il tuo computer
```

Nessun account, nessun abbonamento, nessuna telemetria, nessun vincolo a un
fornitore. Se preferisci puoi puntarlo a un'API remota — è una scelta, non una
dipendenza.

## Costruito per la resilienza

Internet è opzionale: la tua conoscenza sta su dischi tuoi, e i tuoi
dispositivi restano sulla tua rete. Il tuo assistente continua a funzionare
durante

- blackout e guasti di rete che durano più di un giorno,
- disastri ed emergenze — i riferimenti medici, di primo soccorso, di
  riparazione e di agricoltura stanno su un disco tuo, e continuano a
  rispondere alle domande,
- posti isolati — una barca, un camper, un rifugio in montagna, una stazione
  da campo,
- laboratori, officine e sedi di clienti dove collegarsi non è permesso,
- case, studi e ambulatori dove domande e documenti non devono uscire
  dall'edificio.

Quando la rete sparisce, l'assistente resta.

## La biblioteca offline

Invece di chiedere a internet ogni volta, gli agenti cercano in una collezione
tua: **Wikipedia completa offline** insieme a medicina, primo soccorso,
manuali di riparazione, agricoltura ed elettronica — più i tuoi appunti
Markdown, i PDF convertiti e la documentazione tecnica. Decine o centinaia di
gigabyte su un disco tuo, cercati full-text in un attimo, citati con la fonte
([la biblioteca](library/README.md)).

![L'agente Bibliotecario risponde dalla biblioteca offline](docs/images/chat-librarian.png)

*Un modello locale che risponde dalla biblioteca offline: il Bibliotecario
cerca, apre il risultato migliore e risponde da lì. Niente in questo percorso
tocca la rete.*

## AI autonoma

MyAgent non si limita a rispondere alle domande. Gli agenti eseguono task
programmati, controllano e avvisano, si programmano il lavoro futuro,
comandano i dispositivi, delegano ad altri agenti e ricordano quello che hanno
fatto — anche mentre non ci sei ([agenti autonomi](docs/AUTONOMY.md)).

## La privacy è l'architettura

La privacy non è una funzione da accendere: è l'architettura di default.
Nessun account, nessun cloud, nessuna analisi d'uso, nemmeno una chiamata a
casa per gli aggiornamenti. Conversazioni, documenti e comandi ai dispositivi
restano su hardware che controlli tu, e l'unico traffico verso l'esterno è
quello che accendi esplicitamente.

## Filosofia

MyAgent non è l'ennesimo chatbot. Punta a essere un **ambiente operativo AI
personale**: un assistente che vive sul tuo hardware, impara i tuoi documenti,
usa i tuoi strumenti, comanda i tuoi dispositivi e continua a funzionare per
anni. Anche quando internet no.

## Caratteristiche

| Funzionalità | Descrizione |
| ------------ | ----------- |
| **Qualsiasi backend LLM** | llama.cpp, Ollama, qualsiasi API compatibile OpenAI e l'API Anthropic, parlata nativamente; la finestra di contesto viene *sondata*, non indovinata |
| **Pensato per i modelli locali piccoli** | chiamate ai tool interpretate dal testo puro per i modelli senza function calling nativo, protezione dai loop, ritentativi sulle chiamate malformate ([perché](docs/DESIGN.md)) |
| **Biblioteca offline** | archivi ZIM di Wikipedia e i tuoi documenti in `~/myagent/library/`, cercati full-text ([dettagli](library/README.md)) |
| **Agenti atomici** | un agente è solo `modello + prompt di sistema + tool`, modificabile dalla UI e salvato come file JSON |
| **Agenti autonomi** | task programmati, esecuzioni non presidiate, agenti che si programmano il lavoro futuro e ti avvisano ([dettagli](docs/AUTONOMY.md)) |
| **Delega tra agenti** | un agente ne chiama un altro, con permessi per agente |
| **Memoria a lungo termine** | opzionale: i turni vecchi vengono archiviati e sostituiti da riassunti compatti, così un agente ricorda senza far esplodere il contesto di un modello piccolo |
| **I tool sono cartelle** | un `tool.json` più un `run` eseguibile in qualsiasi linguaggio, ricaricato a caldo, senza riavvii; l'AI può scriverne di nuovi ([dettagli](docs/TOOLS.md)) |
| **File nella chat** | i tool consegnano immagini, pagine HTML e download nella conversazione per riferimento, mai attraverso il modello; l'agente HTML Designer costruisce così pagine e report autocontenuti ([dettagli](docs/TOOLS.md#returning-files-to-the-user-resources)) |
| **Server MCP** | server via stdio o HTTP si aggiungono all'elenco dei tool; incolla una configurazione di Claude Desktop per importarli ([dettagli](docs/MCP.md)) |
| **IoT e domotica** | gli agenti chiamano le API HTTP locali dei tuoi dispositivi (Home Assistant, Shelly, Tasmota, ESPHome, Hue …) sulla LAN ([dettagli](docs/AGENTS.md#local-devices--home-automation)) |
| **Chat dal vivo** | streaming dei token, generazione in background che puoi lasciare e riprendere, pulsante di stop, cronologia, rigenerazione, modifica del prompt |
| **Modelli che ragionano** | il ragionamento viene separato mentre scorre e mostrato richiuso: non finisce mai nella risposta, nel prompt successivo o in un altoparlante |
| **Telegram e voce** | collega un agente a un bot Telegram ([connettori](connectors/README.md)) o a un satellite vocale, con il riconoscimento vocale sul tuo server ([satellite](satellite/README.md)) |
| **UI installabile** | si installa come app e viene messa in cache, quindi si apre anche senza rete; inglese e italiano inclusi |

## Partenza rapida

Servono **Python 3.10+** e **un backend LLM**. Se non hai né l'uno né l'altro,
[Ollama](https://ollama.com) è la strada più breve:

```bash
ollama pull qwen3          # va bene qualsiasi modello capace di usare i tool

git clone https://github.com/speleoalex/myagent.git
cd myagent
./setup.sh
server/.venv/bin/python server/main.py
```

Apri **<http://127.0.0.1:8888>**, scegli un agente e scrivi. `setup.sh` ti dice
quale backend ha trovato e ti offre di installare le parti opzionali che
mancano, e MyAgent risponde con il modello locale che risulta raggiungibile —
quindi il primo messaggio funziona prima ancora che tu abbia configurato
qualcosa.

Poi riempi la biblioteca, che è ciò che lo rende utile offline:

```bash
server/.venv/bin/pip install libzim       # serve per gli archivi .zim
library/fetch.py --list                   # il catalogo, con le dimensioni attuali
library/fetch.py --lang it --preset base  # set iniziale, ~1,7 GB con l'italiano
```

Requisiti completi, dipendenze opzionali, esecuzione come servizio e risoluzione
dei problemi: **[docs/INSTALL.md](docs/INSTALL.md)**.

## Sicurezza

> **MyAgent include tool che eseguono comandi shell come utente del server, e
> non c'è nessuna sandbox.** Di default l'API non ha autenticazione e ascolta su
> `127.0.0.1` — lascialo così, a meno che la rete non sia fidata. Prima di
> esporlo, imposta una chiave API in *Impostazioni → Chiave API* (o fissane una
> con `MYAGENT_API_KEY`); su http in chiaro, tieni il traffico dentro una VPN
> oppure lascia che MyAgent serva HTTPS da solo. Considera l'accesso all'API
> equivalente a una shell sulla macchina.

Modello di minaccia, dove stanno i segreti e come esporlo in sicurezza:
**[docs/SECURITY.md](docs/SECURITY.md)**.

## Documentazione

> La documentazione tecnica è **in inglese**. Questa pagina è l'unica tradotta.

### Metterlo in funzione

- [Installazione](docs/INSTALL.md) — requisiti, dipendenze opzionali, servizio,
  installazione della UI come app, hosting altrove, risoluzione dei problemi
- [Configurazione](docs/CONFIGURATION.md) — variabili d'ambiente, struttura di
  `~/myagent/`, cosa salvare nei backup
- [Sicurezza](docs/SECURITY.md) — modello di minaccia, chiave API, trasporto,
  segreti

### Usarlo

- [La biblioteca](library/README.md) — quale conoscenza offline vale la pena
  avere, come scaricarla, come la cercano gli agenti
- [Agenti e dispositivi](docs/AGENTS.md) — gli agenti inclusi, il form
  dell'agente, come collegare la domotica
- [Agenti autonomi](docs/AUTONOMY.md) — task programmati, agenti live,
  protezioni
- [Server MCP](docs/MCP.md) — come aggiungerli, concederli, i loro limiti
- [Connettori](connectors/README.md) — bot Telegram, canali, rubrica
- [Satellite vocale](satellite/README.md) — il client microfono/altoparlante

### Capirlo ed estenderlo

- [Scelte di progetto](docs/DESIGN.md) — perché è fatto così, e cosa manca di
  proposito
- [Scrivere tool](docs/TOOLS.md) — anatomia di un tool, il contratto di `run`,
  un esempio completo
- [Scrivere plugin](docs/PLUGINS.md) — il contratto dei plugin, le regole di
  isolamento
- [Architettura](docs/ARCHITECTURE.md) — flusso di una richiesta, executor,
  provider, rilevamento della finestra di contesto, storage

## Contribuire

Issue e pull request sono benvenute. Mantieni lo stack noioso: libreria standard
Python + FastAPI sul backend, JS puro + Bootstrap sul frontend, nessun passaggio
di build.

## Crediti

Ideato e realizzato da **Alessandro Vernassa**
([@speleoalex](https://github.com/speleoalex)).

## Licenza

[MIT](LICENSE). La UI include [Bootstrap](https://getbootstrap.com) e Bootstrap
Icons (MIT).
