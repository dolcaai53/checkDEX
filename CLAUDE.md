ZADANI PRO CLAUDE CODE

Role:
Act as a Senior Developer with knowledge of Python, API, crpyotworld.
Make a simple and clear code.
Dont modify code by your own decision because you think that it should be like this.

Workflow:
1. analyze
2. show suggestions
3. implement
4. tests
5. everytime verify if your job was really completed
6. push and commit to github - https://github.com/dolcaai53/checkDEX.git


Cil
Vytvor produkcne pouzitelny Python projekt pro notifikacni system trading aktivit na DEXu Extended Exchange s durazem na cistou architekturu, spolehlivost, snadnou rozsiritelnost na dalsi DEXy a provoz primarne v Dockeru.

Projekt bude v prvni fazi implementovan pro Extended Exchange. Pozdeji ma jit stejnou architekturou rozsirit o dalsi DEXy jako Lighter a Hyperliquid.

System ma kontrolovat pres API a pripadne websockety stav obchodniho uctu, zejmena:
- otevrene ordery,
- zmeny orderu,
- partial fill a full fill orderu,
- otevrene pozice,
- zmeny pozic,
- uzavrene pozice/obchody,
- profit nebo ztratu po uzavreni obchodu.

Tyto udalosti ma system odesilat do Telegramu.

Dulezite vychozi pravidlo
Projekt ma byt read-only monitoring system. Nema zadavat ani rusit ordery. Ma pouze sledovat data uctu, vyhodnocovat udalosti a posilat notifikace.

Technologicky ramec
- Jazyk: Python
- Pozadovana verze: preferovane Python 3.11 nebo 3.12, ale zachovat kompatibilitu minimalne s Python 3.10
- Pouzit ofiialni Extended Python SDK: x10-python-trading
- Pouzit asyncio
- Pouzit pydantic pro konfiguraci a datove modely tam, kde to dava smysl
- Pouzit sqlite nebo obdobne jednoduche persistentni uloziste pro stav a deduplikaci notifikaci
- Pouzit standardni logging
- Projekt navrhnout tak, aby slo snadno testovat a rozsirovat

Architektura projektu
Chci plnohodnotny projekt, ne jeden skript.

Navrhni strukturu napr. takto:
- app/main.py                -> entrypoint aplikace
- app/config.py              -> konfigurace z environment variables
- app/exchanges/base.py      -> abstraktni interface pro libovolnou burzu
- app/exchanges/extended.py  -> implementace adapteru pro Extended
- app/notifiers/telegram.py  -> Telegram notifier
- app/services/monitor.py    -> hlavni monitorovaci smycka a event engine
- app/storage/               -> persistence state
- app/models/                -> interni modely order/trade/position/event
- app/utils/                 -> pomocne utility
- tests/                     -> priprava na testy

Docker-first pozadavek
Cele reseni musi byt navrzeno jako Docker-first aplikace. Docker neni doplnek, ale hlavni zpusob provozu a nasazeni.

Povinne dodaj:
- Dockerfile
- docker-compose.yml
- .dockerignore
- README s navodem na build a run v Dockeru

Pozadavky na Docker provoz:
- aplikace musi jit spustit jako dlouhodobe bezici kontejner
- musi mit restart policy
- musi mit healthcheck — implementuj jako file-based touch:
    aplikace kazdych 30 sekund aktualizuje soubor /tmp/healthy
    Docker healthcheck v Dockerfile:
      HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
        CMD test -f /tmp/healthy && test $(($(date +%s) - $(date +%s -r /tmp/healthy))) -lt 60
- musi umet graceful shutdown — zachytit SIGTERM a SIGINT pres asyncio signal handlery:
    loop.add_signal_handler(SIGTERM, shutdown)
    loop.add_signal_handler(SIGINT, shutdown)
    pri shutdownu dokoncit bezici polling cyklus, zapsat stav do DB a teprve pak ukoncit proces
- konfigurace musi jit predavat pres .env nebo env variables
- persistentni stav musi byt ulozen mimo kontejner pres volume nebo bind mount
- po restartu nebo redeploy nesmi dojit ke ztrate stavu ani k opakovanemu odeslani starych notifikaci
- vysvetli v README doporuceny zpusob update image bez ztraty state

Funkcni pozadavky
1. Monitoring otevrenych orderu
System musi umet nacitat otevrene ordery z Extended a drzet jejich aktualni snapshot.

Kdyz se objevi novy order, posli Telegram notifikaci typu:
- ORDER OPENED

Do zpravy zahrn minimalne:
- exchange
- market
- order ID
- side
- type
- price
- qty
- filled qty, pokud je k dispozici
- timestamp

2. Monitoring zmen orderu
System musi detekovat zmeny orderu, zejmena:
- partial fill
- full fill
- cancel
- reject
- jina podstatna zmena statusu nebo vyplneneho mnozstvi

Kdyz se zmeni stav orderu, posli Telegram notifikaci typu:
- ORDER UPDATED
nebo
- ORDER FILLED
podle povahy udalosti.

3. Monitoring pozic
System musi umet nacitat aktualne otevrene pozice a porovnavat jejich stav s predchozim snapshotem.

Musis detekovat:
- nova pozice otevrena
- zmena velikosti pozice
- navyseni nebo snizeni pozice
- volitelne zmenu unrealized PnL nad definovany threshold (pouze pokud je UNREALIZED_PNL_THRESHOLD_USDC nastaven a zmena tuto hodnotu prekroci)

Trigger pravidla pro POSITION_UPDATED:
- POSITION_UPDATED se odesila POUZE pri zmene size pozice (navyseni nebo snizeni)
- Zmena unrealized PnL sama o sobe NESPOUSTI notifikaci — mark price se meni neustale a odesani pri kazde zmene by bylo spam
- Unrealized PnL alert je separatni volitelna funkce ridena hodnotou UNREALIZED_PNL_THRESHOLD_USDC; pokud neni nastavena, unrealized PnL se ve zprave uvadi informativne, ale alert se nevysila

Telegram notifikace:
- POSITION OPENED
- POSITION UPDATED

Do zpravy zahrn minimalne:
- exchange
- market
- side
- size
- leverage, pokud je k dispozici
- open price
- mark price, pokud to dava smysl
- unrealized PnL, pokud jde o update pozice

4. Monitoring uzavrenych obchodu
System musi detekovat uzavreni pozice/obchodu.

Jakmile je pozice uzavrena, zjisti a posli do Telegramu:
- market
- side
- size
- entry price
- exit price
- realised PnL
- PnL v procentech, pokud to lze rozumne a konzistentne spocitat
- dobu trvani obchodu
- cas uzavreni obchodu

Telegram notifikace:
- POSITION CLOSED

5. Vyhodnoceni profit/ztrata
Pri uzavreni obchodu vyhodnot, zda slo o profit, loss nebo breakeven.

Pravidla:
- pokud realised_pnl > 0 -> PROFIT
- pokud realised_pnl < 0 -> LOSS
- pokud realised_pnl == 0 -> BREAKEVEN

Telegram zpravy a vizualni odliseni
Telegram Bot API nepodporuje vlastni barvy textu stylem HTML/CSS color. Proto nechci implementovat neplatne HTML formatovani typu span style color.

Misto toho chci vizualni odliseni takto:
- profit musi byt ve zprave oznacen zelenym vyznamem pomoci emoji a labelu: 🟢 PROFIT
- ztrata musi byt ve zprave oznacena cervenym vyznamem pomoci emoji a labelu: 🔴 LOSS
- breakeven musi byt neutralni: ⚪ BREAKEVEN

Konretni pozadavek pro close zpravy:
- pokud je obchod ziskovy, zprava ma zacinat napr.:
  🟢 <b>POSITION CLOSED — PROFIT</b>
- pokud je obchod ztratovy, zprava ma zacinat napr.:
  🔴 <b>POSITION CLOSED — LOSS</b>
- pokud je obchod na nule, zprava ma zacinat napr.:
  ⚪ <b>POSITION CLOSED — BREAKEVEN</b>

PnL radek ma byt zretelny:
- profit napr. +157.38 USDC
- loss napr. -92.14 USDC
- pri pouziti HTML parse mode zvyrazni PnL aspon pomoci <b> tagu

Pouzivej pouze Telegramem podporovane formatovani, napr.:
- <b>
- <i>
- <code>
- <pre>
- <a>

Nepouzivej neplatne HTML tagy nebo inline CSS.

Priklad zpravy pro profit:
🟢 <b>POSITION CLOSED — PROFIT</b>
Exchange: Extended
Market: BTC-USD
Side: LONG
Size: 0.25
Entry: 63250.5
Exit: 63880.0
PnL: <b>+157.38 USDC</b>
PnL %: <b>+2.49%</b>
Duration: 01h 42m
Closed at: 2026-05-07 13:42:11 UTC

Priklad zpravy pro ztratu:
🔴 <b>POSITION CLOSED — LOSS</b>
Exchange: Extended
Market: BTC-USD
Side: LONG
Size: 0.25
Entry: 63250.5
Exit: 62880.0
PnL: <b>-92.14 USDC</b>
PnL %: <b>-1.46%</b>
Duration: 00h 37m
Closed at: 2026-05-07 13:42:11 UTC

Extended integrace
Odkazy na dokumentaci (viz DOC.md):
- API dokumentace: https://api.docs.extended.exchange/#extended-api-documentation
- Python SDK (x10-python-trading): https://github.com/x10xchange/python_sdk

Pred implementaci si prostuduj SDK README a overit:
- ktere API klice jsou nutne pro read-only volani (get_open_orders, get_positions, atd.)
- zda SDK ma vestaveny rate limiting nebo je nutne ho resit manualne
- jak se inicializuje klient pro mainnet vs testnet
- zda je k dispozici websocket klient a jake eventy publikuje

Pouzij jako primarni zdroj dat ofiialni Extended Python SDK a pokud to bude vhodne, dopln i websocket-assisted monitoring.

Preferovane zdroje dat:
- get_open_orders()
- get_orders_history()
- get_positions()
- get_positions_history()
- get_trades()

Navrhni robustni pristup:
- polling mode jako vychozi, spolehlivy baseline
- websocket-assisted mode jako volitelny realtime doplnek pro rychlejsi detekci order updates, pokud to pujde rozumne implementovat

Pokud websocket cast nebude jednoznacna nebo dobre zdokumentovana, implementuj konzervativni fallback pres polling a tuto skutecnost jasne popis v README.

Event engine a stavova logika
Navrhni interni event engine, ktery bude porovnavat predchozi a aktualni stav.

Musis resit alespon:
- predchozi snapshot open orders vs aktualni snapshot
- predchozi snapshot open positions vs aktualni snapshot
- historii uzavrenych pozic vs posledni zpracovane zaznamy
- historii orderu a/nebo trades pro rozliseni, zda order zmizel kvuli fill, cancel nebo reject

System musi:
- bezpecne prezit restart
- ukladat posledni znamy stav
- mit deduplikaci notifikaci
- znovu neposilat stare close eventy po restartu
- ukladat ID jiz notifikovanych udalosti
- umet detekovat partial fill i full fill
- umet co nejlepe rozlisit duvod zmizeni orderu z open orders

Race condition pri zmizeni orderu — explicitni pravidlo:
Kdyz order zmizi z open_orders mezi dvema polling cykly, postupuj takto:
1. Zkontroluj orders_history za posledni 2 minuty (nebo primerane sirsi okno)
2. Pokud je order nalezen v historii -> urcit duvod (FILLED / CANCELLED / REJECTED) a odeslat notifikaci
3. Pokud order v historii zatim neni (zpozdeni API):
   - uloz order do docasneho "disappeared_pending" stavu
   - opakuj kontrolu v historii v nasledujicich 2 polling cyklech
   - pokud ani po 2 retry neni v historii, oznac jako DISAPPEARED_UNKNOWN, zaloguj warning a odeslat notifikaci s poznamkou "reason unknown"
4. Vsechny tyto stavy ulozit do DB, aby restart nevedl ke ztrate informaci

Persistencni vrstva
Pouzij perzistentni storage, idealne sqlite.

Do persistence ukladej minimalne:
- posledni snapshot orderu
- posledni snapshot pozic
- posledni zpracovana history data
- IDs jiz odeslanych notifikaci
- metadata potrebna pro deduplikaci

Storage musi byt odolne vuci restartu a navrzene pro provoz v Dockeru s persistentnim volume.

Cisteni stare deduplikacni cache:
- zaznamy v tabulce sent_notifications starsi nez NOTIFICATION_DEDUP_TTL_DAYS se automaticky mazi
- cisteni probiha pri startu aplikace a/nebo jednou denne behem bezneho provozu
- bez teto logiky by tabulka rostla donekonecna

Vypocet PnL
Primarni autoritativni hodnota pro realised PnL ma byt prevzata z oficialnich dat, pokud je k dispozici v historii pozic.

Dale:
- zkus dopocitat PnL %
- v kodu i README jasne popis, z jakeho zakladu se PnL % pocita
- pouzij tento konzervativni vzorec jako fallback:
    pnl_pct = (realised_pnl / (entry_price * size)) * 100
  Tento vzorec nepocita s leverage ani fees — v README a ve zprave ho oznac jako "(approx.)" pokud nejsou fees k dispozici
- pokud procento nepujde spolehlive pocitat kvuli chybejicim datum o collateral, leverage nebo fees, tak ho bud vynech nebo oznac jako odhad

Konfigurace
Konfigurace musi jit cist z environment variables.

Navrhni minimalne tyto promenne:

# Extended API — autentizace
- EXTENDED_API_KEY              (povinny)
- EXTENDED_PUBLIC_KEY           (povinny)
- EXTENDED_PRIVATE_KEY          (volitelny — overit, zda ho Extended SDK vyzaduje pro read-only volani; pokud ne, udelej volitelnym a vysvetli v README)
- EXTENDED_VAULT                (povinny — identifikator uctu/vaultu)
- EXTENDED_NETWORK=mainnet      (mainnet nebo testnet)

# Telegram
- TELEGRAM_BOT_TOKEN            (povinny)
- TELEGRAM_CHAT_ID              (povinny)

# Polling intervaly (separate per endpoint group, kvuli rozdilnym rate limitum)
- POLL_INTERVAL_ORDERS_SECONDS=60     # open orders
- POLL_INTERVAL_POSITIONS_SECONDS=60  # open positions
- POLL_INTERVAL_HISTORY_SECONDS=60    # orders/positions history a trades

# Persistence
- STATE_DB_PATH=/data/state.db        # absolutni cesta; v Dockeru mapuj /data jako volume
- NOTIFICATION_DEDUP_TTL_DAYS=30      # jak dlouho uchovavat ID odeslanych notifikaci; starsi zaznamy se automaticky cisti

# Notifikace — zapnout/vypnout jednotlive typy
- ENABLE_ORDER_OPENED=true
- ENABLE_ORDER_UPDATED=true
- ENABLE_ORDER_FILLED=true
- ENABLE_POSITION_OPENED=true
- ENABLE_POSITION_UPDATED=true
- ENABLE_POSITION_CLOSED=true
- ENABLE_STARTUP_NOTIFICATION=true    # odeslat Telegram zpravu pri startu/restartu aplikace

# Thresholds
- UNREALIZED_PNL_THRESHOLD_USDC=      # volitelny; pokud neni nastaven, unrealized PnL alert se nevysila

# Logging
- LOG_LEVEL=INFO
- LOG_FORMAT=json                      # json (vychozi, vhodne pro Docker) nebo text

Poznamky ke konfiguraci:
- EXTENDED_PRIVATE_KEY a podepisovani transakci: overit, zda SDK pro read-only volania (get_open_orders, get_positions, atd.) vyzaduje podpis. Pokud ne, udelej EXTENDED_PRIVATE_KEY volitelnym a v README to vysvetli.
- STATE_DB_PATH je absolutni cesta uvnitr kontejneru; docker-compose volume mapuje /data na host adresar.

Telegram notifier
Implementuj Telegram notifier jako samostatnou komponentu.

Pozadavky:
- podpora parse mode HTML
- deduplikace opakovanych zprav
- retry pri docasnem selhani Telegram API
- ochrana proti spamovani
- prehledne sablony zprav pro jednotlive eventy
- moznost vypnout jednotlive typy notifikaci konfiguraci
- startup notifikace: pokud je ENABLE_STARTUP_NOTIFICATION=true, odeslat pri startu zpravu napr.:
    🟡 <b>checkDEX started</b>
    Exchange: Extended (mainnet)
    Monitoring: orders, positions, trades
    Started at: 2026-05-07 13:00:00 UTC

Error handling a robustnost
Implementuj odolnost vuci:
- timeoutum API
- docasnym vypadkum Extended API
- rate limitum
- HTTP 429 a backoff retry strategii
- vypadku Telegram API
- nekonzistentnim nebo nevalidnim datum z API
- poškozenemu nebo chybnemu lokalnimu state store

Logging a observabilita
Chci kvalitni logging vhodny pro dlouhodoby provoz v Dockeru.

Loguj minimalne:
- start aplikace
- nacteni konfigurace
- inicializaci klientu
- uspesne pripojeni k Extended
- pocet nactenych orderu a pozic
- detekovane eventy
- odeslane Telegram notifikace
- warningy a chyby
- retry a reconnect udalosti
- graceful shutdown

Logy musi byt vhodne pro docker logs.

Format logu:
- vychozi format je JSON (strukturovane logy) — pouzij python-json-logger
- prepinatelny pres LOG_FORMAT=text pro lokalni vyvoj
- vsechny timestampy v logach musi byt UTC, format ISO 8601

Vsechny timestampy v celem projektu (logy, Telegram zpravy, DB zaznamy) musi byt v UTC. Nikdy nepouzivej lokalni casovou zonu.

Rozhrani pro dalsi burzy
Uz ted navrhni projekt tak, aby sel rozsirit na dalsi DEXy, zejmena:
- Lighter
- Hyperliquid

Navrhni jednotne interni modely a eventy:
- OrderOpenedEvent
- OrderUpdatedEvent
- OrderFilledEvent
- PositionOpenedEvent
- PositionUpdatedEvent
- PositionClosedEvent

Kazdy exchange adapter ma prevadet vlastni data na tyto interni modely.

Kodove standardy
Chci:
- cisty a citelny kod
- modularitu
- typove anotace
- oddeleni business logiky od I/O
- zadny monoliticky skript
- pripraveni na unit testy
- rozumne docstringy
- zadne hardcoded secrets

README
Dodaj kvalitni README, ktere popise:
- co projekt dela
- jak funguje Extended integrace
- jak funguje polling a pripadne websocket-assisted rezim
- jak nastavit Telegram bota
- jak vyplnit .env
- jak projekt spustit lokalne
- jak projekt spustit v Dockeru
- jak funguje persistence a deduplikace notifikaci
- jak funguje close event profit/loss rozliseni v Telegramu
- znama omezeni a predpoklady
- co bude potreba doplnit pro Lighter a Hyperliquid

Dodavka
Na konci vygeneruj kompletni projekt vcetne:
- vsech zdrojovych souboru
- requirements.txt nebo pyproject.toml
- .env.example
- Dockerfile
- docker-compose.yml
- .dockerignore
- README.md
- zakladni pripravy na testy — minimalni rozsah testoveho skeletonu:
    tests/test_event_engine.py   -> unit testy pro order diff logiku a position diff logiku s mock daty (zadne volani API)
    tests/test_pnl.py            -> unit testy pro PnL a PnL % vypocet
    tests/test_telegram.py       -> unit testy pro formatovani Telegram zprav (overit spravny HTML output)
    tests/conftest.py            -> sdilene fixtures: mock ExchangeAdapter, mock TelegramNotifier, tmp SQLite DB
    tests/test_storage.py        -> testy deduplikace, ukladani a nacteni snapshotu

Implementacni priority
Postupuj v tomto poradi:
1. vytvor interni modely a base exchange interface
2. implementuj Extended polling connector
3. implementuj persistence state a deduplikaci
4. implementuj Telegram notifier
5. implementuj event detection nad orders, positions a history
6. dopln websocket-assisted update vrstvu, pokud to bude rozumne
7. dopln Docker soubory a README
8. priprav skeleton pro dalsi burzy

Dulezite rozhodovaci pravidlo
Preferuj jednoduchost, spolehlivost a robustnost pred zbytecne komplikovanou architekturou. Pokud nektera cast dokumentace nebude jednoznacna, zvol konzervativni polling fallback, jasne to oznac v README a nevymyslej neoverene implementacni detaily.
