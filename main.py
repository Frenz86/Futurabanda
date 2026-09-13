import difflib
import io
import os
import random
import string
import threading
import unicodedata

import pygame
import speech_recognition as sr
from faster_whisper import WhisperModel
from pgzero.loaders import root as ASSET_ROOT

WIDTH = 1280
HEIGHT = 720
TITLE = "Futura-Banda"

# pgzero overwrites the script's own __file__ global when it merges in its
# builtins, so the project folder is taken from its already-resolved asset
# root instead of os.path.dirname(__file__).
MUSIC_DIR = os.path.join(ASSET_ROOT, "music")
GIOCATORI_DIR = os.path.join(ASSET_ROOT, "giocatori")

# "small" e' il miglior compromesso qualita'/velocita' su CPU per l'italiano;
# al primo avvio faster-whisper scarica il modello da Hugging Face e lo mette
# in cache (~/.cache/huggingface), poi funziona offline.
WHISPER_MODEL_SIZE = "small"
try:
    _modello_whisper = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
except Exception as errore:
    # Niente internet al primo avvio, modello non scaricabile, ecc.: il gioco
    # deve restare giocabile anche senza riconoscimento vocale.
    print(f"Modello Whisper non disponibile, riconoscimento vocale disattivato: {errore}")
    _modello_whisper = None

NUM_DOMANDE = 7
Y_CASELLE = [512 - 58 * i for i in range(NUM_DOMANDE)]
X_COLONNA_P1 = 602
X_COLONNA_P2 = 684

SETUP = "setup"      # schermata iniziale: scelta nome e foto
PARTITA = "partita"  # gioco vero e proprio

ATTESA = "attesa"              # turno in corso, in attesa che il giocatore avvii il brano
RIPRODUZIONE = "riproduzione"  # il brano sta suonando
ASCOLTO = "ascolto"            # in attesa del riconoscimento vocale
FINE = "fine"                  # partita terminata

STATI_TURNO_ATTIVO = {ATTESA, RIPRODUZIONE, ASCOLTO}  # per l'evidenziazione di chi e' in turno
STATI_CONTEGGIO_TEMPO = {RIPRODUZIONE}  # il minuto scorre solo mentre l'mp3 suona, si ferma al click che lo stoppa

ARTICOLI = {"il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "l", "d", "di"}
PAROLA_PASSO = "passo"

TEMPO_LIMITE = 60.0

lock = threading.Lock()


def titolo_di(nome_file):
    return os.path.splitext(nome_file)[0]


def normalizza(testo):
    testo = testo.lower().strip()
    testo = unicodedata.normalize("NFKD", testo)
    testo = "".join(c for c in testo if not unicodedata.combining(c))
    testo = testo.translate(str.maketrans("", "", string.punctuation))
    parole = [p for p in testo.split() if p not in ARTICOLI]
    return " ".join(parole)


def somiglianza(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def risposta_corretta(risposta, titolo):
    if not risposta:
        return False
    r = normalizza(risposta)
    t = normalizza(titolo)
    if not r or not t:
        return False
    if r == t or t in r or r in t:
        return True
    return somiglianza(r, t) >= 0.72


def e_comando_passo(risposta):
    # "passo" e' una parola breve: il riconoscimento vocale la trascrive a
    # volte in modo impreciso (es. "basso", "passi"). Tollera piccole
    # imprecisioni cosi' come gia' facciamo per i titoli delle canzoni.
    normalizzato = normalizza(risposta)
    if not normalizzato:
        return False
    if PAROLA_PASSO in normalizzato.split():
        return True
    return somiglianza(normalizzato, PAROLA_PASSO) >= 0.75


def cerca_foto_disponibili():
    if not os.path.isdir(GIOCATORI_DIR):
        return []
    estensioni = (".jpg", ".jpeg", ".png")
    return sorted(f for f in os.listdir(GIOCATORI_DIR) if f.lower().endswith(estensioni))


FOTO_DISPONIBILI = cerca_foto_disponibili()
LATO_FOTO = 150
_cache_foto = {}


def superficie_foto(nome_file, lato=LATO_FOTO):
    chiave = (nome_file, lato)
    if chiave not in _cache_foto:
        percorso = os.path.join(GIOCATORI_DIR, nome_file)
        originale = pygame.image.load(percorso).convert_alpha()
        _cache_foto[chiave] = pygame.transform.smoothscale(originale, (lato, lato))
    return _cache_foto[chiave]


class Giocatore:
    def __init__(self, numero, x_colonna, foto_indice):
        self.numero = numero
        self.nome = f"Giocatore {numero}"
        self.foto_indice = foto_indice % len(FOTO_DISPONIBILI) if FOTO_DISPONIBILI else 0
        self.coda = []
        self.risolte = 0
        self.corrette = 0
        self.tempo_rimanente = TEMPO_LIMITE
        self.caselle = []
        for y in Y_CASELLE:
            casella = Actor("neutra")
            casella.x = x_colonna
            casella.y = y
            self.caselle.append(casella)

    def foto_attuale(self):
        if not FOTO_DISPONIBILI:
            return None
        return FOTO_DISPONIBILI[self.foto_indice]

    def prossima_foto(self):
        if FOTO_DISPONIBILI:
            self.foto_indice = (self.foto_indice + 1) % len(FOTO_DISPONIBILI)

    def pronto(self):
        return self.risolte < NUM_DOMANDE

    def puo_giocare(self):
        return self.risolte < NUM_DOMANDE and self.tempo_rimanente > 0

    def canzone_corrente(self):
        return self.coda[0][1]

    def salta_corrente(self):
        self.coda.append(self.coda.pop(0))

    def risolvi_corrente(self, indovinato):
        posizione_originale, _ = self.coda.pop(0)
        self.caselle[posizione_originale].image = "corretta" if indovinato else "errata"
        self.risolte += 1
        if indovinato:
            self.corrette += 1

    def resetta(self, canzoni):
        self.coda = list(enumerate(canzoni))
        self.risolte = 0
        self.corrette = 0
        self.tempo_rimanente = TEMPO_LIMITE
        for casella in self.caselle:
            casella.image = "neutra"


giocatori = [Giocatore(1, X_COLONNA_P1, 0), Giocatore(2, X_COLONNA_P2, 1)]

fase = SETUP
campo_attivo = None  # indice del giocatore il cui nome si sta modificando (o None)

RECT_FOTO = [Rect((260, 190), (LATO_FOTO, LATO_FOTO)), Rect((870, 190), (LATO_FOTO, LATO_FOTO))]
RECT_NOME = [Rect((205, 400), (260, 50)), Rect((815, 400), (260, 50))]
RECT_GIOCA = Rect((540, 620), (200, 64))

LATO_FOTO_PARTITA = 340
RECT_FOTO_PARTITA = [
    Rect((40, 120), (LATO_FOTO_PARTITA, LATO_FOTO_PARTITA)),  # Giocatore 1: sinistra
    Rect((WIDTH - 40 - LATO_FOTO_PARTITA, 120), (LATO_FOTO_PARTITA, LATO_FOTO_PARTITA)),  # Giocatore 2: destra, in parallelo
]

ATTESA_MASSIMA_ASCOLTO = 15.0  # se il thread di riconoscimento si blocca (es. driver audio in stallo), lo si abbandona dopo questo tempo

turno_corrente = 0
stato = ATTESA
testo = ""
risultato_ascolto = None
in_attesa_risultato = False
ascolto_id = 0
tempo_in_ascolto = 0.0


def pesca_canzoni_partita():
    disponibili = [f for f in os.listdir(MUSIC_DIR) if f.lower().endswith(".mp3")]
    scelte = random.sample(disponibili, min(2 * NUM_DOMANDE, len(disponibili)))
    meta = len(scelte) // 2
    return scelte[:meta], scelte[meta:]


def nuova_partita():
    global turno_corrente, stato, testo
    canzoni_p1, canzoni_p2 = pesca_canzoni_partita()
    giocatori[0].resetta(canzoni_p1)
    giocatori[1].resetta(canzoni_p2)
    turno_corrente = 0
    stato = ATTESA
    testo = f"Turno di {giocatori[0].nome}: clicca per ascoltare il brano 1 di {NUM_DOMANDE}"


def avvia_partita():
    global fase, campo_attivo
    fase = PARTITA
    campo_attivo = None
    nuova_partita()


def tempo_impiegato(giocatore):
    return TEMPO_LIMITE - giocatore.tempo_rimanente


def classifica():
    """Ordina i giocatori per numero di canzoni indovinate (piu' e' meglio) e,
    a parita', per tempo impiegato (meno e' meglio)."""
    return sorted(giocatori, key=lambda g: (-g.corrette, tempo_impiegato(g)))


def vincitore():
    """Restituisce il Giocatore vincitore, o None in caso di pareggio (stesse
    canzoni indovinate nello stesso tempo)."""
    primo, secondo = classifica()
    if primo.corrette == secondo.corrette and tempo_impiegato(primo) == tempo_impiegato(secondo):
        return None
    return primo


def messaggio_finale():
    g1, g2 = giocatori
    vinc = vincitore()
    esito = f"Vince {vinc.nome}!" if vinc is not None else "Pareggio!"
    return f"{esito} ({g1.corrette}-{g2.corrette})"


def prossimo_turno():
    # Come agli scacchi: una canzone a turno, si alterna sempre all'altro
    # giocatore (se puo' ancora giocare). Il minuto di ciascuno resta pero'
    # cumulativo su tutte le sue 7 canzoni: non si resetta mai tra un turno
    # e l'altro, scorre solo mentre e' la sua canzone a suonare/essere ascoltata.
    altro = 1 - turno_corrente
    if giocatori[altro].puo_giocare():
        return altro
    if giocatori[turno_corrente].puo_giocare():
        return turno_corrente
    return None


def avanza_turno(messaggio):
    global stato, testo, turno_corrente

    prossimo = prossimo_turno()
    if prossimo is None:
        music.stop()
        stato = FINE
        testo = f"{messaggio} - {messaggio_finale()}"
    elif prossimo == turno_corrente:
        stato = ATTESA
        prossimo_g = giocatori[turno_corrente]
        testo = f"{messaggio} - Clicca per il prossimo brano ({prossimo_g.risolte + 1}/{NUM_DOMANDE})"
    else:
        music.stop()
        turno_corrente = prossimo
        prossimo_g = giocatori[turno_corrente]
        stato = ATTESA
        testo = (
            f"{messaggio} - Turno di {prossimo_g.nome}: "
            f"clicca per il brano ({prossimo_g.risolte + 1}/{NUM_DOMANDE})"
        )


def ascolta_risposta(mio_id):
    global risultato_ascolto
    risposta = ""
    try:
        if _modello_whisper is not None:
            recognizer = sr.Recognizer()
            with sr.Microphone() as source:
                recognizer.adjust_for_ambient_noise(source, duration=0.6)
                audio = recognizer.listen(source, timeout=6, phrase_time_limit=8)
            wav_bytes = io.BytesIO(audio.get_wav_data())
            segmenti, _info = _modello_whisper.transcribe(
                wav_bytes, language="it", beam_size=1, vad_filter=True
            )
            risposta = " ".join(segmento.text.strip() for segmento in segmenti).strip()
    except sr.WaitTimeoutError:
        pass
    except Exception as errore:
        # Un thread in background che si blocca su un'eccezione non gestita
        # lascerebbe il gioco bloccato per sempre in attesa di una risposta
        # che non arrivera' mai: qualunque problema (microfono, audio, whisper)
        # deve degradare a "non ho capito" invece di bloccare il turno.
        print(f"Riconoscimento vocale fallito: {errore}")
    with lock:
        # se nel frattempo il turno e' scaduto/cambiato, questo risultato e' obsoleto
        if mio_id == ascolto_id:
            risultato_ascolto = (risposta, [risposta] if risposta else [])


def valuta_risposta(risposta_pronunciata, alternative):
    global stato, testo

    giocatore = giocatori[turno_corrente]
    candidati = alternative or ([risposta_pronunciata] if risposta_pronunciata else [])

    if any(e_comando_passo(c) for c in candidati):
        giocatore.salta_corrente()
        esito = "Brano saltato, ci torneremo più avanti"
    else:
        titolo_corretto = titolo_di(giocatore.canzone_corrente())
        indovinato = any(risposta_corretta(c, titolo_corretto) for c in candidati)
        giocatore.risolvi_corrente(indovinato)

        if indovinato:
            sounds.moseca.play()
            esito = f'Esatto! Era "{titolo_corretto}"'
        else:
            sounds.no.play()
            if risposta_pronunciata:
                esito = f'Hai detto "{risposta_pronunciata}", era "{titolo_corretto}"'
            else:
                esito = f'Non ho capito la risposta, era "{titolo_corretto}"'

    avanza_turno(esito)


def disegna_titolo(y=45, fontsize=46):
    screen.draw.text(
        "FUTURA-BANDA", center=(WIDTH // 2, y), fontsize=fontsize,
        color="white", owidth=1.5, ocolor=(139, 10, 7),
    )


def disegna_setup():
    screen.fill((18, 4, 40))
    disegna_titolo(y=50, fontsize=50)
    screen.draw.text(
        "Prepara i giocatori: scegli foto e nome, poi premi GIOCA",
        center=(WIDTH // 2, 105), fontsize=24, color="white", owidth=1, ocolor="black",
    )

    for i, giocatore in enumerate(giocatori):
        rf = RECT_FOTO[i]
        rn = RECT_NOME[i]

        screen.draw.text(
            f"GIOCATORE {giocatore.numero}", center=(rf.centerx, rf.top - 25),
            fontsize=26, color="white", owidth=1, ocolor="black",
        )

        foto = giocatore.foto_attuale()
        if foto:
            screen.blit(superficie_foto(foto), rf.topleft)
        else:
            screen.draw.filled_rect(rf, (60, 60, 60))
            screen.draw.text("?", center=rf.center, fontsize=60, color="white")
        screen.draw.rect(rf, "white")
        screen.draw.text(
            "clicca la foto per cambiarla", center=(rf.centerx, rf.bottom + 18),
            fontsize=16, color="white", owidth=0.5, ocolor="black",
        )

        attivo = campo_attivo == i
        screen.draw.filled_rect(rn, (40, 40, 70) if not attivo else (70, 70, 110))
        screen.draw.rect(rn, "yellow" if attivo else "white")
        etichetta = giocatore.nome
        if attivo and (pygame.time.get_ticks() // 500) % 2 == 0:
            etichetta += "|"
        screen.draw.text(etichetta, center=rn.center, fontsize=24, color="white")

    screen.draw.filled_rect(RECT_GIOCA, (20, 120, 40))
    screen.draw.rect(RECT_GIOCA, "white")
    screen.draw.text("GIOCA", center=RECT_GIOCA.center, fontsize=34, color="white")


def disegna_scheda_giocatore(giocatore, rf, attivo):
    foto = giocatore.foto_attuale()
    if foto:
        screen.blit(superficie_foto(foto, LATO_FOTO_PARTITA), rf.topleft)
    else:
        screen.draw.filled_rect(rf, (60, 60, 60))
        screen.draw.text("?", center=rf.center, fontsize=60, color="white")

    bordo = "yellow" if attivo else "white"
    screen.draw.rect(rf, bordo)
    if attivo:
        screen.draw.rect(Rect((rf.left - 2, rf.top - 2), (rf.width + 4, rf.height + 4)), bordo)

    screen.draw.text(
        giocatore.nome, center=(rf.centerx, rf.bottom + 22),
        fontsize=24, color=bordo, owidth=1, ocolor="black",
    )

    secondi = max(0, int(giocatore.tempo_rimanente))
    if secondi <= 10 and giocatore.puo_giocare():
        colore_tempo = "red"
    else:
        colore_tempo = "yellow" if attivo else "white"
    screen.draw.text(
        f"{secondi:02d}s", center=(rf.centerx, rf.bottom + 52),
        fontsize=30, color=colore_tempo, owidth=1.2, ocolor="black",
    )


def disegna_partita():
    screen.fill((18, 4, 40))
    screen.blit("7x30", (0, 0))
    for giocatore in giocatori:
        for casella in giocatore.caselle:
            casella.draw()

    for i, giocatore in enumerate(giocatori):
        attivo = stato in STATI_TURNO_ATTIVO and turno_corrente == i
        disegna_scheda_giocatore(giocatore, RECT_FOTO_PARTITA[i], attivo)

    disegna_titolo()

    g1, g2 = giocatori
    avanzamento = (
        f"{g1.nome}: {g1.risolte}/{NUM_DOMANDE} (punti {g1.corrette})     "
        f"{g2.nome}: {g2.risolte}/{NUM_DOMANDE} (punti {g2.corrette})"
    )
    screen.draw.text(
        avanzamento, center=(WIDTH // 2, 95), fontsize=26,
        color="white", owidth=1, ocolor="black",
    )

    screen.draw.text(
        testo, center=(WIDTH // 2, 655), fontsize=28,
        color="white", owidth=1.2, ocolor="black", width=WIDTH - 60,
    )


def disegna_fine():
    screen.fill((18, 4, 40))
    disegna_titolo(y=55, fontsize=50)

    g1, g2 = giocatori
    vinc = vincitore()

    if vinc is not None:
        screen.draw.text(
            f"VINCE {vinc.nome.upper()}!", center=(WIDTH // 2, 155), fontsize=60,
            color="gold", owidth=2, ocolor=(139, 10, 7),
        )
        lato = 300
        rettangolo = Rect((WIDTH // 2 - lato // 2, 215), (lato, lato))
        foto = vinc.foto_attuale()
        if foto:
            screen.blit(superficie_foto(foto, lato), rettangolo.topleft)
        else:
            screen.draw.filled_rect(rettangolo, (60, 60, 60))
        screen.draw.rect(rettangolo, "gold")
        bordo_esterno = Rect(
            (rettangolo.left - 4, rettangolo.top - 4),
            (rettangolo.width + 8, rettangolo.height + 8),
        )
        screen.draw.rect(bordo_esterno, "gold")
    else:
        screen.draw.text(
            "PAREGGIO!", center=(WIDTH // 2, 155), fontsize=60,
            color="white", owidth=2, ocolor=(139, 10, 7),
        )
        lato = 200
        for i, g in enumerate(giocatori):
            centro_x = WIDTH // 2 + (i * 2 - 1) * (lato // 2 + 30)
            rettangolo = Rect((centro_x - lato // 2, 235), (lato, lato))
            foto = g.foto_attuale()
            if foto:
                screen.blit(superficie_foto(foto, lato), rettangolo.topleft)
            else:
                screen.draw.filled_rect(rettangolo, (60, 60, 60))
            screen.draw.rect(rettangolo, "white")

    screen.draw.text(
        f"{g1.nome}: {g1.corrette} corrette su {NUM_DOMANDE}     "
        f"{g2.nome}: {g2.corrette} corrette su {NUM_DOMANDE}",
        center=(WIDTH // 2, 565), fontsize=28, color="white", owidth=1, ocolor="black",
    )

    screen.draw.text(
        "CLASSIFICA", center=(WIDTH // 2, 605), fontsize=24,
        color="gold", owidth=1, ocolor="black",
    )
    for posizione, giocatore in enumerate(classifica(), start=1):
        riga = (
            f"{posizione}. {giocatore.nome} - {giocatore.corrette} indovinate "
            f"in {tempo_impiegato(giocatore):.1f}s"
        )
        screen.draw.text(
            riga, center=(WIDTH // 2, 605 + posizione * 26), fontsize=22,
            color="white", owidth=1, ocolor="black",
        )

    screen.draw.text(
        "Clicca per tornare alla schermata iniziale",
        center=(WIDTH // 2, 605 + (len(giocatori) + 1) * 26 + 20), fontsize=26,
        color="white", owidth=1, ocolor="black",
    )


def draw():
    if fase == SETUP:
        disegna_setup()
    elif stato == FINE:
        disegna_fine()
    else:
        disegna_partita()


def update(dt):
    global risultato_ascolto, in_attesa_risultato, ascolto_id, tempo_in_ascolto

    if fase != PARTITA:
        return

    if stato in STATI_CONTEGGIO_TEMPO:
        giocatore = giocatori[turno_corrente]
        giocatore.tempo_rimanente = max(0.0, giocatore.tempo_rimanente - dt)
        if giocatore.tempo_rimanente <= 0:
            ascolto_id += 1
            in_attesa_risultato = False
            risultato_ascolto = None
            avanza_turno(f"Tempo scaduto per {giocatore.nome}!")
            return

    if in_attesa_risultato:
        tempo_in_ascolto += dt
        if tempo_in_ascolto > ATTESA_MASSIMA_ASCOLTO:
            # il thread di riconoscimento non ha risposto in tempo utile (es. driver
            # audio bloccato): lo si abbandona invece di restare fermi per sempre.
            ascolto_id += 1
            in_attesa_risultato = False
            risultato_ascolto = None
            valuta_risposta("", [])
            return

        with lock:
            pronto = risultato_ascolto is not None
            risultato = risultato_ascolto
        if pronto:
            in_attesa_risultato = False
            risultato_ascolto = None
            risposta, alternative = risultato
            valuta_risposta(risposta, alternative)


def nome_predefinito(giocatore):
    return f"Giocatore {giocatore.numero}"


def disattiva_campo_nome():
    global campo_attivo
    if campo_attivo is not None:
        giocatore = giocatori[campo_attivo]
        if not giocatore.nome.strip():
            giocatore.nome = nome_predefinito(giocatore)
    campo_attivo = None


def gestisci_click_setup(pos):
    global campo_attivo

    for i in range(2):
        if RECT_FOTO[i].collidepoint(pos):
            giocatori[i].prossima_foto()
            return
        if RECT_NOME[i].collidepoint(pos):
            disattiva_campo_nome()
            campo_attivo = i
            if giocatori[i].nome == nome_predefinito(giocatori[i]):
                giocatori[i].nome = ""
            return

    if RECT_GIOCA.collidepoint(pos):
        disattiva_campo_nome()
        avvia_partita()
        return

    disattiva_campo_nome()


def on_mouse_down(pos):
    global stato, testo, in_attesa_risultato, ascolto_id, tempo_in_ascolto, fase

    if fase == SETUP:
        gestisci_click_setup(pos)
        return

    if stato == FINE:
        fase = SETUP
        return

    giocatore = giocatori[turno_corrente]

    if stato == ATTESA:
        music.play(giocatore.canzone_corrente())
        stato = RIPRODUZIONE
        testo = f"{giocatore.nome}: ascolta... clicca quando hai riconosciuto il brano"

    elif stato == RIPRODUZIONE:
        music.stop()
        sounds.ding.play()
        stato = ASCOLTO
        testo = (
            f"{giocatore.nome}, parla ora e di' il titolo della canzone "
            f"(o di' \"{PAROLA_PASSO}\" per saltarla)..."
        )
        in_attesa_risultato = True
        tempo_in_ascolto = 0.0
        ascolto_id += 1
        threading.Thread(target=ascolta_risposta, args=(ascolto_id,), daemon=True).start()


def on_key_down(key, unicode):
    if fase != SETUP or campo_attivo is None:
        return

    giocatore = giocatori[campo_attivo]

    if key == keys.BACKSPACE:
        giocatore.nome = giocatore.nome[:-1]
    elif key in (keys.RETURN, keys.KP_ENTER, keys.ESCAPE, keys.TAB):
        disattiva_campo_nome()
    elif unicode and unicode.isprintable() and len(giocatore.nome) < 16:
        giocatore.nome += unicode
